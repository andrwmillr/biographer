import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Backend URL resolution: ?backend=... query param > VITE_BACKEND_URL build env > same-origin (vite dev proxy).
const _backendOverride = new URLSearchParams(location.search).get("backend") ?? undefined;
const API_BASE = _backendOverride ?? import.meta.env.VITE_BACKEND_URL ?? "";
const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;

// Multi-tenant session: opaque slug stored in localStorage, sent as
// X-Corpus-Session header. Drafting against the existing `_corpus/` is
// reserved for the legacy session "_andrew_legacy" (admin) — set it once in
// DevTools: localStorage.setItem('corpusSession', '_andrew_legacy').
const SESSION_KEY = "corpusSession";

function getSession(): string | null {
  return localStorage.getItem(SESSION_KEY);
}

function setSession(slug: string): void {
  localStorage.setItem(SESSION_KEY, slug);
}

function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}

function authHeaders(): Record<string, string> {
  const s = getSession();
  return s ? { "X-Corpus-Session": s } : {};
}

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
  // Strip any path prefix that ends in the run dir basename.
  if (runDir) {
    const tail = runDir.split("/").pop()!;
    const idx = p.indexOf(tail + "/");
    if (idx >= 0) return p.slice(idx + tail.length + 1);
  }
  // Otherwise just basename.
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

type Era = {
  name: string;
  start: string;
  end: string | null;
  note_count: number;
  has_chapter: boolean;
};

type Note = {
  rel: string;
  date: string;
  title: string;
  label: string;
  source: string;
  body: string;
  editor_note?: string;
};

type Bounds = { start: number; end: number };

function eraBounds(notes: Note[]): Bounds | null {
  if (!notes.length) return null;
  const start = new Date(notes[0].date).getTime();
  const end =
    notes.length > 1
      ? new Date(notes[notes.length - 1].date).getTime()
      : start + 86400000;
  return start < end ? { start, end } : null;
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

export default function App() {
  const [eras, setEras] = useState<Era[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [useFuture, setUseFuture] = useState<boolean>(false);
  const [model, setModel] = useState<string>("opus-4.7");
  const [error, setError] = useState<string>("");

  const [log, setLog] = useState<LogItem[]>([]);
  const [output, setOutput] = useState<string>("");
  const [reply, setReply] = useState<string>("");
  const [wsStatus, setWsStatus] = useState<
    "idle" | "connecting" | "generating" | "awaiting_reply" | "done" | "error"
  >("idle");
  const [wsRunDir, setWsRunDir] = useState<string>("");
  const [wsCost, setWsCost] = useState<number>(0);
  const [wsNotes, setWsNotes] = useState<number>(0);
  const [wsPrior, setWsPrior] = useState<number>(0);
  const [wsFuture, setWsFuture] = useState<number>(0);
  const [wsInputChars, setWsInputChars] = useState<number>(0);
  const [promoteState, setPromoteState] = useState<
    "idle" | "promoting" | "done" | "error"
  >("idle");
  const [promoteResult, setPromoteResult] = useState<{
    dst: string;
    words: number;
    overwritten: boolean;
  } | null>(null);
  const [rightView, setRightView] = useState<"conversation" | "chapter">(
    "conversation",
  );
  const [wsElapsed, setWsElapsed] = useState<number>(0);
  const [wsHasNarration, setWsHasNarration] = useState<boolean>(false);
  const wsTurnStartRef = useRef<number>(0);
  const wsRef = useRef<WebSocket | null>(null);
  const narrationBufRef = useRef<string>("");
  const convLogRef = useRef<HTMLDivElement | null>(null);

  // ---- Themes flow state ----
  const [viewMode, setViewMode] = useState<"eras" | "themes">("eras");
  const [themesTopN, setThemesTopN] = useState<number>(10);
  const [themesPhase, setThemesPhase] = useState<
    "idle" | "spinning" | "spin-done" | "curating" | "locked" | "error"
  >("idle");
  const [themesRunDir, setThemesRunDir] = useState<string>("");
  const [themesOutput, setThemesOutput] = useState<string>("");
  const [themesLog, setThemesLog] = useState<LogItem[]>([]);
  const [themesReply, setThemesReply] = useState<string>("");
  const [themesLocked, setThemesLocked] = useState<string>("");
  const [themesCost, setThemesCost] = useState<number>(0);
  const [themesStatus, setThemesStatus] = useState<
    "idle" | "generating" | "awaiting_reply" | "done" | "error"
  >("idle");
  const themesWsRef = useRef<WebSocket | null>(null);
  const themesNarrationBufRef = useRef<string>("");
  const themesLogRef = useRef<HTMLDivElement | null>(null);

  const [showNotes, setShowNotes] = useState<boolean>(false);
  const [notes, setNotes] = useState<Note[]>([]);
  const [notesLoading, setNotesLoading] = useState<boolean>(false);
  const [notesEra, setNotesEra] = useState<string>("");
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);
  const [notesScrollTop, setNotesScrollTop] = useState<number>(0);
  const [showTimeline, setShowTimeline] = useState<boolean>(true);
  const [notesOverlay, setNotesOverlay] = useState<boolean>(false);

  // ---- Multi-tenant corpus session ----
  type CorpusInfo = {
    slug: string;
    is_legacy: boolean;
    note_count: number;
    has_eras: boolean;
    eras: Array<{ name: string; start: string; end?: string }>;
  };
  const [corpusMode, setCorpusMode] = useState<
    "loading" | "import-notes" | "import-eras" | "ready" | "error"
  >("loading");
  const [corpusInfo, setCorpusInfo] = useState<CorpusInfo | null>(null);
  const [importError, setImportError] = useState<string>("");

  // Bootstrap: on mount, check session and route to the right mode.
  useEffect(() => {
    const slug = getSession();
    if (!slug) {
      setCorpusMode("import-notes");
      return;
    }
    fetch(`${API_BASE}/corpus`, { headers: authHeaders() })
      .then(async (r) => {
        if (r.status === 401) {
          clearSession();
          setCorpusMode("import-notes");
          return null;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        return (await r.json()) as CorpusInfo;
      })
      .then((info) => {
        if (!info) return;
        setCorpusInfo(info);
        if (!info.has_eras) setCorpusMode("import-eras");
        else setCorpusMode("ready");
      })
      .catch((err: Error) => {
        setError(`session check failed: ${err.message}`);
        setCorpusMode("error");
      });
  }, []);

  async function handleNotesUpload(file: File) {
    setImportError("");
    const formData = new FormData();
    formData.append("file", file);
    try {
      const resp = await fetch(`${API_BASE}/import/notes`, {
        method: "POST",
        body: formData,
      });
      if (!resp.ok)
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const data = await resp.json();
      setSession(data.slug);
      setCorpusInfo({
        slug: data.slug,
        is_legacy: false,
        note_count: data.note_count,
        has_eras: false,
        eras: [],
      });
      setCorpusMode("import-eras");
    } catch (err) {
      setImportError(`notes upload failed: ${(err as Error).message}`);
    }
  }

  async function handleErasUpload(file: File) {
    setImportError("");
    const formData = new FormData();
    formData.append("file", file);
    try {
      const resp = await fetch(`${API_BASE}/import/eras`, {
        method: "POST",
        headers: authHeaders(),
        body: formData,
      });
      if (!resp.ok)
        throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const c = await fetch(`${API_BASE}/corpus`, { headers: authHeaders() });
      const info = (await c.json()) as CorpusInfo;
      setCorpusInfo(info);
      setCorpusMode("ready");
    } catch (err) {
      setImportError(`eras upload failed: ${(err as Error).message}`);
    }
  }

  async function handleWipe() {
    setImportError("");
    try {
      await fetch(`${API_BASE}/corpus/wipe`, {
        method: "POST",
        headers: authHeaders(),
      });
      clearSession();
      setCorpusInfo(null);
      setCorpusMode("import-notes");
    } catch (err) {
      setImportError(`wipe failed: ${(err as Error).message}`);
    }
  }

  useEffect(() => {
    if (wsStatus !== "generating") return;
    const id = setInterval(() => {
      setWsElapsed(Math.floor((Date.now() - wsTurnStartRef.current) / 1000));
    }, 250);
    return () => clearInterval(id);
  }, [wsStatus]);

  useEffect(() => {
    if (corpusMode !== "ready") return;
    fetch(`${API_BASE}/eras`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data: Era[]) => {
        setEras(data);
        const firstWithNotes = data.find((e) => e.note_count > 0);
        if (firstWithNotes) setSelected(firstWithNotes.name);
      })
      .catch((e) => setError(String(e)));
  }, [corpusMode]);

  useEffect(() => {
    const el = convLogRef.current;
    if (!el) return;
    const lastItem = log[log.length - 1];
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (lastItem?.kind === "user" || distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [log, wsStatus]);

  useEffect(() => {
    if (corpusMode !== "ready") return;
    if (!showNotes || !selected || selected === notesEra) return;
    let cancelled = false;
    (async () => {
      setNotesLoading(true);
      setNotes([]);
      setSelectedNote(null);
      try {
        const resp = await fetch(`${API_BASE}/notes?era=${encodeURIComponent(selected)}`, { headers: authHeaders() });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: Note[] = await resp.json();
        if (cancelled) return;
        setNotes(data);
        setNotesEra(selected);
      } catch (e) {
        if (cancelled) return;
        setError(`failed to load notes: ${(e as Error).message}`);
      } finally {
        if (!cancelled) setNotesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected, showNotes, notesEra]);

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

  async function openNotes() {
    if (!selected) return;
    if (showNotes) {
      setShowNotes(false);
      return;
    }
    setShowNotes(true);
    if (notesEra === selected && notes.length > 0) return;
    setNotesLoading(true);
    setNotes([]);
    setSelectedNote(null);
    try {
      const resp = await fetch(`${API_BASE}/notes?era=${encodeURIComponent(selected)}`, { headers: authHeaders() });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: Note[] = await resp.json();
      setNotes(data);
      setNotesEra(selected);
    } catch (e) {
      setError(`failed to load notes: ${(e as Error).message}`);
      setShowNotes(false);
    } finally {
      setNotesLoading(false);
    }
  }

  async function jumpToNote(dateKey: string) {
    if (!selected) return;
    setShowNotes(true);
    if (notesEra !== selected || notes.length === 0) {
      setNotesLoading(true);
      setNotes([]);
      setSelectedNote(null);
      try {
        const resp = await fetch(`${API_BASE}/notes?era=${encodeURIComponent(selected)}`, { headers: authHeaders() });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: Note[] = await resp.json();
        setNotes(data);
        setNotesEra(selected);
        const match = data.find((n) => n.date.slice(0, 10) === dateKey);
        if (match) setSelectedNote(match);
      } catch (e) {
        setError(`failed to load notes: ${(e as Error).message}`);
      } finally {
        setNotesLoading(false);
      }
      return;
    }
    const match = notes.find((n) => n.date.slice(0, 10) === dateKey);
    if (match) setSelectedNote(match);
  }

  function startSession() {
    if (!selected) return;
    setLog([]);
    setOutput("");
    setReply("");
    setWsRunDir("");
    setWsCost(0);
    setRightView("conversation");
    setPromoteState("idle");
    setPromoteResult(null);
    setError("");
    setWsStatus("connecting");
    setWsElapsed(0);
    setWsHasNarration(false);
    wsTurnStartRef.current = Date.now();
    narrationBufRef.current = "";

    const ws = new WebSocket(
      `${WS_BASE}/session?session=${encodeURIComponent(getSession() || "")}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(
        JSON.stringify({ type: "start", era: selected, future: useFuture, model }),
      );
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
        setWsRunDir(payload.run_dir);
        setWsNotes(payload.notes ?? 0);
        setWsPrior(payload.prior_chapters ?? 0);
        setWsFuture(payload.future_chapters ?? 0);
        setWsInputChars(payload.input_chars ?? 0);
      } else if (t === "narration") {
        narrationBufRef.current += payload.text;
        flushNarration();
        setWsHasNarration(true);
      } else if (t === "status") {
        flushNarration();
        if (payload.status === "generating") {
          setWsStatus("generating");
          setWsElapsed(0);
          setWsHasNarration(false);
          wsTurnStartRef.current = Date.now();
        } else if (payload.status === "awaiting_reply") {
          setWsStatus("awaiting_reply");
        }
        // swallow noisy intermediate statuses (spawned, requesting, etc.)
      } else if (t === "tool_use" || t === "tool_use_start") {
        flushNarration();
        if (t === "tool_use") {
          setLog((l) => [
            ...l,
            { kind: "tool", id: payload.id, name: payload.name, input: payload.input },
          ]);
        }
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
      } else if (t === "output_update") {
        setOutput(payload.content);
      } else if (t === "log") {
        flushNarration();
        setLog((l) => [...l, { kind: "status", text: payload.text }]);
      } else if (t === "turn_end") {
        flushNarration();
        if (typeof payload.cost_usd === "number") setWsCost(payload.cost_usd);
        console.log("[cache] turn_end usage:", payload.usage);
      } else if (t === "done") {
        flushNarration();
        setWsStatus("done");
        if (typeof payload.cost_usd === "number") setWsCost(payload.cost_usd);
        if (payload.run_dir) setWsRunDir(payload.run_dir);
      } else if (t === "error") {
        setError(payload.message);
        setWsStatus("error");
      }
    };
    ws.onerror = () => {
      setError("websocket error");
      setWsStatus("error");
    };
    ws.onclose = () => {
      flushNarration();
      if (wsStatus !== "done" && wsStatus !== "error") setWsStatus("done");
    };
  }

  function sendReply() {
    const ws = wsRef.current;
    if (!ws || !reply.trim()) return;
    ws.send(JSON.stringify({ type: "reply", text: reply }));
    setLog((l) => [...l, { kind: "user", text: reply }]);
    setReply("");
    setWsStatus("generating");
  }

  async function promoteChapter() {
    if (!selected || !wsRunDir) return;
    if (
      selectedEra?.has_chapter &&
      !confirm(
        `This will overwrite the existing chapter for "${selected}". Continue?`,
      )
    ) {
      return;
    }
    setPromoteState("promoting");
    try {
      const resp = await fetch(`${API_BASE}/promote`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ era: selected, run_dir: wsRunDir }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const data = await resp.json();
      setPromoteResult(data);
      setPromoteState("done");
      // Refresh era list so the ✓ updates.
      fetch(`${API_BASE}/eras`, { headers: authHeaders() })
        .then((r) => r.json())
        .then(setEras)
        .catch(() => {});
    } catch (e) {
      setError(`promote failed: ${(e as Error).message}`);
      setPromoteState("error");
    }
  }

  function stopSession() {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
    setWsStatus("done");
  }

  // ---- Themes flow handlers ----

  function flushThemesNarration() {
    if (!themesNarrationBufRef.current) return;
    const text = themesNarrationBufRef.current;
    themesNarrationBufRef.current = "";
    setThemesLog((l) => {
      const last = l[l.length - 1];
      if (last && last.kind === "narration") {
        return [...l.slice(0, -1), { kind: "narration", text: last.text + text }];
      }
      return [...l, { kind: "narration", text }];
    });
  }

  async function startThemesSpin() {
    setThemesPhase("spinning");
    setThemesOutput("");
    setThemesRunDir("");
    setThemesLocked("");
    setThemesLog([]);
    setError("");

    try {
      const resp = await fetch(`${API_BASE}/themes-spin`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ top_n: themesTopN, model }),
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
            setThemesOutput((s) => s + payload.text);
          } else if (payload.type === "start") {
            setThemesRunDir(payload.run_dir);
          } else if (payload.type === "done") {
            setThemesPhase("spin-done");
            setThemesCost(payload.cost_usd ?? 0);
          } else if (payload.type === "error") {
            setError(payload.message);
            setThemesPhase("error");
          }
        }
      }
    } catch (e) {
      setError(`spin failed: ${(e as Error).message}`);
      setThemesPhase("error");
    }
  }

  function startCuration() {
    if (!themesRunDir) return;
    setThemesPhase("curating");
    setThemesStatus("generating");
    setThemesLog([]);
    setThemesReply("");
    themesNarrationBufRef.current = "";
    setError("");

    const ws = new WebSocket(
      `${WS_BASE}/themes-curate?session=${encodeURIComponent(getSession() || "")}`,
    );
    themesWsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "start", run_dir: themesRunDir, model }));
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
        themesNarrationBufRef.current += payload.text;
        flushThemesNarration();
      } else if (t === "status") {
        flushThemesNarration();
        if (payload.status === "generating") setThemesStatus("generating");
        else if (payload.status === "awaiting_reply") setThemesStatus("awaiting_reply");
      } else if (t === "tool_use") {
        flushThemesNarration();
        setThemesLog((l) => [
          ...l,
          { kind: "tool", id: payload.id, name: payload.name, input: payload.input },
        ]);
      } else if (t === "tool_result") {
        setThemesLog((l) =>
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
        setThemesLocked(payload.content);
        setThemesPhase("locked");
      } else if (t === "turn_end") {
        flushThemesNarration();
        if (typeof payload.cost_usd === "number") setThemesCost(payload.cost_usd);
      } else if (t === "done") {
        flushThemesNarration();
        setThemesStatus("done");
        if (typeof payload.cost_usd === "number") setThemesCost(payload.cost_usd);
      } else if (t === "log") {
        flushThemesNarration();
        setThemesLog((l) => [...l, { kind: "status", text: payload.text }]);
      } else if (t === "error") {
        setError(payload.message);
        setThemesStatus("error");
      }
    };
    ws.onerror = () => {
      setError("themes websocket error");
      setThemesStatus("error");
    };
    ws.onclose = () => {
      flushThemesNarration();
      if (themesStatus !== "done" && themesStatus !== "error") setThemesStatus("done");
    };
  }

  function sendThemesReply() {
    const ws = themesWsRef.current;
    if (!ws || !themesReply.trim()) return;
    ws.send(JSON.stringify({ type: "reply", text: themesReply }));
    setThemesLog((l) => [...l, { kind: "user", text: themesReply }]);
    setThemesReply("");
    setThemesStatus("generating");
  }

  function stopThemesCuration() {
    const ws = themesWsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
    setThemesStatus("done");
  }

  // Auto-scroll themes log to bottom on updates.
  useEffect(() => {
    const el = themesLogRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const lastItem = themesLog[themesLog.length - 1];
    if (lastItem?.kind === "user" || distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [themesLog, themesStatus]);

  const selectedEra = eras.find((e) => e.name === selected);
  const wsActive = wsStatus !== "idle" && wsStatus !== "done" && wsStatus !== "error";

  return (
    <>
      {corpusMode === "loading" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="text-stone-500 text-sm">Loading…</div>
        </div>
      )}
      {corpusMode === "error" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="max-w-md p-8">
            <h1 className="font-serif text-xl mb-2">Something went wrong</h1>
            <p className="text-red-600 text-sm">{error}</p>
          </div>
        </div>
      )}
      {corpusMode === "import-notes" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">Biographer</h1>
            <p className="text-stone-600 mb-6 text-sm leading-relaxed">
              Upload a zip of your notes (e.g. <code className="bg-stone-100 px-1 rounded text-xs">.md</code> or <code className="bg-stone-100 px-1 rounded text-xs">.txt</code> files) to import a corpus. Each browser sees only the corpus it imports. Files live on the host's machine.
            </p>
            <label className="block">
              <span className="text-sm font-medium text-stone-700">Notes (.zip)</span>
              <input
                type="file"
                accept=".zip,application/zip"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleNotesUpload(file);
                }}
                className="mt-2 block w-full text-sm text-stone-700"
              />
            </label>
            {importError && (
              <p className="mt-4 text-red-600 text-sm">{importError}</p>
            )}
          </div>
        </div>
      )}
      {corpusMode === "import-eras" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">One more step</h1>
            <p className="text-stone-600 mb-6 text-sm leading-relaxed">
              {corpusInfo?.note_count ?? 0} notes uploaded. Now provide an{" "}
              <code className="bg-stone-100 px-1 rounded text-xs">eras.yaml</code>{" "}
              defining the era boundaries for this corpus.
            </p>
            <label className="block">
              <span className="text-sm font-medium text-stone-700">eras.yaml</span>
              <input
                type="file"
                accept=".yaml,.yml,application/x-yaml,text/yaml,text/plain"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleErasUpload(file);
                }}
                className="mt-2 block w-full text-sm text-stone-700"
              />
            </label>
            {importError && (
              <p className="mt-4 text-red-600 text-sm">{importError}</p>
            )}
            <button
              onClick={() => {
                if (window.confirm("Discard the uploaded notes and start over?")) {
                  handleWipe();
                }
              }}
              className="mt-6 text-xs text-stone-500 hover:text-stone-700 underline"
            >
              Cancel and discard
            </button>
          </div>
        </div>
      )}
      {corpusMode === "ready" && (
        <div className="min-h-full relative">
          <header className="border-b border-stone-200 bg-white">
        <div className="mx-auto max-w-5xl px-6 py-4 flex items-center gap-4">
          <h1 className="font-serif text-xl">Biographer</h1>
          {corpusInfo && !corpusInfo.is_legacy && (
            <button
              onClick={() => {
                if (
                  window.confirm(
                    "Wipe this corpus? This deletes all uploaded notes and eras from the host.",
                  )
                ) {
                  handleWipe();
                }
              }}
              className="text-xs text-stone-400 hover:text-red-600"
              title="Delete this corpus"
            >
              Wipe corpus
            </button>
          )}
          <div className="flex items-center gap-1 ml-2">
            <button
              className={
                "font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors " +
                (viewMode === "eras"
                  ? "text-stone-700 border-b-2 border-stone-700"
                  : "text-stone-400 hover:text-stone-700")
              }
              onClick={() => setViewMode("eras")}
            >
              Eras
            </button>
            <button
              className={
                "font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors " +
                (viewMode === "themes"
                  ? "text-stone-700 border-b-2 border-stone-700"
                  : "text-stone-400 hover:text-stone-700")
              }
              onClick={() => setViewMode("themes")}
            >
              Themes
            </button>
          </div>
          {viewMode === "eras" && (
          <div className="ml-auto flex items-center gap-2">
            <select
              className="rounded border border-stone-300 bg-white px-2 py-1 text-sm"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              disabled={wsActive}
            >
              {eras.map((e) => (
                <option
                  key={e.name}
                  value={e.name}
                  disabled={e.note_count === 0}
                  title={`from ${formatEraStart(e.start)}`}
                >
                  {e.name} ({e.note_count})
                  {e.has_chapter ? " ✓" : ""}
                </option>
              ))}
            </select>
            <select
              className="rounded border border-stone-300 bg-white px-2 py-1 text-sm"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={wsActive}
              title="Model used for this session"
            >
              <option value="opus-4.7">opus-4.7</option>
              <option value="opus-4.6">opus-4.6</option>
              <option value="sonnet-4.6">sonnet-4.6</option>
            </select>
            <label
              className={
                "flex items-center gap-1 text-xs " +
                (wsActive ? "text-stone-400" : "text-stone-600 cursor-pointer")
              }
              title="Also feed any later eras' chapters & digests already on disk into this draft (hindsight context)."
            >
              <input
                type="checkbox"
                checked={useFuture}
                onChange={(e) => setUseFuture(e.target.checked)}
                disabled={wsActive}
                className="accent-stone-700"
              />
              future
            </label>
            {wsActive ? (
              <button
                className="rounded bg-stone-800 px-3 py-1 text-sm text-white hover:bg-stone-700"
                onClick={stopSession}
              >
                Stop
              </button>
            ) : (
              <button
                className="rounded bg-stone-900 px-3 py-1 text-sm text-white hover:bg-stone-700 disabled:opacity-40"
                onClick={startSession}
                disabled={!selected || (selectedEra?.note_count ?? 0) === 0}
              >
                Start session
              </button>
            )}
          </div>
          )}
          {viewMode === "themes" && (
          <div className="ml-auto flex items-center gap-2">
            <select
              className="rounded border border-stone-300 bg-white px-2 py-1 text-sm"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              disabled={themesPhase === "spinning" || themesPhase === "curating"}
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
                value={themesTopN}
                onChange={(e) => setThemesTopN(parseInt(e.target.value) || 10)}
                disabled={themesPhase === "spinning" || themesPhase === "curating"}
                className="w-12 rounded border border-stone-300 bg-white px-1 py-1 text-sm tabular-nums"
              />
            </label>
            {themesPhase === "curating" ? (
              <button
                className="rounded bg-stone-800 px-3 py-1 text-sm text-white hover:bg-stone-700"
                onClick={stopThemesCuration}
              >
                Stop
              </button>
            ) : themesPhase === "spinning" ? (
              <button
                className="rounded bg-stone-400 px-3 py-1 text-sm text-white"
                disabled
              >
                Generating…
              </button>
            ) : (
              <button
                className="rounded bg-stone-900 px-3 py-1 text-sm text-white hover:bg-stone-700"
                onClick={startThemesSpin}
              >
                {themesPhase === "spin-done" || themesPhase === "locked" ? "Re-spin" : "Generate themes"}
              </button>
            )}
          </div>
          )}
        </div>
      </header>

      <main
        className={
          showNotes ? "pl-12 pr-6 py-4" : "mx-auto max-w-4xl px-6 py-4"
        }
      >
        {error && (
          <div className="mb-6 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
            {error}
          </div>
        )}

        {viewMode === "eras" && (
        <div className={showNotes ? "flex gap-6" : ""}>
            <section className="flex-[11] min-w-0 flex flex-col h-[80vh]">
              <div className="mb-2 flex items-center gap-1 border-b border-stone-200">
                <button
                  className={
                    "font-sans text-xs uppercase tracking-wider px-3 py-1.5 -mb-px border-b-2 transition-colors " +
                    (rightView === "conversation"
                      ? "border-stone-700 text-stone-700"
                      : "border-transparent text-stone-400 hover:text-stone-700")
                  }
                  onClick={() => setRightView("conversation")}
                >
                  Conversation
                </button>
                <button
                  className={
                    "font-sans text-xs uppercase tracking-wider px-3 py-1.5 -mb-px border-b-2 transition-colors disabled:opacity-40 disabled:cursor-not-allowed " +
                    (rightView === "chapter"
                      ? "border-stone-700 text-stone-700"
                      : "border-transparent text-stone-400 hover:text-stone-700")
                  }
                  onClick={() => setRightView("chapter")}
                  disabled={!output}
                >
                  Chapter
                  {output && (
                    <span className="ml-1 normal-case tracking-normal text-stone-400">
                      ({output.length.toLocaleString()} ch)
                    </span>
                  )}
                </button>
              </div>
              {rightView === "chapter" ? (
                <article className="flex-1 rounded border border-stone-200 bg-white p-6 font-serif text-[16px] leading-[1.7] text-stone-900 overflow-auto">
                  {output ? (
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        ...CHAPTER_MD_COMPONENTS,
                        a: ({ href, children, ...props }: any) => {
                          let dateKey = "";
                          if (href?.startsWith("#cite-")) {
                            dateKey = href.slice(6);
                          } else if (href && /^\d{4}-\d{2}-\d{2}$/.test(href)) {
                            dateKey = href;
                          }
                          if (dateKey) {
                            return (
                              <a
                                href={href}
                                className="text-stone-700 underline decoration-dotted underline-offset-2 cursor-pointer hover:text-stone-900"
                                onClick={(e) => {
                                  e.preventDefault();
                                  jumpToNote(dateKey);
                                }}
                              >
                                {children}
                              </a>
                            );
                          }
                          return (
                            <a
                              href={href}
                              className="text-stone-700 underline"
                              {...props}
                            >
                              {children}
                            </a>
                          );
                        },
                      }}
                    >
                      {output.replace(
                        /(?<!\])\[(\d{4}-\d{2}-\d{2})\](?!\()/g,
                        "[\\[$1\\]](#cite-$1)",
                      )}
                    </ReactMarkdown>
                  ) : (
                    <span className="font-sans text-sm text-stone-400">
                      (no chapter content yet)
                    </span>
                  )}
                </article>
              ) : (
                <div
                  ref={convLogRef}
                  className="flex-1 rounded border border-stone-200 bg-white p-4 font-sans text-sm text-stone-800 overflow-auto"
                >
                {wsStatus === "idle" && (
                  <span className="text-stone-400">Press Start session.</span>
                )}
                {wsStatus === "connecting" && (
                  <span className="text-stone-400">connecting…</span>
                )}
                {log.map((it, i) => {
                  if (it.kind === "narration")
                    return (
                      <div key={i}>
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            ...MD_COMPONENTS,
                            a: ({ href, children, ...props }: any) => {
                              let dateKey = "";
                              if (href?.startsWith("#cite-")) {
                                dateKey = href.slice(6);
                              } else if (
                                href &&
                                /^\d{4}-\d{2}-\d{2}$/.test(href)
                              ) {
                                dateKey = href;
                              }
                              if (dateKey) {
                                return (
                                  <a
                                    href={href}
                                    className="text-stone-700 underline decoration-dotted underline-offset-2 cursor-pointer hover:text-stone-900"
                                    onClick={(e) => {
                                      e.preventDefault();
                                      jumpToNote(dateKey);
                                    }}
                                  >
                                    {children}
                                  </a>
                                );
                              }
                              return (
                                <a href={href} {...props}>
                                  {children}
                                </a>
                              );
                            },
                          }}
                        >
                          {it.text.replace(
                            /(?<!\])\[(\d{4}-\d{2}-\d{2})\](?!\()/g,
                            "[\\[$1\\]](#cite-$1)",
                          )}
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
                          {formatTool(it.name, it.input, wsRunDir)}
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
                {wsStatus === "generating" && !wsHasNarration && (
                  (() => {
                    const totalTok = Math.round(wsInputChars / 4);
                    // Conservative prompt-processing rate (~1.5K tok/s) capped
                    // at 90% so we never claim "almost done" before the model
                    // actually emits. The cap is intentional — sitting at 90%
                    // looks honest; sitting at 99% looks broken.
                    const progressTok = Math.min(
                      Math.round(wsElapsed * 1500),
                      Math.round(totalTok * 0.9),
                    );
                    const fmt = (n: number) =>
                      n >= 1000
                        ? `${(n / 1000).toFixed(1)}k`
                        : String(n);
                    const isFirstTurn = log.length === 0;
                    return (
                      <div className="my-2 flex items-center gap-2 text-stone-500">
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-stone-400 animate-pulse" />
                        {isFirstTurn ? (
                          <span>
                            reading {wsNotes} notes
                            {wsPrior > 0
                              ? ` + ${wsPrior} prior chapter${wsPrior === 1 ? "" : "s"}`
                              : ""}
                            {wsFuture > 0
                              ? ` + ${wsFuture} future chapter${wsFuture === 1 ? "" : "s"}`
                              : ""}
                            {totalTok > 0 && (
                              <span className="text-stone-400">
                                {" "}
                                · <span className="tabular-nums">{fmt(progressTok)}</span>
                                {" / "}
                                <span className="tabular-nums">{fmt(totalTok)}</span> tokens
                              </span>
                            )}
                          </span>
                        ) : (
                          <span>thinking…</span>
                        )}
                      </div>
                    );
                  })()
                )}
                {wsStatus === "generating" && wsHasNarration && (
                  <span className="inline-block w-2 h-4 ml-0.5 align-text-bottom bg-stone-400 animate-pulse" />
                )}
              </div>
              )}

              <div className="mt-3">
                <div className="mb-1 flex items-center gap-2 text-xs font-sans">
                  {wsStatus === "generating" && (
                    <>
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                      <span className="text-amber-700">working…</span>
                    </>
                  )}
                  {wsStatus === "awaiting_reply" && (
                    <>
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500" />
                      <span className="text-emerald-700">ready for your reply</span>
                    </>
                  )}
                  {wsStatus === "done" && (
                    <span className="text-stone-500">session ended</span>
                  )}
                  {wsStatus === "idle" && (
                    <span className="text-stone-400">no session</span>
                  )}
                </div>
                <div className="relative">
                <textarea
                  className={
                    "block w-full resize-none rounded border px-3 pt-2 pb-11 text-sm font-sans disabled:text-stone-400 transition-colors " +
                    (wsStatus === "awaiting_reply"
                      ? "border-emerald-400 bg-white"
                      : wsStatus === "generating"
                        ? "border-stone-200 bg-stone-50"
                        : "border-stone-300 bg-stone-50")
                  }
                  rows={3}
                  value={reply}
                  placeholder={
                    wsStatus === "awaiting_reply"
                      ? "reply to the agent…"
                      : wsStatus === "generating"
                        ? "agent is working — wait for it to finish"
                        : wsStatus === "idle"
                          ? "start a session to chat"
                          : "session ended"
                  }
                  disabled={wsStatus !== "awaiting_reply"}
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
                  disabled={wsStatus !== "awaiting_reply" || !reply.trim()}
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

              {(wsRunDir || wsCost > 0) && (
                <div className="mt-4 font-sans text-xs text-stone-500 space-y-1">
                  {wsCost > 0 && <div>cost: ${wsCost.toFixed(4)}</div>}
                  {wsRunDir && <div>run: {wsRunDir}</div>}
                  {output && (
                    <div className="pt-2 flex items-center gap-3">
                      {promoteState === "done" && promoteResult ? (
                        <span className="text-emerald-700">
                          ✓ promoted to {promoteResult.dst} ({promoteResult.words} words
                          {promoteResult.overwritten && ", overwrote previous"})
                        </span>
                      ) : (
                        <button
                          className="rounded bg-emerald-700 px-3 py-1 text-xs text-white hover:bg-emerald-800 disabled:opacity-40"
                          onClick={promoteChapter}
                          disabled={promoteState === "promoting"}
                        >
                          {promoteState === "promoting"
                            ? "promoting…"
                            : selectedEra?.has_chapter
                              ? "Promote (overwrites existing)"
                              : "Promote chapter"}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}
            </section>
            {showNotes && (() => {
              const bounds = eraBounds(notes);
              const dateGroups: { dateKey: string; notes: Note[] }[] = [];
              for (const n of notes) {
                const k = n.date.slice(0, 10);
                const last = dateGroups[dateGroups.length - 1];
                if (last && last.dateKey === k) last.notes.push(n);
                else dateGroups.push({ dateKey: k, notes: [n] });
              }
              const layout = (() => {
                if (!bounds || !dateGroups.length) return null;
                const padTop = 4;
                const padBottom = 16;
                const pxPerDay = 4;
                const intraTitle = 18;
                const interGap = 4;
                const markerOverhead = 32;
                type Item =
                  | { kind: "marker"; monthKey: string; y: number }
                  | {
                      kind: "group";
                      group: { dateKey: string; notes: Note[] };
                      y: number;
                      height: number;
                    };
                const items: Item[] = [];
                let prevBottom = padTop;
                let prevMonth = "";
                for (const g of dateGroups) {
                  const m = g.dateKey.slice(0, 7);
                  const dayMs = new Date(g.dateKey).getTime();
                  const proportionalY =
                    padTop + ((dayMs - bounds.start) / 86400000) * pxPerDay;
                  const isMonthChange = m !== prevMonth;
                  const isFirstItem = items.length === 0;
                  const minGap =
                    isMonthChange && !isFirstItem ? markerOverhead : interGap;
                  const y = Math.max(proportionalY, prevBottom + minGap);
                  if (isMonthChange) {
                    items.push({ kind: "marker", monthKey: m, y });
                    prevMonth = m;
                  }
                  const height = g.notes.length * intraTitle;
                  items.push({ kind: "group", group: g, y, height });
                  prevBottom = y + height;
                }
                const totalHeight = prevBottom + padBottom;
                return { items, totalHeight, padTop };
              })();
              const formatMonth = (mk: string) => {
                const [yy, mm] = mk.split("-").map(Number);
                return new Date(yy, mm - 1, 1).toLocaleDateString("en-US", {
                  month: "short",
                  year: "numeric",
                });
              };
              let visibleMonth = "";
              if (layout && layout.items.length) {
                const probe = notesScrollTop;
                for (const item of layout.items) {
                  if (item.kind !== "marker") continue;
                  if (item.y > probe) break;
                  visibleMonth = formatMonth(item.monthKey);
                }
                if (!visibleMonth) {
                  const firstMarker = layout.items.find(
                    (i) => i.kind === "marker",
                  );
                  if (firstMarker && firstMarker.kind === "marker") {
                    visibleMonth = formatMonth(firstMarker.monthKey);
                  }
                }
              }
              return (
                <aside
                  className={
                    notesOverlay
                      ? "fixed left-12 right-6 top-[114px] h-[80vh] z-40 bg-white border border-stone-200 rounded-lg shadow-2xl flex flex-col"
                      : "flex-[9] min-w-0 flex flex-col h-[80vh] mt-[34px]"
                  }
                >
                  <div className="flex flex-1 overflow-hidden rounded border border-stone-200 bg-white">
                    {showTimeline && (
                    <div className="w-[182px] shrink-0 border-r border-stone-200 relative flex flex-col">
                      {!notesLoading && layout && (
                        <div className="absolute top-3 left-3 z-30 px-2 py-0.5 bg-white rounded font-sans text-[11px] uppercase tracking-wider text-stone-600 shadow-sm pointer-events-none">
                          {visibleMonth}
                        </div>
                      )}
                      <div
                        className="flex-1 overflow-auto"
                        onScroll={(e) =>
                          setNotesScrollTop(
                            (e.currentTarget as HTMLDivElement).scrollTop,
                          )
                        }
                      >
                        <div className="sticky top-0 h-9 bg-white z-20" />
                        {notesLoading && (
                          <div className="p-6 font-sans text-xs text-stone-400">
                            loading…
                          </div>
                        )}
                        {!notesLoading && notes.length === 0 && (
                          <div className="p-6 font-sans text-xs text-stone-400">
                            no notes
                          </div>
                        )}
                        {!notesLoading && layout && (
                          <div
                            className="relative"
                            style={{ height: `${layout.totalHeight}px` }}
                          >
                            <div className="absolute left-[28px] top-0 bottom-0 w-px bg-stone-200" />
                            {layout.items.map((item) => {
                              if (item.kind === "marker") {
                                const text = formatMonth(item.monthKey);
                                if (text === visibleMonth) return null;
                                return (
                                  <div
                                    key={`m-${item.monthKey}`}
                                    className="absolute left-3 -translate-y-full px-2 py-0.5 bg-white rounded font-sans text-[11px] uppercase tracking-wider text-stone-600 shadow-sm pointer-events-none"
                                    style={{ top: item.y - 4 }}
                                  >
                                    {text}
                                  </div>
                                );
                              }
                              const { group, y, height } = item;
                              const groupSelected = group.notes.some(
                                (n) => selectedNote?.rel === n.rel,
                              );
                              return (
                                <div
                                  key={group.dateKey}
                                  className="absolute"
                                  style={{ top: y, left: 0, right: 0, height }}
                                >
                                  <span
                                    className={
                                      "absolute rounded-full transition-all -translate-x-1/2 -translate-y-1/2 " +
                                      (groupSelected
                                        ? "w-3 h-3 bg-stone-900 ring-2 ring-stone-200"
                                        : "w-1.5 h-1.5 bg-stone-400")
                                    }
                                    style={{ top: 9, left: 28 }}
                                  />
                                  <div className="pl-12 pr-3 flex gap-2 items-baseline">
                                    <span className="text-[11px] tabular-nums text-stone-400 shrink-0 leading-[18px] w-5">
                                      {group.dateKey.slice(8)}
                                    </span>
                                    <div className="min-w-0 flex-1 flex flex-col items-start">
                                      {group.notes.map((n) => {
                                        const isSelected =
                                          selectedNote?.rel === n.rel;
                                        return (
                                          <button
                                            key={n.rel}
                                            onClick={() => setSelectedNote(n)}
                                            className={
                                              "text-[12px] leading-[18px] text-left max-w-full truncate hover:text-stone-900 " +
                                              (isSelected
                                                ? "text-stone-900 font-medium"
                                                : "text-stone-700")
                                            }
                                            title={`${n.date.slice(0, 10)} · ${n.label}${
                                              n.source ? ` · ${n.source}` : ""
                                            } · ${n.title || "(untitled)"}`}
                                          >
                                            {n.title || (
                                              <span className="italic text-stone-400">
                                                (untitled)
                                              </span>
                                            )}
                                          </button>
                                        );
                                      })}
                                    </div>
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                    )}
                    <div className="flex-1 min-w-0 overflow-auto">
                      {selectedNote ? (
                        <article className="p-6">
                          <header className="mb-4 pb-3 border-b border-stone-100 font-sans">
                            <div className="text-xs text-stone-500 tabular-nums">
                              {selectedNote.date.slice(0, 10)}
                            </div>
                            <div className="text-xs text-stone-400 mt-0.5">
                              {selectedNote.label}
                              {selectedNote.source && (
                                <span className="text-stone-300">
                                  {" "}
                                  · {selectedNote.source}
                                </span>
                              )}
                            </div>
                            <h2 className="mt-2 text-lg font-serif text-stone-900">
                              {selectedNote.title || (
                                <span className="text-stone-400 italic">
                                  (untitled)
                                </span>
                              )}
                            </h2>
                            {selectedNote.editor_note && (
                              <div className="mt-2 text-xs text-amber-700">
                                ⚠ Editor note: {selectedNote.editor_note}
                              </div>
                            )}
                          </header>
                          <div className="font-serif text-[15px] leading-[1.6] text-stone-900">
                            {selectedNote.body ? (
                              <ReactMarkdown
                                remarkPlugins={[remarkGfm]}
                                components={CHAPTER_MD_COMPONENTS}
                              >
                                {selectedNote.body.replace(
                                  /(\S)\n(?=\S)/g,
                                  "$1  \n",
                                )}
                              </ReactMarkdown>
                            ) : (
                              <span className="font-sans text-sm text-stone-400">
                                (empty)
                              </span>
                            )}
                          </div>
                        </article>
                      ) : (
                        <div className="p-6 font-sans text-sm text-stone-400">
                          {notesLoading
                            ? "loading…"
                            : notes.length === 0
                              ? "no notes"
                              : "Click a marker on the timeline to read."}
                        </div>
                      )}
                    </div>
                  </div>
                </aside>
              );
            })()}
        </div>
        )}
        {viewMode === "themes" && (
          <section className="flex flex-col h-[80vh]">
            <div className="mb-2 flex items-center gap-2 border-b border-stone-200 pb-2">
              <span className="font-sans text-xs uppercase tracking-wider text-stone-700">
                {themesPhase === "idle" && "no run yet"}
                {themesPhase === "spinning" && "generating round 1…"}
                {themesPhase === "spin-done" && "round 1 done — ready to curate"}
                {themesPhase === "curating" && "curating"}
                {themesPhase === "locked" && "themes locked"}
                {themesPhase === "error" && "error"}
              </span>
              {themesRunDir && (
                <span className="font-mono text-[11px] text-stone-400 truncate">
                  {themesRunDir}
                </span>
              )}
              {themesCost > 0 && (
                <span className="font-sans text-xs text-stone-500 ml-auto tabular-nums">
                  ${themesCost.toFixed(4)}
                </span>
              )}
            </div>

            {themesPhase === "idle" && (
              <div className="flex-1 flex items-center justify-center text-stone-400 text-sm">
                Click <span className="font-medium mx-1 text-stone-600">Generate themes</span> to start round 1.
              </div>
            )}

            {(themesPhase === "spinning" || themesPhase === "spin-done") && (
              <article className="flex-1 overflow-auto rounded border border-stone-200 bg-white p-6 font-serif text-[15px] leading-[1.7] text-stone-900">
                {themesOutput ? (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={CHAPTER_MD_COMPONENTS}>
                    {themesOutput}
                  </ReactMarkdown>
                ) : (
                  <span className="font-sans text-sm text-stone-400">
                    {themesPhase === "spinning" ? "starting…" : "(no output)"}
                  </span>
                )}
              </article>
            )}

            {themesPhase === "spin-done" && (
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

            {themesPhase === "curating" && (
              <>
                <div
                  ref={themesLogRef}
                  className="flex-1 rounded border border-stone-200 bg-white p-4 font-sans text-sm text-stone-800 overflow-auto"
                >
                  {themesStatus === "generating" && themesLog.length === 0 && (
                    <span className="text-stone-400">starting curation…</span>
                  )}
                  {themesLog.map((it, i) => {
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
                            {formatTool(it.name, it.input, themesRunDir)}
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
                  {themesStatus === "generating" && themesLog.length > 0 && (
                    <span className="inline-block w-2 h-4 ml-0.5 align-text-bottom bg-stone-400 animate-pulse" />
                  )}
                </div>

                <div className="mt-3">
                  <div className="mb-1 flex items-center gap-2 text-xs font-sans">
                    {themesStatus === "generating" && (
                      <>
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                        <span className="text-amber-700">working…</span>
                      </>
                    )}
                    {themesStatus === "awaiting_reply" && (
                      <>
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-500" />
                        <span className="text-emerald-700">your move (drop, merge, propose, /lock)</span>
                      </>
                    )}
                    {themesStatus === "done" && (
                      <span className="text-stone-500">curation ended</span>
                    )}
                  </div>
                  <div className="relative">
                    <textarea
                      className={
                        "block w-full resize-none rounded border px-3 pt-2 pb-11 text-sm font-sans disabled:text-stone-400 transition-colors " +
                        (themesStatus === "awaiting_reply"
                          ? "border-emerald-400 bg-white"
                          : "border-stone-300 bg-stone-50")
                      }
                      rows={3}
                      value={themesReply}
                      placeholder={
                        themesStatus === "awaiting_reply"
                          ? "drop X / merge X and Y / propose: theme name / /lock"
                          : "wait for the model…"
                      }
                      disabled={themesStatus !== "awaiting_reply"}
                      onChange={(e) => setThemesReply(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey && themesReply.trim()) {
                          e.preventDefault();
                          sendThemesReply();
                        }
                      }}
                    />
                    <button
                      className="absolute bottom-2 right-2 flex h-7 w-7 items-center justify-center rounded-full bg-stone-900 text-white hover:bg-stone-700 disabled:opacity-40"
                      onClick={sendThemesReply}
                      disabled={themesStatus !== "awaiting_reply" || !themesReply.trim()}
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

            {themesPhase === "locked" && themesLocked && (
              <article className="flex-1 overflow-auto rounded border border-stone-200 bg-white p-6 font-serif text-[15px] leading-[1.7] text-stone-900">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={CHAPTER_MD_COMPONENTS}>
                  {themesLocked}
                </ReactMarkdown>
              </article>
            )}
          </section>
        )}
      </main>

      {viewMode === "eras" && selected && (selectedEra?.note_count ?? 0) > 0 && (
        <div className="absolute top-[5rem] right-6 z-50 flex items-end pointer-events-none">
          <div className="flex items-center pointer-events-auto">
            {showNotes && (
              <button
                className="font-sans text-[11px] uppercase tracking-wider px-3 py-1.5 -mb-px text-stone-400 hover:text-stone-700"
                onClick={() => setShowTimeline(!showTimeline)}
              >
                {showTimeline ? "hide timeline" : "show timeline"}
              </button>
            )}
            {showNotes && (
              <button
                className="font-sans text-[11px] uppercase tracking-wider px-3 py-1.5 -mb-px text-stone-400 hover:text-stone-700"
                onClick={() => setNotesOverlay(!notesOverlay)}
              >
                {notesOverlay ? "shrink" : "expand"}
              </button>
            )}
            <button
              className={
                "font-sans text-xs uppercase tracking-wider px-3 py-1.5 -mb-px border-b-2 transition-colors " +
                (showNotes
                  ? "border-stone-700 text-stone-700"
                  : "border-transparent text-stone-400 hover:text-stone-700")
              }
              onClick={openNotes}
            >
              Notes
            </button>
          </div>
        </div>
      )}
        </div>
      )}
    </>
  );
}
