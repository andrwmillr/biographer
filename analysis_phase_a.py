#!/usr/bin/env python3
"""Phase A: per-note significance, kernel, and themes across the writing corpus.

Reads every note under writing labels (journal, creative, poetry, letter) and
asks Claude Sonnet 4.6 for:
  - significance: keeper | notable | minor | skip
  - kernel: one sentence describing what the note IS or what Andrew was working out
  - themes: 1-5 short tags (concrete nouns preferred)

Output feeds Phase B (narrative synthesis). Andrew's manual Phase C triage
runs in parallel and is authoritative — Phase A weighting is just an input.

CALIBRATION_MODE: run on 20 stratified notes first for prompt tuning.
"""
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
WRITING_LABELS = ["journal", "creative", "poetry", "letter"]
MODEL = "claude-sonnet-4-6"
CONCURRENCY = 5
MAX_RETRIES = 8

OUT_JSONL = CORPUS / "_derived" / "_phase_a.jsonl"
CALIBRATION_OUT = CORPUS / "_derived" / "_phase_a_calibration.jsonl"

CALIBRATION_MODE = False
CALIBRATION_YEARS = ["2013", "2016", "2020", "2025"]
CALIBRATION_PER_YEAR = 5

MAX_INPUT_CHARS = 12000
HEAD_CHARS = 5000
TAIL_CHARS = 5000

SYSTEM_PROMPT = """You are helping Andrew weight his 17-year personal writing archive (2009-2026) for a narrative synthesis project. He needs a thumbnail of every note.

For each note you see, return strict JSON:
{
  "significance": "keeper" | "notable" | "minor" | "skip",
  "kernel": "one sentence, under 25 words, describing what the note IS or what Andrew is working out in it",
  "themes": ["1 to 5 short tags"]
}

SIGNIFICANCE rubric:
- "keeper": prose that genuinely stands out — sharp observation, vivid image, emotionally exact writing, a sustained passage where the voice is alive. Or: a turning-point entry where something real was being worked out. These are the notes Andrew will want to re-read.
- "notable": solid writing or meaningful content, but not a standout. A clear piece of thinking, a decent poem draft, a letter with real feeling. Worth surfacing in context but not on its own.
- "minor": competent journaling or routine creative drafts. Most notes land here. Nothing wrong with them, just not where the signal lives.
- "skip": fragments, lists, todo-ish, headers without body, placeholder scribbles, reference material that slipped through labeling.

When in doubt between keeper and notable, lean notable. When in doubt between minor and skip, lean minor — skip is for things that are clearly not writing.

KERNEL guidance:
- Not a summary of what the note "says". A description of what it IS.
- Good: "draft of a letter to his father about leaving the job, unsent"; "working through whether the breakup was the right call"; "fragment of a poem about winter light".
- Bad: "Andrew talks about his day" / "thoughts on life".
- Under 25 words. Omit filler.

THEMES guidance:
- Concrete over abstract. Prefer "father", "breakup", "aftersun", "bootcamp" over "family", "relationships", "movies", "career".
- 1-5 tags. Lowercase. No hashes.
- If the note is a skip, themes can be empty.

Return ONLY the JSON object. No preamble, no commentary, no code fences."""


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


VALID_SIG = {"keeper", "notable", "minor", "skip"}


async def process_one(client, sem, rel):
    path = NOTES_DIR / rel
    async with sem:
        parsed = parse_note(path)
        if parsed is None:
            return {"rel": rel, "error": "read_failed"}
        body = sample_body(parsed["body"])
        if not body.strip():
            return {
                "rel": rel,
                "title": parsed["title"],
                "date": parsed["date"],
                "body_chars": 0,
                "significance": "skip",
                "kernel": "empty note",
                "themes": [],
            }
        user_msg = f"TITLE: {parsed['title']}\nDATE: {parsed['date']}\n\n---\n{body}"
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return {"rel": rel, "error": "no_json", "raw": text[:200]}
            data = json.loads(m.group(0))
            sig = data.get("significance", "")
            if sig not in VALID_SIG:
                return {"rel": rel, "error": f"bad_sig:{sig}", "raw": text[:200]}
            return {
                "rel": rel,
                "title": parsed["title"],
                "date": parsed["date"],
                "body_chars": len(parsed["body"]),
                "significance": sig,
                "kernel": data.get("kernel", "").strip(),
                "themes": data.get("themes", []),
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


def load_existing(path: Path):
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if "rel" in r and "error" not in r:
                done.add(r["rel"])
        except json.JSONDecodeError:
            continue
    return done


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    targets = collect_targets()
    if CALIBRATION_MODE:
        targets = calibration_sample(targets)
        out_path = CALIBRATION_OUT
        print(f"CALIBRATION MODE — {len(targets)} notes")
        mode = "w"
    else:
        out_path = OUT_JSONL
        already = load_existing(out_path)
        before = len(targets)
        targets = [r for r in targets if r not in already]
        print(f"FULL RUN — {before} total, {len(already)} already done, {len(targets)} to process")
        mode = "a"

    if not targets:
        print("Nothing to do.")
        return

    client = AsyncAnthropic(max_retries=MAX_RETRIES)
    sem = asyncio.Semaphore(CONCURRENCY)

    out_f = out_path.open(mode, encoding="utf-8")
    tasks = [process_one(client, sem, rel) for rel in targets]
    done = 0
    counts = {"keeper": 0, "notable": 0, "minor": 0, "skip": 0, "error": 0}
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done += 1
        out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
        out_f.flush()
        if "error" in r:
            counts["error"] += 1
            print(f"[{done}/{len(targets)}] ERR {r['rel']}: {r.get('error')}", file=sys.stderr)
            continue
        sig = r["significance"]
        counts[sig] += 1
        marker = {"keeper": "★", "notable": "·", "minor": " ", "skip": "-"}[sig]
        themes = ",".join(r["themes"][:3])
        print(f"[{done}/{len(targets)}] {marker} {sig:<7} {r['rel']}")
        print(f"    {r['kernel']}  [{themes}]")
    out_f.close()

    total = sum(counts.values())
    print(f"\nDone. {total} notes processed.")
    for k in ("keeper", "notable", "minor", "skip", "error"):
        pct = (100 * counts[k] / total) if total else 0
        print(f"  {k:<8} {counts[k]:4d}  ({pct:.1f}%)")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
