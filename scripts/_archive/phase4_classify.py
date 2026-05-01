#!/usr/bin/env python3
"""Phase 4: label each note in _corpus/ for signal-vs-noise triage.

Reads every .txt in ~/notes-archive/_corpus/ (except evernote/clipped/ which is
pre-classified as noise), asks Claude Haiku to label it, and appends to
_corpus/_signal.tsv. Resumable: re-running skips already-classified paths.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpus"
OUT_TSV = CORPUS / "_derived" / "_signal.tsv"
MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 3
MAX_RETRIES = 8
BODY_CHAR_LIMIT = 1500
SKIP_PREFIXES = ("evernote/clipped/",)

SYSTEM_PROMPT = """You are classifying personal notes from a unified archive spanning 16 years of writing across multiple apps (journals, emails, web clips, to-dos, etc.).

Label each note with exactly ONE of these categories:
- journal: personal reflection, diary-like entries, self-observation, processing thoughts/feelings
- creative: essays, fiction, poetry, aphorisms, essay-seeds, structured thought pieces
- letter: correspondence meant to be sent (drafts of emails or letters to specific people)
- reference: saved info for later lookup, research-link dumps, meeting notes without personal commentary
- todo: tasks, errands, shopping lists, things to do
- business: work notes, project plans, standups, job search, professional logistics
- contact: phone numbers, addresses, contact info
- code: snippets, commands, technical configuration
- other: anything that doesn't fit above

IMPORTANT — err toward signal (journal/creative/letter) on ambiguous cases. The user is building a reading list of his own personal writing and prefers false positives (keeping a borderline note) over false negatives (silently dropping a rich one). Specifically:
- Lists of essay questions or aphorisms → creative
- Short personal observations or self-reflection in list form → journal
- Any writing with a personal/reflective voice, even brief or fragmentary → journal or creative
- Book lists, reading lists with personal commentary → reference is OK, but if there's any reflection wrapping it, prefer journal

Only use reference/todo/business/contact when the note is CLEARLY non-personal: a shopping list, a phone number, a research-link dump without commentary, a work standup note.

Return strict JSON only, no prose: {"label": "<category>", "alt_label": "<category or null>"}

Use alt_label when a second category is also plausible (e.g. a reflective list of books could be journal with alt_label=reference). Set alt_label to null when the primary label is clearly the only fit."""


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


async def classify_one(client, sem, path: Path, rel: str):
    async with sem:
        parsed = parse_note(path)
        if parsed is None:
            return {"rel": rel, "error": "read_failed"}
        body = parsed["body"][:BODY_CHAR_LIMIT]
        if len(parsed["body"]) > BODY_CHAR_LIMIT:
            body += "\n…[truncated]"
        user_msg = f"TITLE: {parsed['title']}\nDATE: {parsed['date_created']}\n\n---\n{body}"
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=80,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{[^}]*\}", text, re.DOTALL)
            if not m:
                return {"rel": rel, "error": "no_json", "raw": text[:200]}
            data = json.loads(m.group(0))
            alt = data.get("alt_label")
            if alt in (None, "null", "", "None"):
                alt = ""
            source = rel.split("/", 1)[0]
            return {
                "rel": rel,
                "source": source,
                "title": parsed["title"],
                "date_created": parsed["date_created"],
                "body_chars": len(parsed["body"]),
                "label": data.get("label", "other"),
                "alt_label": alt,
            }
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}"}


def load_done(out_path: Path):
    if not out_path.exists():
        return set()
    done = set()
    with out_path.open() as f:
        f.readline()  # header
        for line in f:
            parts = line.split("\t", 1)
            if parts:
                done.add(parts[0])
    return done


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)

    all_paths = sorted(CORPUS.rglob("*.txt"))
    done = load_done(OUT_TSV)
    queue = []
    skipped_prefix = 0
    for p in all_paths:
        rel = str(p.relative_to(CORPUS))
        if rel in done:
            continue
        if any(rel.startswith(pref) for pref in SKIP_PREFIXES):
            skipped_prefix += 1
            continue
        queue.append((p, rel))

    print(f"Corpus total: {len(all_paths)}")
    print(f"Skipped (pre-classified noise): {skipped_prefix}")
    print(f"Already classified: {len(done)}")
    print(f"To classify this run: {len(queue)}")
    if not queue:
        print("Nothing to do.")
        return

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    new_file = not OUT_TSV.exists()
    out_f = OUT_TSV.open("a", encoding="utf-8")
    if new_file:
        out_f.write("source_path\tsource\tdate_created\ttitle\tbody_chars\tlabel\talt_label\n")
        out_f.flush()

    tasks = [classify_one(client, sem, p, rel) for p, rel in queue]
    done_count = 0
    errors = 0
    label_counts = {}
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            done_count += 1
            if "error" in result:
                errors += 1
                print(f"[{done_count}/{len(queue)}] ERR  {result['rel']}  {result['error']}", file=sys.stderr)
                continue
            title = result["title"].replace("\t", " ").replace("\n", " ")
            out_f.write(
                f"{result['rel']}\t{result['source']}\t{result['date_created']}\t{title}\t"
                f"{result['body_chars']}\t{result['label']}\t{result['alt_label']}\n"
            )
            out_f.flush()
            label_counts[result["label"]] = label_counts.get(result["label"], 0) + 1
            date_short = (result["date_created"] or "")[:10]
            title_short = title[:60] if title else "(untitled)"
            alt_display = f"/{result['alt_label']}" if result["alt_label"] else ""
            label_display = f"{result['label']}{alt_display}"
            print(f"[{done_count}/{len(queue)}] {result['source']:<13} {label_display:<22} {date_short:<10}  {title_short}")
            if done_count % 200 == 0 or done_count == len(queue):
                summary = " ".join(f"{k}={v}" for k, v in sorted(label_counts.items(), key=lambda x: -x[1]))
                print(f"  --- running tally: {summary} ---")
    finally:
        out_f.close()

    print(f"\nDone. {done_count - errors} classified, {errors} errors.")
    print(f"Output: {OUT_TSV}")


if __name__ == "__main__":
    asyncio.run(main())
