"""Module-level configuration for the FastAPI shim.

Constants live here (not server.py) so tests can monkey-patch the mutable
ones (CORPORA_ROOT, AUTH_DIR, AUTH_STATE_PATH) and have every consumer see
the new value via attribute access (`config.CORPORA_ROOT`). All other
constants are immutable and can be imported by name (`from config import
REPO, MAX_UPLOAD_BYTES, ...`).

Importing this module also has the side effect of (a) loading
`_scripts/.env` into os.environ and (b) inserting `_scripts/` onto
sys.path. This must run before `import corpus as wb` from any
module — keep `import config` (or `from config import ...`) at the top of
every consumer.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "_scripts"))

# Load _scripts/.env into the environment, but skip ANTHROPIC_API_KEY so
# claude-agent-sdk falls back to the user's Claude Code subscription
# instead of billing the API account.
_env_file = REPO / "_scripts" / ".env"
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
KICKOFF_PATH = REPO / "_scripts" / "KICKOFF.md"
THEMES_R1_PATH = REPO / "_scripts" / "THEMES_R1.md"
CURATE_PATH = REPO / "_scripts" / "CURATE.md"
CURATE_KICKOFF_PATH = REPO / "_scripts" / "CURATE_KICKOFF.md"
