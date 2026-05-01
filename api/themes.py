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
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from api.auth import _gc_auth, _load_auth
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

router = APIRouter()


def _themes_base(corpus_id: str | None = None) -> Path:
    return wb.corpus_root(corpus_id) / "claude" / "themes"


def _prepare_themes_run(top_n: int = 7, corpus_id: str | None = None) -> dict:
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
        "**Then transition to curate mode:** emit a `## Current state` block "
        'listing every theme you just proposed with status `[kept]`, then end '
        'with the single line "Ready for your moves." Wait for the user.\n\n'
        f"When the user signals lock, write themes to {run_dir_abs}/themes.md "
        "using the Write tool, in the LOCKING format from your system prompt. "
        "Don't list directories, don't read sibling files, don't browse "
        "anywhere else.\n\n"
        "--- INPUT-START ---\n\n"
        "# Corpus sample (your full context)\n\n"
        + corpus_sample
        + "\n\n--- INPUT-END ---\n"
    )


@router.get("/notes/themes-top-n")
def list_themes_top_n_notes(n: int = 7, session: str = Depends(require_corpus_access)):
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
    tasks: list[asyncio.Task] = []
    run_dir: Path | None = None
    cumulative_cost = 0.0
    finalize_pending = False

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

    try:
        top_n = int(first.get("top_n") or 7)
        model_key = first.get("model")
        model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

        prep = _prepare_themes_run(top_n=top_n, corpus_id=corpus_id)
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

        # Single-phase: round-1 generation + curate happen inside one SDK
        # session. System prompt combines both; kickoff inlines the corpus
        # sample and instructs the agent to generate round-1, then enter
        # curate orientation. Streaming is visible from the first token.
        themes_r1_prompt = THEMES_R1_PATH.read_text(encoding="utf-8")
        curate_prompt = CURATE_PATH.read_text(encoding="utf-8")
        combined_system = (
            themes_r1_prompt.rstrip("\n")
            + "\n\n---\n\n"
            + curate_prompt
        )
        kickoff = _build_themes_kickoff(run_dir_abs, full_user_msg, corpus_id)

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
            system_prompt=combined_system,
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
