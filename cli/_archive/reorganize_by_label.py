#!/usr/bin/env python3
"""Reorganize _corpus/ so the top-level folders are labels (journal/,
creative/, poetry/, letter/, reference/, todo/, business/, contact/, code/,
other/, clipped/). The source ('apple-notes', 'debrief', etc.) stays as a
frontmatter property.

Derives 'poetry' as a new label for notes classified as 'poem' in _poems.tsv
(previously these were labeled 'creative'). Also updates that label in TSV
and in each note's frontmatter.
"""
import re
import shutil
import sys
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
SIGNAL_TSV = CORPUS / "_derived" / "_signal.tsv"
POEMS_TSV = CORPUS / "_derived" / "_poems.tsv"

OLD_SOURCE_DIRS = ["apple-notes", "debrief", "evernote", "letters-backup", "zenedit"]
POETRY_DIR = CORPUS / "_poetry"


def slugify(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\t\n\r]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] or "untitled"


def parse_fm(text: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return None, None, text
    fm_raw, rest = m.group(1), m.group(2)
    title = ""
    date_created = ""
    for line in fm_raw.splitlines():
        s = line.strip()
        if s.startswith("title:"):
            title = s.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("date_created:"):
            date_created = s.split(":", 1)[1].strip().strip('"').strip("'")
    return {"title": title, "date_created": date_created, "fm_raw": fm_raw}, rest, text


# --- Step 1: Load label map -----------------------------------------------
labels = {}  # rel (current .md path) -> label
with SIGNAL_TSV.open() as f:
    header = f.readline()
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 7:
            continue
        rel, _source, _date, _title, _bc, label, _alt = parts[:7]
        labels[rel] = label

# Override with 'poetry' for poem-classified notes
poem_rels = set()
with POEMS_TSV.open() as f:
    f.readline()
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        rel, _date, _title, _bc, cls = parts[:5]
        if cls == "poem":
            poem_rels.add(rel)
            labels[rel] = "poetry"

print(f"Label map: {len(labels)}  |  Poems: {len(poem_rels)}")

# --- Step 2: Plan moves ---------------------------------------------------
moves = []  # (old_rel, new_rel, new_label_for_fm)
used_names = {}  # label -> set of used filenames

def claim_name(label: str, date: str, title_slug: str) -> str:
    used = used_names.setdefault(label, set())
    base = f"{date} - {title_slug}"
    name = f"{base}.md"
    if name not in used:
        used.add(name)
        return name
    i = 2
    while True:
        name = f"{base} [{i}].md"
        if name not in used:
            used.add(name)
            return name
        i += 1


for p in sorted(CORPUS.rglob("*.md")):
    rel = str(p.relative_to(CORPUS))
    # Skip already-organized things and metadata
    if rel.startswith("_"):
        continue
    # Determine label
    if rel in labels:
        label = labels[rel]
    elif rel.startswith("evernote/clipped/"):
        label = "clipped"
    else:
        print(f"SKIP (no label): {rel}", file=sys.stderr)
        continue

    text = p.read_text(encoding="utf-8", errors="replace")
    parsed, _body, _ = parse_fm(text)
    if parsed is None:
        print(f"SKIP (no frontmatter): {rel}", file=sys.stderr)
        continue

    date = (parsed["date_created"] or "")[:10] or "0000-00-00"
    title_slug = slugify(parsed["title"] or p.stem)
    new_name = claim_name(label, date, title_slug)
    new_rel = f"{label}/{new_name}"
    moves.append((rel, new_rel, label, rel in poem_rels))

print(f"Planned moves: {len(moves)}")
by_label = {}
for _, _, lab, _ in moves:
    by_label[lab] = by_label.get(lab, 0) + 1
for lab, n in sorted(by_label.items(), key=lambda x: -x[1]):
    print(f"  {lab:<12} {n}")

# --- Step 3: Create folders ----------------------------------------------
for label in by_label:
    (NOTES_DIR / label).mkdir(exist_ok=True)

# --- Step 4: Execute moves + update label frontmatter ---------------------
move_map = {}  # old_rel -> new_rel
for old_rel, new_rel, label, is_poem in moves:
    src = NOTES_DIR / old_rel
    dst = NOTES_DIR / new_rel
    text = src.read_text(encoding="utf-8", errors="replace")

    # Update label in frontmatter if it became 'poetry'
    if is_poem:
        text = re.sub(
            r'^(label\s*:\s*)"creative"',
            r'\1"poetry"',
            text,
            count=1,
            flags=re.MULTILINE,
        )

    dst.write_text(text, encoding="utf-8")
    src.unlink()
    move_map[old_rel] = new_rel

print(f"Moved: {len(move_map)}")

# --- Step 5: Update TSVs --------------------------------------------------
# _signal.tsv: update source_path column AND label column (for poems)
new_lines = [header]
with SIGNAL_TSV.open() as f:
    f.readline()
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 7:
            new_lines.append(line)
            continue
        old_rel = parts[0]
        if old_rel in move_map:
            parts[0] = move_map[old_rel]
        if old_rel in poem_rels:
            parts[5] = "poetry"  # update label column
        new_lines.append("\t".join(parts) + "\n")
SIGNAL_TSV.write_text("".join(new_lines), encoding="utf-8")
print("Updated _signal.tsv")

# _poems.tsv: update source_path column
text = POEMS_TSV.read_text(encoding="utf-8")
for old_rel, new_rel in move_map.items():
    text = text.replace(f"{old_rel}\t", f"{new_rel}\t")
POEMS_TSV.write_text(text, encoding="utf-8")
print("Updated _poems.tsv")

# --- Step 6: Remove old source dirs ---------------------------------------
for sd in OLD_SOURCE_DIRS:
    p = NOTES_DIR / sd
    if p.exists():
        shutil.rmtree(p)
        print(f"Removed: {sd}/")

# --- Step 7: Remove _poetry/ (duplicate) ---------------------------------
if POETRY_DIR.exists():
    shutil.rmtree(POETRY_DIR)
    print(f"Removed: _poetry/")

print("\nDone.")
