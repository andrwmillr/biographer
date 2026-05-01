import { ChatWorkspace } from "./ChatWorkspace";
import type { Era } from "./types";

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatYearMonth(ym: string | null | undefined): string {
  if (!ym || ym === "9999-99") return "";
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m || m < 1 || m > 12) return ym;
  return `${MONTHS_SHORT[m - 1]} ${y}`;
}

function formatEraRange(start: string | null, end: string | null): string {
  const s = formatYearMonth(start);
  const e = formatYearMonth(end) || "present";
  if (!s) return "";
  return `${s} – ${e}`;
}

type EraTabProps = {
  apiBase: string;
  wsBase: string;
  eras: Era[];
  selectedEra: string | null;
  setSelectedEra: (name: string) => void;
  model: string;
  onChapterFinalized: () => void;
};

export function EraTab({
  apiBase,
  wsBase,
  eras,
  selectedEra,
  setSelectedEra,
  model,
  onChapterFinalized,
}: EraTabProps) {
  if (!eras.length) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-8 text-sm text-stone-400">
        no chapters defined
      </div>
    );
  }

  const era = eras.find((e) => e.name === selectedEra) ?? eras[0];

  if (era.note_count === 0) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-8">
        <div className="rounded border border-stone-200 bg-stone-50 px-4 py-6 text-sm text-stone-500">
          This chapter has no notes — pick another from the selector above.
        </div>
      </div>
    );
  }

  // Chapter picker rendered into the Notes pane header (passed through
  // ChatWorkspace as a slot). Lives here rather than in App.tsx so its
  // styling can match the surrounding pane chrome.
  const chapterPicker = (
    <select
      name="era"
      className="appearance-none bg-transparent border-0 border-b border-dotted border-stone-300 hover:border-stone-500 focus:border-stone-700 focus:outline-none px-1 pr-5 py-0.5 font-serif italic text-sm text-stone-700 cursor-pointer"
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='none' stroke='%2378716c' stroke-width='1.2' d='M1 1l4 4 4-4'/%3E%3C/svg%3E\")",
        backgroundRepeat: "no-repeat",
        backgroundPosition: "right 2px center",
      }}
      value={era.name}
      onChange={(e) => setSelectedEra(e.target.value)}
      title="Select chapter"
    >
      {eras.map((e) => {
        const range = formatEraRange(e.start, e.end);
        return (
          <option key={e.name} value={e.name} disabled={e.note_count === 0}>
            {e.name}
            {range ? ` (${range})` : ""}
            {e.note_count === 0 ? " (empty)" : ""}
          </option>
        );
      })}
    </select>
  );

  return (
    <ChatWorkspace
      key={era.name}
      apiBase={apiBase}
      wsBase={wsBase}
      scope={{ kind: "era", era: era.name }}
      model={model}
      onFinalized={onChapterFinalized}
      notesHeaderSlot={chapterPicker}
    />
  );
}
