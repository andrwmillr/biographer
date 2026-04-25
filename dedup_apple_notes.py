#!/usr/bin/env python3
"""Auto-dedup content-identical notes that differ only by Apple Notes
iCloud versioning suffixes ([N]).

A duplicate group is eligible for auto-dedup iff:
- all files are in the same label folder
- filename stems are identical after stripping " [N]" suffix

Keep preference: un-suffixed filename if it exists, else lowest [N].
Other dup groups (cross-label, renamed, manual duplicates) are reported
separately for manual review.

Also updates _signal.tsv and _poems.tsv to remove deleted source paths.
"""
import hashlib
import re
from collections import defaultdict
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
SIGNAL_TSV = CORPUS / "_signal.tsv"
POEMS_TSV = CORPUS / "_poems.tsv"

SUFFIX_RE = re.compile(r" \[(\d+)\](?=\.md$)")


def extract_body(text: str) -> str:
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    body = m.group(2) if m else text
    return re.sub(r"\s+", " ", body).strip()


def strip_suffix(name: str) -> tuple[str, int]:
    """Return (canonical_stem, suffix_num). suffix_num is 0 if no [N]."""
    m = SUFFIX_RE.search(name)
    if m:
        n = int(m.group(1))
        stem = SUFFIX_RE.sub("", name)
        return stem, n
    return name, 0


# --- Gather hash groups --------------------------------------------------
groups = defaultdict(list)
for p in sorted(CORPUS.rglob("*.md")):
    text = p.read_text(encoding="utf-8", errors="replace")
    body = extract_body(text)
    if not body:
        continue
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    groups[h].append(p)

dup_groups = [files for h, files in groups.items() if len(files) > 1]

# --- Classify: auto vs manual --------------------------------------------
auto_groups = []
manual_groups = []
for files in dup_groups:
    labels = {p.parent.name for p in files}
    stems = {strip_suffix(p.name)[0] for p in files}
    if len(labels) == 1 and len(stems) == 1:
        auto_groups.append(files)
    else:
        manual_groups.append(files)

print(f"Dup groups: {len(dup_groups)}")
print(f"  Auto-dedup eligible: {len(auto_groups)}")
print(f"  Manual review: {len(manual_groups)}")

# --- Plan deletions for auto groups --------------------------------------
to_delete = []  # list of Path
for files in auto_groups:
    # Sort: un-suffixed (suffix_num=0) first, then by [N] ascending
    sorted_files = sorted(files, key=lambda p: strip_suffix(p.name)[1])
    keep, *drop = sorted_files
    for p in drop:
        to_delete.append(p)

print(f"\nFiles to delete: {len(to_delete)}")

# --- Execute deletions ---------------------------------------------------
deleted_rels = set()
for p in to_delete:
    rel = str(p.relative_to(CORPUS))
    deleted_rels.add(rel)
    p.unlink()
print(f"Deleted {len(deleted_rels)} files.")

# --- Update TSVs ---------------------------------------------------------
def prune_tsv(path: Path, path_col: int = 0):
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header, *rows = lines
    kept = [header]
    removed = 0
    for row in rows:
        parts = row.split("\t")
        if len(parts) > path_col and parts[path_col] in deleted_rels:
            removed += 1
            continue
        kept.append(row)
    path.write_text("".join(kept), encoding="utf-8")
    return removed

n_signal = prune_tsv(SIGNAL_TSV)
n_poems = prune_tsv(POEMS_TSV)
print(f"Pruned _signal.tsv: removed {n_signal} rows")
print(f"Pruned _poems.tsv:  removed {n_poems} rows")

# --- Report manual review set -------------------------------------------
print(f"\n=== Manual review ({len(manual_groups)} groups) ===\n")
for files in sorted(manual_groups, key=lambda fs: -len(extract_body(fs[0].read_text(encoding="utf-8", errors="replace") if fs[0].exists() else ""))):
    if not all(p.exists() for p in files):
        continue
    size = len(extract_body(files[0].read_text(encoding="utf-8", errors="replace")))
    print(f"[{size} chars, {len(files)}×]")
    for p in files:
        print(f"    {p.relative_to(CORPUS)}")
    print()
