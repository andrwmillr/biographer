"""Per-era chapter drafting.

`POST /draft` is a one-shot SSE stream; `WS /session` is the bidirectional
chat used by the unified workspace. The WS handler attaches the client to
a `Session` (see core/session.py) which owns the SDK lifecycle, file
watcher, and event log. On `finalize`, the server promotes the run's
output.md to chapters/<era_slug>.md (skipping promotion for samples).

Tier 3: the SDK lives on the Session, so a tab close / disconnect just
detaches the WS — the agent keeps running and reattaches via run_id.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from api.auth import _gc_auth, _load_auth
from claude_agent_sdk import ClaudeAgentOptions
from api.config import KICKOFF_PATH, REPO
from api.corpora import (
    _load_state,
    _session_corpus_id,
    corpus_dir,
    is_sample_corpus,
    require_corpus_access,
    require_writable,
)
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import corpus as wb
from core.session import Session, create_session, get_session
from core.telemetry import log as tlog

router = APIRouter()


@router.get("/session/active")
def session_active(
    run_id: str,
    session: str = Depends(require_corpus_access),
):
    """Cheap liveness check used by the workspace mount effect to decide
    whether to auto-resume. Returns active=True only if the session is in
    the in-memory registry AND owned by the requesting corpus, so we
    don't auto-spin-up a cold-resume SDK from a stale localStorage runId
    every time the page is opened, and don't leak existence of another
    corpus's session."""
    sess = get_session(run_id)
    if sess is None or sess.corpus_id != _session_corpus_id(session):
        return {"active": False}
    return {"active": True}


class DraftRequest(BaseModel):
    era: str
    future: bool = False


def _prepare_run(era_name: str, corpus_id: str = "andrew", include_future: bool = False) -> dict:
    """Build the prompt inputs and create a fresh run dir on disk.
    Returns {run_dir, run_rel, full_user_msg, notes_count, prior_count,
    digest_count, future_count, future_digest_count, in_chars}."""
    _, by_era, _ = _load_state(corpus_id)
    if era_name not in by_era:
        raise HTTPException(404, f"unknown era: {era_name}")
    notes = by_era[era_name]
    if not notes:
        raise HTTPException(400, f"era has no notes: {era_name}")
    prior = wb.load_prior_chapters(era_name, corpus_id)
    prior_blocks = [f"## {wb.era_heading(n, by_era[n])}\n\n{t}" for n, t in prior]
    prior_digests = wb.load_prior_thread_digests(era_name, corpus_id)
    digest_blocks = [f"## {wb.era_heading(n, by_era[n])}\n\n{d}" for n, d in prior_digests]
    future_blocks = []
    future_digest_blocks = []
    if include_future:
        future = wb.load_future_chapters(era_name, corpus_id)
        future_blocks = [f"## {wb.era_heading(n, by_era[n])}\n\n{t}" for n, t in future]
        future_d = wb.load_future_thread_digests(era_name, corpus_id)
        future_digest_blocks = [f"## {wb.era_heading(n, by_era[n])}\n\n{d}" for n, d in future_d]
    era_msg = wb.build_user_msg(era_name, notes, corpus_id=corpus_id)

    parts = []
    if prior_blocks:
        parts.append(
            "--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n"
        )
        for ch in prior_blocks:
            parts.append(ch + "\n\n")
        parts.append("--- END PRIOR CHAPTERS ---\n\n")
    if digest_blocks:
        parts.append(
            "--- PRIOR THREAD DIGESTS (structured per-era state — read alongside the prior chapters) ---\n\n"
        )
        for d in digest_blocks:
            parts.append(d + "\n\n")
        parts.append("--- END PRIOR THREAD DIGESTS ---\n\n")
    if future_blocks:
        parts.append(
            "--- FUTURE CHAPTERS (later eras, drafted in a previous run — for thematic alignment, NOT for events that haven't happened yet in this era; do not foreshadow or anticipate) ---\n\n"
        )
        for ch in future_blocks:
            parts.append(ch + "\n\n")
        parts.append("--- END FUTURE CHAPTERS ---\n\n")
    if future_digest_blocks:
        parts.append(
            "--- FUTURE THREAD DIGESTS (later eras' digests — same caveat: hindsight context, not events to anticipate) ---\n\n"
        )
        for d in future_digest_blocks:
            parts.append(d + "\n\n")
        parts.append("--- END FUTURE THREAD DIGESTS ---\n\n")
    themes_text = wb.load_canonical_themes(corpus_id)
    if themes_text:
        parts.append(
            "--- CORPUS THEMES (locked corpus-level themes — anchor against these for continuity; the era may also surface threads not listed here) ---\n\n"
        )
        parts.append(themes_text.rstrip("\n") + "\n\n")
        parts.append("--- END CORPUS THEMES ---\n\n")
    parts.append(era_msg)
    full_user_msg = "".join(parts)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    slug = wb.era_slug(era_name)
    run_dir = wb.biographies_dir(corpus_id) / "_dump" / slug / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "system.md").write_text(wb.CHAPTER_SYSTEM, encoding="utf-8")
    (run_dir / "user.md").write_text(full_user_msg, encoding="utf-8")
    return {
        "run_dir": run_dir,
        "run_rel": str(run_dir.relative_to(REPO)),
        "full_user_msg": full_user_msg,
        "notes_count": len(notes),
        "prior_count": len(prior_blocks),
        "digest_count": len(digest_blocks),
        "future_count": len(future_blocks),
        "future_digest_count": len(future_digest_blocks),
        "in_chars": len(full_user_msg),
    }


def _promote_era_chapter(run_dir: Path, era_name: str, corpus_id: str) -> dict:
    """Copy run_dir/output.md → chapters/<era_slug>.md. Used by the
    /session WS finalize handler. Raises ValueError on path/era mismatch
    (caller maps to a WS error message). Run dir is verified to live
    under this corpus's biographies/_dump/ tree, and the era slug derived
    from the run_dir path must match the requested era — preventing
    accidental cross-era overwrites if the WS state ever drifts."""
    bio_root = (wb.biographies_dir(corpus_id) / "_dump").resolve()
    rd = run_dir.resolve()
    try:
        rel = rd.relative_to(bio_root)
    except ValueError:
        raise ValueError("run_dir must be under biographies/_dump/")
    parts = rel.parts
    if len(parts) < 3 or parts[1] != "runs":
        raise ValueError(f"unexpected run_dir layout: {rd}")
    slug = parts[0]
    expected_slug = wb.era_slug(era_name)
    if slug != expected_slug:
        raise ValueError(
            f"run_dir era ({slug}) does not match selected era ({expected_slug})"
        )
    src = rd / "output.md"
    if not src.is_file():
        raise ValueError(f"no output.md in {rd}")
    dst = wb.chapters_dir(corpus_id) / f"{slug}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    overwritten = dst.exists()
    content = src.read_text(encoding="utf-8")
    dst.write_text(content, encoding="utf-8")
    digest_src = rd / "threads.md"
    if digest_src.is_file():
        digest_dst = wb.threads_dir(corpus_id) / f"{slug}.md"
        digest_dst.parent.mkdir(parents=True, exist_ok=True)
        digest_dst.write_text(digest_src.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "content": content,
        "location": str(dst.relative_to(REPO)),
        "words": len(content.split()),
        "overwritten": overwritten,
    }


def _build_kickoff(run_dir_abs: Path, user_msg: str, corpus_id: str | None) -> str:
    """Read KICKOFF.md, substitute __RUN_DIR__, strip checkpoint markers,
    prepend the per-corpus subject identity block, and append the era inputs
    between INPUT-START / INPUT-END. Mirrors run.sh."""
    kickoff = KICKOFF_PATH.read_text(encoding="utf-8")
    kickoff = kickoff.replace("__RUN_DIR__", str(run_dir_abs))
    kickoff = kickoff.replace("<!-- CHECKPOINTS:START -->\n", "").replace(
        "<!-- CHECKPOINTS:END -->\n", ""
    )
    return (
        wb.subject_context_for(corpus_id)
        + kickoff.rstrip("\n")
        + "\n\n--- INPUT-START ---\n\n"
        + user_msg
        + "\n\n--- INPUT-END ---\n"
    )


async def _era_watch(session: Session) -> None:
    """Background loop: watch output.md / thinking.md / threads.md for
    changes during a draft run and emit draft_update events. Era finalize
    is server-side (chapter promote), not file-watch driven."""
    paths = {
        "output": session.run_dir / "output.md",
        "thinking": session.run_dir / "thinking.md",
        "threads": session.run_dir / "threads.md",
    }
    mtimes: dict[str, float] = {}
    while True:
        await asyncio.sleep(0.5)
        for kind, p in paths.items():
            try:
                m = p.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtimes.get(kind) == m:
                continue
            mtimes[kind] = m
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                continue
            await session.emit({"type": "draft_update", "kind": kind, "content": content})


@router.post("/draft")
async def draft(req: DraftRequest, session: str = Depends(require_writable)):
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    inputs = _prepare_run(req.era, corpus_id=corpus_id, include_future=req.future)
    notes_count = inputs["notes_count"]
    prior_count = inputs["prior_count"]
    future_count = inputs["future_count"]
    full_user_msg = inputs["full_user_msg"]
    in_chars = inputs["in_chars"]
    run_dir = inputs["run_dir"]
    run_rel = inputs["run_rel"]

    async def gen():
        def sse(obj):
            return f"data: {json.dumps(obj)}\n\n"

        yield sse({
            "type": "start",
            "era": req.era,
            "notes": notes_count,
            "prior_chapters": prior_count,
            "future_chapters": future_count,
            "input_chars": in_chars,
            "model": wb.MODEL,
            "run_dir": run_rel,
        })

        chunks: list[str] = []
        result_evt: dict | None = None
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "--model", wb.MODEL,
                "--system-prompt", wb.CHAPTER_SYSTEM,
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--no-session-persistence",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdin is not None and proc.stdout is not None

            stdin_msg = wb.subject_context_for(corpus_id) + full_user_msg

            async def feed_stdin():
                proc.stdin.write(stdin_msg.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

            stdin_task = asyncio.create_task(feed_stdin())

            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "stream_event":
                    inner = evt.get("event", {})
                    itype = inner.get("type")
                    if itype == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                chunks.append(text)
                                yield sse({"type": "delta", "text": text})
                    elif itype == "message_start":
                        yield sse({"type": "status", "status": "generating"})
                elif t == "system":
                    sub = evt.get("subtype")
                    if sub == "status":
                        yield sse({"type": "status", "status": evt.get("status", "")})
                    elif sub == "init":
                        yield sse({"type": "status", "status": "spawned"})
                elif t == "result":
                    result_evt = evt

            await stdin_task
            rc = await proc.wait()

            (run_dir / "output.md").write_text("".join(chunks), encoding="utf-8")

            if rc != 0 and not result_evt:
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
                yield sse({"type": "error", "message": f"claude exited {rc}: {stderr[-500:]}", "run_dir": run_rel})
                return

            usage = (result_evt or {}).get("usage", {}) if result_evt else {}
            yield sse({
                "type": "done",
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
                "stop_reason": (result_evt or {}).get("stop_reason", ""),
                "cost_usd": (result_evt or {}).get("total_cost_usd", 0),
                "run_dir": run_rel,
            })
        except Exception as e:
            if chunks:
                (run_dir / "output.partial.md").write_text(
                    "".join(chunks), encoding="utf-8"
                )
            yield sse({"type": "error", "message": str(e), "run_dir": run_rel})
        finally:
            # Reap on any exit path (success, exception, or generator close
            # from a client disconnect). Without this, the `claude -p`
            # subprocess outlives the handler and accumulates as orphans.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.websocket("/session")
async def session(ws: WebSocket):
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
    if first.get("type") != "start" or not first.get("era"):
        await reject("first message must be {type:'start', era, session, token}")
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
    # Auth gate: drafting requires an auth token whose user owns the slug.
    # Samples are open to anonymous visitors so demos work without an account
    # — note that drafts spawn a Claude subprocess on the host's dime and
    # writes (run dirs, finalize) mutate the sample's own corpus tree.
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

    sess: Session | None = None
    era = first["era"]
    try:
        # Tier 3 hot resume: client passed a run_id that's still alive.
        resume_run_rel = first.get("run_id") if first.get("resume") else None
        if resume_run_rel:
            existing = get_session(resume_run_rel)
            if existing is not None and existing.kind == "era" and existing.corpus_id == corpus_id:
                sess = existing
                await sess.attach(ws)

        if sess is None:
            model_key = first.get("model")
            model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

            # Tier 2.5 cold resume: rebuild kickoff from disk artifacts.
            if resume_run_rel:
                from core.resume import build_era_resume_kickoff
                run_dir = REPO / resume_run_rel
                if not run_dir.is_dir():
                    await reject(f"resume run_dir not found: {resume_run_rel}")
                    return
                run_dir_abs = run_dir.resolve()
                kickoff = build_era_resume_kickoff(run_dir_abs, corpus_id)
                # Cold resume doesn't recompute prior/future context (the
                # original kickoff text already in user.md captures it),
                # but we still want notes_count for the chat header so
                # the replayed spawned event doesn't say "reading 0 notes"
                # forever after.
                _, by_era, _ = _load_state(corpus_id)
                inputs = {
                    "run_rel": resume_run_rel,
                    "notes_count": len(by_era.get(era, [])),
                    "prior_count": 0,
                    "digest_count": 0,
                    "future_count": 0,
                    "future_digest_count": 0,
                    "in_chars": len(kickoff),
                }
            else:
                try:
                    inputs = _prepare_run(era, corpus_id=corpus_id, include_future=bool(first.get("future")))
                except HTTPException as e:
                    await reject(e.detail)
                    return
                run_dir = inputs["run_dir"]
                run_dir_abs = run_dir.resolve()
                kickoff = _build_kickoff(run_dir_abs, inputs["full_user_msg"], corpus_id)

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

            sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            options = ClaudeAgentOptions(
                model=model,
                system_prompt=wb.CHAPTER_SYSTEM,
                permission_mode="acceptEdits",
                allowed_tools=["Read", "Edit", "Write", "TodoWrite"],
                settings=str(settings_path),
                cwd=str(run_dir_abs),
                include_partial_messages=True,
                env=sub_env,
            )

            spawned_event = {
                "type": "spawned",
                "era": era,
                "model": model,
                "run_dir": inputs["run_rel"],
                "notes": inputs["notes_count"],
                "prior_chapters": inputs["prior_count"],
                "prior_digests": inputs["digest_count"],
                "future_chapters": inputs["future_count"],
                "future_digests": inputs["future_digest_count"],
                "input_chars": inputs["in_chars"],
                "resumed": bool(resume_run_rel),
            }

            sess = await create_session(
                run_id=inputs["run_rel"],
                run_dir=run_dir_abs,
                corpus_id=corpus_id,
                kind="era",
                options=options,
                kickoff=kickoff,
                spawned_event=spawned_event,
                background_loop=_era_watch,
                email=user_email,
                era=era,
            )
            tlog("session_start", kind="era", email=user_email,
                 corpus=corpus_id, era=era, model=model,
                 resumed=bool(resume_run_rel),
                 notes=inputs["notes_count"])
            await sess.attach(ws)

        # Receive loop. Session owns the SDK.
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
                tlog("session_end", kind="era", email=user_email,
                     corpus=corpus_id, era=era, reason="stop",
                     cost_usd=sess.cumulative_cost)
                await sess.stop()
                break
            if mtype == "reply":
                text = (msg.get("text") or "").strip()
                if text:
                    await sess.query(text)
            elif mtype == "finalize":
                # Wait for any in-flight write so we don't promote a
                # half-streamed output.md.
                await sess.wait_idle()
                if is_sample_corpus(session_slug):
                    # Samples: lock the draft for this visitor's session
                    # but don't promote — one visitor's finalize shouldn't
                    # overwrite the corpus's canonical baseline for
                    # everyone else.
                    output_md = sess.run_dir / "output.md"
                    content = (
                        output_md.read_text(encoding="utf-8")
                        if output_md.exists() else ""
                    )
                    await sess.emit({
                        "type": "finalized",
                        "content": content,
                        "location": str(output_md.relative_to(REPO)),
                        "words": len(content.split()),
                        "overwritten": False,
                    })
                    continue
                try:
                    promoted = _promote_era_chapter(sess.run_dir, era, corpus_id)
                except ValueError as exc:
                    await send({"type": "error", "message": str(exc)})
                    continue
                await sess.emit({
                    "type": "finalized",
                    "content": promoted["content"],
                    "location": promoted["location"],
                    "words": promoted["words"],
                    "overwritten": promoted["overwritten"],
                })
                tlog("session_end", kind="era", email=user_email,
                     corpus=corpus_id, era=era, reason="finalized",
                     cost_usd=sess.cumulative_cost,
                     words=promoted["words"])
    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        await send({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        if sess is not None:
            sess.detach(ws)
        try:
            await ws.close()
        except Exception:
            pass
