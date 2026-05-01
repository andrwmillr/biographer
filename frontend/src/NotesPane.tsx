import { useEffect, useRef } from "react";
import { Markdown } from "./markdown";
import type { Note } from "./types";

type NoteCardProps = {
  note: Note;
  selected?: boolean;
  onClick?: () => void;
};

// A single note rendered as a self-contained card. Styling mirrors the note
// article in the previous ErasView (small date/label header, serif title,
// serif body) so cards compose cleanly into a grid OR a column.
export function NoteCard({ note, selected, onClick }: NoteCardProps) {
  const interactive = !!onClick;
  const Comp = interactive ? "button" : "div";
  return (
    <Comp
      onClick={onClick}
      className={
        "block w-full text-left rounded border bg-white p-5 transition-colors " +
        (selected
          ? "border-stone-400 shadow-sm"
          : "border-stone-200 hover:border-stone-300")
      }
    >
      <header className="mb-3 pb-2 border-b border-stone-100 font-sans">
        <div className="text-xs text-stone-500 tabular-nums">
          {note.date.slice(0, 10)}
        </div>
        <div className="text-xs text-stone-400 mt-0.5">
          {note.label}
          {note.source && (
            <span className="text-stone-300"> · {note.source}</span>
          )}
        </div>
        <h3 className="mt-2 text-base font-serif text-stone-900">
          {note.title || (
            <span className="text-stone-400 italic">(untitled)</span>
          )}
        </h3>
        {note.editor_note && (
          <div className="mt-1.5 text-xs text-amber-700">
            ⚠ Editor note: {note.editor_note}
          </div>
        )}
      </header>
      <div className="font-serif text-[14px] leading-[1.55] text-stone-900 max-h-[16rem] overflow-hidden relative">
        {note.body ? (
          <Markdown
            content={note.body.replace(/(\S)\n(?=\S)/g, "$1  \n")}
            variant="chapter"
          />
        ) : (
          <span className="font-sans text-sm text-stone-400">(empty)</span>
        )}
      </div>
    </Comp>
  );
}

type NotesPaneProps = {
  notes: Note[];
  loading: boolean;
  layout: "grid" | "column";
  highlightDate?: string;
  emptyHint?: string;
};

// Pre-gen / generating: layout="grid"  → multi-column responsive cards.
// Iterating / finalized: layout="column" → single-column cards in a side pane.
// In column layout the matching card scrolls into view when highlightDate
// changes (used when the user clicks a citation in the chat or draft).
export function NotesPane({
  notes,
  loading,
  layout,
  highlightDate,
  emptyHint,
}: NotesPaneProps) {
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  useEffect(() => {
    if (!highlightDate) return;
    const match = notes.find((n) => n.date.slice(0, 10) === highlightDate);
    if (!match) return;
    const el = cardRefs.current.get(match.rel);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [highlightDate, notes]);

  if (loading) {
    return (
      <div className="font-sans text-xs text-stone-400 px-2 py-6">
        loading notes…
      </div>
    );
  }
  if (notes.length === 0) {
    return (
      <div className="font-sans text-xs text-stone-400 px-2 py-6">
        {emptyHint ?? "no notes"}
      </div>
    );
  }

  const containerClass =
    layout === "grid"
      ? "grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
      : "flex flex-col gap-3";

  return (
    <div className={containerClass}>
      {notes.map((n) => {
        const dateKey = n.date.slice(0, 10);
        const isHighlighted = highlightDate === dateKey;
        return (
          <div
            key={n.rel}
            ref={(el) => {
              if (el) cardRefs.current.set(n.rel, el);
              else cardRefs.current.delete(n.rel);
            }}
          >
            <NoteCard note={n} selected={isHighlighted} />
          </div>
        );
      })}
    </div>
  );
}
