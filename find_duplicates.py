#!/usr/bin/env python3
"""Scan _corpus/ for content-duplicate notes.

Hashes each note's body (frontmatter stripped, whitespace normalized) and
reports every hash-group with >1 file. Output grouped by body size.
"""
import hashlib
import re
from collections import defaultdict
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
OUT_TSV = CORPUS / "_duplicates.tsv"


def extract_body(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    body = m.group(2) if m else text
    return re.sub(r"\s+", " ", body).strip()


groups = defaultdict(list)
total = 0
for p in sorted(CORPUS.rglob("*.md")):
    text = p.read_text(encoding="utf-8", errors="replace")
    body = extract_body(text)
    if not body:
        continue
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    groups[h].append((p, len(body)))
    total += 1

dups = [(h, files) for h, files in groups.items() if len(files) > 1]
dups.sort(key=lambda x: -x[1][0][1])  # sort by body size desc

print(f"Scanned: {total} notes")
print(f"Duplicate groups: {len(dups)}")
print(f"Extra files (removable): {sum(len(fs) - 1 for _, fs in dups)}")

# Breakdown by size
by_size = {"tiny (<50)": 0, "small (50-500)": 0, "medium (500-2000)": 0, "large (2000+)": 0}
for _, files in dups:
    size = files[0][1]
    if size < 50:
        by_size["tiny (<50)"] += 1
    elif size < 500:
        by_size["small (50-500)"] += 1
    elif size < 2000:
        by_size["medium (500-2000)"] += 1
    else:
        by_size["large (2000+)"] += 1
print("\nBy body size:")
for k, v in by_size.items():
    print(f"  {k:<20} {v}")

# Show top 20 largest dup groups
print("\nLargest duplicate groups:")
for h, files in dups[:20]:
    size = files[0][1]
    print(f"\n  [{size} chars, {len(files)}×]")
    for p, _ in files:
        print(f"    {p.relative_to(CORPUS)}")

# Save full report
with OUT_TSV.open("w", encoding="utf-8") as f:
    f.write("group_id\tbody_chars\tn_files\tpath\n")
    for i, (h, files) in enumerate(dups):
        for p, size in files:
            f.write(f"{i}\t{size}\t{len(files)}\t{p.relative_to(CORPUS)}\n")
print(f"\nSaved: {OUT_TSV}")
