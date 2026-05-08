import { useEffect, useState } from "react";
import { ChapterEditor, type ChapterDraft } from "./ChapterEditor";
import { authHeaders, setSession } from "./auth";

export type CorpusInfo = {
  slug: string;
  title: string | null;
  is_sample: boolean;
  access: {
    mode: string;
    can_read: boolean;
    can_write: boolean;
    can_compute: boolean;
    can_promote: boolean;
    can_delete: boolean;
    can_rename: boolean;
  };
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
   *  → start at "analyzing" step (resume mid-import). */
  initialInfo: CorpusInfo | null;
  /** Called when both notes and chapters are in place. Parent flips to ready mode. */
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
  const [step, setStep] = useState<"notes" | "analyzing" | "editor">(
    initialInfo && !initialInfo.has_eras ? "analyzing" : "notes",
  );
  const [info, setInfo] = useState<CorpusInfo | null>(initialInfo);
  const [error, setError] = useState<string>("");
  const [analyzeStatus, setAnalyzeStatus] = useState<string>("");
  const [proposedChapters, setProposedChapters] = useState<ChapterDraft[]>([]);
  const [noteMonths, setNoteMonths] = useState<string[]>([]);
  const [corpusTitle, setCorpusTitle] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (step === "analyzing" && initialInfo && !initialInfo.has_eras) {
      runPropose();
    }
  }, []);

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
      // already has eras, in which case skip the chapter step entirely.
      const c = await fetch(`${apiBase}/corpus`, { headers: authHeaders() });
      const fresh = (await c.json()) as CorpusInfo;
      setInfo(fresh);
      if (fresh.has_eras) {
        if (data.duplicate) {
          setError(
            "Welcome back — found your existing corpus with this content.",
          );
        }
        onComplete(fresh);
        return;
      }
      if (data.duplicate) {
        setError("Welcome back — found your existing corpus.");
      }
      // Kick off chapter discovery
      setStep("analyzing");
      runPropose();
    } catch (err) {
      setError(`notes upload failed: ${(err as Error).message}`);
    }
  }

  async function runPropose() {
    setError("");
    setAnalyzeStatus("loading notes...");
    setStep("analyzing");
    try {
      const resp = await fetch(`${apiBase}/chapters/propose`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      }
      if (!resp.body) {
        throw new Error("no response body");
      }
      // Parse SSE stream
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE events
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const lines = part.split("\n");
          let eventType = "";
          let eventData = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) eventType = line.slice(7);
            if (line.startsWith("data: ")) eventData = line.slice(6);
          }
          if (!eventType || !eventData) continue;
          const payload = JSON.parse(eventData);

          if (eventType === "progress") {
            if (payload.status === "loading_notes") {
              setAnalyzeStatus("loading notes...");
            } else if (payload.status === "analyzing") {
              setAnalyzeStatus(
                `analyzing ${payload.note_count} notes...`,
              );
            }
          } else if (eventType === "result") {
            const chapters: ChapterDraft[] = payload.chapters.map(
              (ch: { name: string; start: string; note_count: number }) => ({
                id: crypto.randomUUID(),
                name: ch.name,
                start: ch.start,
                note_count: ch.note_count,
              }),
            );
            setProposedChapters(chapters);
            setNoteMonths(payload.note_months);
            setStep("editor");
          } else if (eventType === "error") {
            throw new Error(payload.message);
          }
        }
      }
    } catch (err) {
      setError(`chapter analysis failed: ${(err as Error).message}`);
      setStep("analyzing");
    }
  }

  async function handleConfirm(chapters: ChapterDraft[]) {
    if (!corpusTitle.trim()) {
      setError("Give your corpus a name first");
      return;
    }
    setSaving(true);
    setError("");
    try {
      // Save title
      const tr = await fetch(`${apiBase}/corpus`, {
        method: "PATCH",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ title: corpusTitle.trim() }),
      });
      if (!tr.ok)
        throw new Error(`title save: HTTP ${tr.status}: ${await tr.text()}`);
      const resp = await fetch(`${apiBase}/chapters/save`, {
        method: "PUT",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          chapters: chapters.map((ch) => ({
            name: ch.name,
            start: ch.start,
          })),
        }),
      });
      if (!resp.ok)
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      // Re-fetch corpus state and complete
      const c = await fetch(`${apiBase}/corpus`, { headers: authHeaders() });
      const fresh = (await c.json()) as CorpusInfo;
      onComplete(fresh);
    } catch (err) {
      setError(`save failed: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  if (step === "notes") {
    return (
      <div>
        <p className="mb-4 text-stone-600 text-sm leading-relaxed">
          Drop a zip of .md or .txt files to import a corpus.
        </p>
        <label className="inline-flex items-center justify-center w-12 h-12 rounded-full border border-stone-300 bg-white cursor-pointer hover:bg-stone-100 hover:border-stone-400 transition-colors" title="Upload a zip file">
          <svg className="w-5 h-5 text-stone-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20" />
          </svg>
          <input
            type="file"
            name="notes-zip"
            accept=".zip,application/zip"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleNotesUpload(file);
            }}
            className="hidden"
          />
        </label>
        {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
      </div>
    );
  }

  if (step === "analyzing") {
    return (
      <div>
        <h2 className="font-serif text-lg text-stone-900">
          Discovering chapters
        </h2>
        <p className="mt-1 mb-4 text-stone-600 text-sm leading-relaxed">
          {info?.note_count ?? 0} notes uploaded. Analyzing your notes to
          propose chapter boundaries...
        </p>
        <div className="flex items-center gap-2 text-sm text-stone-500">
          <span className="inline-block w-2 h-2 rounded-full bg-stone-400 animate-pulse" />
          {analyzeStatus}
        </div>
        {error && (
          <div className="mt-4">
            <p className="text-red-600 text-sm">{error}</p>
            <button
              onClick={runPropose}
              className="mt-2 text-xs text-stone-600 underline hover:text-stone-900"
            >
              Retry
            </button>
          </div>
        )}
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
    );
  }

  // step === "editor"
  return (
    <div>
      <p className="mb-4 text-stone-600 text-sm leading-relaxed">
        {info?.note_count ?? 0} notes uploaded. Name your corpus, review the
        proposed chapters, then confirm.
      </p>
      <input
        type="text"
        value={corpusTitle}
        onChange={(e) => setCorpusTitle(e.target.value)}
        placeholder="Corpus name"
        className="w-full mb-4 px-3 py-2 text-sm border border-stone-300 rounded bg-white focus:outline-none focus:border-stone-500"
      />
      <ChapterEditor
        initial={proposedChapters}
        noteMonths={noteMonths}
        onConfirm={handleConfirm}
        onCancel={() => {
          if (window.confirm("Discard the uploaded notes and start over?")) {
            onWipe();
          }
        }}
        saving={saving}
        onReanalyze={runPropose}
      />
      {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
    </div>
  );
}
