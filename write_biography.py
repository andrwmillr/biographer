#!/usr/bin/env python3
"""Phase B: synthesize a 15-year narrative from the corpus.

Walks _corpus/notes/{journal,creative,poetry,letter}/, groups notes by era,
hands every note's full body in chronological order to Claude for an era
chapter. Each chapter is written sequentially with prior chapters as
context for continuity. Then writes a top-level summary conditioned on
the chapters.

Output: _corpus/claude/biographies/biography_<stamp>.md plus a
`biography.md` symlink to the latest.

Flags:
  --model {opus-4.6,opus-4.7,sonnet-4.6} — pick model (default opus-4.7)
  --task {paragraph,bullets}             — chapter framing (default paragraph)
  --future                               — also feed each draft any later eras'
                                            chapters/digests already on disk from
                                            a previous run (hindsight context).
                                            Off by default; breaks inter-era
                                            prefix caching when on.
"""
import asyncio
import json
import os
import pickle
import re
import subprocess
import sys
import time
from datetime import datetime

import yaml
from pathlib import Path

from anthropic import AsyncAnthropic

START = time.time()


def ts():
    elapsed = int(time.time() - START)
    return f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"


def log(msg):
    print(f"{ts()} {msg}", flush=True)

CORPUS = Path(os.environ.get("CORPUS_DIR") or Path.home() / "notes-archive" / "_corpora" / "andrew")
SUBJECT_NAME = os.environ.get("SUBJECT_NAME", "Andrew")
NOTES_DIR = CORPUS / "notes"
AUTHORSHIP = CORPUS / "_derived" / "_authorship.jsonl"
CORPUS_CACHE = CORPUS / "_derived" / "_corpus_cache.pkl"
DATE_OVERRIDES = CORPUS / "_config" / "_date_overrides.json"
NOTE_METADATA = CORPUS / "_config" / "_note_metadata.json"
EDITOR_NOTE_PREFIX = "EDITOR NOTE:"
MIXED_AUTHORSHIP_NOTE = "Contains quoted material — not all of this note is Andrew's own writing."
ERAS_FILE = CORPUS / "_config" / "eras.yaml"
BIOGRAPHIES_DIR = CORPUS / "claude" / "biographies"
CHAPTERS_DIR = BIOGRAPHIES_DIR / "chapters"
THREADS_DIR = BIOGRAPHIES_DIR / "threads"


# Base for multi-tenant corpora. Tests monkey-patch this to redirect into
# a temp dir for isolation; in normal use it's the host's _corpora/.
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


RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
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


def _pick_future() -> bool:
    return "--future" in sys.argv

INCLUDE_FUTURE = _pick_future()


OUT_MD = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}.md"
SYSTEM_SNAPSHOT = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}_system.md"
USERS_SNAPSHOT = BIOGRAPHIES_DIR / f"biography_{RUN_STAMP}_users.md"
LATEST_SYMLINK = BIOGRAPHIES_DIR / "biography.md"


def _prompt_sha():
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _is_dirty():
    try:
        out = subprocess.check_output(
            ["git", "-C", str(Path(__file__).parent), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return bool(out)
    except Exception:
        return False
MAX_RETRIES = 8

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


TOTAL_CHAR_CAP = 700_000
MIN_PER_NOTE = 400


CHAPTER_SYSTEM = (Path(__file__).parent / "DRAFTER.md").read_text(encoding="utf-8")
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__TASK__", TASK_VARIANTS[TASK_VARIANT])
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__SUBJECT__", SUBJECT_NAME)


SUMMARY_SYSTEM = """You are writing the opening synthesis to a retrospective on __SUBJECT__'s personal writing archive, 2011-2026. Plain third-person voice — a thoughtful biographer, not a literary critic.

You'll receive five era chapters. Write 250-450 words covering the through-lines: what preoccupied him across all fifteen years, what changed, what didn't, what the arc looks like from a distance.

VOICE
Plain, direct English. Short sentences welcome. Write so a friend can follow. Third person. No literary-critic jargon ("revisionary sentence", "ars poetica", "the register", "signature gesture", etc.). No ornate sentence structures for their own sake.

ABSOLUTE RULES
- Work only from what the chapters say. Don't introduce new specifics, names, dates, or quotes.
- If you use a quote or specific detail from a chapter, keep any [YYYY-MM-DD] citation it carries.
- No invented imagery. Abstract patterns are fine without citation; specifics need the source chapter's anchor.
- No "in conclusion", no enumerating the era names, no "this retrospective".
- Third person for __SUBJECT__. No sentimentalizing.
- If being accurate and plain makes the paragraph thinner, that's fine."""
SUMMARY_SYSTEM = SUMMARY_SYSTEM.replace("__SUBJECT__", SUBJECT_NAME)


def parse_note_body(rel, corpus_id=None):
    path = _corpus_paths(corpus_id)["notes"] / rel
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
    """Walk the corpus and return one record per note in the writing labels.
    Records have rel, title, date — same shape Phase B expects, sourced
    directly from frontmatter rather than from Phase A's annotation pass.

    Caches the parsed result to CORPUS_CACHE. The cache holds only frontmatter-
    derived fields; note bodies are still loaded on demand from disk via
    parse_note_body(). Delete the cache file to force a fresh walk after
    edits to note files or the parser."""
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

    # Two layouts supported:
    #   - Andrew-style: notes/<label>/*.md where label ∈ WRITING_LABELS
    #   - Flat: notes/*.md (used by friend corpora that didn't get a layout)
    # Date discovery: frontmatter `date_created` first, else filename
    # YYYY-MM-DD prefix (covers Thoreau-style imports without frontmatter).
    for path in sorted(notes_root.rglob("*.md")):
        rel = path.relative_to(notes_root)
        if len(rel.parts) > 2:
            continue
        if len(rel.parts) == 2 and rel.parts[0] not in WRITING_LABELS:
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


def load_prior_chapters(era_name, corpus_id=None):
    """Return [(era_name, chapter_text), ...] for all eras chronologically
    before `era_name` that have a canonical chapter at chapters/<slug>.md.
    Order: chronological (oldest first). Missing chapters are skipped
    silently — partial state is normal early in the project."""
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
    before `era_name` that have a promoted digest at threads/<slug>.md.
    Order: chronological. Missing digests are skipped silently — partial
    state is normal as the corpus fills forward."""
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
    AFTER `era_name` that have a promoted digest at threads/<slug>.md.
    Used when --future is on, to give the drafter hindsight context
    from a previous full run."""
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
    AFTER `era_name` that have a canonical chapter at chapters/<slug>.md.
    Used when --future is on, to give the drafter hindsight context
    from a previous full run."""
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


def normalize_text(s):
    return re.sub(r"\s+", " ", s.lower()).strip()


def extract_quotes(text):
    """Return all quoted passages in the chapter:
    - inline: text between paired double-quote chars
    - block: consecutive lines prefixed with '> ' (markdown blockquote)
    Both forms should appear verbatim in the source notes."""
    out = []
    positions = [i for i, c in enumerate(text) if c == '"']
    pairs = list(zip(positions[::2], positions[1::2]))
    for a, b in pairs:
        content = text[a + 1 : b].strip()
        if len(content) < 10:
            continue
        if re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
            continue
        out.append(content)
    block = []
    for line in text.splitlines():
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            block.append(m.group(1))
        else:
            if block:
                content = " ".join(block).strip()
                if len(content) >= 10 and not re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
                    out.append(content)
                block = []
    if block:
        content = " ".join(block).strip()
        if len(content) >= 10 and not re.search(r"\[\d{4}-\d{2}-\d{2}\]", content):
            out.append(content)
    return out


def verify_quotes(chapter_text, era_notes, corpus_id=None):
    """Extract quoted strings from chapter text; flag any that don't appear verbatim
    in any era note body or title. Titles are included because the prompt
    encourages quoted-noun references like `a piece called "the afternoon sucks"`,
    which would otherwise pollute the unverified list."""
    quotes = extract_quotes(chapter_text)
    haystacks = []
    for n in era_notes:
        body = parse_note_body(n["rel"], corpus_id)
        if body:
            haystacks.append(body)
        title = n.get("title")
        if title:
            haystacks.append(title)
    combined = normalize_text("\n".join(haystacks))
    unverified = [q for q in quotes if normalize_text(q) not in combined]
    return quotes, unverified


CITATION_RE = re.compile(
    r"\[\s*(\d{4}-\d{2}-\d{2}(?:\s*,\s*\d{4}-\d{2}-\d{2})*)\s*\]"
)

# New format: [cited phrase](YYYY-MM-DD). The phrase is captured but not
# used by the resolver — the date alone determines which note is linked.
LINK_CITATION_RE = re.compile(
    r"\[([^\]]+)\]\((\d{4}-\d{2}-\d{2})\)"
)


def _preceding_quote(text, end_pos, lookback=800):
    """Find the quoted passage (block or inline) immediately before end_pos."""
    start = max(0, end_pos - lookback)
    snippet = text[start:end_pos]
    lines = snippet.splitlines()
    block = []
    for line in reversed(lines):
        if re.match(r"^\s*>\s?", line):
            block.append(re.sub(r"^\s*>\s?", "", line))
        elif block:
            break
        elif line.strip() == "":
            continue
        else:
            break
    if block:
        return " ".join(reversed(block)).strip()
    matches = list(re.finditer(r'"([^"]{10,})"', snippet))
    if matches:
        return matches[-1].group(1).strip()
    return None


def resolve_citations(body, all_notes, corpus_id=None):
    """Rewrite citations as Obsidian wikilinks. Handles two formats:
      - Old: [YYYY-MM-DD] -> [[rel|YYYY-MM-DD]]
      - New: [phrase](YYYY-MM-DD) -> [[rel|phrase]]
    When multiple notes share a date, disambiguate via the preceding quote
    (old format) or the link's own phrase (new format)."""
    by_date = {}
    for n in all_notes:
        d = (n.get("date") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(n)

    body_cache = {}
    def body_of(rel):
        if rel not in body_cache:
            body_cache[rel] = normalize_text(parse_note_body(rel, corpus_id) or "")
        return body_cache[rel]

    def pick(date, quote):
        cands = by_date.get(date, [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if quote:
            needle = normalize_text(quote)[:80]
            if needle:
                for n in cands:
                    if needle in body_of(n["rel"]):
                        return n
        return sorted(cands, key=lambda n: n["rel"])[0]

    spans = []
    for m in LINK_CITATION_RE.finditer(body):
        spans.append((m.start(), m.end(), "link", m))
    for m in CITATION_RE.finditer(body):
        s, e = m.span()
        if any(ls <= s < le for ls, le, _, _ in spans):
            continue  # already inside a [phrase](date) link
        spans.append((s, e, "old", m))
    spans.sort(key=lambda x: x[0])

    out = []
    last = 0
    resolved = unresolved = 0
    for s, e, kind, m in spans:
        out.append(body[last:s])
        if kind == "link":
            phrase = m.group(1)
            d = m.group(2)
            note = pick(d, phrase)
            if note:
                rel_no_ext = re.sub(r"\.md$", "", note["rel"])
                out.append(f"[[{rel_no_ext}|{phrase}]]")
                resolved += 1
            else:
                out.append(f"[{phrase}]({d})")
                unresolved += 1
        else:
            dates = [d.strip() for d in m.group(1).split(",")]
            preceding = _preceding_quote(body, s)
            links = []
            for d in dates:
                note = pick(d, preceding)
                if note:
                    rel_no_ext = re.sub(r"\.md$", "", note["rel"])
                    links.append(f"[[{rel_no_ext}|{d}]]")
                    resolved += 1
                else:
                    links.append(f"[{d}]")
                    unresolved += 1
            out.append(", ".join(links))
        last = e
    out.append(body[last:])
    return "".join(out), resolved, unresolved


async def write_chapter(client, era_name, notes, prior_chapters_list=None, prior_digests_list=None,
                        future_chapters_list=None, future_digests_list=None):
    era_msg = build_user_msg(era_name, notes)
    label = f"{era_name}  ({len(notes)} notes"

    # Split prior/future chapters and digests across separate content blocks
    # with the cache marker on the LAST block before era_msg. Within a single
    # request the cache write covers the full reference prefix; across eras,
    # past-only sections have a stable monotone prefix, but enabling --future
    # introduces per-era variation that breaks inter-era prefix caching.
    sections = []  # (open_marker, close_marker, items)
    prior_chapters_list = prior_chapters_list or []
    prior_digests_list = prior_digests_list or []
    future_chapters_list = future_chapters_list or []
    future_digests_list = future_digests_list or []
    if prior_chapters_list:
        sections.append((
            "--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n",
            "--- END PRIOR CHAPTERS ---\n\n",
            prior_chapters_list,
        ))
    if prior_digests_list:
        sections.append((
            "--- PRIOR THREAD DIGESTS (structured per-era state — read alongside the prior chapters) ---\n\n",
            "--- END PRIOR THREAD DIGESTS ---\n\n",
            prior_digests_list,
        ))
    if future_chapters_list:
        sections.append((
            "--- FUTURE CHAPTERS (later eras, drafted in a previous run — for thematic alignment, NOT for events that haven't happened yet in this era; do not foreshadow or anticipate) ---\n\n",
            "--- END FUTURE CHAPTERS ---\n\n",
            future_chapters_list,
        ))
    if future_digests_list:
        sections.append((
            "--- FUTURE THREAD DIGESTS (later eras' digests — same caveat: hindsight context, not events to anticipate) ---\n\n",
            "--- END FUTURE THREAD DIGESTS ---\n\n",
            future_digests_list,
        ))

    user_blocks = []
    for s_idx, (open_m, close_m, items) in enumerate(sections):
        is_last_section = s_idx == len(sections) - 1
        user_blocks.append({"type": "text", "text": open_m})
        for i, item in enumerate(items):
            block = {"type": "text", "text": item + "\n\n"}
            if is_last_section and i == len(items) - 1:
                block["cache_control"] = {"type": "ephemeral"}
            user_blocks.append(block)
        user_blocks.append({"type": "text", "text": close_m})
    user_blocks.append({"type": "text", "text": era_msg})

    in_chars = sum(len(b["text"]) for b in user_blocks)
    log(f"→ {label}, {in_chars:,} chars in)")
    t0 = time.time()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": CHAPTER_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_blocks}],
    )
    dt = time.time() - t0
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"\A##\s+[^\n]+\n+", "", text)
    u = resp.usage
    in_tok = getattr(u, "input_tokens", 0)
    out_tok = getattr(u, "output_tokens", 0)
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    words = len(text.split())
    stop = getattr(resp, "stop_reason", "")
    truncated = " ⚠ TRUNCATED (max_tokens)" if stop == "max_tokens" else ""
    cache_str = f" [cache: {cache_read:,} hit / {cache_write:,} write]" if (cache_read or cache_write) else ""
    log(f"✓ {era_name}  done in {dt:.0f}s  {words} words  (in: {in_tok:,} tok, out: {out_tok:,} tok){cache_str}{truncated}")
    return era_name, text, len(notes)


async def write_summary(client, chapters, by_era):
    parts = []
    for era_name, text, _ in chapters:
        parts.append(f"## {era_heading(era_name, by_era[era_name])}\n\n{text}")
    user_msg = "ERA CHAPTERS:\n\n" + "\n\n".join(parts)
    log(f"→ summary  starting  ({len(user_msg):,} chars in)")
    t0 = time.time()
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=1536,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    dt = time.time() - t0
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    in_tok = getattr(resp.usage, "input_tokens", 0)
    out_tok = getattr(resp.usage, "output_tokens", 0)
    log(f"✓ summary  done in {dt:.0f}s  {len(text.split())} words  (in: {in_tok:,} tok, out: {out_tok:,} tok)")
    return text


async def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    log(f"model: {MODEL}")
    log(f"task variant: {TASK_VARIANT}")
    log(f"future context: {'on' if INCLUDE_FUTURE else 'off'}")
    all_notes = load_corpus_notes()
    n_redated = apply_date_overrides(all_notes)
    verdicts = load_authorship()
    all_notes, n_dropped, n_mixed = apply_authorship(all_notes, verdicts)
    log(f"authorship: dropped {n_dropped} clippings, flagged {n_mixed} mixed notes ({len(verdicts)} verdicts loaded)")
    log(f"date overrides: {n_redated}")
    n_about = apply_note_metadata(all_notes)
    if n_about:
        log(f"note-about overrides: {n_about}")
    n_uncertain = flag_date_clusters(all_notes)
    if n_uncertain:
        log(f"date clusters: flagged {n_uncertain} notes (≥3 sharing a date)")
    by_era = {name: [] for name, _, _ in ERAS}
    skipped_date = 0
    for n in all_notes:
        e = era_of(n.get("date", ""))
        if e is None:
            skipped_date += 1
            continue
        by_era[e].append(n)

    log(f"loaded {len(all_notes)} corpus notes ({skipped_date} dropped for missing date)")
    for name, _, _ in ERAS:
        log(f"  {name}: {len(by_era[name])} notes")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)

    log("")
    eras_with_notes = [name for name, _, _ in ERAS if by_era[name]]
    log(f"writing {len(eras_with_notes)} chapters sequentially, each seeing prior chapters…")

    sha = _prompt_sha()
    dirty = "-dirty" if _is_dirty() else ""
    snapshot_header = (
        f"<!-- prompt_sha: {sha}{dirty}  run: {RUN_STAMP}  "
        f"model: {MODEL}  task: {TASK_VARIANT} -->\n\n"
    )
    BIOGRAPHIES_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_SNAPSHOT.write_text(snapshot_header + CHAPTER_SYSTEM, encoding="utf-8")
    users_parts = [snapshot_header]
    for name in eras_with_notes:
        msg = build_user_msg(
            name, by_era[name],
            prior_digests=load_prior_thread_digests(name),
            future_chapters=load_future_chapters(name) if INCLUDE_FUTURE else None,
            future_digests=load_future_thread_digests(name) if INCLUDE_FUTURE else None,
        )
        users_parts.append(f"=== {name} ===\n\n{msg}\n\n")
    USERS_SNAPSHOT.write_text("".join(users_parts), encoding="utf-8")
    log(f"prompt snapshot: {SYSTEM_SNAPSHOT.name} ({len(CHAPTER_SYSTEM):,} chars system)")
    log(f"prompt snapshot: {USERS_SNAPSHOT.name} ({sum(len(p) for p in users_parts):,} chars users)")

    t_chapters = time.time()

    chapters = []
    for name in eras_with_notes:
        prior_list = [
            f"## {era_heading(n, by_era[n])}\n\n{t}" for n, t, _ in chapters
        ]
        prior_digests_list = [
            f"## {era_heading(en, by_era[en])}\n\n{d}"
            for en, d in load_prior_thread_digests(name)
        ]
        future_list = []
        future_digests_list = []
        if INCLUDE_FUTURE:
            future_list = [
                f"## {era_heading(en, by_era[en])}\n\n{ct}"
                for en, ct in load_future_chapters(name)
            ]
            future_digests_list = [
                f"## {era_heading(en, by_era[en])}\n\n{d}"
                for en, d in load_future_thread_digests(name)
            ]
        result = await write_chapter(
            client, name, by_era[name],
            prior_chapters_list=prior_list,
            prior_digests_list=prior_digests_list,
            future_chapters_list=future_list,
            future_digests_list=future_digests_list,
        )
        _, text, _ = result
        quotes, unverified = verify_quotes(text, by_era[name])
        log(f"  verify {name}: {len(quotes)} quotes, {len(unverified)} unverified")
        for u in unverified:
            preview = u[:100] + ("…" if len(u) > 100 else "")
            log(f"    ⚠ UNVERIFIED: {preview}")
        chapters.append(result)
    log(f"all chapters done in {time.time() - t_chapters:.0f}s")

    log("")
    summary = await write_summary(client, chapters, by_era)

    lines = ["# Fifteen years (2011-2026) — a retrospective", ""]
    lines.append(summary)
    lines.append("")
    for era_name, text, n_total in chapters:
        lines.append(f"## {era_heading(era_name, by_era[era_name])}")
        lines.append("")
        lines.append(f"*{n_total} notes*")
        lines.append("")
        lines.append(text)
        lines.append("")
    body = "\n".join(lines)
    body, n_resolved, n_unresolved = resolve_citations(body, all_notes)
    log(f"citations: {n_resolved} linked, {n_unresolved} unresolved")
    body = snapshot_header + body
    OUT_MD.write_text(body, encoding="utf-8")
    if LATEST_SYMLINK.is_symlink() or LATEST_SYMLINK.exists():
        LATEST_SYMLINK.unlink()
    LATEST_SYMLINK.write_text(body, encoding="utf-8")
    total_words = sum(len(text.split()) for _, text, _, _ in chapters) + len(summary.split())
    log("")
    log(f"saved: {OUT_MD}  ({total_words} words total, {time.time() - START:.0f}s wall)")
    log(f"latest: {LATEST_SYMLINK}")


if __name__ == "__main__":
    asyncio.run(main())
