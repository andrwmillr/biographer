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
                  corpus_id: str | None = None,
                  highlighted_rels: set[str] | None = None) -> list[dict]:
    """Draw random notes from an era until char_budget is filled.

    Returns notes with bodies attached, sorted by date. Skips notes
    shorter than MIN_NOTE_BODY. Snips notes longer than MAX_PER_NOTE.
    Highlighted notes (from the commonplace flow) are always included
    first so they inform themes/preface analysis."""
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

    # Partition: highlighted notes first (deterministic), rest shuffled.
    hl = highlighted_rels or set()
    priority = [n for n in candidates if n["rel"] in hl]
    rest = [n for n in candidates if n["rel"] not in hl]
    random.shuffle(rest)
    ordered = priority + rest

    sampled = []
    used = 0
    for n in ordered:
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


def build_input(top_n, corpus_id=None, char_cap=None, label_filter=None,
                exclude_rels=None, shuffle=False, return_notes=False,
                highlighted_rels: set[str] | None = None):
    """Assemble the corpus-overview + per-era sample input message.

    top_n is ignored for sampling (budget-proportional now) but kept in
    the signature for backward compat with callers.

    char_cap: override THEMES_CHAR_CAP (e.g. 500_000 for commonplace).
    label_filter: if set, only include notes whose label (first path
      segment) is in this set.
    exclude_rels: set of note rel paths to skip (already-seen notes).
    shuffle: if True, flatten all sampled notes and randomize order
      instead of grouping by era chronologically."""
    cap = char_cap or THEMES_CHAR_CAP
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    by_era: dict[str, list[dict]] = {}
    for n in notes:
        # Apply label filter if specified (e.g. commonplace only wants
        # journal, creative, poetry, letter, fiction).
        if label_filter:
            label = n["rel"].split("/", 1)[0] if "/" in n["rel"] else "_"
            if label not in label_filter:
                continue
        # Skip already-seen notes (commonplace "deal from deck" mode).
        if exclude_rels and n["rel"] in exclude_rels:
            continue
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
        era_budgets[name] = max(MIN_ERA_BUDGET, int(cap * share))

    # Scale budgets down if they overshoot (floor can push total above cap).
    budget_total = sum(era_budgets.values())
    if budget_total > cap:
        scale = cap / budget_total
        era_budgets = {k: int(v * scale) for k, v in era_budgets.items()}

    sampled_by_era: dict[str, list[dict]] = {}
    for name in era_budgets:
        era_notes = by_era.get(name, [])
        if not era_notes:
            continue
        sampled = budget_sample(era_notes, era_budgets[name], corpus_id,
                               highlighted_rels=highlighted_rels)
        # Tag each note with its era name so shuffle mode can include it.
        for n in sampled:
            n["era"] = name
        sampled_by_era[name] = sampled

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

    ordered_notes: list[dict] = []
    if shuffle:
        # Flatten all sampled notes and randomize — no era grouping.
        all_sampled = [
            n for ns in sampled_by_era.values() for n in ns
        ]
        random.shuffle(all_sampled)
        ordered_notes = all_sampled
        for n in all_sampled:
            date = (n.get("date") or "")[:10]
            title = n.get("title") or "(untitled)"
            era = n.get("era", "")
            lines.append(f"==== [{date}] · {era} · {title} ====")
            lines.append("")
            lines.append(n["body"])
            lines.append("")
    else:
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
            ordered_notes.extend(sampled)
    text = "\n".join(lines)
    # Collect rels of all sampled notes (for "seen" tracking).
    sampled_rels = [
        n["rel"]
        for ns in sampled_by_era.values()
        for n in ns
    ]
    if return_notes:
        return text, sampled_rels, ordered_notes
    return text, sampled_rels
