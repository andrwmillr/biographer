import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import {
  ImperativePanelGroupHandle,
  ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { Markdown, formatTool } from "./markdown";
import { NotesTimeline } from "./NotesTimeline";
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
  // Called when the server emits `finalized` — used by the eras tab to
  // refetch the era list so the chapter checkmark updates.
  onFinalized?: (info: FinalizedInfo) => void;
  // Title slot for the control bar — string or JSX (e.g. era selector).
  titleNode?: ReactNode;
};

type WSStatus =
  | "idle"
  | "connecting"
  | "generating"
  | "awaiting_reply"
  | "done"
  | "error";

type PaneId = "chat" | "draft" | "notes";
const PANE_ORDER: PaneId[] = ["chat", "draft", "notes"];
const PANE_TITLES: Record<PaneId, string> = {
  chat: "Chat",
  draft: "Draft",
  notes: "Notes",
};
// Per-pane initial size used on first-ever mount. After that, react-
// resizable-panels' autoSaveId persists user adjustments to localStorage
// and restores them across chapter switches and reloads.
const PANE_DEFAULT_SIZE: Record<PaneId, number> = {
  chat: 48.5,
  draft: 3,
  notes: 48.5,
};
// One-shot flag: the auto-expand-draft-on-first-content effect sets this
// the first time it fires, so it doesn't override the user's saved
// layout on later chapter loads.
const DRAFT_AUTO_EXPANDED_KEY = "biographer-draft-auto-expanded";

const MODELS = ["opus-4.7", "opus-4.6", "sonnet-4.6"] as const;

export function ChatWorkspace({
  apiBase,
  wsBase,
  scope,
  onFinalized,
  titleNode,
}: ChatWorkspaceProps) {
  const [phase, setPhase] = useState<Phase>("pre-gen");
  const [model, setModel] = useState<string>("opus-4.7");
  const [future, setFuture] = useState<boolean>(
    scope.kind === "era" ? scope.future : false,
  );
  const [topN, setTopN] = useState<number>(
    scope.kind === "themes" ? scope.topN : 5,
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

  // Pane collapse state. Draft starts collapsed on first-ever mount —
  // it's empty until the agent produces output, and the auto-expand
  // effect below opens it on first content. After that, autoSaveId on
  // the PanelGroup restores layout (and we derive collapsed from the
  // restored sizes via the Panel onCollapse/onExpand callbacks).
  const [collapsed, setCollapsed] = useState<Record<PaneId, boolean>>({
    chat: false,
    draft: true,
    notes: false,
  });
  const panelRefs = useRef<Record<PaneId, ImperativePanelHandle | null>>({
    chat: null,
    draft: null,
    notes: null,
  });
  const panelGroupRef = useRef<ImperativePanelGroupHandle | null>(null);
  // Pop-out: when set, the matching pane renders in a full-screen
  // overlay for roomier reading/interaction. Original pane shows a
  // "popped out" placeholder so content isn't duplicated.
  const [popOut, setPopOut] = useState<PaneId | null>(null);
  // Latches once we've auto-expanded draft on first content. Prevents
  // re-expanding if the user manually collapses it later in the session.
  const draftAutoExpandedRef = useRef<boolean>(false);

  // Tier 2.5 resume: remember the run_dir of the in-flight session so
  // we can reconnect with `resume: true, run_id: …` if the WS dies
  // (tab put to sleep, network blip, etc.). Keyed in localStorage by
  // workflow + scope so era and themes don't collide.
  const runIdStorageKey =
    scope.kind === "era"
      ? `workspace_run_era_${scope.era}`
      : `workspace_run_themes_default`;
  const runIdRef = useRef<string | null>(
    typeof window !== "undefined"
      ? window.localStorage.getItem(runIdStorageKey)
      : null,
  );
  function setRunId(runId: string | null) {
    runIdRef.current = runId;
    if (typeof window === "undefined") return;
    if (runId) window.localStorage.setItem(runIdStorageKey, runId);
    else window.localStorage.removeItem(runIdStorageKey);
  }

  const wsRef = useRef<WebSocket | null>(null);
  const narrationBufRef = useRef<string>("");
  const chatLogRef = useRef<HTMLDivElement | null>(null);
  // Tracks whether we've seen the first awaiting_reply, which is the
  // generating→iterating transition. Used to ignore later awaiting_reply
  // events for phase decisions (we only need the first one to flip layout).
  const sawFirstAwaitingRef = useRef<boolean>(false);
  // Drives the first-turn token-progress estimate (no real progress event
  // from the model during prompt processing).
  const [wsElapsed, setWsElapsed] = useState<number>(0);
  const wsTurnStartRef = useRef<number>(0);

  useEffect(() => {
    if (wsStatus !== "generating") return;
    const id = setInterval(() => {
      setWsElapsed(Math.floor((Date.now() - wsTurnStartRef.current) / 1000));
    }, 250);
    return () => clearInterval(id);
  }, [wsStatus]);


  // Esc closes the pop-out overlay.
  useEffect(() => {
    if (popOut === null) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setPopOut(null);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [popOut]);

  // Tier 2.5 reconnect: when the tab becomes visible again and the WS
  // is dead but we have a remembered run_dir for this scope, try to
  // reconnect with `resume: true` so the agent picks up from disk
  // state. Tab-switching is the most common cause of unintended drops
  // (browser timer throttling → tunnel idle timeout → WS reaped).
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== "visible") return;
      const ws = wsRef.current;
      const wsAlive = ws && ws.readyState <= WebSocket.OPEN;
      if (wsAlive) return;
      const runId = runIdRef.current;
      if (!runId) return;
      // Only reconnect from the terminal states startSession will accept.
      // If we're mid-generating (very unlikely with a dead ws but…), skip.
      if (wsStatus === "generating" || wsStatus === "connecting") return;
      startSession({ resumeRunId: runId });
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsStatus]);

  // Auto-expand draft on first draft content arrival — but only the
  // very first time the workspace has ever shown a draft on this device.
  // The flag below latches in localStorage; subsequent mounts let the
  // PanelGroup's autoSaveId restore whatever the user has chosen instead
  // of overriding it every time a chapter loads.
  useEffect(() => {
    if (!draft) return;
    if (draftAutoExpandedRef.current) return;
    if (localStorage.getItem(DRAFT_AUTO_EXPANDED_KEY)) {
      draftAutoExpandedRef.current = true;
      return;
    }
    draftAutoExpandedRef.current = true;
    try {
      localStorage.setItem(DRAFT_AUTO_EXPANDED_KEY, "1");
    } catch {}
    panelGroupRef.current?.setLayout([3, 48.5, 48.5]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  // On mount, fetch any locked output for this scope and seed the draft
  // pane in read mode. Era → /chapters/<era>; themes → /themes/latest.
  // Both 404 silently — no prior output is the common case.
  useEffect(() => {
    let cancelled = false;
    const url =
      scope.kind === "era"
        ? `${apiBase}/chapters/${encodeURIComponent(scope.era)}`
        : `${apiBase}/themes/latest`;
    fetch(url, { headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) return null;
        return r.json() as Promise<{ content: string }>;
      })
      .then((data) => {
        if (cancelled || !data?.content) return;
        // Populate the draft pane so the user can read what's already
        // locked, but leave phase at "pre-gen" so the prompter stays
        // active — clicking Start kicks off a fresh session that will
        // overwrite this view with new content.
        setDraft(data.content);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, scope]);

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

  function startSession(opts: { resumeRunId?: string } = {}) {
    if (wsStatus !== "idle" && wsStatus !== "done" && wsStatus !== "error") return;
    const resuming = !!opts.resumeRunId;
    if (!resuming) {
      // Fresh session: clear all state. Resume keeps the existing draft
      // visible while the agent rehydrates from disk.
      setLog([]);
      setDraft("");
    }
    setReplyText("");
    draftAutoExpandedRef.current = false;
    setSpawned(null);
    setCost(0);
    setError("");
    setFinalized(null);
    setHighlightDate("");
    narrationBufRef.current = "";
    sawFirstAwaitingRef.current = false;
    wsTurnStartRef.current = Date.now();
    setWsElapsed(0);
    setPhase("generating");
    setWsStatus("connecting");

    const wsPath = scope.kind === "era" ? "/session" : "/themes-curate";
    // Auth + session travel in the first message body, not the URL —
    // keeps tokens out of access logs / browser history / proxy logs.
    const ws = new WebSocket(`${wsBase}${wsPath}`);
    wsRef.current = ws;

    // Keep-alive: ping every 25s so the connection survives idle
    // intermediate timeouts (Cloudflare tunnel, browser App Nap, etc.)
    // when the agent is silently processing a long prompt.
    let pingTimer: ReturnType<typeof setInterval> | null = null;

    ws.onopen = () => {
      const session = getSession() || "";
      const token = getAuthToken() || "";
      const base: Record<string, unknown> = { type: "start", session, token, model };
      if (resuming) {
        base.resume = true;
        base.run_id = opts.resumeRunId;
      }
      // Workflow-specific fields aren't needed on resume — server uses
      // what's already in the run_dir.
      const startMsg =
        scope.kind === "era"
          ? { ...base, era: scope.era, future }
          : { ...base, top_n: topN };
      ws.send(JSON.stringify(startMsg));
      pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 25_000);
    };

    ws.onmessage = (ev) => {
      let payload: any;
      try {
        payload = JSON.parse(ev.data);
      } catch {
        return;
      }
      const t = payload.type;
      if (t === "pong") {
        return; // keep-alive ack; nothing to render
      }
      if (t === "spawned") {
        const info = payload as SpawnedInfo;
        setSpawned(info);
        // Persist run_dir for resume on reconnect.
        if (info.run_dir) setRunId(info.run_dir);
        const summary =
          scope.kind === "era"
            ? `reading ${info.notes ?? 0} notes` +
              ((info.prior_chapters ?? 0) > 0
                ? ` + ${info.prior_chapters} prior chapter${info.prior_chapters === 1 ? "" : "s"}`
                : "") +
              ((info.future_chapters ?? 0) > 0
                ? ` + ${info.future_chapters} future chapter${info.future_chapters === 1 ? "" : "s"}`
                : "")
            : `reading top-${info.top_n ?? topN} per era`;
        setLog((l) => [...l, { kind: "status", text: summary }]);
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
        // Locked — no point resuming this run after disconnect.
        setRunId(null);
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
      if (pingTimer !== null) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      flushNarration();
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
    if (collapsed.notes) {
      panelRefs.current.notes?.expand();
    }
  }

  function togglePaneCollapse(id: PaneId) {
    // Compute target collapsed state synchronously, then build a layout
    // that splits 100% across all panes ourselves. Library defaults can
    // squeeze a non-toggled pane down to collapsedSize without firing
    // onCollapse, leaving the UI showing expanded content in a 3% sliver.
    const COLLAPSED = 3;
    const next: Record<PaneId, boolean> = { ...collapsed, [id]: !collapsed[id] };
    const expanded = PANE_ORDER.filter((p) => !next[p]);
    const share = expanded.length
      ? (100 - COLLAPSED * (PANE_ORDER.length - expanded.length)) /
        expanded.length
      : 0;
    const sizes = PANE_ORDER.map((p) => (next[p] ? COLLAPSED : share));
    panelGroupRef.current?.setLayout(sizes);
  }

  // ---- Render helpers ----
  const sessionLive =
    wsStatus === "connecting" ||
    wsStatus === "generating" ||
    wsStatus === "awaiting_reply";
  // Era flow needs a draft on disk (output.md) before lock — Finalize
  // promotes that file to chapters/. Themes writes themes.md only on
  // /lock itself, so there's no pre-lock draft to require.
  const canFinalize =
    phase === "iterating" &&
    wsStatus === "awaiting_reply" &&
    (scope.kind === "themes" || !!draft);

  const promptStatus: "pre-gen" | "generating" | "awaiting_reply" | "finalized" =
    phase === "finalized"
      ? "finalized"
      : phase === "pre-gen"
        ? "pre-gen"
        : wsStatus === "awaiting_reply"
          ? "awaiting_reply"
          : "generating";

  // Called as `{renderPrompter()}` rather than `<Prompter />`. Declaring this
  // as a component would give it a fresh function identity on every render of
  // ChatWorkspace, so React's reconciler would unmount/remount the textarea
  // on every keystroke — focus drops after the first character. As a plain
  // function it inlines into the parent's JSX tree and the textarea persists.
  function renderPrompter() {
    const enabled = promptStatus === "awaiting_reply";
    const buttonEnabled =
      promptStatus === "pre-gen" ||
      (promptStatus === "awaiting_reply" && replyText.trim().length > 0);
    const placeholder =
      promptStatus === "pre-gen"
        ? scope.kind === "era"
          ? "Press ▶ to start drafting this chapter. The agent reads notes, drafts, then opens the chat."
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
          name="reply"
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

  function renderChatBody() {
    return (
      <div className="flex h-full flex-col min-h-0">
        <div
          ref={chatLogRef}
          className="flex-1 bg-white p-4 font-sans text-sm text-stone-800 overflow-auto min-h-0"
        >
          {log.length === 0 && phase === "pre-gen" && (
            <span className="text-stone-400 text-xs">
              Press ▶ below to start the session.
            </span>
          )}
          {wsStatus === "generating" &&
            !log.some((it) => it.kind === "narration") &&
            (() => {
              const inputChars = spawned?.input_chars ?? 0;
              const totalTok = Math.round(inputChars / 4);
              if (totalTok <= 0) return null;
              // Conservative prompt-processing rate (~1.5K tok/s) capped
              // at 90% so we never claim "almost done" before the model
              // actually emits.
              const progressTok = Math.min(
                Math.round(wsElapsed * 1500),
                Math.round(totalTok * 0.9),
              );
              const fmt = (n: number) =>
                n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
              return (
                <div className="my-2 flex items-center gap-2 text-stone-500">
                  <span className="inline-block w-1.5 h-1.5 rounded-full bg-stone-400 animate-pulse" />
                  <span className="text-stone-400">
                    <span className="tabular-nums">{fmt(progressTok)}</span>
                    {" / "}
                    <span className="tabular-nums">{fmt(totalTok)}</span> tokens
                  </span>
                </div>
              );
            })()}
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
        <div className="border-t border-stone-200 bg-white p-3">
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
            {phase === "pre-gen" && (
              <span className="text-stone-400">not started</span>
            )}
          </div>
          {renderPrompter()}
        </div>
      </div>
    );
  }

  function renderDraftBody() {
    return (
      <article
        className={
          "h-full overflow-auto bg-white p-6 font-serif text-[16px] leading-[1.7] text-stone-900 " +
          (phase === "finalized" ? "ring-1 ring-inset ring-emerald-200" : "")
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
    );
  }

  function renderNotesBody() {
    return (
      <NotesTimeline
        notes={notes}
        loading={notesLoading}
        highlightDate={highlightDate}
        emptyHint={
          scope.kind === "era" ? "no notes in this era" : "no notes available"
        }
      />
    );
  }

  function renderPaneContent(id: PaneId) {
    if (id === "chat") return renderChatBody();
    if (id === "draft") return renderDraftBody();
    return renderNotesBody();
  }

  function PaneHeader({ id, popped = false }: { id: PaneId; popped?: boolean }) {
    const isCollapsed = collapsed[id];
    return (
      <div className="flex items-center justify-between gap-2 border-b border-stone-200 bg-stone-50 px-2 py-1 shrink-0">
        <span className="font-sans text-[11px] uppercase tracking-wider text-stone-500">
          {PANE_TITLES[id]}
          {id === "chat" && cost > 0 && (
            <span className="ml-1 normal-case tracking-normal text-stone-400">
              (${cost.toFixed(4)})
            </span>
          )}
          {id === "draft" && draft && (
            <span className="ml-1 normal-case tracking-normal text-stone-400">
              ({draft.length.toLocaleString()} ch)
            </span>
          )}
          {id === "notes" && notes.length > 0 && (
            <span className="ml-1 normal-case tracking-normal text-stone-400">
              ({notes.length})
            </span>
          )}
        </span>
        <div className="flex items-center gap-0.5">
          <button
            onClick={() => setPopOut(popped ? null : id)}
            className="rounded px-1.5 py-0.5 text-stone-400 hover:bg-stone-200 hover:text-stone-700"
            title={popped ? "exit full-screen (Esc)" : "open full-screen"}
            aria-label={popped ? "exit full-screen" : "open full-screen"}
          >
            {popped ? "×" : "⛶"}
          </button>
          {!popped && (
            <button
              onClick={() => togglePaneCollapse(id)}
              className="rounded px-1.5 py-0.5 text-stone-400 hover:bg-stone-200 hover:text-stone-700"
              title={isCollapsed ? "expand pane" : "collapse pane"}
              aria-label={isCollapsed ? "expand pane" : "collapse pane"}
            >
              {isCollapsed ? "+" : "−"}
            </button>
          )}
        </div>
      </div>
    );
  }

  function CollapsedRail({ id }: { id: PaneId }) {
    return (
      <button
        onClick={() => togglePaneCollapse(id)}
        className="flex h-full w-full flex-col items-center justify-start gap-2 bg-stone-50 py-3 text-stone-500 hover:bg-stone-100 hover:text-stone-700"
        title="expand pane"
        aria-label={`expand ${PANE_TITLES[id]} pane`}
      >
        <span className="text-stone-400 text-xs">+</span>
        <span
          className="font-sans text-[10px] uppercase tracking-wider"
          style={{ writingMode: "vertical-rl" }}
        >
          {PANE_TITLES[id]}
        </span>
      </button>
    );
  }

  return (
    <div className="mx-auto max-w-[120rem] px-6 py-4">
      {/* ---- Title / control bar ---- */}
      <div className="mb-4 flex items-center gap-3 flex-wrap">
        {titleNode}
        <div className="ml-auto flex items-center gap-2 flex-wrap">
          <select
            name="model"
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
                name="future"
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
                name="top-n"
                min={3}
                max={20}
                value={topN}
                onChange={(e) => setTopN(parseInt(e.target.value) || 5)}
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
                : wsStatus !== "awaiting_reply"
                  ? "wait for the agent"
                  : scope.kind === "era" && !draft
                    ? "no draft yet"
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

      {finalized && (
        <div className="mb-3 rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          Locked to <span className="font-mono">{finalized.location}</span> ·{" "}
          {finalized.words} words
          {finalized.overwritten && " · overwrote previous"}
        </div>
      )}

      {/* ---- 3-pane workspace ---- */}
      <div className="h-[80vh] rounded border border-stone-200 overflow-hidden bg-white">
        <PanelGroup
          direction="horizontal"
          autoSaveId="workspace_panels_v2"
          ref={panelGroupRef}
        >
          {PANE_ORDER.map((id, idx) => (
            <Fragment key={id}>
              <Panel
                id={id}
                defaultSize={PANE_DEFAULT_SIZE[id]}
                minSize={12}
                collapsible
                collapsedSize={3}
                onCollapse={() =>
                  setCollapsed((c) => ({ ...c, [id]: true }))
                }
                onExpand={() =>
                  setCollapsed((c) => ({ ...c, [id]: false }))
                }
                ref={(el) => {
                  panelRefs.current[id] = el;
                }}
                className="flex flex-col"
              >
                {collapsed[id] ? (
                  <CollapsedRail id={id} />
                ) : popOut === id ? (
                  <>
                    <PaneHeader id={id} />
                    <button
                      onClick={() => setPopOut(null)}
                      className="flex flex-1 items-center justify-center text-xs text-stone-400 hover:text-stone-700 hover:bg-stone-50"
                    >
                      (popped out — click to restore)
                    </button>
                  </>
                ) : (
                  <>
                    <PaneHeader id={id} />
                    <div className="flex-1 min-h-0 overflow-hidden">
                      {renderPaneContent(id)}
                    </div>
                  </>
                )}
              </Panel>
              {idx < PANE_ORDER.length - 1 && (
                <PanelResizeHandle className="w-1 bg-stone-100 hover:bg-stone-300 transition-colors" />
              )}
            </Fragment>
          ))}
        </PanelGroup>
      </div>

      {/* ---- Pop-out overlay ---- */}
      {popOut !== null && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-stone-900/40 p-4"
          onClick={(e) => {
            if (e.target === e.currentTarget) setPopOut(null);
          }}
        >
          <div className="flex h-full max-h-[95vh] w-full max-w-[95vw] flex-col rounded border border-stone-200 bg-white shadow-2xl">
            <PaneHeader id={popOut} popped />
            <div className="flex-1 min-h-0 overflow-hidden">
              {renderPaneContent(popOut)}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
