"""Multi-tenant zip import + corpus wipe.

`/import/notes` accepts a zip of notes, dedups by content hash within the
caller's owned corpora, attaches a fresh slug to the user. `/import/eras`
swaps in a new eras.yaml. `/corpus/wipe` hard-deletes the session's
corpus and detaches it from the user.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from api import config
from api.auth import (
    _attach_corpus_to_user,
    _detach_corpus_from_user,
    _load_auth,
    get_auth,
    get_auth_optional,
)
from api.config import MAX_UNCOMPRESSED_BYTES, MAX_UPLOAD_BYTES
from api.corpora import corpus_dir, make_slug, require_writable
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

import yaml

router = APIRouter()


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
    """sha256 of (sorted relative paths + file bytes) read directly from
    the zip — stable across re-zips with different metadata or compression."""
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
    """Find a corpus owned by the caller with matching content hash.
    Returns the slug if found, else None. Scoped to allowed_slugs so
    users can't 'discover' another user's corpus by uploading the same
    content."""
    for slug in allowed_slugs:
        meta_path = config.CORPORA_ROOT / slug / "_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("content_hash") == content_hash:
            return slug
    return None


@router.post("/import/notes")
async def import_notes(
    file: UploadFile = File(...),
    email: str = Depends(get_auth),
):
    """Accept a zip of notes, extract into _corpora/<new-slug>/notes/.
    Attaches the new slug to the authenticated user's account.

    If the user already owns a corpus with the same content hash, returns
    that existing slug (with duplicate=true) instead of creating a new
    one. Dedup is scoped per-user so different users uploading identical
    content do not end up sharing a corpus."""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "expected a .zip file")
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
        notes_dir = config.CORPORA_ROOT / existing_slug / "notes"
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
    target = config.CORPORA_ROOT / slug / "notes"
    try:
        note_count = _extract_zip_safe(content, target)
    except HTTPException:
        shutil.rmtree(config.CORPORA_ROOT / slug, ignore_errors=True)
        raise

    meta = {
        "content_hash": content_hash,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "owner": email,
    }
    (config.CORPORA_ROOT / slug / "_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    _attach_corpus_to_user(email, slug)

    return {"slug": slug, "note_count": note_count, "duplicate": False}


@router.post("/import/eras")
async def import_eras(
    file: UploadFile = File(...),
    session: str = Depends(require_writable),
):
    """Accept an eras.yaml for the session's corpus."""
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


@router.post("/corpus/wipe")
def wipe_corpus(
    session: str = Depends(require_writable),
    auth_email: str | None = Depends(get_auth_optional),
):
    """Hard-delete the session's corpus dir."""
    cdir = corpus_dir(session)
    shutil.rmtree(cdir)
    if auth_email:
        _detach_corpus_from_user(auth_email, session)
    return {"ok": True}
