#!/usr/bin/env python3
"""Standalone verifier for an existing biography.md.

Splits the narrative into era chapters, extracts all quoted passages, and
checks each quote verbatim against the bodies of notes from that era.
No API calls — reuses the extract_quotes / verify_quotes logic from
write_biography.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from write_biography import (  # type: ignore
    ERAS,
    BIOGRAPHIES_DIR,
    era_of,
    extract_quotes,
    load_corpus_notes,
    parse_note_body,
)

BIOGRAPHY = BIOGRAPHIES_DIR / "biography.md"


def normalize(s: str) -> str:
    """Aggressive normalization: case, whitespace, curly quotes, en/em dashes,
    poem line-break slashes ('/') treated as whitespace."""
    s = s.lower()
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def verify_with_elision(quote: str, body_norm: str) -> bool:
    """Check quote against body, allowing '…' or '...' as elision markers.
    Each segment between elisions must appear, in order, in body."""
    q = quote.strip().strip(",.;:")
    q = re.sub(r"\s*(\u2026|\.\.\.+)\s*", "\x00", q)
    parts = [p for p in q.split("\x00") if p.strip()]
    if not parts:
        return False
    pos = 0
    for p in parts:
        p_norm = normalize(p).strip().strip(",.;:")
        idx = body_norm.find(p_norm, pos)
        if idx < 0:
            return False
        pos = idx + len(p_norm)
    return True


def split_chapters(md: str):
    """Return {era_name: chapter_body}. Splits on '## <era>' headings where
    <era> is an exact era label from ERAS (optionally followed by a
    parenthetical date range the chapter template might include)."""
    chapters = {}
    era_names = [name for name, _, _ in ERAS]
    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(md))
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        matched_name = None
        for name in era_names:
            if heading == name or heading.startswith(name + " ") or heading.startswith(name + "("):
                matched_name = name
                break
        if not matched_name:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[start:end].strip()
        chapters[matched_name] = body
    return chapters


def main():
    if not BIOGRAPHY.exists():
        print(f"ERROR: {BIOGRAPHY} not found")
        sys.exit(1)

    all_notes = load_corpus_notes()
    by_era = {name: [] for name, _, _ in ERAS}
    for n in all_notes:
        e = era_of(n.get("date", ""))
        if e is not None:
            by_era[e].append(n)

    md = BIOGRAPHY.read_text(encoding="utf-8")
    chapters = split_chapters(md)

    print(f"biography: {BIOGRAPHY}")
    print(f"chapters found: {list(chapters.keys())}")
    print()

    grand_total = 0
    grand_unverified = 0

    for era_name, _, _ in ERAS:
        if era_name not in chapters:
            continue
        chapter_text = chapters[era_name]
        era_notes = by_era[era_name]
        quotes = extract_quotes(chapter_text)

        body_parts = []
        for n in era_notes:
            body = parse_note_body(n["rel"])
            if body:
                body_parts.append(body)
            title = (n.get("title") or "").strip()
            if title:
                body_parts.append(title)
        combined = normalize("\n".join(body_parts))

        unverified = []
        for q in quotes:
            if not verify_with_elision(q, combined):
                unverified.append(q)

        print(f"=== {era_name} ===")
        print(f"  quotes extracted: {len(quotes)}")
        print(f"  unverified:       {len(unverified)}")
        for q in unverified:
            preview = q if len(q) <= 200 else q[:197] + "…"
            print(f"    ⚠ {preview}")
        print()
        grand_total += len(quotes)
        grand_unverified += len(unverified)

    pct = (100 * grand_unverified / grand_total) if grand_total else 0
    print(f"TOTAL: {grand_total} quotes, {grand_unverified} unverified ({pct:.0f}%)")


if __name__ == "__main__":
    main()
