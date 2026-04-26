#!/usr/bin/env python3
"""Inject label and alt_label from _signal.tsv into each note's YAML
frontmatter so Obsidian can filter by them as properties.

Idempotent: re-running updates existing label/alt_label lines instead of
duplicating. Notes in evernote/clipped/ (not in signal.tsv) get label: clipped
so every note has a label property.
"""
import re
import sys
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
SIGNAL_TSV = CORPUS / "_derived" / "_signal.tsv"

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
LABEL_LINE = re.compile(r"^(label|alt_label)\s*:.*$", re.MULTILINE)


def main():
    labels = {}  # rel -> (label, alt_label)
    with SIGNAL_TSV.open() as f:
        f.readline()
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            rel, _source, _date, _title, _bc, label, alt = parts[:7]
            labels[rel] = (label, alt)

    print(f"Labels loaded: {len(labels)}")

    updated = 0
    clipped_tagged = 0
    skipped = 0
    errors = 0
    for path in CORPUS.rglob("*.txt"):
        rel = str(path.relative_to(CORPUS))
        if rel.startswith("_"):  # _signal.tsv etc
            continue

        if rel in labels:
            label, alt = labels[rel]
        elif rel.startswith("evernote/clipped/"):
            label, alt = "clipped", ""
            clipped_tagged += 1
        else:
            skipped += 1
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"ERR read {rel}: {e}", file=sys.stderr)
            errors += 1
            continue

        m = FM_RE.match(text)
        if not m:
            skipped += 1
            continue
        fm = m.group(1)
        rest = text[m.end():]

        # Remove any existing label / alt_label lines
        fm_cleaned = LABEL_LINE.sub("", fm).rstrip("\n")

        # Build new label lines
        new_lines = [f'label: "{label}"']
        if alt:
            new_lines.append(f'alt_label: "{alt}"')

        new_fm = fm_cleaned + "\n" + "\n".join(new_lines)
        new_text = f"---\n{new_fm}\n---\n{rest}"

        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            updated += 1

    print(f"Updated: {updated}")
    print(f"Clipped tagged: {clipped_tagged}")
    print(f"Skipped (no frontmatter or _meta): {skipped}")
    print(f"Errors: {errors}")


if __name__ == "__main__":
    main()
