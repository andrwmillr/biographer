#!/usr/bin/env python3
"""
Phase 3: Normalize all 5 sources into ~/notes-archive/_corpus/ as unified .txt files.

Layout: _corpus/<source>/<mirror-of-source-tree>/<name>.txt
  Each .txt = YAML frontmatter + blank line + body text.

Originals are NEVER modified. This script rebuilds _corpus/ from scratch on each run.
The per-source native-format copies (.enml, .rtf, .docx, raw HTML) serve as the raw layer.

Conversions:
  .txt   → copy as-is
  .docx  → textutil -convert txt  (macOS native)
  .rtf   → textutil -convert txt  (macOS native)
  .enml  → custom XHTML strip + ENML special tags (<en-media>, <en-todo>, <en-crypt>)

Common frontmatter keys: title, date_created, date_updated, source, folder, source_path.
Source-specific keys added where meaningful (locked, clipped, flags, has_* structure flags).
"""
import json, os, re, shutil, subprocess, sys
from datetime import datetime
from html import unescape
from pathlib import Path

ARCHIVE = Path.home() / "notes-archive"
CORPUS  = ARCHIVE / "_corpus"
NOTES_DIR = CORPUS / "notes"

# ---------- YAML helpers ----------

def yaml_scalar(v):
    """Format a scalar for inline YAML. Always quote strings to be safe."""
    if v is None: return 'null'
    if isinstance(v, bool): return 'true' if v else 'false'
    if isinstance(v, (int, float)): return str(v)
    if isinstance(v, list):
        if not v: return '[]'
        return '[' + ', '.join(yaml_scalar(x) for x in v) + ']'
    if isinstance(v, dict):
        # Inline JSON-style is valid YAML flow mapping for simple dicts
        if not v: return '{}'
        return '{' + ', '.join(f'{k}: {yaml_scalar(val)}' for k, val in v.items()) + '}'
    s = str(v)
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'

def write_corpus_file(out_path: Path, frontmatter: dict, body: str,
                      collision_suffix: str = ''):
    """Write a .txt file with YAML frontmatter. If the target already exists,
    append ` [<collision_suffix>]` to disambiguate (e.g. when both foo.txt and
    foo.docx sit in the same source dir)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and collision_suffix:
        stem = out_path.stem
        out_path = out_path.with_name(f'{stem} [{collision_suffix}].txt')
        # Update source_path in frontmatter is already accurate; keep as-is.
    lines = ['---']
    for k, v in frontmatter.items():
        lines.append(f'{k}: {yaml_scalar(v)}')
    lines.append('---')
    lines.append('')
    body_clean = (body.rstrip() + '\n') if body and body.strip() else ''
    text = '\n'.join(lines) + '\n' + body_clean
    out_path.write_text(text, encoding='utf-8')
    return out_path

# ---------- generic helpers ----------

def mtime_iso(path: Path) -> str:
    ts = datetime.fromtimestamp(path.stat().st_mtime)
    return ts.strftime('%Y-%m-%dT%H:%M:%S')

def textutil_to_txt(path: Path) -> str:
    r = subprocess.run(['textutil', '-convert', 'txt', '-stdout', str(path)],
                       capture_output=True, text=True, errors='replace')
    if r.returncode != 0:
        return f'[textutil error: {r.stderr.strip()}]'
    return r.stdout

def meta_path_for(path: Path) -> Path:
    """Return sibling <stem>.meta.json path for a given file."""
    return path.with_suffix('.meta.json')

def read_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

# ---------- ENML → text ----------

_MIME_EXT = {
    'image/jpeg':'jpg','image/png':'png','image/gif':'gif',
    'image/webp':'webp','image/svg+xml':'svg',
    'application/pdf':'pdf','audio/mpeg':'mp3',
}

def enml_to_text(enml: str) -> str:
    if not enml: return ''
    s = enml

    # <en-media hash="X" type="Y"/> → placeholder  (capture before generic tag strip)
    def media_replace(m):
        attrs = m.group(0)
        h = re.search(r'hash="([^"]+)"', attrs)
        t = re.search(r'type="([^"]+)"', attrs)
        hash_str = h.group(1)[:8] if h else 'unknown'
        mime = t.group(1) if t else ''
        ext = _MIME_EXT.get(mime, mime.split('/')[-1] if mime else 'bin')
        return f' [media: {hash_str}.{ext}] '
    s = re.sub(r'<en-media\b[^>]*/?>', media_replace, s, flags=re.IGNORECASE)

    # <en-todo checked="true"/> → [x] ; otherwise → [ ]
    s = re.sub(r'<en-todo\b[^>]*checked="true"[^>]*/?>', '[x] ', s, flags=re.IGNORECASE)
    s = re.sub(r'<en-todo\b[^>]*/?>', '[ ] ', s, flags=re.IGNORECASE)

    # <en-crypt>...</en-crypt> → placeholder (content is base64 ciphertext, unreadable)
    s = re.sub(r'<en-crypt\b[^>]*>.*?</en-crypt>', '[encrypted]', s,
               flags=re.IGNORECASE|re.DOTALL)

    # Strip XML declaration and DOCTYPE
    s = re.sub(r'<\?xml[^>]*\?>', '', s)
    s = re.sub(r'<!DOCTYPE[^>]*>', '', s)

    # Block-level tags → newline
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'</?(div|p|li|h[1-6]|tr|table|thead|tbody|blockquote|pre|en-note|hr)[^>]*>',
               '\n', s, flags=re.IGNORECASE)

    # Strip remaining tags
    s = re.sub(r'<[^>]+>', '', s)

    # Decode entities
    s = unescape(s)

    # Collapse whitespace
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

# ---------- per-source pipelines ----------

def process_flat_source(src_name: str):
    """zenedit, letters-backup: flat dirs of .txt + .docx (no sidecars)."""
    src_dir = ARCHIVE / src_name
    out_dir = NOTES_DIR / src_name
    stats = {'txt': 0, 'docx': 0, 'skipped': 0, 'fail': 0}
    for path in sorted(src_dir.rglob('*')):
        if not path.is_file(): continue
        if path.name.startswith('.'): continue
        rel = path.relative_to(src_dir)
        ext = path.suffix.lower()
        if ext == '.txt':
            body = path.read_text(encoding='utf-8', errors='replace')
            stats['txt'] += 1
        elif ext == '.docx':
            body = textutil_to_txt(path)
            stats['docx'] += 1
            if body.startswith('[textutil error'):
                stats['fail'] += 1
        else:
            stats['skipped'] += 1
            continue
        title = path.stem
        folder = str(rel.parent) if rel.parent != Path('.') else ''
        fm = {
            'title': title,
            'date_created': mtime_iso(path),
            'source': src_name,
            'folder': folder,
            'source_path': f'{src_name}/{rel}',
            'original_format': ext.lstrip('.'),
        }
        out_path = out_dir / rel.with_suffix('.txt')
        # If foo.txt and foo.docx both exist in source, disambiguate by format
        write_corpus_file(out_path, fm, body, collision_suffix=ext.lstrip('.'))
    return stats

def process_debrief():
    src_dir = ARCHIVE / 'debrief'
    out_dir = NOTES_DIR / 'debrief'
    stats = {'rtf': 0, 'fail': 0}
    for rtf_path in sorted(src_dir.rglob('*.rtf')):
        if '/_raw/' in str(rtf_path): continue
        rel = rtf_path.relative_to(src_dir)
        meta = read_json(meta_path_for(rtf_path))
        body = textutil_to_txt(rtf_path)
        if body.startswith('[textutil error'):
            stats['fail'] += 1
        flags_raw = meta.get('flags', {})
        flags = {k: (v == '1') for k, v in flags_raw.items()}
        lineage = rel.parts[0] if rel.parts else ''  # 'notes' or 'notes-old'
        fm = {
            'title': meta.get('subject') or rtf_path.stem,
            'date_created': meta.get('date_create'),
            'date_updated': meta.get('date_update'),
            'source': 'debrief',
            'lineage': lineage,
            'folder': meta.get('folder_path') or '',
            'source_path': f'debrief/{rel}',
            'source_db': meta.get('source_db'),
            'flags': flags,
        }
        out_path = out_dir / rel.with_suffix('.txt')
        write_corpus_file(out_path, fm, body)
        stats['rtf'] += 1
    return stats

def process_evernote():
    src_dir = ARCHIVE / 'evernote'
    out_dir = NOTES_DIR / 'evernote'
    stats = {'enml': 0}
    for enml_path in sorted(src_dir.rglob('*.enml')):
        if '/_raw/' in str(enml_path): continue
        rel = enml_path.relative_to(src_dir)
        meta = read_json(meta_path_for(enml_path))
        body = enml_to_text(enml_path.read_text(encoding='utf-8', errors='replace'))
        partition = meta.get('partition') or (rel.parts[0] if rel.parts else '')
        resources = meta.get('resources', [])
        fm = {
            'title': meta.get('title') or enml_path.stem,
            'date_created': meta.get('created'),
            'date_updated': meta.get('updated'),
            'source': 'evernote',
            'partition': partition,       # 'authored' or 'clipped'
            'folder': meta.get('notebook') or '',
            'source_path': f'evernote/{rel}',
            'web_clip': meta.get('web_clip', False),
            'deleted': meta.get('deleted', False),
            'tags': meta.get('tags', []),
            'note_source': meta.get('note_attributes', {}).get('source', ''),
            'has_resources': bool(resources),
            'resource_count': len(resources),
        }
        out_path = out_dir / rel.with_suffix('.txt')
        write_corpus_file(out_path, fm, body)
        stats['enml'] += 1
    return stats

def process_apple_notes():
    src_dir = ARCHIVE / 'apple-notes'
    out_dir = NOTES_DIR / 'apple-notes'
    stats = {'txt': 0}
    for txt_path in sorted(src_dir.rglob('*.txt')):
        if '/_raw/' in str(txt_path): continue
        rel = txt_path.relative_to(src_dir)
        meta = read_json(meta_path_for(txt_path))
        body = txt_path.read_text(encoding='utf-8', errors='replace')
        fm = {
            'title': meta.get('title') or txt_path.stem,
            'date_created': meta.get('created'),
            'date_updated': meta.get('updated'),
            'source': 'apple-notes',
            'folder': f"{meta.get('account','')}/{meta.get('folder','')}".strip('/'),
            'source_path': f'apple-notes/{rel}',
            'locked': meta.get('locked', False),
            'deleted': meta.get('deleted', False),
            'has_checklist':   meta.get('has_checklist', False),
            'has_bullet_list': meta.get('has_bullet_list', False),
            'has_tables':      meta.get('has_tables', False),
            'has_links':       meta.get('has_links', False),
            'has_images':      meta.get('has_images', False),
            'has_attachments': meta.get('has_attachments', False),
        }
        out_path = out_dir / rel
        write_corpus_file(out_path, fm, body)
        stats['txt'] += 1
    return stats

# ---------- main ----------

def main():
    if not ARCHIVE.exists():
        raise SystemExit(f"archive missing: {ARCHIVE}")
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    CORPUS.mkdir(parents=True)

    print(f"=== zenedit ===")
    r = process_flat_source('zenedit')
    print(f"  .txt→.txt:  {r['txt']}")
    print(f"  .docx→.txt: {r['docx']}  (fails: {r['fail']})")

    print(f"\n=== letters-backup ===")
    r = process_flat_source('letters-backup')
    print(f"  .txt→.txt:  {r['txt']}")
    print(f"  .docx→.txt: {r['docx']}  (fails: {r['fail']})")

    print(f"\n=== debrief ===")
    r = process_debrief()
    print(f"  .rtf→.txt:  {r['rtf']}  (fails: {r['fail']})")

    print(f"\n=== evernote ===")
    r = process_evernote()
    print(f"  .enml→.txt: {r['enml']}")

    print(f"\n=== apple-notes ===")
    r = process_apple_notes()
    print(f"  .txt→.txt:  {r['txt']}")

    total = sum(1 for _ in CORPUS.rglob('*.txt'))
    print(f"\n=== total in _corpus/ ===  {total} files")

    # Per-source subtotals from the filesystem
    print("\nPer source:")
    for sub in sorted((p for p in CORPUS.iterdir() if p.is_dir()), key=lambda p: p.name):
        n = sum(1 for _ in sub.rglob('*.txt'))
        print(f"  {sub.name:18s} {n}")

    # Preserve the script
    raw_root = ARCHIVE / '_raw'
    raw_root.mkdir(exist_ok=True)
    shutil.copy2(__file__, raw_root / 'phase3_normalize.py')
    print(f"\nscript preserved at: {raw_root / 'phase3_normalize.py'}")

if __name__ == '__main__':
    main()
