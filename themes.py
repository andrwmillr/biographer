"""Themes-curate flow.

`GET /notes/themes-top-n` is an admin-gated read returning the
folder-aware top-N corpus sample (input to the themes flow).

`WS /themes-curate` is two-phase: first a `claude -p` round that produces
the round-1 themes list (streamed as `narration`, persisted to
output.md), then a ClaudeSDKClient curate chat that lets the agent edit
themes within the run dir. On `finalize`, the server sends `/lock` to
the agent, which writes themes.md; the file watcher emits `finalized`
when that write lands.
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
from config import (
    ADMIN_EMAILS,
    CURATE_KICKOFF_PATH,
    CURATE_PATH,
    REPO,
    THEMES_R1_PATH,
)
from corpora import (
    _load_state,
    _note_source,
    _session_corpus_id,
    corpus_dir,
    get_session,
    require_admin,
)
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

import write_biography as wb

router = APIRouter()

THEMES_BASE = wb.CORPUS / "claude" / "themes"


def _prepare_themes_run(top_n: int = 7) -> dict:
    """Build the round-1 corpus-themes input message and create a fresh
    themes run dir on disk. Mirrors spin_themes.py's build_input(top_n)
    and OUT_DIR layout.

    Returns {run_dir, run_rel, full_user_msg, top_n, in_chars}."""
    import spin_themes  # imported lazily; module-level OUT_DIR is unused here

    user_msg = spin_themes.build_input(top_n)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = THEMES_BASE / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.md").write_text(user_msg, encoding="utf-8")
    return {
        "run_dir": run_dir,
        "run_rel": str(run_dir.relative_to(REPO)),
        "full_user_msg": user_msg,
        "top_n": top_n,
        "in_chars": len(user_msg),
    }


def _build_curate_kickoff(run_dir_abs: Path) -> str:
    """Read CURATE_KICKOFF.md, substitute placeholders, append the INPUT
    block of round-1 themes + corpus sample inlined from the run dir.
    Mirrors _build_kickoff for the chapter flow."""
    kickoff = CURATE_KICKOFF_PATH.read_text(encoding="utf-8")
    kickoff = kickoff.replace("__RUN_DIR__", str(run_dir_abs))
    kickoff = kickoff.replace("__SUBJECT__", wb.SUBJECT_NAME)

    round1_themes = (run_dir_abs / "output.md").read_text(encoding="utf-8")
    corpus_sample = (run_dir_abs / "input.md").read_text(encoding="utf-8")

    return (
        kickoff.rstrip("\n")
        + "\n\n--- INPUT-START ---\n\n"
        + "# Round-1 themes (the starting list)\n\n"
        + round1_themes
        + "\n\n# Corpus sample (your full context)\n\n"
        + corpus_sample
        + "\n\n--- INPUT-END ---\n"
    )


@router.get("/notes/themes-top-n", dependencies=[Depends(require_admin)])
def list_themes_top_n_notes(n: int = 7, session: str = Depends(get_session)):
    """Folder-aware top-N sample fed to /themes-curate, flattened across
    eras and sorted chronologically. Same item shape as /notes?era=… so
    the UI can use one renderer. Default n=7 — 10 exceeds context."""
    import spin_themes
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, eras = _load_state(corpus_id)
    sampled = []
    for era_name, _, _ in eras:
        era_notes = by_era.get(era_name, [])
        if era_notes:
            sampled.extend(spin_themes.folder_aware_sample(era_notes, n))
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
async def themes_curate(ws: WebSocket, session: str | None = None, auth: str | None = None):
    await ws.accept()
    tasks: list[asyncio.Task] = []
    run_dir: Path | None = None
    cumulative_cost = 0.0
    finalize_pending = False

    async def send(obj: dict):
        try:
            await ws.send_text(json.dumps(obj))
        except Exception:
            pass

    state = _gc_auth(_load_auth())
    record = state["sessions"].get(auth) if auth else None
    email = record["email"] if record else None
    if email is None or email not in ADMIN_EMAILS:
        await send({"type": "error", "message": "themes curation is admin-only"})
        try:
            await ws.close()
        except Exception:
            pass
        return

    try:
        first = await ws.receive_json()
        if first.get("type") != "start":
            await send({"type": "error", "message": "first message must be {type:'start'}"})
            return

        top_n = int(first.get("top_n") or 7)
        model_key = first.get("model")
        model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

        prep = _prepare_themes_run(top_n=top_n)
        run_dir = prep["run_dir"]
        run_rel = prep["run_rel"]
        full_user_msg = prep["full_user_msg"]
        in_chars = prep["in_chars"]
        run_dir_abs = run_dir

        await send({
            "type": "spawned",
            "model": model,
            "run_dir": run_rel,
            "top_n": top_n,
            "input_chars": in_chars,
        })

        sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        # ---- Phase 1: round-1 themes via `claude -p` ----
        themes_r1_prompt = THEMES_R1_PATH.read_text(encoding="utf-8")
        await send({"type": "status", "status": "generating"})

        chunks: list[str] = []
        result_evt: dict | None = None
        proc: asyncio.subprocess.Process | None = None
        side_tasks: list[asyncio.Task] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
                "--model", model,
                "--system-prompt", themes_r1_prompt,
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--no-session-persistence",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sub_env,
            )
            assert (
                proc.stdin is not None
                and proc.stdout is not None
                and proc.stderr is not None
            )

            async def feed_stdin():
                proc.stdin.write(full_user_msg.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

            async def pipe_stderr():
                async for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        await send({"type": "log", "text": f"stderr: {line}"})

            async def heartbeat():
                start = asyncio.get_running_loop().time()
                while True:
                    await asyncio.sleep(15)
                    elapsed = int(asyncio.get_running_loop().time() - start)
                    streamed = sum(len(c) for c in chunks)
                    await send({
                        "type": "log",
                        "text": (
                            f"… still working ({elapsed}s elapsed, "
                            f"{streamed} chars streamed)"
                        ),
                    })

            stdin_task = asyncio.create_task(feed_stdin())
            side_tasks.append(asyncio.create_task(pipe_stderr()))
            side_tasks.append(asyncio.create_task(heartbeat()))

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
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                chunks.append(text)
                                await send({"type": "narration", "text": text})
                elif t == "system":
                    sub = evt.get("subtype")
                    if sub == "init":
                        await send({
                            "type": "log",
                            "text": (
                                f"claude initialized · model="
                                f"{evt.get('model', '?')} · session="
                                f"{evt.get('session_id', '?')}"
                            ),
                        })
                    elif sub == "status":
                        await send({
                            "type": "log",
                            "text": f"status · {evt.get('status', '')}",
                        })
                elif t == "result":
                    result_evt = evt

            await stdin_task
            rc = await proc.wait()
        except Exception as e:
            if chunks:
                (run_dir / "output.partial.md").write_text("".join(chunks), encoding="utf-8")
            await send({"type": "error", "message": f"round-1 failed: {type(e).__name__}: {e}"})
            return
        finally:
            for tk in side_tasks:
                tk.cancel()
            # Reap on any exit path (success, exception, or task cancellation
            # from a WS disconnect). Without this, the `claude -p` subprocess
            # outlives the handler and accumulates as orphans.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        round1_text = "".join(chunks)
        (run_dir / "output.md").write_text(round1_text, encoding="utf-8")

        if rc != 0 and not result_evt:
            stderr = ""
            if proc.stderr:
                try:
                    stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
                except Exception:
                    pass
            await send({"type": "error", "message": f"claude exited {rc}: {stderr[-500:]}"})
            return

        cumulative_cost += float((result_evt or {}).get("total_cost_usd", 0) or 0)
        r1_usage = (result_evt or {}).get("usage", {}) if result_evt else {}

        await send({"type": "draft_update", "kind": "output", "content": round1_text})
        await send({
            "type": "turn_end",
            "cost_usd": cumulative_cost,
            "stop_reason": (result_evt or {}).get("stop_reason", "") or "",
            "usage": r1_usage,
        })

        # ---- Phase 2: curate chat ----
        kickoff = _build_curate_kickoff(run_dir_abs)
        curate_system = CURATE_PATH.read_text(encoding="utf-8").replace(
            "__SUBJECT__", wb.SUBJECT_NAME
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

        themes_relative = str((run_dir / "themes.md").relative_to(REPO))

        async def watch_files():
            nonlocal finalize_pending
            paths = {"themes": run_dir / "themes.md"}
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
                        if finalize_pending and kind == "themes":
                            finalize_pending = False
                            await send({
                                "type": "finalized",
                                "content": content,
                                "location": themes_relative,
                                "words": len(content.split()),
                                "overwritten": False,
                            })

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

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=curate_system,
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Edit", "Write"],
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
                async for msg in client.receive_response():
                    if isinstance(msg, StreamEvent):
                        event = msg.event if hasattr(msg, "event") else {}
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                if text:
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
                        finalize_pending = True
                        await client.query("/lock")
                        turn_task = asyncio.create_task(drain_turn())
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
