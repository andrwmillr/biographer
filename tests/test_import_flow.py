"""End-to-end smoke test for the multi-tenant import → eras flow,
plus the auth-bypass guard on `corpus_dir`. The single test most likely
to catch regressions in the multi-tenant code path.

Run from _web/:
    uv run --with 'fastapi[standard]' --with anthropic --with pyyaml \
        --with claude-agent-sdk --with pytest --with httpx \
        python -m pytest tests/ -v
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Importing server requires LEGACY_SESSION in env (set by conftest.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "_scripts"))
import server  # noqa: E402
import write_biography as wb  # noqa: E402


# ---- Test isolation: redirect both CORPORA_ROOT (server.py) AND
# _CORPORA_BASE (wb) to a temp dir so reads/writes see the same tree.

_test_corpora_root = Path(tempfile.mkdtemp(prefix="biographer_test_"))
server.CORPORA_ROOT = _test_corpora_root
wb._CORPORA_BASE = _test_corpora_root


def teardown_module(_):
    shutil.rmtree(_test_corpora_root, ignore_errors=True)


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


# ---- Auth: corpus_dir guard -----------------------------------------------


def test_corpus_dir_rejects_andrew_bypass():
    """Critical regression test for the slug-as-Andrew bypass."""
    with pytest.raises(HTTPException) as ex:
        server.corpus_dir("andrew")
    assert ex.value.status_code == 401


def test_corpus_dir_rejects_random_strings():
    for bad in ["foo", "c_short", "c_TOOSHORT", "../etc", "", "_other_legacy"]:
        with pytest.raises(HTTPException) as ex:
            server.corpus_dir(bad)
        assert ex.value.status_code == 401, f"expected 401 for {bad!r}"


def test_corpus_dir_accepts_legacy_session():
    p = server.corpus_dir(server.LEGACY_SESSION)
    # Legacy maps to the host's andrew dir (lives outside the test temp dir).
    assert p.name == "andrew"


# ---- End-to-end: import zip → upload eras → /eras returns the right counts


def _build_zip(name_to_body: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in name_to_body.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_import_zip_then_eras_then_eras_endpoint(client: TestClient):
    zip_bytes = _build_zip({
        "2018-09-15.md": "Boston entry one. Walking in the rain.",
        "2019-03-20.md": "Boston entry two.",
        "2020-11-01.md": "New York entry one.",
        "2021-04-10.md": "New York entry two.",
    })

    # 1. Upload notes — establishes session.
    r = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["note_count"] == 4
    slug = data["slug"]
    assert slug.startswith("c_")
    assert data["duplicate"] is False

    # 2. Re-uploading same content dedups to same slug.
    r2 = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
    )
    assert r2.status_code == 200
    assert r2.json()["slug"] == slug
    assert r2.json()["duplicate"] is True

    # 3. Upload eras yaml.
    eras_yaml = b"""\
- name: Pre-NYC
  start: 2018-01
- name: NYC
  start: 2020-01
"""
    r = client.post(
        "/import/eras",
        files={"file": ("eras.yaml", eras_yaml, "text/yaml")},
        headers={"X-Corpus-Session": slug},
    )
    assert r.status_code == 200, r.text
    assert r.json()["era_count"] == 2

    # 4. GET /eras — assert era boundaries pulled from yaml + note counts
    #    pulled from this corpus's filesystem (not Andrew's).
    r = client.get("/eras", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200, r.text
    eras = r.json()
    assert len(eras) == 2
    by_name = {e["name"]: e for e in eras}
    # 2018-09-15 + 2019-03-20 fall in Pre-NYC (start 2018-01, end 2019-12).
    assert by_name["Pre-NYC"]["note_count"] == 2
    # 2020-11-01 + 2021-04-10 fall in NYC.
    assert by_name["NYC"]["note_count"] == 2

    # 5. /corpus shows the right slug + has_eras true.
    r = client.get("/corpus", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200
    info = r.json()
    assert info["slug"] == slug
    assert info["is_legacy"] is False
    assert info["has_eras"] is True
    assert len(info["eras"]) == 2

    # 6. Wipe.
    r = client.post("/corpus/wipe", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200
    assert (_test_corpora_root / slug).exists() is False

    # 7. After wipe, the session is invalid.
    r = client.get("/eras", headers={"X-Corpus-Session": slug})
    assert r.status_code == 401


def test_import_eras_requires_session(client: TestClient):
    r = client.post(
        "/import/eras",
        files={"file": ("eras.yaml", b"- name: x\n  start: 2020-01\n", "text/yaml")},
    )
    assert r.status_code == 401


def test_eras_requires_session(client: TestClient):
    r = client.get("/eras")
    assert r.status_code == 401


def test_oversized_zip_rejected(client: TestClient):
    """Zip-bomb defense: huge uncompressed total is rejected before extract."""
    # Build a pathological zip where one member declares enormous file_size.
    # We compress a long string of zeros to test the uncompressed-size guard.
    big_body = b"\0" * (server.MAX_UNCOMPRESSED_BYTES + 1024)
    zip_bytes = _build_zip({"huge.md": big_body.decode("latin-1")})
    r = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
    )
    # The raw-upload cap might fire first (50 MB), or the uncompressed cap.
    # Either way, the server must refuse — never let the file land on disk.
    assert r.status_code == 413, r.text
