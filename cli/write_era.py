#!/usr/bin/env python3
"""Refresh the per-era system.md and user.md inputs that the iteration
flow (run.sh) consumes. Reuses write_biography.py loaders so the dump
matches what the API run would see.

Output: _corpus/claude/biographies/_dump/<slug>/{system.md, user.md}
        — overwritten in place each invocation.

Usage: python3 write_era.py "Amherst I" [--future]

  --future  also include any later eras' chapters/digests already on disk
            from a previous run (hindsight context).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core.corpus import (
    CHAPTER_SYSTEM, CORPUS, ERAS, apply_authorship, apply_date_overrides,
    apply_note_metadata, build_user_msg, era_of, era_slug, flag_date_clusters,
    load_authorship, load_corpus_notes, load_prior_chapters,
    load_prior_thread_digests, load_future_chapters,
    load_future_thread_digests,
)

ERA_NAMES = [name for name, _, _ in ERAS]

include_future = "--future" in sys.argv
positional = [a for a in sys.argv[1:] if not a.startswith("--")]

if not positional or positional[0] not in ERA_NAMES:
    print(f"Usage: {sys.argv[0]} <era> [--future]", file=sys.stderr)
    print(f"Eras: {', '.join(repr(n) for n in ERA_NAMES)}", file=sys.stderr)
    sys.exit(1)

era_name = positional[0]

all_notes = load_corpus_notes()
apply_date_overrides(all_notes)
verdicts = load_authorship()
all_notes, _, _ = apply_authorship(all_notes, verdicts)
apply_note_metadata(all_notes)
flag_date_clusters(all_notes)

era_notes = [n for n in all_notes if era_of(n.get("date", "")) == era_name]
if not era_notes:
    print(f"No notes for era {era_name!r}", file=sys.stderr)
    sys.exit(1)

prior_chapters = load_prior_chapters(era_name)
prior_digests = load_prior_thread_digests(era_name)
future_chapters = load_future_chapters(era_name) if include_future else []
future_digests = load_future_thread_digests(era_name) if include_future else []
user_msg = build_user_msg(
    era_name, era_notes,
    prior_chapters=prior_chapters,
    prior_digests=prior_digests,
    future_chapters=future_chapters,
    future_digests=future_digests,
)

slug = era_slug(era_name)
dump_dir = CORPUS / "claude" / "biographies" / "_dump" / slug
dump_dir.mkdir(parents=True, exist_ok=True)


def _prompt_sha():
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _is_dirty():
    try:
        out = subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return bool(out)
    except Exception:
        return False


sha = _prompt_sha()
dirty = "-dirty" if _is_dirty() else ""
stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
header = f"<!-- prompt_sha: {sha}{dirty}  era: {era_name}  generated: {stamp} -->\n\n"

(dump_dir / "system.md").write_text(header + CHAPTER_SYSTEM, encoding="utf-8")
(dump_dir / "user.md").write_text(header + user_msg, encoding="utf-8")

era_uncertain = sum(1 for n in era_notes if n.get("date_uncertain"))
print(f"refreshed {era_name} ({len(era_notes)} notes, {era_uncertain} date-uncertain) → {dump_dir}")
print(f"  prompt_sha: {sha}{dirty}")
print(f"  system.md:  {len(CHAPTER_SYSTEM):,} chars")
print(f"  user.md:    {len(user_msg):,} chars")
if prior_chapters:
    print(f"  prior chapters: {len(prior_chapters)} ({', '.join(name for name, _ in prior_chapters)})")
if prior_digests:
    print(f"  prior digests:  {len(prior_digests)} ({', '.join(name for name, _ in prior_digests)})")
if future_chapters:
    print(f"  future chapters: {len(future_chapters)} ({', '.join(name for name, _ in future_chapters)})")
if future_digests:
    print(f"  future digests:  {len(future_digests)} ({', '.join(name for name, _ in future_digests)})")
