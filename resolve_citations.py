#!/usr/bin/env python3
"""Rewrite [YYYY-MM-DD] citations in an existing narrative file to Obsidian
wikilinks of the form [[rel|YYYY-MM-DD]]. In-place by default.

Usage:
    python3 _raw/resolve_citations.py                  # rewrite _narrative_naive.md
    python3 _raw/resolve_citations.py _narrative.md    # rewrite curated narrative
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from narrative_phase_b import (  # type: ignore
    CORPUS,
    NARRATIVES_DIR,
    apply_date_overrides,
    load_phase_a,
    resolve_citations,
)


def main():
    target_name = sys.argv[1] if len(sys.argv) > 1 else "_narrative_naive.md"
    target = NARRATIVES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    notes = load_phase_a()
    apply_date_overrides(notes)
    body = target.read_text(encoding="utf-8")
    new_body, resolved, unresolved = resolve_citations(body, notes)
    target.write_text(new_body, encoding="utf-8")
    print(f"{target.name}: {resolved} citations linked, {unresolved} unresolved")


if __name__ == "__main__":
    main()
