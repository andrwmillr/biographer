import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ErasView } from "./ErasView";
import { ImportFlow, type CorpusInfo } from "./ImportFlow";
import {
  authHeaders,
  clearAuthToken,
  clearSession,
  getAuthToken,
  getSession,
  setAuthToken,
  setSession,
} from "./auth";

// Backend URL resolution: ?backend=... query param > VITE_BACKEND_URL build env > same-origin (vite dev proxy).
const _backendOverride = new URLSearchParams(location.search).get("backend") ?? undefined;
const API_BASE = _backendOverride ?? import.meta.env.VITE_BACKEND_URL ?? "";
const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;

// Markdown component overrides — used by the inline themes flow. Will move to
// ThemesView when phase 3 lands.
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

export default function App() {
  const [error, setError] = useState<string>("");
  const [model, setModel] = useState<string>("opus-4.7");
  const [viewMode, setViewMode] = useState<"eras" | "themes">("eras");

  // ---- Themes flow state (will move to <ThemesView> in phase 3) ----
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

  // ---- Auth + corpus routing ----
  // Modes: loading (bootstrap) → login (no auth) → picker (pick corpus) →
  // import (upload notes/eras) → ready (full app). Legacy admin bypasses
  // login entirely via the LEGACY_SESSION secret in localStorage.
  const [corpusMode, setCorpusMode] = useState<
    "loading" | "login" | "picker" | "import" | "ready" | "error"
  >("loading");
  const [corpusInfo, setCorpusInfo] = useState<CorpusInfo | null>(null);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [userCorpora, setUserCorpora] = useState<string[]>([]);
  const [loginEmail, setLoginEmail] = useState<string>("");
  const [loginSent, setLoginSent] = useState<boolean>(false);
  const [loginError, setLoginError] = useState<string>("");

  // Refresh /auth/me to pick up newly-imported corpora and verify the token
  // is still valid. Returns the parsed user record or null on auth failure.
  async function refreshUser(): Promise<{ email: string; corpora: string[] } | null> {
    if (!getAuthToken()) return null;
    const r = await fetch(`${API_BASE}/auth/me`, { headers: authHeaders() });
    if (r.status === 401) {
      clearAuthToken();
      return null;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    const me = (await r.json()) as { email: string; corpora: string[] };
    setUserEmail(me.email);
    setUserCorpora(me.corpora);
    return me;
  }

  // Bootstrap: land magic-link, then route by auth + corpus state.
  useEffect(() => {
    (async () => {
      // 1. Magic-link landing: pick up #auth=<token> and clean the URL.
      if (location.hash.startsWith("#auth=")) {
        const token = decodeURIComponent(location.hash.slice("#auth=".length));
        if (token) setAuthToken(token);
        history.replaceState(null, "", location.pathname + location.search);
      }

      // 2. If a corpus session is already selected, try it first. This handles
      //    legacy admin (LEGACY_SESSION secret) AND already-picked authed users.
      const slug = getSession();
      if (slug) {
        const r = await fetch(`${API_BASE}/corpus`, { headers: authHeaders() });
        if (r.ok) {
          const info = (await r.json()) as CorpusInfo;
          setCorpusInfo(info);
          setCorpusMode(info.has_eras ? "ready" : "import");
          // Best-effort: if the user is also authed, refresh their corpora list.
          refreshUser().catch(() => {});
          return;
        }
        // 401/403 means: slug isn't valid for current auth state. Drop it
        // and fall through to auth-aware routing.
        if (r.status === 401 || r.status === 403) {
          clearSession();
        } else {
          throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        }
      }

      // 3. No working slug. If not authed, show login.
      if (!getAuthToken()) {
        setCorpusMode("login");
        return;
      }

      // 4. Authed: fetch user state, route to picker or import.
      const me = await refreshUser();
      if (!me) {
        setCorpusMode("login");
        return;
      }
      setCorpusMode(me.corpora.length === 0 ? "import" : "picker");
    })().catch((err: Error) => {
      setError(`bootstrap failed: ${err.message}`);
      setCorpusMode("error");
    });
  }, []);

  async function handleRequestLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoginError("");
    const email = loginEmail.trim().toLowerCase();
    if (!email) return;
    try {
      const r = await fetch(`${API_BASE}/auth/request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          return_url: location.origin + location.pathname,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      setLoginSent(true);
    } catch (err) {
      setLoginError((err as Error).message);
    }
  }

  async function handleLogout() {
    try {
      await fetch(`${API_BASE}/auth/logout`, {
        method: "POST",
        headers: authHeaders(),
      });
    } catch {
      // best-effort; clearing local state is what matters
    }
    clearAuthToken();
    clearSession();
    setCorpusInfo(null);
    setUserEmail(null);
    setUserCorpora([]);
    setLoginSent(false);
    setLoginEmail("");
    setCorpusMode("login");
  }

  async function handlePickCorpus(slug: string) {
    setSession(slug);
    try {
      const r = await fetch(`${API_BASE}/corpus`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const info = (await r.json()) as CorpusInfo;
      setCorpusInfo(info);
      setCorpusMode(info.has_eras ? "ready" : "import");
    } catch (err) {
      setError(`pick failed: ${(err as Error).message}`);
      setCorpusMode("error");
    }
  }

  function handleImportAnother() {
    clearSession();
    setCorpusInfo(null);
    setCorpusMode("import");
  }

  function handleSwitchCorpus() {
    clearSession();
    setCorpusInfo(null);
    setCorpusMode("picker");
  }

  // Wipes the current corpus and routes the parent away from "ready". Throws
  // on network error so the caller view can surface it before this component
  // re-renders. HTTP non-2xx responses are silently ignored — same as before
  // extraction; can tighten later.
  async function handleWipe() {
    await fetch(`${API_BASE}/corpus/wipe`, {
      method: "POST",
      headers: authHeaders(),
    });
    clearSession();
    setCorpusInfo(null);
    if (getAuthToken()) {
      const me = await refreshUser().catch(() => null);
      if (!me) {
        setCorpusMode("login");
        return;
      }
      setCorpusMode(me.corpora.length === 0 ? "import" : "picker");
    } else {
      setCorpusMode("login");
    }
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
      `${WS_BASE}/themes-curate?session=${encodeURIComponent(getSession() || "")}` +
        `&auth=${encodeURIComponent(getAuthToken() || "")}`,
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
      {corpusMode === "login" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">Biographer</h1>
            {loginSent ? (
              <>
                <p className="text-stone-600 mb-4 text-sm leading-relaxed">
                  Check your email for a sign-in link. Click it to come back here.
                </p>
                <button
                  onClick={() => {
                    setLoginSent(false);
                    setLoginEmail("");
                    setLoginError("");
                  }}
                  className="text-xs text-stone-500 hover:text-stone-700 underline"
                >
                  Use a different email
                </button>
              </>
            ) : (
              <>
                <p className="text-stone-600 mb-6 text-sm leading-relaxed">
                  Sign in with your email to import a corpus or come back to one
                  you've already uploaded.
                </p>
                <form onSubmit={handleRequestLogin}>
                  <label className="block">
                    <span className="text-sm font-medium text-stone-700">
                      Email
                    </span>
                    <input
                      type="email"
                      required
                      autoFocus
                      value={loginEmail}
                      onChange={(e) => setLoginEmail(e.target.value)}
                      className="mt-2 block w-full rounded border border-stone-300 bg-white px-3 py-2 text-sm"
                      placeholder="you@example.com"
                    />
                  </label>
                  <button
                    type="submit"
                    className="mt-4 rounded bg-stone-700 px-4 py-2 text-sm text-white hover:bg-stone-800"
                  >
                    Send sign-in link
                  </button>
                </form>
                {loginError && (
                  <p className="mt-4 text-red-600 text-sm">{loginError}</p>
                )}
              </>
            )}
          </div>
        </div>
      )}
      {corpusMode === "picker" && (
        <div className="min-h-screen flex items-center justify-center bg-stone-50">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">Welcome back</h1>
            <p className="text-stone-600 mb-6 text-sm leading-relaxed">
              Signed in as{" "}
              <span className="font-mono text-xs">{userEmail}</span>. Pick a
              corpus to open.
            </p>
            <ul className="space-y-2">
              {userCorpora.map((slug) => (
                <li key={slug}>
                  <button
                    onClick={() => handlePickCorpus(slug)}
                    className="w-full text-left rounded border border-stone-200 bg-white px-4 py-3 text-sm font-mono hover:bg-stone-100"
                  >
                    {slug}
                  </button>
                </li>
              ))}
            </ul>
            <div className="mt-6 flex items-center gap-4">
              <button
                onClick={handleImportAnother}
                className="text-sm text-stone-700 underline hover:text-stone-900"
              >
                + Import another corpus
              </button>
              <button
                onClick={handleLogout}
                className="ml-auto text-xs text-stone-500 hover:text-stone-700 underline"
              >
                Sign out
              </button>
            </div>
          </div>
        </div>
      )}
      {corpusMode === "import" && (
        <ImportFlow
          key={corpusInfo?.slug ?? "fresh"}
          apiBase={API_BASE}
          initialInfo={corpusInfo}
          onComplete={(info) => {
            setCorpusInfo(info);
            setCorpusMode("ready");
          }}
          onWipe={handleWipe}
        />
      )}
      {corpusMode === "ready" && corpusInfo && viewMode === "eras" && (
        <ErasView
          apiBase={API_BASE}
          wsBase={WS_BASE}
          corpusInfo={corpusInfo}
          userEmail={userEmail}
          userCorpora={userCorpora}
          viewMode={viewMode}
          onSetViewMode={setViewMode}
          onWipe={handleWipe}
          onSwitchCorpus={handleSwitchCorpus}
          onLogout={handleLogout}
        />
      )}
      {corpusMode === "ready" && corpusInfo && viewMode === "themes" && (
        <div className="min-h-full relative">
          <header className="border-b border-stone-200 bg-white">
            <div className="mx-auto max-w-5xl px-6 py-4 flex items-center gap-4">
              <h1 className="font-serif text-xl">Biographer</h1>
              {!corpusInfo.is_legacy && (
                <button
                  onClick={() => {
                    if (
                      window.confirm(
                        "Wipe this corpus? This deletes all uploaded notes and eras from the host.",
                      )
                    ) {
                      handleWipe().catch((err) =>
                        setError(`wipe failed: ${(err as Error).message}`),
                      );
                    }
                  }}
                  className="text-xs text-stone-400 hover:text-red-600"
                  title="Delete this corpus"
                >
                  Wipe corpus
                </button>
              )}
              {userEmail && userCorpora.length > 1 && (
                <button
                  onClick={handleSwitchCorpus}
                  className="text-xs text-stone-400 hover:text-stone-700"
                  title="Switch to another corpus"
                >
                  Switch corpus
                </button>
              )}
              {userEmail && (
                <button
                  onClick={handleLogout}
                  className="text-xs text-stone-400 hover:text-stone-700"
                  title={`Signed in as ${userEmail}`}
                >
                  Sign out
                </button>
              )}
              <div className="flex items-center gap-1 ml-2">
                <button
                  className="font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors text-stone-400 hover:text-stone-700"
                  onClick={() => setViewMode("eras")}
                >
                  Eras
                </button>
                <button
                  className="font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors text-stone-700 border-b-2 border-stone-700"
                  onClick={() => setViewMode("themes")}
                >
                  Themes
                </button>
              </div>
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
          </main>
        </div>
      )}
    </>
  );
}
