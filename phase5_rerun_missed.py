#!/usr/bin/env python3
"""Re-run poem classification for notes missed by phase5_find_poems.py
(typically rate-limit errors). Appends results to _poems.tsv.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpus"
SIGNAL_TSV = CORPUS / "_signal.tsv"
POEMS_TSV = CORPUS / "_poems.tsv"
MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 2
MAX_RETRIES = 10
BODY_LIMIT = 3000

SYSTEM_PROMPT = """You are identifying poems inside a personal notes archive. Classify the note with exactly one of:

- poem: the note is essentially a poem (verse with line breaks, stanzas, or prose poetry). Minor leading/trailing metadata is OK.
- contains_poem: the note has a poem embedded in surrounding prose (e.g. a journal entry that quotes or contains a poem the writer composed).
- not_poem: the note is prose, essay, criticism, journal entry, list, or quoted material. Notes ABOUT poetry (reviews, reading lists, criticism of other poets) are not_poem.

Include both formal verse AND free-verse / prose-poetry. Haiku counts. Short lyric fragments count if they read as poems. Only count the user's OWN poems — not quoted passages by other poets.

Return strict JSON only: {"class": "poem|contains_poem|not_poem"}"""


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
    path = CORPUS / rel
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
                "classification": data.get("class", "not_poem"),
            }
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}"}


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    # Rebuild candidate set (same logic as phase5_find_poems.py)
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

    for p in CORPUS.rglob("*.md"):
        rel = str(p.relative_to(CORPUS))
        low = rel.lower()
        if rel.startswith("_poetry/"):
            continue
        if "poetry" in low or "poems" in low:
            candidates.add(rel)

    candidates = {c for c in candidates if not c.startswith("evernote/clipped/")}

    # Load already-classified paths from _poems.tsv
    done = set()
    with POEMS_TSV.open() as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts:
                done.add(parts[0])

    missing = sorted(candidates - done)
    print(f"Total candidates: {len(candidates)}")
    print(f"Already done: {len(done)}")
    print(f"To re-run: {len(missing)}")
    if not missing:
        return
    for m in missing:
        print(f"  {m}")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    # Append to existing _poems.tsv
    out_f = POEMS_TSV.open("a", encoding="utf-8")

    tasks = [classify_one(client, sem, rel) for rel in missing]
    done_count = 0
    counts = {"poem": 0, "contains_poem": 0, "not_poem": 0, "error": 0}
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done_count += 1
        if "error" in r:
            counts["error"] += 1
            print(f"[{done_count}/{len(missing)}] ERR {r['rel']}: {r['error']}", file=sys.stderr)
            continue
        title = r["title"].replace("\t", " ").replace("\n", " ")
        out_f.write(
            f"{r['rel']}\t{r['date_created']}\t{title}\t{r['body_chars']}\t{r['classification']}\n"
        )
        out_f.flush()
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
        marker = {"poem": "★", "contains_poem": "·", "not_poem": " "}.get(r["classification"], "?")
        print(f"[{done_count}/{len(missing)}] {marker} {r['classification']:<14} {r['rel']}")

    out_f.close()
    print(f"\nDone. poem={counts['poem']}  contains_poem={counts['contains_poem']}  not_poem={counts['not_poem']}  err={counts['error']}")


if __name__ == "__main__":
    asyncio.run(main())
