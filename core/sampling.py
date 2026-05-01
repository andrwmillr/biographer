"""Folder-aware corpus sampling for the themes flow.

Web routers (`api/themes.py`) and the CLI tool (`cli/spin_themes.py`)
both call into this module. Pure helpers — no subprocess calls, no
system prompt loading."""
from __future__ import annotations

from core import corpus as wb


def folder_aware_sample(notes_in_era, top_n, corpus_id=None):
    """Per-era sample: top-N longest notes per discovered folder.

    Folder = first path segment of `rel`; notes without a slash bucket
    under "_" (single flat folder). Each bucket yields up to top-N
    longest notes (or all if fewer). Returns notes with bodies attached,
    sorted by date.

    Generalized from a hard-coded {journal, creative, poetry, letter,
    fiction} layout — corpora with arbitrary folder names work without
    sampling to zero."""
    by_label: dict[str, list[dict]] = {}
    for n in notes_in_era:
        rel = n["rel"]
        label = rel.split("/", 1)[0] if "/" in rel else "_"
        body = wb.parse_note_body(rel, corpus_id) if corpus_id else wb.parse_note_body(rel)
        if not body:
            continue
        n2 = dict(n)
        n2["body"] = body
        n2["body_len"] = len(body)
        by_label.setdefault(label, []).append(n2)
    sampled = []
    for notes in by_label.values():
        sampled.extend(sorted(notes, key=lambda x: -x["body_len"])[:top_n])
    sampled.sort(key=lambda x: x.get("date", ""))
    return sampled


def build_input(top_n, corpus_id=None):
    """Assemble the corpus-overview + per-era sample input message used
    as the user message for round-1 themes generation."""
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    by_era = {}
    for n in notes:
        era = wb.era_of(n.get("date", ""), eras)
        if era:
            by_era.setdefault(era, []).append(n)

    lines = []
    lines.append("# Corpus overview")
    lines.append("")
    lines.append(f"Subject: {wb.SUBJECT_NAME}")
    lines.append(f"Total notes: {len(notes)}")
    lines.append("")
    lines.append("## Eras")
    lines.append("")
    for name, lo, hi in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        actual_lo, actual_hi = wb.era_date_range(era_notes)
        lines.append(f"- **{name}** ({actual_lo} – {actual_hi}) — {len(era_notes)} notes")
    lines.append("")

    lines.append("## Per-era sample (folder-aware)")
    lines.append("")
    lines.append(
        f"You are seeing a sample of the corpus, not every note. Per era: "
        f"the top-{top_n} longest journal entries + the top-{top_n} longest "
        f"creative pieces + every poetry note + every letter. Short journal "
        f"and creative notes outside this sample are not shown."
    )
    lines.append("")
    for name, _, _ in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        sampled = folder_aware_sample(era_notes, top_n, corpus_id)
        lines.append(f"### {name} — {len(sampled)} sampled notes")
        lines.append("")
        for n in sampled:
            date = (n.get("date") or "")[:10]
            title = n.get("title") or "(untitled)"
            label = n["rel"].split("/", 1)[0]
            lines.append(f"==== [{date}] · {label} · {title} ====")
            lines.append("")
            lines.append(n["body"])
            lines.append("")
    return "\n".join(lines)
