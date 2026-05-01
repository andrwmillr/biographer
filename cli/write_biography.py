#!/usr/bin/env python3
"""Phase B: synthesize a 15-year narrative from the corpus.

Walks _corpus/notes/{journal,creative,poetry,letter}/, groups notes by era,
hands every note's full body in chronological order to Claude for an era
chapter. Each chapter is written sequentially with prior chapters as
context for continuity. Then writes a top-level summary conditioned on
the chapters.

Output: _corpus/claude/biographies/biography_<stamp>.md plus a
`biography.md` symlink to the latest.

Flags:
  --model {opus-4.6,opus-4.7,sonnet-4.6} — pick model (default opus-4.7)
  --future                               — also feed each draft any later eras'
                                            chapters/digests already on disk from
                                            a previous run (hindsight context).
                                            Off by default; breaks inter-era
                                            prefix caching when on.

The data layer (paths, note loading, era resolution, model registry,
chapter system prompt, prompt builder) lives in `corpus.py`. This
module only contains CLI orchestration: output paths, post-processing
(quote verification + citation resolution), and the asyncio entry
point."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from pathlib import Path

from anthropic import AsyncAnthropic

# Reuse the data layer.
from core.corpus import (
    BIOGRAPHIES_DIR,
    CHAPTER_SYSTEM,
    ERAS,
    EDITOR_NOTE_PREFIX,
    MODEL,
    MODELS,
    SUBJECT_NAME,
    apply_authorship,
    apply_date_overrides,
    apply_note_metadata,
    build_user_msg,
    era_heading,
    era_of,
    flag_date_clusters,
    load_authorship,
    load_corpus_notes,
    load_future_chapters,
    load_future_thread_digests,
    load_prior_chapters,
    load_prior_thread_digests,
    parse_note_body,
)


START = time.time()


def ts():
    elapsed = int(time.time() - START)
    return f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"


def log(msg):
    print(f"{ts()} {msg}", flush=True)


# ---------------------------------------------------------------------------
# CLI flags + run-output paths
# ---------------------------------------------------------------------------

def _pick_future() -> bool:
    return "--future" in sys.argv


INCLUDE_FUTURE = _pick_future()


RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_MD = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}.md"
SYSTEM_SNAPSHOT = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}_system.md"
USERS_SNAPSHOT = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}_users.md"
LATEST_SYMLINK = BIOGRAPHIES_DIR / "biography.md"


def _prompt_sha():
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _is_dirty():
    try:
        out = subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return bool(out)
    except Exception:
        return False


MAX_RETRIES = 8


# ---------------------------------------------------------------------------
# Summary system prompt (CLI-only; web flow doesn't run the summary step)
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """You are writing the opening synthesis to a retrospective on the subject's personal writing archive. Plain third-person voice — a thoughtful biographer, not a literary critic. The user message will identify the subject and the period covered.

You'll receive every era chapter the retrospective contains. Write 250-450 words covering the through-lines: what preoccupied them across the whole archive, what changed, what didn't, what the arc looks like from a distance.

VOICE
Plain, direct English. Short sentences welcome. Write so a friend can follow. Third person — refer to the subject by name and use the pronouns the chapters establish. No literary-critic jargon ("revisionary sentence", "ars poetica", "the register", "signature gesture", etc.). No ornate sentence structures for their own sake.

ABSOLUTE RULES
- Work only from what the chapters say. Don't introduce new specifics, names, dates, or quotes.
- If you use a quote or specific detail from a chapter, keep any [YYYY-MM-DD] citation it carries.
- No invented imagery. Abstract patterns are fine without citation; specifics need the source chapter's anchor.
- No "in conclusion", no enumerating the era names, no "this retrospective".
- Third person for the subject. No sentimentalizing.
- If being accurate and plain makes the paragraph thinner, that's fine."""


# ---------------------------------------------------------------------------
# Quote verification + citation resolution (CLI post-processing)
# ---------------------------------------------------------------------------

def normalize_text(s):
    return re.sub(r"\s+", " ", s.lower()).strip()


def extract_quotes(text):
    """Return all quoted passages in the chapter:
    - inline: text between paired double-quote chars
    - block: consecutive lines prefixed with '> ' (markdown blockquote)
    Both forms should appear verbatim in the source notes."""
    out = []
    positions = [i for i, c in enumerate(text) if c == '"']
    pairs = list(zip(positions[::2], positions[1::2]))
    for a, b in pairs:
        content = text[a + 1 : b].strip()
        if len(content) < 10:
            continue
        if re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
            continue
        out.append(content)
    block = []
    for line in text.splitlines():
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            block.append(m.group(1))
        else:
            if block:
                content = " ".join(block).strip()
                if len(content) >= 10 and not re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
                    out.append(content)
                block = []
    if block:
        content = " ".join(block).strip()
        if len(content) >= 10 and not re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
            out.append(content)
    return out


def verify_quotes(chapter_text, era_notes, corpus_id=None):
    """Extract quoted strings from chapter text; flag any that don't appear verbatim
    in any era note body or title. Titles are included because the prompt
    encourages quoted-noun references like `a piece called "the afternoon sucks"`,
    which would otherwise pollute the unverified list."""
    quotes = extract_quotes(chapter_text)
    haystacks = []
    for n in era_notes:
        body = parse_note_body(n["rel"], corpus_id)
        if body:
            haystacks.append(body)
        title = n.get("title")
        if title:
            haystacks.append(title)
    combined = normalize_text("\n".join(haystacks))
    unverified = [q for q in quotes if normalize_text(q) not in combined]
    return quotes, unverified


CITATION_RE = re.compile(
    r"\[\s*(\d{4}-\d{2}-\d{2}(?:\s*,\s*\d{4}-\d{2}-\d{2})*)\s*\]"
)

# New format: [cited phrase](YYYY-MM-DD). The phrase is captured but not
# used by the resolver — the date alone determines which note is linked.
LINK_CITATION_RE = re.compile(
    r"\[([^\]]+)\]\((\d{4}-\d{2}-\d{2})\)"
)


def _preceding_quote(text, end_pos, lookback=800):
    """Find the quoted passage (block or inline) immediately before end_pos."""
    start = max(0, end_pos - lookback)
    snippet = text[start:end_pos]
    lines = snippet.splitlines()
    block = []
    for line in reversed(lines):
        if re.match(r"^\s*>\s?", line):
            block.append(re.sub(r"^\s*>\s?", "", line))
        elif block:
            break
        elif line.strip() == "":
            continue
        else:
            break
    if block:
        return " ".join(reversed(block)).strip()
    matches = list(re.finditer(r'"([^"]{10,})"', snippet))
    if matches:
        return matches[-1].group(1).strip()
    return None


def resolve_citations(body, all_notes, corpus_id=None):
    """Rewrite citations as Obsidian wikilinks. Handles two formats:
      - Old: [YYYY-MM-DD] -> [[rel|YYYY-MM-DD]]
      - New: [phrase](YYYY-MM-DD) -> [[rel|phrase]]
    When multiple notes share a date, disambiguate via the preceding quote
    (old format) or the link's own phrase (new format)."""
    by_date = {}
    for n in all_notes:
        d = (n.get("date") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(n)

    body_cache = {}
    def body_of(rel):
        if rel not in body_cache:
            body_cache[rel] = normalize_text(parse_note_body(rel, corpus_id) or "")
        return body_cache[rel]

    def pick(date, quote):
        cands = by_date.get(date, [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if quote:
            needle = normalize_text(quote)[:80]
            if needle:
                for n in cands:
                    if needle in body_of(n["rel"]):
                        return n
        return sorted(cands, key=lambda n: n["rel"])[0]

    spans = []
    for m in LINK_CITATION_RE.finditer(body):
        spans.append((m.start(), m.end(), "link", m))
    for m in CITATION_RE.finditer(body):
        s, e = m.span()
        if any(ls <= s < le for ls, le, _, _ in spans):
            continue  # already inside a [phrase](date) link
        spans.append((s, e, "old", m))
    spans.sort(key=lambda x: x[0])

    out = []
    last = 0
    resolved = unresolved = 0
    for s, e, kind, m in spans:
        out.append(body[last:s])
        if kind == "link":
            phrase = m.group(1)
            d = m.group(2)
            note = pick(d, phrase)
            if note:
                rel_no_ext = re.sub(r"\.md$", "", note["rel"])
                out.append(f"[[{rel_no_ext}|{phrase}]]")
                resolved += 1
            else:
                out.append(f"[{phrase}]({d})")
                unresolved += 1
        else:
            dates = [d.strip() for d in m.group(1).split(",")]
            preceding = _preceding_quote(body, s)
            links = []
            for d in dates:
                note = pick(d, preceding)
                if note:
                    rel_no_ext = re.sub(r"\.md$", "", note["rel"])
                    links.append(f"[[{rel_no_ext}|{d}]]")
                    resolved += 1
                else:
                    links.append(f"[{d}]")
                    unresolved += 1
            out.append(", ".join(links))
        last = e
    out.append(body[last:])
    return "".join(out), resolved, unresolved


# ---------------------------------------------------------------------------
# Async chapter / summary writers + main()
# ---------------------------------------------------------------------------

async def write_chapter(client, era_name, notes, prior_chapters_list=None, prior_digests_list=None,
                        future_chapters_list=None, future_digests_list=None):
    era_msg = build_user_msg(era_name, notes)
    label = f"{era_name}  ({len(notes)} notes"

    # Split prior/future chapters and digests across separate content blocks
    # with the cache marker on the LAST block before era_msg. Within a single
    # request the cache write covers the full reference prefix; across eras,
    # past-only sections have a stable monotone prefix, but enabling --future
    # introduces per-era variation that breaks inter-era prefix caching.
    sections = []  # (open_marker, close_marker, items)
    prior_chapters_list = prior_chapters_list or []
    prior_digests_list = prior_digests_list or []
    future_chapters_list = future_chapters_list or []
    future_digests_list = future_digests_list or []
    if prior_chapters_list:
        sections.append((
            "--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n",
            "--- END PRIOR CHAPTERS ---\n\n",
            prior_chapters_list,
        ))
    if prior_digests_list:
        sections.append((
            "--- PRIOR THREAD DIGESTS (structured per-era state — read alongside the prior chapters) ---\n\n",
            "--- END PRIOR THREAD DIGESTS ---\n\n",
            prior_digests_list,
        ))
    if future_chapters_list:
        sections.append((
            "--- FUTURE CHAPTERS (later eras, drafted in a previous run — for thematic alignment, NOT for events that haven't happened yet in this era; do not foreshadow or anticipate) ---\n\n",
            "--- END FUTURE CHAPTERS ---\n\n",
            future_chapters_list,
        ))
    if future_digests_list:
        sections.append((
            "--- FUTURE THREAD DIGESTS (later eras' digests — same caveat: hindsight context, not events to anticipate) ---\n\n",
            "--- END FUTURE THREAD DIGESTS ---\n\n",
            future_digests_list,
        ))

    user_blocks = []
    for s_idx, (open_m, close_m, items) in enumerate(sections):
        is_last_section = s_idx == len(sections) - 1
        user_blocks.append({"type": "text", "text": open_m})
        for i, item in enumerate(items):
            block = {"type": "text", "text": item + "\n\n"}
            if is_last_section and i == len(items) - 1:
                block["cache_control"] = {"type": "ephemeral"}
            user_blocks.append(block)
        user_blocks.append({"type": "text", "text": close_m})
    user_blocks.append({"type": "text", "text": era_msg})

    in_chars = sum(len(b["text"]) for b in user_blocks)
    log(f"→ {label}, {in_chars:,} chars in)")
    t0 = time.time()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": CHAPTER_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_blocks}],
    )
    dt = time.time() - t0
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"\A##\s+[^\n]+\n+", "", text)
    u = resp.usage
    in_tok = getattr(u, "input_tokens", 0)
    out_tok = getattr(u, "output_tokens", 0)
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    words = len(text.split())
    stop = getattr(resp, "stop_reason", "")
    truncated = " ⚠ TRUNCATED (max_tokens)" if stop == "max_tokens" else ""
    cache_str = f" [cache: {cache_read:,} hit / {cache_write:,} write]" if (cache_read or cache_write) else ""
    log(f"✓ {era_name}  done in {dt:.0f}s  {words} words  (in: {in_tok:,} tok, out: {out_tok:,} tok){cache_str}{truncated}")
    return era_name, text, len(notes)


async def write_summary(client, chapters, by_era):
    from core.corpus import subject_context_for
    parts = []
    for era_name, text, _ in chapters:
        parts.append(f"## {era_heading(era_name, by_era[era_name])}\n\n{text}")
    user_msg = subject_context_for() + "ERA CHAPTERS:\n\n" + "\n\n".join(parts)
    log(f"→ summary  starting  ({len(user_msg):,} chars in)")
    t0 = time.time()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=1536,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    dt = time.time() - t0
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    in_tok = getattr(resp.usage, "input_tokens", 0)
    out_tok = getattr(resp.usage, "output_tokens", 0)
    log(f"✓ summary  done in {dt:.0f}s  {len(text.split())} words  (in: {in_tok:,} tok, out: {out_tok:,} tok)")
    return text


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    log(f"model: {MODEL}")
    log(f"future context: {'on' if INCLUDE_FUTURE else 'off'}")
    all_notes = load_corpus_notes()
    n_redated = apply_date_overrides(all_notes)
    verdicts = load_authorship()
    all_notes, n_dropped, n_mixed = apply_authorship(all_notes, verdicts)
    log(f"authorship: dropped {n_dropped} clippings, flagged {n_mixed} mixed notes ({len(verdicts)} verdicts loaded)")
    log(f"date overrides: {n_redated}")
    n_about = apply_note_metadata(all_notes)
    if n_about:
        log(f"note-about overrides: {n_about}")
    n_uncertain = flag_date_clusters(all_notes)
    if n_uncertain:
        log(f"date clusters: flagged {n_uncertain} notes (≥3 sharing a date)")
    by_era = {name: [] for name, _, _ in ERAS}
    skipped_date = 0
    for n in all_notes:
        e = era_of(n.get("date", ""))
        if e is None:
            skipped_date += 1
            continue
        by_era[e].append(n)

    log(f"loaded {len(all_notes)} corpus notes ({skipped_date} dropped for missing date)")
    for name, _, _ in ERAS:
        log(f"  {name}: {len(by_era[name])} notes")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)

    log("")
    eras_with_notes = [name for name, _, _ in ERAS if by_era[name]]
    log(f"writing {len(eras_with_notes)} chapters sequentially, each seeing prior chapters…")

    sha = _prompt_sha()
    dirty = "-dirty" if _is_dirty() else ""
    snapshot_header = (
        f"<!-- prompt_sha: {sha}{dirty}  run: {RUN_STAMP}  "
        f"model: {MODEL} -->\n\n"
    )
    BIOGRAPHIES_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_SNAPSHOT.write_text(snapshot_header + CHAPTER_SYSTEM, encoding="utf-8")
    users_parts = [snapshot_header]
    for name in eras_with_notes:
        msg = build_user_msg(
            name, by_era[name],
            prior_digests=load_prior_thread_digests(name),
            future_chapters=load_future_chapters(name) if INCLUDE_FUTURE else None,
            future_digests=load_future_thread_digests(name) if INCLUDE_FUTURE else None,
        )
        users_parts.append(f"=== {name} ===\n\n{msg}\n\n")
    USERS_SNAPSHOT.write_text("".join(users_parts), encoding="utf-8")
    log(f"prompt snapshot: {SYSTEM_SNAPSHOT.name} ({len(CHAPTER_SYSTEM):,} chars system)")
    log(f"prompt snapshot: {USERS_SNAPSHOT.name} ({sum(len(p) for p in users_parts):,} chars users)")

    t_chapters = time.time()

    chapters = []
    for name in eras_with_notes:
        prior_list = [
            f"## {era_heading(n, by_era[n])}\n\n{t}" for n, t, _ in chapters
        ]
        prior_digests_list = [
            f"## {era_heading(en, by_era[en])}\n\n{d}"
            for en, d in load_prior_thread_digests(name)
        ]
        future_list = []
        future_digests_list = []
        if INCLUDE_FUTURE:
            future_list = [
                f"## {era_heading(en, by_era[en])}\n\n{ct}"
                for en, ct in load_future_chapters(name)
            ]
            future_digests_list = [
                f"## {era_heading(en, by_era[en])}\n\n{d}"
                for en, d in load_future_thread_digests(name)
            ]
        result = await write_chapter(
            client, name, by_era[name],
            prior_chapters_list=prior_list,
            prior_digests_list=prior_digests_list,
            future_chapters_list=future_list,
            future_digests_list=future_digests_list,
        )
        _, text, _ = result
        quotes, unverified = verify_quotes(text, by_era[name])
        log(f"  verify {name}: {len(quotes)} quotes, {len(unverified)} unverified")
        for u in unverified:
            preview = u[:100] + ("…" if len(u) > 100 else "")
            log(f"    ⚠ UNVERIFIED: {preview}")
        chapters.append(result)
    log(f"all chapters done in {time.time() - t_chapters:.0f}s")

    log("")
    summary = await write_summary(client, chapters, by_era)

    lines = ["# Fifteen years (2011-2026) — a retrospective", ""]
    lines.append(summary)
    lines.append("")
    for era_name, text, n_total in chapters:
        lines.append(f"## {era_heading(era_name, by_era[era_name])}")
        lines.append("")
        lines.append(f"*{n_total} notes*")
        lines.append("")
        lines.append(text)
        lines.append("")
    body = "\n".join(lines)
    body, n_resolved, n_unresolved = resolve_citations(body, all_notes)
    log(f"citations: {n_resolved} linked, {n_unresolved} unresolved")
    body = snapshot_header + body
    OUT_MD.write_text(body, encoding="utf-8")
    if LATEST_SYMLINK.is_symlink() or LATEST_SYMLINK.exists():
        LATEST_SYMLINK.unlink()
    LATEST_SYMLINK.write_text(body, encoding="utf-8")
    total_words = sum(len(text.split()) for _, text, _, _ in chapters) + len(summary.split())
    log("")
    log(f"saved: {OUT_MD}  ({total_words} words total, {time.time() - START:.0f}s wall)")
    log(f"latest: {LATEST_SYMLINK}")


if __name__ == "__main__":
    asyncio.run(main())
