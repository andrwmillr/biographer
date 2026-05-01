"""Shared corpus helpers — extracted from write_biography.py.

Contains the data layer used by both the CLI biographer and the web
routers: paths (single- and multi-tenant), note loading + parsing, era
resolution, prompt builders, and the model registry. No CLI side
effects beyond reading prompt files and resolving the active model
from sys.argv (which defaults sensibly when the flag isn't present).

Tests monkey-patch `corpus._CORPORA_BASE` to redirect into a temp
fixture; do that on the corpus module, not via re-exports."""
from __future__ import annotations

import json
import os
import pickle
import re
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Subject + paths
# ---------------------------------------------------------------------------

CORPUS = Path(os.environ.get("CORPUS_DIR") or Path.home() / "notes-archive" / "_corpora" / "andrew")
SUBJECT_NAME = os.environ.get("SUBJECT_NAME", "Andrew")
NOTES_DIR = CORPUS / "notes"
AUTHORSHIP = CORPUS / "_derived" / "_authorship.jsonl"
CORPUS_CACHE = CORPUS / "_derived" / "_corpus_cache.pkl"
DATE_OVERRIDES = CORPUS / "_config" / "_date_overrides.json"
NOTE_METADATA = CORPUS / "_config" / "_note_metadata.json"
EDITOR_NOTE_PREFIX = "EDITOR NOTE:"
MIXED_AUTHORSHIP_NOTE = "Contains quoted material — not all of this note is the subject's own writing."
ERAS_FILE = CORPUS / "_config" / "eras.yaml"
BIOGRAPHIES_DIR = CORPUS / "claude" / "biographies"
CHAPTERS_DIR = BIOGRAPHIES_DIR / "chapters"
THREADS_DIR = BIOGRAPHIES_DIR / "threads"

# Base for multi-tenant corpora. Tests monkey-patch this on the corpus
# module to redirect into a temp fixture; in normal use it's the host's
# _corpora/.
_CORPORA_BASE = Path.home() / "notes-archive" / "_corpora"


def _corpus_paths(corpus_id=None):
    """Per-corpus path bundle. corpus_id=None returns the module-level
    Andrew defaults; otherwise returns paths under _CORPORA_BASE/<corpus_id>/."""
    if corpus_id is None:
        return {
            "root": CORPUS,
            "notes": NOTES_DIR,
            "cache": CORPUS_CACHE,
            "authorship": AUTHORSHIP,
            "date_overrides": DATE_OVERRIDES,
            "note_metadata": NOTE_METADATA,
            "eras_file": ERAS_FILE,
            "biographies": BIOGRAPHIES_DIR,
            "chapters": CHAPTERS_DIR,
            "threads": THREADS_DIR,
        }
    root = _CORPORA_BASE / corpus_id
    return {
        "root": root,
        "notes": root / "notes",
        "cache": root / "_derived" / "_corpus_cache.pkl",
        "authorship": root / "_derived" / "_authorship.jsonl",
        "date_overrides": root / "_config" / "_date_overrides.json",
        "note_metadata": root / "_config" / "_note_metadata.json",
        "eras_file": root / "_config" / "eras.yaml",
        "biographies": root / "claude" / "biographies",
        "chapters": root / "claude" / "biographies" / "chapters",
        "threads": root / "claude" / "biographies" / "threads",
    }


def corpus_root(corpus_id=None):
    return _corpus_paths(corpus_id)["root"]


def chapters_dir(corpus_id=None):
    return _corpus_paths(corpus_id)["chapters"]


def threads_dir(corpus_id=None):
    return _corpus_paths(corpus_id)["threads"]


def biographies_dir(corpus_id=None):
    return _corpus_paths(corpus_id)["biographies"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

MODELS = {
    "opus-4.6": "claude-opus-4-6",
    "opus-4.7": "claude-opus-4-7",
    "sonnet-4.6": "claude-sonnet-4-6",
}


def _pick_model() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--model" and i + 1 < len(sys.argv):
            key = sys.argv[i + 1]
            if key in MODELS:
                return MODELS[key]
            return key
    return MODELS["opus-4.7"]


MODEL = _pick_model()


# ---------------------------------------------------------------------------
# Task variants + chapter system prompt
# ---------------------------------------------------------------------------

TASK_VARIANTS = {
    "paragraph": """Each chapter should make the era feel like time that was lived. Track the texture of daily life — what room, what hour, who was around, what he was reading or eating — and also the abstract preoccupations he kept turning over: how to live, what work matters, what kind of mind is worth having. Both belong. Track schemes (sleep, food, dating, work) and where they went. Let contradictions stand: he often held opposite views on the same day, and the truth is in the holding, not the resolution. Be honest about hard stretches — the corpus contains crises and a hospital stay; record what's there without smoothing or dramatizing. Notice when the prose itself shifts: sentences shortening, a journaling lapse, a poem arriving unprompted — those changes are usually load-bearing. Name people specifically when the notes do. The chapter is evidence of a life, not a verdict on it.""",

    "bullets": """Write a chapter covering, for this era:
- what was on his mind — preoccupations, what he kept coming back to, what shifted
- what was actually happening — events, people, relationships, decisions, mental-health stretches
- patterns and turning points — what recurs, what changes, what gets dropped""",
}


def _pick_task() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--task" and i + 1 < len(sys.argv):
            key = sys.argv[i + 1]
            if key in TASK_VARIANTS:
                return key
            print(f"ERROR: unknown --task '{key}'. Choices: {', '.join(TASK_VARIANTS)}", file=sys.stderr)
            sys.exit(1)
    return "paragraph"


TASK_VARIANT = _pick_task()


CHAPTER_SYSTEM = (Path(__file__).parent / "prompts" / "drafter.md").read_text(encoding="utf-8")
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__TASK__", TASK_VARIANTS[TASK_VARIANT])


# ---------------------------------------------------------------------------
# Subject identity (per-corpus)
# ---------------------------------------------------------------------------

def subject_context_for(corpus_id=None):
    """Return a kickoff-ready block identifying the subject of this corpus.
    Reads `_meta.json` title/description; falls back to the SUBJECT_NAME env
    default for the original Andrew corpus where no meta exists.

    System prompts use generic phrasing ("the subject"); this block is what
    actually tells the model who they're writing about. Inject at the top
    of each kickoff message."""
    paths = _corpus_paths(corpus_id)
    meta_path = paths["root"] / "_meta.json"
    title = ""
    description = ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = (meta.get("title") or "").strip()
            description = (meta.get("description") or "").strip()
        except Exception:
            pass
    if not title:
        title = SUBJECT_NAME
    lines = [f"# Subject", "", f"You're writing about {title}."]
    if description:
        lines.append("")
        lines.append(description)
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Eras
# ---------------------------------------------------------------------------

WRITING_LABELS = ["journal", "creative", "poetry", "letter", "fiction"]


def era_slug(name):
    base = name.replace(" ", "_").replace("/", "-")
    for i, (n, _, _) in enumerate(ERAS, start=1):
        if n == name:
            return f"{i:02d}_{base}"
    return base


def _prev_month(ym):
    """'2013-06' -> '2013-05'; '2014-01' -> '2013-12'."""
    y, m = int(ym[:4]), int(ym[5:7])
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def _load_eras(eras_file=None):
    """Load era boundaries from eras.yaml. Each entry is {name, start};
    end dates are derived as the month before the next entry's start.
    The last era is open-ended (9999-99)."""
    if eras_file is None:
        eras_file = ERAS_FILE
    if not eras_file.exists():
        return []
    raw = yaml.safe_load(eras_file.read_text(encoding="utf-8")) or []
    raw_sorted = sorted(raw, key=lambda e: e["start"])
    out = []
    for i, e in enumerate(raw_sorted):
        if i + 1 < len(raw_sorted):
            end = _prev_month(raw_sorted[i + 1]["start"])
        else:
            end = "9999-99"
        out.append((e["name"], e["start"], end))
    return out


ERAS = _load_eras()


def load_eras(corpus_id=None):
    """Public: load eras for a corpus. None means module-level ERAS (Andrew's)."""
    if corpus_id is None:
        return ERAS
    return _load_eras(_corpus_paths(corpus_id)["eras_file"])


def era_of(date, eras=None):
    if eras is None:
        eras = ERAS
    if not date or len(date) < 7:
        return None
    ym = date[:7]
    for name, lo, hi in eras:
        if lo <= ym <= hi:
            return name
    return None


def era_date_range(notes):
    """Return (lo, hi) YYYY-MM strings from this era's actual notes — usually
    tighter than the era's defined boundaries (especially Amherst I which is
    0000-00 to 2013-05)."""
    months = sorted({(n.get("date") or "")[:7] for n in notes if (n.get("date") or "")[:7]})
    if not months:
        return None, None
    return months[0], months[-1]


def era_heading(era_name, notes):
    lo, hi = era_date_range(notes)
    return f"{era_name} ({lo} – {hi})" if lo else era_name


# ---------------------------------------------------------------------------
# Note loading + transforms
# ---------------------------------------------------------------------------

TOTAL_CHAR_CAP = 700_000
MIN_PER_NOTE = 400


def _safe_note_path(rel, corpus_id=None):
    """Return the absolute path for note `rel` inside the corpus's notes
    dir, defending against path traversal (e.g. rel='../../_auth/state.json').
    Returns None if the resolved path escapes the notes dir or `rel`
    is empty/absolute.

    `_extract_zip_safe` already blocks malicious paths at upload time,
    but this is a belt-and-suspenders check on the read side: any future
    endpoint that takes a user-supplied `rel` and feeds it through
    `parse_note_body` is automatically defended."""
    if not rel or Path(rel).is_absolute():
        return None
    notes_dir = _corpus_paths(corpus_id)["notes"].resolve()
    try:
        candidate = (notes_dir / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(notes_dir):
        return None
    return candidate


def parse_note_body(rel, corpus_id=None):
    path = _safe_note_path(rel, corpus_id)
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return text.strip()
    return m.group(2).lstrip("\n").strip()


def sample_keeper(body, cap):
    if len(body) <= cap:
        return body
    head = cap // 2
    tail = cap - head - 40
    middle = len(body) - head - tail
    return f"{body[:head]}\n\n…[middle {middle} chars snipped]…\n\n{body[-tail:]}"


def load_corpus_notes(corpus_id=None):
    """Walk the corpus and return one record per .md file with a date.
    Records have rel, title, date — same shape Phase B expects, sourced
    directly from frontmatter rather than from Phase A's annotation pass.

    Layout-agnostic: any depth, any folder name. The first path segment
    of `rel` is used downstream as a "label" (folder_aware_sample buckets
    by it; the chapter prompt surfaces it). Hidden directories
    (`.git`, `.obsidian`, etc.) are skipped.

    Caches the parsed result to CORPUS_CACHE. The cache holds only
    frontmatter-derived fields; note bodies are still loaded on demand
    from disk via parse_note_body(). Delete the cache file to force a
    fresh walk after edits to note files or the parser."""
    paths = _corpus_paths(corpus_id)
    cache = paths["cache"]
    notes_root = paths["notes"]
    if cache.exists():
        try:
            with cache.open("rb") as f:
                return pickle.load(f)
        except Exception:
            pass  # fall through and rebuild
    out = []
    if not notes_root.exists():
        return out

    # Date discovery: frontmatter `date_created` first, else filename
    # YYYY-MM-DD prefix (covers Thoreau-style imports without frontmatter).
    for path in sorted(notes_root.rglob("*.md")):
        rel = path.relative_to(notes_root)
        # Skip hidden directories (Obsidian configs, dotfiles, etc.).
        if any(part.startswith(".") for part in rel.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        front = m.group(1) if m else ""
        title = ""
        date = ""
        for line in front.splitlines():
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "title":
                title = v
            elif k == "date_created" and not date:
                date = v.rstrip("Z")
        if not date:
            fn_match = re.match(r"(\d{4}-\d{2}-\d{2})", path.stem)
            if fn_match:
                date = fn_match.group(1)
        if not date:
            continue
        out.append({
            "rel": str(rel),
            "title": title,
            "date": date,
        })
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as f:
        pickle.dump(out, f)
    return out


def apply_date_overrides(notes, corpus_id=None):
    """Override a note's date where `date_created` reflects import-time rather
    than actual write-time (e.g., a poem pasted into Apple Notes years later)."""
    overrides_path = _corpus_paths(corpus_id)["date_overrides"]
    if not overrides_path.exists():
        return 0
    overrides = json.loads(overrides_path.read_text())
    applied = 0
    for n in notes:
        new_date = overrides.get(n["rel"])
        if new_date:
            n["date"] = new_date
            applied += 1
    return applied


def apply_note_metadata(notes, corpus_id=None):
    """Stamp n["editor_note"] = freeform contextual guidance for individual
    notes. Examples:
    - Retrospective: note written in year Y describes events from year X
      (entry says what era to place the scenes in).
    - Pseudonym/disambiguation: a name in the note is altered, or a referent
      is non-obvious (entry clarifies identity without changing era).
    - Authorship: appended automatically for mixed/unclear verdicts.
    The user.md renderer prepends EDITOR_NOTE_PREFIX and emits verbatim.
    If a note already has an editor_note (e.g. from apply_authorship), this
    appends to it with a space separator so multiple sources coexist."""
    metadata_path = _corpus_paths(corpus_id)["note_metadata"]
    if not metadata_path.exists():
        return 0
    overrides = json.loads(metadata_path.read_text())
    applied = 0
    for n in notes:
        extra = overrides.get(n["rel"])
        if extra:
            existing = n.get("editor_note")
            n["editor_note"] = f"{existing} {extra}" if existing else extra
            applied += 1
    return applied


def flag_date_clusters(notes, threshold=3):
    """Mark notes as date-uncertain when threshold+ notes share a date.
    Such clusters likely reflect import time or last-viewed/edited time
    rather than the original write date. The prompt uses the flag to
    keep prose vague about exact timing while still emitting the citation."""
    counts = {}
    for n in notes:
        d = (n.get("date") or "")[:10]
        if d:
            counts[d] = counts.get(d, 0) + 1
    flagged = 0
    for n in notes:
        d = (n.get("date") or "")[:10]
        if d and counts.get(d, 0) >= threshold:
            n["date_uncertain"] = True
            n["date_cluster_size"] = counts[d]
            flagged += 1
    return flagged


def load_authorship(corpus_id=None):
    """Return {rel: verdict} using the latest row per rel (file is append-only
    and may contain human-review overrides at the end)."""
    authorship_path = _corpus_paths(corpus_id)["authorship"]
    if not authorship_path.exists():
        return {}
    latest = {}
    for line in authorship_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r:
            continue
        rel = r.get("rel")
        if rel:
            latest[rel] = r.get("authored", "unclear")
    return latest


def apply_authorship(notes, verdicts):
    """Drop 'no' notes entirely. For mixed/unclear, set editor_note to the
    fixed authorship warning so the prompt can warn Opus to distinguish
    Andrew's prose from embedded quotes. Manual editor notes from
    _note_metadata.json are appended later by apply_note_metadata."""
    kept = []
    n_dropped = 0
    n_mixed = 0
    for n in notes:
        v = verdicts.get(n["rel"])
        if v == "no":
            n_dropped += 1
            continue
        if v in ("mixed", "unclear"):
            n["editor_note"] = MIXED_AUTHORSHIP_NOTE
            n_mixed += 1
        kept.append(n)
    return kept, n_dropped, n_mixed


# ---------------------------------------------------------------------------
# Chapter / digest loaders (per-era predecessors and successors)
# ---------------------------------------------------------------------------

def load_prior_chapters(era_name, corpus_id=None):
    """Return [(era_name, chapter_text), ...] for all eras chronologically
    before `era_name` that have a canonical chapter at chapters/<slug>.md."""
    eras = load_eras(corpus_id)
    chapters = _corpus_paths(corpus_id)["chapters"]
    out = []
    for name, _, _ in eras:
        if name == era_name:
            return out
        ch_path = chapters / f"{era_slug(name)}.md"
        if ch_path.exists():
            ch_text = ch_path.read_text(encoding="utf-8").strip()
            if ch_text:
                out.append((name, ch_text))
    return out


def load_prior_thread_digests(era_name, corpus_id=None):
    """Return [(era_name, digest_text), ...] for all eras chronologically
    before `era_name` that have a promoted digest at threads/<slug>.md."""
    eras = load_eras(corpus_id)
    threads = _corpus_paths(corpus_id)["threads"]
    out = []
    for name, _, _ in eras:
        if name == era_name:
            return out
        d_path = threads / f"{era_slug(name)}.md"
        if d_path.exists():
            d_text = d_path.read_text(encoding="utf-8").strip()
            if d_text:
                out.append((name, d_text))
    return out


def load_future_thread_digests(era_name, corpus_id=None):
    """Return [(era_name, digest_text), ...] for all eras chronologically
    AFTER `era_name` that have a promoted digest at threads/<slug>.md."""
    eras = load_eras(corpus_id)
    threads = _corpus_paths(corpus_id)["threads"]
    out = []
    seen_self = False
    for name, _, _ in eras:
        if name == era_name:
            seen_self = True
            continue
        if not seen_self:
            continue
        d_path = threads / f"{era_slug(name)}.md"
        if d_path.exists():
            d_text = d_path.read_text(encoding="utf-8").strip()
            if d_text:
                out.append((name, d_text))
    return out


def load_future_chapters(era_name, corpus_id=None):
    """Return [(era_name, chapter_text), ...] for all eras chronologically
    AFTER `era_name` that have a canonical chapter at chapters/<slug>.md."""
    eras = load_eras(corpus_id)
    chapters = _corpus_paths(corpus_id)["chapters"]
    out = []
    seen_self = False
    for name, _, _ in eras:
        if name == era_name:
            seen_self = True
            continue
        if not seen_self:
            continue
        ch_path = chapters / f"{era_slug(name)}.md"
        if ch_path.exists():
            ch_text = ch_path.read_text(encoding="utf-8").strip()
            if ch_text:
                out.append((name, ch_text))
    return out


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_user_msg(era_name, notes, prior_chapters=None, prior_digests=None,
                   future_chapters=None, future_digests=None, corpus_id=None):
    sorted_notes = sorted(notes, key=lambda n: n.get("date", ""))
    bodies = []
    for n in sorted_notes:
        body = parse_note_body(n["rel"], corpus_id)
        if not body:
            continue
        bodies.append((n, body))
    total = sum(len(b) for _, b in bodies)
    if total > TOTAL_CHAR_CAP and bodies:
        ratio = TOTAL_CHAR_CAP / total
        bodies = [(n, sample_keeper(b, max(MIN_PER_NOTE, int(len(b) * ratio)))) for n, b in bodies]
    lines = []
    prior_chapters = prior_chapters or []
    if prior_chapters:
        lines.append("--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---")
        lines.append("")
        for prior_era, ch_text in prior_chapters:
            lines.append(f"### {prior_era}")
            lines.append("")
            lines.append(ch_text)
            lines.append("")
        lines.append("--- END PRIOR CHAPTERS ---")
        lines.append("")
    prior_digests = prior_digests or []
    if prior_digests:
        lines.append("--- PRIOR THREAD DIGESTS (structured per-era state — read alongside the prior chapters) ---")
        lines.append("")
        for prior_era, d_text in prior_digests:
            lines.append(f"### {prior_era}")
            lines.append("")
            lines.append(d_text)
            lines.append("")
        lines.append("--- END PRIOR THREAD DIGESTS ---")
        lines.append("")
    future_chapters = future_chapters or []
    if future_chapters:
        lines.append("--- FUTURE CHAPTERS (later eras, drafted in a previous run — for thematic alignment, NOT for events that haven't happened yet in this era; do not foreshadow or anticipate) ---")
        lines.append("")
        for future_era, ch_text in future_chapters:
            lines.append(f"### {future_era}")
            lines.append("")
            lines.append(ch_text)
            lines.append("")
        lines.append("--- END FUTURE CHAPTERS ---")
        lines.append("")
    future_digests = future_digests or []
    if future_digests:
        lines.append("--- FUTURE THREAD DIGESTS (later eras' digests — same caveat: hindsight context, not events to anticipate) ---")
        lines.append("")
        for future_era, d_text in future_digests:
            lines.append(f"### {future_era}")
            lines.append("")
            lines.append(d_text)
            lines.append("")
        lines.append("--- END FUTURE THREAD DIGESTS ---")
        lines.append("")
    lo, hi = era_date_range(notes)
    range_str = f" ({lo} – {hi})" if lo else ""
    lines.extend([
        f"ERA: {era_name}{range_str}",
        f"NOTES IN THIS ERA: {len(notes)} total, {len(bodies)} with body content",
        "",
        "--- ALL NOTES (full text, chronological) ---",
        "",
    ])
    for n, body in bodies:
        title = n.get("title") or "(untitled)"
        date = (n.get("date") or "")[:10]
        label = n["rel"].split("/", 1)[0]
        date_warn = ""
        if n.get("date_uncertain"):
            date_warn = f"  ⚠ DATE-CLUSTER ({n.get('date_cluster_size')} notes share this date — likely import or last-edit time, not write time)"
        lines.append(f"=== {date} · {label} · {title}{date_warn} ===")
        if n.get("editor_note"):
            lines.append(f"  {EDITOR_NOTE_PREFIX} {n['editor_note']}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)
