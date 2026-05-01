import { ChatWorkspace } from "./ChatWorkspace";
import type { Era } from "./types";

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
          This chapter has no notes — pick another from the selector above.
        </div>
      </div>
    );
  }

  return (
    <ChatWorkspace
      key={era.name}
      apiBase={apiBase}
      wsBase={wsBase}
      scope={{ kind: "era", era: era.name }}
      model={model}
      onFinalized={onChapterFinalized}
    />
  );
}
