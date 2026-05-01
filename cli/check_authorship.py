#!/usr/bin/env python3
"""Flag keeper notes that look like clippings from other writers.

Phase A sometimes marked polished third-party essays (saved into Andrew's notes
app) as "keeper" because the prose is clearly good. When the narrative script
then quotes from those notes, Opus attributes the quote to Andrew. This script
asks Sonnet to classify each keeper as authored / clipped / unclear.

Output: _authorship.jsonl (one row per keeper). Resumable.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from anthropic import AsyncAnthropic

sys.path.insert(0, str(Path(__file__).parent))
from core.corpus import (  # type: ignore
    CORPUS,
    parse_note_body,
)

MODEL = "claude-sonnet-4-6"
CONCURRENCY = 5
MAX_RETRIES = 8
OUT_JSONL = CORPUS / "_derived" / "_authorship.jsonl"
PHASE_A = CORPUS / "_derived" / "_phase_a.jsonl"
TRIAGE_STATE = CORPUS / "_config" / "_triage_state.json"
RATING_TO_SIG = {1: "skip", 2: "minor", 3: "notable", 4: "keeper"}


def load_phase_a():
    """Return list of dicts from _phase_a.jsonl: rel, title, date, body_chars,
    significance, kernel, themes. Latest row wins per rel."""
    out = {}
    if not PHASE_A.exists():
        return []
    for line in PHASE_A.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r or "rel" not in r:
            continue
        out[r["rel"]] = r
    return list(out.values())


def apply_triage_overrides(notes):
    """Override n['significance'] from manual ratings in _triage_state.json."""
    if not TRIAGE_STATE.exists():
        return 0
    state = json.loads(TRIAGE_STATE.read_text())
    decisions = state.get("decisions", {})
    applied = 0
    for n in notes:
        rating = decisions.get(n["rel"])
        if rating in RATING_TO_SIG:
            n["significance"] = RATING_TO_SIG[rating]
            applied += 1
    return applied

MAX_CHARS = 3000
HEAD_CHARS = 1500
TAIL_CHARS = 1500

SYSTEM_PROMPT = """You are helping Andrew audit his personal writing archive. Some notes are things he wrote himself. Others are clippings — essays, articles, or passages from other writers he saved into his notes app. The narrative synthesizer keeps quoting from the clippings as if Andrew wrote them.

For the note you see, return strict JSON:
{
  "authored": "yes" | "no" | "mixed" | "unclear",
  "confidence": "high" | "medium" | "low",
  "reason": "one short phrase"
}

- "yes": the note is Andrew's own writing (his journal, his poem draft, his letter, his essay attempt). Even if it's polished, even if it references other writers, the words are his.
- "no": the note is verbatim text from another source — an essay he saved, a substack post, song lyrics, a quoted passage, an article. The words are not his.
- "mixed": Andrew's own writing surrounds or interleaves a clipped passage (e.g. his commentary plus a quoted paragraph).
- "unclear": you can't tell.

Signals that something is NOT Andrew's:
- Opens "Your X is also in letting Y…" or similar 2nd-person self-help register
- Polished essay tone completely unlike his usual lowercase, messy, 1st-person journal voice
- Contains a phrase like "my recent essay" where the author is clearly someone else
- Reads like substack / therapy writing / song lyrics / article excerpt

Signals that something IS Andrew's:
- First-person ("i", "I"), lowercase journal style, his typical run-on sentences
- Names of his friends/family (Mollie, Sarah, McKenna, Grace, Jacob, Hope, etc.)
- Specific personal memories, dates, locations from his life
- His characteristic voice — even when he's trying on another writer's style, his drafts have his fingerprints

Return ONLY the JSON. No preamble."""


def sample_body(body: str) -> str:
    if len(body) <= MAX_CHARS:
        return body
    return f"{body[:HEAD_CHARS]}\n\n…[middle snipped]…\n\n{body[-TAIL_CHARS:]}"


VALID = {"yes", "no", "mixed", "unclear"}


async def classify(client, sem, n):
    rel = n["rel"]
    body = parse_note_body(rel)
    if not body:
        return {"rel": rel, "authored": "unclear", "confidence": "low", "reason": "empty"}
    async with sem:
        title = n.get("title") or ""
        date = (n.get("date") or "")[:10]
        user_msg = f"TITLE: {title}\nDATE: {date}\nLABEL: {rel.split('/', 1)[0]}\n\n---\n{sample_body(body)}"
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return {"rel": rel, "error": "no_json", "raw": text[:200]}
            data = json.loads(m.group(0))
            a = data.get("authored", "")
            if a not in VALID:
                return {"rel": rel, "error": f"bad_authored:{a}"}
            return {
                "rel": rel,
                "title": title,
                "date": date,
                "authored": a,
                "confidence": data.get("confidence", ""),
                "reason": data.get("reason", "").strip(),
            }
        except json.JSONDecodeError as e:
            return {"rel": rel, "error": f"json:{str(e)[:100]}"}
        except Exception as e:
            return {"rel": rel, "error": f"api:{type(e).__name__}:{str(e)[:120]}"}


def load_existing():
    if not OUT_JSONL.exists():
        return set()
    done = set()
    for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
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

    notes = load_phase_a()
    apply_triage_overrides(notes)
    keepers = [n for n in notes if n.get("significance") == "keeper"]
    print(f"{len(keepers)} keepers total")

    done = load_existing()
    todo = [n for n in keepers if n["rel"] not in done]
    print(f"{len(done)} already checked, {len(todo)} to go")
    if not todo:
        print("nothing to do — summarizing existing results")
    else:
        client = AsyncAnthropic(max_retries=MAX_RETRIES)
        sem = asyncio.Semaphore(CONCURRENCY)
        out_f = OUT_JSONL.open("a", encoding="utf-8")
        tasks = [classify(client, sem, n) for n in todo]
        processed = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            processed += 1
            out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
            out_f.flush()
            if "error" in r:
                print(f"[{processed}/{len(todo)}] ERR {r['rel']}: {r['error']}", file=sys.stderr)
                continue
            a = r["authored"]
            mark = {"yes": " ", "no": "✗", "mixed": "~", "unclear": "?"}[a]
            print(f"[{processed}/{len(todo)}] {mark} {a:<7} {r['rel']}  — {r['reason']}")
        out_f.close()

    print()
    print("=== NOT ANDREW'S WRITING ===")
    counts = {"yes": 0, "no": 0, "mixed": 0, "unclear": 0}
    for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r:
            continue
        a = r.get("authored", "")
        if a not in counts:
            continue
        counts[a] += 1
        if a in ("no", "mixed", "unclear"):
            print(f"  {a:<7} ({r.get('confidence','?')})  {r['rel']}")
            print(f"          — {r.get('reason','')}")
    total = sum(counts.values())
    print()
    print(f"Total keepers classified: {total}")
    for k in ("yes", "no", "mixed", "unclear"):
        print(f"  {k:<8} {counts[k]}")


if __name__ == "__main__":
    asyncio.run(main())
