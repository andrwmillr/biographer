#!/usr/bin/env python3
"""Build the Louisa May Alcott sample corpus from Gutenberg #38049
(Cheney 1889 ed. — _Louisa May Alcott: Her Life, Letters, and Journals_).

Output mirrors the Thoreau corpus shape:
  _corpora/<slug>/
    notes/YYYY-MM-DD.md     (one file per dated journal entry)
    _config/eras.yaml
    _meta.json              (content_hash, source, created_at)

Idempotent: pass --slug to write to a known directory; otherwise generates
`c_<16hex>`. Re-runs overwrite contents in place.

Heuristics for parsing Cheney's text:

- 3-space-indented italic date headers like `   _September 1st._--` or
  `   _Friday, Nov. 2nd._--`. Period sits inside the italics in Gutenberg's
  encoding.
- Year context inherits across entries; section markers like
  `_Early Diary kept at Fruitlands_, 1843.` and prose like "In 1842 Mr.
  Alcott..." reset the running year.
- Month context inherits from the previous entry — entries headed only by
  day-name+day ("_Thursday, 14th._") reuse the prior month.
- Headers without a parseable date (e.g. `_Journal_.`, `_Ten Years Old._`)
  are treated as section breaks.
- Letters embedded in the journal stream are glommed onto the surrounding
  journal entry rather than split out — acceptable for v1; can be tightened
  later by detecting `_From X._` column-0 headers and their `PLACE, DATE.`
  follow-up lines.

Run:
    python3 _web/scripts/build_corpus_alcott.py [--slug c_xxxx]
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

GUTENBERG_URL = "https://www.gutenberg.org/cache/epub/38049/pg38049.txt"
CACHE = Path("/tmp/alcott_pg38049.txt")
OUT_ROOT = Path.home() / "notes-archive" / "_corpora"

# 3-space-indented italic date header. Period sits inside the italics in
# Gutenberg's encoding: `_September 1st._--I rose...`.
ENTRY_RE = re.compile(r'^   _([^_]+)\._\s*-?-(.*)$')

# Italic month variant: "   _March_, 1882.--Helped..." (italic is just the month)
ENTRY_RE_ALT = re.compile(r'^   _([A-Z][a-z]+)_,\s*(\d{4})\.\s*-?-(.*)$')

# "   _October_ 24, 1882.--Telegram..." (italic month, day+year outside italics)
ENTRY_RE_ALT2 = re.compile(r'^   _([A-Z][a-z]+)_\s+(\d{1,2}),?\s*(\d{4})?\.\s*-?-(.*)$')

# Year-establishing markers: any 19th-century year token in prose or section
# labels, e.g. `_Early Diary kept at Fruitlands_, 1843.`, "In 1842 Mr. Alcott..."
YEAR_MARKER_RE = re.compile(r'\b(18[3-9]\d|19[0-9]\d)\b')

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}

DAY_NAMES = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}

NON_DATE_HEADERS = {
    'journal', 'ten years old', 'eleven years old', 'twelve years old',
    'novels', 'little women series', 'spinning-wheel stories series',
    "aunt jo's scrap-bag", "lulu's library",
}

ERAS_YAML = """- name: Childhood and Fruitlands
  start: 1843-01
- name: Concord and Boston
  start: 1848-01
- name: Hospital Sketches and First Books
  start: 1862-01
- name: Little Women and After
  start: 1868-01
- name: Late Career and Family
  start: 1878-01
"""


def fetch_text() -> str:
    if not CACHE.exists():
        with urllib.request.urlopen(GUTENBERG_URL) as r:
            CACHE.write_bytes(r.read())
    return CACHE.read_text(encoding='utf-8')


def parse_date_header(
    header: str, year: int | None, month: int | None
) -> tuple[int, int, int] | None:
    """Extract (yyyy, mm, dd) from a date header. Falls back to running
    year/month context for missing parts. Returns None if we can't get at
    least year+month."""
    s = header.strip().rstrip('.').strip()
    low = s.lower()
    if low in NON_DATE_HEADERS:
        return None
    if not any(c.isdigit() for c in s) and not any(m in low for m in MONTHS):
        return None

    parts = [p.strip() for p in s.split(',')]
    parts = [p for p in parts if p.lower() not in DAY_NAMES]
    text = ', '.join(parts)
    tokens = re.findall(r"[A-Za-z]+|\d+", text)

    found_year = None
    found_month = None
    found_day = None
    for tok in tokens:
        low_tok = tok.lower()
        if tok.isdigit():
            n = int(tok)
            if 1830 <= n <= 1900 and found_year is None:
                found_year = n
            elif 1 <= n <= 31 and found_day is None:
                found_day = n
        elif low_tok in MONTHS and found_month is None:
            found_month = MONTHS[low_tok]

    yyyy = found_year if found_year is not None else year
    mm = found_month if found_month is not None else month
    dd = found_day if found_day is not None else 1

    if yyyy is None or mm is None:
        return None
    try:
        datetime(yyyy, mm, dd)
    except ValueError:
        dd = 1
    return (yyyy, mm, dd)


def parse_entries(text: str) -> list[dict]:
    lines = text.splitlines()
    start_idx = 0
    end_idx = len(lines)
    for i, ln in enumerate(lines):
        if ln.strip() == 'CHAPTER I.':
            start_idx = i
            break
    for i, ln in enumerate(lines):
        if ln.startswith('*** END'):
            end_idx = i
            break

    cur_year: int | None = None
    cur_month: int | None = None
    in_entry = False
    cur_entry: dict | None = None
    entries: list[dict] = []

    def flush():
        nonlocal cur_entry, in_entry
        if cur_entry is not None:
            while cur_entry['body_lines'] and not cur_entry['body_lines'][-1].strip():
                cur_entry['body_lines'].pop()
            if cur_entry['body_lines']:
                entries.append(cur_entry)
        cur_entry = None
        in_entry = False

    i = start_idx
    while i < end_idx:
        ln = lines[i]

        if ln and not ln.startswith(' ') and not ln.startswith('_'):
            for ym in YEAR_MARKER_RE.findall(ln):
                cur_year = int(ym)
            flush()
            i += 1
            continue

        if ln.startswith('_'):
            for ym in YEAR_MARKER_RE.findall(ln):
                cur_year = int(ym)
            flush()
            i += 1
            continue

        if ln.strip().startswith('*       *'):
            flush()
            i += 1
            continue

        m_alt = ENTRY_RE_ALT.match(ln)
        m_alt2 = ENTRY_RE_ALT2.match(ln)
        m = ENTRY_RE.match(ln)

        if m_alt:
            month_name, year_str, rest = m_alt.groups()
            header = f'{month_name}, {year_str}'
            body_first = rest
        elif m_alt2:
            month_name, day_str, year_str, rest = m_alt2.groups()
            header = f'{month_name} {day_str}'
            if year_str:
                header += f', {year_str}'
            body_first = rest
        elif m:
            header, body_first = m.groups()
        else:
            header = None
            body_first = None

        if header is not None:
            parsed = parse_date_header(header, cur_year, cur_month)
            if parsed is None:
                flush()
                i += 1
                continue
            yyyy, mm, dd = parsed
            cur_year, cur_month = yyyy, mm
            flush()
            cur_entry = {
                'date': f'{yyyy:04d}-{mm:02d}-{dd:02d}',
                'header_raw': header,
                'body_lines': [body_first.strip()] if body_first.strip() else [],
            }
            in_entry = True
            i += 1
            continue

        if in_entry:
            if ln.strip() == '':
                cur_entry['body_lines'].append('')
            elif ln.startswith('   ') or ln.startswith('     '):
                stripped = re.sub(r'^   ', '', ln, count=1)
                cur_entry['body_lines'].append(stripped)
            else:
                flush()
        i += 1

    flush()
    return entries


def write_corpus(entries: list[dict], slug: str) -> Path:
    corpus_dir = OUT_ROOT / slug
    notes_dir = corpus_dir / "notes"
    if notes_dir.exists():
        shutil.rmtree(notes_dir)
    notes_dir.mkdir(parents=True)

    by_date: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_date[e['date']].append(e)

    for date, items in sorted(by_date.items()):
        chunks = ['\n'.join(it['body_lines']).strip() for it in items]
        out = ('\n\n* * *\n\n'.join(chunks)).strip() + '\n'
        (notes_dir / f"{date}.md").write_text(out, encoding='utf-8')

    h = hashlib.sha256()
    files = sorted(p for p in notes_dir.rglob("*") if p.is_file())
    for f in files:
        rel = str(f.relative_to(notes_dir))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0\0")
    content_hash = h.hexdigest()

    meta = {
        "content_hash": content_hash,
        "created_at": datetime.now().isoformat(timespec='seconds'),
        "source": "Project Gutenberg #38049 (Cheney 1889 ed.)",
        "title": "Louisa May Alcott: Her Life, Letters, and Journals",
        "description": "Alcott's journals from age ten at Fruitlands (1843) through her Civil War nursing, the writing of Little Women, and her late career caring for family — edited by Ednah Cheney.",
        "backfilled": True,
        "sample": True,
    }
    (corpus_dir / "_meta.json").write_text(json.dumps(meta), encoding='utf-8')

    cfg = corpus_dir / "_config"
    cfg.mkdir(exist_ok=True)
    (cfg / "eras.yaml").write_text(ERAS_YAML, encoding='utf-8')

    return corpus_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", help="target slug (e.g. c_abcd…); generates a random one if omitted")
    args = ap.parse_args()

    text = fetch_text()
    entries = parse_entries(text)
    print(f"parsed {len(entries)} entries")
    by_date = defaultdict(list)
    for e in entries:
        by_date[e['date']].append(e)
    print(f"  unique dates: {len(by_date)}")
    print(f"  date range: {min(by_date)} → {max(by_date)}")

    slug = args.slug or f"c_{secrets.token_hex(8)}"
    corpus_dir = write_corpus(entries, slug)
    print(f"\nwrote corpus to {corpus_dir}")
    print(f"  slug: {slug}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
