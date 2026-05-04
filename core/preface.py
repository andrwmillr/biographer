"""Preface input builder.

Assembles all chapters + canonical themes + cited source notes into a
single input message for the preface drafter. Source notes are snipped
at MAX_NOTE_CHARS to keep total context manageable."""
from __future__ import annotations

import re
from pathlib import Path

from core import corpus as wb


# Snip cited notes longer than this (head + tail kept).
MAX_NOTE_CHARS = 2000

# Citation patterns in chapters and themes
_CHAPTER_CITE_RE = re.compile(r"\]\((\d{4}-\d{2}-\d{2})\)")
_THEMES_CITE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")


def _extract_cited_dates(corpus_id: str | None = None) -> set[str]:
    """Collect all YYYY-MM-DD dates cited in chapters and themes."""
    dates: set[str] = set()
    chapters_path = wb.chapters_dir(corpus_id)
    if chapters_path.exists():
        for ch in sorted(chapters_path.glob("*.md")):
            dates.update(_CHAPTER_CITE_RE.findall(ch.read_text(encoding="utf-8")))
    themes_text = wb.load_canonical_themes(corpus_id)
    if themes_text:
        dates.update(_THEMES_CITE_RE.findall(themes_text))
    return dates


def _snip(body: str, max_chars: int = MAX_NOTE_CHARS) -> str:
    """Keep head + tail of a long note."""
    if len(body) <= max_chars:
        return body
    half = max_chars // 2
    return body[:half] + "\n\n[…snipped…]\n\n" + body[-half:]


def build_preface_input(corpus_id: str | None = None) -> str:
    """Assemble the full preface input: chapters + themes + cited notes."""
    eras = wb.load_eras(corpus_id)
    chapters_path = wb.chapters_dir(corpus_id)

    # Load all chapters in order
    chapter_blocks: list[str] = []
    by_era: dict[str, list[dict]] = {}
    all_notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(all_notes, corpus_id)
    for n in all_notes:
        era = wb.era_of(n.get("date", ""), eras)
        if era:
            by_era.setdefault(era, []).append(n)

    for name, _, _ in eras:
        slug_path = chapters_path / f"{wb.era_slug(name)}.md"
        if slug_path.exists():
            text = slug_path.read_text(encoding="utf-8").strip()
            if text:
                era_notes = by_era.get(name, [])
                heading = wb.era_heading(name, era_notes)
                chapter_blocks.append(f"## {heading}\n\n{text}")

    # Load canonical themes
    themes_text = wb.load_canonical_themes(corpus_id)

    # Load cited source notes
    cited_dates = _extract_cited_dates(corpus_id)
    cited_notes: list[tuple[str, str, str, str]] = []  # (date, title, label, body)
    for n in all_notes:
        d = (n.get("date") or "")[:10]
        if d not in cited_dates:
            continue
        body = wb.parse_note_body(n["rel"], corpus_id)
        if not body:
            continue
        title = n.get("title") or "(untitled)"
        label = n["rel"].split("/", 1)[0] if "/" in n["rel"] else ""
        cited_notes.append((d, title, label, _snip(body)))
    cited_notes.sort(key=lambda x: x[0])

    # Assemble
    parts: list[str] = []

    parts.append("--- ALL CHAPTERS (the full biography, in order) ---\n\n")
    for ch in chapter_blocks:
        parts.append(ch + "\n\n")
    parts.append("--- END CHAPTERS ---\n\n")

    if themes_text:
        parts.append("--- CORPUS THEMES ---\n\n")
        parts.append(themes_text.rstrip("\n") + "\n\n")
        parts.append("--- END CORPUS THEMES ---\n\n")

    parts.append("--- CITED SOURCE NOTES (notes referenced in the chapters and themes — available for direct quotation) ---\n\n")
    parts.append(
        f"You are seeing {len(cited_notes)} source notes that the chapters "
        f"and themes cite. Notes longer than {MAX_NOTE_CHARS:,} chars are "
        f"snipped (head + tail). Use these for block quotes and grounded "
        f"citations in the preface.\n\n"
    )
    for date, title, label, body in cited_notes:
        parts.append(f"==== [{date}] · {label} · {title} ====\n\n")
        parts.append(body + "\n\n")
    parts.append("--- END CITED SOURCE NOTES ---\n")

    return "".join(parts)
