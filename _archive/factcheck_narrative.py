#!/usr/bin/env python3
"""Fact-check a resolved narrative file. For each paragraph (cited or not),
send (paragraph + every note cited anywhere in the same chapter + PEOPLE
block) to Haiku and ask which concrete factual claims are not supported.

Chapter boundary = top-level header line (`^# `). Notes cited anywhere in a
chapter form that chapter's verification pool — so uncited paragraphs
(chapter intros, era summaries, "what the era leaves him with" wrap-ups)
still get checked, which is where the worst confabulations tend to live.

Output: `<narrative>.factcheck.json` with only the flagged paragraphs.

Usage:
    python3 _scripts/factcheck_narrative.py                    # biography.md
    python3 _scripts/factcheck_narrative.py biography_20260424_171813.md
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from write_biography import (  # type: ignore
    BIOGRAPHIES_DIR,
    apply_authorship,
    apply_date_overrides,
    apply_note_about,
    format_people_block,
    load_authorship,
    load_corpus_notes,
    load_people,
    log,
    parse_note_body,
)

from anthropic import AsyncAnthropic

HAIKU = "claude-haiku-4-5-20251001"

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)\|(\d{4}-\d{2}-\d{2})\]\]")
CHAPTER_RE = re.compile(r"^# [^#]")
CONCURRENCY = 8
MAX_NOTE_CHARS = 8000
MAX_CHAPTER_NOTES_CHARS = 120000  # cap the chapter-notes block per prompt

FACTCHECK_SYSTEM = """You are fact-checking one paragraph of a biographical narrative against the source notes available for its chapter.

CONTEXT
You'll receive:
- A PEOPLE block with facts the notes may not state (trust these).
- One PARAGRAPH from the narrative.
- CHAPTER NOTES: every note cited anywhere in this chapter. The paragraph itself may or may not carry a wikilink — uncited paragraphs (chapter intros, era wrap-ups) still need to ground their concrete claims somewhere in the chapter's notes.

TASK
Identify every concrete factual claim in the paragraph and judge whether it is supported by the PEOPLE block or the CHAPTER NOTES. A claim is NOT_SUPPORTED when nothing in the provided material contains or clearly implies it.

CLAIMS TO CHECK
- Specific actions ("he sends the letter", "he takes capsules", "he moves to X")
- Specific people (names, roles, relationships — "his brother Max", "at Bloomberg")
- Specific places (apartment type, workplace, city, venue — "a studio", "at work", "in LA")
- Specific temporal claims ("in fall 2012", "the week between X and Y")
- Specific economic framings ("somewhere he can afford", "cheap sublet")
- Specific ingestion/medical contexts ("high at work", "after taking acid")
- Specific relationship states ("starts dating X", "after they break up")
- Letters/emails described as *sent* (letter-folder notes are drafts by default)

DO NOT FLAG
- Thematic observations (voice, tone, recurring preoccupations, patterns across notes)
- Characterizations of how the writing feels ("it's careful", "the prose gets shorter")
- Generalizations that reflect aggregate reading ("he writes less in this era")
- Direct quotations (checked by a separate tool)
- Facts stated in the PEOPLE block (trust those)

OUTPUT
Return a JSON array — nothing else, no preamble, no code fences. Each element:
{
  "claim": "<short verbatim phrase from the paragraph>",
  "verdict": "SUPPORTED" | "NOT_SUPPORTED",
  "reason": "<one sentence — cite note date or PEOPLE if supported; describe what's missing if not>"
}

If there are no concrete claims, return [].
"""


def split_paragraphs(body):
    """Return list of (line_start, text) for non-empty paragraph blocks."""
    lines = body.splitlines()
    paras = []
    buf = []
    start = None
    for i, line in enumerate(lines, 1):
        if line.strip():
            if start is None:
                start = i
            buf.append(line)
        else:
            if buf:
                paras.append((start, "\n".join(buf)))
                buf = []
                start = None
    if buf:
        paras.append((start, "\n".join(buf)))
    return paras


def paragraph_is_chapter_header(text):
    first = text.splitlines()[0].lstrip()
    return first.startswith("# ") and not first.startswith("## ")


def assign_chapters(paragraphs):
    """Walk paragraphs in order, tag each with a chapter id (0 = pre-first-chapter).
    Returns a list parallel to paragraphs with the chapter id for each."""
    chapter_id = 0
    tagged = []
    for line, text in paragraphs:
        if paragraph_is_chapter_header(text):
            chapter_id += 1
        tagged.append((line, text, chapter_id))
    return tagged


def collect_chapter_notes(tagged, by_rel):
    """Return {chapter_id: [note_dict, ...]} — every note cited anywhere in the chapter, dedup'd."""
    per_chapter = {}
    for line, text, cid in tagged:
        seen = per_chapter.setdefault(cid, {})
        for m in WIKILINK_RE.finditer(text):
            rel = m.group(1) + ".md"
            if rel in seen:
                continue
            n = by_rel.get(rel)
            if n:
                seen[rel] = n
    return {cid: list(notes.values()) for cid, notes in per_chapter.items()}


def format_chapter_notes_block(notes):
    chunks = []
    total = 0
    for n in sorted(notes, key=lambda n: (n.get("date") or "")):
        body = parse_note_body(n["rel"]) or ""
        if len(body) > MAX_NOTE_CHARS:
            body = body[:MAX_NOTE_CHARS] + "\n[...truncated...]"
        date = (n.get("date") or "")[:10]
        label = n["rel"].split("/", 1)[0]
        chunk = f"=== {date} · {label} · {n['rel']} ===\n{body}"
        if total + len(chunk) > MAX_CHAPTER_NOTES_CHARS:
            chunks.append(f"[...{len(notes) - len(chunks)} additional notes omitted for length...]")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n\n".join(chunks)


def _strip_json_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


async def factcheck_paragraph(client, paragraph, chapter_notes_block, people_block):
    # Cache-prefix = stable-per-chapter content (PEOPLE + chapter notes).
    # Variable suffix = per-paragraph text. Keeps the prefix byte-identical
    # across every paragraph in the same chapter so we get cache hits.
    prefix_parts = []
    if people_block:
        prefix_parts.append(people_block)
    prefix_parts.append(f"--- CHAPTER NOTES ---\n{chapter_notes_block or '(none)'}")
    prefix = "\n\n".join(prefix_parts)

    content = [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"PARAGRAPH:\n{paragraph}"},
    ]

    resp = await client.messages.create(
        model=HAIKU,
        max_tokens=2048,
        system=FACTCHECK_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = _strip_json_fences(resp.content[0].text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        return [{"claim": "[PARSE ERROR]", "verdict": "ERROR", "reason": f"{e}: {text[:200]}"}]


async def main():
    target_name = sys.argv[1] if len(sys.argv) > 1 else "biography.md"
    target = BIOGRAPHIES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    log("loading notes...")
    notes = load_corpus_notes()
    apply_date_overrides(notes)
    apply_note_about(notes)
    verdicts = load_authorship()
    notes, _, _ = apply_authorship(notes, verdicts)
    by_rel = {n["rel"]: n for n in notes}
    people = load_people()
    people_block = format_people_block(people)

    body = target.read_text(encoding="utf-8")
    paragraphs = split_paragraphs(body)
    tagged = assign_chapters(paragraphs)
    chapter_notes = collect_chapter_notes(tagged, by_rel)
    chapter_blocks = {cid: format_chapter_notes_block(ns) for cid, ns in chapter_notes.items()}

    # Skip chapter-header paragraphs (nothing to fact-check).
    to_check = [
        (line, text, cid)
        for (line, text, cid) in tagged
        if not paragraph_is_chapter_header(text)
    ]
    log(f"paragraphs to check: {len(to_check)} across {len(chapter_notes)} chapters")

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    total = len(to_check)

    async def one(line, text, cid):
        nonlocal done
        async with sem:
            block = chapter_blocks.get(cid, "")
            claims = await factcheck_paragraph(client, text, block, people_block)
            done += 1
            n_flagged = sum(1 for c in claims if c.get("verdict") == "NOT_SUPPORTED")
            log(f"[{done}/{total}] line {line} (ch{cid}): {n_flagged} flagged")
            cited_here = sorted({m.group(2) for m in WIKILINK_RE.finditer(text)})
            return {"line": line, "paragraph": text, "chapter": cid, "cited_here": cited_here, "claims": claims}

    # Two-phase run so prompt caching actually pays off. If we fired all
    # paragraphs at once, concurrent calls with identical cache prefixes
    # would all race and each pay cache-write rate. Instead: one paragraph
    # per chapter first (warms caches in parallel — different chapters have
    # different prefixes, no racing), then the rest hit warm caches.
    seen_ch = set()
    warmup, rest = [], []
    for (l, t, cid) in to_check:
        if cid not in seen_ch:
            warmup.append((l, t, cid))
            seen_ch.add(cid)
        else:
            rest.append((l, t, cid))
    log(f"warming {len(warmup)} chapter caches...")
    warm_results = await asyncio.gather(*(one(l, t, cid) for (l, t, cid) in warmup))
    log(f"running {len(rest)} paragraphs against warm caches...")
    rest_results = await asyncio.gather(*(one(l, t, cid) for (l, t, cid) in rest))
    results = warm_results + rest_results

    flagged = []
    for r in results:
        unsupported = [c for c in r["claims"] if c.get("verdict") == "NOT_SUPPORTED"]
        if unsupported:
            flagged.append({
                "line": r["line"],
                "chapter": r["chapter"],
                "cited_here": r["cited_here"],
                "paragraph": r["paragraph"],
                "unsupported_claims": unsupported,
            })

    out_name = target.stem + ".factcheck.json"
    out_path = BIOGRAPHIES_DIR / out_name
    out_path.write_text(json.dumps(flagged, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote {out_path.name}: {len(flagged)} paragraphs with flagged claims (of {total} checked)")


if __name__ == "__main__":
    asyncio.run(main())
