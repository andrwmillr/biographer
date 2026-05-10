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
from dataclasses import dataclass
from pathlib import Path

from api import config
import yaml
from api.auth import _gc_auth, _load_auth, get_auth_optional
from api.config import ADMIN_EMAILS
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from core import corpus as wb
from core.telemetry import log as tlog

router = APIRouter()

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class AccessContext:
    slug: str
    mode: str
    actor_label: str
    can_read: bool
    can_write: bool
    can_compute: bool
    can_promote: bool
    can_delete: bool
    can_rename: bool

    def capabilities(self) -> dict:
        return {
            "mode": self.mode,
            "can_read": self.can_read,
            "can_write": self.can_write,
            "can_compute": self.can_compute,
            "can_promote": self.can_promote,
            "can_delete": self.can_delete,
            "can_rename": self.can_rename,
        }


def get_session(x_corpus_session: str | None = Header(None)) -> str:
    if not x_corpus_session:
        raise HTTPException(401, "missing X-Corpus-Session header")
    return x_corpus_session


def require_admin(email: str | None = Depends(get_auth_optional)) -> str:
    if email is None or email not in ADMIN_EMAILS:
        raise HTTPException(403, "admin endpoint")
    return email


def _valid_slug(slug: str) -> bool:
    """Accept only c_-namespaced imported/sample corpus slugs.

    The public web API deliberately does not accept legacy local corpus
    names such as "andrew". Local CLI tooling may still target those
    directories explicitly through core.corpus, but internet-facing routes
    must not expose them through X-Corpus-Session.
    """
    return bool(re.fullmatch(r"c_[0-9a-z_]+", slug))


def is_sample_corpus(slug: str) -> bool:
    """A corpus is a sample if its `_meta.json` has `"sample": true`.
    Sample corpora are readable without auth (so visitors can explore
    famous PD diaries before importing their own notes) but stay
    write-locked through require_writable."""
    if not _valid_slug(slug):
        return False
    meta_path = config.CORPORA_ROOT / slug / "_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(meta.get("sample"))


def _load_corpus_meta(slug: str) -> dict:
    if not _valid_slug(slug):
        return {}
    meta_path = config.CORPORA_ROOT / slug / "_meta.json"
    if not meta_path.exists():
        return {}
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _get_corpus_secret(slug: str) -> str | None:
    """Return the secret key for a corpus if one is configured, else None."""
    if not _valid_slug(slug):
        return None
    meta = _load_corpus_meta(slug)
    return meta.get("secret") or None


def _owner_access(slug: str, email: str) -> AccessContext:
    return AccessContext(
        slug=slug,
        mode="owner",
        actor_label=email,
        can_read=True,
        can_write=True,
        can_compute=True,
        can_promote=True,
        can_delete=True,
        can_rename=True,
    )


def _read_only_access(slug: str, mode: str) -> AccessContext:
    return AccessContext(
        slug=slug,
        mode=mode,
        actor_label=f"({mode})",
        can_read=True,
        can_write=False,
        can_compute=True,
        can_promote=False,
        can_delete=False,
        can_rename=False,
    )


def resolve_corpus_access(
    session: str,
    auth_email: str | None = None,
    corpus_secret: str | None = None,
) -> AccessContext:
    """Resolve the request into a single capability set.

    Owner auth wins; otherwise samples and matching secret corpora are
    read/compute-only public trial surfaces. Non-owned private corpora remain
    invisible without the right auth path.
    """
    corpus_dir(session)
    state = _load_auth()
    if auth_email and session in state["users"].get(auth_email, []):
        return _owner_access(session, auth_email)
    if is_sample_corpus(session):
        return _read_only_access(session, "sample")
    secret = _get_corpus_secret(session)
    if secret:
        if corpus_secret == secret:
            return _read_only_access(session, "secret")
        raise HTTPException(404, "not found")
    if auth_email is None:
        raise HTTPException(401, "auth required: missing or invalid X-Auth-Token")
    raise HTTPException(403, "this corpus is not owned by the authenticated user")


def require_capability(access: AccessContext, capability: str) -> str:
    if not getattr(access, capability, False):
        if capability == "can_write" and access.mode in ("sample", "secret"):
            raise HTTPException(403, f"{access.mode} corpora are read-only")
        raise HTTPException(403, f"corpus does not allow {capability}")
    return access.slug


def require_corpus_access(
    session: str = Depends(get_session),
    auth_email: str | None = Depends(get_auth_optional),
    x_corpus_secret: str | None = Header(None),
) -> str:
    """Require read access to the corpus."""
    access = resolve_corpus_access(session, auth_email, x_corpus_secret)
    return require_capability(access, "can_read")


def require_access_context(
    session: str = Depends(get_session),
    auth_email: str | None = Depends(get_auth_optional),
    x_corpus_secret: str | None = Header(None),
) -> AccessContext:
    access = resolve_corpus_access(session, auth_email, x_corpus_secret)
    require_capability(access, "can_read")
    return access


def _owner_email_for_token(session: str, auth_token: str | None) -> str | None:
    if not auth_token:
        return None
    state = _gc_auth(_load_auth())
    record = state["sessions"].get(auth_token)
    if not record:
        return None
    email = record["email"]
    if session not in state["users"].get(email, []):
        return None
    return email


def resolve_ws_access(
    session: str,
    auth_token: str | None,
    corpus_secret: str | None = None,
) -> AccessContext:
    """Validate WebSocket start access.

    Public samples and matching secret-backed corpora can start trial compute
    sessions, but they cannot promote shared/canonical files. Authenticated
    owners can both compute and persist.
    """
    corpus_dir(session)
    owner_email = _owner_email_for_token(session, auth_token)
    if owner_email:
        access = _owner_access(session, owner_email)
    else:
        access = resolve_corpus_access(session, None, corpus_secret)
    require_capability(access, "can_compute")
    return access


def require_writable(
    session: str = Depends(get_session),
    auth_email: str | None = Depends(get_auth_optional),
    x_corpus_secret: str | None = Header(None),
) -> str:
    """Gate destructive / compute-spending operations to corpus owners."""
    access = resolve_corpus_access(session, auth_email, x_corpus_secret)
    return require_capability(access, "can_write")


def corpus_dir(session: str) -> Path:
    """Resolve a session string to its on-disk corpus directory.
    Raises 401 for invalid / nonexistent sessions."""
    if not _valid_slug(session):
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


def _load_state(corpus_id: str):
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


def _note_source(rel: str, corpus_id: str) -> str:
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
            "written_at": (
                wb.canonical_written_at(corpus_id, chapter_path, wb.era_slug(name))
                if chapter_path.exists()
                else None
            ),
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
    slug = wb.era_slug(era)
    return {
        "content": chapter_path.read_text(encoding="utf-8"),
        "written_at": wb.canonical_written_at(corpus_id, chapter_path, slug),
    }


@router.get("/notes")
def list_notes(era: str, session: str = Depends(require_corpus_access)):
    from api.commonplace import load_highlighted_rels
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, _ = _load_state(corpus_id)
    if era not in by_era:
        raise HTTPException(404, f"unknown era: {era}")
    highlighted = load_highlighted_rels(corpus_id)
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
            "highlighted": rel in highlighted,
        }
        if n.get("editor_note"):
            item["editor_note"] = n["editor_note"]
        out.append(item)
    return out


@router.get("/notes/all")
def list_all_notes(top_n: int = 5, session: str = Depends(require_corpus_access)):
    """Return every note across all eras, sorted chronologically.
    Each note includes `sampled: true` when it falls in the folder-aware
    top-N selection used by the themes flow — so the UI can highlight
    which notes the agent actually reads."""
    from api.commonplace import load_highlighted_rels
    from core.sampling import folder_aware_sample
    corpus_id = _session_corpus_id(session)
    corpus_dir(session)
    _, by_era, eras = _load_state(corpus_id)

    # Build the set of sampled rels (mirrors themes.py's selection)
    sampled_rels: set[str] = set()
    for era_name, _, _ in eras:
        era_notes = by_era.get(era_name, [])
        if era_notes:
            for n in folder_aware_sample(era_notes, top_n, corpus_id):
                sampled_rels.add(n["rel"])

    highlighted = load_highlighted_rels(corpus_id)

    # Flatten all eras, chronological
    all_notes = []
    for era_name, _, _ in eras:
        all_notes.extend(by_era.get(era_name, []))
    all_notes.sort(key=lambda n: n.get("date", ""))

    out = []
    for n in all_notes:
        rel = n["rel"]
        item = {
            "rel": rel,
            "date": n.get("date", ""),
            "title": n.get("title", ""),
            "label": rel.split("/", 1)[0] if "/" in rel else "",
            "source": _note_source(rel, corpus_id),
            "body": wb.parse_note_body(rel, corpus_id),
            "sampled": rel in sampled_rels,
            "highlighted": rel in highlighted,
        }
        if n.get("editor_note"):
            item["editor_note"] = n["editor_note"]
        out.append(item)
    return out


@router.get("/samples")
def list_samples(request: Request, x_corpus_secret: str | None = Header(None)):
    """List all sample corpora (any `_corpora/<slug>/_meta.json` with
    `"sample": true`). Open without auth — visitors can pick a sample
    slug to set as their corpusSession and browse it read-only.
    Also includes secret-protected corpora when the matching secret is
    provided via X-Corpus-Secret header."""
    ip = request.headers.get("cf-connecting-ip") or request.client.host
    tlog("page_view", page="samples", ip=ip)
    out = []
    if not config.CORPORA_ROOT.exists():
        return out
    for d in sorted(config.CORPORA_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if not _valid_slug(d.name):
            continue
        meta_path = d / "_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Include if: sample corpus, OR secret-protected with matching key
        is_sample = bool(meta.get("sample"))
        corpus_secret = meta.get("secret")
        if not is_sample:
            if not corpus_secret or x_corpus_secret != corpus_secret:
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
def get_corpus(access: AccessContext = Depends(require_access_context)):
    """Return current corpus state for the session — used at page load to
    decide whether to show import flow, imported view, or sample view."""
    session = access.slug
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
        "is_sample": access.mode in ("sample", "secret"),
        "access": access.capabilities(),
        "note_count": note_count,
        "has_eras": has_eras,
        "eras": eras,
    }


class RenameCorpusRequest(BaseModel):
    title: str | None


@router.patch("/corpus")
def rename_corpus(req: RenameCorpusRequest, session: str = Depends(require_writable)):
    """Update the corpus's display title in `_meta.json`. Pass `null` (or
    an empty string) to clear it; the picker / header tag fall back to
    the slug when no title is set. Samples are read-only via
    require_writable."""
    cdir = corpus_dir(session)
    meta_path = cdir / "_meta.json"
    title = (req.title or "").strip() or None
    if title and len(title) > 200:
        raise HTTPException(400, "title too long (max 200 chars)")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    else:
        meta = {}
    if title is None:
        meta.pop("title", None)
    else:
        meta["title"] = title
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return {"slug": session, "title": title}
