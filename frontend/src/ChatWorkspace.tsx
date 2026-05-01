import { useEffect, useRef, useState } from "react";
import { Markdown, formatTool } from "./markdown";
import { NotesPane } from "./NotesPane";
import { authHeaders, getAuthToken, getSession } from "./auth";
import type {
  FinalizedInfo,
  LogItem,
  Note,
  Phase,
  SpawnedInfo,
  WorkspaceScope,
} from "./types";

type ChatWorkspaceProps = {
  apiBase: string;
  wsBase: string;
  scope: WorkspaceScope;
  // Optional control: shown above the workspace as a back link. Used by the
  // eras tab to return to the era list. Themes scope omits this.
  onBack?: () => void;
  // Called when the server emits `finalized` — used by the eras tab to
  // refetch the era list so the chapter checkmark updates.
  onFinalized?: (info: FinalizedInfo) => void;
  // Optional title for the workspace (e.g. era name, "Themes").
  title?: string;
};

type WSStatus =
  | "idle"
  | "connecting"
  | "generating"
  | "awaiting_reply"
  | "done"
  | "error";

const MODELS = ["opus-4.7", "opus-4.6", "sonnet-4.6"] as const;

export function ChatWorkspace({
  apiBase,
  wsBase,
  scope,
  onBack,
  onFinalized,
  title,
}: ChatWorkspaceProps) {
  const [phase, setPhase] = useState<Phase>("pre-gen");
  const [model, setModel] = useState<string>("opus-4.7");
  const [future, setFuture] = useState<boolean>(
    scope.kind === "era" ? scope.future : false,
  );
  const [topN, setTopN] = useState<number>(
    scope.kind === "themes" ? scope.topN : 7,
  );

  const [notes, setNotes] = useState<Note[]>([]);
  const [notesLoading, setNotesLoading] = useState<boolean>(false);
  const [notesError, setNotesError] = useState<string>("");

  const [log, setLog] = useState<LogItem[]>([]);
  const [draft, setDraft] = useState<string>("");
  const [replyText, setReplyText] = useState<string>("");
  const [wsStatus, setWsStatus] = useState<WSStatus>("idle");
  const [spawned, setSpawned] = useState<SpawnedInfo | null>(null);
  const [cost, setCost] = useState<number>(0);
  const [error, setError] = useState<string>("");
  const [finalized, setFinalized] = useState<FinalizedInfo | null>(null);
  const [highlightDate, setHighlightDate] = useState<string>("");

  const wsRef = useRef<WebSocket | null>(null);
  const narrationBufRef = useRef<string>("");
  const chatLogRef = useRef<HTMLDivElement | null>(null);
  // Tracks whether we've seen the first awaiting_reply, which is the
  // generating→iterating transition. Used to ignore later awaiting_reply
  // events for phase decisions (we only need the first one to flip layout).
  const sawFirstAwaitingRef = useRef<boolean>(false);

  // ---- Notes fetch ----
  // Re-runs when scope changes (which only happens via remount via key)
  // or when topN changes (themes mode, pre-gen only — see input handler).
  useEffect(() => {
    let cancelled = false;
    setNotesLoading(true);
    setNotes([]);
    setNotesError("");
    const url =
      scope.kind === "era"
        ? `${apiBase}/notes?era=${encodeURIComponent(scope.era)}`
        : `${apiBase}/notes/themes-top-n?n=${topN}`;
    fetch(url, { headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        return r.json() as Promise<Note[]>;
      })
      .then((data) => {
        if (cancelled) return;
        setNotes(data);
      })
      .catch((e) => {
        if (cancelled) return;
        setNotesError(`failed to load notes: ${(e as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setNotesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, scope, topN]);

  // ---- Auto-scroll the chat log when new content arrives. ----
  useEffect(() => {
    const el = chatLogRef.current;
    if (!el) return;
    const last = log[log.length - 1];
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (last?.kind === "user" || distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [log, wsStatus]);

  // ---- Cleanup: close WS on unmount. ----
  useEffect(() => {
    return () => {
      const ws = wsRef.current;
      if (ws && ws.readyState <= WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "stop" }));
        } catch {
          // ignore
        }
        ws.close();
      }
    };
  }, []);

  // ---- WS handlers ----
  function flushNarration() {
    if (!narrationBufRef.current) return;
    const text = narrationBufRef.current;
    narrationBufRef.current = "";
    setLog((l) => {
      const last = l[l.length - 1];
      if (last && last.kind === "narration") {
        return [...l.slice(0, -1), { kind: "narration", text: last.text + text }];
      }
      return [...l, { kind: "narration", text }];
    });
  }

  function startSession() {
    if (wsStatus !== "idle" && wsStatus !== "done" && wsStatus !== "error") return;
    setLog([]);
    setDraft("");
    setReplyText("");
    setSpawned(null);
    setCost(0);
    setError("");
    setFinalized(null);
    setHighlightDate("");
    narrationBufRef.current = "";
    sawFirstAwaitingRef.current = false;
    setPhase("generating");
    setWsStatus("connecting");

    const wsPath = scope.kind === "era" ? "/session" : "/themes-curate";
    const ws = new WebSocket(
      `${wsBase}${wsPath}` +
        `?session=${encodeURIComponent(getSession() || "")}` +
        `&auth=${encodeURIComponent(getAuthToken() || "")}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      const startMsg =
        scope.kind === "era"
          ? { type: "start", era: scope.era, future, model }
          : { type: "start", top_n: topN, model };
      ws.send(JSON.stringify(startMsg));
    };

    ws.onmessage = (ev) => {
      let payload: any;
      try {
        payload = JSON.parse(ev.data);
      } catch {
        return;
      }
      const t = payload.type;
      if (t === "spawned") {
        setSpawned(payload as SpawnedInfo);
      } else if (t === "narration") {
        narrationBufRef.current += payload.text;
        flushNarration();
      } else if (t === "status") {
        flushNarration();
        if (payload.status === "generating") {
          setWsStatus("generating");
        } else if (payload.status === "awaiting_reply") {
          setWsStatus("awaiting_reply");
          if (!sawFirstAwaitingRef.current) {
            sawFirstAwaitingRef.current = true;
            // First awaiting_reply unlocks the iterating layout.
            setPhase((p) => (p === "finalized" ? p : "iterating"));
          }
        }
      } else if (t === "tool_use") {
        flushNarration();
        setLog((l) => [
          ...l,
          { kind: "tool", id: payload.id, name: payload.name, input: payload.input },
        ]);
      } else if (t === "tool_result") {
        setLog((l) =>
          l.map((it) =>
            it.kind === "tool" && it.id === payload.id
              ? {
                  ...it,
                  result: payload.is_error ? "err" : "ok",
                  error_text: payload.is_error ? payload.text : undefined,
                }
              : it,
          ),
        );
      } else if (t === "draft_update") {
        // Eras: only "output" is the chapter. Themes: both "output" (round-1
        // candidates) and "themes" (curated) update the same draft pane.
        const interesting =
          (scope.kind === "era" && payload.kind === "output") ||
          (scope.kind === "themes" &&
            (payload.kind === "output" || payload.kind === "themes"));
        if (interesting) setDraft(payload.content);
      } else if (t === "log") {
        flushNarration();
        setLog((l) => [...l, { kind: "status", text: payload.text }]);
      } else if (t === "turn_end") {
        flushNarration();
        if (typeof payload.cost_usd === "number") setCost(payload.cost_usd);
      } else if (t === "finalized") {
        flushNarration();
        const info: FinalizedInfo = {
          content: payload.content,
          location: payload.location,
          words: payload.words,
          overwritten: payload.overwritten,
        };
        setFinalized(info);
        setDraft(info.content);
        setPhase("finalized");
        if (onFinalized) onFinalized(info);
      } else if (t === "done") {
        flushNarration();
        setWsStatus("done");
        if (typeof payload.cost_usd === "number") setCost(payload.cost_usd);
      } else if (t === "error") {
        setError(payload.message ?? "unknown error");
        setWsStatus("error");
      }
    };

    ws.onerror = () => {
      setError("websocket error");
      setWsStatus("error");
    };

    ws.onclose = () => {
      flushNarration();
      // If we never reached done/error, treat as done.
      setWsStatus((s) => (s === "done" || s === "error" ? s : "done"));
    };
  }

  function sendReply() {
    const ws = wsRef.current;
    const text = replyText.trim();
    if (!ws || !text || wsStatus !== "awaiting_reply") return;
    ws.send(JSON.stringify({ type: "reply", text }));
    setLog((l) => [...l, { kind: "user", text }]);
    setReplyText("");
    setWsStatus("generating");
  }

  function sendFinalize() {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "finalize" }));
    // For themes the server runs an agent turn before emitting `finalized`,
    // so flip wsStatus back to generating to disable the prompter meanwhile.
    if (scope.kind === "themes") setWsStatus("generating");
  }

  function sendStop() {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
    setWsStatus("done");
  }

  function handleCiteClick(dateKey: string) {
    setHighlightDate(dateKey);
  }

  // ---- Render helpers ----
  const sessionLive =
    wsStatus === "connecting" ||
    wsStatus === "generating" ||
    wsStatus === "awaiting_reply";
  const canFinalize =
    phase === "iterating" && wsStatus === "awaiting_reply" && !!draft;

  const promptStatus: "pre-gen" | "generating" | "awaiting_reply" | "finalized" =
    phase === "finalized"
      ? "finalized"
      : phase === "pre-gen"
        ? "pre-gen"
        : wsStatus === "awaiting_reply"
          ? "awaiting_reply"
          : "generating";

  function Prompter() {
    const enabled = promptStatus === "awaiting_reply";
    const buttonEnabled =
      promptStatus === "pre-gen" ||
      (promptStatus === "awaiting_reply" && replyText.trim().length > 0);
    const placeholder =
      promptStatus === "pre-gen"
        ? scope.kind === "era"
          ? "Press ▶ to start drafting this era. The agent reads notes, drafts, then opens the chat."
          : "Press ▶ to surface the top recurring threads. The agent reads notes, sketches candidates, then opens the chat."
        : promptStatus === "generating"
          ? "thinking…"
          : promptStatus === "finalized"
            ? "draft locked"
            : "reply to the agent…";
    const borderClass =
      promptStatus === "awaiting_reply"
        ? "border-emerald-400 bg-white"
        : promptStatus === "generating"
          ? "border-stone-200 bg-stone-50"
          : promptStatus === "pre-gen"
            ? "border-stone-300 bg-white"
            : "border-stone-200 bg-stone-50";
    return (
      <div className="relative">
        <textarea
          className={
            "block w-full resize-none rounded border px-3 pt-2 pb-11 text-sm font-sans transition-colors disabled:text-stone-400 " +
            borderClass
          }
          rows={3}
          value={replyText}
          placeholder={placeholder}
          disabled={!enabled}
          onChange={(e) => setReplyText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && replyText.trim()) {
              e.preventDefault();
              sendReply();
            }
          }}
        />
        <button
          className="absolute bottom-2 right-2 flex h-7 w-7 items-center justify-center rounded-full bg-stone-900 text-white hover:bg-stone-700 disabled:opacity-40"
          onClick={() => {
            if (promptStatus === "pre-gen") startSession();
            else sendReply();
          }}
          disabled={!buttonEnabled}
          aria-label={promptStatus === "pre-gen" ? "Start" : "Send"}
        >
          {promptStatus === "pre-gen" ? (
            <svg
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-3.5 w-3.5"
              aria-hidden="true"
            >
              <path d="M8 5v14l11-7z" />
            </svg>
          ) : (
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-4 w-4"
            >
              <path d="M9 14l-4-4 4-4" />
              <path d="M5 10h11a4 4 0 0 1 4 4v2" />
            </svg>
          )}
        </button>
      </div>
    );
  }

  const isPreOrGen = phase === "pre-gen" || phase === "generating";

  return (
    <div className="mx-auto max-w-7xl px-6 py-4">
      {/* ---- Title / control bar ---- */}
      <div className="mb-4 flex items-center gap-3 flex-wrap">
        {onBack && (
          <button
            onClick={onBack}
            className="font-sans text-xs text-stone-500 hover:text-stone-800 underline-offset-2 hover:underline"
          >
            ← back
          </button>
        )}
        {title && (
          <h2 className="font-serif text-lg text-stone-900">{title}</h2>
        )}
        <div className="ml-auto flex items-center gap-2 flex-wrap">
          <select
            className="rounded border border-stone-300 bg-white px-2 py-1 text-sm disabled:text-stone-400"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={phase !== "pre-gen"}
            title="Model used for this session"
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          {scope.kind === "era" && (
            <label
              className={
                "flex items-center gap-1 text-xs " +
                (phase === "pre-gen"
                  ? "text-stone-600 cursor-pointer"
                  : "text-stone-400")
              }
              title="Also feed any later eras' chapters & digests already on disk into this draft (hindsight context)."
            >
              <input
                type="checkbox"
                checked={future}
                onChange={(e) => setFuture(e.target.checked)}
                disabled={phase !== "pre-gen"}
                className="accent-stone-700"
              />
              future
            </label>
          )}
          {scope.kind === "themes" && (
            <label
              className={
                "flex items-center gap-1 text-xs " +
                (phase === "pre-gen" ? "text-stone-600" : "text-stone-400")
              }
            >
              top-n
              <input
                type="number"
                min={3}
                max={20}
                value={topN}
                onChange={(e) => setTopN(parseInt(e.target.value) || 7)}
                disabled={phase !== "pre-gen"}
                className="w-12 rounded border border-stone-300 bg-white px-1 py-1 text-sm tabular-nums disabled:text-stone-400"
              />
            </label>
          )}
          {sessionLive && (
            <button
              className="rounded border border-stone-300 bg-white px-3 py-1 text-xs text-stone-700 hover:bg-stone-100"
              onClick={sendStop}
            >
              Stop
            </button>
          )}
          <button
            className="rounded bg-emerald-700 px-3 py-1 text-xs text-white hover:bg-emerald-800 disabled:opacity-40 disabled:cursor-not-allowed"
            onClick={sendFinalize}
            disabled={!canFinalize}
            title={
              phase === "finalized"
                ? "already finalized"
                : !draft
                  ? "no draft yet"
                  : wsStatus !== "awaiting_reply"
                    ? "wait for the agent"
                    : "lock the current draft to disk"
            }
          >
            {phase === "finalized" ? "Finalized ✓" : "Finalize"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          {error}
        </div>
      )}
      {notesError && (
        <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          {notesError}
        </div>
      )}

      {/* ---- Run/cost metadata ---- */}
      {(spawned || cost > 0) && (
        <div className="mb-3 flex items-center gap-3 font-mono text-[11px] text-stone-400">
          {spawned?.run_dir && <span>run: {spawned.run_dir}</span>}
          {spawned?.input_chars != null && spawned.input_chars > 0 && (
            <span>· {spawned.input_chars.toLocaleString()} input chars</span>
          )}
          {scope.kind === "era" && spawned?.notes != null && (
            <span>
              · {spawned.notes} notes
              {spawned.prior_chapters
                ? ` + ${spawned.prior_chapters} prior`
                : ""}
              {spawned.future_chapters
                ? ` + ${spawned.future_chapters} future`
                : ""}
            </span>
          )}
          {scope.kind === "themes" && spawned?.top_n != null && (
            <span>· top-{spawned.top_n}</span>
          )}
          {cost > 0 && <span>· ${cost.toFixed(4)}</span>}
        </div>
      )}

      {finalized && (
        <div className="mb-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          Locked to <span className="font-mono">{finalized.location}</span> ·{" "}
          {finalized.words} words
          {finalized.overwritten && " · overwrote previous"}
        </div>
      )}

      {/* ---- Phase-dependent body ---- */}
      {isPreOrGen ? (
        <div className="space-y-6">
          <Prompter />
          <NotesPane
            notes={notes}
            loading={notesLoading}
            layout="grid"
            emptyHint={
              scope.kind === "era"
                ? "no notes in this era"
                : "no notes available"
            }
          />
        </div>
      ) : (
        <div className="grid gap-4 grid-cols-1 lg:grid-cols-12 h-[80vh]">
          {/* ---- Chat pane ---- */}
          <section className="lg:col-span-4 flex flex-col min-h-0">
            <div
              ref={chatLogRef}
              className="flex-1 rounded border border-stone-200 bg-white p-4 font-sans text-sm text-stone-800 overflow-auto min-h-0"
            >
              {log.length === 0 && wsStatus === "generating" && (
                <span className="text-stone-400">starting…</span>
              )}
              {log.map((it, i) => {
                if (it.kind === "narration") {
                  return (
                    <div key={i}>
                      <Markdown
                        content={it.text}
                        variant="narration"
                        onCiteClick={handleCiteClick}
                      />
                    </div>
                  );
                }
                if (it.kind === "user") {
                  return (
                    <div key={i} className="my-3 flex justify-end">
                      <div className="max-w-[85%] rounded-lg bg-stone-100 border border-stone-200 px-3 py-2 text-stone-800 whitespace-pre-wrap">
                        {it.text}
                      </div>
                    </div>
                  );
                }
                if (it.kind === "tool") {
                  return (
                    <div key={i} className="my-1 text-xs font-mono">
                      <div className="text-stone-500">
                        {formatTool(it.name, it.input, spawned?.run_dir ?? "")}
                        {it.result === "ok" && " ✓"}
                        {it.result === "err" && " ✗"}
                      </div>
                      {it.error_text && (
                        <div className="ml-4 mt-0.5 whitespace-pre-wrap text-red-700">
                          {it.error_text}
                        </div>
                      )}
                    </div>
                  );
                }
                if (it.kind === "status") {
                  return (
                    <div
                      key={i}
                      className="my-1 text-xs font-mono text-stone-400"
                    >
                      {it.text}
                    </div>
                  );
                }
                return null;
              })}
              {wsStatus === "generating" && log.length > 0 && (
                <span className="inline-block w-2 h-4 ml-0.5 align-text-bottom bg-stone-400 animate-pulse" />
              )}
            </div>
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2 text-xs font-sans">
                {wsStatus === "generating" && (
                  <>
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                    <span className="text-amber-700">working…</span>
                  </>
                )}
                {wsStatus === "awaiting_reply" && phase !== "finalized" && (
                  <>
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500" />
                    <span className="text-emerald-700">ready for your reply</span>
                  </>
                )}
                {phase === "finalized" && (
                  <span className="text-stone-500">draft locked</span>
                )}
                {wsStatus === "done" && phase !== "finalized" && (
                  <span className="text-stone-500">session ended</span>
                )}
              </div>
              <Prompter />
            </div>
          </section>

          {/* ---- Draft pane ---- */}
          <section className="lg:col-span-5 flex flex-col min-h-0">
            <div className="mb-1 flex items-center gap-2 text-xs font-sans uppercase tracking-wider text-stone-500">
              <span>{phase === "finalized" ? "locked draft" : "draft"}</span>
              {draft && (
                <span className="normal-case tracking-normal text-stone-400">
                  ({draft.length.toLocaleString()} ch)
                </span>
              )}
            </div>
            <article
              className={
                "flex-1 rounded border bg-white p-6 font-serif text-[16px] leading-[1.7] text-stone-900 overflow-auto min-h-0 " +
                (phase === "finalized"
                  ? "border-emerald-200"
                  : "border-stone-200")
              }
            >
              {draft ? (
                <Markdown
                  content={draft}
                  variant="chapter"
                  onCiteClick={handleCiteClick}
                />
              ) : (
                <span className="font-sans text-sm text-stone-400">
                  (no draft content yet)
                </span>
              )}
            </article>
          </section>

          {/* ---- Notes pane ---- */}
          <aside className="lg:col-span-3 flex flex-col min-h-0">
            <div className="mb-1 flex items-center gap-2 text-xs font-sans uppercase tracking-wider text-stone-500">
              <span>notes</span>
              {notes.length > 0 && (
                <span className="normal-case tracking-normal text-stone-400">
                  ({notes.length})
                </span>
              )}
            </div>
            <div className="flex-1 overflow-auto rounded border border-stone-200 bg-stone-50 p-3 min-h-0">
              <NotesPane
                notes={notes}
                loading={notesLoading}
                layout="column"
                highlightDate={highlightDate}
                emptyHint={
                  scope.kind === "era"
                    ? "no notes in this era"
                    : "no notes available"
                }
              />
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}
