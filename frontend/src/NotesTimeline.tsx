import { useEffect, useState } from "react";
import { Markdown } from "./markdown";
import { TimelineSidebar } from "./TimelineSidebar";
import type { Note } from "./types";

type NotesTimelineProps = {
  notes: Note[];
  loading: boolean;
  emptyHint?: string;
  /** When set, selects the matching note (by date) and brings it into view. */
  highlightDate?: string;
  /** Surrounding text from the citation click — used to disambiguate when
   *  multiple notes share the same date. */
  highlightContext?: string;
};

/** Pick the best note for a date citation. When multiple notes share the
 *  same date, use surrounding text (from the clicked paragraph) to find
 *  the one whose title appears in context. Falls back to first match. */
function resolveNote(
  notes: Note[],
  dateKey: string,
  context?: string,
): Note | undefined {
  const matches = notes.filter((n) => n.date.slice(0, 10) === dateKey);
  if (matches.length <= 1) return matches[0];
  if (context) {
    const ctx = context.toLowerCase();
    const byTitle = matches.find(
      (n) => n.title && ctx.includes(n.title.toLowerCase()),
    );
    if (byTitle) return byTitle;
  }
  return matches[0];
}

export function NotesTimeline({
  notes,
  loading,
  emptyHint,
  highlightDate,
  highlightContext,
}: NotesTimelineProps) {
  const [selected, setSelected] = useState<Note | null>(null);

  // Reset selection when the notes set changes (era/topN change).
  useEffect(() => {
    setSelected(null);
  }, [notes]);

  // Honor citation clicks from chat / draft markdown.
  useEffect(() => {
    if (!highlightDate) return;
    const match = resolveNote(notes, highlightDate, highlightContext);
    if (match) setSelected(match);
  }, [highlightDate, highlightContext, notes]);

  return (
    <div className="flex h-full min-h-0">
      <div className="shrink-0 h-full">
        <TimelineSidebar
          notes={notes}
          loading={loading}
          emptyHint={emptyHint}
          selectedRel={selected?.rel}
          highlightDate={highlightDate}
          onSelect={setSelected}
        />
      </div>
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
                  {"⚠"} Editor note: {selected.editor_note}
                </div>
              )}
            </header>
            <div className="font-serif text-[14px] leading-[1.6] text-stone-900">
              {selected.body ? (
                <Markdown
                  content={selected.body
                    .replace(/^\t+/gm, "")
                    .replace(/^([-=]{3,})\s*$/gm, "​$1")
                    .replace(/(\S)\n(?=\S)/g, "$1  \n")}
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
            Click a title in the timeline to read.
          </div>
        )}
      </div>
    </div>
  );
}
