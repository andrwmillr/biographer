import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CorpusInfo } from "./ImportFlow";
import { authHeaders, getAuthToken, getSession } from "./auth";
import { HeaderMenu } from "./HeaderMenu";

const MD_COMPONENTS = {
  p: (props: any) => <p className="my-2 leading-relaxed" {...props} />,
  ul: (props: any) => <ul className="my-2 list-disc pl-5 space-y-0.5" {...props} />,
  ol: (props: any) => <ol className="my-2 list-decimal pl-5 space-y-0.5" {...props} />,
  li: (props: any) => <li className="leading-relaxed" {...props} />,
  h1: (props: any) => <h1 className="my-3 text-base font-semibold text-stone-900" {...props} />,
  h2: (props: any) => <h2 className="my-3 text-sm font-semibold text-stone-900" {...props} />,
  h3: (props: any) => <h3 className="my-2 text-sm font-semibold text-stone-800" {...props} />,
  strong: (props: any) => <strong className="font-semibold text-stone-900" {...props} />,
  em: (props: any) => <em className="italic" {...props} />,
  code: ({ inline, ...props }: any) =>
    inline ? (
      <code className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.85em]" {...props} />
    ) : (
      <code className="block rounded bg-stone-100 p-2 font-mono text-xs overflow-auto" {...props} />
    ),
  blockquote: (props: any) => (
    <blockquote className="my-2 border-l-2 border-stone-300 pl-3 text-stone-600" {...props} />
  ),
  a: (props: any) => <a className="text-stone-700 underline" {...props} />,
  hr: () => <hr className="my-3 border-stone-200" />,
};

const CHAPTER_MD_COMPONENTS = {
  p: (props: any) => <p className="my-4" {...props} />,
  ul: (props: any) => <ul className="my-4 list-disc pl-6 space-y-1" {...props} />,
  ol: (props: any) => <ol className="my-4 list-decimal pl-6 space-y-1" {...props} />,
  li: (props: any) => <li {...props} />,
  h1: (props: any) => <h1 className="mt-6 mb-3 text-2xl font-semibold text-stone-900" {...props} />,
  h2: (props: any) => <h2 className="mt-6 mb-3 text-xl font-semibold text-stone-900" {...props} />,
  h3: (props: any) => <h3 className="mt-5 mb-2 text-lg font-semibold text-stone-900" {...props} />,
  strong: (props: any) => <strong className="font-semibold text-stone-900" {...props} />,
  em: (props: any) => <em className="italic" {...props} />,
  blockquote: (props: any) => (
    <blockquote className="my-5 border-l-4 border-stone-300 pl-4 italic text-stone-700" {...props} />
  ),
  a: (props: any) => <a className="text-stone-700 underline" {...props} />,
  hr: () => <hr className="my-5 border-stone-200" />,
  code: ({ inline, ...props }: any) =>
    inline ? (
      <code className="rounded bg-stone-100 px-1 py-0.5 font-mono text-[0.85em]" {...props} />
    ) : (
      <code className="block rounded bg-stone-100 p-3 font-mono text-sm overflow-auto" {...props} />
    ),
};

function shortPath(p: string, runDir: string): string {
  if (!p) return "";
  if (runDir) {
    const tail = runDir.split("/").pop()!;
    const idx = p.indexOf(tail + "/");
    if (idx >= 0) return p.slice(idx + tail.length + 1);
  }
  return p.split("/").pop() || p;
}

const TOOL_VERB: Record<string, string> = {
  Read: "read",
  Write: "write",
  Edit: "edit",
  TodoWrite: "todos",
};

function formatTool(name: string, input: unknown, runDir: string): string {
  const verb = TOOL_VERB[name] ?? name.toLowerCase();
  const i = (input ?? {}) as Record<string, unknown>;
  if (name === "Read" || name === "Write" || name === "Edit") {
    const path = shortPath(String(i.file_path ?? ""), runDir);
    let suffix = "";
    if (name === "Read" && (i.offset || i.limit)) {
      const off = Number(i.offset ?? 0);
      const lim = Number(i.limit ?? 0);
      suffix = ` :${off}${lim ? `-${off + lim}` : ""}`;
    }
    return `${verb} ${path || "?"}${suffix}`;
  }
  if (name === "TodoWrite") {
    const todos = Array.isArray(i.todos) ? i.todos : [];
    return `${verb} (${todos.length})`;
  }
  return verb;
}

type LogItem =
  | { kind: "narration"; text: string }
  | { kind: "user"; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      input?: unknown;
      result?: "ok" | "err";
      error_text?: string;
    }
  | { kind: "status"; text: string };

type ThemesViewProps = {
  apiBase: string;
  wsBase: string;
  corpusInfo: CorpusInfo;
  userEmail: string | null;
  userCorpora: string[];
  viewMode: "eras" | "themes";
  onSetViewMode: (m: "eras" | "themes") => void;
  /** Calls /corpus/wipe and routes the parent away from "ready". Throws on
   *  HTTP error so this view can surface it before unmount. */
  onWipe: () => Promise<void>;
  onSwitchCorpus: () => void;
  onLogout: () => void;
};

export function ThemesView({
  apiBase,
  wsBase,
  corpusInfo,
  userEmail,
  userCorpora,
  onSetViewMode,
  onWipe,
  onSwitchCorpus,
  onLogout,
}: ThemesViewProps) {
  const [model, setModel] = useState<string>("opus-4.7");
  const [error, setError] = useState<string>("");
  const [topN, setTopN] = useState<number>(10);
  const [phase, setPhase] = useState<
    "idle" | "spinning" | "spin-done" | "curating" | "locked" | "error"
  >("idle");
  const [runDir, setRunDir] = useState<string>("");
  const [output, setOutput] = useState<string>("");
  const [log, setLog] = useState<LogItem[]>([]);
  const [reply, setReply] = useState<string>("");
  const [locked, setLocked] = useState<string>("");
  const [cost, setCost] = useState<number>(0);
  const [status, setStatus] = useState<
    "idle" | "generating" | "awaiting_reply" | "done" | "error"
  >("idle");
  const wsRef = useRef<WebSocket | null>(null);
  const narrationBufRef = useRef<string>("");
  const logRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll the log to bottom when new content arrives.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const lastItem = log[log.length - 1];
    if (lastItem?.kind === "user" || distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [log, status]);

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

  async function startSpin() {
    setPhase("spinning");
    setOutput("");
    setRunDir("");
    setLocked("");
    setLog([]);
    setError("");

    try {
      const resp = await fetch(`${apiBase}/themes-spin`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ top_n: topN, model }),
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      }
      const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += value;
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let payload: any;
          try {
            payload = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          if (payload.type === "delta") {
            setOutput((s) => s + payload.text);
          } else if (payload.type === "start") {
            setRunDir(payload.run_dir);
          } else if (payload.type === "done") {
            setPhase("spin-done");
            setCost(payload.cost_usd ?? 0);
          } else if (payload.type === "error") {
            setError(payload.message);
            setPhase("error");
          }
        }
      }
    } catch (e) {
      setError(`spin failed: ${(e as Error).message}`);
      setPhase("error");
    }
  }

  function startCuration() {
    if (!runDir) return;
    setPhase("curating");
    setStatus("generating");
    setLog([]);
    setReply("");
    narrationBufRef.current = "";
    setError("");

    const ws = new WebSocket(
      `${wsBase}/themes-curate?session=${encodeURIComponent(getSession() || "")}` +
        `&auth=${encodeURIComponent(getAuthToken() || "")}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "start", run_dir: runDir, model }));
    };
    ws.onmessage = (ev) => {
      let payload: any;
      try {
        payload = JSON.parse(ev.data);
      } catch {
        return;
      }
      const t = payload.type;
      if (t === "narration") {
        narrationBufRef.current += payload.text;
        flushNarration();
      } else if (t === "status") {
        flushNarration();
        if (payload.status === "generating") setStatus("generating");
        else if (payload.status === "awaiting_reply") setStatus("awaiting_reply");
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
      } else if (t === "themes_update") {
        setLocked(payload.content);
        setPhase("locked");
      } else if (t === "turn_end") {
        flushNarration();
        if (typeof payload.cost_usd === "number") setCost(payload.cost_usd);
      } else if (t === "done") {
        flushNarration();
        setStatus("done");
        if (typeof payload.cost_usd === "number") setCost(payload.cost_usd);
      } else if (t === "log") {
        flushNarration();
        setLog((l) => [...l, { kind: "status", text: payload.text }]);
      } else if (t === "error") {
        setError(payload.message);
        setStatus("error");
      }
    };
    ws.onerror = () => {
      setError("themes websocket error");
      setStatus("error");
    };
    ws.onclose = () => {
      flushNarration();
      if (status !== "done" && status !== "error") setStatus("done");
    };
  }

  function sendReply() {
    const ws = wsRef.current;
    if (!ws || !reply.trim()) return;
    ws.send(JSON.stringify({ type: "reply", text: reply }));
    setLog((l) => [...l, { kind: "user", text: reply }]);
    setReply("");
    setStatus("generating");
  }

  function stopCuration() {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
    setStatus("done");
  }

  async function handleWipeClick() {
    if (
      !window.confirm(
        "Wipe this corpus? This deletes all uploaded notes and eras from the host.",
      )
    ) {
      return;
    }
    try {
      await onWipe();
    } catch (err) {
      setError(`wipe failed: ${(err as Error).message}`);
    }
  }

  return (
    <div className="min-h-full relative">
      <header className="border-b border-stone-200 bg-white">
        <div className="mx-auto max-w-5xl px-6 py-4 flex items-center gap-4">
          <h1 className="font-serif text-xl">Biographer</h1>
          <div className="flex items-center gap-1 ml-2">
            <button
              className="font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors text-stone-400 hover:text-stone-700"
              onClick={() => onSetViewMode("eras")}
            >
              Eras
            </button>
            <button
              className="font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors text-stone-700 border-b-2 border-stone-700"
              onClick={() => onSetViewMode("themes")}
            >
              Themes
            </button>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <select
              className="rounded border border-stone-300 bg-white px-2 py-1 text-sm"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={phase === "spinning" || phase === "curating"}
              title="Model used for themes"
            >
              <option value="opus-4.7">opus-4.7</option>
              <option value="opus-4.6">opus-4.6</option>
              <option value="sonnet-4.6">sonnet-4.6</option>
            </select>
            <label className="text-xs text-stone-600 flex items-center gap-1">
              top-n
              <input
                type="number"
                min={3}
                max={20}
                value={topN}
                onChange={(e) => setTopN(parseInt(e.target.value) || 10)}
                disabled={phase === "spinning" || phase === "curating"}
                className="w-12 rounded border border-stone-300 bg-white px-1 py-1 text-sm tabular-nums"
              />
            </label>
            {phase === "curating" ? (
              <button
                className="rounded bg-stone-800 px-3 py-1 text-sm text-white hover:bg-stone-700"
                onClick={stopCuration}
              >
                Stop
              </button>
            ) : phase === "spinning" ? (
              <button
                className="rounded bg-stone-400 px-3 py-1 text-sm text-white"
                disabled
              >
                Generating…
              </button>
            ) : (
              <button
                className="rounded bg-stone-900 px-3 py-1 text-sm text-white hover:bg-stone-700"
                onClick={startSpin}
              >
                {phase === "spin-done" || phase === "locked" ? "Re-spin" : "Generate themes"}
              </button>
            )}
            <HeaderMenu
              isLegacy={corpusInfo.is_legacy}
              userEmail={userEmail}
              hasMultipleCorpora={userCorpora.length > 1}
              onWipe={handleWipeClick}
              onSwitchCorpus={onSwitchCorpus}
              onLogout={onLogout}
            />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-6 py-4">
        {error && (
          <div className="mb-6 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
            {error}
          </div>
        )}

        <section className="flex flex-col h-[80vh]">
          <div className="mb-2 flex items-center gap-2 border-b border-stone-200 pb-2">
            <span className="font-sans text-xs uppercase tracking-wider text-stone-700">
              {phase === "idle" && "no run yet"}
              {phase === "spinning" && "generating round 1…"}
              {phase === "spin-done" && "round 1 done — ready to curate"}
              {phase === "curating" && "curating"}
              {phase === "locked" && "themes locked"}
              {phase === "error" && "error"}
            </span>
            {runDir && (
              <span className="font-mono text-[11px] text-stone-400 truncate">
                {runDir}
              </span>
            )}
            {cost > 0 && (
              <span className="font-sans text-xs text-stone-500 ml-auto tabular-nums">
                ${cost.toFixed(4)}
              </span>
            )}
          </div>

          {phase === "idle" && (
            <div className="flex-1 flex items-center justify-center text-stone-400 text-sm">
              Click <span className="font-medium mx-1 text-stone-600">Generate themes</span> to start round 1.
            </div>
          )}

          {(phase === "spinning" || phase === "spin-done") && (
            <article className="flex-1 overflow-auto rounded border border-stone-200 bg-white p-6 font-serif text-[15px] leading-[1.7] text-stone-900">
              {output ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={CHAPTER_MD_COMPONENTS}>
                  {output}
                </ReactMarkdown>
              ) : (
                <span className="font-sans text-sm text-stone-400">
                  {phase === "spinning" ? "starting…" : "(no output)"}
                </span>
              )}
            </article>
          )}

          {phase === "spin-done" && (
            <div className="mt-3 flex items-center gap-3">
              <button
                className="rounded bg-stone-900 px-4 py-2 text-sm text-white hover:bg-stone-700"
                onClick={startCuration}
              >
                Curate themes
              </button>
              <span className="text-xs text-stone-500">
                go from candidates to ~5 final themes
              </span>
            </div>
          )}

          {phase === "curating" && (
            <>
              <div
                ref={logRef}
                className="flex-1 rounded border border-stone-200 bg-white p-4 font-sans text-sm text-stone-800 overflow-auto"
              >
                {status === "generating" && log.length === 0 && (
                  <span className="text-stone-400">starting curation…</span>
                )}
                {log.map((it, i) => {
                  if (it.kind === "narration")
                    return (
                      <div key={i}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                          {it.text}
                        </ReactMarkdown>
                      </div>
                    );
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
                          {formatTool(it.name, it.input, runDir)}
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
                  return null;
                })}
                {status === "generating" && log.length > 0 && (
                  <span className="inline-block w-2 h-4 ml-0.5 align-text-bottom bg-stone-400 animate-pulse" />
                )}
              </div>

              <div className="mt-3">
                <div className="mb-1 flex items-center gap-2 text-xs font-sans">
                  {status === "generating" && (
                    <>
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                      <span className="text-amber-700">working…</span>
                    </>
                  )}
                  {status === "awaiting_reply" && (
                    <>
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500" />
                      <span className="text-emerald-700">your move (drop, merge, propose, /lock)</span>
                    </>
                  )}
                  {status === "done" && (
                    <span className="text-stone-500">curation ended</span>
                  )}
                </div>
                <div className="relative">
                  <textarea
                    className={
                      "block w-full resize-none rounded border px-3 pt-2 pb-11 text-sm font-sans disabled:text-stone-400 transition-colors " +
                      (status === "awaiting_reply"
                        ? "border-emerald-400 bg-white"
                        : "border-stone-300 bg-stone-50")
                    }
                    rows={3}
                    value={reply}
                    placeholder={
                      status === "awaiting_reply"
                        ? "drop X / merge X and Y / propose: theme name / /lock"
                        : "wait for the model…"
                    }
                    disabled={status !== "awaiting_reply"}
                    onChange={(e) => setReply(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && reply.trim()) {
                        e.preventDefault();
                        sendReply();
                      }
                    }}
                  />
                  <button
                    className="absolute bottom-2 right-2 flex h-7 w-7 items-center justify-center rounded-full bg-stone-900 text-white hover:bg-stone-700 disabled:opacity-40"
                    onClick={sendReply}
                    disabled={status !== "awaiting_reply" || !reply.trim()}
                    aria-label="Send"
                  >
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
                  </button>
                </div>
              </div>
            </>
          )}

          {phase === "locked" && locked && (
            <article className="flex-1 overflow-auto rounded border border-stone-200 bg-white p-6 font-serif text-[15px] leading-[1.7] text-stone-900">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={CHAPTER_MD_COMPONENTS}>
                {locked}
              </ReactMarkdown>
            </article>
          )}
        </section>
      </main>
    </div>
  );
}
