import { useEffect, useRef, useState } from "react";
import { TimelineSidebar } from "./TimelineSidebar";
import { authHeaders } from "./auth";
import type { Note } from "./types";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type StagedNote = {
  rel: string;
  date: string;
  era: string;
  title: string;
  body: string;
  highlights: string[];
};

type DealtNote = {
  rel: string;
  date: string;
  title: string;
  era: string;
  body: string;
};

type Props = { apiBase: string; wsBase: string; model: string; readOnly?: boolean };
type Era = { name: string; start: string | null; end: string | null };

const TRUNCATE = 250;

/* Era label colors — saturated but not garish */
const ERA_COLORS = [
  "#c06a2a", // amber
  "#4a8c32", // green
  "#b8366b", // magenta
  "#2a7ab8", // blue
  "#a07028", // gold
  "#1a9a7a", // teal
  "#8a3ab8", // purple
  "#b89a1a", // mustard
  "#2a5a9a", // navy
  "#c04040", // red
];

function eraColor(era?: string): string {
  if (!era) return ERA_COLORS[0];
  let h = 0;
  for (let i = 0; i < era.length; i++) h = ((h << 5) - h + era.charCodeAt(i)) | 0;
  return ERA_COLORS[((h % ERA_COLORS.length) + ERA_COLORS.length) % ERA_COLORS.length];
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function _eraForDate(date: string, eras: Era[]): string {
  const ym = date.slice(0, 7);
  for (const e of eras) {
    const lo = e.start ?? "";
    const hi = e.end ?? "9999";
    if (ym >= lo && ym <= hi) return e.name;
  }
  return "";
}
void _eraForDate;


function isUntitled(title: string): boolean {
  return !title || /^\(?untitled\)?$/i.test(title.trim());
}

function normQuotes(s: string): string {
  return s
    .replace(/[''′]/g, "'")
    .replace(/[""″]/g, '"')
    .replace(/[–—]/g, "-");
}

function highlightedNote(
  noteBody: string,
  passageBody: string,
): React.ReactNode[] {
  const normBody = normQuotes(noteBody);
  const lines = passageBody
    .split(/\n· · ·\n/)
    .flatMap((chunk) => chunk.split("\n"))
    .map((l) => l.trim())
    .filter(Boolean);

  function findInBody(needle: string): [number, number] | null {
    const idx = normBody.indexOf(needle);
    if (idx >= 0) return [idx, idx + needle.length];
    const wsBody = normBody.replace(/\s+/g, " ");
    const wsNeedle = needle.replace(/\s+/g, " ");
    const ni = wsBody.indexOf(wsNeedle);
    if (ni < 0) return null;
    let oi = 0, ci = 0;
    while (ci < ni && oi < noteBody.length) {
      if (/\s/.test(noteBody[oi])) {
        while (oi < noteBody.length && /\s/.test(noteBody[oi])) oi++;
        ci++;
      } else { oi++; ci++; }
    }
    const start = oi;
    const endNorm = ni + wsNeedle.length;
    while (ci < endNorm && oi < noteBody.length) {
      if (/\s/.test(noteBody[oi])) {
        while (oi < noteBody.length && /\s/.test(noteBody[oi])) oi++;
        ci++;
      } else { oi++; ci++; }
    }
    return [start, oi];
  }

  const ranges: [number, number][] = [];
  for (const line of lines) {
    const normLine = normQuotes(line);
    let match = findInBody(normLine);
    if (!match) {
      const stripped = normLine.replace(/^["']|["']$/g, "").trim();
      if (stripped !== normLine) match = findInBody(stripped);
    }
    if (match) ranges.push(match);
  }

  if (!ranges.length) return [<span key="all">{noteBody}</span>];

  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [ranges[0]];
  for (let i = 1; i < ranges.length; i++) {
    const last = merged[merged.length - 1];
    const gap = noteBody.slice(last[1], ranges[i][0]);
    if (ranges[i][0] <= last[1] || /^\s*$/.test(gap)) {
      last[1] = Math.max(last[1], ranges[i][1]);
    } else {
      merged.push(ranges[i]);
    }
  }

  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (cursor < start)
      nodes.push(<span key={`c-${start}`}>{noteBody.slice(cursor, start)}</span>);
    nodes.push(
      <mark key={`h-${start}`} className="bg-amber-100 rounded-sm px-0.5 -mx-0.5">
        {noteBody.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  if (cursor < noteBody.length)
    nodes.push(<span key="tail">{noteBody.slice(cursor)}</span>);
  return nodes;
}

/* ------------------------------------------------------------------ */
/*  Main component                                                     */
/* ------------------------------------------------------------------ */

export function CommonplaceWorkspace({ apiBase, readOnly }: Props) {
  // Staged notes (persisted in staging.json).
  const [staged, setStaged] = useState<StagedNote[]>([]);

  // Browse (Read tab) — paginated chronological notes.
  const [browseIndex, setBrowseIndex] = useState<{ rel: string; date: string; title: string; era: string }[]>([]);
  const [browseNotes, setBrowseNotes] = useState<(DealtNote & { staged?: boolean })[]>([]);
  const [browseOffset, setBrowseOffset] = useState(0);
  const [browseTotal, setBrowseTotal] = useState(0);

  // Dismissed notes view (sub-mode of Read).
  const [showDismissed, setShowDismissed] = useState(false);
  const [dismissedNotes, setDismissedNotes] = useState<DealtNote[]>([]);
  const [dismissedOffset, setDismissedOffset] = useState(0);
  const [dismissedTotal, setDismissedTotal] = useState(0);
  const [dismissedSpread, setDismissedSpread] = useState(0);

  // Discover tab — random dealt cards.
  const [dealt, setDealt] = useState<DealtNote[]>([]);
  const [dealing, setDealing] = useState(false);

  // View mode.
  const [view, setView] = useState<"read" | "highlight" | "discover">("read");

  // Book view state — spread index for both Read and Highlight books.
  const [readSpread, setReadSpread] = useState(0);
  const [highlightSpread, setHighlightSpread] = useState(0);

  // Overlay state (for Discover cards).
  const [overlayIndex, setOverlayIndex] = useState<number | null>(null);

  const [highlightDate, setHighlightDate] = useState<string | undefined>();
  const gridRef = useRef<HTMLDivElement>(null);

  // Prefetch cache: keyed by offset.
  const browseCache = useRef<Map<number, { notes: any[]; total: number }>>(new Map());

  const BROWSE_PAGE = 20;

  // ---- Load initial data ----
  useEffect(() => {
    // Read-only corpora still need browse data; readOnly only disables
    // mutation actions such as save, dismiss, edit, and AI curation.
    fetch(`${apiBase}/commonplace/browse/index`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d?.notes) setBrowseIndex(d.notes); })
      .catch(() => {});
    if (!readOnly) {
      fetch(`${apiBase}/commonplace/staging`, { headers: authHeaders() })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (d?.notes) setStaged(d.notes); })
        .catch(() => {});
      // Get dismissed count for toggle.
      fetch(`${apiBase}/commonplace/browse/dismissed?offset=0&limit=1`, { headers: authHeaders() })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { if (d) setDismissedTotal(d.total ?? 0); })
        .catch(() => {});
    }
  }, [apiBase, readOnly]);

  // ---- Browse: fetch a page of chronological notes ----
  function prefetchBrowse(offset: number) {
    if (offset < 0 || browseCache.current.has(offset)) return;
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(BROWSE_PAGE),
    });
    fetch(`${apiBase}/commonplace/browse?${params}`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) browseCache.current.set(offset, { notes: d.notes || [], total: d.total ?? 0 });
      })
      .catch(() => {});
  }

  function applyBrowsePage(offset: number, notes: any[], total: number, startSpread: number) {
    setBrowseNotes(notes);
    setBrowseOffset(offset);
    setBrowseTotal(total);
    setReadSpread(startSpread);
    // Prefetch neighbors.
    prefetchBrowse(offset + BROWSE_PAGE);
    if (offset > 0) prefetchBrowse(offset - BROWSE_PAGE);
  }

  function fetchBrowsePage(offset: number, startSpread: number = 0) {
    // Use cache if available.
    const cached = browseCache.current.get(offset);
    if (cached) {
      applyBrowsePage(offset, cached.notes, cached.total, startSpread);
      return;
    }
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(BROWSE_PAGE),
    });
    fetch(`${apiBase}/commonplace/browse?${params}`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          browseCache.current.set(offset, { notes: d.notes || [], total: d.total ?? 0 });
          applyBrowsePage(offset, d.notes || [], d.total ?? 0, startSpread);
        }
      })
      .catch(() => {});
  }

  function fetchDismissedPage(offset: number, startSpread: number = 0) {
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(BROWSE_PAGE),
    });
    fetch(`${apiBase}/commonplace/browse/dismissed?${params}`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          setDismissedNotes(d.notes || []);
          setDismissedOffset(d.offset ?? offset);
          setDismissedTotal(d.total ?? 0);
          setDismissedSpread(startSpread);
        }
      })
      .catch(() => {});
  }

  // Load first page on mount.
  useEffect(() => {
    fetchBrowsePage(0);
  }, [apiBase, readOnly]);

  // ---- Discover: deal random cards ----
  function fetchDeal() {
    setDealing(true);
    fetch(`${apiBase}/commonplace/curate`, {
      method: "POST",
      headers: authHeaders(),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          setDealt(d.notes || []);
          setView("discover");
        }
        setDealing(false);
      })
      .catch(() => setDealing(false));
  }

  function openOverlay(index: number) {
    setOverlayIndex(index);
  }

  function navigateOverlay(delta: -1 | 1) {
    if (overlayIndex === null) return;
    const next = overlayIndex + delta;
    if (next < 0 || next >= dealt.length) return;
    setOverlayIndex(next);
  }

  function closeOverlay() {
    setOverlayIndex(null);
  }

  function stageNote(note: DealtNote, highlight: string) {
    if (readOnly) return;
    const isFullNote = highlight === note.body;
    const params = new URLSearchParams({
      rel: note.rel, date: note.date, era: note.era,
      title: note.title, body: note.body,
      highlight: isFullNote ? "" : highlight,
    });
    fetch(`${apiBase}/commonplace/stage?${params}`, {
      method: "POST",
      headers: authHeaders(),
    })
      .then((r) => {
        if (r.ok) {
          setStaged((prev) => {
            const existing = prev.find((s) => s.rel === note.rel);
            if (existing) {
              if (!isFullNote && highlight && !existing.highlights.includes(highlight)) {
                return prev.map((s) =>
                  s.rel === note.rel
                    ? { ...s, highlights: [...s.highlights, highlight] }
                    : s,
                );
              }
              return prev;
            }
            return [...prev, {
              rel: note.rel, date: note.date, era: note.era,
              title: note.title, body: note.body,
              highlights: isFullNote ? [] : highlight ? [highlight] : [],
            }];
          });
          // Mark as staged in browse view too.
          setBrowseNotes((prev) =>
            prev.map((n) => n.rel === note.rel ? { ...n, staged: true } : n),
          );
          // Remove from dealt cards and advance overlay if open.
          setDealt((prev) => {
            const next = prev.filter((d) => d.rel !== note.rel);
            if (overlayIndex !== null) {
              if (next.length === 0) {
                setOverlayIndex(null);
              } else if (overlayIndex >= next.length) {
                setOverlayIndex(next.length - 1);
              }
            }
            return next;
          });
        }
      })
      .catch(() => {});
  }

  function dismissNote(rel: string) {
    if (readOnly) return;
    fetch(`${apiBase}/commonplace/dismiss?${new URLSearchParams({ rel })}`, {
      method: "POST", headers: authHeaders(),
    }).catch(() => {});
    setBrowseNotes((prev) => prev.filter((n) => n.rel !== rel));
    setDealt((prev) => prev.filter((d) => d.rel !== rel));
    setDismissedTotal((prev) => prev + 1);
  }

  function undismissNote(rel: string) {
    if (readOnly) return;
    fetch(`${apiBase}/commonplace/undismiss?${new URLSearchParams({ rel })}`, {
      method: "POST", headers: authHeaders(),
    }).catch(() => {});
    setDismissedNotes((prev) => prev.filter((n) => n.rel !== rel));
    setDismissedTotal((prev) => Math.max(0, prev - 1));
  }

  function unstageNote(s: StagedNote) {
    if (readOnly) return;
    const params = new URLSearchParams({ rel: s.rel });
    fetch(`${apiBase}/commonplace/stage?${params}`, {
      method: "DELETE",
      headers: authHeaders(),
    })
      .then((r) => {
        if (r.ok) setStaged((prev) => prev.filter((x) => x.rel !== s.rel));
      })
      .catch(() => {});
  }

  function editNote(rel: string, body: string) {
    if (readOnly) return;
    const params = new URLSearchParams({ rel, body });
    fetch(`${apiBase}/commonplace/note?${params}`, {
      method: "PUT", headers: authHeaders(),
    })
      .then((r) => {
        if (r.ok) {
          setStaged((prev) => prev.map((s) => s.rel === rel ? { ...s, body } : s));
          setBrowseNotes((prev) => prev.map((n) => n.rel === rel ? { ...n, body } : n));
          setDealt((prev) => prev.map((d) => d.rel === rel ? { ...d, body } : d));
        }
      })
      .catch(() => {});
  }

  function scrollToCard(date: string) {
    setHighlightDate(date);
    setTimeout(() => setHighlightDate(undefined), 2000);
    if (view === "discover") {
      const el = gridRef.current?.querySelector(
        `[data-date="${date}"]`,
      ) as HTMLElement | null;
      if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (view === "read") {
      // Find the note's position in the full index and load the right page.
      const idx = browseIndex.findIndex((n) => n.date === date);
      if (idx < 0) return;
      const targetOffset = Math.floor(idx / BROWSE_PAGE) * BROWSE_PAGE;
      if (targetOffset !== browseOffset) {
        fetchBrowsePage(targetOffset);
        // Spread will reset to 0 when page loads — good enough.
      } else {
        // Already on the right page — scroll the book.
        const flow = gridRef.current?.querySelector("[data-book-flow]") as HTMLElement | null;
        if (!flow) return;
        const noteEl = flow.querySelector(`[data-note-date="${date}"]`) as HTMLElement | null;
        if (!noteEl) return;
        const spreadIdx = Math.floor(noteEl.offsetLeft / flow.clientWidth);
        setReadSpread(spreadIdx);
      }
    } else if (view === "highlight") {
      const flow = gridRef.current?.querySelector("[data-book-flow]") as HTMLElement | null;
      if (!flow) return;
      const noteEl = flow.querySelector(`[data-note-date="${date}"]`) as HTMLElement | null;
      if (!noteEl) return;
      const spreadIdx = Math.floor(noteEl.offsetLeft / flow.clientWidth);
      setHighlightSpread(spreadIdx);
    }
  }

  // ---- Derived ----
  const sortedStaged = [...staged].sort((a, b) => a.date.localeCompare(b.date));

  // Filter out already-staged notes from browse (they've been saved).
  const stagedRels = new Set(staged.map((s) => s.rel));
  const readNotes = browseNotes.filter((n) => !n.staged && !stagedRels.has(n.rel));

  const timelineNotes: Note[] = (
    view === "read" ? browseIndex
    : view === "highlight" ? sortedStaged
    : dealt
  )
    .map((n, i) => ({
      rel: `${view}/${i}`,
      date: n.date,
      title: isUntitled(n.title) ? "" : n.title,
      label: n.era,
      source: "",
      body: "",
    }))
    .sort((a, b) => a.date.localeCompare(b.date));

  const overlayItem = overlayIndex !== null ? dealt[overlayIndex] : null;

  // ---- Render ----
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      {/* Status bar */}
      {!readOnly && (
        <div className="border-b border-stone-200 bg-white px-6 py-3">
          <div className="flex items-center gap-4">
            {/* View toggle */}
            <div className="flex items-center gap-1 text-[11px] font-sans">
              {(["read", "highlight", "discover"] as const).map((tab) => (
                <button
                  key={tab}
                  onClick={() => {
                    setView(tab);
                    if (tab !== "read") setShowDismissed(false);
                    if (tab === "discover" && staged.length >= 5 && !dealing) fetchDeal();
                  }}
                  className={
                    "px-2 py-0.5 rounded transition-colors " +
                    (view === tab
                      ? "bg-stone-100 text-stone-700"
                      : "text-stone-400 hover:text-stone-600")
                  }
                >
                  {tab === "highlight"
                    ? `highlight${staged.length > 0 ? ` (${staged.length})` : ""}`
                    : tab}
                </button>
              ))}
            </div>

            {/* Reshuffle — opens dismissed notes book */}
            {dismissedTotal > 0 && (
              <button
                onClick={() => {
                  const next = !showDismissed;
                  setShowDismissed(next);
                  if (next) { setView("read"); if (dismissedNotes.length === 0) fetchDismissedPage(0); }
                }}
                className={
                  "ml-auto text-xs font-sans transition-colors shrink-0 whitespace-nowrap " +
                  (showDismissed
                    ? "text-stone-700"
                    : "text-stone-400 hover:text-stone-600")
                }
              >
                review ({dismissedTotal})
              </button>
            )}
          </div>
        </div>
      )}

      {/* Main content */}
      <div className="flex flex-1 min-h-0">
        {timelineNotes.length > 0 && (
          <div className="shrink-0">
            <TimelineSidebar
              notes={timelineNotes}
              loading={false}
              highlightDate={highlightDate}
              onSelect={(n) => scrollToCard(n.date)}
              bg="bg-stone-50"
            />
          </div>
        )}

        <div
          ref={gridRef}
          className="flex-1 min-w-0 overflow-y-auto bg-stone-50"
        >
          {view === "read" ? (
            /* ---- Read: chronological book ---- */
            showDismissed ? (
              /* Dismissed sub-view */
              dismissedNotes.length === 0 ? (
                <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
                  no dismissed notes
                </div>
              ) : (
                <NoteBook
                  notes={dismissedNotes}
                  readOnly={readOnly}
                  index={dismissedSpread}
                  onNavigate={setDismissedSpread}
                  onHighlight={(rel, text) => {
                    const note = dismissedNotes.find((n) => n.rel === rel);
                    if (note) { undismissNote(rel); stageNote(note, text); }
                  }}
                  onRemove={(rel) => undismissNote(rel)}
                  onSave={(rel) => {
                    const note = dismissedNotes.find((n) => n.rel === rel);
                    if (note) { undismissNote(rel); stageNote(note, note.body); }
                  }}
                  onEditSave={(rel, body) => editNote(rel, body)}
                  onLastPage={
                    dismissedOffset + BROWSE_PAGE < dismissedTotal
                      ? () => fetchDismissedPage(dismissedOffset + BROWSE_PAGE)
                      : undefined
                  }
                  onFirstPage={
                    dismissedOffset > 0
                      ? () => fetchDismissedPage(Math.max(0, dismissedOffset - BROWSE_PAGE), Infinity)
                      : undefined
                  }
                />
              )
            ) : readNotes.length === 0 ? (
              <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
                {browseTotal === 0 ? "no notes" : "all notes on this page saved or dismissed"}
              </div>
            ) : (
              <NoteBook
                notes={readNotes}
                readOnly={readOnly}
                index={readSpread}
                onNavigate={setReadSpread}
                onHighlight={(rel, text) => {
                  const note = readNotes.find((n) => n.rel === rel);
                  if (note) stageNote(note, text);
                }}
                onRemove={(rel) => dismissNote(rel)}
                onSave={(rel) => {
                  const note = readNotes.find((n) => n.rel === rel);
                  if (note) stageNote(note, note.body);
                }}
                onEditSave={(rel, body) => editNote(rel, body)}
                onLastPage={
                  browseOffset + BROWSE_PAGE < browseTotal
                    ? () => fetchBrowsePage(browseOffset + BROWSE_PAGE)
                    : undefined
                }
                onFirstPage={
                  browseOffset > 0
                    ? () => fetchBrowsePage(Math.max(0, browseOffset - BROWSE_PAGE), Infinity)
                    : undefined
                }
              />
            )
          ) : view === "highlight" ? (
            /* ---- Highlight: saved notes book ---- */
            staged.length === 0 ? (
              <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
                no saved notes yet
              </div>
            ) : (
              <NoteBook
                notes={sortedStaged}
                readOnly={readOnly}
                index={highlightSpread}
                onNavigate={setHighlightSpread}
                onHighlight={(rel, text) => {
                  const note = sortedStaged.find((s) => s.rel === rel);
                  if (note) stageNote(note as DealtNote, text);
                }}
                onRemove={(rel) => {
                  const note = staged.find((s) => s.rel === rel);
                  if (note) unstageNote(note);
                }}
                onEditSave={(rel, body) => editNote(rel, body)}
              />
            )
          ) : (
            /* ---- Discover: dealt cards ---- */
            dealt.length === 0 ? (
              <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
                {dealing ? "finding notes that match your taste..." : "save 5+ notes in Read to unlock"}
              </div>
            ) : (
              <div className="p-5">
                <div className="grid grid-cols-4 gap-4">
                  {dealt.map((note, i) => (
                    <DealtCard
                      key={`${note.date}-${note.title}-${i}`}
                      note={note}
                      onClick={() => openOverlay(i)}
                      onSave={() => stageNote(dealt[i], dealt[i].body)}
                      onDismiss={() => dismissNote(dealt[i].rel)}
                    />
                  ))}
                </div>
              </div>
            )
          )}
        </div>
      </div>

      {/* Overlay (Discover tab only) */}
      {overlayItem && overlayIndex !== null && (() => {
        const stagedEntry = staged.find((s) => s.rel === overlayItem.rel);
        const highlights = stagedEntry?.highlights ?? [];
        return (
          <NoteOverlay
            note={overlayItem}
            highlights={highlights}
            onClose={closeOverlay}
            onPrev={overlayIndex > 0 ? () => navigateOverlay(-1) : undefined}
            onNext={
              overlayIndex < dealt.length - 1
                ? () => navigateOverlay(1)
                : undefined
            }
            position={`${overlayIndex + 1} / ${dealt.length}`}
            onHighlight={(body) => stageNote(overlayItem, body)}
            onSaveAll={() => {
              stageNote(overlayItem, overlayItem.body);
              closeOverlay();
            }}
            onEditSave={(body) => editNote(overlayItem.rel, body)}
          />
        );
      })()}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  DealtCard — truncated note card for browsing                       */
/* ------------------------------------------------------------------ */

function DealtCard({
  note,
  onClick,
  onSave,
  onDismiss,
}: {
  note: DealtNote;
  onClick: () => void;
  onSave: () => void;
  onDismiss: () => void;
}) {
  const truncated = note.body.length > TRUNCATE
    ? note.body.slice(0, TRUNCATE).replace(/\s+\S*$/, "") + "..."
    : note.body;

  return (
    <div
      data-date={note.date}
      onClick={onClick}
      className={
        "group rounded border border-stone-200 bg-white px-4 py-4 shadow-sm cursor-pointer " +
        "transition-all duration-200 hover:shadow-md hover:border-stone-300 hover:-translate-y-0.5"
      }
    >
      <div className="mb-2 flex items-baseline gap-2 text-xs font-sans text-stone-400">
        <span className="tabular-nums">{note.date}</span>
        {note.era && (
          <>
            <span className="text-stone-300">/</span>
            <span style={{ color: eraColor(note.era) }}>{note.era}</span>
          </>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onSave(); }}
          className="ml-auto opacity-0 group-hover:opacity-100 text-stone-300 hover:text-stone-500 transition-all text-sm leading-none"
          title="Save"
        >
          &#x2713;
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="opacity-0 group-hover:opacity-100 text-stone-300 hover:text-stone-500 transition-all text-sm leading-none"
          title="Dismiss"
        >
          &times;
        </button>
      </div>
      {!isUntitled(note.title) && (
        <h3 className="mb-2 text-xs font-serif font-semibold text-stone-800 leading-snug">
          {note.title}
        </h3>
      )}
      <div className="text-xs font-serif text-stone-600 leading-relaxed whitespace-pre-wrap">
        {truncated}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  NoteBook — CSS-column paginated book for close reading             */
/* ------------------------------------------------------------------ */

function NoteBook({
  notes,
  readOnly = false,
  index,
  onNavigate,
  onHighlight,
  onRemove,
  onEditSave,
  onSave,
  onLastPage,
  onFirstPage,
}: {
  notes: (StagedNote | DealtNote)[];
  readOnly?: boolean;
  index: number;
  onNavigate: (i: number) => void;
  onHighlight: (rel: string, text: string) => void;
  onRemove: (rel: string) => void;
  onEditSave: (rel: string, body: string) => void;
  onSave?: (rel: string) => void;
  onLastPage?: () => void;
  onFirstPage?: () => void;
}) {
  const flowRef = useRef<HTMLDivElement>(null);
  const [totalSpreads, setTotalSpreads] = useState(1);
  const [selection, setSelection] = useState<{ text: string; rel: string; rect: { top: number; left: number } } | null>(null);
  const [editingRel, setEditingRel] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");

  // Current spread (0-indexed).
  const spread = Math.max(0, Math.min(index, totalSpreads - 1));

  // Measure total spreads after render.
  useEffect(() => {
    const el = flowRef.current;
    if (!el) return;
    // Each spread = container clientWidth (2 columns). Total = scrollWidth / clientWidth.
    const measure = () => {
      const w = el.clientWidth;
      if (w > 0) setTotalSpreads(Math.max(1, Math.ceil(el.scrollWidth / w)));
    };
    measure();
    const obs = new ResizeObserver(measure);
    obs.observe(el);
    return () => obs.disconnect();
  }, [notes]);

  // Scroll to current spread.
  useEffect(() => {
    const el = flowRef.current;
    if (!el) return;
    el.scrollLeft = Math.round(spread * el.clientWidth);
  }, [spread, totalSpreads]);

  // Clear selection on spread change.
  useEffect(() => { setSelection(null); window.getSelection()?.removeAllRanges(); }, [spread]);

  // Keyboard nav.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.target as HTMLElement)?.tagName === "TEXTAREA") return;
      if (e.key === "ArrowLeft") {
        if (spread > 0) onNavigate(spread - 1);
        else if (onFirstPage) onFirstPage();
      }
      if (e.key === "ArrowRight") {
        if (spread < totalSpreads - 1) onNavigate(spread + 1);
        else if (onLastPage) onLastPage();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [spread, totalSpreads, onNavigate, onFirstPage, onLastPage]);

  function handleMouseUp() {
    if (readOnly) return;
    const sel = window.getSelection();
    const text = sel?.toString().trim() ?? "";
    const el = flowRef.current;
    if (!text || !el) { setSelection(null); return; }
    // Find which note the selection belongs to.
    let node: Node | null = sel?.anchorNode ?? null;
    let rel = "";
    while (node && node !== el) {
      if (node instanceof HTMLElement && node.dataset.noteRel) {
        rel = node.dataset.noteRel;
        break;
      }
      node = node.parentNode;
    }
    if (!rel) { setSelection(null); return; }
    const range = sel!.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    const parentRect = el.getBoundingClientRect();
    setSelection({
      text,
      rel,
      rect: {
        top: rect.top - parentRect.top + el.scrollTop - 36,
        left: rect.left - parentRect.left + rect.width / 2 + el.scrollLeft,
      },
    });
  }

  function handleHighlight() {
    if (!selection) return;
    onHighlight(selection.rel, selection.text);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
  }

  const spreadsLeft = spread;
  const spreadsRight = totalSpreads - spread - 1;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 flex items-stretch justify-center p-6 gap-0 min-h-0">
        {/* Left stack */}
        <div
          onClick={() => {
            if (spreadsLeft > 0) onNavigate(spread - 1);
            else if (onFirstPage) onFirstPage();
          }}
          className={
            "w-4 shrink-0 flex flex-col justify-center self-stretch rounded-l transition-colors " +
            (spreadsLeft > 0 || onFirstPage
              ? "cursor-pointer bg-stone-200 hover:bg-stone-300"
              : "bg-stone-100")
          }
        />

        {/* Two-column flow */}
        <div className="flex-1 min-w-0 border border-stone-200 shadow-sm bg-white overflow-hidden relative">
          {/* Center divider — fixed over the viewport */}
          <div className="absolute top-0 bottom-0 left-1/2 w-px bg-stone-200 z-0 pointer-events-none" />
          <div
            ref={flowRef}
            data-book-flow
            className="h-full overflow-hidden select-text"
            style={{
              columnCount: 2,
              columnGap: "0px",
              columnFill: "auto",
              paddingTop: "1.5rem",
              paddingBottom: "1.5rem",
            }}
            onMouseUp={handleMouseUp}
          >
            {notes.map((note, i) => (
              <div
                key={note.rel}
                data-note-rel={note.rel}
                data-note-date={note.date}
                className="mb-0"
                style={{ ...(i > 0 ? { breakBefore: "column" } : {}) }}
              >
                {/* Note header — avoid breaking away from first paragraph */}
                <div className="px-8 pb-2" style={{ breakAfter: "avoid" }}>
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-baseline gap-2 text-xs font-sans text-stone-400">
                      <span className="tabular-nums">{note.date}</span>
                      {note.era && (
                        <>
                          <span className="text-stone-300">/</span>
                          <span style={{ color: eraColor(note.era) }}>{note.era}</span>
                        </>
                      )}
                      {(("highlights" in note) && note.highlights.length > 0) && (
                        <span className="text-stone-300">
                          · {note.highlights.length} highlight{note.highlights.length > 1 ? "s" : ""}
                        </span>
                      )}
                    </div>
                    {!readOnly && (
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => {
                            if (editingRel === note.rel) {
                              onEditSave(note.rel, editDraft);
                              setEditingRel(null);
                            } else {
                              setEditingRel(note.rel);
                              setEditDraft(note.body);
                            }
                          }}
                          className={
                            "transition-colors text-sm leading-none " +
                            (editingRel === note.rel
                              ? "text-stone-600"
                              : "text-stone-300 hover:text-stone-500")
                          }
                          title={editingRel === note.rel ? "Save edit" : "Edit"}
                        >
                          &#x270E;
                        </button>
                        {onSave && (
                          <button
                            onClick={() => onSave(note.rel)}
                            className="text-stone-300 hover:text-stone-500 transition-colors text-sm leading-none"
                            title="Save"
                          >
                            &#x2713;
                          </button>
                        )}
                        <button
                          onClick={() => onRemove(note.rel)}
                          className="text-stone-300 hover:text-stone-500 transition-colors text-sm leading-none"
                          title={onSave ? "Dismiss" : "Remove"}
                        >
                          &times;
                        </button>
                      </div>
                    )}
                  </div>
                  {!isUntitled(note.title) && (
                    <h3 className="text-base font-serif font-bold text-stone-700 leading-snug">
                      {note.title}
                    </h3>
                  )}
                </div>
                {/* Note body */}
                <div
                  className={
                    "px-8 pb-8 text-[14px] font-serif text-stone-700 leading-[1.65] whitespace-pre-wrap " +
                    (editingRel === note.rel ? "bg-stone-50 outline-none" : "")
                  }
                  contentEditable={editingRel === note.rel}
                  suppressContentEditableWarning
                  onInput={(e) => setEditDraft((e.target as HTMLElement).innerText)}
                >
                  {("highlights" in note) && note.highlights.length > 0
                    ? highlightedNote(note.body, note.highlights.join("\n· · ·\n"))
                    : note.body}
                </div>
              </div>
            ))}
          </div>

          {/* Highlight button — positioned over the flow */}
          {!readOnly && selection && (
            <button
              onClick={handleHighlight}
              style={{ top: selection.rect.top, left: selection.rect.left }}
              className="absolute -translate-x-1/2 z-10 rounded-full bg-stone-800 text-white text-xs font-sans px-3 py-1.5 shadow-lg hover:bg-stone-700 transition-colors"
            >
              highlight
            </button>
          )}
        </div>

        {/* Right stack */}
        <div
          onClick={() => {
            if (spreadsRight > 0) onNavigate(spread + 1);
            else if (onLastPage) onLastPage();
          }}
          className={
            "w-4 shrink-0 flex flex-col justify-center self-stretch rounded-r transition-colors " +
            (spreadsRight > 0 || onLastPage
              ? "cursor-pointer bg-stone-200 hover:bg-stone-300"
              : "bg-stone-100")
          }
        />
      </div>

    </div>
  );
}


/* ------------------------------------------------------------------ */
/*  NoteOverlay — full note with text selection highlighting           */
/* ------------------------------------------------------------------ */

function NoteOverlay({
  note,
  highlights,
  onClose,
  onPrev,
  onNext,
  position: _position,
  onHighlight,
  onSaveAll,
  onEditSave,
}: {
  note: { date: string; title: string; era?: string; body: string };
  highlights: string[];
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  position: string;
  onHighlight: (body: string) => void;
  onSaveAll: () => void;
  onEditSave: (body: string) => void;
}) {
  const [selection, setSelection] = useState<string>("");
  const [selRect, setSelRect] = useState<{ top: number; left: number } | null>(null);
  const [saved, setSaved] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState(note.body);
  const bodyRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setSelection("");
    setSelRect(null);
    setSaved(false);
    setEditing(false);
    setEditText(note.body);
    window.getSelection()?.removeAllRanges();
  }, [note]);

  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [editing]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (editing) {
        if (e.key === "Escape") { setEditing(false); setEditText(note.body); }
        return;
      }
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && onPrev) onPrev();
      if (e.key === "ArrowRight" && onNext) onNext();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onPrev, onNext, editing, note.body]);

  function handleMouseUp() {
    const sel = window.getSelection();
    const text = sel?.toString().trim() ?? "";
    if (!text || !bodyRef.current) {
      setSelection("");
      setSelRect(null);
      return;
    }
    if (!bodyRef.current.contains(sel?.anchorNode ?? null)) {
      setSelection("");
      setSelRect(null);
      return;
    }
    setSelection(text);
    const range = sel!.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    const parentRect = bodyRef.current.getBoundingClientRect();
    setSelRect({
      top: rect.top - parentRect.top + bodyRef.current.scrollTop - 36,
      left: rect.left - parentRect.left + rect.width / 2,
    });
  }

  function handleAdd() {
    if (!selection) return;
    onHighlight(selection);
    setSelection("");
    setSelRect(null);
    window.getSelection()?.removeAllRanges();
  }

  const hasHighlights = !saved && highlights.length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="relative w-full max-w-3xl max-h-[90vh] mx-4 rounded bg-white shadow-2xl flex flex-col overflow-hidden">
        {/* Nav arrows — fixed in the margins, vertically centered */}
        <button
          onClick={onPrev}
          disabled={!onPrev}
          className={
            "absolute left-2 top-1/2 -translate-y-1/2 z-10 text-2xl leading-none px-1.5 py-4 rounded transition-colors " +
            (onPrev
              ? "text-stone-300 hover:text-stone-500"
              : "text-stone-200/30 cursor-default")
          }
          title="Previous (←)"
        >
          ‹
        </button>
        <button
          onClick={onNext}
          disabled={!onNext}
          className={
            "absolute right-2 top-1/2 -translate-y-1/2 z-10 text-2xl leading-none px-1.5 py-4 rounded transition-colors " +
            (onNext
              ? "text-stone-300 hover:text-stone-500"
              : "text-stone-200/30 cursor-default")
          }
          title="Next (→)"
        >
          ›
        </button>

        {/* Date/era + title + save check, matching card layout */}
        <div className="px-12 pt-8 pb-0">
          <div className="flex items-center justify-between mb-1.5">
            <div className="flex items-baseline gap-2 text-xs font-sans text-stone-400">
              <span className="tabular-nums">{note.date}</span>
              {note.era && (
                <>
                  <span className="text-stone-300">/</span>
                  <span style={{ color: eraColor(note.era) }}>{note.era}</span>
                </>
              )}
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => {
                  if (editing) {
                    onEditSave(editText);
                    setEditing(false);
                  } else {
                    setEditing(true);
                  }
                }}
                className={
                  "text-sm leading-none transition-colors " +
                  (editing
                    ? "text-stone-600"
                    : "text-stone-300 hover:text-stone-500")
                }
                title={editing ? "Save edits" : "Edit note"}
              >
                ✎
              </button>
              <button
                onClick={() => { onSaveAll(); setSaved(true); }}
                className={
                  "text-lg leading-none transition-colors " +
                  (saved
                    ? "text-green-500"
                    : "text-stone-300 hover:text-stone-500")
                }
                title={saved ? "Saved" : "Save full note"}
              >
                ✓
              </button>
            </div>
          </div>
          {!isUntitled(note.title) && (
            <h3 className="text-lg font-serif font-bold text-stone-700 leading-snug">
              {note.title}
            </h3>
          )}
        </div>

        {/* Body */}
        {editing ? (
          <textarea
            ref={textareaRef}
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            className="flex-1 w-full px-12 pt-4 pb-8 text-[15px] font-serif text-stone-700 leading-[1.65] bg-transparent border-none outline-none resize-none"
          />
        ) : (
          <div
            ref={bodyRef}
            className="relative flex-1 overflow-y-auto px-12 pt-4 pb-8"
            onMouseUp={handleMouseUp}
          >
            <div className="text-[15px] font-serif text-stone-700 leading-[1.65] whitespace-pre-wrap select-text">
              {hasHighlights
                ? highlightedNote(
                    note.body,
                    highlights.join("\n· · ·\n"),
                  )
                : note.body}
            </div>

            {selection && selRect && (
              <button
                onClick={handleAdd}
                style={{ top: selRect.top, left: selRect.left }}
                className="absolute -translate-x-1/2 z-10 rounded-full bg-stone-800 text-white text-xs font-sans px-3 py-1.5 shadow-lg hover:bg-stone-700 transition-colors"
              >
                highlight
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
