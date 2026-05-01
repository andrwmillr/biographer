#!/usr/bin/env python3
"""Spin THEMES_R1 against the corpus (CLI).

Builds the folder-aware sample (via `core.sampling.build_input`),
assembles a user message, and pipes it to the `claude` CLI with
THEMES_R1 as the system prompt. Writes input + output to
_corpus/claude/themes/run_<stamp>/.

The web flow (`api/themes.py`) imports the helpers from `core.sampling`
directly; this module is the standalone CLI entry point.

ANTHROPIC_API_KEY is scrubbed from the subprocess env so the CLI uses
subscription credits.

Flags:
  --top-n N        notes per era for journal/creative (default 10)
  --model KEY      opus-4.7 (default), opus-4.6, sonnet-4.6
  --no-call        assemble input only, don't call claude (for inspection)
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Put _web/ on the path so `from core.X import ...` resolves when
# running this file directly (`python3 _web/cli/spin_themes.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import corpus as wb
from core.sampling import build_input


CORPUS = wb.CORPUS
THEMES_PROMPT_PATH = Path(__file__).resolve().parent.parent / "core" / "prompts" / "themes_r1.md"
THEMES_PROMPT = THEMES_PROMPT_PATH.read_text(encoding="utf-8").replace("__SUBJECT__", wb.SUBJECT_NAME)

OUT_DIR = CORPUS / "claude" / "themes" / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def parse_flags():
    top_n = 10
    model = wb.MODELS["opus-4.7"]
    no_call = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--top-n" and i + 1 < len(args):
            top_n = int(args[i + 1]); i += 2
        elif a == "--model" and i + 1 < len(args):
            key = args[i + 1]
            model = wb.MODELS.get(key, key); i += 2
        elif a == "--no-call":
            no_call = True; i += 1
        else:
            print(f"Unknown flag: {a}", file=sys.stderr); sys.exit(2)
    return top_n, model, no_call


def call_claude(user_msg, model):
    """Stream output via claude's stream-json format. Prints deltas to stdout
    as they arrive; returns the full assembled text for saving."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            "--model", model,
            "--system-prompt", THEMES_PROMPT,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--no-session-persistence",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    proc.stdin.write(user_msg)
    proc.stdin.close()

    chunks = []
    result_evt = None
    started = False
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = evt.get("type")
        if t == "stream_event":
            inner = evt.get("event", {})
            itype = inner.get("type")
            if itype == "message_start" and not started:
                print("[generating]", file=sys.stderr, flush=True)
                started = True
            elif itype == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        chunks.append(text)
                        sys.stdout.write(text)
                        sys.stdout.flush()
        elif t == "result":
            result_evt = evt

    proc.wait()
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.read() or "")
        sys.exit(proc.returncode)
    print("", flush=True)

    if result_evt:
        usage = result_evt.get("usage", {})
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        cache_write = usage.get("cache_creation_input_tokens", 0) or 0
        cost = result_evt.get("total_cost_usd", 0)
        print(
            f"[usage] input={in_tok:,} output={out_tok:,} "
            f"cache_read={cache_read:,} cache_write={cache_write:,} "
            f"cost=${cost:.4f}",
            file=sys.stderr,
            flush=True,
        )

    return "".join(chunks)


def main():
    top_n, model, no_call = parse_flags()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Building input (top-{top_n} per era, folder-aware)...", file=sys.stderr)
    user_msg = build_input(top_n)
    in_path = OUT_DIR / "input.md"
    in_path.write_text(user_msg, encoding="utf-8")
    print(f"Wrote input ({len(user_msg):,} chars) to {in_path}", file=sys.stderr)

    if no_call:
        print("--no-call set; stopping after input assembly.", file=sys.stderr)
        return

    print(f"Calling claude ({model})...", file=sys.stderr)
    out = call_claude(user_msg, model)
    out_path = OUT_DIR / "output.md"
    out_path.write_text(out, encoding="utf-8")
    print(f"Wrote output to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
