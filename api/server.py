#!/usr/bin/env python3
"""FastAPI shim around the corpus library — multi-tenant biographer.

Endpoints are split across routers in sibling modules:
  api/auth.py     — magic-link auth (/auth/*)
  api/corpora.py  — session→slug resolution + read endpoints (/eras, /notes,
                    /corpus, /samples)
  api/imports.py  — zip import + corpus wipe (/import/*, /corpus/wipe)
  api/drafts.py   — chapter drafting (/draft, /session WS)
  api/themes.py   — themes-curate flow (/themes-curate WS, /notes/themes-top-n)

`api.config` (imported first, side effects only) loads `_web/.env` into
the environment. The `sys.path` insert below puts `_web/` on the path so
`from api import X` and `from core import X` resolve when fastapi-dev
loads this file directly.

Run:
  uv run --with 'fastapi[standard]' --with anthropic --with pyyaml \\
    --with claude-agent-sdk fastapi dev _web/api/server.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# fastapi-dev imports this file directly; put _web/ on sys.path so
# `from api import …` and `from core import …` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import config  # noqa: F401, E402  — must precede other api/core imports

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api import auth, chapters, corpora, drafts, imports, themes  # noqa: E402
from core.session import all_sessions, gc_loop  # noqa: E402
from core.telemetry import log as tlog  # noqa: E402


def _reap_orphan_subprocesses() -> None:
    """Kill any claude agent subprocesses left over from a prior server run.
    On startup these are definitionally orphans — no session object owns them."""
    import signal
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude_agent_sdk/_bundled/claude"],
            capture_output=True, text=True,
        )
        for pid_str in result.stdout.strip().splitlines():
            pid = int(pid_str)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if result.stdout.strip():
            import time
            time.sleep(0.5)
            # SIGKILL any that didn't exit
            for pid_str in result.stdout.strip().splitlines():
                try:
                    os.kill(int(pid_str), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the session GC reaper. Sessions outlive their WS by design
    (Tier 3 resilience), so something has to clean up sessions whose
    user has truly walked away — gc_loop reaps after GC_IDLE_SECONDS."""
    _reap_orphan_subprocesses()
    gc_task = asyncio.create_task(gc_loop())
    try:
        yield
    finally:
        gc_task.cancel()
        try:
            await gc_task
        except (asyncio.CancelledError, Exception):
            pass
        # Stop all live sessions (kills subprocess) and log telemetry.
        # Run stops concurrently with a tight timeout — uvicorn's
        # graceful-shutdown window is only 2s.
        sessions = all_sessions()
        if sessions:
            async def _stop_one(s):
                try:
                    await asyncio.wait_for(s.stop(), timeout=3)
                except Exception:
                    pass
            await asyncio.gather(*[_stop_one(s) for s in sessions])
        for sess in sessions:
            extra = {"era": sess.era} if sess.era else {}
            tlog("session_end", kind=sess.kind, email=sess.email,
                 corpus=sess.corpus_id, reason="shutdown",
                 cost_usd=sess.cumulative_cost,
                 run_id=sess.run_id, **extra)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(corpora.router)
app.include_router(imports.router)
app.include_router(chapters.router)
app.include_router(drafts.router)
app.include_router(themes.router)
