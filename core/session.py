"""Session lifecycle decoupled from the WebSocket.

A Session owns the SDK client + drain loop + event log for a single
run_dir. It outlives the WS connection: a tab-switch / reload / brief
disconnect detaches the client but leaves the agent running. When
a WS reattaches with the matching run_id, the full event log is
replayed and live events resume streaming.

In-memory only. Server restart drops all sessions; the disk artifacts
(output.md / state.md / themes.md / etc.) remain and the cold-path
Tier 2.5 resume in core/resume.py rebuilds a fresh session from them.

Lifecycle:
  1. create_session() → registers, kicks off background lifecycle task
  2. Bootstrap task: opens SDK ClaudeSDKClient, sends kickoff, runs the
     first drain_turn, waits on stop_event.
  3. attach(ws) replays event_log to ws, adds to attached set.
  4. detach(ws) removes from set, marks last_attached_at.
  5. query(text) waits for any in-flight drain, sends to SDK, starts
     a fresh drain_task that emits events through session.emit.
  6. finalize() is just query("/lock").
  7. stop() sets stop_event → bootstrap task exits async with → SDK
     closes → unregister_session.
  8. gc_loop() reaps sessions with no attached WS for > GC_IDLE_SECONDS.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

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
from fastapi import WebSocket


# Idle timeout: when no WS has been attached for this long, the session
# is reaped. Long enough to forgive a meal break, short enough that
# orphaned subprocesses don't accumulate forever.
GC_IDLE_SECONDS = 30 * 60

_SESSIONS: dict[str, "Session"] = {}


def get_session(run_id: str) -> "Session | None":
    return _SESSIONS.get(run_id)


def register_session(session: "Session") -> None:
    _SESSIONS[session.run_id] = session


def unregister_session(run_id: str) -> None:
    _SESSIONS.pop(run_id, None)


def all_sessions() -> list["Session"]:
    return list(_SESSIONS.values())


@dataclass
class Session:
    run_id: str
    run_dir: Path
    corpus_id: str
    kind: str  # "era" or "themes"
    options: ClaudeAgentOptions
    kickoff: str
    # Optional hook called after each turn finishes with the full
    # narration text. Themes uses this to persist state.md for
    # cold-path Tier 2.5 resume.
    on_turn_complete: Callable[[str], Awaitable[None]] | None = None
    # Optional long-running task that emits side-channel events (file
    # watch, etc.). Started after the SDK client opens, cancelled when
    # the session shuts down.
    background_loop: Callable[["Session"], Awaitable[None]] | None = None
    # User email — stored for telemetry on unclean shutdown.
    email: str = ""
    # Era name (era sessions only) — stored for telemetry.
    era: str = ""

    client: ClaudeSDKClient | None = field(default=None, init=False)
    bootstrap_task: asyncio.Task | None = field(default=None, init=False)
    drain_task: asyncio.Task | None = field(default=None, init=False)
    background_task: asyncio.Task | None = field(default=None, init=False)
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    event_log: list[dict] = field(default_factory=list, init=False)
    attached: set[WebSocket] = field(default_factory=set, init=False)
    last_attached_at: float = field(default_factory=time.time, init=False)
    cumulative_cost: float = field(default=0.0, init=False)
    finalize_pending: bool = field(default=False, init=False)
    # status: starting | running | finalized | error | abandoned
    status: str = field(default="starting", init=False)

    # ---- Public API ----

    async def emit(self, event: dict) -> None:
        """Append to event log and broadcast to all attached clients.
        Failed sends just drop that client from the attached set; the
        event still lives in the log and replays on next attach."""
        self.event_log.append(event)
        for ws in list(self.attached):
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                self.attached.discard(ws)

    async def attach(self, ws: WebSocket) -> None:
        """Replay event_log then add to attached set. Uses a catch-up
        loop: each ws.send_text is a yield point where emit() can append
        new events. We re-slice after each batch until the gap is zero,
        then add to attached — no await between the empty check and the
        add, so no event can slip through."""
        sent = 0
        while True:
            pending = self.event_log[sent:]
            if not pending:
                break
            for event in pending:
                try:
                    await ws.send_text(json.dumps(event))
                except Exception:
                    return
            sent += len(pending)
        self.attached.add(ws)
        self.last_attached_at = time.time()

    def detach(self, ws: WebSocket) -> None:
        self.attached.discard(ws)
        self.last_attached_at = time.time()

    async def query(self, text: str) -> None:
        """User-initiated query. Waits for any in-flight drain, sends
        to SDK, kicks off a new drain_task. Serialized via query_lock
        so two attached tabs can't interleave queries."""
        async with self.query_lock:
            if not self.client or self.status not in ("running", "finalizing"):
                return
            if self.drain_task and not self.drain_task.done():
                try:
                    await self.drain_task
                except Exception:
                    pass
            await self.emit({"type": "user_message", "text": text})
            await self.client.query(text)
            self.drain_task = asyncio.create_task(self._drain_turn())

    async def wait_idle(self) -> None:
        """Block until any in-flight drain settles. Used by the era
        finalize path so server-side promote captures a fully-written
        output.md instead of a partial mid-stream snapshot."""
        if self.drain_task and not self.drain_task.done():
            try:
                await self.drain_task
            except Exception:
                pass

    async def stop(self) -> None:
        """Graceful shutdown. Cancels in-flight drain, signals bootstrap
        to exit, waits for cleanup."""
        self.status = "abandoned"
        if self.drain_task and not self.drain_task.done():
            self.drain_task.cancel()
            try:
                await self.drain_task
            except (asyncio.CancelledError, Exception):
                pass
        self.stop_event.set()
        if self.bootstrap_task and not self.bootstrap_task.done():
            try:
                await self.bootstrap_task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- Lifecycle (called by create_session, not external callers) ----

    async def _drain_turn(self) -> None:
        """Run one drain pass over the SDK's receive_response stream.
        Translates SDK messages into client events via session.emit."""
        if not self.client:
            return
        await self.emit({"type": "status", "status": "generating"})
        turn_narration: list[str] = []
        try:
            async for msg in self.client.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event if hasattr(msg, "event") else {}
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                turn_narration.append(text)
                                await self.emit({"type": "narration", "text": text})
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            await self.emit({
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
                            await self.emit({
                                "type": "tool_result",
                                "id": block.tool_use_id,
                                "is_error": bool(block.is_error),
                                "text": tr,
                            })
                elif isinstance(msg, ResultMessage):
                    self.cumulative_cost = msg.total_cost_usd or self.cumulative_cost
                    usage = getattr(msg, "usage", None) or {}
                    await self.emit({
                        "type": "turn_end",
                        "cost_usd": self.cumulative_cost,
                        "stop_reason": getattr(msg, "stop_reason", "") or "",
                        "usage": usage,
                    })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self.emit({"type": "error", "message": f"drain: {type(e).__name__}: {e}"})

        full_text = "".join(turn_narration).strip()
        if full_text and self.on_turn_complete:
            try:
                await self.on_turn_complete(full_text)
            except Exception:
                pass
        await self.emit({"type": "status", "status": "awaiting_reply"})

    async def _run_lifecycle(self) -> None:
        """Long-lived owner of the SDK client context. Sends kickoff,
        runs first drain, then blocks on stop_event."""
        try:
            async with ClaudeSDKClient(options=self.options) as client:
                self.client = client
                if self.background_loop is not None:
                    self.background_task = asyncio.create_task(
                        self.background_loop(self)
                    )
                await client.query(self.kickoff)
                self.status = "running"
                self.drain_task = asyncio.create_task(self._drain_turn())
                await self.stop_event.wait()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self.emit({"type": "error", "message": f"lifecycle: {type(e).__name__}: {e}"})
            self.status = "error"
        finally:
            if self.background_task and not self.background_task.done():
                self.background_task.cancel()
                try:
                    await self.background_task
                except (asyncio.CancelledError, Exception):
                    pass
            self.client = None
            unregister_session(self.run_id)


async def create_session(
    *,
    run_id: str,
    run_dir: Path,
    corpus_id: str,
    kind: str,
    options: ClaudeAgentOptions,
    kickoff: str,
    spawned_event: dict,
    on_turn_complete: Callable[[str], Awaitable[None]] | None = None,
    background_loop: Callable[[Session], Awaitable[None]] | None = None,
    email: str = "",
    era: str = "",
) -> Session:
    """Create + register + kick off a new session. spawned_event is
    pre-emitted into the event log so first-attach replay shows it."""
    session = Session(
        run_id=run_id,
        run_dir=run_dir,
        corpus_id=corpus_id,
        kind=kind,
        options=options,
        kickoff=kickoff,
        on_turn_complete=on_turn_complete,
        background_loop=background_loop,
        email=email,
        era=era,
    )
    # Pre-seed the spawned event so attach replay sends it to clients.
    session.event_log.append(spawned_event)
    register_session(session)
    session.bootstrap_task = asyncio.create_task(session._run_lifecycle())
    return session


async def gc_loop() -> None:
    """Background task — reap sessions with no attached WS for too long."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for run_id, session in list(_SESSIONS.items()):
            if session.attached:
                continue
            if (now - session.last_attached_at) > GC_IDLE_SECONDS:
                # Recheck: a WS may have attached during a prior
                # iteration's stop() (which awaits the bootstrap task).
                if session.attached:
                    continue
                try:
                    await session.stop()
                except Exception:
                    pass
