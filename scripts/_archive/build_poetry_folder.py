#!/usr/bin/env python3
"""Build _corpus/_poetry/ from _poems.tsv.

For each note classified as 'poem', copy it into _poetry/ with filename
YYYY-MM-DD - <title>.md. Also updates the _poems.tsv to use .md paths (since
it was generated before the .txt → .md rename).
"""
import re
import shutil
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
POEMS_TSV = CORPUS / "_derived" / "_poems.tsv"
OUT_DIR = CORPUS / "_poetry"

OUT_DIR.mkdir(exist_ok=True)


def slugify(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\t\n\r]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len] or "untitled"


# Rewrite _poems.tsv paths .txt -> .md
text = POEMS_TSV.read_text(encoding="utf-8")
new = text.replace(".txt\t", ".md\t")
if new != text:
    POEMS_TSV.write_text(new, encoding="utf-8")
    print("Updated _poems.tsv paths (.txt -> .md)")

rows = []
with POEMS_TSV.open() as f:
    f.readline()
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        rel, date_created, title, body_chars, cls = parts[:5]
        rows.append({"rel": rel, "date": date_created, "title": title, "cls": cls})

poems = [r for r in rows if r["cls"] == "poem"]
contains = [r for r in rows if r["cls"] == "contains_poem"]
print(f"poems={len(poems)}  contains_poem={len(contains)}")

copied = 0
missing = 0
used_names = set()
for r in poems:
    src = NOTES_DIR / r["rel"]
    if not src.exists():
        print(f"MISSING: {r['rel']}")
        missing += 1
        continue
    date = (r["date"] or "")[:10] or "0000-00-00"
    title_slug = slugify(r["title"] or src.stem)
    dst = OUT_DIR / f"{date} - {title_slug}.md"
    # Collision only when TWO distinct source rels produce the same dst name
    # within this run — handled via used_names set; repeat runs overwrite.
    if dst.name in used_names:
        i = 2
        while True:
            alt_name = f"{date} - {title_slug} [{i}].md"
            if alt_name not in used_names:
                dst = OUT_DIR / alt_name
                break
            i += 1
    used_names.add(dst.name)
    shutil.copy2(src, dst)
    copied += 1

print(f"Copied: {copied}  Missing: {missing}")
print(f"Output: {OUT_DIR}")

# Save list of contains_poem for user to review
if contains:
    review_path = OUT_DIR / "_contains_poem_review.tsv"
    with review_path.open("w") as f:
        f.write("source_path\tdate_created\ttitle\n")
        for r in contains:
            f.write(f"{r['rel']}\t{r['date']}\t{r['title']}\n")
    print(f"Contains-poem review list: {review_path}")
