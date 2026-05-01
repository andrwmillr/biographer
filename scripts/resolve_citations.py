#!/usr/bin/env python3
"""Rewrite [YYYY-MM-DD] citations in an existing narrative file to Obsidian
wikilinks of the form [[rel|YYYY-MM-DD]]. In-place by default.

Usage:
    python3 _scripts/resolve_citations.py                  # rewrite biography.md
    python3 _scripts/resolve_citations.py biography_20260424_171813.md   # rewrite a specific snapshot
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from write_biography import (  # type: ignore
    BIOGRAPHIES_DIR,
    apply_date_overrides,
    load_corpus_notes,
    resolve_citations,
)


def main():
    target_name = sys.argv[1] if len(sys.argv) > 1 else "biography.md"
    target = BIOGRAPHIES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    notes = load_corpus_notes()
    apply_date_overrides(notes)
    body = target.read_text(encoding="utf-8")
    new_body, resolved, unresolved = resolve_citations(body, notes)
    target.write_text(new_body, encoding="utf-8")
    print(f"{target.name}: {resolved} citations linked, {unresolved} unresolved")


if __name__ == "__main__":
    main()
