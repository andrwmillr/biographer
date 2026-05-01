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

import sys
from pathlib import Path

# fastapi-dev imports this file directly; put _web/ on sys.path so
# `from api import …` and `from core import …` resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import config  # noqa: F401, E402  — must precede other api/core imports

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api import auth, corpora, drafts, imports, themes  # noqa: E402

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(corpora.router)
app.include_router(imports.router)
app.include_router(drafts.router)
app.include_router(themes.router)
