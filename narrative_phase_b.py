#!/usr/bin/env python3
"""Phase B: synthesize a 15-year narrative from Phase A output.

Reads _phase_a.jsonl, groups notes by era, hands every note's full body
in chronological order to Claude for an era chapter. Each chapter is
written sequentially with prior chapters as context for continuity.
Then writes a top-level summary conditioned on the chapters.

Output: _corpus/artifacts/narratives/_narrative_<task>_<stamp>.md plus
a `_narrative.md` (or `_narrative_tagged.md`) symlink to the latest.

Flags:
  --model {opus-4.6,opus-4.7,sonnet-4.6} — pick model (default opus-4.6)
  --task {paragraph,bullets}             — chapter framing (default paragraph)
  --tags                                 — expose Phase A weight tags to the model
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from anthropic import AsyncAnthropic

START = time.time()


def ts():
    elapsed = int(time.time() - START)
    return f"[{elapsed // 60:02d}:{elapsed % 60:02d}]"


def log(msg):
    print(f"{ts()} {msg}", flush=True)

CORPUS = Path.home() / "notes-archive" / "_corpus"
PHASE_A = CORPUS / "_phase_a.jsonl"
TRIAGE_STATE = CORPUS / "_triage_state.json"
AUTHORSHIP = CORPUS / "_authorship.jsonl"
DATE_OVERRIDES = CORPUS / "_date_overrides.json"
NOTE_ABOUT = CORPUS / "_note_about.json"
PEOPLE = CORPUS / "_people.json"
ERA_CONTEXT_FILE = CORPUS / "_era_context.json"
NARRATIVES_DIR = CORPUS / "artifacts" / "narratives"
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


def _pick_tags() -> bool:
    return "--tags" in sys.argv

USE_TAGS = _pick_tags()

OUT_MD = NARRATIVES_DIR / (
    f"_narrative_{RUN_STAMP}_tagged_{TASK_VARIANT}.md" if USE_TAGS
    else f"_narrative_{RUN_STAMP}_{TASK_VARIANT}.md"
)
SYSTEM_SNAPSHOT = NARRATIVES_DIR / f"_narrative_{RUN_STAMP}_system.md"
USERS_SNAPSHOT = NARRATIVES_DIR / f"_narrative_{RUN_STAMP}_users.md"
LATEST_SYMLINK = NARRATIVES_DIR / ("_narrative_tagged.md" if USE_TAGS else "_narrative.md")


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

RATING_TO_SIG = {5: "keeper", 4: "notable", 3: "notable", 2: "minor", 1: "skip"}

ERAS = [
    ("Amherst I",          "0000-00", "2013-05"),
    ("Junior year",        "2013-06", "2014-08"),
    ("Senior year",        "2014-09", "2015-05"),
    ("Chicago",            "2015-06", "2017-05"),
    ("LA/Amherst/Boston",  "2017-06", "2018-12"),
    ("Boston",             "2019-01", "2020-08"),
    ("New York I",         "2020-09", "2024-10"),
    ("New York II",        "2024-11", "9999-99"),
]

TOTAL_CHAR_CAP = 700_000
MIN_PER_NOTE = 400


NOTE_HEADER_DESCRIPTIONS = {
    False: 'Each note is headed with date, label, and title.',
    True: 'Each note is headed with date, label, a Phase A weight tag (one of "keeper", "notable", "minor", "skip" — a signal-vs-noise judgment from a prior pass), and title. Use the tags to allocate attention. The chapter should be anchored on "keeper" and "notable" notes: most cited specifics, and nearly all block quotes, should come from these. "Minor" notes are background — fine to mention or briefly cite when they fill a gap, but not where the chapter lives. "Skip" notes are mostly noise; ignore them unless one turns out to carry something nothing else does, in which case you can promote it. The default posture is to trust the tags.',
}


CHAPTER_SYSTEM = """You are writing one chapter of a readable retrospective on Andrew's personal writing archive, 2011-2026. Think of it as a thoughtful biographer or a friend who has read everything and is telling Andrew what's in there.

INPUT
You'll receive the full text of every note in one era, chronologically. No pre-filtering, no weighting — the whole archive for this era. __NOTE_HEADER__ Some notes are tagged "⚠ MIXED" meaning they contain quoted material from other writers.

TASK
__TASK__

Length: aim for 900-1500 words, OR 500-800 if the era's archive is thin (few notes, little sustained writing). This is a soft target — exceed it if the material genuinely demands more space (a dense era with multiple distinct strands), and undercut it if the era really is thin. Don't pad, but don't artificially trim either.

STRUCTURE: write the chapter as continuous prose. The chapter title (`##`) is supplied externally; do not add another at that level. Subheaders at `###` are allowed ONLY when the era splits into distinct chronological or geographical chapters of life — e.g., a junior year that goes Hawaii → Amherst → Berkeley could use each location as a `###` heading. The subheader names a place or a clean time-block, nothing else. Do NOT use thematic subheaders ("Hawaii: reading", "What's there at the end") or topic subheaders ("the writing", "his relationships"). If the era is one continuous setting, no subheaders at all.

VOICE — READ THIS CAREFULLY
Plain, direct English. Short sentences are fine. Write so a friend can follow without rereading. Use concrete language. Third person ("Andrew", "he").

DO NOT WRITE LIKE A LITERARY CRITIC. Specifically avoid:
- jargon like "signature gesture", "ars poetica", "revisionary sentence", "the grammar of X", "iconography", "in medias res", "close reading", "the era's keystone"
- highfalutin phrasings like "the prose is doing X", "the sentence earns itself", "a theory of Y", "the register softens"
- abstract pronouncements where a plain description would work
- ornate sentence structures, nested clauses for their own sake, theatrical flourishes

DON'T RANK OR ANOINT. The distinction is between rating the writing's *stature* and describing what the writing *does*. **Block** stature claims: "his best prose", "the strongest writing of the era", "his most accomplished poem", "one of the best things he's ever written", "his earliest X", "his most ambitious Y", "the era's keystone", "his ars poetica", "probably the most important X in the archive", "genuinely finished". These are grades and they pile up fast — a reader forms their own judgment from the quoted passages. **Allow** descriptive observation of what's on the page when it's doing real work: "the register shifts here", "the prose tightens", "the sentences crack open", "this one breaks form". These point at moves a reader can verify in the quote. Test: is this a grade in disguise (could swap for a number), or is it pointing at a specific behavior? One or two stature-claims per chapter is a hard ceiling; prefer zero. If a piece stands out, show it by quoting it. Strong openers are fine — lead with the note that carries voice, even if it falls oddly in time, just don't frame it as a ranking.

If you catch yourself writing a sentence that sounds like a New York Review of Books essay, rewrite it plainer. It's okay to sound less impressive if it means a reader can actually follow.

CLOSINGS CAN SYNTHESIZE. The end of the chapter is the place to step back — pull a thread, name what's recurred, point at where the era leaves him. The closing doesn't need to defer to one specific note; it can speak to patterns across the chapter. Synthesis is not the same as anointing: describing what recurred ("the year keeps returning to X", "by the end he's still circling Y") is fine; ranking what mattered most ("the era's most important moment") is not.

ABSOLUTE RULES (prior runs fabricated details — these rules are non-negotiable)

1. CITATIONS: every concrete detail must end with [YYYY-MM-DD]. The date must match a note you were given. Examples:
   - "the morning breeze — the kehau" [2013-07-27]
   - a backhoe at dawn [2014-10-24]
   Sentences without citations should stay abstract (about themes, patterns, how the writing feels), not about specific incidents.

   **LISTS OF DETAILS NEED PER-ITEM GROUNDING.** When a sentence lists 3+ concrete images ("his father's lawn-care neighbors, his mother yelling at AT&T, dinners that bypass everything important"), each item must either (a) carry its own inline [YYYY-MM-DD], or (b) all come from a single note cited at the end of the sentence. Floating image lists where the reader can't trace any item to a source are not allowed. If you can only ground 1-2 of the items, drop the rest — a shorter list with full grounding beats a longer one without.

2. QUOTES. Two forms — use both. All quoted content must be verbatim, character-for-character, from the source.
   - Inline quotes: in double quotes, ≤30 words, each followed by [YYYY-MM-DD]. For short phrases woven into your prose.
   - Block quotes: set off as a markdown blockquote (each line prefixed with "> "), 30-200 words, followed on the next line by [YYYY-MM-DD]. Aim for 2-5 block quotes per chapter; more is fine if the material warrants it — Andrew's own voice should break up your prose, especially for passages that show how he thinks or writes. Ellipses (…) are permitted to elide passages within a block quote — e.g., to compress a long entry to its strongest beats. Ellipses openly mark the elision and don't fabricate; the non-elided text must still be verbatim.
   If you can't find an exact passage, paraphrase without quote marks.

3. NO INVENTED DETAILS. Don't add colors, locations, times of day, weather, names, or sensory specifics absent from the source. Don't infer biographical facts the notes don't state either — when Andrew arrived somewhere, started a job, met someone, began or ended a relationship. If you're tempted to write "when he arrives at X in YYYY" or "after he started working at Y", stop: the notes rarely announce these transitions, and the year you'd guess from context is usually wrong. Stay vague ("that fall", "around this time", "by YYYY") rather than guessing. This applies especially to *relationships between people*. The rule is about *rights/roles*, not headspace. **Block** labels that imply specific rights or roles the other person would have to ratify: girlfriend, boyfriend, brother, sister, roommate, colleague, best friend, partner, fiancé. These need explicit corroboration in the notes or the PEOPLE block. If a name is unlabeled, just use the name — "Max", not "his brother Max". **Allow** labels that describe Andrew's headspace or observable co-presence: crush, friend, classmate. These are fine when the notes plainly show the dynamic — sustained preoccupation, explicit affection, recurring co-presence over weeks or months. Test: would the other person have to agree to the label for it to be true? Crush, no — that's in Andrew's head. Girlfriend, yes — that requires her too.

This applies to *contextual circumstance details* too: apartment type ("studio", "one-bedroom"), economic framing ("somewhere he can afford", "cheap sublet"), ingestion context ("high at work", "drunk at a bar"), workplace location ("from the office"), commute specifics, and so on. **When a note doesn't establish a detail, omit the detail** — don't replace it with a plausible-sounding placeholder. If you find yourself reaching for a generic descriptor to round out a sentence, cut it. A plainer sentence with fewer details is always better than a richer sentence with invented ones.

**Common knowledge is fine.** Background facts a reasonable reader knows — Amherst is a small college in western Massachusetts, Berkeley is in California, the academic year runs fall through spring — don't need to come from the corpus. Only era-specific or biographical claims about Andrew need grounding. Pattern-based inference *from the corpus itself* is also okay when stated as a pattern rather than a fact: "Jacob recurs across the year's entries" is fine; "his close friend Jacob" is not. The line is: don't invent details, but don't pretend you've never heard of Massachusetts.

If multiple notes share a date, verify your citation by content — the date alone doesn't disambiguate, so read the note you're about to cite and confirm it contains the detail you're describing. When notes are tagged ⚠ DATE-CLUSTER (3+ notes sharing a date), the date likely reflects import time or last-viewed/edited time rather than the original write date. Still cite [YYYY-MM-DD] for linkability, but in prose refer to time vaguely ("that summer", "around this time") rather than asserting the exact date.

**IDENTITY AND AMBITION CLAIMS.** Material in the archive doesn't prove identity or ambition. Don't write "he wants to be a fiction writer", "he becomes a poet", "he tries to be Y" unless a note explicitly says so. Poems in the archive don't make him "a poet trying to make it"; philosophy notes don't make him "studying philosophy." If you can quote him naming the aspiration, fine — otherwise omit the role frame.

**RELATIONAL POSSESSIVES.** A name in a note isn't necessarily Andrew's family or partner. A note saying "grandpa died" may be about a friend's grandpa, a roommate's, a character's. The default for unattributed people is to leave them unattributed ("a grandfather", "someone's grandpa") rather than to default-assign them to Andrew.

**NOT-EVENTS ARE STILL CLAIMS.** "Considering X and then not", "almost Y-ing", "thinking about Z and deciding against" are factual claims requiring the same grounding as the events would. Don't soften an inference into a near-event — it's still an inference, plus a fabricated decision.

**LETTERS ARE DRAFTS.** Notes in the `letter/` folder are almost always drafts kept in the archive, not sent correspondence. Don't assert they were sent — avoid verbs like "sends", "emails", "mails", and avoid "correspondence" (which implies two-way exchange). A letter in second person that signs off with "Andrew" is evidence of the draft form, not evidence of sending. Use "drafts", "writes a letter to", "addressed to", "an unsent piece to". Only claim a letter was sent if a separate note explicitly documents the send (a reply quoted, a later entry confirming send, receipt acknowledged). Titles that assert sending ("I didn't tell anyone I was sending this letter…") are part of the draft's internal framing, not evidence of an actual send.

4. ERA CONTEXT IS GROUND TRUTH. The user message includes an "ERA CONTEXT" block stating where Andrew is, what he's doing, his life stage. Treat it as authoritative — it overrides any inference from the notes or from the era heading. The era heading ("Amherst I", "Chicago") is a chronological label, not evidence; never infer school, job, location, or life stage from it. If ERA CONTEXT says he was in high school in March 2011, do not write "Andrew already at college" because the era is named "Amherst I." If ERA CONTEXT is silent on something, stay silent too.

5. SWEEPING PATTERN CLAIMS NEED PER-INSTANCE GROUNDING. When you describe a recurring pattern using a list of named instances ("Mollie, Sarah, McKenna — different women, the same shape"), every named instance must independently demonstrate the pattern in the source notes. If only three of seven fit and the rest are filler, drop the four that don't. A shorter list of true instances beats a longer list that overstates the pattern.

6. NO COMPOSITE SCENES. Each cited detail traces to a single note, and details from different notes cannot be fused into a single scene. If a sentence names an action, a place, and a time (e.g., "he X's in Y on Z"), all three must come from the same cited note. When two adjacent notes describe different events — even a few weeks apart, even both involving coffee shops — they are not one event. If you're tempted to write "he does A and B in place P" and A comes from one note and B from another, split into two sentences each with its own citation, and only name P if P actually appears in the relevant note.

7. NO FIRST-PERSON for Andrew. No sentimentalizing.

8. MIXED NOTES. Some notes are tagged "⚠ MIXED" — they contain quoted material from other writers (substack posts, song lyrics, book excerpts, news articles, dialogue from films). The quoted material is usually clearly marked: indented "…" blocks, lines following an attribution like "Sacks:" or "Lemire:", text after a "——" separator, or stretches of polished third-person self-help / lyric register that break from Andrew's usual voice. ONLY attribute prose to Andrew that is clearly outside such quoted regions. Never quote the embedded material as if it were his — that's the exact failure mode we're fixing.

9. TRIAGE YOURSELF. You're getting everything, including fragments, lists, and throwaway scribbles. Most of the signal is in a minority of notes. Don't try to cover every note; pick the ones that actually carry weight and use the rest as context.

10. SPARSITY. When an era's archive is thin (few notes, little sustained writing), frame long entries as *exceptional* — "one of the few substantial entries from this stretch" — not representative of months of life. Acknowledge the thinness directly. Don't over-index on whichever long entries happen to exist.

**Proportional coverage within an era:** When density is uneven within an era — e.g., a few months of sustained writing inside an otherwise sparse year — weight your coverage to the dense stretches. Follow the writing's gravity, not the calendar's spacing.

11. CHRONOLOGY. Move through the era roughly in time order. You may group related moments across weeks or months when a theme demands it, but don't skip around in ways that cloud what actually happened. A reader should be able to track what came before what.

12. CONTINUITY. You may be given chapters for earlier eras as prior context. Don't rewrite them or repeat their material. Use them to maintain a consistent voice and to pick up threads (people, preoccupations, patterns) where they left off — including thematic callbacks to earlier-era moments (a recurring image, a person reappearing, a pattern returning). When you reference earlier-era moments by date or wikilink, only use dates/wikilinks that already appear in the prior-chapter context — don't invent earlier-era citations. Don't contradict what they established.

Accuracy and readability both beat vividness. If being accurate and plain makes the prose thinner, that's fine."""

CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__TASK__", TASK_VARIANTS[TASK_VARIANT])
CHAPTER_SYSTEM = CHAPTER_SYSTEM.replace("__NOTE_HEADER__", NOTE_HEADER_DESCRIPTIONS[USE_TAGS])


SUMMARY_SYSTEM = """You are writing the opening synthesis to a retrospective on Andrew's personal writing archive, 2011-2026. Plain third-person voice — a thoughtful biographer, not a literary critic.

You'll receive five era chapters. Write 250-450 words covering the through-lines: what preoccupied him across all fifteen years, what changed, what didn't, what the arc looks like from a distance.

VOICE
Plain, direct English. Short sentences welcome. Write so a friend can follow. Third person. No literary-critic jargon ("revisionary sentence", "ars poetica", "the register", "signature gesture", etc.). No ornate sentence structures for their own sake.

ABSOLUTE RULES
- Work only from what the chapters say. Don't introduce new specifics, names, dates, or quotes.
- If you use a quote or specific detail from a chapter, keep any [YYYY-MM-DD] citation it carries.
- No invented imagery. Abstract patterns are fine without citation; specifics need the source chapter's anchor.
- No "in conclusion", no enumerating the era names, no "this retrospective".
- Third person for Andrew. No sentimentalizing.
- If being accurate and plain makes the paragraph thinner, that's fine."""


def parse_note_body(rel):
    path = CORPUS / rel
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


def load_phase_a():
    out = []
    for line in PHASE_A.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r:
            continue
        out.append(r)
    return out


def apply_triage_overrides(notes):
    if not TRIAGE_STATE.exists():
        return 0, 0
    state = json.loads(TRIAGE_STATE.read_text())
    ratings = state.get("decisions", {})
    applied = 0
    changed = 0
    for n in notes:
        r = ratings.get(n["rel"])
        if r not in RATING_TO_SIG:
            continue
        applied += 1
        new_sig = RATING_TO_SIG[r]
        if n.get("significance") != new_sig:
            changed += 1
        n["significance"] = new_sig
    return applied, changed


def apply_date_overrides(notes):
    """Override a note's date where `date_created` reflects import-time rather
    than actual write-time (e.g., a poem pasted into Apple Notes years later)."""
    if not DATE_OVERRIDES.exists():
        return 0
    overrides = json.loads(DATE_OVERRIDES.read_text())
    applied = 0
    for n in notes:
        new_date = overrides.get(n["rel"])
        if new_date:
            n["date"] = new_date
            applied += 1
    return applied


def apply_note_about(notes):
    """Stamp n["about"] = description for notes that were written on one date
    but are *about* a different period (retrospectives, memoir-mode entries).
    The narrative prompt uses this to avoid placing the note's scenes in the
    write-date's era."""
    if not NOTE_ABOUT.exists():
        return 0
    overrides = json.loads(NOTE_ABOUT.read_text())
    applied = 0
    for n in notes:
        about = overrides.get(n["rel"])
        if about:
            n["about"] = about
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


def load_people():
    """Return {name: description} for recurring people the notes may not
    adequately identify (relationships, context). Used to prevent the model
    from inferring e.g. siblings/partners from narrative context alone."""
    if not PEOPLE.exists():
        return {}
    return json.loads(PEOPLE.read_text())


def load_era_context():
    """Return {era_name: anchor_string} — short biographical anchors per era
    so the model doesn't infer Andrew's life situation from era names alone.
    See ERAS for the canonical list of era names."""
    if not ERA_CONTEXT_FILE.exists():
        return {}
    return json.loads(ERA_CONTEXT_FILE.read_text())


def format_people_block(people):
    if not people:
        return ""
    lines = ["--- PEOPLE (facts the notes may not state — trust these over inference) ---"]
    for name, desc in sorted(people.items()):
        lines.append(f"- {name}: {desc}")
    lines.append("--- END PEOPLE ---")
    return "\n".join(lines)


def load_authorship():
    """Return {rel: verdict} using the latest row per rel (file is append-only
    and may contain human-review overrides at the end)."""
    if not AUTHORSHIP.exists():
        return {}
    latest = {}
    for line in AUTHORSHIP.read_text(encoding="utf-8").splitlines():
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
    """Drop 'no' notes entirely. Tag mixed/unclear with is_mixed=True so the
    prompt can warn Opus to distinguish Andrew's prose from embedded quotes."""
    kept = []
    n_dropped = 0
    n_mixed = 0
    for n in notes:
        v = verdicts.get(n["rel"])
        if v == "no":
            n_dropped += 1
            continue
        n["is_mixed"] = v in ("mixed", "unclear")
        if n["is_mixed"]:
            n_mixed += 1
        kept.append(n)
    return kept, n_dropped, n_mixed


def era_of(date):
    if not date or len(date) < 7:
        return None
    ym = date[:7]
    for name, lo, hi in ERAS:
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


def build_user_msg(era_name, notes, era_context=""):
    sorted_notes = sorted(notes, key=lambda n: n.get("date", ""))
    bodies = []
    for n in sorted_notes:
        body = parse_note_body(n["rel"])
        if not body:
            continue
        bodies.append((n, body))
    total = sum(len(b) for _, b in bodies)
    if total > TOTAL_CHAR_CAP and bodies:
        ratio = TOTAL_CHAR_CAP / total
        bodies = [(n, sample_keeper(b, max(MIN_PER_NOTE, int(len(b) * ratio)))) for n, b in bodies]
    lines = []
    if era_context:
        lines.extend([
            "--- ERA CONTEXT (where Andrew is during this era — use as factual anchor; don't override with inference from the notes) ---",
            era_context,
            "--- END ERA CONTEXT ---",
            "",
        ])
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
        sig_part = f" · {n.get('significance', '')}" if USE_TAGS else ""
        mix = "  ⚠ MIXED — contains quoted material not written by Andrew" if n.get("is_mixed") else ""
        date_warn = ""
        if n.get("date_uncertain"):
            date_warn = f"  ⚠ DATE-CLUSTER ({n.get('date_cluster_size')} notes share this date — likely import or last-edit time, not write time)"
        lines.append(f"=== {date} · {label}{sig_part} · {title}{mix}{date_warn} ===")
        if n.get("about"):
            lines.append(f"  ⚠ ABOUT: {n['about']} — don't place this note's scenes in the write-date's era.")
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


def verify_quotes(chapter_text, era_notes):
    """Extract quoted strings from chapter text; flag any that don't appear verbatim
    in any era note body or title. Titles are included because the prompt
    encourages quoted-noun references like `a piece called "the afternoon sucks"`,
    which would otherwise pollute the unverified list."""
    quotes = extract_quotes(chapter_text)
    haystacks = []
    for n in era_notes:
        body = parse_note_body(n["rel"])
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


def resolve_citations(body, all_notes):
    """Rewrite [YYYY-MM-DD] as Obsidian wikilinks [[rel|YYYY-MM-DD]].
    When multiple notes share a date, disambiguate via the preceding quote."""
    by_date = {}
    for n in all_notes:
        d = (n.get("date") or "")[:10]
        if d:
            by_date.setdefault(d, []).append(n)

    body_cache = {}
    def body_of(rel):
        if rel not in body_cache:
            body_cache[rel] = normalize_text(parse_note_body(rel) or "")
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

    out = []
    last = 0
    resolved = unresolved = 0
    for m in CITATION_RE.finditer(body):
        s, e = m.span()
        out.append(body[last:s])
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


async def write_chapter(client, era_name, notes, prior_chapters_list=None, people=None, era_context=""):
    n_keepers = sum(1 for n in notes if n.get("significance") == "keeper")
    era_msg = build_user_msg(era_name, notes, era_context=era_context)
    label = f"{era_name}  ({len(notes)} notes"

    # Split prior chapters across separate content blocks with the cache marker
    # on the LAST one. Each chapter's block content is identical between requests
    # (older chapters' text is fixed once written), so the prefix matches and the
    # next request hits cache on system + opener + chapter1 + ... + chapter_{N-1},
    # writing only the new last chapter.
    user_blocks = []
    prior_chapters_list = prior_chapters_list or []
    if prior_chapters_list:
        user_blocks.append({
            "type": "text",
            "text": "--- PRIOR CHAPTERS (earlier eras in this retrospective — for continuity only; do not rewrite or repeat) ---\n\n",
        })
        for i, ch in enumerate(prior_chapters_list):
            block = {"type": "text", "text": ch + "\n\n"}
            if i == len(prior_chapters_list) - 1:
                block["cache_control"] = {"type": "ephemeral"}
            user_blocks.append(block)
        user_blocks.append({"type": "text", "text": "--- END PRIOR CHAPTERS ---\n\n"})
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
    return era_name, text, n_keepers, len(notes)


async def write_summary(client, chapters, by_era):
    parts = []
    for era_name, text, _, _ in chapters:
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
    if not PHASE_A.exists():
        print(f"ERROR: {PHASE_A} not found — run analysis_phase_a.py first.", file=sys.stderr)
        sys.exit(1)

    log(f"model: {MODEL}")
    log(f"task variant: {TASK_VARIANT}")
    log(f"phase A tags exposed: {USE_TAGS}")
    all_notes = load_phase_a()
    applied, changed = apply_triage_overrides(all_notes)
    n_redated = apply_date_overrides(all_notes)
    verdicts = load_authorship()
    all_notes, n_dropped, n_mixed = apply_authorship(all_notes, verdicts)
    log(f"authorship: dropped {n_dropped} clippings, flagged {n_mixed} mixed notes ({len(verdicts)} verdicts loaded)")
    log(f"date overrides: {n_redated}")
    n_about = apply_note_about(all_notes)
    if n_about:
        log(f"note-about overrides: {n_about}")
    n_uncertain = flag_date_clusters(all_notes)
    if n_uncertain:
        log(f"date clusters: flagged {n_uncertain} notes (≥3 sharing a date)")
    era_context_map = load_era_context()
    if era_context_map:
        n_filled = sum(1 for v in era_context_map.values() if v)
        log(f"era context: {n_filled}/{len(era_context_map)} eras filled")
    by_era = {name: [] for name, _, _ in ERAS}
    skipped_date = 0
    for n in all_notes:
        e = era_of(n.get("date", ""))
        if e is None:
            skipped_date += 1
            continue
        by_era[e].append(n)

    log(f"loaded {len(all_notes)} Phase A entries ({skipped_date} dropped for missing date)")
    log(f"applied {applied} human ratings ({changed} overrode Phase A bucket)")
    for name, _, _ in ERAS:
        ns = by_era[name]
        kcount = sum(1 for n in ns if n.get("significance") == "keeper")
        ncount = sum(1 for n in ns if n.get("significance") == "notable")
        log(f"  {name}: {len(ns)} notes  ({kcount} keepers, {ncount} notable)")

    client = AsyncAnthropic(max_retries=MAX_RETRIES)

    log("")
    eras_with_notes = [name for name, _, _ in ERAS if by_era[name]]
    log(f"writing {len(eras_with_notes)} chapters sequentially, each seeing prior chapters…")

    sha = _prompt_sha()
    dirty = "-dirty" if _is_dirty() else ""
    snapshot_header = (
        f"<!-- prompt_sha: {sha}{dirty}  run: {RUN_STAMP}  "
        f"model: {MODEL}  task: {TASK_VARIANT}  tags: {USE_TAGS} -->\n\n"
    )
    NARRATIVES_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEM_SNAPSHOT.write_text(snapshot_header + CHAPTER_SYSTEM, encoding="utf-8")
    users_parts = [snapshot_header]
    for name in eras_with_notes:
        msg = build_user_msg(name, by_era[name], era_context=era_context_map.get(name, ""))
        users_parts.append(f"=== {name} ===\n\n{msg}\n\n")
    USERS_SNAPSHOT.write_text("".join(users_parts), encoding="utf-8")
    log(f"prompt snapshot: {SYSTEM_SNAPSHOT.name} ({len(CHAPTER_SYSTEM):,} chars system)")
    log(f"prompt snapshot: {USERS_SNAPSHOT.name} ({sum(len(p) for p in users_parts):,} chars users)")

    t_chapters = time.time()

    chapters = []
    for name in eras_with_notes:
        prior_list = [
            f"## {era_heading(n, by_era[n])}\n\n{t}" for n, t, _, _ in chapters
        ]
        # First-pass deliberately omits PEOPLE block: putting names in the
        # prompt biases the model toward mentioning them. Relationship
        # accuracy gets caught by check_relationships.py post-hoc instead.
        result = await write_chapter(
            client, name, by_era[name],
            prior_chapters_list=prior_list,
            people=None,
            era_context=era_context_map.get(name, ""),
        )
        _, text, _, _ = result
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
    for era_name, text, n_keepers, n_total in chapters:
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
