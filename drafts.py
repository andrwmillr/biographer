"""Per-era chapter drafting.

`POST /draft` is a one-shot SSE stream; `WS /session` is the bidirectional
chat used by the unified workspace. The WS handler spawns a
ClaudeSDKClient with permissions scoped to a fresh run dir, watches
output.md / thinking.md / threads.md for changes (streamed as
`draft_update` events), and on `finalize` promotes the run's output.md
to chapters/<era_slug>.md.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from auth import _gc_auth, _load_auth
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent
from config import KICKOFF_PATH, REPO
from corpora import (
    _load_state,
    _session_corpus_id,
    corpus_dir,
    is_sample_corpus,
    require_writable,
)
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import corpus as wb

router = APIRouter()


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
    return {
        "content": content,
        "location": str(dst.relative_to(REPO)),
        "words": len(content.split()),
        "overwritten": overwritten,
    }


def _build_kickoff(run_dir_abs: Path, user_msg: str) -> str:
    """Read KICKOFF.md, substitute __RUN_DIR__, strip checkpoint markers,
    append the era inputs between INPUT-START / INPUT-END. Mirrors run.sh."""
    kickoff = KICKOFF_PATH.read_text(encoding="utf-8")
    kickoff = kickoff.replace("__RUN_DIR__", str(run_dir_abs))
    kickoff = kickoff.replace("<!-- CHECKPOINTS:START -->\n", "").replace(
        "<!-- CHECKPOINTS:END -->\n", ""
    )
    return (
        kickoff.rstrip("\n")
        + "\n\n--- INPUT-START ---\n\n"
        + user_msg
        + "\n\n--- INPUT-END ---\n"
    )


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

            async def feed_stdin():
                proc.stdin.write(full_user_msg.encode("utf-8"))
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
async def session(
    ws: WebSocket,
    session: str | None = None,
    auth: str | None = None,
):
    await ws.accept()
    tasks: list[asyncio.Task] = []
    run_dir: Path | None = None
    cumulative_cost = 0.0

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

    if not session:
        await reject("missing ?session= query param")
        return
    try:
        corpus_dir(session)
    except HTTPException as e:
        await reject(e.detail)
        return
    # Auth gate: drafting requires an auth token whose user owns the slug.
    # Samples are open to anonymous visitors so demos work without an account
    # — note that drafts spawn a Claude subprocess on the host's dime and
    # writes (run dirs, finalize) mutate the sample's own corpus tree.
    if not is_sample_corpus(session):
        if not auth:
            await reject("auth required: missing ?auth= query param")
            return
        state = _gc_auth(_load_auth())
        record = state["sessions"].get(auth)
        if not record:
            await reject("invalid or expired auth token")
            return
        if session not in state["users"].get(record["email"], []):
            await reject("this corpus is not owned by the authenticated user")
            return
    corpus_id = _session_corpus_id(session)

    try:
        first = await ws.receive_json()
        if first.get("type") != "start" or not first.get("era"):
            await send({"type": "error", "message": "first message must be {type:'start', era}"})
            return

        era = first["era"]
        try:
            inputs = _prepare_run(era, corpus_id=corpus_id, include_future=bool(first.get("future")))
        except HTTPException as e:
            await send({"type": "error", "message": e.detail})
            return

        model_key = first.get("model")
        model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

        run_dir = inputs["run_dir"]
        run_dir_abs = run_dir.resolve()
        kickoff = _build_kickoff(run_dir_abs, inputs["full_user_msg"])

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

        await send({
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
        })

        async def watch_files():
            paths = {
                "output": run_dir / "output.md",
                "thinking": run_dir / "thinking.md",
                "threads": run_dir / "threads.md",
            }
            mtimes: dict[str, float] = {}
            while True:
                await asyncio.sleep(0.5)
                for kind, p in paths.items():
                    try:
                        m = p.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if mtimes.get(kind) != m:
                        mtimes[kind] = m
                        try:
                            content = p.read_text(encoding="utf-8")
                        except Exception:
                            continue
                        await send({"type": "draft_update", "kind": kind, "content": content})

        watch_task = asyncio.create_task(watch_files())
        tasks = [watch_task]

        loop = asyncio.get_running_loop()

        async def stderr_cb(line: str):
            line = (line or "").strip()
            if not line:
                return
            await send({"type": "log", "text": line})

        def stderr_sync(line: str):
            asyncio.run_coroutine_threadsafe(stderr_cb(line), loop)

        sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=wb.CHAPTER_SYSTEM,
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Edit", "Write", "TodoWrite"],
            settings=str(settings_path),
            cwd=str(run_dir_abs),
            include_partial_messages=True,
            stderr=stderr_sync,
            env=sub_env,
        )

        async with ClaudeSDKClient(options=options) as client:
            async def drain_turn():
                nonlocal cumulative_cost
                await send({"type": "status", "status": "generating"})
                narration_chars = 0

                async def heartbeat():
                    start = asyncio.get_running_loop().time()
                    while True:
                        await asyncio.sleep(30)
                        elapsed = int(asyncio.get_running_loop().time() - start)
                        await send({
                            "type": "log",
                            "text": (
                                f"… still working ({elapsed}s elapsed, "
                                f"{narration_chars} chars streamed)"
                            ),
                        })

                hb_task = asyncio.create_task(heartbeat())
                try:
                    async for msg in client.receive_response():
                        if isinstance(msg, StreamEvent):
                            event = msg.event if hasattr(msg, "event") else {}
                            etype = event.get("type")
                            if etype == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    text = delta.get("text", "")
                                    if text:
                                        narration_chars += len(text)
                                        await send({"type": "narration", "text": text})
                        elif isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, ToolUseBlock):
                                    await send({
                                        "type": "tool_use",
                                        "id": block.id,
                                        "name": block.name,
                                        "input": block.input or {},
                                    })
                        elif isinstance(msg, UserMessage):
                            for block in msg.content if isinstance(msg.content, list) else []:
                                if isinstance(block, ToolResultBlock):
                                    tr = block.content
                                    if isinstance(tr, list):
                                        tr = "".join(
                                            getattr(x, "text", "") or str(x) for x in tr
                                        )
                                    tr = str(tr or "")
                                    if len(tr) > 600:
                                        tr = tr[:600] + "…"
                                    await send({
                                        "type": "tool_result",
                                        "id": block.tool_use_id,
                                        "is_error": bool(block.is_error),
                                        "text": tr,
                                    })
                        elif isinstance(msg, ResultMessage):
                            cumulative_cost = msg.total_cost_usd or cumulative_cost
                            usage = getattr(msg, "usage", None) or {}
                            await send({
                                "type": "turn_end",
                                "cost_usd": cumulative_cost,
                                "stop_reason": getattr(msg, "stop_reason", "") or "",
                                "usage": usage,
                            })
                finally:
                    hb_task.cancel()
                await send({"type": "status", "status": "awaiting_reply"})

            await client.query(kickoff)
            turn_task = asyncio.create_task(drain_turn())

            while True:
                client_recv = asyncio.create_task(ws.receive_json())
                done, _ = await asyncio.wait(
                    [client_recv, turn_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if client_recv in done:
                    try:
                        msg = client_recv.result()
                    except WebSocketDisconnect:
                        break
                    mtype = msg.get("type")
                    if mtype == "stop":
                        break
                    if mtype == "reply":
                        text = (msg.get("text") or "").strip()
                        if not text:
                            continue
                        if not turn_task.done():
                            await turn_task
                        await client.query(text)
                        turn_task = asyncio.create_task(drain_turn())
                    elif mtype == "finalize":
                        if not turn_task.done():
                            await turn_task
                        try:
                            promoted = _promote_era_chapter(run_dir, era, corpus_id)
                        except ValueError as exc:
                            await send({"type": "error", "message": str(exc)})
                            continue
                        await send({
                            "type": "finalized",
                            "content": promoted["content"],
                            "location": promoted["location"],
                            "words": promoted["words"],
                            "overwritten": promoted["overwritten"],
                        })
                else:
                    client_recv.cancel()
                    try:
                        await client_recv
                    except (asyncio.CancelledError, WebSocketDisconnect):
                        pass

            await send({
                "type": "done",
                "cost_usd": cumulative_cost,
                "run_dir": str(run_dir.relative_to(REPO)),
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        await send({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        for tk in tasks:
            tk.cancel()
        try:
            await ws.close()
        except Exception:
            pass
