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

# conftest.py sets ADMIN_EMAILS in env before this module imports server.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import auth  # noqa: E402
import config  # noqa: E402
import corpora  # noqa: E402
import server  # noqa: E402
import corpus as wb  # noqa: E402


# ---- Test isolation: redirect both config.CORPORA_ROOT (server) AND
# wb._CORPORA_BASE to a temp dir so reads/writes see the same tree.
# Also redirect config.AUTH_STATE_PATH so test auth state doesn't clobber
# the host's real ~/_auth/state.json. Helpers in auth.py read these via
# config attribute access, so post-import mutation propagates correctly.

_test_corpora_root = Path(tempfile.mkdtemp(prefix="biographer_test_"))
_test_auth_dir = Path(tempfile.mkdtemp(prefix="biographer_test_auth_"))
config.CORPORA_ROOT = _test_corpora_root
config.AUTH_DIR = _test_auth_dir
config.AUTH_STATE_PATH = _test_auth_dir / "state.json"
wb._CORPORA_BASE = _test_corpora_root


def teardown_module(_):
    shutil.rmtree(_test_corpora_root, ignore_errors=True)
    shutil.rmtree(_test_auth_dir, ignore_errors=True)


def _issue_test_token(email: str = "test@example.com") -> str:
    """Inject a valid auth session into the test auth state and return its
    token. Bypasses the email-magic-link round-trip; the on-disk record is
    indistinguishable from one issued via /auth/verify."""
    import json as _json
    import secrets as _secrets

    token = _secrets.token_urlsafe(32)
    state = auth._load_auth()
    state["sessions"][token] = {
        "email": email,
        "expires": auth._now_ts() + 3600,
    }
    state["users"].setdefault(email, [])
    auth._save_auth(state)
    return token


@pytest.fixture
def client() -> TestClient:
    return TestClient(server.app)


@pytest.fixture
def auth_token() -> str:
    return _issue_test_token()


# ---- Auth: corpus_dir guard -----------------------------------------------


def test_corpus_dir_rejects_missing_andrew_dir():
    """`andrew` passes the slug-shape gate but must 401 when the dir doesn't
    exist on disk (it doesn't, in the test tempdir)."""
    with pytest.raises(HTTPException) as ex:
        corpora.corpus_dir("andrew")
    assert ex.value.status_code == 401


def test_corpus_dir_rejects_random_strings():
    for bad in ["foo", "c_short", "c_TOOSHORT", "../etc", "", "_other_legacy"]:
        with pytest.raises(HTTPException) as ex:
            corpora.corpus_dir(bad)
        assert ex.value.status_code == 401, f"expected 401 for {bad!r}"


# ---- End-to-end: import zip → upload eras → /eras returns the right counts


def _build_zip(name_to_body: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in name_to_body.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_import_zip_then_eras_then_eras_endpoint(client: TestClient, auth_token: str):
    zip_bytes = _build_zip({
        "2018-09-15.md": "Boston entry one. Walking in the rain.",
        "2019-03-20.md": "Boston entry two.",
        "2020-11-01.md": "New York entry one.",
        "2021-04-10.md": "New York entry two.",
    })
    auth = {"X-Auth-Token": auth_token}

    # 1. Upload notes — establishes session.
    r = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
        headers=auth,
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
        headers=auth,
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
        headers={"X-Corpus-Session": slug, **auth},
    )
    assert r.status_code == 200, r.text
    assert r.json()["era_count"] == 2

    # 4. GET /eras — assert era boundaries pulled from yaml + note counts
    #    pulled from this corpus's filesystem (not Andrew's).
    r = client.get("/eras", headers={"X-Corpus-Session": slug, **auth})
    assert r.status_code == 200, r.text
    eras = r.json()
    assert len(eras) == 2
    by_name = {e["name"]: e for e in eras}
    # 2018-09-15 + 2019-03-20 fall in Pre-NYC (start 2018-01, end 2019-12).
    assert by_name["Pre-NYC"]["note_count"] == 2
    # 2020-11-01 + 2021-04-10 fall in NYC.
    assert by_name["NYC"]["note_count"] == 2

    # 5. /corpus shows the right slug + has_eras true.
    r = client.get("/corpus", headers={"X-Corpus-Session": slug, **auth})
    assert r.status_code == 200
    info = r.json()
    assert info["slug"] == slug
    assert info["has_eras"] is True
    assert len(info["eras"]) == 2

    # 6. Wipe.
    r = client.post("/corpus/wipe", headers={"X-Corpus-Session": slug, **auth})
    assert r.status_code == 200
    assert (_test_corpora_root / slug).exists() is False

    # 7. After wipe, the slug is detached from the user — auth gate fires
    #    with 403 (not-owned) before we ever try to resolve the dir.
    r = client.get("/eras", headers={"X-Corpus-Session": slug, **auth})
    assert r.status_code == 403


def test_import_eras_requires_session(client: TestClient, auth_token: str):
    r = client.post(
        "/import/eras",
        files={"file": ("eras.yaml", b"- name: x\n  start: 2020-01\n", "text/yaml")},
        headers={"X-Auth-Token": auth_token},
    )
    assert r.status_code == 401


def test_eras_requires_session(client: TestClient, auth_token: str):
    r = client.get("/eras", headers={"X-Auth-Token": auth_token})
    assert r.status_code == 401


def test_import_notes_requires_auth(client: TestClient):
    """`/import/notes` must reject anonymous uploads — anyone could otherwise
    spam the corpora directory."""
    zip_bytes = _build_zip({"2020-01-01.md": "hi"})
    r = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 401, r.text


def test_oversized_zip_rejected(client: TestClient, auth_token: str):
    """Zip-bomb defense: huge uncompressed total is rejected before extract."""
    # Build a pathological zip where one member declares enormous file_size.
    # We compress a long string of zeros to test the uncompressed-size guard.
    big_body = b"\0" * (config.MAX_UNCOMPRESSED_BYTES + 1024)
    zip_bytes = _build_zip({"huge.md": big_body.decode("latin-1")})
    r = client.post(
        "/import/notes",
        files={"file": ("notes.zip", zip_bytes, "application/zip")},
        headers={"X-Auth-Token": auth_token},
    )
    # The raw-upload cap might fire first (50 MB), or the uncompressed cap.
    # Either way, the server must refuse — never let the file land on disk.
    assert r.status_code == 413, r.text


# ---- Sample corpora flow --------------------------------------------------


def _lay_down_sample(
    slug: str,
    *,
    title: str = "Test Sample",
    description: str = "A sample for testing.",
    notes: dict[str, str] | None = None,
    eras_yaml: str | None = None,
    sample: bool = True,
) -> Path:
    """Materialize a sample corpus directly on disk (bypassing /import/notes,
    which would assign a random slug and not set the sample flag)."""
    import json as _json

    cdir = _test_corpora_root / slug
    notes_dir = cdir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    if notes is None:
        notes = {
            "1850-06-01.md": "First sample entry.",
            "1851-06-01.md": "Second sample entry.",
        }
    for name, body in notes.items():
        (notes_dir / name).write_text(body, encoding="utf-8")
    cfg = cdir / "_config"
    cfg.mkdir(exist_ok=True)
    if eras_yaml is None:
        eras_yaml = "- name: Era One\n  start: 1850-01\n- name: Era Two\n  start: 1851-01\n"
    (cfg / "eras.yaml").write_text(eras_yaml, encoding="utf-8")
    meta = {
        "title": title,
        "description": description,
        "source": "Project Gutenberg #00000",
    }
    if sample:
        meta["sample"] = True
    (cdir / "_meta.json").write_text(_json.dumps(meta), encoding="utf-8")
    return cdir


def test_is_sample_corpus_helper():
    """Direct unit-test of the gating predicate. Covers slug-shape, meta
    presence, and the sample flag."""
    sample_slug = "c_aaaaaaaaaaaaaaaa"
    not_sample_slug = "c_bbbbbbbbbbbbbbbb"
    _lay_down_sample(sample_slug)
    _lay_down_sample(not_sample_slug, sample=False)

    assert corpora.is_sample_corpus(sample_slug) is True
    assert corpora.is_sample_corpus(not_sample_slug) is False

    # Bad shapes never resolve.
    assert corpora.is_sample_corpus("andrew") is False
    assert corpora.is_sample_corpus("c_short") is False
    assert corpora.is_sample_corpus("c_NOTHEXNOTHEXNO") is False
    assert corpora.is_sample_corpus("c_ffffffffffffffff") is False  # no dir


def test_samples_endpoint_lists_sample_corpora(client: TestClient):
    """`GET /samples` returns flagged corpora only, sorted by title, with
    the right shape for a picker UI."""
    _lay_down_sample(
        "c_1111111111111111",
        title="Zoltan Diary",
        description="Z.",
        notes={"1900-01-01.md": "z"},
        eras_yaml="- name: Solo\n  start: 1900-01\n",
    )
    _lay_down_sample(
        "c_2222222222222222",
        title="Aardvark Letters",
        description="A.",
        notes={"1900-01-01.md": "a", "1900-02-01.md": "a2"},
        eras_yaml="- name: Solo\n  start: 1900-01\n",
    )
    # Non-sample corpus should not appear.
    _lay_down_sample(
        "c_3333333333333333",
        title="Hidden",
        sample=False,
    )

    r = client.get("/samples")
    assert r.status_code == 200, r.text
    out = r.json()
    titles = [s["title"] for s in out]
    assert "Aardvark Letters" in titles
    assert "Zoltan Diary" in titles
    assert "Hidden" not in titles
    # Sorted case-insensitively by title.
    assert titles.index("Aardvark Letters") < titles.index("Zoltan Diary")

    aard = next(s for s in out if s["title"] == "Aardvark Letters")
    assert aard["slug"] == "c_2222222222222222"
    assert aard["note_count"] == 2
    assert aard["era_count"] == 1
    assert aard["description"] == "A."
    assert aard["source"] == "Project Gutenberg #00000"


def test_samples_endpoint_open_without_auth(client: TestClient):
    """`/samples` is callable without any auth header — that's the whole point;
    visitors must be able to discover samples before signing in."""
    r = client.get("/samples")
    assert r.status_code == 200


def test_sample_corpus_readable_anonymously(client: TestClient):
    """Anonymous reads (`/corpus`, `/eras`, `/notes`) work for sample slugs."""
    slug = "c_4444444444444444"
    _lay_down_sample(slug, title="Readable Sample")

    r = client.get("/corpus", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["slug"] == slug
    assert info["is_sample"] is True
    assert info["has_eras"] is True
    assert info["note_count"] == 2

    r = client.get("/eras", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200, r.text
    eras = r.json()
    assert len(eras) == 2
    assert {e["name"] for e in eras} == {"Era One", "Era Two"}

    r = client.get("/notes?era=Era One", headers={"X-Corpus-Session": slug})
    assert r.status_code == 200, r.text


def test_sample_corpus_blocks_writes(client: TestClient):
    """Sample slugs must reject every destructive / compute-spending endpoint
    with 403 — even with no auth, the right answer is 'forbidden, sample is
    read-only', not 'unauthenticated'."""
    slug = "c_5555555555555555"
    _lay_down_sample(slug)

    # Eras replace.
    r = client.post(
        "/import/eras",
        files={"file": ("eras.yaml", b"- name: x\n  start: 2020-01\n", "text/yaml")},
        headers={"X-Corpus-Session": slug},
    )
    assert r.status_code == 403, r.text
    assert "sample" in r.json()["detail"].lower()

    # Wipe.
    r = client.post("/corpus/wipe", headers={"X-Corpus-Session": slug})
    assert r.status_code == 403, r.text

    # /promote was removed; chapter-write now happens inside the /session
    # WS via a `finalize` message, which short-circuits at WS accept-time
    # for sample slugs (covered by the WS-level reject path).

    # Draft (Claude subprocess — must never fire on the host's dime).
    r = client.post(
        "/draft",
        json={"era": "Era One"},
        headers={"X-Corpus-Session": slug},
    )
    assert r.status_code == 403, r.text

    # And the on-disk corpus is untouched.
    assert (_test_corpora_root / slug).exists()


def test_sample_flag_does_not_leak_to_andrew(client: TestClient):
    """Even if andrew/_meta.json had `sample: true`, the andrew slug must
    not be classified as a sample (otherwise it would silently become
    read-only). The slug-shape gate already excludes it — this confirms."""
    assert corpora.is_sample_corpus("andrew") is False


def test_non_sample_corpus_still_requires_auth(client: TestClient):
    """The samples relaxation must not weaken auth on regular corpora — a
    valid-looking imported slug without `sample: true` must still 401."""
    slug = "c_6666666666666666"
    _lay_down_sample(slug, sample=False)

    r = client.get("/corpus", headers={"X-Corpus-Session": slug})
    assert r.status_code == 401, r.text
    r = client.get("/eras", headers={"X-Corpus-Session": slug})
    assert r.status_code == 401, r.text
