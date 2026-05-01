"""Multi-tenant corpus access — session→slug resolution, ownership gates,
and the read-only `/eras`, `/notes`, `/corpus`, `/samples` endpoints.

The session header value IS the corpus slug under `_corpora/<slug>/`.
Authenticated users own a list of slugs; sample slugs are readable by
anyone (open demo) but write-locked through `require_writable`. Admin
endpoints gate on `config.ADMIN_EMAILS`.
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from api import config
import yaml
from api.auth import _load_auth, get_auth_optional
from api.config import ADMIN_EMAILS
from fastapi import APIRouter, Depends, Header, HTTPException

from core import corpus as wb

router = APIRouter()

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def get_session(x_corpus_session: str | None = Header(None)) -> str:
    if not x_corpus_session:
        raise HTTPException(401, "missing X-Corpus-Session header")
    return x_corpus_session


def require_admin(email: str | None = Depends(get_auth_optional)) -> str:
    if email is None or email not in ADMIN_EMAILS:
        raise HTTPException(403, "admin endpoint")
    return email


def is_sample_corpus(slug: str) -> bool:
    """A corpus is a sample if its `_meta.json` has `"sample": true`.
    Sample corpora are readable without auth (so visitors can explore
    famous PD diaries before importing their own notes) but stay
    write-locked through require_writable."""
    if not re.fullmatch(r"c_[0-9a-f]{16}", slug):
        return False
    meta_path = config.CORPORA_ROOT / slug / "_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(meta.get("sample"))


def require_corpus_access(
    session: str = Depends(get_session),
    auth_email: str | None = Depends(get_auth_optional),
) -> str:
    """Gate on (auth token, corpus ownership). Sample corpora are
    readable without auth. Every other corpus session requires an
    X-Auth-Token whose user owns the slug."""
    if is_sample_corpus(session):
        return session
    if auth_email is None:
        raise HTTPException(401, "auth required: missing or invalid X-Auth-Token")
    state = _load_auth()
    if session not in state["users"].get(auth_email, []):
        raise HTTPException(403, "this corpus is not owned by the authenticated user")
    return session


def require_writable(session: str = Depends(require_corpus_access)) -> str:
    """Gate destructive / compute-spending operations. Sample corpora are
    read-only — no eras replacement, no wipe. (Drafting on samples is
    explicitly allowed via a bypass in the /session WS handler.)"""
    if is_sample_corpus(session):
        raise HTTPException(403, "sample corpora are read-only")
    return session


def corpus_dir(session: str) -> Path:
    """Resolve a session string to its on-disk corpus directory.
    Raises 401 for invalid / nonexistent sessions."""
    if session != "andrew" and not re.fullmatch(r"c_[0-9a-f]{16}", session):
        raise HTTPException(401, "invalid session")
    candidate = config.CORPORA_ROOT / session
    try:
        candidate.resolve().relative_to(config.CORPORA_ROOT.resolve())
    except (ValueError, RuntimeError):
        raise HTTPException(401, "invalid session")
    if not candidate.is_dir():
        raise HTTPException(401, "session not found (corpus may have been wiped)")
    return candidate


def make_slug() -> str:
    return f"c_{secrets.token_hex(8)}"


def _session_corpus_id(session: str) -> str:
    """Map a session header value to the wb corpus_id. The slug IS the id."""
    return session


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


def _note_source(rel: str, corpus_id: str = "andrew") -> str:
    path = wb._safe_note_path(rel, corpus_id)
    if path is None:
        return ""
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


@router.get("/eras")
def list_eras(session: str = Depends(require_corpus_access)):
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, eras = _load_state(corpus_id)
    chapters = wb.chapters_dir(corpus_id)
    out = []
    for name, start, end in eras:
        chapter_path = chapters / f"{wb.era_slug(name)}.md"
        # Derive the displayed range from the actual notes when possible —
        # YAML boundaries can be sentinels ("0000-00", "9999-99") that
        # render badly in the UI. Fall back to the YAML range when an era
        # has no notes yet.
        notes_in_era = by_era[name]
        actual_lo, actual_hi = wb.era_date_range(notes_in_era)
        display_start = actual_lo or (start if start != "0000-00" else None)
        display_end = actual_hi or (end if end != "9999-99" else None)
        out.append({
            "name": name,
            "start": display_start,
            "end": display_end,
            "note_count": len(notes_in_era),
            "has_chapter": chapter_path.exists(),
        })
    return out


@router.get("/chapters/{era}")
def get_chapter(era: str, session: str = Depends(require_corpus_access)):
    """Return the locked chapter for `era`, if one exists. Used by the
    workspace to populate the draft pane in read mode."""
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    chapter_path = wb.chapters_dir(corpus_id) / f"{wb.era_slug(era)}.md"
    if not chapter_path.exists():
        raise HTTPException(404, f"no chapter for era: {era}")
    return {"content": chapter_path.read_text(encoding="utf-8")}


@router.get("/notes")
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


@router.get("/samples")
def list_samples():
    """List all sample corpora (any `_corpora/<slug>/_meta.json` with
    `"sample": true`). Open without auth — visitors can pick a sample
    slug to set as their corpusSession and browse it read-only."""
    out = []
    if not config.CORPORA_ROOT.exists():
        return out
    for d in sorted(config.CORPORA_ROOT.iterdir()):
        if not d.is_dir() or not is_sample_corpus(d.name):
            continue
        meta_path = d / "_meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        eras_yaml_path = d / "_config" / "eras.yaml"
        era_count = 0
        if eras_yaml_path.exists():
            try:
                loaded = yaml.safe_load(eras_yaml_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    era_count = len(loaded)
            except Exception:
                pass
        notes_dir = d / "notes"
        note_count = (
            sum(1 for p in notes_dir.rglob("*") if p.is_file())
            if notes_dir.exists()
            else 0
        )
        out.append({
            "slug": d.name,
            "title": meta.get("title") or d.name,
            "description": meta.get("description") or "",
            "source": meta.get("source") or "",
            "note_count": note_count,
            "era_count": era_count,
        })
    out.sort(key=lambda x: x["title"].lower())
    return out


@router.get("/corpus")
def get_corpus(session: str = Depends(require_corpus_access)):
    """Return current corpus state for the session — used at page load to
    decide whether to show import flow, imported view, or sample view."""
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
    title: str | None = None
    meta_path = cdir / "_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = (meta.get("title") or "").strip() or None
        except Exception:
            pass
    return {
        "slug": session,
        "title": title,
        "is_sample": is_sample_corpus(session),
        "note_count": note_count,
        "has_eras": has_eras,
        "eras": eras,
    }
