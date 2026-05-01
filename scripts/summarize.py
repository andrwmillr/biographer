#!/usr/bin/env python3
"""Iterate on the title-page summary against a fixed chapter input.

Reads chapters from a previous narrative run, runs the SUMMARY prompt,
prints output. Fast loop for tuning the summary prompt or harvesting
variants — ~30 seconds per call, no chapter regeneration.

Usage:
    python3 _web/scripts/summarize.py                                          # default: 17:18 run
    python3 _web/scripts/summarize.py _history/biography_20260424_171813.md   # explicit
"""
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from anthropic import AsyncAnthropic

CORPUS = Path(__file__).resolve().parent.parent / "_corpora" / "andrew"
BIOGRAPHIES_DIR = CORPUS / "claude" / "biographies"
SUMMARIES_DIR = CORPUS / "claude" / "summaries"
MODEL = "claude-opus-4-7"
DEFAULT_INPUT = "_history/biography_20260424_171813.md"

SUMMARY_SYSTEM = """You are writing the opening synthesis to a retrospective on Andrew's personal writing archive, 2011-2026. Plain third-person voice — a thoughtful biographer, not a literary critic.

You'll receive era chapters covering fifteen years. Write 500-1000 words covering the through-lines: what preoccupied him across all fifteen years, what changed, what didn't, what the arc looks like from a distance.

VOICE
Plain, direct English. Short sentences welcome. Write so a friend can follow. Third person. No literary-critic jargon ("revisionary sentence", "ars poetica", "the register", "signature gesture", etc.). No ornate sentence structures for their own sake.

ABSOLUTE RULES
- Work only from what the chapters say. Don't introduce new specifics, names, dates, or quotes.
- If you use a quote or specific detail from a chapter, keep any [YYYY-MM-DD] citation it carries.
- No invented imagery. Abstract patterns are fine without citation; specifics need the source chapter's anchor.
- No "in conclusion", no enumerating the era names, no "this retrospective".
- Third person for Andrew. No sentimentalizing.
- If being accurate and plain makes the paragraph thinner, that's fine."""


def extract_chapters(body: str) -> str:
    """Strip the title-page summary, keep just the era chapters from the
    first `## ` marker onward."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[i:])
    return body


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    target_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    target = BIOGRAPHIES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found", file=sys.stderr)
        sys.exit(1)

    body = target.read_text(encoding="utf-8")
    chapters_text = extract_chapters(body)
    print(f"input: {target.name}  ({len(chapters_text):,} chars)", file=sys.stderr)

    client = AsyncAnthropic()
    t0 = time.time()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SUMMARY_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"ERA CHAPTERS:\n\n{chapters_text}",
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        }],
    )
    dt = time.time() - t0
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    words = len(text.split())
    u = resp.usage
    in_tok = getattr(u, "input_tokens", 0)
    out_tok = getattr(u, "output_tokens", 0)
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_status = (
        f"cache hit ({cache_read:,} tok)" if cache_read
        else f"cache write ({cache_write:,} tok)" if cache_write
        else "no cache"
    )
    print(f"done in {dt:.0f}s, {words} words (in: {in_tok:,} tok, out: {out_tok:,} tok, {cache_status})", file=sys.stderr)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = SUMMARIES_DIR / f"summary_{stamp}.md"
    front = (
        f"---\n"
        f"input: {target.name}\n"
        f"model: {MODEL}\n"
        f"words: {words}\n"
        f"---\n\n"
    )
    out_path.write_text(front + text + "\n", encoding="utf-8")
    print(f"wrote {out_path.relative_to(CORPUS)}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
