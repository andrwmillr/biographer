"""Spotify integration: OAuth flow + era-based playlist filtering.

Stores per-user refresh tokens in _auth/spotify_tokens.json and
playlist date estimates in _auth/spotify_playlist_cache.json.
Uses the Spotify Web API to list playlists, estimating each playlist's
era from the median album release date of its tracks.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlencode

from api import config
from api.config import ALLOWED_ORIGINS
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/spotify")

TOKENS_PATH = config.AUTH_DIR / "spotify_tokens.json"
CACHE_PATH = config.AUTH_DIR / "spotify_playlist_cache.json"
SCOPES = "playlist-read-private playlist-read-collaborative"

# In-memory pending OAuth states (short-lived, keyed by random state param)
_pending_oauth: dict[str, dict] = {}


# ---- Helpers ----

def _client_id() -> str:
    v = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not v:
        raise HTTPException(500, "SPOTIFY_CLIENT_ID not configured")
    return v


def _client_secret() -> str:
    v = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not v:
        raise HTTPException(500, "SPOTIFY_CLIENT_SECRET not configured")
    return v


def _redirect_uri() -> str:
    return os.environ.get(
        "SPOTIFY_REDIRECT_URI", "http://localhost:8000/spotify/callback"
    )


def _load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_tokens(data: dict) -> None:
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(TOKENS_PATH)



def _get_email(
    x_corpus_session: str = Header(None, alias="X-Corpus-Session"),
    x_auth_token: str = Header(None, alias="X-Auth-Token"),
) -> str:
    from api.auth import _load_auth as load_auth, _gc_auth
    if not x_auth_token:
        raise HTTPException(401, "missing auth token")
    state = _gc_auth(load_auth())
    record = state["sessions"].get(x_auth_token)
    if not record:
        raise HTTPException(401, "invalid auth token")
    return record["email"]


def _ensure_fresh_token(email: str) -> str | None:
    """Return a valid access token for the user, refreshing if needed."""
    tokens = _load_tokens()
    record = tokens.get(email)
    if not record:
        return None

    if record.get("expires_at", 0) > time.time() + 60:
        return record["access_token"]

    refresh = record.get("refresh_token")
    if not refresh:
        return None

    data = urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }).encode()

    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return None

    record["access_token"] = body["access_token"]
    record["expires_at"] = int(time.time()) + body.get("expires_in", 3600)
    if "refresh_token" in body:
        record["refresh_token"] = body["refresh_token"]
    tokens[email] = record
    _save_tokens(tokens)
    return record["access_token"]


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cache(data: dict) -> None:
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _spotify_get(token: str, url: str) -> tuple[dict | None, bool]:
    """Fetch from Spotify API. Returns (data, success) — success=False means
    the request failed (rate limit, network error) vs. success=True with data."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), True
    except urllib.error.HTTPError as e:
        print(f"[spotify] HTTP {e.code} for {url}")
        return None, False
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"[spotify] error for {url}: {e}")
        return None, False


# ---- Endpoints ----

@router.get("/connect")
def connect(token: str = Query("")):
    """Redirect to Spotify authorization page."""
    if not token:
        raise HTTPException(401, "missing auth token")
    from api.auth import _load_auth, _gc_auth
    auth_state = _gc_auth(_load_auth())
    record = auth_state["sessions"].get(token)
    if not record:
        raise HTTPException(401, "invalid auth token")
    email = record["email"]
    state = secrets.token_urlsafe(24)
    _pending_oauth[state] = {"email": email, "ts": time.time()}
    cutoff = time.time() - 900
    for k in list(_pending_oauth):
        if _pending_oauth[k]["ts"] < cutoff:
            del _pending_oauth[k]

    params = urlencode({
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": SCOPES,
        "state": state,
    })
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@router.get("/callback")
def callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"{ALLOWED_ORIGINS[0]}/#spotify=error&msg={error}")

    pending = _pending_oauth.pop(state, None)
    if not pending:
        return RedirectResponse(f"{ALLOWED_ORIGINS[0]}/#spotify=error&msg=invalid_state")

    email = pending["email"]
    data = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }).encode()

    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError):
        return RedirectResponse(
            f"{ALLOWED_ORIGINS[0]}/#spotify=error&msg=token_exchange_failed"
        )

    tokens = _load_tokens()
    tokens[email] = {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", ""),
        "expires_at": int(time.time()) + body.get("expires_in", 3600),
        "scope": body.get("scope", ""),
    }
    _save_tokens(tokens)
    return RedirectResponse(f"{ALLOWED_ORIGINS[0]}/#spotify=connected")


@router.get("/status")
def status(email: str = Depends(_get_email)):
    tokens = _load_tokens()
    record = tokens.get(email)
    connected = bool(record and record.get("refresh_token"))
    return {"connected": connected}


@router.post("/disconnect")
def disconnect(email: str = Depends(_get_email)):
    tokens = _load_tokens()
    tokens.pop(email, None)
    _save_tokens(tokens)
    return {"ok": True}


@router.get("/playlists")
def playlists(email: str = Depends(_get_email)):
    """Return all user playlists, sorted alphabetically."""
    token = _ensure_fresh_token(email)
    if not token:
        raise HTTPException(401, "spotify not connected")

    data, ok = _spotify_get(token, "https://api.spotify.com/v1/me/playlists?limit=50")
    if not data or "items" not in data:
        raise HTTPException(502, "failed to fetch playlists from Spotify")

    results = []
    for pl in data["items"]:
        if not pl or not pl.get("id"):
            continue
        results.append({
            "id": pl["id"],
            "name": pl.get("name", "") or "Untitled",
            "image": (pl.get("images") or [{}])[0].get("url", ""),
            "uri": pl.get("uri", ""),
            "external_url": pl.get("external_urls", {}).get("spotify", ""),
        })

    results.sort(key=lambda x: x["name"].lower())
    return {"playlists": results}


def _parse_ym(ym: str) -> datetime | None:
    if not ym or ym == "9999-99":
        return None
    try:
        parts = ym.split("-")
        return datetime(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        return None


_YEAR_RE = re.compile(r"(?:^|[\s'(])(\d{4})(?:[\s')]|$)")


def _year_from_name(name: str) -> int | None:
    """Extract a plausible year (1990–2030) from a playlist name."""
    for m in _YEAR_RE.finditer(name):
        y = int(m.group(1))
        if 1990 <= y <= 2030:
            return y
    return None


def _estimate_playlist_date(name: str) -> datetime | None:
    """Extract a year from the playlist name. This is the only reliable
    signal — added_at timestamps reset on account migration and album
    release dates reflect when the music was made, not when the user
    was listening. Returns None for playlists without a year in the name."""
    y = _year_from_name(name)
    return datetime(y, 6, 15) if y else None


