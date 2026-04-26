#!/usr/bin/env python3
"""Scan a narrative for relationship claims and flag any that contradict
`_people.json` or aren't backed by it. Used after `write_biography.py`
runs without the PEOPLE block (to avoid biasing generation).

For each paragraph, ask Haiku to identify every sentence that asserts a
relationship between Andrew and a named person ("his brother Max", "his
girlfriend Sarah", etc.) and judge it against the PEOPLE block.

Output: `<narrative>.relationships.json` with only paragraphs that have
flagged claims.

Usage:
    python3 _scripts/check_relationships.py                    # biography.md
    python3 _scripts/check_relationships.py biography.md
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from write_biography import (  # type: ignore
    BIOGRAPHIES_DIR,
    format_people_block,
    load_people,
    log,
)

from anthropic import AsyncAnthropic

HAIKU = "claude-haiku-4-5-20251001"
CONCURRENCY = 8

CHAPTER_RE = re.compile(r"^# [^#]")

CHECK_SYSTEM = """You are checking one paragraph of a biographical narrative for relationship claims.

CONTEXT
You'll receive a PEOPLE block (ground-truth facts about named people in Andrew's life) and one PARAGRAPH from the narrative.

TASK
Identify every sentence that asserts a relationship between Andrew and a named person — phrases like "his brother Max", "her sister Lily", "his girlfriend Sarah", "his colleague Joe", "his roommate", "his friend Nicky". For each, judge against the PEOPLE block:

- CONTRADICTS_PEOPLE — the asserted relationship contradicts what PEOPLE says (e.g., narrative says "his brother Max" but PEOPLE says Max is a friend)
- UNVERIFIED — the named person is NOT in PEOPLE; the asserted relationship can't be confirmed from the ground truth
- CONSISTENT — the named person IS in PEOPLE and the asserted relationship matches

DO NOT FLAG
- Mentions of named people that don't assert a relationship ("Andrew sees Max", "writes a letter to Sarah" — no relation claim)
- Generic references ("his mother", "his father", "his grandmother" without a name)
- Andrew himself

OUTPUT
Return a JSON array — nothing else, no preamble, no code fences. Each element:
{
  "claim": "<short verbatim phrase from the paragraph>",
  "person": "<the named person>",
  "verdict": "CONTRADICTS_PEOPLE" | "UNVERIFIED" | "CONSISTENT",
  "reason": "<one sentence>"
}

If there are no relationship claims, return [].
"""


def split_paragraphs(body):
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


def _strip_json_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


async def check_paragraph(client, paragraph, people_block):
    content = [
        {"type": "text", "text": people_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"PARAGRAPH:\n{paragraph}"},
    ]
    resp = await client.messages.create(
        model=HAIKU,
        max_tokens=1024,
        system=CHECK_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = _strip_json_fences(resp.content[0].text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        return [{"claim": "[PARSE ERROR]", "person": "", "verdict": "ERROR", "reason": f"{e}: {text[:200]}"}]


async def main():
    target_name = sys.argv[1] if len(sys.argv) > 1 else "biography.md"
    target = BIOGRAPHIES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    people = load_people()
    if not people:
        print("ERROR: _people.json is empty — nothing to check against.")
        sys.exit(1)
    people_block = format_people_block(people)
    log(f"loaded {len(people)} PEOPLE entries: {', '.join(sorted(people))}")

    body = target.read_text(encoding="utf-8")
    paragraphs = split_paragraphs(body)
    to_check = [(line, text) for (line, text) in paragraphs if not paragraph_is_chapter_header(text)]
    log(f"paragraphs to check: {len(to_check)}")

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    total = len(to_check)

    async def one(line, text):
        nonlocal done
        async with sem:
            claims = await check_paragraph(client, text, people_block)
            done += 1
            n_flagged = sum(1 for c in claims if c.get("verdict") in ("CONTRADICTS_PEOPLE", "UNVERIFIED"))
            log(f"[{done}/{total}] line {line}: {n_flagged} flagged")
            return {"line": line, "paragraph": text, "claims": claims}

    results = await asyncio.gather(*(one(l, t) for (l, t) in to_check))

    flagged = []
    for r in results:
        bad = [c for c in r["claims"] if c.get("verdict") in ("CONTRADICTS_PEOPLE", "UNVERIFIED")]
        if bad:
            flagged.append({"line": r["line"], "paragraph": r["paragraph"], "flagged_claims": bad})

    out_name = target.stem + ".relationships.json"
    out_path = BIOGRAPHIES_DIR / out_name
    out_path.write_text(json.dumps(flagged, indent=2, ensure_ascii=False), encoding="utf-8")

    n_contradict = sum(1 for r in flagged for c in r["flagged_claims"] if c["verdict"] == "CONTRADICTS_PEOPLE")
    n_unverified = sum(1 for r in flagged for c in r["flagged_claims"] if c["verdict"] == "UNVERIFIED")
    log(f"wrote {out_path.name}: {len(flagged)} paragraphs flagged")
    log(f"  {n_contradict} CONTRADICTS_PEOPLE")
    log(f"  {n_unverified} UNVERIFIED")


if __name__ == "__main__":
    asyncio.run(main())
