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
  --task {paragraph,bullets}             — chapter framing (default paragraph)
"""
import asyncio
import json
import os
import pickle
import re
import subprocess
import sys
import time
from datetime import datetime

import yaml
from pathlib import Path

from anthropic import AsyncAnthropic

START = time.time()


def ts():
    elapsed = int(time.time() - START)
    return f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"


def log(msg):
    print(f"{ts()} {msg}", flush=True)

CORPUS = Path(os.environ.get("CORPUS_DIR") or Path.home() / "notes-archive" / "_corpus")
SUBJECT_NAME = os.environ.get("SUBJECT_NAME", "Andrew")
NOTES_DIR = CORPUS / "notes"
AUTHORSHIP = CORPUS / "_derived" / "_authorship.jsonl"
CORPUS_CACHE = CORPUS / "_derived" / "_corpus_cache.pkl"
DATE_OVERRIDES = CORPUS / "_config" / "_date_overrides.json"
NOTE_METADATA = CORPUS / "_config" / "_note_metadata.json"
EDITOR_NOTE_PREFIX = "EDITOR NOTE:"
MIXED_AUTHORSHIP_NOTE = "Contains quoted material — not all of this note is Andrew's own writing."
PEOPLE = CORPUS / "_config" / "_people.json"
ERAS_FILE = CORPUS / "_config" / "eras.yaml"
ERAS_BODY_DIR = CORPUS / "_config" / "eras"
BIOGRAPHIES_DIR = CORPUS / "claude" / "biographies"
CHAPTERS_DIR = BIOGRAPHIES_DIR / "chapters"
RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
MODELS = {
    "opus-4.6": "claude-opus-4-6",
    "opus-4.7": "claude-opus-4-7",
    "sonnet-4.6": "claude-sonnet-4-6",
}

def _pick_model() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--model" and i + 1 < len(sys.argv):
            key = sys.argv[i + 1]
            if key in MODELS:
                return MODELS[key]
            return key
    return MODELS["opus-4.7"]

MODEL = _pick_model()


TASK_VARIANTS = {
    "paragraph": """Each chapter should make the era feel like time that was lived. Track the texture of daily life — what room, what hour, who was around, what he was reading or eating — and also the abstract preoccupations he kept turning over: how to live, what work matters, what kind of mind is worth having. Both belong. Track schemes (sleep, food, dating, work) and where they went. Let contradictions stand: he often held opposite views on the same day, and the truth is in the holding, not the resolution. Be honest about hard stretches — the corpus contains crises and a hospital stay; record what's there without smoothing or dramatizing. Notice when the prose itself shifts: sentences shortening, a journaling lapse, a poem arriving unprompted — those changes are usually load-bearing. Name people specifically when the notes do. The chapter is evidence of a life, not a verdict on it.""",

    "bullets": """Write a chapter covering, for this era:
- what was on his mind — preoccupations, what he kept coming back to, what shifted
- what was actually happening — events, people, relationships, decisions, mental-health stretches
- patterns and turning points — what recurs, what changes, what gets dropped""",
}


def _pick_task() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--task" and i + 1 < len(sys.argv):
            key = sys.argv[i + 1]
            if key in TASK_VARIANTS:
                return key
            print(f"ERROR: unknown --task '{key}'. Choices: {', '.join(TASK_VARIANTS)}", file=sys.stderr)
            sys.exit(1)
    return "paragraph"

TASK_VARIANT = _pick_task()


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

WRITING_LABELS = ["journal", "creative", "poetry", "letter", "fiction"]

def era_slug(name):
    base = name.replace(" ", "_").replace("/", "-")
    for i, (n, _, _) in enumerate(ERAS, start=1):
        if n == name:
            return f"{i:02d}_{base}"
    return base


def _prev_month(ym):
    """'2013-06' -> '2013-05'; '2014-01' -> '2013-12'."""
    y, m = int(ym[:4]), int(ym[5:7])
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def _load_eras():
    """Load era boundaries from eras.yaml. Each entry is {name, start};
    end dates are derived as the month before the next entry's start.
    The last era is open-ended (9999-99)."""
    if not ERAS_FILE.exists():
        return []
    raw = yaml.safe_load(ERAS_FILE.read_text(encoding="utf-8")) or []
    raw_sorted = sorted(raw, key=lambda e: e["start"])
    out = []
    for i, e in enumerate(raw_sorted):
        if i + 1 < len(raw_sorted):
            end = _prev_month(raw_sorted[i + 1]["start"])
        else:
            end = "9999-99"
        out.append((e["name"], e["start"], end))
    return out


ERAS = _load_eras()

TOTAL_CHAR_CAP = 700_000
MIN_PER_NOTE = 400


CHAPTER_SYSTEM = (Path(__file__).parent / "DRAFTER.md").read_text(encoding="utf-8")
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__TASK__", TASK_VARIANTS[TASK_VARIANT])
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__SUBJECT__", SUBJECT_NAME)


SUMMARY_SYSTEM = """You are writing the opening synthesis to a retrospective on __SUBJECT__'s personal writing archive, 2011-2026. Plain third-person voice — a thoughtful biographer, not a literary critic.

You'll receive five era chapters. Write 250-450 words covering the through-lines: what preoccupied him across all fifteen years, what changed, what didn't, what the arc looks like from a distance.

VOICE
Plain, direct English. Short sentences welcome. Write so a friend can follow. Third person. No literary-critic jargon ("revisionary sentence", "ars poetica", "the register", "signature gesture", etc.). No ornate sentence structures for their own sake.

ABSOLUTE RULES
- Work only from what the chapters say. Don't introduce new specifics, names, dates, or quotes.
- If you use a quote or specific detail from a chapter, keep any [YYYY-MM-DD] citation it carries.
- No invented imagery. Abstract patterns are fine without citation; specifics need the source chapter's anchor.
- No "in conclusion", no enumerating the era names, no "this retrospective".
- Third person for __SUBJECT__. No sentimentalizing.
- If being accurate and plain makes the paragraph thinner, that's fine."""
SUMMARY_SYSTEM = SUMMARY_SYSTEM.replace("__SUBJECT__", SUBJECT_NAME)


def parse_note_body(rel):
    path = NOTES_DIR / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return text.strip()
    return m.group(2).lstrip("\n").strip()


def sample_keeper(body, cap):
    if len(body) <= cap:
        return body
    head = cap // 2
    tail = cap - head - 40
    middle = len(body) - head - tail
    return f"{body[:head]}\n\n…[middle {middle} chars snipped]…\n\n{body[-tail:]}"


def load_corpus_notes():
    """Walk the corpus and return one record per note in the writing labels.
    Records have rel, title, date — same shape Phase B expects, sourced
    directly from frontmatter rather than from Phase A's annotation pass.

    Caches the parsed result to CORPUS_CACHE. The cache holds only frontmatter-
    derived fields; note bodies are still loaded on demand from disk via
    parse_note_body(). Delete the cache file to force a fresh walk after
    edits to note files or the parser."""
    if CORPUS_CACHE.exists():
        try:
            with CORPUS_CACHE.open("rb") as f:
                return pickle.load(f)
        except Exception:
            pass  # fall through and rebuild
    out = []
    for label in WRITING_LABELS:
        d = NOTES_DIR / label
        if not d.exists():
            continue
        for path in sorted(d.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
            front = m.group(1) if m else ""
            title = ""
            date = ""
            for line in front.splitlines():
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "title":
                    title = v
                elif k == "date_created" and not date:
                    date = v.rstrip("Z")
            if not date:
                continue
            out.append({
                "rel": f"{label}/{path.name}",
                "title": title,
                "date": date,
            })
    CORPUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS_CACHE.open("wb") as f:
        pickle.dump(out, f)
    return out


def apply_date_overrides(notes):
    """Override a note's date where `date_created` reflects import-time rather
    than actual write-time (e.g., a poem pasted into Apple Notes years later)."""
    if not DATE_OVERRIDES.exists():
        return 0
    overrides = json.loads(DATE_OVERRIDES.read_text())
    applied = 0
    for n in notes:
        new_date = overrides.get(n["rel"])
        if new_date:
            n["date"] = new_date
            applied += 1
    return applied


def apply_note_metadata(notes):
    """Stamp n["editor_note"] = freeform contextual guidance for individual
    notes. Examples:
    - Retrospective: note written in year Y describes events from year X
      (entry says what era to place the scenes in).
    - Pseudonym/disambiguation: a name in the note is altered, or a referent
      is non-obvious (entry clarifies identity without changing era).
    - Authorship: appended automatically for mixed/unclear verdicts.
    The user.md renderer prepends EDITOR_NOTE_PREFIX and emits verbatim.
    If a note already has an editor_note (e.g. from apply_authorship), this
    appends to it with a space separator so multiple sources coexist."""
    if not NOTE_METADATA.exists():
        return 0
    overrides = json.loads(NOTE_METADATA.read_text())
    applied = 0
    for n in notes:
        extra = overrides.get(n["rel"])
        if extra:
            existing = n.get("editor_note")
            n["editor_note"] = f"{existing} {extra}" if existing else extra
            applied += 1
    return applied


def flag_date_clusters(notes, threshold=3):
    """Mark notes as date-uncertain when threshold+ notes share a date.
    Such clusters likely reflect import time or last-viewed/edited time
    rather than the original write date. The prompt uses the flag to
    keep prose vague about exact timing while still emitting the citation."""
    counts = {}
    for n in notes:
        d = (n.get("date") or "")[:10]
        if d:
            counts[d] = counts.get(d, 0) + 1
    flagged = 0
    for n in notes:
        d = (n.get("date") or "")[:10]
        if d and counts.get(d, 0) >= threshold:
            n["date_uncertain"] = True
            n["date_cluster_size"] = counts[d]
            flagged += 1
    return flagged


def load_people():
    """Return {name: description} for recurring people the notes may not
    adequately identify (relationships, context). Used to prevent the model
    from inferring e.g. siblings/partners from narrative context alone."""
    if not PEOPLE.exists():
        return {}
    return json.loads(PEOPLE.read_text())


def load_era_brief():
    """Return {era_name: body} — per-era authoring brief read from
    _config/eras/<slug>.md. Tells the drafter how to write this era:
    factual where/when anchor, threads worth tracking, structural
    decisions, drafting guidance accumulated from prior runs.
    Missing files are skipped silently."""
    out = {}
    for name, _, _ in ERAS:
        body_path = ERAS_BODY_DIR / f"{era_slug(name)}.md"
        if body_path.exists():
            body = body_path.read_text(encoding="utf-8").strip()
            if body:
                out[name] = body
    return out


def load_prior_chapters(era_name):
    """Return [(era_name, chapter_text), ...] for all eras chronologically
    before `era_name` that have a canonical chapter at chapters/<slug>.md.
    Order: chronological (oldest first). Missing chapters are skipped
    silently — partial state is normal early in the project."""
    out = []
    for name, _, _ in ERAS:
        if name == era_name:
            return out
        ch_path = CHAPTERS_DIR / f"{era_slug(name)}.md"
        if ch_path.exists():
            ch_text = ch_path.read_text(encoding="utf-8").strip()
            if ch_text:
                out.append((name, ch_text))
    return out


def format_people_block(people):
    if not people:
        return ""
    lines = ["--- PEOPLE (facts the notes may not state — trust these over inference) ---"]
    for name, desc in sorted(people.items()):
        lines.append(f"- {name}: {desc}")
    lines.append("--- END PEOPLE ---")
    return "\n".join(lines)


def load_authorship():
    """Return {rel: verdict} using the latest row per rel (file is append-only
    and may contain human-review overrides at the end)."""
    if not AUTHORSHIP.exists():
        return {}
    latest = {}
    for line in AUTHORSHIP.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r:
            continue
        rel = r.get("rel")
        if rel:
            latest[rel] = r.get("authored", "unclear")
    return latest


def apply_authorship(notes, verdicts):
    """Drop 'no' notes entirely. For mixed/unclear, set editor_note to the
    fixed authorship warning so the prompt can warn Opus to distinguish
    Andrew's prose from embedded quotes. Manual editor notes from
    _note_metadata.json are appended later by apply_note_metadata."""
    kept = []
    n_dropped = 0
    n_mixed = 0
    for n in notes:
        v = verdicts.get(n["rel"])
        if v == "no":
            n_dropped += 1
            continue
        if v in ("mixed", "unclear"):
            n["editor_note"] = MIXED_AUTHORSHIP_NOTE
            n_mixed += 1
        kept.append(n)
    return kept, n_dropped, n_mixed


def era_of(date):
    if not date or len(date) < 7:
        return None
    ym = date[:7]
    for name, lo, hi in ERAS:
        if lo <= ym <= hi:
            return name
    return None


def era_date_range(notes):
    """Return (lo, hi) YYYY-MM strings from this era's actual notes — usually
    tighter than the era's defined boundaries (especially Amherst I which is
    0000-00 to 2013-05)."""
    months = sorted({(n.get("date") or "")[:7] for n in notes if (n.get("date") or "")[:7]})
    if not months:
        return None, None
    return months[0], months[-1]


def era_heading(era_name, notes):
    lo, hi = era_date_range(notes)
    return f"{era_name} ({lo} – {hi})" if lo else era_name


def build_user_msg(era_name, notes, era_brief="", prior_chapters=None):
    sorted_notes = sorted(notes, key=lambda n: n.get("date", ""))
    bodies = []
    for n in sorted_notes:
        body = parse_note_body(n["rel"])
        if not body:
            continue
        bodies.append((n, body))
    total = sum(len(b) for _, b in bodies)
    if total > TOTAL_CHAR_CAP and bodies:
        ratio = TOTAL_CHAR_CAP / total
        bodies = [(n, sample_keeper(b, max(MIN_PER_NOTE, int(len(b) * ratio)))) for n, b in bodies]
    lines = []
    prior_chapters = prior_chapters or []
    if prior_chapters:
        lines.append("--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---")
        lines.append("")
        for prior_era, ch_text in prior_chapters:
            lines.append(f"### {prior_era}")
            lines.append("")
            lines.append(ch_text)
            lines.append("")
        lines.append("--- END PRIOR CHAPTERS ---")
        lines.append("")
    lo, hi = era_date_range(notes)
    range_str = f" ({lo} – {hi})" if lo else ""
    lines.extend([
        f"ERA: {era_name}{range_str}",
        f"NOTES IN THIS ERA: {len(notes)} total, {len(bodies)} with body content",
        "",
        "--- ALL NOTES (full text, chronological) ---",
        "",
    ])
    for n, body in bodies:
        title = n.get("title") or "(untitled)"
        date = (n.get("date") or "")[:10]
        label = n["rel"].split("/", 1)[0]
        date_warn = ""
        if n.get("date_uncertain"):
            date_warn = f"  ⚠ DATE-CLUSTER ({n.get('date_cluster_size')} notes share this date — likely import or last-edit time, not write time)"
        lines.append(f"=== {date} · {label} · {title}{date_warn} ===")
        if n.get("editor_note"):
            lines.append(f"  {EDITOR_NOTE_PREFIX} {n['editor_note']}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


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


def verify_quotes(chapter_text, era_notes):
    """Extract quoted strings from chapter text; flag any that don't appear verbatim
    in any era note body or title. Titles are included because the prompt
    encourages quoted-noun references like `a piece called "the afternoon sucks"`,
    which would otherwise pollute the unverified list."""
    quotes = extract_quotes(chapter_text)
    haystacks = []
    for n in era_notes:
        body = parse_note_body(n["rel"])
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


def resolve_citations(body, all_notes):
    """Rewrite [YYYY-MM-DD] as Obsidian wikilinks [[rel|YYYY-MM-DD]].
    When multiple notes share a date, disambiguate via the preceding quote."""
    by_date = {}
    for n in all_notes:
        d = (n.get("date") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(n)

    body_cache = {}
    def body_of(rel):
        if rel not in body_cache:
            body_cache[rel] = normalize_text(parse_note_body(rel) or "")
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

    out = []
    last = 0
    resolved = unresolved = 0
    for m in CITATION_RE.finditer(body):
        s, e = m.span()
        out.append(body[last:s])
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


async def write_chapter(client, era_name, notes, prior_chapters_list=None, people=None, era_brief=""):
    era_msg = build_user_msg(era_name, notes, era_brief=era_brief)
    label = f"{era_name}  ({len(notes)} notes"

    # Split prior chapters across separate content blocks with the cache marker
    # on the LAST one. Each chapter's block content is identical between requests
    # (older chapters' text is fixed once written), so the prefix matches and the
    # next request hits cache on system + opener + chapter1 + ... + chapter_{N-1},
    # writing only the new last chapter.
    user_blocks = []
    prior_chapters_list = prior_chapters_list or []
    if prior_chapters_list:
        user_blocks.append({
            "type": "text",
            "text": "--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n",
        })
        for i, ch in enumerate(prior_chapters_list):
            block = {"type": "text", "text": ch + "\n\n"}
            if i == len(prior_chapters_list) - 1:
                block["cache_control"] = {"type": "ephemeral"}
            user_blocks.append(block)
        user_blocks.append({"type": "text", "text": "--- END PRIOR CHAPTERS ---\n\n"})
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
    parts = []
    for era_name, text, _ in chapters:
        parts.append(f"## {era_heading(era_name, by_era[era_name])}\n\n{text}")
    user_msg = "ERA CHAPTERS:\n\n" + "\n\n".join(parts)
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
    log(f"task variant: {TASK_VARIANT}")
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
    era_brief_map = load_era_brief()
    if era_brief_map:
        n_filled = sum(1 for v in era_brief_map.values() if v)
        log(f"era brief: {n_filled}/{len(era_brief_map)} eras filled")
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
        f"model: {MODEL}  task: {TASK_VARIANT} -->\n\n"
    )
    BIOGRAPHIES_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_SNAPSHOT.write_text(snapshot_header + CHAPTER_SYSTEM, encoding="utf-8")
    users_parts = [snapshot_header]
    for name in eras_with_notes:
        msg = build_user_msg(name, by_era[name], era_brief=era_brief_map.get(name, ""))
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
        # First-pass deliberately omits PEOPLE block: putting names in the
        # prompt biases the model toward mentioning them. Relationship
        # accuracy gets caught by check_relationships.py post-hoc instead.
        result = await write_chapter(
            client, name, by_era[name],
            prior_chapters_list=prior_list,
            people=None,
            era_brief=era_brief_map.get(name, ""),
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
