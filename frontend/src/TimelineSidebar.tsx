import { useEffect, useRef, useState } from "react";
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

export type TimelineSidebarProps = {
  notes: Note[];
  loading: boolean;
  emptyHint?: string;
  /** Called when the user clicks a note in the timeline. */
  onSelect?: (note: Note) => void;
  /** The currently selected note (by rel), for dot highlighting. */
  selectedRel?: string;
  /** When set, scrolls to the matching note (by date). */
  highlightDate?: string;
  /** Whether the hide/show toggle is shown. Defaults to true. */
  collapsible?: boolean;
  /** Background class for sticky header / month markers. Defaults to "bg-white". */
  bg?: string;
};

const GAP_THRESHOLD_DAYS = 365;
const GAP_HEIGHT = 28;

type Item =
  | { kind: "marker"; monthKey: string; y: number }
  | { kind: "gap"; y: number; months: number }
  | {
      kind: "group";
      group: { dateKey: string; notes: Note[] };
      y: number;
      height: number;
    };

/**
 * Pure timeline sidebar — the dot-and-line column with month markers
 * and gap breaks. No reading pane; the parent decides what to do with
 * the selected note.
 */
export function TimelineSidebar({
  notes,
  loading,
  emptyHint,
  onSelect,
  selectedRel,
  highlightDate,
  collapsible = true,
  bg = "bg-white",
}: TimelineSidebarProps) {
  const [expanded, setExpanded] = useState(true);
  const [scrollTop, setScrollTop] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Scroll to highlighted date when it changes.
  useEffect(() => {
    if (!highlightDate) return;
    requestAnimationFrame(() => {
      const container = scrollRef.current;
      if (!container) return;
      const target = container.querySelector(
        `[data-date="${highlightDate}"]`,
      ) as HTMLElement | null;
      if (!target) return;
      const containerRect = container.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const offset = targetRect.top - containerRect.top + container.scrollTop;
      container.scrollTo({
        top: offset - container.clientHeight / 2,
        behavior: "smooth",
      });
    });
  }, [highlightDate, notes]);

  if (loading) {
    return (
      <div className="font-sans text-xs text-stone-400 px-3 py-6">
        loading notes...
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
    let collapsedMs = 0;
    let prevDayMs = bounds.start;
    for (const g of dateGroups) {
      const m = g.dateKey.slice(0, 7);
      const dayMs = new Date(g.dateKey).getTime();
      const gapDays = (dayMs - prevDayMs) / 86400000;
      if (items.length > 0 && gapDays > GAP_THRESHOLD_DAYS) {
        const gapMonths = Math.round(gapDays / 30);
        prevBottom += interGap;
        items.push({ kind: "gap", y: prevBottom, months: gapMonths });
        prevBottom += GAP_HEIGHT;
        collapsedMs += (dayMs - prevDayMs) - GAP_THRESHOLD_DAYS * 86400000;
      }
      const proportionalY =
        padTop + ((dayMs - bounds.start - collapsedMs) / 86400000) * pxPerDay;
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
      prevDayMs = dayMs;
    }
    const totalHeight = prevBottom + padBottom;
    const lineSegments: { top: number; bottom: number }[] = [];
    let segStart = padTop;
    for (const item of items) {
      if (item.kind === "gap") {
        if (segStart < item.y) lineSegments.push({ top: segStart, bottom: item.y });
        segStart = item.y + GAP_HEIGHT;
      }
    }
    lineSegments.push({ top: segStart, bottom: totalHeight });
    return { items, totalHeight, padTop, lineSegments };
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

  if (collapsible && !expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="shrink-0 border-r border-stone-200 px-2 pt-3 pb-1 font-sans text-[10px] uppercase tracking-wider text-stone-400 hover:text-stone-700 [writing-mode:vertical-lr] self-start h-full"
        style={{ textAlign: "start" }}
      >
        timeline
      </button>
    );
  }

  return (
    <div className="relative flex flex-col h-full w-[182px] border-r border-stone-200">
      {collapsible && (
        <div className="flex items-center justify-end border-b border-stone-100 px-2 py-1">
          <button
            onClick={() => setExpanded(false)}
            className="font-sans text-[10px] uppercase tracking-wider px-2 py-0.5 text-stone-400 hover:text-stone-700"
          >
            hide timeline
          </button>
        </div>
      )}
      <div
        ref={scrollRef}
        className="flex-1 overflow-auto relative"
        onScroll={(e) =>
          setScrollTop((e.currentTarget as HTMLDivElement).scrollTop)
        }
      >
        <div className={`sticky top-0 z-20 ${bg}`}>
          {layout && visibleMonth && (
            <div className="px-2 py-1.5 pl-5 font-sans text-[11px] uppercase tracking-wider text-stone-600 pointer-events-none">
              {visibleMonth}
            </div>
          )}
          {!visibleMonth && <div className="h-7" />}
        </div>
        {layout && (
          <div
            className="relative"
            style={{ minHeight: `max(100%, ${layout.totalHeight}px)` }}
          >
            {layout.lineSegments.map((seg, i) => (
              <div
                key={`line-${i}`}
                className="absolute left-[28px] w-px bg-stone-200"
                style={i === layout.lineSegments.length - 1
                  ? { top: seg.top, bottom: 0 }
                  : { top: seg.top, height: seg.bottom - seg.top }}
              />
            ))}
            {layout.items.map((item, idx) => {
              if (item.kind === "gap") {
                const label =
                  item.months >= 18
                    ? `${Math.round(item.months / 12)} yr`
                    : `${item.months} mo`;
                return (
                  <div
                    key={`gap-${idx}`}
                    className="absolute left-0 right-0 flex items-center"
                    style={{ top: item.y, height: GAP_HEIGHT }}
                  >
                    <span className="absolute left-[25px] top-1/2 -translate-y-1/2 font-sans text-[10px] text-stone-300 leading-none tracking-widest">
                      {"⋮"}
                    </span>
                    <span className="ml-[42px] font-sans text-[10px] text-stone-300 italic">
                      {label}
                    </span>
                  </div>
                );
              }
              if (item.kind === "marker") {
                const text = formatMonth(item.monthKey);
                if (text === visibleMonth) return null;
                return (
                  <div
                    key={`m-${item.monthKey}`}
                    className={`absolute left-3 -translate-y-full px-2 py-0.5 ${bg} rounded font-sans text-[11px] uppercase tracking-wider text-stone-600 shadow-sm pointer-events-none`}
                    style={{ top: item.y - 4 }}
                  >
                    {text}
                  </div>
                );
              }
              const { group, y, height } = item;
              const groupSelected = group.notes.some(
                (n) => selectedRel === n.rel,
              );
              return (
                <div
                  key={group.dateKey}
                  data-date={group.dateKey}
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
                        const isSelected = selectedRel === n.rel;
                        const excerpt = n.title || bodyExcerpt(n.body);
                        return (
                          <button
                            key={n.rel}
                            onClick={() => onSelect?.(n)}
                            className={
                              "text-[12px] leading-[18px] text-left max-w-full truncate hover:text-stone-900 " +
                              (isSelected
                                ? "text-stone-900 font-medium"
                                : n.sampled === false
                                  ? "text-stone-400"
                                  : n.highlighted
                                    ? "text-stone-900 font-semibold"
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
  );
}
