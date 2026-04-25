#!/usr/bin/env python3
"""Dump CHAPTER_SYSTEM and the per-era user message to .md files for
manual paste into Claude Desktop / Claude Code. Reuses narrative_phase_b.py
loaders so the dump matches what the API would see.

Output: _corpus/artifacts/narratives/_dump/<era>/<timestamp>/{system.md, user.md}

Each invocation creates a fresh timestamped subdirectory so prior dumps
(and any output/ subdirs alongside them) are preserved.

Usage: python3 dump_era.py "Amherst I"
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from narrative_phase_b import (
    CHAPTER_SYSTEM, CORPUS, ERAS, apply_authorship, apply_date_overrides,
    apply_note_about, apply_triage_overrides, build_user_msg, era_of,
    flag_date_clusters, load_authorship, load_era_context, load_phase_a,
)

ERA_NAMES = [name for name, _, _ in ERAS]

if len(sys.argv) < 2 or sys.argv[1] not in ERA_NAMES:
    print(f"Usage: {sys.argv[0]} <era>", file=sys.stderr)
    print(f"Eras: {', '.join(repr(n) for n in ERA_NAMES)}", file=sys.stderr)
    sys.exit(1)

era_name = sys.argv[1]

all_notes = load_phase_a()
apply_triage_overrides(all_notes)
apply_date_overrides(all_notes)
verdicts = load_authorship()
all_notes, _, _ = apply_authorship(all_notes, verdicts)
apply_note_about(all_notes)
n_uncertain = flag_date_clusters(all_notes)
era_context_map = load_era_context()

era_notes = [n for n in all_notes if era_of(n.get("date", "")) == era_name]
if not era_notes:
    print(f"No notes for era {era_name!r}", file=sys.stderr)
    sys.exit(1)

era_context = era_context_map.get(era_name, "")
user_msg = build_user_msg(era_name, era_notes, era_context=era_context)

slug = era_name.replace(" ", "_").replace("/", "-")
stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
dump_dir = CORPUS / "artifacts" / "narratives" / "_dump" / slug / stamp
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
header = f"<!-- prompt_sha: {sha}{dirty}  era: {era_name}  generated: {stamp} -->\n\n"

(dump_dir / "system.md").write_text(header + CHAPTER_SYSTEM, encoding="utf-8")
(dump_dir / "user.md").write_text(header + user_msg, encoding="utf-8")

era_uncertain = sum(1 for n in era_notes if n.get("date_uncertain"))
print(f"dumped {era_name} ({len(era_notes)} notes, {era_uncertain} date-uncertain) to {dump_dir}")
print(f"  prompt_sha: {sha}{dirty}")
print(f"  system.md:  {len(CHAPTER_SYSTEM):,} chars")
print(f"  user.md:    {len(user_msg):,} chars")
