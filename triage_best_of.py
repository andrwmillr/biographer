#!/usr/bin/env python3
"""Triage best-of candidates — Phase A defaults + manual 1-4 overrides.

Serves http://localhost:8765/. Every note has a rating on the 1-4 scale
(same cardinality as Phase A's significance labels): Phase A provides
the default, Andrew's manual choice overrides it. Filter pills at top
narrow the queue by effective rating and by rated/unrated status.

Phase A → rating:
  skip    → 1
  minor   → 2
  notable → 3
  keeper  → 4

State file (_triage_state.json) stores ONLY Andrew's manual overrides.
Notes absent from the file fall through to their Phase A default.
"""
import http.server
import json
import random
import re
import socketserver
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
from write_biography import ERAS, era_of  # type: ignore

CORPUS = Path.home() / "notes-archive" / "_corpus"
NOTES_DIR = CORPUS / "notes"
PHASE_A = CORPUS / "_derived" / "_phase_a.jsonl"
WRITING_LABELS = ["journal", "creative", "poetry", "letter"]
STATE_PATH = CORPUS / "_config" / "_triage_state.json"
PORT = 8765
BATCH_SIZE = 15
RATINGS = (1, 2, 3, 4)

SIG_TO_RATING = {
    "skip": 1,
    "minor": 2,
    "notable": 3,
    "keeper": 4,
}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"decisions": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_phase_a():
    out = {}
    if not PHASE_A.exists():
        return out
    for line in PHASE_A.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r or "rel" not in r:
            continue
        out[r["rel"]] = {
            "sig": r.get("significance", ""),
            "kernel": (r.get("kernel") or "").strip(),
            "themes": r.get("themes") or [],
            "date": r.get("date", ""),
        }
    return out


def collect_queue():
    rels = []
    for label in WRITING_LABELS:
        d = NOTES_DIR / label
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            rels.append(str(p.relative_to(CORPUS)))
    random.Random(42).shuffle(rels)
    return rels


def parse_note(rel):
    path = NOTES_DIR / rel
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {"title": "", "date": "", "body": text.strip()}
    fm, body = m.group(1), m.group(2).lstrip("\n")
    title = ""
    date = ""
    for line in fm.splitlines():
        s = line.strip()
        if s.startswith("title:"):
            title = s.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("date_created:"):
            date = s.split(":", 1)[1].strip().strip('"').strip("'")
    return {"title": title, "date": date[:10], "body": body.strip()}


def escape(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def body_to_html(body):
    paragraphs = re.split(r"\n\s*\n", body)
    out = []
    for p in paragraphs:
        esc = escape(p).replace("\n", "<br>")
        esc = re.sub(
            r"(https?://[^\s<]+)",
            r'<a href="\1" target="_blank" rel="noopener">\1</a>',
            esc,
        )
        out.append(f"<p>{esc}</p>")
    return "\n".join(out)


def effective_rating(rel, decisions, phase_a):
    if rel in decisions:
        return decisions[rel]
    sig = phase_a.get(rel, {}).get("sig", "")
    return SIG_TO_RATING.get(sig, 2)


ERA_NAMES = [name for name, _, _ in ERAS]


def overall_counts(queue, decisions, phase_a):
    c = {"total": len(queue), "rated": 0, "unrated": 0}
    for r in RATINGS:
        c[f"r{r}"] = 0
    for name in ERA_NAMES:
        c[f"e_{name}"] = 0
    for rel in queue:
        c[f"r{effective_rating(rel, decisions, phase_a)}"] += 1
        if rel in decisions:
            c["rated"] += 1
        else:
            c["unrated"] += 1
        e = era_of(phase_a.get(rel, {}).get("date", ""))
        if e:
            c[f"e_{e}"] += 1
    return c


def parse_filters(qs):
    ratings_raw = qs.get("rating", [""])[0]
    ratings = set()
    for part in ratings_raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) in RATINGS:
            ratings.add(int(part))
    status = qs.get("status", ["all"])[0]
    if status not in ("all", "rated", "unrated"):
        status = "all"
    eras_raw = qs.get("era", [""])[0]
    eras = {e.strip() for e in eras_raw.split(",") if e.strip() in ERA_NAMES}
    return ratings, status, eras


def filter_queue(queue, decisions, phase_a, f_ratings, f_status, f_eras):
    out = []
    for rel in queue:
        is_rated = rel in decisions
        if f_status == "rated" and not is_rated:
            continue
        if f_status == "unrated" and is_rated:
            continue
        r = effective_rating(rel, decisions, phase_a)
        if f_ratings and r not in f_ratings:
            continue
        if f_eras:
            e = era_of(phase_a.get(rel, {}).get("date", ""))
            if e not in f_eras:
                continue
        out.append(rel)
    return out


INDEX_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Triage best-of</title>
<style>
  body { font-family: Georgia, "Iowan Old Style", serif; max-width: 820px; margin: 0 auto; padding: 1rem; background: #1a1a1a; color: #e0e0e0; }
  .header { position: sticky; top: 0; background: #1a1a1a; padding: 0.6rem 0 0.75rem; border-bottom: 1px solid #333; margin-bottom: 1.25rem; z-index: 10; }
  .counts { font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem; color: #aaa; margin-bottom: 0.5rem; }
  .counts b { color: #e0e0e0; }
  .counts span { margin-right: 0.9rem; }
  .filters { display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: center; font-family: ui-monospace, Menlo, monospace; font-size: 0.8rem; }
  .filters .label { color: #777; margin-right: 0.3rem; }
  .pill { padding: 0.22rem 0.55rem; border-radius: 99px; border: 1px solid #444; background: #222; color: #bbb; cursor: pointer; user-select: none; }
  .pill:hover { border-color: #666; color: #e0e0e0; }
  .pill.active { background: #88c; border-color: #aac; color: #000; }
  .pill.r1.active { background: #c55; border-color: #e77; }
  .pill.r2.active { background: #c85; border-color: #ea7; }
  .pill.r3.active { background: #cc6; border-color: #ee8; }
  .pill.r4.active { background: #6c6; border-color: #8e8; }
  .pill.era-pill.active { background: #9ac; border-color: #bce; color: #000; }
  .era-badge { display: inline-block; padding: 0.08rem 0.45rem; border-radius: 3px; background: #334; color: #aac; font-size: 0.72rem; font-weight: bold; }
  .spacer { width: 0.6rem; }
  .hint { margin-top: 0.45rem; font-size: 0.78rem; color: #777; font-family: ui-monospace, Menlo, monospace; }
  .hint b { color: #bbb; }
  .note { border: 1px solid #333; padding: 1rem 1.3rem; margin-bottom: 1.5rem; border-radius: 8px; background: #1f1f1f; }
  .note.active { border-color: #88c; box-shadow: 0 0 0 2px #88c; }
  .note.yours { border-left: 3px solid #88c; }
  .note-meta { color: #888; font-size: 0.78rem; margin-bottom: 0.4rem; font-family: ui-monospace, Menlo, monospace; display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .note-meta .path { color: #999; }
  .sig { display: inline-block; padding: 0.08rem 0.45rem; border-radius: 3px; font-weight: bold; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }
  .sig.keeper { background: #6c6; color: #000; }
  .sig.notable { background: #cc6; color: #000; }
  .sig.minor { background: #555; color: #ccc; }
  .sig.skip { background: #444; color: #888; }
  .yours-tag { color: #88c; font-weight: bold; }
  .note-title { font-size: 1.2rem; margin: 0 0 0.4rem 0; color: #f0f0f0; }
  .kernel { color: #aac; font-size: 0.88rem; font-style: italic; margin-bottom: 0.8rem; line-height: 1.4; }
  .themes { color: #777; font-size: 0.75rem; font-family: ui-monospace, Menlo, monospace; margin-bottom: 0.8rem; }
  .note-body { font-size: 1rem; line-height: 1.55; }
  .note-body p { margin: 0 0 0.9em 0; }
  .note-body a { color: #9ac; }
  .buttons { margin-top: 1rem; display: flex; gap: 0.4rem; align-items: center; }
  .buttons button { padding: 0.4rem 0.85rem; font-size: 0.9rem; border: 1px solid #555; background: #2a2a2a; color: #bbb; border-radius: 4px; cursor: pointer; font-family: ui-monospace, Menlo, monospace; min-width: 2.2rem; }
  .buttons button:hover { background: #3a3a3a; color: #e0e0e0; }
  .buttons button.r1.current { background: #c55; border-color: #e77; color: #000; font-weight: bold; }
  .buttons button.r2.current { background: #c85; border-color: #ea7; color: #000; font-weight: bold; }
  .buttons button.r3.current { background: #cc6; border-color: #ee8; color: #000; font-weight: bold; }
  .buttons button.r4.current { background: #6c6; border-color: #8e8; color: #000; font-weight: bold; }
  .buttons .source { margin-left: 0.8rem; color: #777; font-size: 0.75rem; font-family: ui-monospace, Menlo, monospace; }
  .buttons button.clear { color: #999; min-width: auto; padding: 0.4rem 0.7rem; }
  .done { text-align: center; padding: 3rem 1rem; font-size: 1.1rem; color: #aaa; }
  .loading { text-align: center; padding: 1.5rem; color: #888; font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem; }
</style>
</head>
<body>
<div class="header">
  <div class="counts">
    <span>Queue: <b id="count-filtered">__FILTERED__</b> / <b>__TOTAL__</b></span>
    <span>Rated by you: <b id="count-rated">__RATED__</b></span>
    <span>4 keeper: <b id="count-r4">__N_R4__</b></span>
    <span>3 notable: <b id="count-r3">__N_R3__</b></span>
    <span>2 minor: <b id="count-r2">__N_R2__</b></span>
    <span>1 skip: <b id="count-r1">__N_R1__</b></span>
  </div>
  <div class="filters">
    <span class="label">rating:</span>
    <span class="pill rating-pill r4" data-rating="4">4 keeper <span class="pill-count">__N_R4__</span></span>
    <span class="pill rating-pill r3" data-rating="3">3 notable <span class="pill-count">__N_R3__</span></span>
    <span class="pill rating-pill r2" data-rating="2">2 minor <span class="pill-count">__N_R2__</span></span>
    <span class="pill rating-pill r1" data-rating="1">1 skip <span class="pill-count">__N_R1__</span></span>
    <span class="spacer"></span>
    <span class="label">status:</span>
    <span class="pill status-pill" data-status="all">all</span>
    <span class="pill status-pill" data-status="unrated">unrated <span class="pill-count">__UNRATED__</span></span>
    <span class="pill status-pill" data-status="rated">rated <span class="pill-count">__RATED__</span></span>
  </div>
  <div class="filters" style="margin-top: 0.3rem;">
    <span class="label">era:</span>
    __ERA_PILLS__
  </div>
  <div class="hint"><b>1</b>-<b>4</b> rate · <b>0</b> clear · <b>↑↓</b> move · click pills to filter</div>
</div>
<div id="notes-container">
__NOTES_HTML__
</div>
<div id="sentinel" class="loading">loading more…</div>
<script>
let activeIdx = 0;
let loading = false;
let exhausted = __EXHAUSTED__;
let filterRatings = new Set(__FILTER_RATINGS__);
let filterStatus = "__FILTER_STATUS__";
let filterEras = new Set(__FILTER_ERAS__);

function getNotes() { return Array.from(document.querySelectorAll('.note')); }

function setActive(i) {
  const ns = getNotes();
  if (ns.length === 0) return;
  activeIdx = Math.max(0, Math.min(ns.length - 1, i));
  ns.forEach((n, j) => n.classList.toggle('active', j === activeIdx));
  ns[activeIdx].scrollIntoView({behavior: 'smooth', block: 'center'});
}

function updateCountsInDom(c) {
  document.getElementById('count-filtered').textContent = c.filtered;
  document.getElementById('count-rated').textContent = c.rated;
  for (const r of [1,2,3,4]) {
    document.getElementById('count-r' + r).textContent = c['r' + r];
  }
  document.querySelectorAll('.rating-pill').forEach(p => {
    const r = p.dataset.rating;
    const span = p.querySelector('.pill-count');
    if (span) span.textContent = c['r' + r];
  });
  document.querySelectorAll('.status-pill').forEach(p => {
    const s = p.dataset.status;
    const span = p.querySelector('.pill-count');
    if (span && c[s] !== undefined) span.textContent = c[s];
  });
  document.querySelectorAll('.era-pill').forEach(p => {
    const e = p.dataset.era;
    const span = p.querySelector('.pill-count');
    if (span) span.textContent = c['e_' + e] || 0;
  });
}

function bindButtons(scope) {
  scope.querySelectorAll('.buttons button').forEach(btn => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => {
      const note = btn.closest('.note');
      const rating = btn.dataset.rating;
      if (rating === 'clear') mark(note, null);
      else mark(note, parseInt(rating, 10));
    });
  });
}

async function mark(noteEl, rating) {
  const rel = noteEl.dataset.rel;
  const res = await fetch('/mark', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rel, rating})
  });
  const data = await res.json();
  updateNoteUi(noteEl, data.effective, data.rated_by_you);
  updateCountsInDom(data.counts);
}

function updateNoteUi(noteEl, effective, ratedByYou) {
  noteEl.classList.toggle('yours', ratedByYou);
  noteEl.querySelectorAll('.buttons button').forEach(b => {
    b.classList.toggle('current', b.dataset.rating == String(effective));
  });
  const src = noteEl.querySelector('.source');
  if (src) src.textContent = ratedByYou ? 'yours' : 'phase a';
  const tag = noteEl.querySelector('.yours-tag');
  if (tag) tag.style.display = ratedByYou ? '' : 'none';
}

function currentQuery() {
  const params = new URLSearchParams();
  const rs = [...filterRatings].sort().join(',');
  if (rs) params.set('rating', rs);
  if (filterStatus && filterStatus !== 'all') params.set('status', filterStatus);
  const es = [...filterEras].join(',');
  if (es) params.set('era', es);
  return params.toString();
}

function applyFilters() {
  const q = currentQuery();
  window.location.href = '/' + (q ? '?' + q : '');
}

function initFilterPills() {
  document.querySelectorAll('.rating-pill').forEach(p => {
    const r = parseInt(p.dataset.rating, 10);
    if (filterRatings.has(r)) p.classList.add('active');
    p.addEventListener('click', () => {
      if (filterRatings.has(r)) filterRatings.delete(r);
      else filterRatings.add(r);
      applyFilters();
    });
  });
  document.querySelectorAll('.status-pill').forEach(p => {
    if (p.dataset.status === filterStatus) p.classList.add('active');
    p.addEventListener('click', () => {
      filterStatus = p.dataset.status;
      applyFilters();
    });
  });
  document.querySelectorAll('.era-pill').forEach(p => {
    const e = p.dataset.era;
    if (filterEras.has(e)) p.classList.add('active');
    p.addEventListener('click', () => {
      if (filterEras.has(e)) filterEras.delete(e);
      else filterEras.add(e);
      applyFilters();
    });
  });
}

async function loadMore() {
  if (loading || exhausted) return;
  loading = true;
  const loaded = getNotes().map(n => n.dataset.rel);
  try {
    const res = await fetch('/more', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        loaded, count: __BATCH__,
        rating: [...filterRatings],
        status: filterStatus,
        era: [...filterEras]
      })
    });
    const data = await res.json();
    if (data.html) {
      const container = document.getElementById('notes-container');
      const tmp = document.createElement('div');
      tmp.innerHTML = data.html;
      while (tmp.firstChild) container.appendChild(tmp.firstChild);
      bindButtons(container);
    }
    if (data.exhausted) {
      exhausted = true;
      const s = document.getElementById('sentinel');
      s.textContent = '— end of queue —';
      s.classList.remove('loading');
    }
  } finally {
    loading = false;
  }
}

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowDown') { setActive(activeIdx + 1); e.preventDefault(); return; }
  if (e.key === 'ArrowUp') { setActive(activeIdx - 1); e.preventDefault(); return; }
  if (['1','2','3','4'].includes(e.key)) {
    const ns = getNotes();
    const note = ns[activeIdx];
    if (!note) return;
    mark(note, parseInt(e.key, 10));
    e.preventDefault();
  } else if (e.key === '0') {
    const ns = getNotes();
    const note = ns[activeIdx];
    if (!note) return;
    mark(note, null);
    e.preventDefault();
  }
});

initFilterPills();
bindButtons(document);
const initial = getNotes();
if (initial.length > 0) setActive(0);

const observer = new IntersectionObserver(entries => {
  if (entries[0].isIntersecting) loadMore();
}, {rootMargin: '400px'});
observer.observe(document.getElementById('sentinel'));
</script>
</body>
</html>
"""


def render_note(rel, decisions, phase_a):
    n = parse_note(rel)
    pa = phase_a.get(rel, {})
    sig = pa.get("sig", "")
    kernel = pa.get("kernel", "")
    themes = pa.get("themes", [])
    era = era_of(pa.get("date", "")) or ""
    rated_by_you = rel in decisions
    effective = effective_rating(rel, decisions, phase_a)
    sig_html = f'<span class="sig {escape(sig)}">{escape(sig)}</span>' if sig else ""
    era_html = f'<span class="era-badge">{escape(era)}</span>' if era else ""
    yours_style = "" if rated_by_you else 'style="display:none"'
    note_cls = "note" + (" yours" if rated_by_you else "")
    kernel_html = f'<div class="kernel">{escape(kernel)}</div>' if kernel else ""
    themes_html = (
        f'<div class="themes">{escape(" · ".join(themes))}</div>' if themes else ""
    )
    label_by_rating = {1: "skip", 2: "minor", 3: "notable", 4: "keeper"}
    buttons = "".join(
        f'<button class="r{r}{" current" if r == effective else ""}" data-rating="{r}">{r} {label_by_rating[r]}</button>'
        for r in reversed(RATINGS)
    )
    buttons += '<button class="clear" data-rating="clear">clear</button>'
    source = "yours" if rated_by_you else "phase a"
    return (
        f'<div class="{note_cls}" data-rel="{escape(rel)}">'
        f'  <div class="note-meta">'
        f'    {sig_html}'
        f'    {era_html}'
        f'    <span class="yours-tag" {yours_style}>✓ yours</span>'
        f'    <span class="path">{escape(rel)}</span>'
        f'    <span>· {escape(n["date"])}</span>'
        f'  </div>'
        f'  <h2 class="note-title">{escape(n["title"]) or "(untitled)"}</h2>'
        f'  {kernel_html}'
        f'  {themes_html}'
        f'  <div class="note-body">{body_to_html(n["body"])}</div>'
        f'  <div class="buttons">{buttons}<span class="source">{source}</span></div>'
        f'</div>'
    )


def render_notes(batch, decisions, phase_a):
    return "\n".join(render_note(rel, decisions, phase_a) for rel in batch)


def render_era_pills(oc):
    parts = []
    for name in ERA_NAMES:
        parts.append(
            f'<span class="pill era-pill" data-era="{escape(name)}">'
            f'{escape(name)} <span class="pill-count">{oc.get(f"e_{name}", 0)}</span>'
            f'</span>'
        )
    return "\n    ".join(parts)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            qs = parse_qs(parsed.query)
            f_ratings, f_status, f_eras = parse_filters(qs)
            state = load_state()
            phase_a = load_phase_a()
            queue = collect_queue()
            decisions = state["decisions"]
            oc = overall_counts(queue, decisions, phase_a)
            filtered = filter_queue(queue, decisions, phase_a, f_ratings, f_status, f_eras)
            batch = filtered[:BATCH_SIZE]
            exhausted = len(filtered) <= BATCH_SIZE
            html = (
                INDEX_HTML.replace("__TOTAL__", str(oc["total"]))
                .replace("__FILTERED__", str(len(filtered)))
                .replace("__RATED__", str(oc["rated"]))
                .replace("__UNRATED__", str(oc["unrated"]))
                .replace("__BATCH__", str(BATCH_SIZE))
                .replace("__EXHAUSTED__", "true" if exhausted else "false")
                .replace("__FILTER_RATINGS__", json.dumps(sorted(f_ratings)))
                .replace("__FILTER_STATUS__", f_status)
                .replace("__FILTER_ERAS__", json.dumps(sorted(f_eras)))
                .replace("__ERA_PILLS__", render_era_pills(oc))
            )
            for r in RATINGS:
                html = html.replace(f"__N_R{r}__", str(oc[f"r{r}"]))
            if not batch:
                summary = (
                    f"{oc['rated']} of {oc['total']} rated by you · "
                    + " · ".join(f"{r}: {oc[f'r{r}']}" for r in reversed(RATINGS))
                )
                html = html.replace(
                    "__NOTES_HTML__",
                    f'<div class="done">No notes match current filters.<br><br>{summary}</div>',
                )
            else:
                html = html.replace("__NOTES_HTML__", render_notes(batch, decisions, phase_a))
            self._send_html(html)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/mark":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            rel = data["rel"]
            rating = data.get("rating")
            state = load_state()
            phase_a = load_phase_a()
            if rating is None:
                state["decisions"].pop(rel, None)
            else:
                if not isinstance(rating, int) or rating not in RATINGS:
                    return self._send_json({"error": "bad rating"}, 400)
                state["decisions"][rel] = rating
            save_state(state)
            queue = collect_queue()
            oc = overall_counts(queue, state["decisions"], phase_a)
            eff = effective_rating(rel, state["decisions"], phase_a)
            oc["filtered"] = oc["total"]
            self._send_json({
                "counts": oc,
                "effective": eff,
                "rated_by_you": rel in state["decisions"],
            })
        elif self.path == "/more":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            loaded = set(data.get("loaded", []))
            count = int(data.get("count", BATCH_SIZE))
            f_ratings = {int(x) for x in data.get("rating", []) if int(x) in RATINGS}
            f_status = data.get("status", "all")
            if f_status not in ("all", "rated", "unrated"):
                f_status = "all"
            f_eras = {e for e in data.get("era", []) if e in ERA_NAMES}
            state = load_state()
            phase_a = load_phase_a()
            queue = collect_queue()
            decisions = state["decisions"]
            filtered = filter_queue(queue, decisions, phase_a, f_ratings, f_status, f_eras)
            available = [r for r in filtered if r not in loaded]
            batch = available[:count]
            html = render_notes(batch, decisions, phase_a) if batch else ""
            exhausted = len(available) <= count
            self._send_json({"html": html, "exhausted": exhausted})
        else:
            self.send_error(404)

    def log_message(self, *args, **kwargs):
        pass


def main():
    queue = collect_queue()
    state = load_state()
    phase_a = load_phase_a()
    oc = overall_counts(queue, state["decisions"], phase_a)
    print(f"Queue: {oc['total']} notes  |  rated by you: {oc['rated']}  |  phase a loaded: {len(phase_a)}")
    print(f"Distribution: 4 keeper {oc['r4']} · 3 notable {oc['r3']} · 2 minor {oc['r2']} · 1 skip {oc['r1']}")
    print(f"State: {STATE_PATH}")
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}/"
        print(f"Serving at {url}")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
