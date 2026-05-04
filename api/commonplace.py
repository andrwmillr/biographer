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

# Char budget per run — ~100 notes per batch.
CHAR_CAP = 150_000


def _commonplace_base(corpus_id: str | None = None) -> Path:
    return wb.corpus_root(corpus_id) / "claude" / "commonplace"


def _seen_path(corpus_id: str | None = None) -> Path:
    return _commonplace_base(corpus_id) / "seen.json"


def _load_seen(corpus_id: str | None = None) -> set[str]:
    p = _seen_path(corpus_id)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return set()


def _save_seen(seen: set[str], corpus_id: str | None = None) -> None:
    p = _seen_path(corpus_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    tmp.replace(p)


def _norm_quotes(s: str) -> str:
    return (s.replace("‘", "'").replace("’", "'").replace("′", "'")
             .replace("“", '"').replace("”", '"').replace("″", '"')
             .replace("–", "-").replace("—", "-"))


def _remove_passage(date: str, title: str, body: str,
                    corpus_id: str | None = None) -> bool:
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
                 corpus_id: str | None = None) -> None:
    """Append a passage block to canonical.md."""
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    existing = canonical.read_text(encoding="utf-8") if canonical.exists() else ""
    if existing and not existing.endswith("\n\n"):
        existing = existing.rstrip("\n") + "\n\n"
    block = f"### [{date}] · {era} · {title}\n\n{body.strip()}\n"
    canonical.write_text(existing + block, encoding="utf-8")


def _count_eligible(corpus_id: str | None = None) -> int:
    """Count high-signal notes in the corpus."""
    notes = wb.load_corpus_notes(corpus_id)
    count = 0
    for n in notes:
        label = n["rel"].split("/", 1)[0] if "/" in n["rel"] else "_"
        if label in HIGH_SIGNAL_LABELS:
            count += 1
    return count


def _prepare_run(corpus_id: str | None = None) -> dict:
    """Build the commonplace input from unseen notes and create a run dir."""
    from core.sampling import build_input

    seen = _load_seen(corpus_id)
    user_msg, sampled_rels = build_input(
        top_n=0,
        corpus_id=corpus_id,
        char_cap=CHAR_CAP,
        label_filter=HIGH_SIGNAL_LABELS,
        exclude_rels=seen,
        shuffle=True,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _commonplace_base(corpus_id) / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.md").write_text(user_msg, encoding="utf-8")
    (run_dir / "sampled_rels.json").write_text(
        json.dumps(sampled_rels), encoding="utf-8"
    )
    return {
        "run_dir": run_dir,
        "run_rel": str(run_dir.relative_to(REPO)),
        "full_user_msg": user_msg,
        "in_chars": len(user_msg),
        "sampled_count": len(sampled_rels),
        "seen_before": len(seen),
        "total_eligible": _count_eligible(corpus_id),
    }


def _build_kickoff(run_dir: Path, corpus_sample: str, corpus_id: str | None) -> str:
    return (
        wb.subject_context_for(corpus_id)
        + "You're building a commonplace book — extracting the best passages "
        "from this person's notes archive. The notes are inlined between "
        "INPUT-START / INPUT-END below.\n\n"
        "Read through every note. For each note that has something worth "
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


def _promote_empty(run_dir: Path, corpus_id: str | None,
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


def _promote(run_dir: Path, corpus_id: str | None,
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


# ---- REST endpoints ----

@router.get("/commonplace/latest")
def get_latest(session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    canonical = _commonplace_base(corpus_id) / "canonical.md"
    if not canonical.exists():
        raise HTTPException(404, "no commonplace book yet")
    return {"content": canonical.read_text(encoding="utf-8")}


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


@router.post("/commonplace/passage")
def add_passage(
    date: str = Query("", description="YYYY-MM-DD"),
    era: str = Query(""),
    title: str = Query(""),
    body: str = Query(""),
    session: str = Depends(require_corpus_access),
):
    """Add a user-highlighted passage to the commonplace book."""
    corpus_id = _session_corpus_id(session)
    if not body.strip():
        raise HTTPException(400, "empty body")
    _add_passage(date, era, title, body, corpus_id)
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

            kickoff = _build_kickoff(run_dir, prep["full_user_msg"], corpus_id)
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
                "input_chars": prep["in_chars"],
                "sampled_count": prep["sampled_count"],
                "seen_before": prep["seen_before"],
                "total_eligible": prep["total_eligible"],
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
