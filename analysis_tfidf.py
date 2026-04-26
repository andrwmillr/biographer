#!/usr/bin/env python3
"""TF-IDF signature lexicon per quarter.

For each quarter (derived from date_created in frontmatter), compute the most
distinctive words — frequent within that quarter but rare across the full span.

Runs on the "writing" labels (journal, creative, poetry, letter). Clipped/
reference/todo/business/contact/code are excluded to keep the signal literary.
"""
import math
import re
from collections import Counter
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
WRITING_LABELS = ["journal", "creative", "poetry", "letter"]
OUT_TSV = CORPUS / "_derived" / "_tfidf_quarterly.tsv"
OUT_MD = CORPUS / "_derived" / "_tfidf_quarterly.md"

MIN_SLICE_NOTES = 1       # drop quarters with fewer notes than this
MIN_TOTAL_FREQ = 3        # word must appear at least this many times corpus-wide
MIN_LEN = 3               # word must be at least this long
TOP_K = 15                # words to print per quarter

STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can cannot could couldn't did didn't
do does doesn't doing don't dont down during each few for from further had hadn't
has hasn't have haven't having he he'd he'll he's her here here's hers herself
him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its
itself let's me more most mustn't my myself no nor not of off on once only or
other ought our ours ourselves out over own same shan't she she'd she'll she's
should shouldn't so some such than that that's the their theirs them themselves
then there there's these they they'd they'll they're theyre they've this those
through to too under until up very was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's whom why
why's with won't would wouldn't you you'd you'll you're youre you've your yours
yourself yourselves just get got getting one two also like really thing things
something someone anyone anything nothing everyone everything even way back well
still much many make made making know known knew knows think thought thinking
say said says saying see seen seeing look looked looking come came coming go
going went gone want wanted wants need needed needs take took taking put putting
feel felt feels give gave gives tell told tells let lets ever never always
sometimes often usually maybe perhaps may might must shall will would could
should wanna gonna gotta kinda sorta lotta oughta dunno
""".split())

WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def clean_body(body: str) -> str:
    body = MD_IMG_RE.sub(" ", body)
    body = MD_LINK_RE.sub(r"\1", body)  # keep link text, drop url
    body = URL_RE.sub(" ", body)
    body = EMAIL_RE.sub(" ", body)
    return body


def parse_note(text: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return "", text
    fm, body = m.group(1), m.group(2)
    date = ""
    for line in fm.splitlines():
        s = line.strip()
        if s.startswith("date_created:"):
            date = s.split(":", 1)[1].strip().strip('"').strip("'")
            break
    return date, body


def tokenize(body: str):
    return [
        t for t in WORD_RE.findall(clean_body(body).lower())
        if len(t) >= MIN_LEN and t not in STOPWORDS
    ]


def to_quarter(date: str) -> str | None:
    # date is ISO-ish, e.g. "2014-02-01T19:03:56" or "2014-02-01"
    if len(date) < 7 or not date[:4].isdigit() or date[4] != "-":
        return None
    year = date[:4]
    try:
        month = int(date[5:7])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    q = (month - 1) // 3 + 1
    return f"{year}-Q{q}"


# --- Gather tokens per quarter --------------------------------------------
slice_tokens = {}   # quarter -> Counter
slice_notes = {}    # quarter -> int

for label in WRITING_LABELS:
    d = NOTES_DIR / label
    if not d.exists():
        continue
    for p in sorted(d.glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        date, body = parse_note(text)
        q = to_quarter(date)
        if q is None:
            continue
        toks = tokenize(body)
        if not toks:
            continue
        slice_tokens.setdefault(q, Counter()).update(toks)
        slice_notes[q] = slice_notes.get(q, 0) + 1

slices = sorted([q for q, n in slice_notes.items() if n >= MIN_SLICE_NOTES])
print(f"Quarters: {slices[0]}–{slices[-1]}  ({len(slices)} non-empty)")

# --- Corpus-wide frequency & slice-presence -------------------------------
total_freq = Counter()
slices_with = Counter()
for q in slices:
    total_freq.update(slice_tokens[q])
    for w in slice_tokens[q]:
        slices_with[w] += 1

keep = {w for w, n in total_freq.items() if n >= MIN_TOTAL_FREQ}

# --- TF-IDF ---------------------------------------------------------------
N = len(slices)
results = {}
for q in slices:
    c = slice_tokens[q]
    total_in_slice = sum(c.values())
    scored = []
    for w, n in c.items():
        if w not in keep:
            continue
        tf = (1 + math.log(n)) / (1 + math.log(total_in_slice))
        idf = math.log(N / slices_with[w])
        scored.append((w, tf * idf, n))
    scored.sort(key=lambda x: -x[1])
    results[q] = scored[:TOP_K]

# --- Print ---------------------------------------------------------------
for q in slices:
    print(f"\n=== {q}  ({slice_notes[q]} notes) ===")
    for w, score, n in results[q]:
        print(f"  {w:<20} {score:6.3f}  ({n}×)")

# --- Save TSV -------------------------------------------------------------
with OUT_TSV.open("w", encoding="utf-8") as f:
    f.write("quarter\trank\tword\ttfidf\tcount\n")
    for q in slices:
        for rank, (w, score, n) in enumerate(results[q], 1):
            f.write(f"{q}\t{rank}\t{w}\t{score:.4f}\t{n}\n")
print(f"\nSaved: {OUT_TSV}")

# --- Save Markdown --------------------------------------------------------
with OUT_MD.open("w", encoding="utf-8") as f:
    f.write("# TF-IDF signature lexicon (quarterly)\n\n")
    f.write(
        "Words frequent in a given quarter but rare across the corpus. "
        "Counts in parens. Labels: journal, creative, poetry, letter.\n\n"
    )
    current_year = None
    for q in slices:
        year = q[:4]
        if year != current_year:
            f.write(f"\n## {year}\n\n")
            current_year = year
        f.write(f"### {q} — {slice_notes[q]} notes\n\n")
        f.write(
            ", ".join(f"**{w}** ({n})" for w, _, n in results[q])
        )
        f.write("\n\n")
print(f"Saved: {OUT_MD}")
