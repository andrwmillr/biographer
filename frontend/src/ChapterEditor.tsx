import { useEffect, useState } from "react";

export type ChapterDraft = {
  id: string;
  name: string;
  start: string; // YYYY-MM
  note_count: number;
};

type ChapterEditorProps = {
  /** Initial chapters (from proposal or existing eras) */
  initial: ChapterDraft[];
  /** Sorted list of every note's YYYY-MM — used to recompute counts */
  noteMonths: string[];
  /** Called when user confirms */
  onConfirm: (chapters: ChapterDraft[]) => void;
  /** Optional cancel handler */
  onCancel?: () => void;
  /** True while the parent is saving */
  saving?: boolean;
  /** Optional: show a "Re-analyze" button */
  onReanalyze?: () => void;
};

function makeId(): string {
  return crypto.randomUUID();
}

/** Display YYYY-MM as MM-YYYY */
function toDisplayDate(ym: string): string {
  if (!ym || ym === "0000-00") return ym;
  const [y, m] = ym.split("-");
  return `${m}-${y}`;
}

/** Parse MM-YYYY back to YYYY-MM for storage */
function fromDisplayDate(display: string): string {
  if (!display || display === "0000-00") return display;
  const [m, y] = display.split("-");
  return `${y}-${m}`;
}

/** Given sorted chapters and a flat sorted list of note months,
 *  compute how many notes fall in each chapter. */
function recomputeCounts(
  chapters: ChapterDraft[],
  noteMonths: string[],
): ChapterDraft[] {
  const sorted = [...chapters].sort((a, b) => a.start.localeCompare(b.start));
  // If noteMonths is empty, preserve existing counts (initial data from server)
  if (noteMonths.length === 0) return sorted;
  return sorted.map((ch, idx) => {
    const start = ch.start;
    const end = idx + 1 < sorted.length ? sorted[idx + 1].start : "9999-99";
    const count = noteMonths.filter((m) => m >= start && m < end).length;
    return { ...ch, note_count: count };
  });
}

export function ChapterEditor({
  initial,
  noteMonths,
  onConfirm,
  onCancel,
  saving,
  onReanalyze,
}: ChapterEditorProps) {
  const [chapters, setChapters] = useState<ChapterDraft[]>(() =>
    recomputeCounts(initial, noteMonths),
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingField, setEditingField] = useState<"name" | "start" | null>(
    null,
  );
  const [editValue, setEditValue] = useState("");
  const [validationError, setValidationError] = useState("");

  // Re-sort and recompute counts whenever chapters or noteMonths change.
  useEffect(() => {
    setChapters((prev) => recomputeCounts(prev, noteMonths));
  }, [noteMonths]);

  function startEdit(ch: ChapterDraft, field: "name" | "start") {
    setEditingId(ch.id);
    setEditingField(field);
    setEditValue(field === "name" ? ch.name : toDisplayDate(ch.start));
  }

  function commitEdit() {
    if (!editingId || !editingField) return;
    const value = editValue.trim();
    if (!value) {
      cancelEdit();
      return;
    }
    if (editingField === "start" && value !== "0000-00" && !/^\d{2}-\d{4}$/.test(value)) {
      setValidationError("Start must be MM-YYYY format");
      return;
    }
    const storeValue = editingField === "start" ? fromDisplayDate(value) : value;
    setChapters((prev) => {
      const updated = prev.map((ch) =>
        ch.id === editingId ? { ...ch, [editingField!]: storeValue } : ch,
      );
      return recomputeCounts(updated, noteMonths);
    });
    setValidationError("");
    cancelEdit();
  }

  function cancelEdit() {
    setEditingId(null);
    setEditingField(null);
    setEditValue("");
    setValidationError("");
  }

  function addChapter() {
    const sorted = [...chapters].sort((a, b) =>
      a.start.localeCompare(b.start),
    );
    // Default to one month after the last chapter's start
    let newStart = "0000-00";
    if (sorted.length > 0) {
      const last = sorted[sorted.length - 1].start;
      if (last && last !== "0000-00") {
        const [y, m] = last.split("-").map(Number);
        const nm = m + 3 > 12 ? 1 : m + 3;
        const ny = m + 3 > 12 ? y + 1 : y;
        newStart = `${ny}-${String(nm).padStart(2, "0")}`;
      }
    }
    const newCh: ChapterDraft = {
      id: makeId(),
      name: "",
      start: newStart,
      note_count: 0,
    };
    setChapters((prev) => recomputeCounts([...prev, newCh], noteMonths));
    // Auto-focus the new chapter's name
    setTimeout(() => {
      setEditingId(newCh.id);
      setEditingField("name");
      setEditValue("");
    }, 0);
  }

  function removeChapter(id: string) {
    setChapters((prev) =>
      recomputeCounts(
        prev.filter((ch) => ch.id !== id),
        noteMonths,
      ),
    );
  }

  function handleConfirm() {
    // Validate
    const names = new Set<string>();
    for (const ch of chapters) {
      if (!ch.name.trim()) {
        setValidationError("All chapters need a name");
        return;
      }
      if (names.has(ch.name)) {
        setValidationError(`Duplicate name: ${ch.name}`);
        return;
      }
      names.add(ch.name);
    }
    if (chapters.length === 0) {
      setValidationError("At least one chapter is required");
      return;
    }
    setValidationError("");
    onConfirm(chapters);
  }

  const sorted = [...chapters].sort((a, b) =>
    a.start.localeCompare(b.start),
  );

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-serif text-lg text-stone-900">
          Chapters ({sorted.length})
        </h2>
        <div className="flex items-center gap-2">
          {onReanalyze && (
            <button
              onClick={onReanalyze}
              disabled={saving}
              className="text-xs text-stone-500 hover:text-stone-700 underline disabled:opacity-50"
            >
              Re-analyze
            </button>
          )}
          <button
            onClick={addChapter}
            disabled={saving}
            className="text-xs text-stone-600 hover:text-stone-900 border border-stone-300 rounded px-2 py-1 hover:bg-stone-50 disabled:opacity-50"
          >
            + Add
          </button>
        </div>
      </div>

      <p className="text-xs text-stone-500 mb-3">
        Click a name or date to edit. Chapters partition your notes into
        time periods for drafting.
      </p>

      <ul className="space-y-1">
        {sorted.map((ch) => {
          const isEditing = editingId === ch.id;
          return (
            <li
              key={ch.id}
              className="group flex items-center gap-3 rounded border border-stone-200 bg-white px-3 py-2 hover:bg-stone-50 transition-colors"
            >
              <div className="flex-1 min-w-0">
                {isEditing && editingField === "name" ? (
                  <input
                    autoFocus
                    type="text"
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    onBlur={commitEdit}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitEdit();
                      if (e.key === "Escape") cancelEdit();
                    }}
                    className="w-full text-sm text-stone-900 border-b border-stone-400 bg-transparent outline-none px-0 py-0"
                    placeholder="Chapter name"
                  />
                ) : (
                  <button
                    onClick={() => startEdit(ch, "name")}
                    className="text-sm text-stone-900 hover:text-stone-600 text-left truncate block w-full"
                    title="Click to edit name"
                  >
                    {ch.name || (
                      <span className="italic text-stone-400">
                        untitled
                      </span>
                    )}
                  </button>
                )}
                <div className="flex items-center gap-2 mt-0.5">
                  {isEditing && editingField === "start" ? (
                    <input
                      autoFocus
                      type="text"
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      onBlur={commitEdit}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitEdit();
                        if (e.key === "Escape") cancelEdit();
                      }}
                      className="font-mono text-[11px] text-stone-500 border-b border-stone-400 bg-transparent outline-none px-0 py-0 w-20"
                      placeholder="MM-YYYY"
                    />
                  ) : (
                    <button
                      onClick={() => startEdit(ch, "start")}
                      className="font-mono text-[11px] text-stone-400 hover:text-stone-600"
                      title="Click to edit start date"
                    >
                      from {toDisplayDate(ch.start)}
                    </button>
                  )}
                  <span className="text-[11px] text-stone-400">
                    · {ch.note_count} note{ch.note_count !== 1 ? "s" : ""}
                  </span>
                </div>
              </div>
              <button
                onClick={() => removeChapter(ch.id)}
                disabled={saving || sorted.length <= 1}
                className="text-stone-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-0 text-sm px-1"
                title="Remove chapter"
              >
                ×
              </button>
            </li>
          );
        })}
      </ul>

      {validationError && (
        <p className="mt-2 text-xs text-red-600">{validationError}</p>
      )}

      <div className="mt-4 flex items-center gap-3">
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={saving}
            className="text-xs text-stone-500 hover:text-stone-700 underline disabled:opacity-50"
          >
            Cancel
          </button>
        )}
        <button
          onClick={handleConfirm}
          disabled={saving || sorted.length === 0}
          className="ml-auto text-sm font-medium text-white bg-stone-900 hover:bg-stone-700 rounded px-4 py-1.5 disabled:opacity-50"
        >
          {saving ? "Saving..." : "Confirm"}
        </button>
      </div>
    </div>
  );
}
