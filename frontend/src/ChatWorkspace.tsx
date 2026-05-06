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
  // Model selected for new sessions. Owned by App.tsx so it survives
  // chapter remounts and is shared across views.
  model: string;
  // Available model choices. Scope-specific filtering (e.g. no opus-4.7
  // for themes) is handled by the caller.
  models: readonly string[];
  onModelChange: (m: string) => void;
  // Called when the server emits `finalized` — used by the eras tab to
  // refetch the era list so chapter date ranges update after a draft is
  // promoted.
  onFinalized?: (info: FinalizedInfo) => void;
  // Optional content rendered centered in the Draft pane header — used
  // by the chapters tab to label the pane with the active chapter's
  // name. Themes leaves it null.
  draftHeaderSlot?: ReactNode;
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
// Defaults baked into the protocol — these used to be exposed as user-
// adjustable controls in the now-removed control bar but never had a
// real reason to vary per-session.
const THEMES_TOP_N = 7;
const ERA_INCLUDE_FUTURE = true;

export function ChatWorkspace({
  apiBase,
  wsBase,
  scope,
  model,
  models,
  onModelChange,
  onFinalized,
  draftHeaderSlot,
}: ChatWorkspaceProps) {
  // Resolve model: if the current selection isn't in this scope's allowed
  // list (e.g. opus-4.7 on themes), fall back to the first available.
  const effectiveModel = models.includes(model) ? model : models[0];

  const [phase, setPhase] = useState<Phase>("pre-gen");

  const [notes, setNotes] = useState<Note[]>([]);
  const [notesLoading, setNotesLoading] = useState<boolean>(false);
  const [notesError, setNotesError] = useState<string>("");

  const [log, setLog] = useState<LogItem[]>([]);
  const [draft, setDraft] = useState<string>("");
  const replyRef = useRef<HTMLTextAreaElement | null>(null);
  const [replyHasText, setReplyHasText] = useState(false);
  const [wsStatus, setWsStatus] = useState<WSStatus>("idle");
  const [spawned, setSpawned] = useState<SpawnedInfo | null>(null);
  const [cost, setCost] = useState<number>(0);
  const [error, setError] = useState<string>("");
  const [canonicalDraft, setCanonicalDraft] = useState<string>("");
  const draftViewKey = `draftView:${scope.kind}`;
  const [draftView, _setDraftView] = useState<"canonical" | "working">(() => {
    const stored = sessionStorage.getItem(draftViewKey);
    return stored === "working" ? "working" : "canonical";
  });
  const setDraftView = (v: "canonical" | "working") => {
    _setDraftView(v);
    sessionStorage.setItem(draftViewKey, v);
  };
  const [sessionStartedOnce, setSessionStartedOnce] = useState(false);
  const [highlightDate, setHighlightDate] = useState<string>("");
  const [highlightContext, setHighlightContext] = useState<string>("");

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
  const autoSwitchedToDraftRef = useRef<boolean>(false);

  // Run ID of the current session — only lives in memory. Server is the
  // source of truth for whether a session exists; we just need the ID
  // to pass on WS resume messages.
  const runIdRef = useRef<string | null>(null);

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
  // Tracks whether this connection was a resume. The progress estimator
  // is anchored to a fresh kickoff time; on resume the agent may already
  // be mid-turn or done, so showing a 0-anchored counter would mislead
  // (and would mismatch any other tab still attached to the same session).
  const resumedRef = useRef<boolean>(false);
  // Last pong receipt timestamp. The ping loop closes the WS if pongs
  // stop arriving so the visibilitychange handler can reconnect quickly,
  // rather than waiting for the browser to notice TCP-level death.
  const lastPongRef = useRef<number>(0);

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

  // Auto-attach: on mount and on tab-becoming-visible, ask the server
  // if there's a live session for this corpus+kind. If yes, reconnect.
  // Server is the sole source of truth — no client-side run_id storage.
  function checkAndResume() {
    const ws = wsRef.current;
    if (ws && ws.readyState <= WebSocket.OPEN) return; // already connected
    const params = new URLSearchParams({ kind: scope.kind });
    if (scope.kind === "era") params.set("era", scope.era);
    fetch(
      `${apiBase}/session/active?${params}`,
      { headers: authHeaders() },
    )
      .then((r) => (r.ok ? r.json() : { active: false }))
      .then((data) => {
        if (data?.active && data.run_id) {
          startSession({ resumeRunId: data.run_id });
        }
      })
      .catch(() => {});
  }

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== "visible") return;
      checkAndResume();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsStatus]);

  // Also check on mount (visibilitychange doesn't fire on initial load).
  useEffect(() => {
    checkAndResume();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-expand draft pane on first draft content each session. The ref
  // latches after the first expansion so subsequent draft_update messages
  // don't override the user's manual layout adjustments mid-session.
  useEffect(() => {
    if (!draft) return;
    if (draftAutoExpandedRef.current) return;
    draftAutoExpandedRef.current = true;
    if (collapsed.draft) {
      panelGroupRef.current?.setLayout([33, 34, 33]);
    }
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
        : scope.kind === "preface"
          ? `${apiBase}/preface/latest`
          : scope.kind === "commonplace"
            ? `${apiBase}/commonplace/latest`
            : `${apiBase}/themes/latest`;
    fetch(url, { headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) return null;
        return r.json() as Promise<{ content: string }>;
      })
      .then((data) => {
        if (cancelled || !data?.content) return;
        setCanonicalDraft(data.content);
        // Only seed draft on mount — don't clobber a working draft from
        // a live session if this effect re-fires (e.g. scope ref change).
        setDraft((prev) => prev || data.content);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, scope]);

  // ---- Notes fetch ----
  // Re-runs only when scope changes (which only happens via remount via key).
  useEffect(() => {
    let cancelled = false;
    setNotesLoading(true);
    setNotes([]);
    setNotesError("");
    const url =
      scope.kind === "era"
        ? `${apiBase}/notes?era=${encodeURIComponent(scope.era)}`
        : `${apiBase}/notes/all?top_n=${THEMES_TOP_N}`;
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
  }, [apiBase, scope]);

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

  // ---- Cleanup: close WS on unmount (detach only, no stop). ----
  // Unmount happens on Eras↔Themes toggle and era switches — the session
  // should keep running (Tier 3). The server sees the disconnect as a
  // detach. When the user comes back, the mount-effect auto-attaches.
  // Explicit stop is only sent via the Stop button (sendStop).
  useEffect(() => {
    return () => {
      const ws = wsRef.current;
      if (ws && ws.readyState <= WebSocket.OPEN) {
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
    // Synchronous guard against double-fire in dev (StrictMode runs effects
    // twice before state updates propagate, so the wsStatus check above
    // can pass twice for the same intent). wsRef.current is set
    // synchronously inside this function — checking it catches the dupe.
    if (wsRef.current && wsRef.current.readyState !== WebSocket.CLOSED) return;
    const resuming = !!opts.resumeRunId;
    setLog([]);
    if (!resuming) {
      setDraft("");
      setDraftView("canonical");
      setSessionStartedOnce(true);
      autoSwitchedToDraftRef.current = false;
    }
    if (replyRef.current) replyRef.current.value = "";
    setReplyHasText(false);
    draftAutoExpandedRef.current = false;
    setSpawned(null);
    setCost(0);
    setError("");
    setHighlightDate("");
    setHighlightContext("");
    narrationBufRef.current = "";
    sawFirstAwaitingRef.current = false;
    wsTurnStartRef.current = Date.now();
    resumedRef.current = resuming;
    lastPongRef.current = Date.now();
    setWsElapsed(0);
    setPhase("generating");
    setWsStatus("connecting");

    const wsPath =
      scope.kind === "era" ? "/session"
        : scope.kind === "preface" ? "/preface-session"
          : scope.kind === "commonplace" ? "/commonplace-session"
            : "/themes-curate";
    // Auth + session travel in the first message body, not the URL —
    // keeps tokens out of access logs / browser history / proxy logs.
    const ws = new WebSocket(`${wsBase}${wsPath}`);
    wsRef.current = ws;

    // Keep-alive: ping every 10s so idle intermediate timeouts (Cloudflare
    // tunnel, browser App Nap) don't reap the WS, AND so we notice a dead
    // connection quickly — if no pong arrives within ~25s we close the WS
    // ourselves so the visibilitychange handler can reattach. Without this
    // the browser only flips readyState→CLOSED after its own (slow) TCP
    // timeout, which can take a minute or more.
    const PING_MS = 10_000;
    const PONG_DEADLINE_MS = 25_000;
    let pingTimer: ReturnType<typeof setInterval> | null = null;

    ws.onopen = () => {
      const session = getSession() || "";
      const token = getAuthToken() || "";
      const base: Record<string, unknown> = { type: "start", session, token, model: effectiveModel };
      if (resuming) {
        base.resume = true;
        base.run_id = opts.resumeRunId;
      }
      // Workflow-specific fields aren't needed on resume — server uses
      // what's already in the run_dir.
      const startMsg =
        scope.kind === "era"
          ? { ...base, era: scope.era, future: ERA_INCLUDE_FUTURE }
          : scope.kind === "themes"
            ? { ...base, top_n: THEMES_TOP_N }
            : base;
      ws.send(JSON.stringify(startMsg));
      lastPongRef.current = Date.now();
      pingTimer = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN) return;
        if (Date.now() - lastPongRef.current > PONG_DEADLINE_MS) {
          // Pongs stopped arriving — connection is dead in everything but
          // name. Force-close so onclose fires and visibility handler can
          // reconnect.
          try { ws.close(); } catch {}
          return;
        }
        ws.send(JSON.stringify({ type: "ping" }));
      }, PING_MS);
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
        lastPongRef.current = Date.now();
        return; // keep-alive ack; nothing to render
      }
      if (t === "spawned") {
        const info = payload as SpawnedInfo;
        setSpawned(info);
        // The agent is now live — promote from "connecting" so the
        // progress counter and elapsed timer activate immediately,
        // rather than waiting for the SDK's delayed "generating" event
        // (which can lag during extended thinking).
        setWsStatus((s) => (s === "connecting" ? "generating" : s));
        // Persist run_dir for resume on reconnect.
        if (info.run_dir) {
          runIdRef.current = info.run_dir;
          console.info(`[biographer] session: ${info.run_dir}`);
        }
        const summary =
          scope.kind === "era"
            ? `reading ${info.notes ?? 0} notes` +
              ((info.prior_chapters ?? 0) > 0
                ? ` + ${info.prior_chapters} prior chapter${info.prior_chapters === 1 ? "" : "s"}`
                : "") +
              ((info.future_chapters ?? 0) > 0
                ? ` + ${info.future_chapters} future chapter${info.future_chapters === 1 ? "" : "s"}`
                : "")
            : scope.kind === "preface"
              ? `reading all chapters + themes + cited source notes`
              : scope.kind === "commonplace"
                ? `reading ${info.sampled_count ?? 0} unseen notes (${info.seen_before ?? 0} already processed)`
                : `reading top-${info.top_n ?? THEMES_TOP_N} per era`;
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
          ((scope.kind === "era" || scope.kind === "preface") && payload.kind === "output") ||
          (scope.kind === "themes" &&
            (payload.kind === "output" || payload.kind === "themes")) ||
          (scope.kind === "commonplace" &&
            (payload.kind === "output" || payload.kind === "commonplace"));
        if (interesting) {
          setDraft(payload.content);
          if (!autoSwitchedToDraftRef.current) {
            autoSwitchedToDraftRef.current = true;
            setDraftView("working");
          }
        }
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
        setDraft(info.content);
        setCanonicalDraft(info.content);
        setDraftView("working");
        setPhase("finalized");
        // Locked — no point resuming this run after disconnect.
        runIdRef.current = null;
        if (onFinalized) onFinalized(info);
      } else if (t === "user_message") {
        flushNarration();
        setLog((l) => [...l, { kind: "user", text: payload.text }]);
      } else if (t === "done") {
        flushNarration();
        setWsStatus("done");
        runIdRef.current = null;
        if (typeof payload.cost_usd === "number") setCost(payload.cost_usd);
      } else if (t === "error") {
        setError(payload.message ?? "unknown error");
        setWsStatus("error");
        runIdRef.current = null;
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
    const ta = replyRef.current;
    const text = (ta?.value ?? "").trim();
    if (!ws || !text || wsStatus !== "awaiting_reply") return;
    ws.send(JSON.stringify({ type: "reply", text }));
    if (ta) ta.value = "";
    setReplyHasText(false);
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
    if (ws) {
      if (ws.readyState === WebSocket.OPEN) {
        // Send stop and let the server close the WS after handling it.
        // If we close ourselves, the close frame races the stop message
        // and the server can see the disconnect first — treating it as
        // a Tier 3 detach (session keeps running) rather than an
        // explicit shutdown.
        ws.send(JSON.stringify({ type: "stop" }));
      }
      // Detach handlers so any in-flight messages or the eventual
      // server-initiated close don't race with our state reset below.
      // (Without this, ws.onclose would flip wsStatus back to "done"
      // milliseconds after we set it to "idle".)
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
    }
    wsRef.current = null;
    // Clear runId regardless: even if the stop message gets dropped, we
    // don't want the next mount-effect to auto-attach to the abandoned
    // session. GC will reap it after 30 min.
    runIdRef.current = null;
    // Reset only the prompter / session-status surface back to its OG
    // state — the chat log and the draft stay so the user can review
    // the run after stopping. cost is preserved so the running tally
    // survives. (Hitting ▶ again will start a fresh session that
    // appends to the existing log.)
    setWsStatus("idle");
    setPhase("pre-gen");
    setSpawned(null);
    if (replyRef.current) replyRef.current.value = "";
    setReplyHasText(false);
    setError("");
    setWsElapsed(0);
    sawFirstAwaitingRef.current = false;
    resumedRef.current = false;
    narrationBufRef.current = "";
  }

  function handleCiteClick(dateKey: string, context?: string) {
    setHighlightDate(dateKey);
    setHighlightContext(context ?? "");
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
  // Enable finalize only when the draft pane shows new content that
  // differs from the canonical version. Both workflows now write a
  // preliminary draft during generation (eras → output.md, themes →
  // themes.md after round-1), so this check is uniform.
  const canFinalize =
    phase === "iterating" &&
    wsStatus === "awaiting_reply" &&
    !!draft &&
    draft !== canonicalDraft;

  const promptStatus: "pre-gen" | "generating" | "awaiting_reply" | "finalized" =
    phase === "finalized"
      ? "finalized"
      : phase === "pre-gen"
        ? "pre-gen"
        : wsStatus === "awaiting_reply"
          ? "awaiting_reply"
          : "generating";

  const displayedDraft =
    draftView === "canonical" ? canonicalDraft : draft;
  const showDraftToggle =
    !!canonicalDraft &&
    !!draft &&
    canonicalDraft !== draft &&
    sessionStartedOnce;

  // Called as `{renderPrompter()}` rather than `<Prompter />`. Declaring this
  // as a component would give it a fresh function identity on every render of
  // ChatWorkspace, so React's reconciler would unmount/remount the textarea
  // on every keystroke — focus drops after the first character. As a plain
  // function it inlines into the parent's JSX tree and the textarea persists.
  function renderPrompter() {
    const enabled = promptStatus === "awaiting_reply";
    const buttonEnabled =
      promptStatus === "pre-gen" ||
      (promptStatus === "awaiting_reply" && replyHasText);
    const placeholder =
      promptStatus === "pre-gen"
        ? scope.kind === "era"
          ? "Press ▶ to start drafting this chapter. The agent reads notes then proposes ideas for you to respond to."
          : scope.kind === "preface"
            ? "Press ▶ to start. The agent reads all chapters, then proposes a structure and key quotes for you to approve before drafting."
            : scope.kind === "commonplace"
              ? "Press ▶ to find the good stuff. The agent reads unseen notes and extracts standout passages."
              : "Press ▶ to surface top recurring threads. The agent reads notes then proposes ideas for you to respond to."
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
          ref={replyRef}
          name="reply"
          className={
            "block w-full resize-none rounded border px-3 pt-2 pb-11 text-sm font-sans transition-colors disabled:text-stone-400 " +
            borderClass
          }
          rows={3}
          placeholder={placeholder}
          disabled={!enabled}
          onChange={(e) => {
            const has = e.target.value.trim().length > 0;
            if (has !== replyHasText) setReplyHasText(has);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && (replyRef.current?.value ?? "").trim()) {
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
              const fmtTime = (s: number) =>
                s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
              // Fresh session with known input size: show token progress bar.
              if (!resumedRef.current && totalTok > 0) {
                const progressTok = Math.min(
                  Math.round(wsElapsed * 1500),
                  Math.round(totalTok * 0.9),
                );
                const fmt = (n: number) =>
                  n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
                return (
                  <div className="my-2 space-y-1">
                    <div className="flex items-center gap-2 text-stone-500">
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-stone-400 animate-pulse" />
                      <span className="text-stone-400">
                        <span className="tabular-nums">{fmt(progressTok)}</span>
                        {" / "}
                        <span className="tabular-nums">{fmt(totalTok)}</span> tokens
                        <span className="ml-2 text-stone-300">{fmtTime(wsElapsed)}</span>
                      </span>
                    </div>
                    {wsElapsed >= 120 && (
                      <div className="text-xs text-amber-600">
                        Taking longer than expected — session may be stuck.
                      </div>
                    )}
                  </div>
                );
              }
              // Resumed session or no spawned info: show generic elapsed.
              return (
                <div className="my-2 space-y-1">
                  <div className="flex items-center gap-2 text-stone-500">
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-stone-400 animate-pulse" />
                    <span className="text-stone-400">
                      {resumedRef.current ? "reconnected — " : ""}agent working… {fmtTime(wsElapsed)}
                    </span>
                  </div>
                  {wsElapsed >= 120 && (
                    <div className="text-xs text-amber-600">
                      Taking longer than expected — session may be stuck.
                    </div>
                  )}
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
            {cost > 0 && (
              <span className="ml-auto font-mono text-[11px] tabular-nums text-stone-400">
                ${cost.toFixed(4)}
              </span>
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
          "h-full overflow-auto bg-white font-serif text-[16px] leading-[1.7] text-stone-900 " +
          (phase === "finalized" ? "ring-1 ring-inset ring-emerald-200" : "")
        }
      >
        {draftHeaderSlot && (
          <div className="sticky top-0 z-10 bg-white">
            <div className="px-6 pt-2 pb-3 text-center font-serif text-lg text-stone-800">
              {draftHeaderSlot}
            </div>
            <div className="mx-[11px] h-px bg-[linear-gradient(to_right,transparent_0%,#d6d3d1_6%,#d6d3d1_94%,transparent_100%)]" />
          </div>
        )}
        <div className="px-6 pb-6 pt-3">
          {displayedDraft ? (
            <Markdown
              content={displayedDraft}
              variant="chapter"
              onCiteClick={handleCiteClick}
            />
          ) : (
            <span className="font-sans text-sm text-stone-400">
              (no draft content yet)
            </span>
          )}
        </div>
      </article>
    );
  }

  function renderNotesBody() {
    return (
      <NotesTimeline
        notes={notes}
        loading={notesLoading}
        highlightDate={highlightDate}
        highlightContext={highlightContext}
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
      <div className="flex items-center gap-2 border-b border-stone-200 bg-stone-50 px-2 py-1 shrink-0">
        <span className="font-sans text-[11px] uppercase tracking-wider text-stone-500 shrink-0">
          {PANE_TITLES[id]}
          {id === "draft" && displayedDraft && (
            <span className="ml-1 normal-case tracking-normal text-stone-400">
              ({displayedDraft.split(/\s+/).filter(Boolean).length.toLocaleString()} words)
            </span>
          )}
          {id === "notes" && notes.length > 0 && (
            <span className="ml-1 normal-case tracking-normal text-stone-400">
              ({notes.length})
            </span>
          )}
        </span>
        {id === "chat" && (
          <select
            className="appearance-none bg-transparent border-0 border-b border-dotted border-stone-300 hover:border-stone-500 focus:border-stone-700 focus:outline-none pl-1 pr-5 py-0.5 font-sans text-[10px] text-stone-400 hover:text-stone-600 cursor-pointer tabular-nums"
            style={{
              backgroundImage:
                "url(\"data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 10 6'%3E%3Cpath fill='none' stroke='%23a8a29e' stroke-width='1.2' d='M1 1l4 4 4-4'/%3E%3C/svg%3E\")",
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 2px center",
            }}
            value={effectiveModel}
            onChange={(e) => onModelChange(e.target.value)}
            title="Model used for new sessions"
          >
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        )}
        {id === "draft" && showDraftToggle && (
          <div className="flex items-center gap-1 text-[10px] font-sans">
            <button
              onClick={() => setDraftView("canonical")}
              className={
                draftView === "canonical"
                  ? "text-stone-600 font-medium"
                  : "text-stone-400 hover:text-stone-600"
              }
            >
              canonical
            </button>
            <span className="text-stone-300">/</span>
            <button
              onClick={() => setDraftView("working")}
              className={
                draftView === "working"
                  ? "text-stone-600 font-medium"
                  : "text-stone-400 hover:text-stone-600"
              }
            >
              working
            </button>
          </div>
        )}
        <div className="flex items-center gap-1.5 ml-auto">
          {id === "chat" && sessionLive && (
            <button
              onClick={sendStop}
              className="rounded px-1.5 py-0.5 text-red-400 hover:bg-red-50 hover:text-red-600"
              title="stop Claude session"
              aria-label="stop Claude session"
            >
              <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-2.5 h-2.5"><rect x="0.75" y="0.75" width="8.5" height="8.5" rx="1" /></svg>
            </button>
          )}
          {id === "draft" && (
            <button
              onClick={sendFinalize}
              disabled={!canFinalize}
              className={
                "rounded px-1.5 py-0.5 disabled:opacity-30 disabled:cursor-not-allowed " +
                (phase === "finalized"
                  ? "text-emerald-600"
                  : "text-stone-400 hover:bg-emerald-50 hover:text-emerald-700")
              }
              title={
                phase === "finalized"
                  ? "already canonical"
                  : wsStatus !== "awaiting_reply"
                    ? "wait for the agent"
                    : scope.kind === "era" && !draft
                      ? "no draft yet"
                      : "mark draft as canonical"
              }
              aria-label="mark draft as canonical"
            >
              ✓
            </button>
          )}
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
    <div className="mx-auto w-full max-w-[120rem] px-6 py-4 flex-1 flex flex-col min-h-0">
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

      {/* ---- 3-pane workspace ---- */}
      <div className="flex-1 min-h-0 rounded border border-stone-200 overflow-hidden bg-white">
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
