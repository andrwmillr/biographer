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
  model: string;
  onChapterFinalized: () => void;
};

export function EraTab({
  apiBase,
  wsBase,
  eras,
  selectedEra,
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
          This chapter has no notes — pick another from the Chapters dropdown.
        </div>
      </div>
    );
  }

  // Era label rendered as a sticky banner inside the Draft pane.
  // Picker itself lives in the Chapters dropdown in the global header.
  const range = formatEraRange(era.start, era.end);
  const eraLabel = (
    <>
      {era.name}
      {range ? ` (${range})` : ""}
    </>
  );

  return (
    <ChatWorkspace
      key={era.name}
      apiBase={apiBase}
      wsBase={wsBase}
      scope={{ kind: "era", era: era.name }}
      model={model}
      onFinalized={onChapterFinalized}
      draftHeaderSlot={eraLabel}
    />
  );
}
