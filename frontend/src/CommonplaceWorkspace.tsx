import { useCallback, useEffect, useRef, useState } from "react";
import { TimelineSidebar } from "./TimelineSidebar";
import { authHeaders, getAuthToken, getSession } from "./auth";
import type { Note } from "./types";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type Passage = {
  date: string;
  era: string; // resolved from eras data, not LLM output
  title: string; // cleaned — empty string if untitled
  body: string;
};

type FetchedNote = { body: string; title: string; date: string };

type Props = { apiBase: string; wsBase: string; model: string; readOnly?: boolean };
type Progress = { seen: number; total: number; complete: boolean };
type Era = { name: string; start: string | null; end: string | null };

const PING_MS = 10_000;
const PONG_DEADLINE_MS = 25_000;

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function eraForDate(date: string, eras: Era[]): string {
  const ym = date.slice(0, 7);
  for (const e of eras) {
    const lo = e.start ?? "";
    const hi = e.end ?? "9999";
    if (ym >= lo && ym <= hi) return e.name;
  }
  return "";
}

/** Parse commonplace markdown into passages, resolving eras from date. */
function parsePassages(md: string, eras: Era[]): Passage[] {
  return md
    .split(/^### /m)
    .filter(Boolean)
    .map((block) => {
      const lines = block.split("\n");
      const parts = (lines[0]?.trim() ?? "").split(" · ");
      const date = (parts[0] ?? "").replace(/^\[|\]$/g, "");
      const llmEra = parts[1] ?? "";
      const title = (parts[2] ?? "").trim();
      const body = lines.slice(1).join("\n").trim()
        .replace(/\n*DONE:.*$/s, "").trim();
      return { date, era: eraForDate(date, eras) || llmEra, title, body };
    })
    .filter((p) => p.body);
}

function isUntitled(title: string): boolean {
  return !title || /^\(?untitled\)?$/i.test(title.trim());
}

/** Normalize smart quotes/dashes so LLM-extracted text matches originals. */
function normQuotes(s: string): string {
  return s
    .replace(/[‘’′]/g, "'")
    .replace(/[“”″]/g, '"')
    .replace(/[–—]/g, "-");
}

/**
 * Find passage text within full note body and return React nodes with
 * the matched ranges highlighted. Matches line by line so a single
 * changed word doesn't kill the whole highlight.
 */
function highlightedNote(
  noteBody: string,
  passageBody: string,
): React.ReactNode[] {
  // Normalize smart quotes for matching (LLM straightens them).
  const normBody = normQuotes(noteBody);

  // Split passage into lines, try to find each in the note body.
  const lines = passageBody
    .split(/\n· · ·\n/)
    .flatMap((chunk) => chunk.split("\n"))
    .map((l) => l.trim())
    .filter(Boolean);

  // Try to find a string in normBody; returns [start, end] or null.
  function findInBody(needle: string): [number, number] | null {
    let idx = normBody.indexOf(needle);
    if (idx >= 0) return [idx, idx + needle.length];
    // Whitespace-normalized fallback.
    const wsBody = normBody.replace(/\s+/g, " ");
    const wsNeedle = needle.replace(/\s+/g, " ");
    const ni = wsBody.indexOf(wsNeedle);
    if (ni < 0) return null;
    // Map normalized index back to original positions.
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
    // Try exact, then whitespace-normalized.
    let match = findInBody(normLine);
    // If no match, try stripping quotes the LLM may have added.
    if (!match) {
      const stripped = normLine.replace(/^["']|["']$/g, "").trim();
      if (stripped !== normLine) match = findInBody(stripped);
    }
    if (match) { ranges.push(match); continue; }
  }

  if (!ranges.length) return [<span key="all">{noteBody}</span>];

  // Sort and merge overlapping/adjacent ranges.
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [ranges[0]];
  for (let i = 1; i < ranges.length; i++) {
    const last = merged[merged.length - 1];
    // Merge if overlapping or separated by only whitespace.
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
    if (cursor < start) {
      nodes.push(<span key={`c-${start}`}>{noteBody.slice(cursor, start)}</span>);
    }
    nodes.push(
      <mark key={`h-${start}`} className="bg-amber-100 rounded-sm px-0.5 -mx-0.5">
        {noteBody.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  if (cursor < noteBody.length) {
    nodes.push(<span key="tail">{noteBody.slice(cursor)}</span>);
  }
  return nodes;
}

/* ------------------------------------------------------------------ */
/*  Main component                                                     */
/* ------------------------------------------------------------------ */

export function CommonplaceWorkspace({ apiBase, wsBase, model, readOnly }: Props) {
  const [passages, setPassages] = useState<Passage[]>([]);
  const [status, setStatus] = useState<
    "idle" | "connecting" | "running" | "done" | "error"
  >("idle");
  const [statusText, setStatusText] = useState("");
  const [progress, setProgress] = useState<Progress | null>(null);
  const [eras, setEras] = useState<Era[]>([]);
  const [highlightDate, setHighlightDate] = useState<string | undefined>();
  // Overlay: the passage to show + the fetched full note (null while loading).
  const [overlayPassage, setOverlayPassage] = useState<Passage | null>(null);
  const [overlayNote, setOverlayNote] = useState<FetchedNote | null>(null);
  const [overlayLoading, setOverlayLoading] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const runIdRef = useRef<string | null>(null);
  const lastPongRef = useRef<number>(0);
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef(status);
  statusRef.current = status;

  // Keep a ref to eras so the WS message handler (closed over at connect
  // time) can always see the latest value.
  const erasRef = useRef(eras);
  erasRef.current = eras;

  // ---- Load initial data on mount ----
  useEffect(() => {
    // Fetch eras first since parsePassages needs them.
    const erasP = fetch(`${apiBase}/eras`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => {
        const list: Era[] = Array.isArray(d) ? d : [];
        setEras(list);
        return list;
      })
      .catch(() => [] as Era[]);

    if (readOnly) {
      // Read-only mode: load every note as a passage (no extraction).
      erasP.then((eraList) => {
        fetch(`${apiBase}/notes/all?top_n=0`, { headers: authHeaders() })
          .then((r) => (r.ok ? r.json() : []))
          .then((notes: any[]) => {
            const ps: Passage[] = notes.map((n) => ({
              date: (n.date || "").slice(0, 10),
              era: eraForDate((n.date || "").slice(0, 10), eraList),
              title: n.title || "",
              body: n.body || "",
            }));
            setPassages(ps);
            setStatus("done");
          })
          .catch(() => {});
      });
      return;
    }

    erasP.then((eraList) => {
      fetch(`${apiBase}/commonplace/latest`, { headers: authHeaders() })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (d?.content) setPassages(parsePassages(d.content, eraList));
        })
        .catch(() => {});
    });

    fetch(`${apiBase}/commonplace/progress`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) setProgress(d);
      })
      .catch(() => {});
  }, [apiBase, readOnly]);

  // Clean up ping timer on unmount.
  useEffect(() => {
    return () => {
      if (pingTimerRef.current) clearInterval(pingTimerRef.current);
    };
  }, []);

  // ---- WebSocket ----
  const connectWs = useCallback(
    (resumeRunId?: string) => {
      if (wsRef.current && wsRef.current.readyState <= WebSocket.OPEN) return;
      setStatus("connecting");
      if (!resumeRunId) setStatusText("starting...");

      const ws = new WebSocket(`${wsBase}/commonplace-session`);
      wsRef.current = ws;

      let runPassages: Passage[] = [];
      let basePassages: Passage[] | null = null;

      ws.onopen = () => {
        const session = getSession() || "";
        const token = getAuthToken() || "";
        const msg: Record<string, unknown> = {
          type: "start",
          session,
          token,
          model,
        };
        if (resumeRunId) {
          msg.resume = true;
          msg.run_id = resumeRunId;
        }
        ws.send(JSON.stringify(msg));

        lastPongRef.current = Date.now();
        if (pingTimerRef.current) clearInterval(pingTimerRef.current);
        pingTimerRef.current = setInterval(() => {
          if (ws.readyState !== WebSocket.OPEN) return;
          if (Date.now() - lastPongRef.current > PONG_DEADLINE_MS) {
            try {
              ws.close();
            } catch {}
            return;
          }
          ws.send(JSON.stringify({ type: "ping" }));
        }, PING_MS);
      };

      ws.onmessage = (ev) => {
        let payload: any;
        try {
          payload = JSON.parse(ev.data);
        } catch {
          return;
        }
        const t = payload.type;

        if (t === "pong") {
          lastPongRef.current = Date.now();
          return;
        }

        if (t === "spawned") {
          setStatus("running");
          if (payload.run_dir) runIdRef.current = payload.run_dir;
          const sampled = payload.sampled_count ?? 0;
          const total = payload.total_eligible ?? 0;
          setStatusText(
            `reading ${sampled} notes (${payload.seen_before ?? 0} of ${total} previously seen)`,
          );
          setPassages((prev) => {
            basePassages = prev;
            return prev;
          });
        } else if (t === "draft_update") {
          runPassages = parsePassages(payload.content ?? "", erasRef.current);
          setPassages([
            ...(basePassages ?? []),
            ...runPassages,
          ]);
        } else if (t === "finalized") {
          setStatus("done");
          setPassages([
            ...(basePassages ?? []),
            ...parsePassages(payload.content ?? "", erasRef.current),
          ]);
          setStatusText("done");
          fetch(`${apiBase}/commonplace/progress`, { headers: authHeaders() })
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => {
              if (d) setProgress(d);
            })
            .catch(() => {});
        } else if (t === "error") {
          setStatus("error");
          setStatusText(payload.message ?? "unknown error");
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (pingTimerRef.current) {
          clearInterval(pingTimerRef.current);
          pingTimerRef.current = null;
        }
        if (
          statusRef.current === "running" ||
          statusRef.current === "connecting"
        ) {
          setStatus("idle");
          setStatusText("disconnected — will reconnect when tab is active");
        }
      };
      ws.onerror = () => {};
    },
    [apiBase, wsBase, model],
  );

  // ---- Reconnect on tab focus ----
  const checkAndResume = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState <= WebSocket.OPEN) return;
    fetch(`${apiBase}/session/active?kind=commonplace`, {
      headers: authHeaders(),
    })
      .then((r) => (r.ok ? r.json() : { active: false }))
      .then((data) => {
        if (data?.active && data.run_id) {
          runIdRef.current = data.run_id;
          connectWs(data.run_id);
        }
      })
      .catch(() => {});
  }, [apiBase, connectWs]);

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== "visible") return;
      checkAndResume();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [checkAndResume]);

  useEffect(() => {
    checkAndResume();
  }, []); // eslint-disable-line

  // ---- Actions ----
  function startRun() {
    if (wsRef.current) return;
    runIdRef.current = null;
    connectWs();
  }

  function scrollToCard(date: string) {
    setHighlightDate(date);
    // Clear highlight after animation completes.
    setTimeout(() => setHighlightDate(undefined), 2000);
    const el = gridRef.current?.querySelector(
      `[data-date="${date}"]`,
    ) as HTMLElement | null;
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function openOverlay(p: Passage) {
    setOverlayPassage(p);
    setOverlayNote(null);
    setOverlayLoading(true);
    const params = new URLSearchParams({ date: p.date, title: p.title });
    fetch(`${apiBase}/commonplace/note?${params}`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) setOverlayNote({ body: d.body, title: d.title, date: d.date });
        setOverlayLoading(false);
      })
      .catch(() => setOverlayLoading(false));
  }

  function rejectPassage(p: Passage) {
    const params = new URLSearchParams({
      date: p.date, title: p.title, body: p.body,
    });
    fetch(`${apiBase}/commonplace/passage?${params}`, {
      method: "DELETE",
      headers: authHeaders(),
    })
      .then((r) => {
        if (r.ok) setPassages((prev) => prev.filter((x) => x !== p));
      })
      .catch(() => {});
  }

  function addPassage(date: string, era: string, title: string, body: string) {
    const params = new URLSearchParams({ date, era, title, body });
    fetch(`${apiBase}/commonplace/passage?${params}`, {
      method: "POST",
      headers: authHeaders(),
    })
      .then((r) => {
        if (r.ok) {
          const p: Passage = { date, era, title, body };
          setPassages((prev) => [p, ...prev]);
        }
      })
      .catch(() => {});
  }

  function closeOverlay() {
    setOverlayPassage(null);
    setOverlayNote(null);
  }

  // ---- Derived ----
  const canRun = status === "idle" || status === "done" || status === "error";
  const progressPct = progress
    ? Math.round((progress.seen / Math.max(progress.total, 1)) * 100)
    : 0;

  const timelineNotes: Note[] = passages
    .map((p, i) => ({
      rel: `commonplace/${i}`,
      date: p.date,
      title: isUntitled(p.title) ? "" : p.title,
      label: p.era,
      source: "",
      body: p.body,
    }))
    .sort((a, b) => a.date.localeCompare(b.date));

  // ---- Render ----
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      {/* Status bar — hidden in readOnly mode */}
      {!readOnly && (
        <div className="border-b border-stone-200 bg-white px-6 py-3">
          <div className="flex items-center gap-4">
            <button
              disabled={!canRun || progress?.complete}
              onClick={startRun}
              className={
                "shrink-0 rounded px-4 py-1.5 text-sm font-sans transition-colors " +
                (canRun && !progress?.complete
                  ? "bg-stone-800 text-white hover:bg-stone-700"
                  : "bg-stone-200 text-stone-400 cursor-not-allowed")
              }
            >
              {progress?.complete
                ? "all notes read"
                : status === "running"
                  ? "extracting..."
                  : passages.length
                    ? "run again"
                    : "discover highlights"}
            </button>
            <div className="flex-1 min-w-0">
              {statusText && (
                <p className="text-xs font-sans text-stone-500 truncate">
                  {statusText}
                </p>
              )}
              {progress && (
                <div className="flex items-center gap-2 mt-1">
                  <div className="flex-1 h-1.5 rounded-full bg-stone-100 overflow-hidden">
                    <div
                      className="h-full rounded-full bg-stone-400 transition-all duration-500"
                      style={{ width: `${progressPct}%` }}
                    />
                  </div>
                  <span className="text-[10px] font-sans text-stone-400 tabular-nums shrink-0">
                    {progress.seen}/{progress.total} notes
                  </span>
                </div>
              )}
            </div>
            <span className="text-xs font-sans text-stone-400 shrink-0 tabular-nums">
              {passages.length} passage{passages.length !== 1 ? "s" : ""}
            </span>
          </div>
        </div>
      )}

      {/* Main: timeline + card grid */}
      <div className="flex flex-1 min-h-0">
        {passages.length > 0 && (
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
          {passages.length === 0 && status === "idle" ? (
            <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
              press &ldquo;discover highlights&rdquo; to start
            </div>
          ) : passages.length === 0 && status === "running" ? (
            <div className="flex items-center justify-center h-full text-sm text-stone-400 font-sans">
              reading notes...
            </div>
          ) : (
            <div className="p-6">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                {passages.map((p, i) => (
                  <PassageCard
                    key={`${p.date}-${p.title}-${i}`}
                    passage={p}
                    isNew={status === "running" && i < 3}
                    highlighted={highlightDate === p.date}
                    onClickDate={() => scrollToCard(p.date)}
                    onClick={() => openOverlay(p)}
                    onReject={() => rejectPassage(p)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Note overlay */}
      {overlayPassage && (
        <NoteOverlay
          passage={overlayPassage}
          allPassages={passages.filter(
            (p) => p.date === overlayPassage.date && p.title === overlayPassage.title,
          )}
          note={overlayNote}
          loading={overlayLoading}
          onClose={closeOverlay}
          onAddHighlight={(body) =>
            addPassage(
              overlayPassage.date,
              overlayPassage.era,
              overlayPassage.title,
              body,
            )
          }
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  PassageCard                                                        */
/* ------------------------------------------------------------------ */

function PassageCard({
  passage,
  isNew,
  highlighted,
  onClickDate,
  onClick,
  onReject,
}: {
  passage: Passage;
  isNew: boolean;
  highlighted: boolean;
  onClickDate: () => void;
  onClick: () => void;
  onReject: () => void;
}) {
  const { date, era, title, body } = passage;
  const parts = body.split(/\n· · ·\n/);
  return (
    <div
      data-date={date}
      onClick={onClick}
      className={
        "group break-inside-avoid rounded border bg-white px-5 py-5 shadow-sm cursor-pointer " +
        "transition-all duration-300 hover:shadow-md hover:border-stone-300 hover:-translate-y-0.5 " +
        (highlighted
          ? "border-stone-400 shadow-md ring-2 ring-stone-300/50"
          : isNew
            ? "border-stone-300 shadow-md"
            : "border-stone-200")
      }
    >
      <div className="mb-2 flex items-baseline gap-2 text-[11px] font-sans text-stone-400">
        <button
          onClick={(e) => {
            e.stopPropagation();
            onClickDate();
          }}
          className="tabular-nums hover:text-stone-700 transition-colors"
          title="Show on timeline"
        >
          {date}
        </button>
        {era && (
          <>
            <span className="text-stone-300">/</span>
            <span>{era}</span>
          </>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onReject();
          }}
          className="ml-auto opacity-0 group-hover:opacity-100 text-stone-300 hover:text-stone-500 transition-all text-sm leading-none"
          title="Remove passage"
        >
          &times;
        </button>
      </div>
      {!isUntitled(title) && (
        <h3 className="mb-2 text-sm font-serif font-semibold text-stone-800 leading-snug">
          {title}
        </h3>
      )}
      {parts.map((part, i) => (
        <div key={i}>
          {i > 0 && (
            <div className="my-3 text-center text-stone-300 text-xs">
              · · ·
            </div>
          )}
          <div className="text-sm font-serif text-stone-700 leading-relaxed whitespace-pre-wrap">
            {part.trim()}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  NoteOverlay                                                        */
/* ------------------------------------------------------------------ */

function NoteOverlay({
  passage,
  allPassages,
  note,
  loading,
  onClose,
  onAddHighlight,
}: {
  passage: Passage;
  allPassages: Passage[];
  note: FetchedNote | null;
  loading: boolean;
  onClose: () => void;
  onAddHighlight: (body: string) => void;
}) {
  const [selection, setSelection] = useState<string>("");
  const [selRect, setSelRect] = useState<{ top: number; left: number } | null>(
    null,
  );
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function handleMouseUp() {
    const sel = window.getSelection();
    const text = sel?.toString().trim() ?? "";
    if (!text || !bodyRef.current) {
      setSelection("");
      setSelRect(null);
      return;
    }
    // Only accept selections within the note body.
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
    onAddHighlight(selection);
    setSelection("");
    setSelRect(null);
    window.getSelection()?.removeAllRanges();
  }

  const displayTitle = note?.title || passage.title;
  const displayDate = note?.date || passage.date;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative w-full max-w-2xl max-h-[80vh] mx-4 rounded-lg border border-stone-200 bg-white shadow-xl flex flex-col">
        {/* Header */}
        <div className="flex items-baseline gap-3 border-b border-stone-100 px-5 py-3">
          <span className="text-xs font-sans text-stone-400 tabular-nums">
            {displayDate}
          </span>
          {passage.era && (
            <>
              <span className="text-stone-300 text-xs">/</span>
              <span className="text-xs font-sans text-stone-400">
                {passage.era}
              </span>
            </>
          )}
          {!isUntitled(displayTitle) && (
            <h3 className="text-sm font-serif font-semibold text-stone-800 leading-snug">
              {displayTitle}
            </h3>
          )}
          <button
            onClick={onClose}
            className="ml-auto text-stone-400 hover:text-stone-600 transition-colors text-lg leading-none"
            title="Close"
          >
            &times;
          </button>
        </div>

        {/* Body */}
        <div
          ref={bodyRef}
          className="relative flex-1 overflow-y-auto px-5 py-4"
          onMouseUp={handleMouseUp}
        >
          {loading ? (
            <div className="flex items-center justify-center py-12 text-sm text-stone-400 font-sans">
              loading note...
            </div>
          ) : note ? (
            <div className="text-sm font-serif text-stone-700 leading-relaxed whitespace-pre-wrap select-text">
              {highlightedNote(
                note.body,
                allPassages.map((p) => p.body).join("\n· · ·\n"),
              )}
            </div>
          ) : (
            <div className="text-sm font-serif text-stone-400 leading-relaxed whitespace-pre-wrap italic">
              could not load the original note
            </div>
          )}

          {/* Floating "highlight" button near selection */}
          {selection && selRect && (
            <button
              onClick={handleAdd}
              style={{ top: selRect.top, left: selRect.left }}
              className="absolute -translate-x-1/2 z-10 rounded bg-stone-800 text-white text-xs font-sans px-2.5 py-1 shadow-lg hover:bg-stone-700 transition-colors"
            >
              highlight
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
