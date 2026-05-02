"""Magic-link auth: email a one-time token, exchange it for a long-lived
session token, persist state in `_auth/state.json`. Multi-tenant — an
email can own multiple corpus slugs.

Mutable paths live in `config` so tests can redirect the on-disk state
(see config.AUTH_STATE_PATH). Helpers read `config.AUTH_STATE_PATH` at
call time, so monkey-patching `config.AUTH_STATE_PATH` after import works.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from api import config
from api.config import ALLOWED_ORIGINS, AUTH_TOKEN_TTL, EMAIL_RE, MAGIC_TOKEN_TTL
from core.telemetry import log as tlog
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

router = APIRouter()


def _now_ts() -> int:
    return int(datetime.now().timestamp())


def _empty_auth_state() -> dict:
    return {"users": {}, "sessions": {}, "pending": {}}


def _load_auth() -> dict:
    if not config.AUTH_STATE_PATH.exists():
        return _empty_auth_state()
    try:
        return json.loads(config.AUTH_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_auth_state()


def _save_auth(state: dict) -> None:
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.AUTH_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(config.AUTH_STATE_PATH)


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


@router.post("/auth/request", status_code=204)
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


@router.get("/auth/verify")
def auth_verify(token: str):
    state = _gc_auth(_load_auth())
    record = state["pending"].get(token)
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
    tlog("auth_login", email=email)
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
    """Like get_auth, but returns None for missing/invalid tokens instead
    of raising. Used by require_corpus_access so anonymous visitors can
    still pass through to sample corpora."""
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


@router.get("/auth/me")
def auth_me(email: str = Depends(get_auth)):
    state = _load_auth()
    slugs = state["users"].get(email, [])
    corpora = []
    for slug in slugs:
        meta_path = config.CORPORA_ROOT / slug / "_meta.json"
        title: str | None = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = (meta.get("title") or "").strip() or None
            except Exception:
                pass
        corpora.append({"slug": slug, "title": title})
    return {"email": email, "corpora": corpora}


@router.post("/auth/logout", status_code=204)
def auth_logout(x_auth_token: str | None = Header(None)):
    if not x_auth_token:
        return
    state = _load_auth()
    state["sessions"].pop(x_auth_token, None)
    _save_auth(state)


@router.post("/auth/delete-account")
def auth_delete_account(email: str = Depends(get_auth)):
    """Irreversibly delete every trace of the authenticated user:
    every owned corpus directory, the user's auth record, all of their
    active sessions, and any pending magic-link tokens issued to them.

    This is the GDPR right-to-erasure endpoint — no soft-delete, no grace
    period. Returns a count summary so the client can confirm the scope
    of what was removed."""
    import shutil

    state = _load_auth()
    owned_slugs = list(state["users"].get(email, []))

    # 1. Delete each corpus directory tree.
    deleted = 0
    for slug in owned_slugs:
        corpus_path = config.CORPORA_ROOT / slug
        if corpus_path.exists():
            shutil.rmtree(corpus_path, ignore_errors=True)
            deleted += 1

    # 2. Drop the user's record (their list of owned slugs).
    state["users"].pop(email, None)

    # 3. Invalidate every session belonging to this email.
    sessions_killed = 0
    for token, record in list(state["sessions"].items()):
        if record.get("email") == email:
            state["sessions"].pop(token, None)
            sessions_killed += 1

    # 4. Clear any pending magic-link tokens issued to this email.
    pending_killed = 0
    for token, record in list(state["pending"].items()):
        if record.get("email") == email:
            state["pending"].pop(token, None)
            pending_killed += 1

    _save_auth(state)
    return {
        "corpora_deleted": deleted,
        "sessions_invalidated": sessions_killed,
        "pending_links_invalidated": pending_killed,
    }
