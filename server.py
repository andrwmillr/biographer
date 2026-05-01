#!/usr/bin/env python3
"""FastAPI shim around write_biography.py — multi-tenant biographer.

Endpoints are split across routers in sibling modules:
  auth.py     — magic-link auth (/auth/*)
  corpora.py  — session→slug resolution + read endpoints (/eras, /notes,
                /corpus, /samples)
  imports.py  — zip import + corpus wipe (/import/*, /corpus/wipe)
  drafts.py   — chapter drafting (/draft, /session WS)
  themes.py   — themes-curate flow (/themes-curate WS, /notes/themes-top-n)

`config.py` (imported first, side effects only) loads `scripts/.env` and
inserts `scripts/` onto sys.path before `import corpus as wb`.

Run:
  uv run --with 'fastapi[standard]' --with anthropic --with pyyaml \\
    --with claude-agent-sdk fastapi dev _web/server.py
"""
from __future__ import annotations

import config  # noqa: F401  — must precede `import corpus`

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import auth
import corpora
import drafts
import imports
import themes

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
