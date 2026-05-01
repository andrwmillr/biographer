"""Module-level configuration for the FastAPI shim.

Constants live here (not server.py) so tests can monkey-patch the mutable
ones (CORPORA_ROOT, AUTH_DIR, AUTH_STATE_PATH) and have every consumer see
the new value via attribute access (`api.config.CORPORA_ROOT`). All other
constants are immutable and can be imported by name (`from api.config
import REPO, MAX_UPLOAD_BYTES, ...`).

Importing this module also has the side effect of loading
`_web/.env` into os.environ. This must run before any other api/* or
core/* import — keep `from api import config` at the top of every
consumer.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Resolve directory paths.
WEB_DIR = Path(__file__).resolve().parent.parent  # _web/
REPO = WEB_DIR.parent                              # notes-archive/
PROMPTS_DIR = WEB_DIR / "core" / "prompts"

# Load _web/.env into the environment, but skip ANTHROPIC_API_KEY so
# claude-agent-sdk falls back to the user's Claude Code subscription
# instead of billing the API account.
_env_file = WEB_DIR / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k == "ANTHROPIC_API_KEY":
            continue
        if not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")
os.environ.pop("ANTHROPIC_API_KEY", None)

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS", "http://localhost:5173"
    ).split(",") if o.strip()
]

# Auth — magic-link layer.
AUTH_DIR = REPO / "_auth"
AUTH_STATE_PATH = AUTH_DIR / "state.json"
MAGIC_TOKEN_TTL = 60 * 15            # 15 minutes
AUTH_TOKEN_TTL = 60 * 60 * 24 * 90   # 90 days
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Multi-tenant corpora.
CORPORA_ROOT = REPO / "_corpora"
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Upload limits for /import/notes.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # 50 MB raw zip
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # 500 MB uncompressed (zip-bomb defense)

# Prompt files used by drafts + themes flows.
KICKOFF_PATH = PROMPTS_DIR / "kickoff.md"
THEMES_R1_PATH = PROMPTS_DIR / "themes_r1.md"
CURATE_PATH = PROMPTS_DIR / "curate.md"
CURATE_KICKOFF_PATH = PROMPTS_DIR / "curate_kickoff.md"
