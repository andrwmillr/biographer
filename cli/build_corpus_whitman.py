#!/usr/bin/env python3
"""Build the Walt Whitman *Specimen Days* sample corpus from Project
Gutenberg #8813 (*Complete Prose Works*, Whitman 1892 ed.).

Output mirrors the Thoreau/Alcott corpus shape:
  _corpora/<slug>/
    notes/YYYY-MM-DD.md     (one file per dated journal entry)
    _config/eras.yaml
    _meta.json              (content_hash, source, created_at, sample: True)

Specimen Days uses italic underscore-wrapped date markers at column 0:

    _Down in the Woods, July 2d, 1882_.-If I do it at all I must delay no
    _July 29, 1881_.--After more than forty years' absence...
    _May 12_.--There was part of the late battle at Chancellorsville,
    _Sunday, January 29th, 1865_.--Have been in Armory-square...

Day-name and place-name prefixes are stripped before parsing. Day-of-month
suffixes (`2d`, `12th`, etc.) are stripped. Year inherits from the most
recent year-bearing marker; abbreviated years (`'63`, `'65`) expand to
`1863`, `1865`. Italic asides without a parseable date (e.g.
`_Letter Writing_.--`, `_Ice Cream Treat_.--`) belong to the surrounding
day and are kept inline as part of its body.

The surrounding "Complete Prose Works" volume also contains *November
Boughs* and *Good Bye My Fancy*. We stop at the first `NOVEMBER BOUGHS`
heading after Specimen Days begins, ignoring the back-matter.

Run:
    python3 _web/cli/build_corpus_whitman.py [--slug c_xxxx]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

GUTENBERG_URL = "https://www.gutenberg.org/cache/epub/8813/pg8813.txt"
CACHE = Path("/tmp/whitman_pg8813.txt")
OUT_ROOT = Path.home() / "notes-archive" / "_corpora"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

DAY_NAME_RE = re.compile(
    r"^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+",
    re.IGNORECASE,
)

# Italic-wrapped header line. Body of the line (after `_..._.-`) is kept
# and prepended to the new entry's body. The italic span is sometimes
# prefixed by an uppercase place clause like
#   FALMOUTH, VA., _opposite Fredericksburgh, December 21, 1862_.--
# so the regex tolerates a leading non-italic prefix as long as the
# italics (and the closing `.-` / `.--`) are still on the same line.
# Closing punctuation is `._--`, `_.--`, or `_.-` — i.e. a period and one
# or two dashes around the closing italic underscore in any combination.
HEADER_RE = re.compile(
    r"^(?P<prefix>[A-Z][A-Z .,'-]*)?_(?P<spec>[^_\n]+?)\.?_\.?\-{1,2}(?P<rest>.*)$"
)

ERAS_YAML = """- name: Civil War Nursing
  start: 1862-12
- name: Convalescence and Camden
  start: 1876-01
- name: Late Travels and Reflection
  start: 1879-01
"""

DESCRIPTION = (
    "Whitman's diary jottings — Civil War hospital service in Washington "
    "(1862–1865), then Camden / Timber Creek nature notes after his 1873 "
    "stroke, plus late travels and reflections through 1882. Drawn from "
    "*Specimen Days* in the 1892 *Complete Prose Works*."
)

TITLE = "Walt Whitman: Specimen Days"
SOURCE = "Project Gutenberg #8813 (Complete Prose Works, 1892 ed.)"


def fetch_text() -> str:
    if not CACHE.exists():
        with urllib.request.urlopen(GUTENBERG_URL) as r:
            CACHE.write_bytes(r.read())
    return CACHE.read_text(encoding="utf-8")


def slice_specimen_days(text: str) -> str:
    """Cut the source down to the Specimen Days body, dropping the TOC and
    the November Boughs / Good Bye My Fancy back-matter."""
    lines = text.splitlines()
    body_start = None
    body_end = len(lines)
    saw_specimen = False
    for i, ln in enumerate(lines):
        if ln.strip() == "SPECIMEN DAYS":
            if not saw_specimen:
                # First occurrence is the TOC header.
                saw_specimen = True
                continue
            # Second occurrence opens the body.
            body_start = i + 1
            break
    if body_start is None:
        raise SystemExit("could not find Specimen Days body start")
    for i in range(body_start, len(lines)):
        if lines[i].strip() in ("NOVEMBER BOUGHS", "*** END OF THE PROJECT GUTENBERG EBOOK"):
            body_end = i
            break
    return "\n".join(lines[body_start:body_end])


def parse_spec(spec: str, fallback_year: int | None) -> tuple[int, int, int] | None:
    """Extract (year, month, day) from the inside of an italic header.
    Returns None if the spec is a topic heading (no parseable date)."""
    s = spec.strip()
    s = DAY_NAME_RE.sub("", s)  # strip "Sunday, " etc.
    # Strip a leading place-name clause "Down in the Woods, " if present.
    # Heuristic: keep only the suffix that contains a month token.
    if "," in s:
        # If there's a month token after a comma, slice from the month.
        m_month_pos = re.search(
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*",
            s,
            re.IGNORECASE,
        )
        if m_month_pos:
            s = s[m_month_pos.start():]
    # Find the month.
    m_month = re.match(
        r"(?P<m>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?",
        s,
        re.IGNORECASE,
    )
    if not m_month:
        return None
    month_name = m_month.group("m").lower().rstrip(".")
    month = MONTHS.get(month_name)
    if month is None:
        return None
    rest = s[m_month.end():].strip(" .,")
    # Pull numeric tokens, treating `'63` and `'65` as abbreviated years.
    tokens = re.findall(r"'?\d+(?:st|nd|rd|th|d)?", rest)
    day: int | None = None
    year: int | None = None
    for tok in tokens:
        clean = tok.lstrip("'").rstrip(".")
        clean = re.sub(r"(?:st|nd|rd|th|d)$", "", clean)
        if not clean.isdigit():
            continue
        n = int(clean)
        if tok.startswith("'") and 0 <= n <= 99:
            year = 1800 + n if n >= 50 else 1900 + n
        elif 1830 <= n <= 1900 and year is None:
            year = n
        elif 1 <= n <= 31 and day is None:
            day = n
    if day is None:
        # Month-only marker like "_January, '63_" — pin to day 1.
        day = 1
    yyyy = year if year is not None else fallback_year
    if yyyy is None:
        return None
    try:
        datetime(yyyy, month, day)
    except ValueError:
        return None
    return yyyy, month, day


def parse(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    lines = text.splitlines()
    cur_year: int | None = None
    cur_date: str | None = None
    cur_body: list[str] = []

    def flush():
        nonlocal cur_body
        body = "\n".join(cur_body).rstrip()
        while body.startswith("\n"):
            body = body[1:]
        if cur_date and body.strip():
            entries.append((cur_date, body))
        cur_body = []

    for line in lines:
        m = HEADER_RE.match(line)
        if m:
            spec = m.group("spec")
            rest = m.group("rest")
            parsed = parse_spec(spec, cur_year)
            if parsed:
                yyyy, mm, dd = parsed
                flush()
                cur_year = yyyy
                cur_date = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
                if rest.strip():
                    cur_body.append(rest.lstrip())
                continue
            # Not a parseable date — keep the topic heading as inline body.
            cur_body.append(line)
            continue
        cur_body.append(line)

    flush()
    return entries


def write_corpus(entries: list[tuple[str, str]], slug: str) -> Path:
    corpus_dir = OUT_ROOT / slug
    notes_dir = corpus_dir / "notes"
    if notes_dir.exists():
        shutil.rmtree(notes_dir)
    notes_dir.mkdir(parents=True)

    by_date: dict[str, list[str]] = defaultdict(list)
    for date, body in entries:
        by_date[date].append(body)

    for date, items in sorted(by_date.items()):
        out = "\n\n* * *\n\n".join(s.rstrip() for s in items).strip() + "\n"
        (notes_dir / f"{date}.md").write_text(out, encoding="utf-8")

    h = hashlib.sha256()
    for f in sorted(p for p in notes_dir.rglob("*") if p.is_file()):
        rel = str(f.relative_to(notes_dir))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0\0")
    meta = {
        "content_hash": h.hexdigest(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "title": TITLE,
        "description": DESCRIPTION,
        "sample": True,
    }
    (corpus_dir / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")

    cfg = corpus_dir / "_config"
    cfg.mkdir(exist_ok=True)
    (cfg / "eras.yaml").write_text(ERAS_YAML, encoding="utf-8")

    return corpus_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="target slug (e.g. c_abcd…); generates a random one if omitted")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = fetch_text()
    body = slice_specimen_days(text)
    entries = parse(body)

    by_date: dict[str, list[str]] = defaultdict(list)
    for date, body_text in entries:
        by_date[date].append(body_text)

    print(f"parsed {len(entries)} dated entries")
    print(f"  unique dates: {len(by_date)}")
    if by_date:
        sample = sorted(by_date.keys())
        print(f"  date range: {sample[0]} → {sample[-1]}")
        from collections import Counter
        years = Counter(d[:4] for d in sample)
        for y, c in sorted(years.items()):
            print(f"    {y}: {c} days")

    if args.dry_run:
        return 0

    slug = args.slug or f"c_{secrets.token_hex(8)}"
    corpus_dir = write_corpus(entries, slug)
    print(f"\nwrote corpus to {corpus_dir}")
    print(f"  slug: {slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
