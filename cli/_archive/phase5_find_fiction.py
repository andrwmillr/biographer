#!/usr/bin/env python3
"""Scan creative-labeled notes and classify each as: fiction, contains_fiction,
or not_fiction. Mirrors phase5_find_poems.py.

Output: _corpus/_fiction.tsv
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
SIGNAL_TSV = CORPUS / "_derived" / "_signal.tsv"
OUT_TSV = CORPUS / "_derived" / "_fiction.tsv"
MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 3
MAX_RETRIES = 8
BODY_LIMIT = 3000

SYSTEM_PROMPT = """You are identifying original fiction inside a personal notes archive. Classify the note with exactly one of:

- fiction: the note is essentially Andrew's own fiction — a short story, dialogue scene, character sketch, or scene fragment with invented characters or imagined scenarios. Completeness doesn't matter; attempts count. Stylistic imitations of other writers (Woolf, Coetzee, etc.) with invented content count.
- contains_fiction: the note is primarily something else (journal, essay, letter) but contains a substantial embedded fiction attempt — a scene, a dialogue, a sustained passage of invented narrative.
- not_fiction: everything else. Includes journal entries (first-person autobiographical), letters to real people (even unsent), essays and criticism, lists, todos, observations, plain dream recountings, and quoted passages from other writers.

IMPORTANT:
- Poems are not_fiction.
- Letters to real people are not_fiction, even when literary.
- Dialogue between invented characters counts as fiction even without prose setting.
- If the narrator is clearly Andrew writing about his own life, it's not_fiction — even if stylized.

Return strict JSON only: {"class": "fiction|contains_fiction|not_fiction"}"""


def parse_note(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {"title": "", "date_created": "", "body": text}
    fm_raw, body = m.group(1), m.group(2).lstrip("\n")
    title = ""
    date_created = ""
    for line in fm_raw.splitlines():
        s = line.strip()
        if s.startswith("title:"):
            title = s.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("date_created:"):
            date_created = s.split(":", 1)[1].strip().strip('"').strip("'")
    return {"title": title, "date_created": date_created, "body": body}


async def classify_one(client, sem, rel: str):
    path = NOTES_DIR / rel
    async with sem:
        parsed = parse_note(path)
        if parsed is None:
            return {"rel": rel, "error": "read_failed"}
        body = parsed["body"][:BODY_LIMIT]
        user_msg = f"TITLE: {parsed['title']}\n\n---\n{body}"
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=40,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{[^}]*\}", text, re.DOTALL)
            if not m:
                return {"rel": rel, "error": "no_json"}
            data = json.loads(m.group(0))
            return {
                "rel": rel,
                "title": parsed["title"],
                "date_created": parsed["date_created"],
                "body_chars": len(parsed["body"]),
                "classification": data.get("class", "not_fiction"),
            }
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}"}


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    candidates = set()
    with SIGNAL_TSV.open() as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            rel, _source, _date, _title, _bc, label, _alt = parts[:7]
            if label == "creative":
                candidates.add(rel)

    candidates = {c for c in candidates if not c.startswith("evernote/clipped/")}
    queue = sorted(candidates)

    print(f"Candidates: {len(queue)}")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    out_f = OUT_TSV.open("w", encoding="utf-8")
    out_f.write("source_path\tdate_created\ttitle\tbody_chars\tclassification\n")
    out_f.flush()

    tasks = [classify_one(client, sem, rel) for rel in queue]
    done_count = 0
    counts = {"fiction": 0, "contains_fiction": 0, "not_fiction": 0, "error": 0}
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done_count += 1
        if "error" in r:
            counts["error"] += 1
            print(f"[{done_count}/{len(queue)}] ERR {r['rel']}: {r['error']}", file=sys.stderr)
            continue
        title = r["title"].replace("\t", " ").replace("\n", " ")
        out_f.write(
            f"{r['rel']}\t{r['date_created']}\t{title}\t{r['body_chars']}\t{r['classification']}\n"
        )
        out_f.flush()
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
        marker = {"fiction": "★", "contains_fiction": "·", "not_fiction": " "}.get(r["classification"], "?")
        print(f"[{done_count}/{len(queue)}] {marker} {r['classification']:<16} {r['rel'][:90]}")

    out_f.close()
    print(f"\nDone. fiction={counts['fiction']}  contains_fiction={counts['contains_fiction']}  not_fiction={counts['not_fiction']}  err={counts['error']}")
    print(f"Output: {OUT_TSV}")


if __name__ == "__main__":
    asyncio.run(main())
