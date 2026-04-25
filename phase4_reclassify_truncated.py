#!/usr/bin/env python3
"""Targeted rerun of phase4: re-classify long notes (>1500 chars) currently
labeled non-signal, using head+tail sampling so personal reflection at the
end of a clipped note isn't lost behind the leading quote.

Reads _corpus/_signal.tsv, filters to candidates, re-asks Claude with a
wider context, writes _corpus/_signal_reclass.tsv showing flips.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpus"
IN_TSV = CORPUS / "_signal.tsv"
OUT_TSV = CORPUS / "_signal_reclass.tsv"
MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 3
MAX_RETRIES = 8
HEAD_CHARS = 2000
TAIL_CHARS = 2000
SIGNAL_LABELS = {"journal", "creative", "letter"}

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

IMPORTANT — err toward signal (journal/creative/letter) on ambiguous cases. The user is building a reading list of his own personal writing and prefers false positives (keeping a borderline note) over false negatives (silently dropping a rich one).

NOTE: This note may contain a clipped article, quote, or reference material at the start, followed by the user's own reflection/commentary. Weigh the user's OWN words (typically at the end, or interleaved) more heavily than the clipped source material when deciding the label. If there is substantive original reflection anywhere in the note, label it journal or creative even if a quoted passage is longer.

Return strict JSON only, no prose: {"label": "<category>", "alt_label": "<category or null>"}"""


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


def head_tail(body: str) -> str:
    if len(body) <= HEAD_CHARS + TAIL_CHARS + 80:
        return body
    head = body[:HEAD_CHARS]
    tail = body[-TAIL_CHARS:]
    return f"{head}\n\n…[middle {len(body) - HEAD_CHARS - TAIL_CHARS} chars snipped]…\n\n{tail}"


async def classify_one(client, sem, rel: str, old_label: str, old_alt: str):
    path = CORPUS / rel
    async with sem:
        parsed = parse_note(path)
        if parsed is None:
            return {"rel": rel, "error": "read_failed", "old_label": old_label}
        body = head_tail(parsed["body"])
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
                return {"rel": rel, "error": "no_json", "old_label": old_label}
            data = json.loads(m.group(0))
            alt = data.get("alt_label")
            if alt in (None, "null", "", "None"):
                alt = ""
            return {
                "rel": rel,
                "title": parsed["title"],
                "body_chars": len(parsed["body"]),
                "old_label": old_label,
                "old_alt": old_alt,
                "new_label": data.get("label", "other"),
                "new_alt": alt,
            }
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}", "old_label": old_label}


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    queue = []
    with IN_TSV.open() as f:
        f.readline()  # header
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            rel, source, date, title, body_chars, label, alt = parts[:7]
            try:
                bc = int(body_chars)
            except ValueError:
                continue
            if bc > 1500 and label not in SIGNAL_LABELS:
                queue.append((rel, label, alt))

    print(f"Candidates to re-classify: {len(queue)}")
    if not queue:
        return

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    out_f = OUT_TSV.open("w", encoding="utf-8")
    out_f.write("source_path\told_label\told_alt\tnew_label\tnew_alt\tbody_chars\ttitle\n")
    out_f.flush()

    tasks = [classify_one(client, sem, rel, lab, alt) for rel, lab, alt in queue]
    done_count = 0
    flips_to_signal = 0
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done_count += 1
        if "error" in r:
            print(f"[{done_count}/{len(queue)}] ERR {r['rel']}: {r['error']}", file=sys.stderr)
            continue
        title = r["title"].replace("\t", " ").replace("\n", " ")
        out_f.write(
            f"{r['rel']}\t{r['old_label']}\t{r['old_alt']}\t{r['new_label']}\t{r['new_alt']}\t{r['body_chars']}\t{title}\n"
        )
        out_f.flush()
        flipped = r["old_label"] != r["new_label"]
        to_signal = r["new_label"] in SIGNAL_LABELS
        if flipped and to_signal:
            flips_to_signal += 1
            marker = "★"
        elif flipped:
            marker = "·"
        else:
            marker = " "
        print(f"[{done_count}/{len(queue)}] {marker} {r['old_label']:<10} -> {r['new_label']:<10} {r['rel']}")

    out_f.close()
    print(f"\nDone. {flips_to_signal} notes flipped into signal (journal/creative/letter).")
    print(f"Output: {OUT_TSV}")


if __name__ == "__main__":
    asyncio.run(main())
