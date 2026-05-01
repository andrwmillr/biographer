import { useState } from "react";
import { authHeaders, setSession } from "./auth";

export type CorpusInfo = {
  slug: string;
  title: string | null;
  is_sample: boolean;
  note_count: number;
  has_eras: boolean;
  eras: Array<{ name: string; start: string; end?: string }>;
};

export type Sample = {
  slug: string;
  title: string;
  description: string;
  source: string;
  note_count: number;
  era_count: number;
};

type ImportFlowProps = {
  apiBase: string;
  /** Bootstrap state. null → start at "notes" step. Non-null with has_eras=false
   *  → start at "eras" step (resume mid-import). */
  initialInfo: CorpusInfo | null;
  /** Called when both notes and eras are in place. Parent flips to ready mode. */
  onComplete: (info: CorpusInfo) => void;
  /** Called when the user cancels mid-flow. Parent should POST /corpus/wipe,
   *  clear localStorage, and re-render this component (use a `key` prop tied
   *  to the current corpus to force a fresh mount). */
  onWipe: () => Promise<void>;
};

export function ImportFlow({
  apiBase,
  initialInfo,
  onComplete,
  onWipe,
}: ImportFlowProps) {
  const [step, setStep] = useState<"notes" | "eras">(
    initialInfo && !initialInfo.has_eras ? "eras" : "notes",
  );
  const [info, setInfo] = useState<CorpusInfo | null>(initialInfo);
  const [error, setError] = useState<string>("");

  async function handleNotesUpload(file: File) {
    setError("");
    const formData = new FormData();
    formData.append("file", file);
    try {
      const resp = await fetch(`${apiBase}/import/notes`, {
        method: "POST",
        headers: authHeaders(),
        body: formData,
      });
      if (!resp.ok)
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const data = await resp.json();
      setSession(data.slug);
      // Re-fetch state — a dedup hit might land on an existing corpus that
      // already has eras, in which case skip the eras step entirely.
      const c = await fetch(`${apiBase}/corpus`, { headers: authHeaders() });
      const fresh = (await c.json()) as CorpusInfo;
      setInfo(fresh);
      if (fresh.has_eras) {
        if (data.duplicate) {
          setError("Welcome back — found your existing corpus with this content.");
        }
        onComplete(fresh);
        return;
      }
      if (data.duplicate) {
        setError("Welcome back — found your existing corpus. Continue with eras.");
      }
      setStep("eras");
    } catch (err) {
      setError(`notes upload failed: ${(err as Error).message}`);
    }
  }

  async function handleErasUpload(file: File) {
    setError("");
    const formData = new FormData();
    formData.append("file", file);
    try {
      const resp = await fetch(`${apiBase}/import/eras`, {
        method: "POST",
        headers: authHeaders(),
        body: formData,
      });
      if (!resp.ok)
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const c = await fetch(`${apiBase}/corpus`, { headers: authHeaders() });
      const fresh = (await c.json()) as CorpusInfo;
      onComplete(fresh);
    } catch (err) {
      setError(`eras upload failed: ${(err as Error).message}`);
    }
  }

  if (step === "notes") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-stone-50">
        <div className="max-w-md w-full p-8">
          <h1 className="font-serif text-2xl mb-2">Biographer</h1>
          <p className="text-stone-600 mb-6 text-sm leading-relaxed">
            Upload a zip of your notes (e.g.{" "}
            <code className="bg-stone-100 px-1 rounded text-xs">.md</code> or{" "}
            <code className="bg-stone-100 px-1 rounded text-xs">.txt</code>{" "}
            files) to import a corpus. Each browser sees only the corpus it
            imports. Files live on the host's machine.
          </p>
          <label className="block">
            <span className="text-sm font-medium text-stone-700">
              Notes (.zip)
            </span>
            <input
              type="file"
              name="notes-zip"
              accept=".zip,application/zip"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleNotesUpload(file);
              }}
              className="mt-2 block w-full text-sm text-stone-700"
            />
          </label>
          {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
        </div>
      </div>
    );
  }

  // step === "eras"
  return (
    <div className="min-h-screen flex items-center justify-center bg-stone-50">
      <div className="max-w-md w-full p-8">
        <h1 className="font-serif text-2xl mb-2">One more step</h1>
        <p className="text-stone-600 mb-6 text-sm leading-relaxed">
          {info?.note_count ?? 0} notes uploaded. Now provide an{" "}
          <code className="bg-stone-100 px-1 rounded text-xs">eras.yaml</code>{" "}
          defining the era boundaries for this corpus.
        </p>
        <label className="block">
          <span className="text-sm font-medium text-stone-700">eras.yaml</span>
          <input
            type="file"
            name="eras-yaml"
            accept=".yaml,.yml,application/x-yaml,text/yaml,text/plain"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleErasUpload(file);
            }}
            className="mt-2 block w-full text-sm text-stone-700"
          />
        </label>
        {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
        <button
          onClick={() => {
            if (window.confirm("Discard the uploaded notes and start over?")) {
              onWipe();
            }
          }}
          className="mt-6 text-xs text-stone-500 hover:text-stone-700 underline"
        >
          Cancel and discard
        </button>
      </div>
    </div>
  );
}
