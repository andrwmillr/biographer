#!/usr/bin/env python3
"""Re-parse the Thoreau corpus to recover dated entries that the original
ingest merged into the surrounding day's file.

The original ingest produced 383 .md files with the right filenames but
missed italic date markers buried inside their bodies — e.g.
`_Sept. 28. Tuesday._ I anticipate the coming in of spring...`.
This pass walks each existing note, splits its body on those markers,
and writes the new sub-entries as their own dated files.

Year context inside a file: starts from the filename year, and embedded
`_<Month> <Day>, <YEAR>._` markers update it. Project Gutenberg license
boilerplate (which the giant 1845-03-27 merged file contains) is stripped.

Usage: python3 _web/cli/reparse_thoreau.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CORPUS = Path.home() / "notes-archive" / "_corpora" / "c_c09893a58f4bd7dd"
NOTES = CORPUS / "notes"
META = CORPUS / "_meta.json"
CACHE = CORPUS / "_derived" / "_corpus_cache.pkl"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Italic date marker at column 0. Captures the spec between leading `_<Month>`
# and the closing `_`. Examples:
#   _June 14. Saturday._ Full moon last night...
#   _March 13, 1846._ The song sparrow...
#   _Sept. 28. Tuesday._ I anticipate...
#   _March 7, 8, 9, 10._ The Sphinx...
DATE_LINE_RE = re.compile(
    r"^_(?P<spec>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?[^_\n]*?)\._",
    re.IGNORECASE,
)
# Day-name-leading variant: "_Friday, Nov. 18, 1837._".
DAY_FIRST_RE = re.compile(
    r"^_(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+"
    r"(?P<spec>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?[^_\n]*?)\._",
    re.IGNORECASE,
)

# Year-range chapter header (e.g. "1845-1847" on its own line) — sets the
# running year inside a file when we hit it.
YEAR_RANGE_RE = re.compile(r"^(?P<y>18[3-9]\d|19[0-9]\d)(?:-\d{4})?$")

# Mark from where Project Gutenberg's boilerplate license tail begins.
GUTENBERG_END_MARKERS = (
    "*** END OF",
    "End of the Project Gutenberg",
    "End of Project Gutenberg",
)


def parse_spec(spec: str, fallback_year: int | None) -> tuple[int, int, int] | None:
    """`spec` is everything between the leading `_<Month>` and the closing `_`.
    Returns (year, month, day) or None."""
    s = spec.strip()
    month_match = re.match(
        r"(?P<m>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?",
        s,
        re.IGNORECASE,
    )
    if not month_match:
        return None
    month_name = month_match.group("m").lower().rstrip(".")
    month = MONTHS.get(month_name)
    if month is None:
        return None
    rest = s[month_match.end():].strip(" .,")
    rest = re.sub(
        r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b",
        "",
        rest,
        flags=re.IGNORECASE,
    )
    nums = [int(n) for n in re.findall(r"\d+", rest)]
    if not nums:
        return None
    day = None
    year = None
    for n in nums:
        if 1830 <= n <= 1900 and year is None:
            year = n
        elif 1 <= n <= 31 and day is None:
            day = n
    if day is None:
        return None
    yyyy = year if year is not None else fallback_year
    if yyyy is None:
        return None
    try:
        datetime(yyyy, month, day)
    except ValueError:
        return None
    return yyyy, month, day


def truncate_at_gutenberg_tail(text: str) -> str:
    """Cut the text at the Gutenberg END marker / license boilerplate."""
    for marker in GUTENBERG_END_MARKERS:
        idx = text.find(marker)
        if idx >= 0:
            return text[:idx].rstrip()
    # No END marker but big LICENSE block in the middle — find first occurrence.
    for header in ("PROJECT GUTENBERG LICENSE", "START: FULL LICENSE", "Section 5. General Information"):
        idx = text.find(header)
        if idx >= 0:
            text = text[:idx].rstrip()
            break
    return text


def split_file(path: Path) -> list[tuple[str, str]]:
    """Return list of (YYYY-MM-DD, body) split out from a single existing
    note. The first segment keeps the file's original date; later segments
    get dates from their leading italic markers."""
    m_fn = re.match(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
    if not m_fn:
        return []
    yyyy, mm, dd = (int(x) for x in m_fn.groups())
    cur_year = yyyy
    cur_date = f"{yyyy:04d}-{mm:02d}-{dd:02d}"

    text = path.read_text(encoding="utf-8")
    text = truncate_at_gutenberg_tail(text)
    lines = text.split("\n")

    out: list[tuple[str, str]] = []
    cur_body: list[str] = []

    def flush():
        nonlocal cur_body
        body = "\n".join(cur_body).rstrip()
        while body.startswith("\n"):
            body = body[1:]
        if body.strip():
            out.append((cur_date, body))
        cur_body = []

    for line in lines:
        stripped = line.strip()

        m_yr = YEAR_RANGE_RE.match(stripped)
        if m_yr:
            cur_year = int(m_yr.group("y"))
            continue

        m = DATE_LINE_RE.match(line) or DAY_FIRST_RE.match(line)
        if m:
            spec = m.group("spec")
            parsed = parse_spec(spec, cur_year)
            if parsed:
                yyyy2, mm2, dd2 = parsed
                flush()
                cur_year = yyyy2
                cur_date = f"{yyyy2:04d}-{mm2:02d}-{dd2:02d}"
                tail = line[m.end():].lstrip(" -–—")
                if tail:
                    cur_body.append(tail)
                continue

        cur_body.append(line)

    flush()
    return out


def parse_corpus() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for p in sorted(NOTES.glob("*.md")):
        entries.extend(split_file(p))
    return entries


def write_corpus(entries: list[tuple[str, str]], dry_run: bool) -> tuple[int, int]:
    by_date: dict[str, list[str]] = defaultdict(list)
    for date, body in entries:
        by_date[date].append(body)

    n_unique = len(by_date)
    n_total = len(entries)

    if dry_run:
        sample = sorted(by_date.keys())
        print(f"  unique dates: {n_unique}")
        print(f"  total entries: {n_total}")
        print(f"  date range: {sample[0]} → {sample[-1]}")
        from collections import Counter
        years = Counter(d[:4] for d in sample)
        for y, c in sorted(years.items()):
            print(f"    {y}: {c} days")
        return n_unique, n_total

    if NOTES.exists():
        shutil.rmtree(NOTES)
    NOTES.mkdir(parents=True)
    for date, items in sorted(by_date.items()):
        out = "\n\n* * *\n\n".join(s.rstrip() for s in items).strip() + "\n"
        (NOTES / f"{date}.md").write_text(out, encoding="utf-8")

    h = hashlib.sha256()
    for f in sorted(p for p in NOTES.rglob("*") if p.is_file()):
        rel = str(f.relative_to(NOTES))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0\0")
    meta = json.loads(META.read_text())
    meta["content_hash"] = h.hexdigest()
    META.write_text(json.dumps(meta))
    if CACHE.exists():
        CACHE.unlink()
    return n_unique, n_total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = parse_corpus()
    n_unique, n_total = write_corpus(entries, dry_run=args.dry_run)
    print(f"\n{'(dry-run) ' if args.dry_run else ''}wrote {n_unique} files ({n_total} entries before date-collapse)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
