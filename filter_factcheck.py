#!/usr/bin/env python3
"""Post-process a `.factcheck.json` file. For each NOT_SUPPORTED claim,
extract distinctive proper nouns / acronyms and search the paragraph's era
(all notes in the era's date range, not just the ones cited in the chapter)
for literal matches. If any term hits, mark the claim as
`likely_grounded_elsewhere` so triage can deprioritize it.

Annotates in-place. Run after `factcheck_narrative.py`.

Usage:
    python3 _raw/filter_factcheck.py                   # _narrative_naive.factcheck.json
    python3 _raw/filter_factcheck.py _narrative.factcheck.json
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from narrative_phase_b import (  # type: ignore
    CORPUS,
    ERAS,
    NARRATIVES_DIR,
    apply_authorship,
    apply_date_overrides,
    apply_note_about,
    load_authorship,
    load_phase_a,
    log,
    parse_note_body,
)

PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]+(?:[-\u2019'][a-z]+)*(?:\s+[A-Z][a-z]+(?:[-\u2019'][a-z]+)*)*\b")
ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:&[A-Z]+)?\b")

STOPWORDS = {
    # Sentence-initial / common starters that happen to be capitalized
    "The", "He", "She", "His", "Her", "It", "Its", "They", "Their", "There",
    "This", "That", "These", "Those", "A", "An", "And", "But", "For", "When",
    "While", "After", "Before", "On", "In", "At", "Of", "By", "To", "So",
    "Yes", "No", "Just", "Like", "Not", "Still", "Maybe", "Probably", "Only",
    "Sometimes", "Here", "There", "Then", "Now", "Never", "Always", "Often",
    "Andrew", "Andrews", "He's", "She's", "It's", "They've",
    # Very common words capitalized mid-sentence
    "I", "I'm", "I've", "I'd", "I'll", "Yes", "No", "OK",
}


def extract_terms(claim: str) -> set[str]:
    """Pull distinctive proper nouns / acronyms from a claim string."""
    terms: set[str] = set()
    for m in PROPER_NOUN_RE.finditer(claim):
        t = m.group()
        # Multi-word phrases are almost always real proper nouns — keep
        if " " in t:
            terms.add(t)
            continue
        # Single word: filter stopwords and very short
        if t in STOPWORDS:
            continue
        if len(t) < 4:
            continue
        terms.add(t)
    for m in ACRONYM_RE.finditer(claim):
        terms.add(m.group())
    return terms


def chapter_to_era_idx(chapter_id: int) -> int | None:
    """Chapter 1 in the narrative is the title page. Chapters 2..9 map to
    ERAS[0..7]. Returns None for chapter 1."""
    if chapter_id <= 1:
        return None
    idx = chapter_id - 2
    if 0 <= idx < len(ERAS):
        return idx
    return None


def notes_in_era(notes, era_idx: int) -> list:
    _, start, end = ERAS[era_idx]
    out = []
    for n in notes:
        d = (n.get("date") or "")[:7]
        if start <= d <= end:
            out.append(n)
    return out


def main():
    target_name = sys.argv[1] if len(sys.argv) > 1 else "_narrative_naive.factcheck.json"
    target = NARRATIVES_DIR / target_name
    if not target.exists():
        print(f"ERROR: {target} not found")
        sys.exit(1)

    log("loading notes...")
    notes = load_phase_a()
    apply_date_overrides(notes)
    apply_note_about(notes)
    verdicts = load_authorship()
    notes, _, _ = apply_authorship(notes, verdicts)

    # Pre-compute note bodies per era so we don't read each file multiple times.
    log("caching era note bodies...")
    era_bodies: dict[int, list[tuple[str, str]]] = {}
    for era_idx in range(len(ERAS)):
        era_notes = notes_in_era(notes, era_idx)
        era_bodies[era_idx] = [
            (n["rel"], (parse_note_body(n["rel"]) or ""))
            for n in era_notes
        ]
        log(f"  era {era_idx} ({ERAS[era_idx][0]}): {len(era_notes)} notes")

    # Chapter 1 (title page) and anything else without an era mapping sweeps
    # the whole corpus — those paragraphs are usually meta-summaries whose
    # concrete hooks could come from any era.
    all_bodies = [b for era_idx in era_bodies for b in era_bodies[era_idx]]

    data = json.loads(target.read_text(encoding="utf-8"))
    total_claims = 0
    grounded_claims = 0
    no_terms_claims = 0

    for para in data:
        era_idx = chapter_to_era_idx(para["chapter"])
        bodies = all_bodies if era_idx is None else era_bodies[era_idx]
        scope = "all eras" if era_idx is None else f"era {era_idx} ({ERAS[era_idx][0]})"
        for c in para["unsupported_claims"]:
            total_claims += 1
            terms = extract_terms(c["claim"])
            if not terms:
                c["likely_grounded_elsewhere"] = False
                c["filter_note"] = "no distinctive terms extracted"
                no_terms_claims += 1
                continue
            hits: dict[str, list[str]] = {}
            for term in terms:
                for rel, body in bodies:
                    if term in body:
                        hits.setdefault(term, []).append(rel)
            if hits:
                c["likely_grounded_elsewhere"] = True
                c["grounded_terms"] = {t: rels[:3] for t, rels in hits.items()}
                c["filter_scope"] = scope
                grounded_claims += 1
            else:
                c["likely_grounded_elsewhere"] = False
                c["filter_note"] = f"no hits for terms {sorted(terms)} in {scope}"

    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"annotated {total_claims} claims:")
    log(f"  {grounded_claims} likely grounded elsewhere ({100*grounded_claims/total_claims:.0f}%)")
    log(f"  {no_terms_claims} had no extractable terms")
    log(f"  {total_claims - grounded_claims - no_terms_claims} remain flagged")


if __name__ == "__main__":
    main()
