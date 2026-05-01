#!/usr/bin/env python3
"""List, remove, or backfill metadata for corpora under ~/notes-archive/_corpora/.

Examples:
  python3 list_corpora.py                  # table of all corpora
  python3 list_corpora.py --rm c_a1b2c3d4  # remove a corpus by slug (with confirm)
  python3 list_corpora.py --backfill-meta  # write _meta.json for any corpus
                                           # missing it, so it dedups against
                                           # future re-uploads

Each corpus row shows its slug, note count, whether eras.yaml exists,
and the create / last-modified timestamps. The 'andrew' corpus is the
host's own legacy data — listed for completeness, but you almost
certainly do not want to --rm it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

CORPORA = Path.home() / "notes-archive" / "_corpora"


def _row(corpus_dir: Path) -> dict:
    notes_dir = corpus_dir / "notes"
    note_count = (
        sum(1 for p in notes_dir.rglob("*") if p.is_file())
        if notes_dir.exists()
        else 0
    )
    eras_yaml = corpus_dir / "_config" / "eras.yaml"
    has_eras = eras_yaml.exists()
    meta_path = corpus_dir / "_meta.json"
    created = "?"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            created = (meta.get("created_at") or "?")[:16].replace("T", " ")
        except Exception:
            pass
    try:
        mtime = max(
            (p.stat().st_mtime for p in corpus_dir.rglob("*") if p.is_file()),
            default=corpus_dir.stat().st_mtime,
        )
    except Exception:
        mtime = corpus_dir.stat().st_mtime
    last_mod = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    return {
        "slug": corpus_dir.name,
        "notes": note_count,
        "has_eras": has_eras,
        "created": created,
        "last_mod": last_mod,
    }


def list_all() -> int:
    if not CORPORA.exists():
        print(f"(no _corpora dir at {CORPORA})")
        return 0
    rows = [_row(d) for d in sorted(CORPORA.iterdir()) if d.is_dir()]
    if not rows:
        print("(no corpora)")
        return 0
    print(f"{'slug':40} {'notes':>6}  {'eras':4}  {'created':16}  {'last_mod':16}")
    print("-" * 95)
    for r in rows:
        eras = "yes" if r["has_eras"] else "no"
        print(
            f"{r['slug']:40} {r['notes']:>6}  {eras:4}  {r['created']:16}  {r['last_mod']:16}"
        )
    return 0


def _content_hash_from_dir(notes_dir: Path) -> str:
    """sha256 of (sorted relative paths + file bytes), matching the zip-side
    hash in server.py:_zip_content_hash. Stable across re-zips."""
    h = hashlib.sha256()
    if not notes_dir.exists():
        return h.hexdigest()
    files = sorted(p for p in notes_dir.rglob("*") if p.is_file())
    for f in files:
        rel = str(f.relative_to(notes_dir))
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0\0")
    return h.hexdigest()


def backfill_meta() -> int:
    """For each corpus lacking _meta.json, compute its content hash and write
    one. Lets pre-existing corpora participate in dedup against future
    re-uploads."""
    if not CORPORA.exists():
        print(f"(no _corpora dir at {CORPORA})")
        return 0
    n_done = 0
    n_skipped = 0
    for d in sorted(CORPORA.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "_meta.json"
        if meta_path.exists():
            print(f"  {d.name}: already has _meta.json (skipping)")
            n_skipped += 1
            continue
        notes_dir = d / "notes"
        h = _content_hash_from_dir(notes_dir)
        ts = datetime.fromtimestamp(d.stat().st_ctime).isoformat(timespec="seconds")
        meta_path.write_text(
            json.dumps(
                {"content_hash": h, "created_at": ts, "backfilled": True}
            ),
            encoding="utf-8",
        )
        n_done += 1
        print(f"  {d.name}: hash={h[:12]}…  created_at={ts}")
    print(f"backfilled {n_done}, skipped {n_skipped}")
    return 0


def remove(slug: str, force: bool = False) -> int:
    target = CORPORA / slug
    if not target.is_dir():
        print(f"no corpus at {target}", file=sys.stderr)
        return 1
    if not force:
        confirm = input(f"Delete {target}? [y/N] ").strip().lower()
        if confirm != "y":
            print("cancelled")
            return 0
    shutil.rmtree(target)
    print(f"deleted {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List or remove corpora under ~/notes-archive/_corpora/"
    )
    parser.add_argument("--rm", metavar="SLUG", help="remove the given corpus slug")
    parser.add_argument(
        "-f", "--force", action="store_true", help="skip confirmation on --rm"
    )
    parser.add_argument(
        "--backfill-meta",
        action="store_true",
        help="write _meta.json for any corpus missing it (one-shot)",
    )
    args = parser.parse_args()
    if args.backfill_meta:
        return backfill_meta()
    if args.rm:
        return remove(args.rm, force=args.force)
    return list_all()


if __name__ == "__main__":
    sys.exit(main())
