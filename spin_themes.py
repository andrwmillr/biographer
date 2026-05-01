#!/usr/bin/env python3
"""Spin THEMES_R1.md against the corpus.

Builds a folder-aware sample of notes (top-N longest journal/creative + all
poetry + all letters per era), assembles a user message with era metadata
and corpus-wide titles, and pipes it to the `claude` CLI with THEMES_R1.md
as the system prompt. Writes the assembled input and the model output to
_corpus/claude/themes/run_<stamp>/.

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

# Reuse helpers from write_biography.
sys.path.insert(0, str(Path(__file__).parent))
import write_biography as wb


CORPUS = wb.CORPUS
THEMES_PROMPT = (Path(__file__).parent / "THEMES_R1.md").read_text(encoding="utf-8")
THEMES_PROMPT = THEMES_PROMPT.replace("__SUBJECT__", wb.SUBJECT_NAME)

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


def folder_aware_sample(notes_in_era, top_n, corpus_id=None):
    """Per-era sample: top-N longest notes per discovered folder.

    Folder = first path segment of `rel`; notes without a slash bucket
    under "_" (single flat folder). Each bucket yields up to top-N
    longest notes (or all if fewer). Returns notes with bodies attached,
    sorted by date.

    Generalized from a hard-coded {journal, creative, poetry, letter,
    fiction} layout — corpora with arbitrary folder names work without
    sampling to zero."""
    by_label: dict[str, list[dict]] = {}
    for n in notes_in_era:
        rel = n["rel"]
        label = rel.split("/", 1)[0] if "/" in rel else "_"
        body = wb.parse_note_body(rel, corpus_id) if corpus_id else wb.parse_note_body(rel)
        if not body:
            continue
        n2 = dict(n)
        n2["body"] = body
        n2["body_len"] = len(body)
        by_label.setdefault(label, []).append(n2)
    sampled = []
    for notes in by_label.values():
        sampled.extend(sorted(notes, key=lambda x: -x["body_len"])[:top_n])
    sampled.sort(key=lambda x: x.get("date", ""))
    return sampled


def build_input(top_n, corpus_id=None):
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    by_era = {}
    for n in notes:
        era = wb.era_of(n.get("date", ""), eras)
        if era:
            by_era.setdefault(era, []).append(n)

    lines = []
    lines.append(f"# Corpus overview")
    lines.append("")
    lines.append(f"Subject: {wb.SUBJECT_NAME}")
    lines.append(f"Total notes: {len(notes)}")
    lines.append("")
    lines.append("## Eras")
    lines.append("")
    for name, lo, hi in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        actual_lo, actual_hi = wb.era_date_range(era_notes)
        lines.append(f"- **{name}** ({actual_lo} – {actual_hi}) — {len(era_notes)} notes")
    lines.append("")

    lines.append("## Per-era sample (folder-aware)")
    lines.append("")
    lines.append(
        f"You are seeing a sample of the corpus, not every note. Per era: "
        f"the top-{top_n} longest journal entries + the top-{top_n} longest "
        f"creative pieces + every poetry note + every letter. Short journal "
        f"and creative notes outside this sample are not shown."
    )
    lines.append("")
    for name, _, _ in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        sampled = folder_aware_sample(era_notes, top_n, corpus_id)
        lines.append(f"### {name} — {len(sampled)} sampled notes")
        lines.append("")
        for n in sampled:
            date = (n.get("date") or "")[:10]
            title = n.get("title") or "(untitled)"
            label = n["rel"].split("/", 1)[0]
            lines.append(f"==== [{date}] · {label} · {title} ====")
            lines.append("")
            lines.append(n["body"])
            lines.append("")
    return "\n".join(lines)


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
