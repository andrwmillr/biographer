import { useEffect, useState } from "react";
import { ChatWorkspace } from "./ChatWorkspace";
import { authHeaders } from "./auth";
import type { Era } from "./types";

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatYearMonth(ym: string | null | undefined): string {
  if (!ym || ym === "9999-99") return "";
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m || m < 1 || m > 12) return ym;
  return `${MONTHS[m - 1]} ${y}`;
}

function formatRange(start: string | null, end: string | null): string {
  const s = formatYearMonth(start);
  const e = formatYearMonth(end) || "present";
  if (!s) return "";
  return `${s} – ${e}`;
}

type EraTabProps = {
  apiBase: string;
  wsBase: string;
};

export function EraTab({ apiBase, wsBase }: EraTabProps) {
  const [eras, setEras] = useState<Era[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string>("");
  const [selectedEra, setSelectedEra] = useState<string | null>(null);

  function loadEras() {
    setLoading(true);
    fetch(`${apiBase}/eras`, { headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        return r.json() as Promise<Era[]>;
      })
      .then((data) => {
        setEras(data);
        setError("");
        // Default-select the first era with notes if nothing's picked yet.
        setSelectedEra((cur) => {
          if (cur && data.some((e) => e.name === cur)) return cur;
          const firstWithNotes = data.find((e) => e.note_count > 0);
          return firstWithNotes?.name ?? data[0]?.name ?? null;
        });
      })
      .catch((e) => setError(`failed to load eras: ${(e as Error).message}`))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadEras();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase]);

  if (loading && !eras.length) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-8 text-sm text-stone-400">
        loading eras…
      </div>
    );
  }
  if (error) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-8">
        <div className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      </div>
    );
  }
  if (!eras.length) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-8 text-sm text-stone-400">
        no eras defined
      </div>
    );
  }

  const era = eras.find((e) => e.name === selectedEra) ?? eras[0];
  const empty = era.note_count === 0;

  const titleNode = (
    <div className="flex items-baseline gap-2">
      <select
        name="era"
        className="font-serif text-lg text-stone-900 bg-transparent border-0 border-b border-stone-200 hover:border-stone-400 focus:border-stone-600 focus:outline-none px-1 py-0.5"
        value={era.name}
        onChange={(e) => setSelectedEra(e.target.value)}
        title="Select era"
      >
        {eras.map((e) => {
          const range = formatRange(e.start, e.end);
          return (
            <option key={e.name} value={e.name} disabled={e.note_count === 0}>
              {e.name}
              {range ? ` (${range})` : ""}
              {e.note_count === 0 ? " (empty)" : ""}
            </option>
          );
        })}
      </select>
    </div>
  );

  if (empty) {
    return (
      <div className="mx-auto max-w-[120rem] px-6 py-4">
        <div className="mb-4">{titleNode}</div>
        <div className="rounded border border-stone-200 bg-stone-50 px-4 py-6 text-sm text-stone-500">
          This era has no notes — pick another from the selector above.
        </div>
      </div>
    );
  }

  return (
    <ChatWorkspace
      key={era.name}
      apiBase={apiBase}
      wsBase={wsBase}
      scope={{ kind: "era", era: era.name, future: false }}
      titleNode={titleNode}
      onFinalized={() => loadEras()}
    />
  );
}
