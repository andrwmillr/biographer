#!/usr/bin/env python3
"""Generate canonical chapters + themes for a corpus.

For sample corpora, this is the way to seed the canonical baseline that
visitors see in read mode. For owned corpora, it's a way to bulk-bootstrap
without doing every era interactively.

Usage:
  python3 _web/cli/build_canonical.py <corpus_slug>
  nohup python3 _web/cli/build_canonical.py c_thoreau > /tmp/canonical.log 2>&1 &

What it does, per corpus:
  1. For each era with notes: claude -p with CHAPTER_SYSTEM as system, the
     era's notes + prior canonical chapters as user message. Writes to
     chapters/<era_slug>.md (canonical).
  2. claude -p round-1 themes: THEMES_R1.md as system, folder-aware
     corpus sample as user. Captures the candidate list (~10 themes).
  3. claude -p auto-curate: CURATE.md as system, the round-1 list as
     user with an instruction to collapse to ~5 final themes in LOCKED
     THEMES format. Writes to themes/canonical.md.

All claude -p calls scrub ANTHROPIC_API_KEY so they use the user's
subscription credits, not the API account.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Put _web/ on sys.path so `from core.X import …` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import corpus as wb
from core.sampling import build_input


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "core" / "prompts"
THEMES_R1_PROMPT = PROMPTS_DIR / "themes_r1.md"
CURATE_PROMPT = PROMPTS_DIR / "curate.md"
MODEL = "claude-opus-4-7"

START = time.time()


def log(msg: str) -> None:
    elapsed = int(time.time() - START)
    print(f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {msg}", flush=True)


def claude_p(system_prompt: str, user_msg: str) -> str:
    """Run `claude -p` with the given system prompt + user message via
    stdin. Uses --output-format stream-json so we can reliably capture
    text content via content_block_delta events (plain claude -p stdout
    is unreliable for large prompts). ANTHROPIC_API_KEY is scrubbed so
    the subscription path is used."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.Popen(
        [
            "claude", "-p",
            "--model", MODEL,
            "--system-prompt", system_prompt,
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

    chunks: list[str] = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "stream_event":
            inner = evt.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        chunks.append(text)

    proc.wait()
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.read() or "")
        sys.exit(proc.returncode)
    return "".join(chunks)


def build_chapter(corpus_id: str, era_name: str, era_notes: list, by_era: dict) -> None:
    """Generate one era's chapter and write to canonical chapters/<slug>.md.
    Includes prior canonical chapters as context for continuity."""
    prior = wb.load_prior_chapters(era_name, corpus_id)
    prior_blocks = [
        f"## {wb.era_heading(n, by_era[n])}\n\n{t}" for n, t in prior
    ]

    era_msg = wb.build_user_msg(era_name, era_notes, corpus_id=corpus_id)

    parts: list[str] = [wb.subject_context_for(corpus_id)]
    if prior_blocks:
        parts.append("--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n")
        for p in prior_blocks:
            parts.append(p + "\n\n")
        parts.append("--- END PRIOR CHAPTERS ---\n\n")
    parts.append(era_msg)
    user_msg = "".join(parts)

    # CHAPTER_SYSTEM is tuned for the SDK interactive flow (with checkpoints,
    # narration, tool calls). In a one-shot claude -p context the agent
    # otherwise tends to emit a meta-preamble ("I'll read this and draft…")
    # or fall into thread-digest format on small eras. Override via the user
    # message: chapter prose only.
    user_msg = (
        user_msg
        + "\n\n--- ONE-SHOT INSTRUCTIONS ---\n\n"
        "This is a one-shot non-interactive run. You have NO tools available "
        "(no Write, no Edit, no Read). Do not attempt tool calls.\n\n"
        "Output ONLY the chapter prose — what would appear in the printed "
        "book. No preamble, no narration about your process, no checkpoints, "
        "no 'I'll draft this in a single pass' meta. No thread-digest "
        "format (no '## Subject framing', '## Threads', '## People', "
        "'## Picked up from earlier', '## Open / unresolved' headings). "
        "Just the chapter, ready to drop into the retrospective."
    )

    log(f"  era '{era_name}': {len(era_notes)} notes, {len(user_msg):,} chars in")
    text = claude_p(wb.CHAPTER_SYSTEM, user_msg).strip()

    canonical = wb.chapters_dir(corpus_id) / f"{wb.era_slug(era_name)}.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(text, encoding="utf-8")
    log(f"    → {canonical} ({len(text.split())} words)")


def build_themes(corpus_id: str) -> None:
    """Round-1 + auto-curate to ~5 final themes. Writes to themes/canonical.md."""
    subject = wb.SUBJECT_NAME
    # Use subject_context_for to derive a sensible per-corpus subject string
    # for the __SUBJECT__ tokens still present in the theme prompts.
    meta = wb._corpus_paths(corpus_id)["root"] / "_meta.json"
    if meta.exists():
        try:
            import json
            data = json.loads(meta.read_text(encoding="utf-8"))
            if data.get("title"):
                subject = data["title"]
        except Exception:
            pass

    themes_r1 = THEMES_R1_PROMPT.read_text(encoding="utf-8").replace("__SUBJECT__", subject)
    curate = CURATE_PROMPT.read_text(encoding="utf-8").replace("__SUBJECT__", subject)

    # Round-1: corpus sample → ~10 candidate themes.
    corpus_sample, _ = build_input(top_n=5, corpus_id=corpus_id)
    log(f"  themes round-1: {len(corpus_sample):,} chars in")
    round1 = claude_p(themes_r1, corpus_sample).strip()
    log(f"    round-1 produced {len(round1):,} chars ({len(round1.split())} words)")

    # Auto-curate: round-1 → ~5 final themes.
    log("  themes auto-curate: collapsing to ~5 final themes")
    curate_msg = (
        "This is a one-shot non-interactive run. You have NO tools available "
        "(no Write, no Edit, no Read). Do not attempt to call them — write "
        "directly to your text response.\n\n"
        "Below is a round-1 themes list. Curate down to 5 final themes — "
        "merge duplicates, drop weak candidates, refine names. Output ONLY "
        "the LOCKED THEMES block (the body that would normally go inside "
        "themes.md), in the format from your system prompt's LOCKING "
        "section. Do NOT emit `## Current state`, do NOT emit "
        "`[locked] wrote themes.md.` (you have no Write tool — there's "
        "nothing to write to), do NOT preamble. Just the themes. Be "
        "decisive.\n\n"
        "--- ROUND-1 THEMES ---\n\n"
        f"{round1}\n\n"
        "--- END ROUND-1 ---\n"
    )
    final = claude_p(curate, curate_msg).strip()

    canonical = wb.corpus_root(corpus_id) / "claude" / "themes" / "canonical.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(final, encoding="utf-8")
    log(f"    → {canonical} ({len(final.split())} words)")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(
            "Usage: build_canonical.py <corpus_slug> [--era <name>] [--themes-only] [--skip-themes]",
            file=sys.stderr,
        )
        sys.exit(2)
    slug = args[0]
    era_filter: str | None = None
    themes_only = False
    skip_themes = False
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--era" and i + 1 < len(args):
            era_filter = args[i + 1]; i += 2
        elif a == "--themes-only":
            themes_only = True; i += 1
        elif a == "--skip-themes":
            skip_themes = True; i += 1
        else:
            print(f"Unknown flag: {a}", file=sys.stderr); sys.exit(2)

    paths = wb._corpus_paths(slug)
    if not paths["notes"].exists():
        log(f"corpus '{slug}' has no notes dir at {paths['notes']} — aborting")
        sys.exit(1)

    log(f"corpus: {slug}")

    if not themes_only:
        notes = wb.load_corpus_notes(slug)
        wb.apply_date_overrides(notes, slug)
        wb.apply_note_metadata(notes, slug)
        eras = wb.load_eras(slug)

        by_era: dict[str, list] = {}
        for n in notes:
            e = wb.era_of(n.get("date", ""), eras)
            if e:
                by_era.setdefault(e, []).append(n)

        eras_with_notes = [(name, by_era[name]) for name, _, _ in eras if by_era.get(name)]
        if era_filter:
            eras_with_notes = [(n, en) for n, en in eras_with_notes if n == era_filter]
            if not eras_with_notes:
                log(f"--era {era_filter!r} matched no era with notes")
                sys.exit(1)
        log(f"{len(eras_with_notes)} era(s) selected; chapters first")

        for name, era_notes in eras_with_notes:
            build_chapter(slug, name, era_notes, by_era)

    if skip_themes:
        log("done (chapters only).")
        return

    if not themes_only:
        log("chapters done; running themes round-1 + auto-curate")
    build_themes(slug)

    log("done.")


if __name__ == "__main__":
    main()
