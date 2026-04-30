import { useEffect, useState } from "react";
import { ErasView } from "./ErasView";
import { ImportFlow, type CorpusInfo } from "./ImportFlow";
import { ThemesView } from "./ThemesView";
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

export default function App() {
  const [error, setError] = useState<string>("");
  const [viewMode, setViewMode] = useState<"eras" | "themes">("eras");

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
        <ThemesView
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
    </>
  );
}
