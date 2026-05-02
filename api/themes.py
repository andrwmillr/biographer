"""Themes-curate flow.

`GET /notes/themes-top-n` returns the folder-aware top-N corpus sample
(input to the themes flow). Auth mirrors `/notes?era=…`: samples are
open, owned corpora require an X-Auth-Token.

`WS /themes-curate` is single-phase: a ClaudeSDKClient session whose
system prompt combines THEMES_R1.md + CURATE.md and whose kickoff
inlines the corpus sample with instructions to generate round-1 themes
inline, then transition to curate mode. Streaming is visible from the
first token. On `finalize`, the server sends `/lock` to the agent,
which writes themes.md; the file watcher emits `finalized` when that
write lands.

Tier 3 disconnect-resilience: the SDK lifecycle lives on a `Session`
(see core/session.py) registered by run_id, not on the WS. The WS is
just a transport — it attaches/detaches and the session keeps running.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from api.auth import _gc_auth, _load_auth
from claude_agent_sdk import ClaudeAgentOptions
from api.config import (
    CURATE_PATH,
    REPO,
    THEMES_R1_PATH,
)
from api.corpora import (
    _load_state,
    _note_source,
    _session_corpus_id,
    corpus_dir,
    is_sample_corpus,
    require_corpus_access,
)
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from core import corpus as wb
from core.session import Session, create_session, get_session
from core.telemetry import log as tlog

router = APIRouter()


def _themes_base(corpus_id: str | None = None) -> Path:
    return wb.corpus_root(corpus_id) / "claude" / "themes"


def _prepare_themes_run(top_n: int = 5, corpus_id: str | None = None) -> dict:
    """Build the round-1 corpus-themes input message and create a fresh
    themes run dir on disk. Mirrors spin_themes.py's build_input(top_n)
    and OUT_DIR layout.

    Returns {run_dir, run_rel, full_user_msg, top_n, in_chars}."""
    from core.sampling import build_input

    user_msg = build_input(top_n, corpus_id=corpus_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _themes_base(corpus_id) / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.md").write_text(user_msg, encoding="utf-8")
    return {
        "run_dir": run_dir,
        "run_rel": str(run_dir.relative_to(REPO)),
        "full_user_msg": user_msg,
        "top_n": top_n,
        "in_chars": len(user_msg),
    }


def _build_themes_kickoff(run_dir_abs: Path, corpus_sample: str, corpus_id: str | None) -> str:
    """Build a single-phase themes kickoff: generate round-1 themes
    inline in chat, then transition to curate orientation. Replaces the
    old two-phase flow (`claude -p` round-1 then curate)."""
    return (
        wb.subject_context_for(corpus_id)
        + "You're starting a fresh themes session. "
        "The corpus sample is inlined between INPUT-START / INPUT-END below — "
        "treat it as the entirety of your authorized source material.\n\n"
        "**First, generate round-1 themes** following the rules in your system "
        "prompt's round-1 section: 8-12 candidate themes, each with a short "
        "name, one-line gloss, era list, and 8-10 candidate notes. Use the "
        "OUTPUT FORMAT from the round-1 section. Stream as you go — don't "
        "summarize, don't wait.\n\n"
        f"**After generating all themes,** write them to {run_dir_abs}/themes.md "
        "using the Write tool in the LOCKING format from your system prompt. "
        "This populates the Draft pane for the user.\n\n"
        "**Then transition to curate mode:** end with the single line "
        '"Ready for your moves." Wait for the user.\n\n'
        f"On subsequent locks, overwrite {run_dir_abs}/themes.md with the "
        "final curated version. "
        "Don't list directories, don't read sibling files, don't browse "
        "anywhere else.\n\n"
        "--- INPUT-START ---\n\n"
        "# Corpus sample (your full context)\n\n"
        + corpus_sample
        + "\n\n--- INPUT-END ---\n"
    )


async def _themes_watch(session: Session) -> None:
    """Background loop: watch themes.md for changes, emit draft_update.
    When finalize_pending is set and themes.md changes, emit finalized
    and clear the flag."""
    p = session.run_dir / "themes.md"
    themes_relative = str(p.relative_to(REPO))
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
        await session.emit({"type": "draft_update", "kind": "themes", "content": content})
        if session.finalize_pending:
            session.finalize_pending = False
            await session.emit({
                "type": "finalized",
                "content": content,
                "location": themes_relative,
                "words": len(content.split()),
                "overwritten": False,
            })


@router.get("/themes/latest")
def get_latest_themes(session: str = Depends(require_corpus_access)):
    """Return the canonical themes.md for the corpus. Used by the
    workspace to populate the draft pane in read mode.

    Sample corpora: read from a fixed canonical path (themes/canonical.md)
    pre-populated when the sample is built. One visitor's /lock writes
    to their session run dir but never overwrites this canonical view.

    Owned corpora: read from the most recent run dir's themes.md, since
    the owner's iteration is canonical by definition."""
    corpus_id = _session_corpus_id(session)
    base = _themes_base(corpus_id)
    if is_sample_corpus(session):
        canonical = base / "canonical.md"
        if not canonical.exists():
            raise HTTPException(404, "no canonical themes for this sample yet")
        return {"content": canonical.read_text(encoding="utf-8")}
    if not base.exists():
        raise HTTPException(404, "no themes runs yet")
    runs = sorted(
        (p for p in base.iterdir() if p.is_dir() and p.name.startswith("run_")),
        key=lambda p: p.name,
        reverse=True,
    )
    for run in runs:
        themes = run / "themes.md"
        if themes.exists():
            return {"content": themes.read_text(encoding="utf-8")}
    raise HTTPException(404, "no locked themes yet")


@router.get("/notes/themes-top-n")
def list_themes_top_n_notes(n: int = 5, session: str = Depends(require_corpus_access)):
    """Folder-aware top-N sample fed to /themes-curate, flattened across
    eras and sorted chronologically. Same item shape as /notes?era=… so
    the UI can use one renderer. Default n=7 — 10 exceeds context."""
    from core.sampling import folder_aware_sample
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, eras = _load_state(corpus_id)
    sampled = []
    for era_name, _, _ in eras:
        era_notes = by_era.get(era_name, [])
        if era_notes:
            sampled.extend(folder_aware_sample(era_notes, n, corpus_id))
    sampled.sort(key=lambda x: x.get("date", ""))
    out = []
    for note in sampled:
        rel = note["rel"]
        item = {
            "rel": rel,
            "date": note.get("date", ""),
            "title": note.get("title", ""),
            "label": rel.split("/", 1)[0] if "/" in rel else "",
            "source": _note_source(rel, corpus_id),
            "body": note.get("body") or wb.parse_note_body(rel, corpus_id),
        }
        if note.get("editor_note"):
            item["editor_note"] = note["editor_note"]
        out.append(item)
    return out


@router.websocket("/themes-curate")
async def themes_curate(ws: WebSocket):
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

    # Auth + intent in the first message body (not the URL) so tokens
    # never appear in access logs / browser history / proxy logs.
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
    # Auth gate mirrors /session: samples are open to anonymous visitors so
    # the demo flow works without an account; non-samples require ownership.
    if not is_sample_corpus(session_slug):
        if not auth_token:
            await reject("auth required: missing token in start message")
            return
        state = _gc_auth(_load_auth())
        record = state["sessions"].get(auth_token)
        if not record:
            await reject("invalid or expired auth token")
            return
        if session_slug not in state["users"].get(record["email"], []):
            await reject("this corpus is not owned by the authenticated user")
            return
    corpus_id = _session_corpus_id(session_slug)
    user_email = record["email"] if not is_sample_corpus(session_slug) else "(sample)"

    session: Session | None = None
    try:
        # Attempt Tier 3 hot resume: client passed run_id of a session
        # that's still alive in the registry.
        resume_run_rel = first.get("run_id") if first.get("resume") else None
        if resume_run_rel:
            existing = get_session(resume_run_rel)
            if existing is not None and existing.kind == "themes" and existing.corpus_id == corpus_id:
                session = existing
                await session.attach(ws)

        if session is None:
            top_n = int(first.get("top_n") or 5)
            model_key = first.get("model")
            model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

            # Tier 2.5 cold resume: run_dir exists on disk but no live
            # session — rebuild kickoff from input.md / state.md.
            if resume_run_rel:
                from core.resume import build_themes_resume_kickoff
                run_dir = REPO / resume_run_rel
                if not run_dir.is_dir():
                    await reject(f"resume run_dir not found: {resume_run_rel}")
                    return
                run_rel = resume_run_rel
                full_user_msg = (run_dir / "input.md").read_text(encoding="utf-8") if (run_dir / "input.md").exists() else ""
                in_chars = len(full_user_msg)
                kickoff = build_themes_resume_kickoff(run_dir, corpus_id)
            else:
                prep = _prepare_themes_run(top_n=top_n, corpus_id=corpus_id)
                run_dir = prep["run_dir"]
                run_rel = prep["run_rel"]
                full_user_msg = prep["full_user_msg"]
                in_chars = prep["in_chars"]
                kickoff = _build_themes_kickoff(run_dir, full_user_msg, corpus_id)
            run_dir_abs = run_dir

            sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

            # Single-phase: round-1 generation + curate happen inside one SDK
            # session. System prompt combines both.
            themes_r1_prompt = THEMES_R1_PATH.read_text(encoding="utf-8")
            curate_prompt = CURATE_PATH.read_text(encoding="utf-8")
            combined_system = (
                themes_r1_prompt.rstrip("\n")
                + "\n\n---\n\n"
                + curate_prompt
            )

            runs_parent_abs = run_dir_abs.parent
            settings = {
                "permissions": {
                    "deny": [
                        f"Read({runs_parent_abs}/**)",
                        f"Edit({runs_parent_abs}/**)",
                        f"Write({runs_parent_abs}/**)",
                    ],
                    "allow": [
                        f"Read({run_dir_abs}/**)",
                        f"Edit({run_dir_abs}/**)",
                        f"Write({run_dir_abs}/**)",
                    ],
                }
            }
            settings_path = run_dir / ".claude-settings.json"
            settings_path.write_text(json.dumps(settings), encoding="utf-8")

            options = ClaudeAgentOptions(
                model=model,
                system_prompt=combined_system,
                permission_mode="acceptEdits",
                allowed_tools=["Read", "Edit", "Write"],
                settings=str(settings_path),
                cwd=str(run_dir_abs),
                include_partial_messages=True,
                env=sub_env,
            )

            spawned_event = {
                "type": "spawned",
                "model": model,
                "run_dir": run_rel,
                "top_n": top_n,
                "input_chars": in_chars,
                "resumed": bool(resume_run_rel),
            }

            # Persist the agent's last response to state.md after each turn
            # so a cold-path Tier 2.5 resume (server restart, GC) can rebuild
            # context. CURATE prompt opens every response with `## Current
            # state`, so this file always reflects the latest curation state.
            state_path = run_dir_abs / "state.md"

            async def on_turn_complete(text: str) -> None:
                try:
                    state_path.write_text(text, encoding="utf-8")
                except Exception:
                    pass

            session = await create_session(
                run_id=run_rel,
                run_dir=run_dir_abs,
                corpus_id=corpus_id,
                kind="themes",
                options=options,
                kickoff=kickoff,
                spawned_event=spawned_event,
                on_turn_complete=on_turn_complete,
                background_loop=_themes_watch,
                email=user_email,
            )
            tlog("session_start", kind="themes", email=user_email,
                 corpus=corpus_id, model=model,
                 resumed=bool(resume_run_rel))
            await session.attach(ws)

        # Receive loop. Session owns the SDK; this loop just relays
        # client messages to it. Detach (not stop) on disconnect so the
        # session keeps running for a reattach.
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                break
            mtype = msg.get("type")
            if mtype == "ping":
                await send({"type": "pong"})
                continue
            if mtype == "stop":
                tlog("session_end", kind="themes", email=user_email,
                     corpus=corpus_id, reason="stop",
                     cost_usd=session.cumulative_cost)
                await session.stop()
                break
            if mtype == "reply":
                text = (msg.get("text") or "").strip()
                if text:
                    await session.query(text)
            elif mtype == "finalize":
                session.finalize_pending = True
                tlog("session_end", kind="themes", email=user_email,
                     corpus=corpus_id, reason="finalized",
                     cost_usd=session.cumulative_cost)
                await session.query("/lock")
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
