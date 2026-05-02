import { useEffect, useState } from "react";
import { Markdown } from "./markdown";
import type { Note } from "./types";

type Bounds = { start: number; end: number };

function eraBounds(notes: Note[]): Bounds | null {
  if (!notes.length) return null;
  const start = new Date(notes[0].date).getTime();
  const end =
    notes.length > 1
      ? new Date(notes[notes.length - 1].date).getTime()
      : start + 86400000;
  return start < end ? { start, end } : null;
}

function formatMonth(monthKey: string): string {
  const [yy, mm] = monthKey.split("-").map(Number);
  return new Date(yy, mm - 1, 1).toLocaleDateString("en-US", {
    month: "short",
    year: "numeric",
  });
}

// Frontmatter-less corpora (e.g. Thoreau, Alcott) have empty titles.
// Show a flattened opening of the body instead of "(untitled)" so the
// timeline reads as distinct entries.
function bodyExcerpt(body: string, maxLen = 80): string {
  if (!body) return "";
  const flat = body.replace(/\s+/g, " ").trim();
  if (flat.length <= maxLen) return flat;
  const cut = flat.slice(0, maxLen);
  const lastSpace = cut.lastIndexOf(" ");
  return (lastSpace > 40 ? cut.slice(0, lastSpace) : cut) + "…";
}

type NotesTimelineProps = {
  notes: Note[];
  loading: boolean;
  emptyHint?: string;
  /** When set, selects the matching note (by date) and brings it into view. */
  highlightDate?: string;
};

// Two-column notes view: left timeline of dot+title rows positioned
// proportionally by date, right pane shows the selected note's full body.
// Adopted from the pre-unification ErasView.
export function NotesTimeline({
  notes,
  loading,
  emptyHint,
  highlightDate,
}: NotesTimelineProps) {
  const [selected, setSelected] = useState<Note | null>(null);
  const [scrollTop, setScrollTop] = useState<number>(0);
  const [showTimeline, setShowTimeline] = useState<boolean>(true);

  // Reset selection when the notes set changes (era/topN change).
  useEffect(() => {
    setSelected(null);
  }, [notes]);

  // Honor citation clicks from chat / draft markdown.
  useEffect(() => {
    if (!highlightDate) return;
    const match = notes.find((n) => n.date.slice(0, 10) === highlightDate);
    if (match) setSelected(match);
  }, [highlightDate, notes]);

  if (loading) {
    return (
      <div className="font-sans text-xs text-stone-400 px-3 py-6">
        loading notes…
      </div>
    );
  }
  if (notes.length === 0) {
    return (
      <div className="font-sans text-xs text-stone-400 px-3 py-6">
        {emptyHint ?? "no notes"}
      </div>
    );
  }

  const bounds = eraBounds(notes);
  const dateGroups: { dateKey: string; notes: Note[] }[] = [];
  for (const n of notes) {
    const k = n.date.slice(0, 10);
    const last = dateGroups[dateGroups.length - 1];
    if (last && last.dateKey === k) last.notes.push(n);
    else dateGroups.push({ dateKey: k, notes: [n] });
  }
  type Item =
    | { kind: "marker"; monthKey: string; y: number }
    | {
        kind: "group";
        group: { dateKey: string; notes: Note[] };
        y: number;
        height: number;
      };
  const layout = (() => {
    if (!bounds || !dateGroups.length) return null;
    const padTop = 4;
    const padBottom = 16;
    const pxPerDay = 4;
    const intraTitle = 18;
    const interGap = 4;
    const markerOverhead = 32;
    const items: Item[] = [];
    let prevBottom = padTop;
    let prevMonth = "";
    for (const g of dateGroups) {
      const m = g.dateKey.slice(0, 7);
      const dayMs = new Date(g.dateKey).getTime();
      const proportionalY =
        padTop + ((dayMs - bounds.start) / 86400000) * pxPerDay;
      const isMonthChange = m !== prevMonth;
      const isFirstItem = items.length === 0;
      const minGap =
        isMonthChange && !isFirstItem ? markerOverhead : interGap;
      const y = Math.max(proportionalY, prevBottom + minGap);
      if (isMonthChange) {
        items.push({ kind: "marker", monthKey: m, y });
        prevMonth = m;
      }
      const height = g.notes.length * intraTitle;
      items.push({ kind: "group", group: g, y, height });
      prevBottom = y + height;
    }
    const totalHeight = prevBottom + padBottom;
    return { items, totalHeight, padTop };
  })();

  let visibleMonth = "";
  if (layout && layout.items.length) {
    for (const item of layout.items) {
      if (item.kind !== "marker") continue;
      if (item.y > scrollTop) break;
      visibleMonth = formatMonth(item.monthKey);
    }
    if (!visibleMonth) {
      const firstMarker = layout.items.find((i) => i.kind === "marker");
      if (firstMarker && firstMarker.kind === "marker") {
        visibleMonth = formatMonth(firstMarker.monthKey);
      }
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-end gap-1 border-b border-stone-100 px-2 py-1">
        <button
          onClick={() => setShowTimeline((v) => !v)}
          className="font-sans text-[10px] uppercase tracking-wider px-2 py-0.5 text-stone-400 hover:text-stone-700"
          title={showTimeline ? "hide timeline column" : "show timeline column"}
        >
          {showTimeline ? "hide timeline" : "show timeline"}
        </button>
      </div>
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {showTimeline && (
          <div className="w-[182px] shrink-0 border-r border-stone-200 relative flex flex-col">
            {layout && (
              <div className="absolute top-1 left-3 z-30 px-2 py-0.5 bg-white rounded font-sans text-[11px] uppercase tracking-wider text-stone-600 shadow-sm pointer-events-none">
                {visibleMonth}
              </div>
            )}
            <div
              className="flex-1 overflow-auto"
              onScroll={(e) =>
                setScrollTop(
                  (e.currentTarget as HTMLDivElement).scrollTop,
                )
              }
            >
              <div className="sticky top-0 h-7 bg-white z-20" />
              {layout && (
                <div
                  className="relative"
                  style={{ height: `${layout.totalHeight}px` }}
                >
                  <div className="absolute left-[28px] top-0 bottom-0 w-px bg-stone-200" />
                  {layout.items.map((item) => {
                    if (item.kind === "marker") {
                      const text = formatMonth(item.monthKey);
                      if (text === visibleMonth) return null;
                      return (
                        <div
                          key={`m-${item.monthKey}`}
                          className="absolute left-3 -translate-y-full px-2 py-0.5 bg-white rounded font-sans text-[11px] uppercase tracking-wider text-stone-600 shadow-sm pointer-events-none"
                          style={{ top: item.y - 4 }}
                        >
                          {text}
                        </div>
                      );
                    }
                    const { group, y, height } = item;
                    const groupSelected = group.notes.some(
                      (n) => selected?.rel === n.rel,
                    );
                    return (
                      <div
                        key={group.dateKey}
                        className="absolute"
                        style={{ top: y, left: 0, right: 0, height }}
                      >
                        <span
                          className={
                            "absolute rounded-full transition-all -translate-x-1/2 -translate-y-1/2 " +
                            (groupSelected
                              ? "w-3 h-3 bg-stone-900 ring-2 ring-stone-200"
                              : "w-1.5 h-1.5 bg-stone-400")
                          }
                          style={{ top: 9, left: 28 }}
                        />
                        <div className="pl-12 pr-3 flex gap-2 items-baseline">
                          <span className="text-[11px] tabular-nums text-stone-400 shrink-0 leading-[18px] w-5">
                            {group.dateKey.slice(8)}
                          </span>
                          <div className="min-w-0 flex-1 flex flex-col items-start">
                            {group.notes.map((n) => {
                              const isSelected = selected?.rel === n.rel;
                              const excerpt = n.title || bodyExcerpt(n.body);
                              return (
                                <button
                                  key={n.rel}
                                  onClick={() => setSelected(n)}
                                  className={
                                    "text-[12px] leading-[18px] text-left max-w-full truncate hover:text-stone-900 " +
                                    (isSelected
                                      ? "text-stone-900 font-medium"
                                      : n.sampled === false
                                        ? "text-stone-400"
                                        : "text-stone-700")
                                  }
                                  title={`${n.date.slice(0, 10)} · ${n.label}${
                                    n.source ? ` · ${n.source}` : ""
                                  } · ${excerpt || "(untitled)"}`}
                                >
                                  {excerpt ? (
                                    <span
                                      className={
                                        n.title ? "" : "text-stone-500"
                                      }
                                    >
                                      {excerpt}
                                    </span>
                                  ) : (
                                    <span className="italic text-stone-400">
                                      (untitled)
                                    </span>
                                  )}
                                </button>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
        <div className="flex-1 min-w-0 overflow-auto">
          {selected ? (
            <article className="p-5">
              <header className="mb-4 pb-3 border-b border-stone-100 font-sans">
                <div className="text-xs text-stone-500 tabular-nums">
                  {selected.date.slice(0, 10)}
                </div>
                <div className="text-xs text-stone-400 mt-0.5">
                  {selected.label}
                  {selected.source && (
                    <span className="text-stone-300">
                      {" "}
                      · {selected.source}
                    </span>
                  )}
                </div>
                {selected.title && (
                  <h2 className="mt-2 text-base font-serif text-stone-900">
                    {selected.title}
                  </h2>
                )}
                {selected.editor_note && (
                  <div className="mt-2 text-xs text-amber-700">
                    ⚠ Editor note: {selected.editor_note}
                  </div>
                )}
              </header>
              <div className="font-serif text-[14px] leading-[1.6] text-stone-900">
                {selected.body ? (
                  <Markdown
                    content={selected.body.replace(/(\S)\n(?=\S)/g, "$1  \n")}
                    variant="chapter"
                  />
                ) : (
                  <span className="font-sans text-sm text-stone-400">
                    (empty)
                  </span>
                )}
              </div>
            </article>
          ) : (
            <div className="p-5 font-sans text-xs text-stone-400">
              {showTimeline
                ? "Click a title in the timeline to read."
                : "(timeline hidden)"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
