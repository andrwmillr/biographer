#!/usr/bin/env python3
"""Find passages of glittering prose across Andrew's notes corpus.

Runs Claude Sonnet 4.6 over each note in the writing labels (journal, creative,
poetry, letter), asking for standout passages. Strict JSON output, empty when
no passage stands out.

CALIBRATION_MODE: run on a stratified sample of 20 notes first for prompt
tuning. Flip to False for the full run.
"""
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpora" / "andrew"
NOTES_DIR = CORPUS / "notes"
WRITING_LABELS = ["journal", "creative", "poetry", "letter"]
MODEL = "claude-sonnet-4-6"
CONCURRENCY = 5
MAX_RETRIES = 8

OUT_JSONL = CORPUS / "_derived" / "_good_stuff.jsonl"
CALIBRATION_OUT = CORPUS / "_derived" / "_good_stuff_calibration.jsonl"

CALIBRATION_MODE = True
CALIBRATION_YEARS = ["2013", "2016", "2020", "2025"]
CALIBRATION_PER_YEAR = 5

MAX_INPUT_CHARS = 8000
HEAD_CHARS = 3000
TAIL_CHARS = 3000

SYSTEM_PROMPT = """You are helping Andrew rediscover his best prose from a 17-year personal writing archive (2009-2026). He wants you to flag passages that genuinely stand out.

Your job: return a JSON array of standout passages from the given note.

What counts as "standout":
- A turn of phrase that's unusually sharp or precise
- An image or observation that's genuinely vivid
- A moment of self-awareness or insight that still reads as true
- Writing that's funny in a way that isn't trying
- Emotionally exact prose without sentimentality
- A paragraph or stretch of paragraphs that sustains its quality start to finish

Passage length: quote as much as hangs together. A passage can be one sharp sentence, or a paragraph that builds, or several paragraphs if the whole run holds up. Don't artificially shorten when the surrounding context is doing real work, and don't artificially extend a standalone one-liner. If a note has one great half-page, quote the whole half-page.

What doesn't count:
- Competent prose. Most writing is competent. That's not what we want.
- Quoted passages from other writers. Only Andrew's own words.
- Todo items, headers, URLs, fragments without literary value.
- Intellectual synthesis or philosophical notes unless the expression itself is uncommonly good.

Be selective, not greedy. Many notes should return []. That is correct behavior. But when a note is good, let the excerpt breathe — don't cherry-pick a single line from a paragraph that's strong all the way through.

Output strict JSON with no preamble or commentary. Format:
{"passages": ["passage 1 verbatim", "passage 2 verbatim"]}
Passages must be quoted verbatim from the note."""


def parse_note(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {"title": "", "date": "", "body": text.strip()}
    fm, body = m.group(1), m.group(2).lstrip("\n")
    title = ""
    date = ""
    for line in fm.splitlines():
        s = line.strip()
        if s.startswith("title:"):
            title = s.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("date_created:"):
            date = s.split(":", 1)[1].strip().strip('"').strip("'")
    return {"title": title, "date": date, "body": body.strip()}


def sample_body(body: str) -> str:
    if len(body) <= MAX_INPUT_CHARS:
        return body
    middle = len(body) - HEAD_CHARS - TAIL_CHARS
    return f"{body[:HEAD_CHARS]}\n\n…[middle {middle} chars snipped]…\n\n{body[-TAIL_CHARS:]}"


async def process_one(client, sem, rel):
    path = NOTES_DIR / rel
    async with sem:
        parsed = parse_note(path)
        if parsed is None:
            return {"rel": rel, "error": "read_failed"}
        body = sample_body(parsed["body"])
        if not body.strip():
            return {"rel": rel, "passages": []}
        user_msg = f"TITLE: {parsed['title']}\nDATE: {parsed['date']}\n\n---\n{body}"
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return {"rel": rel, "error": "no_json", "raw": text[:200]}
            data = json.loads(m.group(0))
            return {
                "rel": rel,
                "title": parsed["title"],
                "date": parsed["date"],
                "body_chars": len(parsed["body"]),
                "passages": data.get("passages", []),
            }
        except json.JSONDecodeError as e:
            return {"rel": rel, "error": f"json:{str(e)[:100]}"}
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}"}


def collect_targets():
    targets = []
    for label in WRITING_LABELS:
        d = NOTES_DIR / label
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            targets.append(str(p.relative_to(CORPUS)))
    return targets


def calibration_sample(targets):
    random.seed(42)
    by_year = {}
    for rel in targets:
        name = rel.split("/", 1)[1]
        year = name[:4]
        if year in CALIBRATION_YEARS:
            by_year.setdefault(year, []).append(rel)
    sampled = []
    for y in CALIBRATION_YEARS:
        notes = by_year.get(y, [])
        k = min(CALIBRATION_PER_YEAR, len(notes))
        sampled.extend(random.sample(notes, k))
    return sampled


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    targets = collect_targets()
    if CALIBRATION_MODE:
        targets = calibration_sample(targets)
        out_path = CALIBRATION_OUT
        print(f"CALIBRATION MODE — {len(targets)} notes")
    else:
        out_path = OUT_JSONL
        print(f"FULL RUN — {len(targets)} notes")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    out_f = out_path.open("w", encoding="utf-8")
    tasks = [process_one(client, sem, rel) for rel in targets]
    done = 0
    n_hits = n_empty = n_err = 0
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done += 1
        out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
        out_f.flush()
        if "error" in r:
            n_err += 1
            print(f"[{done}/{len(targets)}] ERR {r['rel']}: {r.get('error')}", file=sys.stderr)
            continue
        ps = r.get("passages", [])
        marker = "★" if ps else " "
        print(f"[{done}/{len(targets)}] {marker} {len(ps):2d} passages  {r['rel']}")
        for p in ps:
            print(f"    » {p}")
        if ps:
            n_hits += 1
        else:
            n_empty += 1
    out_f.close()
    print(f"\nDone. {n_hits} with passages, {n_empty} empty, {n_err} errors.")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
