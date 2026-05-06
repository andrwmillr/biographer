"""Commonplace book — extract standout passages from the notes corpus.

Each run samples unseen notes (proportional by era, filtered to
high-signal folders), sends them to the LLM with the extraction prompt,
and appends new passages to the growing commonplace book. Tracks which
notes have been seen so subsequent runs draw from a shrinking pool
until the entire corpus has been processed.

WS /commonplace-session  — streaming extraction session
GET /commonplace/latest   — return the accumulated commonplace book
GET /commonplace/progress — how many notes seen vs total
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from api.auth import _gc_auth, _load_auth
from api.config import COMMONPLACE_PATH, REPO
from api.corpora import (
    _session_corpus_id,
    corpus_dir,
    is_sample_corpus,
    require_corpus_access,
)
from claude_agent_sdk import ClaudeAgentOptions
from core import corpus as wb
from core.session import Session, create_session, get_session
from core.telemetry import log as tlog
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

router = APIRouter()

# Only process folders likely to contain the person's own writing.
HIGH_SIGNAL_LABELS = {"journal", "creative", "poetry", "letter", "fiction", "other"}

# Char budget per run — ~50 notes per batch.
CHAR_CAP = 75_000


def _commonplace_base(corpus_id: str) -> Path:
    return wb.corpus_root(corpus_id) / "claude" / "commonplace"


def _seen_path(corpus_id: str) -> Path:
    return _commonplace_base(corpus_id) / "seen.json"


def _load_seen(corpus_id: str) -> set[str]:
    p = _seen_path(corpus_id)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return set()


def _save_seen(seen: set[str], corpus_id: str) -> None:
    p = _seen_path(corpus_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    tmp.replace(p)


def _staging_path(corpus_id: str) -> Path:
    return _commonplace_base(corpus_id) / "staging.json"


def _load_staging(corpus_id: str) -> list[dict]:
    p = _staging_path(corpus_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _save_staging(entries: list[dict], corpus_id: str) -> None:
    p = _staging_path(corpus_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(p)


def _norm_quotes(s: str) -> str:
    return (s.replace("‘", "'").replace("’", "'").replace("′", "'")
             .replace("“", '"').replace("”", '"').replace("″", '"')
             .replace("–", "-").replace("—", "-"))


def _remove_passage(date: str, title: str, body: str,
                    corpus_id: str) -> bool:
    """Remove a passage block from canonical.md. Returns True if found.
    Matches on date + title + body (quote-normalized) so duplicate
    headers with different bodies are disambiguated."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        return False
    content = canonical.read_text(encoding="utf-8")
    blocks = content.split("### ")
    kept = []
    removed = False
    norm_title = _norm_quotes(title)
    norm_body = _norm_quotes(body.strip())
    for block in blocks:
        if not block.strip():
            continue
        header, _, block_body = block.partition("\n")
        parts = header.strip().split(" · ")
        b_date = (parts[0] if parts else "").replace("[", "").replace("]", "")
        b_title = parts[2].strip() if len(parts) > 2 else ""
        if (b_date == date
                and _norm_quotes(b_title) == norm_title
                and _norm_quotes(block_body.strip()) == norm_body
                and not removed):
            removed = True
            continue
        kept.append(block)
    if not removed:
        return False
    new_content = ("### " + "### ".join(kept)).strip() + "\n" if kept else ""
    canonical.write_text(new_content, encoding="utf-8")
    return True


def _add_passage(date: str, era: str, title: str, body: str,
                 corpus_id: str) -> None:
    """Append a passage block to canonical.md."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    existing = canonical.read_text(encoding="utf-8") if canonical.exists() else ""
    if existing and not existing.endswith("\n\n"):
        existing = existing.rstrip("\n") + "\n\n"
    block = f"### [{date}] · {era} · {title}\n\n{body.strip()}\n"
    canonical.write_text(existing + block, encoding="utf-8")


def _count_eligible(corpus_id: str) -> int:
    """Count high-signal notes in the corpus."""
    notes = wb.load_corpus_notes(corpus_id)
    count = 0
    for n in notes:
        if "/" in n["rel"]:
            label = n["rel"].split("/", 1)[0]
            if label not in HIGH_SIGNAL_LABELS:
                continue
        count += 1
    return count


def _prepare_run(corpus_id: str) -> dict:
    """Build the commonplace input from unseen notes and create a run dir."""
    from core.sampling import build_input

    seen = _load_seen(corpus_id)
    user_msg, sampled_rels, ordered_notes = build_input(
        top_n=0,
        corpus_id=corpus_id,
        char_cap=CHAR_CAP,
        label_filter=HIGH_SIGNAL_LABELS,
        exclude_rels=seen,
        shuffle=True,
        return_notes=True,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _commonplace_base(corpus_id) / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.md").write_text(user_msg, encoding="utf-8")
    (run_dir / "sampled_rels.json").write_text(
        json.dumps(sampled_rels), encoding="utf-8"
    )
    # Slim note data for the frontend (date, title, era, body).
    notes_for_client = [
        {
            "date": (n.get("date") or "")[:10],
            "title": n.get("title") or "",
            "era": n.get("era", ""),
            "body": n.get("body", ""),
        }
        for n in ordered_notes
    ]
    return {
        "run_dir": run_dir,
        "run_rel": str(run_dir.relative_to(REPO)),
        "full_user_msg": user_msg,
        "in_chars": len(user_msg),
        "sampled_count": len(sampled_rels),
        "seen_before": len(seen),
        "total_eligible": _count_eligible(corpus_id),
        "notes": notes_for_client,
    }


def _build_kickoff(run_dir: Path, corpus_sample: str, corpus_id: str,
                    guidance: str | None = None) -> str:
    guidance_block = ""
    if guidance:
        guidance_block = (
            "**The user has a specific request for this run:**\n\n"
            f"> {guidance}\n\n"
            "This is a hard filter, not a suggestion. Only extract passages "
            "that match this request. If a note is beautifully written but "
            "doesn't fit what the user asked for, skip it. The craft standard "
            "from your system prompt still applies — but the guidance decides "
            "which notes are even eligible.\n\n"
        )
    return (
        wb.subject_context_for(corpus_id)
        + "You're building a commonplace book — extracting the best passages "
        "from this person's notes archive. The notes are inlined between "
        "INPUT-START / INPUT-END below.\n\n"
        + guidance_block
        + "Read through every note. For each note that has something worth "
        "keeping, extract the standout passage(s) using the format from your "
        "system prompt. Skip notes with nothing remarkable — no explanation "
        "needed.\n\n"
        "**Pacing is critical.** The user is reading each passage as it "
        "appears. After you find each passage, write the accumulated results "
        f"to {run_dir}/commonplace.md using the Write tool immediately — "
        "don't batch them up. One passage, one Write. The user sees each "
        "update in real time and needs a moment to read before the next one "
        "appears. This is a reading experience, not a dump.\n\n"
        "End the file with the DONE line as described in your system prompt.\n\n"
        "--- INPUT-START ---\n\n"
        + corpus_sample
        + "\n\n--- INPUT-END ---\n"
    )


def _promote_empty(run_dir: Path, corpus_id: str,
                   persist: bool = True) -> None:
    """Mark sampled notes as seen without adding any passages.
    Used when triage finds nothing worth extracting."""
    if not persist:
        return
    rels_file = run_dir / "sampled_rels.json"
    if rels_file.exists():
        new_rels = set(json.loads(rels_file.read_text(encoding="utf-8")))
        seen = _load_seen(corpus_id)
        seen |= new_rels
        _save_seen(seen, corpus_id)


def _promote(run_dir: Path, corpus_id: str,
             persist: bool = True) -> dict:
    """Append this run's passages to the canonical commonplace book and
    mark the sampled notes as seen.  When persist=False (sample corpora),
    return the run's content without writing to disk."""
    src = run_dir / "commonplace.md"
    if not src.is_file():
        raise ValueError(f"no commonplace.md in {run_dir}")

    new_content = src.read_text(encoding="utf-8")

    if persist:
        # Append to canonical (don't overwrite — accumulates across runs).
        canonical = _commonplace_base(corpus_id) / "canonical.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        existing = canonical.read_text(encoding="utf-8") if canonical.exists() else ""
        if existing and not existing.endswith("\n\n"):
            existing = existing.rstrip("\n") + "\n\n"
        combined = existing + new_content
        canonical.write_text(combined, encoding="utf-8")

        # Mark sampled notes as seen.
        rels_file = run_dir / "sampled_rels.json"
        if rels_file.exists():
            new_rels = set(json.loads(rels_file.read_text(encoding="utf-8")))
            seen = _load_seen(corpus_id)
            seen |= new_rels
            _save_seen(seen, corpus_id)
    else:
        combined = new_content

    return {
        "content": combined,
        "location": str(run_dir.relative_to(REPO)),
        "words": len(combined.split()),
        "new_words": len(new_content.split()),
        "overwritten": False,
    }


async def _commonplace_watch(session: Session) -> None:
    """Watch commonplace.md for changes and stream draft updates."""
    p = session.run_dir / "commonplace.md"
    last_m: float | None = None
    while True:
        await asyncio.sleep(0.5)
        try:
            m = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if last_m == m:
            continue
        last_m = m
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        await session.emit({
            "type": "draft_update",
            "kind": "commonplace",
            "content": content,
        })
        if session.finalize_pending:
            session.finalize_pending = False
            try:
                _persist = not is_sample_corpus(session.corpus_id)
                result = _promote(session.run_dir, session.corpus_id, persist=_persist)
                await session.emit({
                    "type": "finalized",
                    "content": result["content"],
                    "location": result["location"],
                    "words": result["words"],
                    "overwritten": result["overwritten"],
                })
            except Exception:
                pass


_PASSAGE_HEADER_RE = re.compile(r"^### \[(\d{4}-\d{2}-\d{2})\] · .+ · (.*)$", re.MULTILINE)


def load_highlighted_keys(corpus_id: str) -> set[tuple[str, str]]:
    """Parse canonical.md and return (date, title) pairs for every note
    that has at least one highlighted passage."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        return set()
    text = canonical.read_text(encoding="utf-8")
    return {(m.group(1), m.group(2).strip()) for m in _PASSAGE_HEADER_RE.finditer(text)}


def load_passages_for_era(era_name: str, corpus_id: str) -> str:
    """Return the passage blocks from canonical.md whose dates fall
    within the given era. Used to inject highlights into the chapter
    drafting prompt."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        return ""
    text = canonical.read_text(encoding="utf-8")
    eras = wb.load_eras(corpus_id)
    blocks = text.split("### ")
    out = []
    for block in blocks:
        if not block.strip():
            continue
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})\]", block)
        if not m:
            continue
        date = m.group(1)
        if wb.era_of(date, eras) == era_name:
            out.append("### " + block.rstrip())
    return "\n\n".join(out)


def load_all_passages(corpus_id: str) -> str:
    """Return the full canonical.md content, or empty string if none."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        return ""
    return canonical.read_text(encoding="utf-8").strip()


def load_highlighted_rels(corpus_id: str) -> set[str]:
    """Return the set of note rels that have commonplace passages.
    Matches on (date, title); falls back to date-only when a title
    doesn't match any note (LLM may have cleaned/shortened it)."""
    keys = load_highlighted_keys(corpus_id)
    if not keys:
        return set()
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)

    # Build lookup: (date[:10], title) -> rel
    by_date_title: dict[tuple[str, str], str] = {}
    by_date: dict[str, list[str]] = {}
    for n in notes:
        d = n.get("date", "")[:10]
        t = n.get("title", "").strip()
        by_date_title[(d, t)] = n["rel"]
        by_date.setdefault(d, []).append(n["rel"])

    rels: set[str] = set()
    for date, title in keys:
        # Exact match on date+title
        rel = by_date_title.get((date, title))
        if rel:
            rels.add(rel)
            continue
        # Fallback: if only one note on that date, use it
        candidates = by_date.get(date, [])
        if len(candidates) == 1:
            rels.add(candidates[0])
    return rels


# ---- REST endpoints ----

def _build_guidance_map(corpus_id: str) -> list[dict]:
    """Scan run dirs and return an ordered list mapping passage ranges to prompts.

    Each entry: {"guidance": str|null, "count": int} — passage count for that run.
    Runs are sorted chronologically (same order they were appended to canonical.md).
    """
    base = _commonplace_base(corpus_id)
    if not base.exists():
        return []
    runs = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not d.name.startswith("run_"):
            continue
        cp = d / "commonplace.md"
        if not cp.exists():
            continue
        content = cp.read_text(encoding="utf-8")
        count = content.count("\n### ")
        if content.startswith("### "):
            count += 1
        if count == 0:
            continue
        gp = d / "guidance.txt"
        guidance = gp.read_text(encoding="utf-8").strip() if gp.exists() else None
        runs.append({"guidance": guidance, "count": count})
    return runs


@router.get("/commonplace/latest")
def get_latest(session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        raise HTTPException(404, "no commonplace book yet")
    return {
        "content": canonical.read_text(encoding="utf-8"),
        "runs": _build_guidance_map(corpus_id),
    }


@router.get("/commonplace/note")
def get_note(
    date: str = Query("", description="YYYY-MM-DD"),
    title: str = Query(""),
    session: str = Depends(require_corpus_access),
):
    """Look up a note by date + title and return its full body."""
    corpus_id = _session_corpus_id(session)
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)

    def _make_result(n: dict) -> dict:
        body = wb.parse_note_body(n["rel"], corpus_id)
        n_title = n.get("title") or ""
        n_date = (n.get("date") or "")[:10]
        label = n["rel"].split("/", 1)[0] if "/" in n["rel"] else ""
        return {"body": body, "title": n_title, "date": n_date, "label": label}

    # Collect all notes on this date.
    date_matches = [
        n for n in notes if (n.get("date") or "")[:10] == date
    ]
    if not date_matches:
        raise HTTPException(404, "note not found")

    # 1) Exact title match.
    for n in date_matches:
        if (n.get("title") or "") == title:
            return _make_result(n)

    # 2) Case-insensitive title match.
    title_lower = title.strip().lower()
    for n in date_matches:
        if (n.get("title") or "").strip().lower() == title_lower:
            return _make_result(n)

    # 3) If only one note on this date, return it regardless of title.
    if len(date_matches) == 1:
        return _make_result(date_matches[0])

    # 4) If title looks empty/untitled, return first match.
    if not title or title_lower in ("", "(untitled)", "untitled"):
        return _make_result(date_matches[0])

    # 5) Substring match — LLM may have truncated the title.
    for n in date_matches:
        n_title = (n.get("title") or "").lower()
        if title_lower and (title_lower in n_title or n_title in title_lower):
            return _make_result(n)

    # 6) Last resort — return the first date match.
    return _make_result(date_matches[0])


def _eligible_notes(corpus_id: str) -> tuple[list[dict], set[str]]:
    """Return (eligible_notes, seen_set) for the corpus.
    Each note dict has rel, date, title, era, body."""
    import random as _rand
    seen = _load_seen(corpus_id)
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    eligible = []
    for n in notes:
        if "/" in n["rel"]:
            label = n["rel"].split("/", 1)[0]
            if label not in HIGH_SIGNAL_LABELS:
                continue
        if n["rel"] in seen:
            continue
        body = wb.parse_note_body(n["rel"], corpus_id)
        if len(body) < 80:
            continue
        era = ""
        ym = (n.get("date") or "")[:7]
        for name, lo, hi in eras:
            if (lo or "") <= ym <= (hi or "9999"):
                era = name
                break
        eligible.append({
            "rel": n["rel"],
            "date": (n.get("date") or "")[:10],
            "title": n.get("title") or "",
            "era": era,
            "body": body,
        })
    _rand.shuffle(eligible)
    return eligible, seen


def _sample_up_to(eligible: list[dict], cap: int) -> list[dict]:
    """Take notes from eligible until char cap is reached."""
    sampled = []
    used = 0
    for n in eligible:
        if used + len(n["body"]) > cap and sampled:
            break
        sampled.append(n)
        used += len(n["body"])
    return sampled


def _deal_response(sampled: list[dict], seen: set[str],
                   corpus_id: str) -> dict:
    """Build the response dict for deal/curate endpoints."""
    total = _count_eligible(corpus_id)
    return {
        "notes": [
            {"rel": n["rel"], "date": n["date"], "title": n["title"],
             "era": n["era"], "body": n["body"]}
            for n in sampled
        ],
        "seen": len(seen),
        "total": total,
        "complete": len(seen) >= total,
    }


def _browse_eligible(corpus_id: str, *, dismissed_only: bool = False) -> list[dict]:
    """Return eligible notes sorted chronologically (no body).
    By default skips dismissed notes; with dismissed_only=True returns only dismissed."""
    seen = _load_seen(corpus_id)
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    wb.apply_note_metadata(notes, corpus_id)
    eras = wb.load_eras(corpus_id)

    eligible = []
    for n in notes:
        # Filter by high-signal label when notes live in subfolders (e.g. journal/, creative/).
        # Skip the filter for flat corpora (sample corpora) where rels have no folder prefix.
        if "/" in n["rel"]:
            label = n["rel"].split("/", 1)[0]
            if label not in HIGH_SIGNAL_LABELS:
                continue
        in_seen = n["rel"] in seen
        if dismissed_only and not in_seen:
            continue
        if not dismissed_only and in_seen:
            continue
        body_len = len(wb.parse_note_body(n["rel"], corpus_id))
        if body_len < 80:
            continue
        era = ""
        ym = (n.get("date") or "")[:7]
        for name, lo, hi in eras:
            if (lo or "") <= ym <= (hi or "9999"):
                era = name
                break
        eligible.append({
            "rel": n["rel"],
            "date": (n.get("date") or "")[:10],
            "title": n.get("title") or "",
            "era": era,
        })
    eligible.sort(key=lambda n: n["date"])
    return eligible


@router.get("/commonplace/browse/index")
def browse_index(session: str = Depends(require_corpus_access)):
    """Return lightweight index of all browsable notes (no bodies)."""
    corpus_id = _session_corpus_id(session)
    staged_rels = {s["rel"] for s in _load_staging(corpus_id)}
    eligible = _browse_eligible(corpus_id)
    for n in eligible:
        n["staged"] = n["rel"] in staged_rels
    return {"notes": eligible, "total": len(eligible)}


@router.get("/commonplace/browse")
def browse_notes(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    session: str = Depends(require_corpus_access),
):
    """Return high-signal notes in chronological order with pagination.
    Skips already-dismissed notes. Includes staging status."""
    corpus_id = _session_corpus_id(session)
    staged_rels = {s["rel"] for s in _load_staging(corpus_id)}
    eligible = _browse_eligible(corpus_id)

    # Add bodies only for the requested page.
    page = eligible[offset:offset + limit]
    for n in page:
        n["body"] = wb.parse_note_body(n["rel"], corpus_id)
        n["staged"] = n["rel"] in staged_rels

    return {
        "notes": page,
        "offset": offset,
        "total": len(eligible),
    }


@router.get("/commonplace/browse/dismissed")
def browse_dismissed(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    session: str = Depends(require_corpus_access),
):
    """Return dismissed notes in chronological order with pagination.
    Excludes notes that are currently staged (saved)."""
    corpus_id = _session_corpus_id(session)
    staged_rels = {s["rel"] for s in _load_staging(corpus_id)}
    eligible = [
        n for n in _browse_eligible(corpus_id, dismissed_only=True)
        if n["rel"] not in staged_rels
    ]

    page = eligible[offset:offset + limit]
    for n in page:
        n["body"] = wb.parse_note_body(n["rel"], corpus_id)

    return {
        "notes": page,
        "offset": offset,
        "total": len(eligible),
    }


@router.post("/commonplace/undismiss")
def undismiss_note(
    rel: str = Query(...),
    session: str = Depends(require_corpus_access),
):
    """Remove a note from the seen/dismissed set."""
    corpus_id = _session_corpus_id(session)
    seen = _load_seen(corpus_id)
    seen.discard(rel)
    _save_seen(seen, corpus_id)
    return {"ok": True}


@router.post("/commonplace/deal")
def deal_notes(session: str = Depends(require_corpus_access)):
    """Deal a random batch of unseen notes for manual curation."""
    corpus_id = _session_corpus_id(session)
    eligible, seen = _eligible_notes(corpus_id)
    if not eligible:
        return {"notes": [], "complete": True}

    sampled = _sample_up_to(eligible, CHAR_CAP)
    return _deal_response(sampled, seen, corpus_id)


# Budget for curate mode — sample more candidates than we'll return.
CURATE_CANDIDATE_CAP = CHAR_CAP * 3


@router.post("/commonplace/curate")
async def curate_notes(session: str = Depends(require_corpus_access)):
    """Deal notes filtered by LLM taste-matching against past highlights."""
    from anthropic import AsyncAnthropic

    corpus_id = _session_corpus_id(session)
    eligible, seen = _eligible_notes(corpus_id)
    if not eligible:
        return {"notes": [], "complete": True}

    # Sample a larger pool of candidates.
    candidates = _sample_up_to(eligible, CURATE_CANDIDATE_CAP)

    # Load staged notes as taste examples.
    staging = _load_staging(corpus_id)
    highlights = ""
    if staging:
        parts = []
        for entry in staging:
            header = f"### [{entry['date']}] · {entry.get('era', '')} · {entry.get('title', '')}"
            if entry.get("highlights"):
                parts.append(header + "\n\n" + "\n· · ·\n".join(entry["highlights"]))
            else:
                parts.append(header + "\n\n" + entry.get("body", ""))
        highlights = "\n\n".join(parts)

    if not highlights:
        # No taste data yet — fall back to random deal.
        sampled = candidates[:len(_sample_up_to(candidates, CHAR_CAP))]
        return _deal_response(sampled, seen, corpus_id)

    # Build the LLM prompt.
    candidate_blocks = []
    for i, n in enumerate(candidates):
        header = f"[{i}] {n['date']} · {n['era']} · {n['title'] or '(untitled)'}"
        # Truncate long notes to keep prompt reasonable.
        body = n["body"][:2000] + ("..." if len(n["body"]) > 2000 else "")
        candidate_blocks.append(f"=== {header} ===\n{body}")

    prompt = (
        "You are helping someone curate a commonplace book from their personal "
        "archive. Below are passages they have previously chosen to highlight. "
        "Study the patterns — what kind of writing draws them, what they skip.\n\n"
        "--- THEIR HIGHLIGHTS ---\n\n"
        f"{highlights}\n\n"
        "--- END HIGHLIGHTS ---\n\n"
        "Now here are candidate notes. Return ONLY the index numbers (the [N] "
        "at the start of each note) of notes this person would likely want to "
        "highlight, based on the pattern above. Return them as a JSON array of "
        "integers, e.g. [0, 3, 7, 12]. Be selective — only pick notes where "
        "the writing genuinely matches their taste. If none match, return [].\n\n"
        "--- CANDIDATES ---\n\n"
        + "\n\n".join(candidate_blocks)
        + "\n\n--- END CANDIDATES ---"
    )

    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Parse the JSON array from the response.
        # Handle cases where the LLM wraps it in markdown.
        match = re.search(r"\[[\d,\s]*\]", text)
        if match:
            indices = json.loads(match.group())
            picked = [
                candidates[i] for i in indices
                if isinstance(i, int) and 0 <= i < len(candidates)
            ]
        else:
            picked = []
    except Exception as e:
        print(f"[commonplace/curate] LLM error: {e}")
        # Fall back to random on error.
        picked = _sample_up_to(candidates, CHAR_CAP)

    if not picked:
        # LLM found nothing — return a small random set anyway.
        picked = _sample_up_to(candidates, CHAR_CAP)

    return _deal_response(picked, seen, corpus_id)


@router.delete("/commonplace/passage")
def reject_passage(
    date: str = Query("", description="YYYY-MM-DD"),
    title: str = Query(""),
    body: str = Query(""),
    session: str = Depends(require_corpus_access),
):
    """Remove a passage from the commonplace book."""
    corpus_id = _session_corpus_id(session)
    if not _remove_passage(date, title, body, corpus_id):
        raise HTTPException(404, "passage not found")
    return {"ok": True}


@router.post("/commonplace/dismiss")
def dismiss_note(
    rel: str = Query("", description="Note rel path"),
    session: str = Depends(require_corpus_access),
):
    """Mark a single note as seen (dismissed without highlighting)."""
    corpus_id = _session_corpus_id(session)
    if not rel:
        raise HTTPException(400, "missing rel")
    seen = _load_seen(corpus_id)
    seen.add(rel)
    _save_seen(seen, corpus_id)
    return {"ok": True}


@router.post("/commonplace/passage")
def add_passage(
    date: str = Query("", description="YYYY-MM-DD"),
    era: str = Query(""),
    title: str = Query(""),
    body: str = Query(""),
    rel: str = Query("", description="Note rel path"),
    session: str = Depends(require_corpus_access),
):
    """Add a user-highlighted passage to the commonplace book."""
    corpus_id = _session_corpus_id(session)
    if not body.strip():
        raise HTTPException(400, "empty body")
    _add_passage(date, era, title, body, corpus_id)
    # Mark the source note as seen.
    if rel:
        seen = _load_seen(corpus_id)
        seen.add(rel)
        _save_seen(seen, corpus_id)
    return {"ok": True}


@router.put("/commonplace/note")
def edit_note(
    rel: str = Query("", description="Note rel path"),
    body: str = Query(""),
    session: str = Depends(require_corpus_access),
):
    """Update a note's body, preserving its frontmatter."""
    corpus_id = _session_corpus_id(session)
    if not rel:
        raise HTTPException(400, "missing rel")
    path = wb._safe_note_path(rel, corpus_id)
    if path is None or not path.is_file():
        raise HTTPException(404, "note not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^(---\n.*?\n---\n)", text, re.DOTALL)
    if m:
        new_text = m.group(1) + "\n" + body.strip() + "\n"
    else:
        new_text = body.strip() + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True}


# ---- Staging endpoints ----

@router.get("/commonplace/staging")
def get_staging(session: str = Depends(require_corpus_access)):
    """Return all staged notes."""
    corpus_id = _session_corpus_id(session)
    return {"notes": _load_staging(corpus_id)}


@router.post("/commonplace/stage")
def stage_note(
    rel: str = Query(""),
    date: str = Query(""),
    era: str = Query(""),
    title: str = Query(""),
    body: str = Query(""),
    highlight: str = Query("", description="Highlighted fragment, or empty for full note"),
    session: str = Depends(require_corpus_access),
):
    """Add a note to the staging area. If the note is already staged,
    append the highlight to its list."""
    corpus_id = _session_corpus_id(session)
    if not rel:
        raise HTTPException(400, "missing rel")

    entries = _load_staging(corpus_id)
    existing = next((e for e in entries if e["rel"] == rel), None)
    if existing:
        # Add highlight if it's new and non-empty.
        if highlight and highlight not in existing.get("highlights", []):
            existing.setdefault("highlights", []).append(highlight)
        # Update body in case it was edited.
        existing["body"] = body
    else:
        entry = {
            "rel": rel, "date": date, "era": era, "title": title,
            "body": body,
            "highlights": [highlight] if highlight else [],
        }
        entries.append(entry)

    _save_staging(entries, corpus_id)

    # Mark as seen.
    seen = _load_seen(corpus_id)
    seen.add(rel)
    _save_seen(seen, corpus_id)
    return {"ok": True}


@router.delete("/commonplace/stage")
def unstage_note(
    rel: str = Query(""),
    session: str = Depends(require_corpus_access),
):
    """Remove a note from the staging area."""
    corpus_id = _session_corpus_id(session)
    if not rel:
        raise HTTPException(400, "missing rel")
    entries = _load_staging(corpus_id)
    entries = [e for e in entries if e["rel"] != rel]
    _save_staging(entries, corpus_id)
    return {"ok": True}


@router.post("/commonplace/staging/clear")
def clear_staging(session: str = Depends(require_corpus_access)):
    """Empty the staging area."""
    corpus_id = _session_corpus_id(session)
    _save_staging([], corpus_id)
    return {"ok": True}


@router.get("/commonplace/progress")
def get_progress(session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    seen = _load_seen(corpus_id)
    total = _count_eligible(corpus_id)
    return {
        "seen": len(seen),
        "total": total,
        "complete": len(seen) >= total,
    }


@router.post("/commonplace/reshuffle")
def reshuffle(session: str = Depends(require_corpus_access)):
    """Clear all seen state — every note becomes eligible again."""
    corpus_id = _session_corpus_id(session)
    _save_seen(set(), corpus_id)
    total = _count_eligible(corpus_id)
    return {"seen": 0, "total": total, "complete": False}


# ---- WebSocket session ----

@router.websocket("/commonplace-session")
async def commonplace_session(ws: WebSocket):
    await ws.accept()

    async def send(obj: dict):
        try:
            await ws.send_text(json.dumps(obj))
        except Exception:
            pass

    async def reject(message: str):
        await send({"type": "error", "message": message})
        try:
            await ws.close()
        except Exception:
            pass

    try:
        first = await ws.receive_json()
    except WebSocketDisconnect:
        return
    if first.get("type") != "start":
        await reject("first message must be {type:'start', session, token}")
        return

    session_slug = first.get("session")
    auth_token = first.get("token")
    if not session_slug:
        await reject("missing session in start message")
        return
    try:
        corpus_dir(session_slug)
    except HTTPException as e:
        await reject(e.detail)
        return

    if not is_sample_corpus(session_slug):
        if not auth_token:
            await reject("auth required")
            return
        state = _gc_auth(_load_auth())
        record = state["sessions"].get(auth_token)
        if not record:
            await reject("invalid or expired auth token")
            return
        if session_slug not in state["users"].get(record["email"], []):
            await reject("corpus not owned by authenticated user")
            return
    corpus_id = _session_corpus_id(session_slug)
    sample = is_sample_corpus(session_slug)
    persist = not sample
    user_email = record["email"] if not sample else "(sample)"

    session: Session | None = None
    try:
        # Hot resume — if an extraction session is already running, reattach.
        resume_run_rel = first.get("run_id") if first.get("resume") else None
        if resume_run_rel:
            existing = get_session(resume_run_rel)
            if existing is not None and existing.kind == "commonplace" and existing.corpus_id == corpus_id:
                session = existing
                await session.attach(ws)

        if session is None:
            model_key = first.get("model")
            model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

            prep = _prepare_run(corpus_id=corpus_id)
            run_dir = prep["run_dir"]
            run_rel = prep["run_rel"]

            if prep["sampled_count"] == 0:
                await send({
                    "type": "error",
                    "message": "all notes have been processed — commonplace book is complete",
                })
                await ws.close()
                return

            user_guidance = (first.get("guidance") or "").strip() or None
            if user_guidance:
                (run_dir / "guidance.txt").write_text(
                    user_guidance, encoding="utf-8"
                )
            kickoff = _build_kickoff(run_dir, prep["full_user_msg"], corpus_id,
                                     guidance=user_guidance)
            system_prompt = COMMONPLACE_PATH.read_text(encoding="utf-8")

            sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

            runs_parent = run_dir.parent
            settings = {
                "permissions": {
                    "deny": [
                        f"Read({runs_parent}/**)",
                        f"Edit({runs_parent}/**)",
                        f"Write({runs_parent}/**)",
                    ],
                    "allow": [
                        f"Read({run_dir}/**)",
                        f"Edit({run_dir}/**)",
                        f"Write({run_dir}/**)",
                    ],
                }
            }
            settings_path = run_dir / ".claude-settings.json"
            settings_path.write_text(json.dumps(settings), encoding="utf-8")

            options = ClaudeAgentOptions(
                model=model,
                system_prompt=system_prompt,
                permission_mode="acceptEdits",
                allowed_tools=["Read", "Edit", "Write"],
                settings=str(settings_path),
                cwd=str(run_dir),
                include_partial_messages=True,
                effort="low",
                env=sub_env,
            )

            spawned_event = {
                "type": "spawned",
                "model": model,
                "run_dir": run_rel,
                "run_id": run_dir.name,
                "input_chars": prep["in_chars"],
                "sampled_count": prep["sampled_count"],
                "seen_before": prep["seen_before"],
                "total_eligible": prep["total_eligible"],
                "guidance": user_guidance,
                "notes": prep["notes"],
            }

            async def on_turn_complete(text: str) -> None:
                if session and not session.finalize_pending:
                    session.finalize_pending = True
                    await asyncio.sleep(1.0)
                    if session.finalize_pending:
                        session.finalize_pending = False
                        cp_file = run_dir / "commonplace.md"
                        if cp_file.is_file():
                            try:
                                result = _promote(run_dir, corpus_id, persist=persist)
                                await session.emit({
                                    "type": "finalized",
                                    "content": result["content"],
                                    "location": result["location"],
                                    "words": result["words"],
                                    "overwritten": result["overwritten"],
                                })
                            except Exception:
                                pass

            session = await create_session(
                run_id=run_rel,
                run_dir=run_dir,
                corpus_id=corpus_id,
                kind="commonplace",
                options=options,
                kickoff=kickoff,
                spawned_event=spawned_event,
                on_turn_complete=on_turn_complete,
                background_loop=_commonplace_watch,
                email=user_email,
            )
            tlog("session_start", kind="commonplace", email=user_email,
                 corpus=corpus_id, model=model, run_id=run_rel)
            await session.attach(ws)

        # Receive loop — keepalive + stop.
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                break
            mtype = msg.get("type")
            if mtype == "ping":
                await send({"type": "pong"})
            elif mtype == "stop":
                tlog("session_end", kind="commonplace", email=user_email,
                     corpus=corpus_id, reason="stop",
                     cost_usd=session.cumulative_cost if session else 0,
                     run_id=session.run_id if session else "")
                if session:
                    await session.stop()
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        await send({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        if session is not None:
            session.detach(ws)
        try:
            await ws.close()
        except Exception:
            pass
