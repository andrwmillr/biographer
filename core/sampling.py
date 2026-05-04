"""Budget-proportional random corpus sampling for the themes flow.

Web routers (`api/themes.py`) and the CLI tool (`cli/spin_themes.py`)
both call into this module. Pure helpers — no subprocess calls, no
system prompt loading.

Sampling strategy: each era gets a character budget proportional to its
note count. Within each era, notes are drawn in random order until the
budget fills. Notes under MIN_NOTE_BODY chars are skipped (too thin to
carry signal). Oversized notes are snipped (head + tail kept) so one
monster entry doesn't eat the whole budget."""
from __future__ import annotations

import random

from core import corpus as wb


# Total body-character budget across all eras.
THEMES_CHAR_CAP = 250_000

# Skip notes shorter than this — not enough signal.
MIN_NOTE_BODY = 200

# If a single note is longer than this, snip it (head + tail).
MAX_PER_NOTE = 8_000

# Floor budget per era so small eras still get representation.
MIN_ERA_BUDGET = 5_000


def budget_sample(notes_in_era: list[dict], char_budget: int,
                  corpus_id: str | None = None) -> list[dict]:
    """Draw random notes from an era until char_budget is filled.

    Returns notes with bodies attached, sorted by date. Skips notes
    shorter than MIN_NOTE_BODY. Snips notes longer than MAX_PER_NOTE."""
    # Load bodies and filter out empties / too-short
    candidates = []
    for n in notes_in_era:
        rel = n["rel"]
        body = wb.parse_note_body(rel, corpus_id)
        if not body or len(body) < MIN_NOTE_BODY:
            continue
        n2 = dict(n)
        n2["body"] = body
        n2["body_len"] = len(body)
        candidates.append(n2)

    # Shuffle for random selection
    random.shuffle(candidates)

    sampled = []
    used = 0
    for n in candidates:
        body = n["body"]
        # Snip oversized notes so one entry doesn't dominate
        if len(body) > MAX_PER_NOTE:
            body = wb.sample_keeper(body, MAX_PER_NOTE)
            n["body"] = body
            n["body_len"] = len(body)
        if used + len(body) > char_budget and sampled:
            # Budget full — stop (but always include at least one note)
            break
        sampled.append(n)
        used += len(body)

    sampled.sort(key=lambda x: x.get("date", ""))
    return sampled


def folder_aware_sample(notes_in_era, top_n, corpus_id=None):
    """Per-era sample: top-N longest notes per discovered folder.

    Kept for the /notes/themes-top-n REST endpoint (timeline display).
    The themes session itself now uses budget_sample via build_input."""
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
    as the user message for round-1 themes generation.

    top_n is ignored for sampling (budget-proportional now) but kept in
    the signature for backward compat with callers."""
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    by_era: dict[str, list[dict]] = {}
    for n in notes:
        era = wb.era_of(n.get("date", ""), eras)
        if era:
            by_era.setdefault(era, []).append(n)

    # Compute per-era budgets proportional to note count.
    total_notes = sum(len(by_era.get(name, [])) for name, _, _ in eras)
    era_budgets: dict[str, int] = {}
    for name, _, _ in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        share = len(era_notes) / total_notes if total_notes else 0
        era_budgets[name] = max(MIN_ERA_BUDGET, int(THEMES_CHAR_CAP * share))

    # Scale budgets down if they overshoot (floor can push total above cap).
    budget_total = sum(era_budgets.values())
    if budget_total > THEMES_CHAR_CAP:
        scale = THEMES_CHAR_CAP / budget_total
        era_budgets = {k: int(v * scale) for k, v in era_budgets.items()}

    sampled_by_era: dict[str, list[dict]] = {}
    for name in era_budgets:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        sampled_by_era[name] = budget_sample(
            era_notes, era_budgets[name], corpus_id,
        )

    total_sampled = sum(len(ns) for ns in sampled_by_era.values())
    total_body = sum(len(n["body"]) for ns in sampled_by_era.values() for n in ns)

    lines = []
    lines.append("# Corpus overview")
    lines.append("")
    lines.append(f"Total notes: {len(notes)}")
    lines.append("")
    lines.append("## Eras")
    lines.append("")
    for name, lo, hi in eras:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        actual_lo, actual_hi = wb.era_date_range(era_notes)
        sampled_n = len(sampled_by_era.get(name, []))
        lines.append(
            f"- **{name}** ({actual_lo} – {actual_hi}) — "
            f"{len(era_notes)} notes ({sampled_n} sampled)"
        )
    lines.append("")

    lines.append("## Per-era sample (random, budget-proportional)")
    lines.append("")
    lines.append(
        f"You are seeing {total_sampled} randomly selected notes from a "
        f"corpus of {len(notes)}. Each era's budget is proportional to its "
        f"note count. Notes shorter than {MIN_NOTE_BODY} chars were skipped; "
        f"notes longer than {MAX_PER_NOTE:,} chars were snipped (head + tail "
        f"kept, middle elided). The sample is random — each session sees "
        f"different notes."
    )
    lines.append("")
    for name, _, _ in eras:
        sampled = sampled_by_era.get(name)
        if not sampled:
            continue
        era_total = len(by_era.get(name, []))
        lines.append(f"### {name} — {len(sampled)} of {era_total} notes")
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
