import { useCallback, useEffect, useState } from "react";
import { ChatWorkspace } from "./ChatWorkspace";
import { EraTab } from "./EraTab";
import { HeaderMenu } from "./HeaderMenu";
import { ImportFlow, type CorpusInfo, type Sample } from "./ImportFlow";
import {
  authHeaders,
  clearAuthToken,
  clearSession,
  getAuthToken,
  getSession,
  setAuthToken,
  setSession,
} from "./auth";
import type { Era } from "./types";

// Backend URL resolution: ?backend=... query param > VITE_BACKEND_URL build env > same-origin (vite dev proxy).
const _backendOverride = new URLSearchParams(location.search).get("backend") ?? undefined;
const API_BASE = _backendOverride ?? import.meta.env.VITE_BACKEND_URL ?? "";
const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;

const MODELS = ["opus-4.7", "opus-4.6", "sonnet-4.6"] as const;

const MONTHS_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function formatYearMonth(ym: string | null | undefined): string {
  if (!ym || ym === "9999-99") return "";
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m || m < 1 || m > 12) return ym;
  return `${MONTHS_SHORT[m - 1]} ${y}`;
}

function formatEraRange(start: string | null, end: string | null): string {
  const s = formatYearMonth(start);
  const e = formatYearMonth(end) || "present";
  if (!s) return "";
  return `${s} – ${e}`;
}

export default function App() {
  const [error, setError] = useState<string>("");
  const [viewMode, setViewMode] = useState<"eras" | "themes">("eras");

  // ---- Auth + corpus routing ----
  // Modes: loading (bootstrap) → login (no auth) → picker (pick corpus) →
  // import (upload notes/eras) → ready (full app).
  const [corpusMode, setCorpusMode] = useState<
    "loading" | "login" | "picker" | "import" | "ready" | "error"
  >("loading");
  const [corpusInfo, setCorpusInfo] = useState<CorpusInfo | null>(null);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [userCorpora, setUserCorpora] = useState<string[]>([]);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [loginEmail, setLoginEmail] = useState<string>("");
  const [loginSent, setLoginSent] = useState<boolean>(false);
  const [loginError, setLoginError] = useState<string>("");

  // Workspace controls live in the global header now (model picker on the
  // right; era picker centered when viewMode === "eras"). State is lifted
  // here so it survives chapter remounts and so EraTab + ChatWorkspace
  // share the same selection.
  const [model, setModel] = useState<(typeof MODELS)[number]>("opus-4.7");
  const [eras, setEras] = useState<Era[]>([]);
  const [selectedEra, setSelectedEra] = useState<string | null>(null);

  const reloadEras = useCallback(async () => {
    if (!getSession()) return;
    try {
      const r = await fetch(`${API_BASE}/eras`, { headers: authHeaders() });
      if (!r.ok) return;
      const data = (await r.json()) as Era[];
      setEras(data);
      setSelectedEra((cur) => {
        if (cur && data.some((e) => e.name === cur)) return cur;
        const firstWithNotes = data.find((e) => e.note_count > 0);
        return firstWithNotes?.name ?? data[0]?.name ?? null;
      });
    } catch {
      // best-effort; the EraTab body will show its own error if it cares
    }
  }, []);

  // Refetch eras when the corpus changes.
  useEffect(() => {
    if (corpusMode !== "ready" || !corpusInfo) {
      setEras([]);
      setSelectedEra(null);
      return;
    }
    reloadEras();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corpusMode, corpusInfo?.slug]);

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

  // Best-effort fetch of the samples list. Open without auth, so we can
  // show the cards on both login and picker screens. Failures are silent —
  // the cards just don't render.
  async function refreshSamples(): Promise<void> {
    try {
      const r = await fetch(`${API_BASE}/samples`);
      if (!r.ok) return;
      setSamples((await r.json()) as Sample[]);
    } catch {
      // ignore
    }
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

      // Kick off samples fetch in the background — they're for any non-ready
      // routing destination (login or picker) and we don't want to block.
      refreshSamples();

      // 2. If a corpus session is already selected, try it first.
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
    // Anonymous sample-viewers have no auth → route them back to login
    // (which also lists the samples to pick another from).
    setCorpusMode(getAuthToken() ? "picker" : "login");
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

  // Irreversible: wipes every owned corpus + the user's auth record.
  // Two-prompt confirmation (warning + email re-entry) to make
  // misclicks unlikely.
  async function handleDeleteAccount() {
    if (!userEmail) return;
    const confirmed = window.confirm(
      `Permanently delete your account?\n\n` +
        `This irreversibly wipes every corpus you own (${userCorpora.length}) ` +
        `and removes your account record. This cannot be undone.`,
    );
    if (!confirmed) return;
    const typed = window.prompt(`Type your email (${userEmail}) to confirm:`);
    if (typed?.trim().toLowerCase() !== userEmail.toLowerCase()) {
      setError("Account deletion cancelled — email did not match.");
      return;
    }
    try {
      const r = await fetch(`${API_BASE}/auth/delete-account`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      clearAuthToken();
      clearSession();
      setCorpusInfo(null);
      setUserEmail(null);
      setUserCorpora([]);
      setLoginSent(false);
      setLoginEmail("");
      setCorpusMode("login");
    } catch (err) {
      setError(`delete failed: ${(err as Error).message}`);
    }
  }

  // Card-list of sample corpora — anonymous-readable, low-friction explore
  // path. Rendered on both the login and picker screens.
  function renderSamples(label: string) {
    if (!samples.length) return null;
    return (
      <div className="mt-8 border-t border-stone-200 pt-6">
        <p className="mb-3 text-xs uppercase tracking-wider text-stone-500">
          {label}
        </p>
        <ul className="space-y-2">
          {samples.map((s) => (
            <li key={s.slug}>
              <button
                onClick={() => handlePickCorpus(s.slug)}
                className="w-full rounded border border-stone-200 bg-white px-4 py-3 text-left hover:bg-stone-100"
              >
                <div className="font-serif text-sm text-stone-800">{s.title}</div>
                {s.description && (
                  <div className="mt-1 text-xs leading-relaxed text-stone-500">
                    {s.description}
                  </div>
                )}
                <div className="mt-1 font-mono text-[10px] text-stone-400">
                  {s.note_count.toLocaleString()} entries · {s.era_count} era
                  {s.era_count === 1 ? "" : "s"}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
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
        <div className="min-h-screen flex items-start justify-center bg-stone-50 py-16">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">Biographer</h1>
            {loginSent ? (
              <>
                <p className="text-stone-600 mb-4 text-sm leading-relaxed">
                  Check your email for a sign-in link — including your spam
                  folder, since this domain is brand new. Click the link to
                  come back here.
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
                      name="email"
                      autoComplete="email"
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
            {!loginSent && renderSamples("Or explore a sample diary")}
          </div>
        </div>
      )}
      {corpusMode === "picker" && (
        <div className="min-h-screen flex items-start justify-center bg-stone-50 py-16">
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
            {renderSamples("Sample diaries")}
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
      {corpusMode === "ready" && corpusInfo && (
        <div className="min-h-full">
          <header className="border-b border-stone-200 bg-white">
            <div className="mx-auto max-w-[120rem] px-6 py-4 flex items-center gap-4">
              <div className="flex flex-1 items-center gap-4 min-w-0">
                <h1 className="font-serif text-xl shrink-0">Biographer</h1>
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
                    Chapters
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
              </div>
              {(corpusInfo.title || corpusInfo.slug) && (
                <button
                  type="button"
                  onClick={handleSwitchCorpus}
                  className="max-w-[40ch] shrink truncate border-[3px] border-double border-stone-300 px-3 py-0.5 font-serif italic text-base text-stone-600 transition-colors hover:border-stone-400 hover:text-stone-800"
                  style={{
                    backgroundColor: "rgba(120, 113, 108, 0.04)",
                    boxShadow: "inset 0 1px 2px rgba(0,0,0,0.05)",
                  }}
                  title={`${corpusInfo.title || corpusInfo.slug} — click to switch corpus`}
                >
                  {corpusInfo.title || corpusInfo.slug}
                </button>
              )}
              <div className="flex flex-1 items-center justify-end gap-3 min-w-0">
                <select
                  name="model"
                  className="appearance-none bg-transparent border-0 border-b border-dotted border-stone-300 hover:border-stone-500 focus:border-stone-700 focus:outline-none pl-1 pr-5 py-0.5 font-sans text-xs text-stone-500 hover:text-stone-700 cursor-pointer tabular-nums"
                  style={{
                    backgroundImage:
                      "url(\"data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='none' stroke='%23a8a29e' stroke-width='1.2' d='M1 1l4 4 4-4'/%3E%3C/svg%3E\")",
                    backgroundRepeat: "no-repeat",
                    backgroundPosition: "right 2px center",
                  }}
                  value={model}
                  onChange={(e) => setModel(e.target.value as (typeof MODELS)[number])}
                  title="Model used for new sessions"
                >
                  {MODELS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                <HeaderMenu
                  isSample={corpusInfo.is_sample}
                  userEmail={userEmail}
                  onWipe={async () => {
                    if (
                      !window.confirm(
                        "Delete this corpus? This permanently removes all uploaded notes and eras from the host.",
                      )
                    ) {
                      return;
                    }
                    try {
                      await handleWipe();
                    } catch (err) {
                      setError(`wipe failed: ${(err as Error).message}`);
                    }
                  }}
                  onLogout={handleLogout}
                  onDeleteAccount={handleDeleteAccount}
                />
              </div>
            </div>
          </header>
          {viewMode === "eras" ? (
            <EraTab
              apiBase={API_BASE}
              wsBase={WS_BASE}
              eras={eras}
              selectedEra={selectedEra}
              setSelectedEra={setSelectedEra}
              model={model}
              onChapterFinalized={reloadEras}
            />
          ) : (
            <ChatWorkspace
              key="themes"
              apiBase={API_BASE}
              wsBase={WS_BASE}
              scope={{ kind: "themes" }}
              model={model}
            />
          )}
        </div>
      )}
    </>
  );
}
