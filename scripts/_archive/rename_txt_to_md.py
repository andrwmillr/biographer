#!/usr/bin/env python3
"""Rename all _corpus/**/*.txt to .md so Obsidian renders them natively.
Also updates source_path column in _signal.tsv and _signal_reclass.tsv.
"""
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"

renamed = 0
for p in CORPUS.rglob("*.txt"):
    new = p.with_suffix(".md")
    if new.exists():
        print(f"SKIP (target exists): {p}")
        continue
    p.rename(new)
    renamed += 1

print(f"Renamed: {renamed}")

# Update source_path column in TSVs
for tsv in [CORPUS / "_derived" / "_signal.tsv", CORPUS / "_derived" / "_signal_reclass.tsv"]:
    if not tsv.exists():
        continue
    text = tsv.read_text(encoding="utf-8")
    new = text.replace(".txt\t", ".md\t")
    if new != text:
        tsv.write_text(new, encoding="utf-8")
        print(f"Updated TSV paths in: {tsv.name}")
