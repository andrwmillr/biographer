#!/usr/bin/env python3
"""Local-only FastAPI shim around write_biography.py.

Endpoints:
  GET  /eras          -> [{name, start, end, note_count, has_chapter}]
  POST /draft         -> SSE stream of {type, ...} events for one era (one-shot)
  WS   /session       -> bidirectional draft session with KICKOFF checkpoints

Run:
  uv run --with 'fastapi[standard]' --with anthropic --with pyyaml \
    --with claude-agent-sdk fastapi dev _web/server.py
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "_scripts"))

_env_file = REPO / "_scripts" / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        # Skip ANTHROPIC_API_KEY — we want claude-agent-sdk to use the
        # user's Claude Code subscription, not bill the API account.
        if k == "ANTHROPIC_API_KEY":
            continue
        if not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")
# Belt-and-suspenders: also remove it if it was already in the shell env.
os.environ.pop("ANTHROPIC_API_KEY", None)

import write_biography as wb  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    ToolUseBlock,
    ToolResultBlock,
)
from claude_agent_sdk.types import StreamEvent  # noqa: E402

app = FastAPI()

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS", "http://localhost:5173"
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Magic-link auth layer ------------------------------------------------
# Auth tokens identify a user (email); X-Corpus-Session continues to identify
# which corpus to operate on. An email can own multiple corpora. State is
# persisted to _auth/state.json (single file, atomic write-replace).

AUTH_DIR = REPO / "_auth"
AUTH_STATE_PATH = AUTH_DIR / "state.json"
MAGIC_TOKEN_TTL = 60 * 15            # 15 minutes
AUTH_TOKEN_TTL = 60 * 60 * 24 * 90   # 90 days
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _now_ts() -> int:
    return int(datetime.now().timestamp())


def _empty_auth_state() -> dict:
    return {"users": {}, "sessions": {}, "pending": {}}


def _load_auth() -> dict:
    if not AUTH_STATE_PATH.exists():
        return _empty_auth_state()
    try:
        return json.loads(AUTH_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_auth_state()


def _save_auth(state: dict) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(AUTH_STATE_PATH)


def _gc_auth(state: dict) -> dict:
    now = _now_ts()
    state["pending"] = {
        t: r for t, r in state["pending"].items()
        if r.get("expires", 0) > now
    }
    state["sessions"] = {
        t: r for t, r in state["sessions"].items()
        if r.get("expires", 0) > now
    }
    return state


def _send_email(to: str, subject: str, html: str, text: str | None = None) -> None:
    provider = os.environ.get("EMAIL_PROVIDER") or (
        "resend" if os.environ.get("RESEND_API_KEY") else "console"
    )
    if provider == "console":
        print(f"\n--- EMAIL (console) ---", file=sys.stderr)
        print(f"to: {to}", file=sys.stderr)
        print(f"subject: {subject}", file=sys.stderr)
        print(html, file=sys.stderr)
        print("--- END EMAIL ---\n", file=sys.stderr)
        return
    if provider == "resend":
        api_key = os.environ.get("RESEND_API_KEY")
        if not api_key:
            raise HTTPException(500, "RESEND_API_KEY not configured")
        sender = os.environ.get("EMAIL_FROM") or "onboarding@resend.dev"
        payload = {
            "from": sender,
            "to": [to],
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "biographer/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 400:
                    raise HTTPException(502, f"resend error: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise HTTPException(502, f"resend error: {e.code} {detail}")
        except urllib.error.URLError as e:
            raise HTTPException(502, f"resend network error: {e}")
        return
    raise HTTPException(500, f"unknown EMAIL_PROVIDER: {provider}")


class AuthRequestBody(BaseModel):
    email: str
    return_url: str


@app.post("/auth/request", status_code=204)
def auth_request(req: AuthRequestBody):
    email = req.email.strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "invalid email")
    parsed = urlparse(req.return_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in ALLOWED_ORIGINS:
        raise HTTPException(400, "return_url origin not allowed")

    state = _gc_auth(_load_auth())
    magic = secrets.token_urlsafe(32)
    state["pending"][magic] = {
        "email": email,
        "return_url": req.return_url,
        "expires": _now_ts() + MAGIC_TOKEN_TTL,
    }
    _save_auth(state)

    backend_base = os.environ.get("BACKEND_URL") or "http://localhost:8000"
    verify_url = f"{backend_base}/auth/verify?token={magic}"
    html = (
        f'<p>Hi,</p>'
        f'<p>Use the link below to sign in to Biographer. It expires in 15 minutes.</p>'
        f'<p><a href="{verify_url}" style="display:inline-block;padding:10px 20px;'
        f'background:#44403c;color:#ffffff;text-decoration:none;border-radius:4px;'
        f'font-family:sans-serif;font-size:14px">Sign in to Biographer</a></p>'
        f'<p style="color:#999;font-size:12px;margin-top:24px">'
        f'If you didn\'t request this, you can safely ignore this email — '
        f'someone may have typed your address by mistake.</p>'
        f'<p style="color:#999;font-size:12px">— Biographer</p>'
    )
    text = (
        f"Hi,\n\n"
        f"Use the link below to sign in to Biographer. It expires in 15 minutes.\n\n"
        f"{verify_url}\n\n"
        f"If you didn't request this, you can safely ignore this email — "
        f"someone may have typed your address by mistake.\n\n"
        f"— Biographer"
    )
    _send_email(
        to=email,
        subject="Sign in to Biographer",
        html=html,
        text=text,
    )


@app.get("/auth/verify")
def auth_verify(token: str):
    state = _gc_auth(_load_auth())
    record = state["pending"].pop(token, None)
    if not record:
        raise HTTPException(400, "invalid or expired token")
    email = record["email"]
    return_url = record["return_url"]
    auth_token = secrets.token_urlsafe(32)
    state["sessions"][auth_token] = {
        "email": email,
        "expires": _now_ts() + AUTH_TOKEN_TTL,
    }
    state["users"].setdefault(email, [])
    _save_auth(state)
    return RedirectResponse(f"{return_url}#auth={auth_token}", status_code=302)


def get_auth(x_auth_token: str | None = Header(None)) -> str:
    if not x_auth_token:
        raise HTTPException(401, "missing X-Auth-Token header")
    state = _gc_auth(_load_auth())
    record = state["sessions"].get(x_auth_token)
    if not record:
        raise HTTPException(401, "invalid or expired auth token")
    return record["email"]


def get_auth_optional(x_auth_token: str | None = Header(None)) -> str | None:
    """Like get_auth, but returns None for missing/invalid tokens instead of
    raising. Used by require_corpus_access so the legacy admin session (which
    has no auth token) can still pass through."""
    if not x_auth_token:
        return None
    state = _gc_auth(_load_auth())
    record = state["sessions"].get(x_auth_token)
    return record["email"] if record else None


def _attach_corpus_to_user(email: str, slug: str) -> None:
    state = _load_auth()
    owned = state["users"].setdefault(email, [])
    if slug not in owned:
        owned.append(slug)
    _save_auth(state)


def _detach_corpus_from_user(email: str, slug: str) -> None:
    state = _load_auth()
    owned = state["users"].get(email, [])
    if slug in owned:
        owned.remove(slug)
        _save_auth(state)


@app.get("/auth/me")
def auth_me(email: str = Depends(get_auth)):
    state = _load_auth()
    return {"email": email, "corpora": state["users"].get(email, [])}


@app.post("/auth/logout", status_code=204)
def auth_logout(x_auth_token: str | None = Header(None)):
    if not x_auth_token:
        return
    state = _load_auth()
    state["sessions"].pop(x_auth_token, None)
    _save_auth(state)


# ---- Multi-tenant corpus session layer ------------------------------------
# Each browser holds an opaque session value in localStorage and sends it as
# X-Corpus-Session on every request. The session value IS the corpus slug
# under _corpora/<slug>/. The legacy single-tenant corpus at _corpus/ is
# accessed by the special session value LEGACY_SESSION (Andrew's admin
# session). Value is loaded from env (set in _scripts/.env, never committed).
# Set it once in DevTools:
#   localStorage.setItem('corpusSession', '<value of LEGACY_SESSION>')

CORPORA_ROOT = REPO / "_corpora"
LEGACY_SESSION = os.environ["LEGACY_SESSION"]

# Upload limits for /import/notes.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024          # 50 MB raw zip
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # 500 MB uncompressed (zip-bomb defense)


def get_session(x_corpus_session: str | None = Header(None)) -> str:
    if not x_corpus_session:
        raise HTTPException(401, "missing X-Corpus-Session header")
    return x_corpus_session


def require_legacy(session: str = Depends(get_session)) -> None:
    if session != LEGACY_SESSION:
        raise HTTPException(403, "this endpoint is reserved for the legacy admin session")


def require_corpus_access(
    session: str = Depends(get_session),
    auth_email: str | None = Depends(get_auth_optional),
) -> str:
    """Gate on (auth token, corpus ownership). The legacy admin session
    bypasses the email check — it's authenticated by knowledge of the
    server-side LEGACY_SESSION secret. Every other corpus session requires
    an X-Auth-Token whose user owns the slug."""
    if session == LEGACY_SESSION:
        return session
    if auth_email is None:
        raise HTTPException(401, "auth required: missing or invalid X-Auth-Token")
    state = _load_auth()
    if session not in state["users"].get(auth_email, []):
        raise HTTPException(403, "this corpus is not owned by the authenticated user")
    return session


def corpus_dir(session: str) -> Path:
    """Resolve a session string to its on-disk corpus directory.
    Raises 401 for invalid / nonexistent sessions."""
    if session == LEGACY_SESSION:
        return REPO / "_corpora" / "andrew"
    # Reject session values that could be the host's directory name or that
    # don't match the issued slug shape. Imported corpora use slugs like
    # "c_<16 hex>"; "andrew" is the legacy admin slug now ownable by email.
    if session != "andrew" and not re.fullmatch(r"c_[0-9a-f]{16}", session):
        raise HTTPException(401, "invalid session")
    candidate = CORPORA_ROOT / session
    try:
        candidate.resolve().relative_to(CORPORA_ROOT.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(401, "invalid session")
    if not candidate.is_dir():
        raise HTTPException(401, "session not found (corpus may have been wiped)")
    return candidate


def make_slug() -> str:
    return f"c_{secrets.token_hex(8)}"


def _session_corpus_id(session: str) -> str:
    """Map a session header value to the wb corpus_id. Legacy admin session
    points at andrew's corpus; everything else is the slug as-is."""
    return "andrew" if session == LEGACY_SESSION else session


def _load_state(corpus_id: str = "andrew"):
    notes = wb.load_corpus_notes(corpus_id)
    wb.apply_date_overrides(notes, corpus_id)
    verdicts = wb.load_authorship(corpus_id)
    notes, _, _ = wb.apply_authorship(notes, verdicts)
    wb.apply_note_metadata(notes, corpus_id)
    wb.flag_date_clusters(notes)
    eras = wb.load_eras(corpus_id)
    by_era = {name: [] for name, _, _ in eras}
    for n in notes:
        e = wb.era_of(n.get("date", ""), eras)
        if e in by_era:
            by_era[e].append(n)
    return notes, by_era, eras


@app.get("/eras")
def list_eras(session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    # Validate the session resolves to an actual on-disk corpus.
    corpus_dir(session)
    _, by_era, eras = _load_state(corpus_id)
    chapters = wb.chapters_dir(corpus_id)
    out = []
    for name, start, end in eras:
        chapter_path = chapters / f"{wb.era_slug(name)}.md"
        out.append({
            "name": name,
            "start": start,
            "end": end if end != "9999-99" else None,
            "note_count": len(by_era[name]),
            "has_chapter": chapter_path.exists(),
        })
    return out


_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _note_source(rel: str, corpus_id: str = "andrew") -> str:
    path = wb._corpus_paths(corpus_id)["notes"] / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = _FRONT_RE.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.startswith("source:"):
            return line.partition(":")[2].strip().strip('"').strip("'")
    return ""


@app.get("/notes")
def list_notes(era: str, session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, _ = _load_state(corpus_id)
    if era not in by_era:
        raise HTTPException(404, f"unknown era: {era}")
    notes = sorted(by_era[era], key=lambda n: n.get("date", ""))
    out = []
    for n in notes:
        rel = n["rel"]
        label = rel.split("/", 1)[0] if "/" in rel else ""
        item = {
            "rel": rel,
            "date": n.get("date", ""),
            "title": n.get("title", ""),
            "label": label,
            "source": _note_source(rel, corpus_id),
            "body": wb.parse_note_body(rel, corpus_id),
        }
        if n.get("editor_note"):
            item["editor_note"] = n["editor_note"]
        out.append(item)
    return out


class DraftRequest(BaseModel):
    era: str
    future: bool = False


class PromoteRequest(BaseModel):
    era: str
    run_dir: str  # repo-relative run dir, e.g. _corpus/.../runs/2026-04-27T...


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


def _prepare_themes_run(top_n: int = 10) -> dict:
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


@app.post("/promote")
def promote(req: PromoteRequest, session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    run_dir = (REPO / req.run_dir).resolve()
    # Reject paths that escape this corpus's biographies dump tree.
    bio_root = (wb.biographies_dir(corpus_id) / "_dump").resolve()
    try:
        rel = run_dir.relative_to(bio_root)
    except ValueError:
        raise HTTPException(400, "run_dir must be under biographies/_dump/")
    # Derive destination slug from the run_dir path itself: <era_slug>/runs/<ts>.
    # Trusting req.era was a footgun — the user could change the dropdown after
    # a session ended and overwrite the wrong chapter.
    parts = rel.parts
    if len(parts) < 3 or parts[1] != "runs":
        raise HTTPException(400, f"unexpected run_dir layout: {req.run_dir}")
    slug = parts[0]
    expected_slug = wb.era_slug(req.era)
    if slug != expected_slug:
        raise HTTPException(
            400,
            f"run_dir era ({slug}) does not match selected era ({expected_slug}). "
            f"Refusing to promote to avoid overwriting the wrong chapter.",
        )
    src = run_dir / "output.md"
    if not src.is_file():
        raise HTTPException(404, f"no output.md in {req.run_dir}")
    dst = wb.chapters_dir(corpus_id) / f"{slug}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    overwritten = dst.exists()
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "src": str(src.relative_to(REPO)),
        "dst": str(dst.relative_to(REPO)),
        "overwritten": overwritten,
        "words": len(src.read_text(encoding="utf-8").split()),
    }


@app.post("/draft")
async def draft(req: DraftRequest, session: str = Depends(require_corpus_access)):
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
            if proc and proc.returncode is None:
                proc.kill()
            yield sse({"type": "error", "message": str(e), "run_dir": run_rel})

    return StreamingResponse(gen(), media_type="text/event-stream")


KICKOFF_PATH = REPO / "_scripts" / "KICKOFF.md"
THEMES_R1_PATH = REPO / "_scripts" / "THEMES_R1.md"
CURATE_PATH = REPO / "_scripts" / "CURATE.md"
CURATE_KICKOFF_PATH = REPO / "_scripts" / "CURATE_KICKOFF.md"
THEMES_BASE = wb.CORPUS / "claude" / "themes"


class ThemesSpinRequest(BaseModel):
    top_n: int = 10
    model: str | None = None


@app.post("/themes-spin", dependencies=[Depends(require_legacy)])
async def themes_spin(req: ThemesSpinRequest):
    """Run round-1 corpus-themes generation. Streams stream_event deltas as
    SSE and persists the final output.md to the new run dir. Mirrors /draft."""
    inputs = _prepare_themes_run(req.top_n)
    full_user_msg = inputs["full_user_msg"]
    in_chars = inputs["in_chars"]
    run_dir = inputs["run_dir"]
    run_rel = inputs["run_rel"]

    themes_prompt = THEMES_R1_PATH.read_text(encoding="utf-8").replace(
        "__SUBJECT__", wb.SUBJECT_NAME
    )
    model_key = req.model
    model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

    async def gen():
        def sse(obj):
            return f"data: {json.dumps(obj)}\n\n"

        yield sse({
            "type": "start",
            "top_n": req.top_n,
            "input_chars": in_chars,
            "model": model,
            "run_dir": run_rel,
        })

        chunks: list[str] = []
        result_evt: dict | None = None
        proc: asyncio.subprocess.Process | None = None
        try:
            sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "--model", model,
                "--system-prompt", themes_prompt,
                "--output-format", "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--no-session-persistence",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sub_env,
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
                    if sub == "init":
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
            if proc and proc.returncode is None:
                proc.kill()
            yield sse({"type": "error", "message": str(e), "run_dir": run_rel})

    return StreamingResponse(gen(), media_type="text/event-stream")


def _build_curate_kickoff(run_dir_abs: Path) -> str:
    """Read CURATE_KICKOFF.md, substitute placeholders, append the INPUT block
    of round-1 themes + corpus sample inlined from the run dir. Mirrors
    _build_kickoff for the chapter flow."""
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


@app.websocket("/themes-curate")
async def themes_curate(ws: WebSocket, session: str | None = None):
    """Multi-turn curation chat against an existing themes run dir. The user
    drops, merges, tightens, and proposes themes; on /lock the model writes
    themes.md via the Write tool, which the file-watcher streams back.
    Mirrors /session structurally."""
    await ws.accept()
    tasks: list[asyncio.Task] = []
    run_dir: Path | None = None
    cumulative_cost = 0.0

    async def send(obj: dict):
        try:
            await ws.send_text(json.dumps(obj))
        except Exception:
            pass

    if session != LEGACY_SESSION:
        await send({"type": "error", "message": "themes curation is reserved for the legacy admin session"})
        try:
            await ws.close()
        except Exception:
            pass
        return

    try:
        first = await ws.receive_json()
        if first.get("type") != "start" or not first.get("run_dir"):
            await send({"type": "error", "message": "first message must be {type:'start', run_dir}"})
            return

        run_rel = first["run_dir"]
        run_dir = (REPO / run_rel).resolve()
        themes_root = THEMES_BASE.resolve()
        try:
            run_dir.relative_to(themes_root)
        except ValueError:
            await send({"type": "error", "message": "run_dir must be under themes/"})
            return
        if not (run_dir / "input.md").exists() or not (run_dir / "output.md").exists():
            await send({"type": "error", "message": "run_dir missing input.md or output.md"})
            return

        model_key = first.get("model")
        model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

        run_dir_abs = run_dir
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

        await send({
            "type": "spawned",
            "model": model,
            "run_dir": run_rel,
        })

        async def watch_files():
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
                        await send({"type": f"{kind}_update", "content": content})

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


@app.websocket("/session")
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

    # Browser WebSockets can't set custom headers, so both the corpus session
    # and the auth token ride on the URL: /session?session=<slug>&auth=<token>.
    if not session:
        await reject("missing ?session= query param")
        return
    try:
        corpus_dir(session)
    except HTTPException as e:
        await reject(e.detail)
        return
    # Auth gate: legacy admin session bypasses email auth; everything else
    # requires an auth token whose user owns the slug.
    if session != LEGACY_SESSION:
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

        try:
            inputs = _prepare_run(first["era"], corpus_id=corpus_id, include_future=bool(first.get("future")))
        except HTTPException as e:
            await send({"type": "error", "message": e.detail})
            return

        # Resolve the per-session model: client sends a friendly key like
        # "opus-4.7"; fall back to the default if absent or unrecognized.
        model_key = first.get("model")
        model = wb.MODELS.get(model_key, wb.MODEL) if model_key else wb.MODEL

        run_dir = inputs["run_dir"]
        run_dir_abs = run_dir.resolve()
        kickoff = _build_kickoff(run_dir_abs, inputs["full_user_msg"])

        # Cross-iteration blinding via per-run settings.
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
            "era": first["era"],
            "model": model,
            "run_dir": inputs["run_rel"],
            "notes": inputs["notes_count"],
            "prior_chapters": inputs["prior_count"],
            "prior_digests": inputs["digest_count"],
            "future_chapters": inputs["future_count"],
            "future_digests": inputs["future_digest_count"],
            "input_chars": inputs["in_chars"],
        })

        # Watch output.md / thinking.md and stream changes to the client.
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
                        await send({"type": f"{kind}_update", "content": content})

        watch_task = asyncio.create_task(watch_files())
        tasks = [watch_task]

        loop = asyncio.get_running_loop()

        async def stderr_cb(line: str):
            # Forward stderr lines from the underlying claude process so we
            # can show real progress signals (if any) to the client.
            line = (line or "").strip()
            if not line:
                return
            await send({"type": "log", "text": line})

        def stderr_sync(line: str):
            asyncio.run_coroutine_threadsafe(stderr_cb(line), loop)

        # Don't leak our API key into the claude subprocess — we want it to
        # use the user's Claude Code subscription, not bill the API account.
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
            # Coroutine: drain one turn's worth of messages and forward them.
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
                        # With partial messages on, text already streamed via
                        # StreamEvent — only forward tool_use blocks here.
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

            # Kick off the first turn with the inlined inputs.
            await client.query(kickoff)
            turn_task = asyncio.create_task(drain_turn())

            while True:
                # Wait for either the client or the current turn to finish.
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
                        # Wait for current turn to drain before sending the next.
                        if not turn_task.done():
                            await turn_task
                        await client.query(text)
                        turn_task = asyncio.create_task(drain_turn())
                else:
                    # Turn ended; cancel the dangling receive and loop to wait
                    # for the next user message.
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


# ---- Multi-tenant corpus import / wipe ------------------------------------


def _validate_eras_yaml(text: str) -> list:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"invalid yaml: {e}")
    if not isinstance(data, list):
        raise HTTPException(400, "eras.yaml must be a YAML list at top level")
    for i, era in enumerate(data):
        if not isinstance(era, dict):
            raise HTTPException(400, f"era #{i + 1} must be a mapping")
        if "name" not in era:
            raise HTTPException(400, f"era #{i + 1} missing 'name'")
        if "start" not in era:
            raise HTTPException(400, f"era #{i + 1} missing 'start'")
    return data


def _extract_zip_safe(content: bytes, target: Path) -> int:
    """Extract zip into target, rejecting paths that escape target and
    rejecting zip bombs that would extract beyond MAX_UNCOMPRESSED_BYTES.
    Returns count of extracted regular files."""
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            total_uncompressed = sum(m.file_size for m in infos)
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise HTTPException(
                    413,
                    f"zip would extract to {total_uncompressed:,} bytes; "
                    f"max is {MAX_UNCOMPRESSED_BYTES:,}",
                )
            members = [m.filename for m in infos]
            for member in members:
                if not member or member.endswith("/"):
                    continue
                mp = Path(member)
                if mp.is_absolute():
                    raise HTTPException(400, f"unsafe zip member (absolute): {member}")
                final = (target / member).resolve()
                try:
                    final.relative_to(target_resolved)
                except ValueError:
                    raise HTTPException(400, f"unsafe zip member (escapes target): {member}")
            zf.extractall(target)
            return sum(1 for m in members if m and not m.endswith("/"))
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a valid zip file")


def _zip_content_hash(zip_bytes: bytes) -> str:
    """sha256 of (sorted relative paths + file bytes) read directly from the
    zip — stable across re-zips with different metadata or compression."""
    h = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = sorted(m for m in zf.namelist() if not m.endswith("/"))
        for member in members:
            h.update(member.encode("utf-8"))
            h.update(b"\0")
            with zf.open(member) as f:
                h.update(f.read())
            h.update(b"\0\0")
    return h.hexdigest()


def _find_existing_corpus_by_hash(
    content_hash: str, allowed_slugs: list[str]
) -> str | None:
    """Find a corpus owned by the caller with matching content hash. Returns
    the slug if found, else None. Scoped to allowed_slugs so users can't
    'discover' another user's corpus by uploading the same content."""
    for slug in allowed_slugs:
        meta_path = CORPORA_ROOT / slug / "_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("content_hash") == content_hash:
            return slug
    return None


@app.post("/import/notes")
async def import_notes(
    file: UploadFile = File(...),
    email: str = Depends(get_auth),
):
    """Accept a zip of notes, extract into _corpora/<new-slug>/notes/.
    Attaches the new slug to the authenticated user's account.

    If the user already owns a corpus with the same content hash, returns
    that existing slug (with duplicate=true) instead of creating a new one.
    Dedup is scoped per-user so different users uploading identical content
    do not end up sharing a corpus."""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "expected a .zip file")
    # Streaming size cap — abort if the upload exceeds MAX_UPLOAD_BYTES
    # without loading the whole thing into memory first.
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    413,
                    f"upload exceeds {MAX_UPLOAD_BYTES:,} bytes",
                )
            chunks.append(chunk)
    finally:
        await file.close()
    content = b"".join(chunks)

    try:
        content_hash = _zip_content_hash(content)
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a valid zip file")

    user_corpora = _load_auth()["users"].get(email, [])
    existing_slug = _find_existing_corpus_by_hash(content_hash, user_corpora)
    if existing_slug:
        notes_dir = CORPORA_ROOT / existing_slug / "notes"
        note_count = (
            sum(1 for p in notes_dir.rglob("*") if p.is_file())
            if notes_dir.exists()
            else 0
        )
        return {
            "slug": existing_slug,
            "note_count": note_count,
            "duplicate": True,
        }

    slug = make_slug()
    target = CORPORA_ROOT / slug / "notes"
    try:
        note_count = _extract_zip_safe(content, target)
    except HTTPException:
        # Clean up partial extraction.
        shutil.rmtree(CORPORA_ROOT / slug, ignore_errors=True)
        raise

    meta = {
        "content_hash": content_hash,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "owner": email,
    }
    (CORPORA_ROOT / slug / "_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    _attach_corpus_to_user(email, slug)

    return {"slug": slug, "note_count": note_count, "duplicate": False}


@app.post("/import/eras")
async def import_eras(
    file: UploadFile = File(...),
    session: str = Depends(require_corpus_access),
):
    """Accept an eras.yaml for the session's corpus."""
    if session == LEGACY_SESSION:
        raise HTTPException(403, "cannot replace the legacy corpus's eras through the app")
    cdir = corpus_dir(session)
    try:
        content = await file.read()
    finally:
        await file.close()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "eras.yaml must be utf-8 encoded")
    data = _validate_eras_yaml(text)
    cfg_dir = cdir / "_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "eras.yaml").write_text(text, encoding="utf-8")
    return {"ok": True, "era_count": len(data)}


@app.get("/corpus")
def get_corpus(session: str = Depends(require_corpus_access)):
    """Return current corpus state for the session — used at page load to
    decide whether to show import flow, imported view, or legacy app."""
    cdir = corpus_dir(session)
    notes_dir = cdir / "notes"
    note_count = (
        sum(1 for p in notes_dir.rglob("*") if p.is_file())
        if notes_dir.exists()
        else 0
    )
    eras_yaml_path = cdir / "_config" / "eras.yaml"
    has_eras = eras_yaml_path.exists()
    eras: list = []
    if has_eras:
        try:
            loaded = yaml.safe_load(eras_yaml_path.read_text(encoding="utf-8"))
            eras = loaded if isinstance(loaded, list) else []
        except Exception:
            eras = []
    return {
        "slug": session,
        "is_legacy": session == LEGACY_SESSION,
        "note_count": note_count,
        "has_eras": has_eras,
        "eras": eras,
    }


@app.post("/corpus/wipe")
def wipe_corpus(
    session: str = Depends(require_corpus_access),
    auth_email: str | None = Depends(get_auth_optional),
):
    """Hard-delete the session's corpus dir. Legacy is never wipeable here."""
    if session == LEGACY_SESSION:
        raise HTTPException(403, "cannot wipe the legacy corpus through the app")
    cdir = corpus_dir(session)
    shutil.rmtree(cdir)
    if auth_email:
        _detach_corpus_from_user(auth_email, session)
    return {"ok": True}
