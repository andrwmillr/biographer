"""Chapter discovery: propose chapter boundaries from note metadata via
Claude, save validated chapter lists to eras.yaml, and expose note-month
data for client-side count recomputation.

Replaces the old manual `POST /import/eras` YAML-upload step with an
intelligent discovery flow: upload notes → Claude proposes chapters →
user reviews/edits → confirm.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

import yaml
from api.corpora import (
    _session_corpus_id,
    corpus_dir,
    require_writable,
)
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import corpus as wb

router = APIRouter()

# ---- Validation ----

_YM_RE = re.compile(r"^\d{4}-\d{2}$")


def _validate_chapters(chapters: list[dict]) -> list[dict]:
    if not isinstance(chapters, list) or len(chapters) == 0:
        raise HTTPException(400, "at least one chapter is required")
    names_seen: set[str] = set()
    for i, ch in enumerate(chapters):
        name = (ch.get("name") or "").strip()
        if not name:
            raise HTTPException(400, f"chapter #{i + 1} missing 'name'")
        if name in names_seen:
            raise HTTPException(400, f"duplicate chapter name: {name}")
        names_seen.add(name)
        start = (ch.get("start") or "").strip()
        if not start:
            raise HTTPException(400, f"chapter #{i + 1} missing 'start'")
        if start != "0000-00" and not _YM_RE.fullmatch(start):
            raise HTTPException(400, f"chapter #{i + 1} has invalid start: {start}")
    # Sort by start, build eras.yaml-compatible list (name + start + end).
    sorted_chs = sorted(chapters, key=lambda c: c["start"])
    out = []
    for idx, ch in enumerate(sorted_chs):
        entry: dict = {"name": ch["name"].strip(), "start": ch["start"].strip()}
        if idx + 1 < len(sorted_chs):
            # End one month before next chapter's start? No — eras.yaml
            # uses inclusive ranges and load_eras derives end from next
            # start. We just omit end and let the loader figure it out.
            pass
        out.append(entry)
    return out


# ---- PUT /chapters/save ----

class SaveChaptersRequest(BaseModel):
    chapters: list[dict]


@router.put("/chapters/save")
def save_chapters(
    req: SaveChaptersRequest,
    session: str = Depends(require_writable),
):
    """Validate and write chapters to eras.yaml. Works for both initial
    import and post-import editing."""
    cdir = corpus_dir(session)
    validated = _validate_chapters(req.chapters)
    cfg_dir = cdir / "_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "eras.yaml").write_text(
        yaml.dump(validated, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return {"ok": True, "era_count": len(validated)}


# ---- GET /chapters/note-months ----

@router.get("/chapters/note-months")
def note_months(session: str = Depends(require_writable)):
    """Return a sorted list of every note's YYYY-MM. The frontend uses this
    to recompute per-chapter note counts client-side as boundaries change."""
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    months: list[str] = []
    for n in notes:
        d = n.get("date", "")
        if d and len(d) >= 7:
            months.append(d[:7])
    months.sort()
    return {"months": months}


# ---- POST /chapters/propose ----

PROPOSE_SYSTEM = """\
You are analyzing a personal journal or diary corpus to propose meaningful \
chapter boundaries. Chapters should reflect life phases — moves, jobs, \
relationships, projects, seasons of writing — not arbitrary date splits. \
Prefer 4–12 chapters for a typical corpus.

Each chapter needs a short evocative name (not just a date range) and a \
start month (YYYY-MM format). The first chapter's start should be the \
earliest month with notes, or "0000-00" if you want a catch-all for \
undated/early notes.

Respond with ONLY a JSON array of {"name": "...", "start": "YYYY-MM"} \
objects sorted chronologically. No explanation, no markdown fences, no \
trailing text."""

EXCERPT_CHARS_PER_NOTE = 200
MAX_EXCERPTS_PER_MONTH = 3
MAX_OVERVIEW_CHARS = 120_000  # leave room for system prompt + response


def _build_corpus_overview(corpus_id: str) -> tuple[str, list[str]]:
    """Build a condensed overview of all notes for Claude to analyze.
    Returns (overview_text, sorted_note_months)."""
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    notes = [n for n in notes if n.get("date") and len(n["date"]) >= 7]
    notes.sort(key=lambda n: n["date"])

    if not notes:
        return "No dated notes found.", []

    note_months = [n["date"][:7] for n in notes]

    # Group notes by month
    by_month: dict[str, list[dict]] = defaultdict(list)
    for n in notes:
        by_month[n["date"][:7]].append(n)

    # Label distribution across time
    label_ranges: dict[str, list[str]] = defaultdict(list)
    for n in notes:
        rel = n.get("rel", "")
        label = rel.split("/", 1)[0] if "/" in rel else ""
        if label:
            label_ranges[label].append(n["date"][:7])

    # Build monthly summaries
    lines = [
        f"# Corpus: {len(notes)} notes spanning "
        f"{note_months[0]} to {note_months[-1]}",
        "",
        "## Monthly summary (chronological)",
        "",
    ]

    total_chars = sum(len(line) for line in lines)

    for ym in sorted(by_month.keys()):
        month_notes = by_month[ym]
        labels = Counter(
            (n.get("rel", "").split("/", 1)[0] if "/" in n.get("rel", "") else "")
            for n in month_notes
        )
        labels.pop("", None)
        label_str = ", ".join(
            f"{lbl}x{cnt}" if cnt > 1 else lbl
            for lbl, cnt in labels.most_common()
        )

        header = f"### {ym} ({len(month_notes)} note{'s' if len(month_notes) != 1 else ''}"
        if label_str:
            header += f", labels: {label_str}"
        header += ")"
        lines.append(header)

        # Titles
        for n in month_notes[:10]:
            title = n.get("title", "").strip()
            lines.append(f"- {title if title else '(untitled)'}")
        if len(month_notes) > 10:
            lines.append(f"- ... and {len(month_notes) - 10} more")

        # Excerpts from a few representative notes
        excerpted = 0
        for n in month_notes:
            if excerpted >= MAX_EXCERPTS_PER_MONTH:
                break
            if total_chars > MAX_OVERVIEW_CHARS:
                break
            body = wb.parse_note_body(n["rel"], corpus_id)
            if not body:
                continue
            excerpt = body[:EXCERPT_CHARS_PER_NOTE].strip()
            if len(body) > EXCERPT_CHARS_PER_NOTE:
                excerpt += "..."
            lines.append(f'Excerpt: "{excerpt}"')
            total_chars += len(excerpt) + 20
            excerpted += 1

        lines.append("")

    # Label distribution summary
    if label_ranges:
        lines.append("## Label distribution across time")
        for lbl, months_list in sorted(
            label_ranges.items(), key=lambda x: -len(x[1])
        ):
            lo, hi = months_list[0], months_list[-1]
            lines.append(f"- {lbl}: {len(months_list)} notes ({lo} – {hi})")

    return "\n".join(lines), sorted(note_months)


@router.post("/chapters/propose")
async def propose_chapters(session: str = Depends(require_writable)):
    """Analyze uploaded notes and propose chapter boundaries via Claude.
    Returns SSE stream with progress events and the final result."""
    import anthropic

    corpus_id = _session_corpus_id(session)
    cdir = corpus_dir(session)

    async def generate():
        # Phase 1: load notes and build overview
        yield _sse("progress", {"status": "loading_notes"})
        try:
            overview, note_months = _build_corpus_overview(corpus_id)
        except Exception as e:
            yield _sse("error", {"message": f"failed to load notes: {e}"})
            return

        if not note_months:
            yield _sse("error", {"message": "no dated notes found"})
            return

        yield _sse("progress", {
            "status": "analyzing",
            "note_count": len(note_months),
        })

        # Phase 2: call Claude
        subject_ctx = wb.subject_context_for(corpus_id)
        user_msg = f"{subject_ctx}\n\n{overview}"

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=PROPOSE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except Exception as e:
            yield _sse("error", {"message": f"Claude API error: {e}"})
            return

        # Phase 3: parse response
        try:
            # Strip markdown fences if model included them anyway
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            proposed = json.loads(cleaned)
            if not isinstance(proposed, list):
                raise ValueError("expected a JSON array")
        except (json.JSONDecodeError, ValueError) as e:
            yield _sse("error", {
                "message": f"failed to parse Claude's response: {e}",
                "raw": raw,
            })
            return

        # Compute note counts for each proposed chapter
        proposed.sort(key=lambda c: c.get("start", ""))
        for idx, ch in enumerate(proposed):
            start = ch.get("start", "0000-00")
            end = (
                proposed[idx + 1]["start"]
                if idx + 1 < len(proposed)
                else "9999-99"
            )
            ch["note_count"] = sum(
                1 for m in note_months if start <= m < end
            )

        yield _sse("result", {
            "chapters": proposed,
            "note_months": note_months,
        })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
