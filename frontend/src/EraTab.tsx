import { useEffect, useState } from "react";
import { ChatWorkspace } from "./ChatWorkspace";
import { authHeaders } from "./auth";
import type { Era } from "./types";

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function formatEraStart(start: string): string {
  const m = /^(\d{4})-(\d{1,2})/.exec(start);
  if (!m) return start;
  const month = parseInt(m[2], 10);
  if (month < 1 || month > 12) return start;
  return `${MONTH_NAMES[month - 1]} ${m[1]}`;
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
      })
      .catch((e) => setError(`failed to load eras: ${(e as Error).message}`))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadEras();
    // intentionally only depends on apiBase
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase]);

  if (selectedEra) {
    const era = eras.find((e) => e.name === selectedEra);
    return (
      <ChatWorkspace
        key={selectedEra}
        apiBase={apiBase}
        wsBase={wsBase}
        scope={{ kind: "era", era: selectedEra, future: false }}
        title={era ? `${era.name} · ${era.note_count} notes` : selectedEra}
        onBack={() => setSelectedEra(null)}
        onFinalized={() => loadEras()}
      />
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <h2 className="font-serif text-2xl mb-1">Eras</h2>
      <p className="text-sm text-stone-500 mb-6">
        Pick an era to start drafting its chapter.
      </p>

      {error && (
        <div className="mb-4 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {loading && (
        <div className="text-sm text-stone-400">loading eras…</div>
      )}

      {!loading && eras.length === 0 && !error && (
        <div className="text-sm text-stone-400">no eras defined</div>
      )}

      {!loading && eras.length > 0 && (
        <ul className="grid gap-3 sm:grid-cols-2">
          {eras.map((era) => {
            const empty = era.note_count === 0;
            return (
              <li key={era.name}>
                <button
                  onClick={() => !empty && setSelectedEra(era.name)}
                  disabled={empty}
                  className={
                    "w-full text-left rounded border bg-white p-4 transition-colors " +
                    (empty
                      ? "border-stone-200 opacity-50 cursor-not-allowed"
                      : "border-stone-200 hover:border-stone-400")
                  }
                  title={empty ? "no notes in this era" : "open workspace"}
                >
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="font-serif text-base text-stone-900">
                      {era.name}
                    </span>
                    {era.has_chapter && (
                      <span
                        className="text-emerald-700 text-sm shrink-0"
                        title="chapter on disk"
                      >
                        ✓
                      </span>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-stone-500">
                    from {formatEraStart(era.start)} · {era.note_count} notes
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
