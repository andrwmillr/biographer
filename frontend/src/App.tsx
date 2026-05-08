import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChapterEditor, type ChapterDraft } from "./ChapterEditor";
import { ChatWorkspace } from "./ChatWorkspace";
import { CommonplaceWorkspace } from "./CommonplaceWorkspace";
import { EraTab } from "./EraTab";
import { HeaderMenu } from "./HeaderMenu";
import { ImportFlow, type CorpusInfo, type Sample } from "./ImportFlow";
import {
  authHeaders,
  clearAuthToken,
  clearSession,
  getAuthToken,
  getCorpusSecret,
  getSession,
  setAuthToken,
  setCorpusSecret,
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
  const [magicLinkLanded, setMagicLinkLanded] = useState(false);
  const [viewMode, _setViewMode] = useState<"eras" | "themes" | "commonplace">(() => {
    const stored = localStorage.getItem("viewMode");
    return stored === "eras" ? "eras" : stored === "themes" ? "themes" : "commonplace";
  });
  const setViewMode = (v: "eras" | "themes" | "commonplace") => {
    _setViewMode(v);
    localStorage.setItem("viewMode", v);
  };
  const applyCorpusInfo = (info: CorpusInfo) => {
    setCorpusInfo(info);
    if (info.slug === "c_poems") {
      setViewMode("commonplace");
    }
  };

  // ---- Auth + corpus routing ----
  // Modes: loading (bootstrap) → login (no auth) → picker (pick corpus) →
  // import (upload notes/eras) → ready (full app).
  const [corpusMode, setCorpusMode] = useState<
    "loading" | "login" | "picker" | "import" | "ready" | "error"
  >("loading");
  const [corpusInfo, setCorpusInfo] = useState<CorpusInfo | null>(null);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [userCorpora, setUserCorpora] = useState<
    { slug: string; title: string | null }[]
  >([]);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [loginEmail, setLoginEmail] = useState<string>("");
  const [loginSent, setLoginSent] = useState<boolean>(false);
  const [loginError, setLoginError] = useState<string>("");

  // Workspace controls live in the global header now (model picker on the
  // right; the "Chapters" tab doubles as the era selector dropdown when
  // active). State is lifted here so it survives chapter remounts and so
  // EraTab + ChatWorkspace share the same selection.
  const [model, _setModel] = useState<(typeof MODELS)[number]>(() => {
    const stored = localStorage.getItem("model");
    return MODELS.includes(stored as any) ? (stored as (typeof MODELS)[number]) : "sonnet-4.6";
  });
  const setModel = (v: (typeof MODELS)[number]) => {
    _setModel(v);
    localStorage.setItem("model", v);
  };
  const [eras, setEras] = useState<Era[]>([]);
  const [selectedEra, _setSelectedEra] = useState<string | null>(() => {
    const slug = getSession();
    return slug
      ? localStorage.getItem(`selectedEra_${slug}`)
      : null;
  });
  const setSelectedEra = (v: string | null | ((prev: string | null) => string | null)) => {
    _setSelectedEra((prev) => {
      const next = typeof v === "function" ? v(prev) : v;
      const slug = getSession();
      if (slug && next) localStorage.setItem(`selectedEra_${slug}`, next);
      return next;
    });
  };
  // Stable reference so ChatWorkspace's scope-dependent effects don't
  // re-fire on every App render (object literals fail Object.is checks).
  const themesScope = useMemo(() => ({ kind: "themes" as const }), []);
  const [chaptersOpen, setChaptersOpen] = useState<boolean>(false);
  const chaptersMenuRef = useRef<HTMLDivElement | null>(null);
  const [chapterEditorOpen, setChapterEditorOpen] = useState(false);
  const [chapterEditorMonths, setChapterEditorMonths] = useState<string[]>([]);
  const [chapterEditorSaving, setChapterEditorSaving] = useState(false);

  // Close the chapters dropdown on outside click / Escape.
  useEffect(() => {
    if (!chaptersOpen) return;
    function onDocClick(e: MouseEvent) {
      if (
        chaptersMenuRef.current &&
        !chaptersMenuRef.current.contains(e.target as Node)
      ) {
        setChaptersOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setChaptersOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [chaptersOpen]);

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
  async function refreshUser(): Promise<{
    email: string;
    corpora: { slug: string; title: string | null }[];
  } | null> {
    if (!getAuthToken()) return null;
    const r = await fetch(`${API_BASE}/auth/me`, { headers: authHeaders() });
    if (r.status === 401) {
      clearAuthToken();
      return null;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    const me = (await r.json()) as {
      email: string;
      corpora: { slug: string; title: string | null }[];
    };
    setUserEmail(me.email);
    setUserCorpora(me.corpora);
    return me;
  }

  async function renameCorpus(slug: string, currentTitle: string | null) {
    const next = window.prompt(
      "Rename this corpus:",
      currentTitle ?? "",
    );
    if (next === null) return; // user cancelled
    const trimmed = next.trim();
    const tok = getAuthToken();
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-Corpus-Session": slug,
    };
    if (tok) headers["X-Auth-Token"] = tok;
    try {
      const r = await fetch(`${API_BASE}/corpus`, {
        method: "PATCH",
        headers,
        body: JSON.stringify({ title: trimmed || null }),
      });
      if (!r.ok) {
        setError(`rename failed: HTTP ${r.status}: ${await r.text()}`);
        return;
      }
      await refreshUser();
      // If the renamed corpus is the one currently open, update its title
      // in corpusInfo so the header tag reflects the change immediately.
      if (corpusInfo?.slug === slug) {
        setCorpusInfo({ ...corpusInfo, title: trimmed || null });
      }
    } catch (e) {
      setError(`rename failed: ${(e as Error).message}`);
    }
  }

  async function refreshSamples(): Promise<void> {
    try {
      const headers: Record<string, string> = {};
      const secret = getCorpusSecret();
      if (secret) headers["X-Corpus-Secret"] = secret;
      const r = await fetch(`${API_BASE}/samples`, { headers });
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
      //    Show a "you can close this tab" interstitial — the original tab
      //    picks up the token via the storage event listener and transitions
      //    automatically. The user can also click "continue here" to proceed.
      if (location.hash.startsWith("#auth=")) {
        const token = decodeURIComponent(location.hash.slice("#auth=".length));
        if (token) setAuthToken(token);
        history.replaceState(null, "", location.pathname + location.search);
        setMagicLinkLanded(true);
        return;
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
          applyCorpusInfo(info);
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

  useEffect(() => {
    if (corpusMode !== "picker") return;
    refreshSamples();
    const TARGET = (import.meta.env.VITE_EASTER_EGG_TRIGGER || "").toLowerCase();
    const SECRET = import.meta.env.VITE_CORPUS_SECRET || "";
    if (!TARGET || !SECRET) return;
    let buffer = "";
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      buffer += e.key.toLowerCase();
      if (buffer.length > TARGET.length) buffer = buffer.slice(-TARGET.length);
      if (buffer === TARGET) {
        buffer = "";
        setCorpusSecret(SECRET);
        refreshSamples();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [corpusMode]);

  // Cross-tab auth: when the magic link opens in a new tab, it writes
  // authToken to localStorage. The `storage` event fires in THIS tab,
  // so we can re-bootstrap without the user having to manually switch
  // back and refresh.
  useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key !== "authToken" || !e.newValue) return;
      if (corpusMode !== "login") return;
      // Re-run the same bootstrap logic as the mount effect.
      (async () => {
        const me = await refreshUser();
        if (!me) return;
        setCorpusMode(me.corpora.length === 0 ? "import" : "picker");
      })().catch(() => {});
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [corpusMode]);

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
    localStorage.removeItem("corpusSecret");
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
      applyCorpusInfo(info);
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

  async function openChapterEditor() {
    // Fetch note months for client-side count recomputation
    try {
      const r = await fetch(`${API_BASE}/chapters/note-months`, {
        headers: authHeaders(),
      });
      if (r.ok) {
        const data = await r.json();
        setChapterEditorMonths(data.months);
      }
    } catch {}
    setChapterEditorOpen(true);
  }

  async function handleChapterEditorConfirm(chapters: ChapterDraft[]) {
    setChapterEditorSaving(true);
    try {
      const r = await fetch(`${API_BASE}/chapters/save`, {
        method: "PUT",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          chapters: chapters.map((ch) => ({
            name: ch.name,
            start: ch.start,
          })),
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      setChapterEditorOpen(false);
      await reloadEras();
    } catch (err) {
      setError(`save chapters failed: ${(err as Error).message}`);
    } finally {
      setChapterEditorSaving(false);
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
    const main = samples.filter((s) => s.slug !== "c_poems");
    const bonus = samples.filter((s) => s.slug === "c_poems");
    function sampleCard(s: Sample) {
      return (
        <button
          key={s.slug}
          onClick={() => handlePickCorpus(s.slug)}
          className="w-full rounded border border-stone-200 bg-white px-4 py-3 text-left hover:bg-stone-100 flex flex-col"
        >
          <div className="font-serif text-sm text-stone-800">{s.title}</div>
          {s.description && (
            <div className="mt-1 text-xs leading-relaxed text-stone-500 flex-1">
              {s.description}
            </div>
          )}
          <div className="mt-1 font-mono text-[10px] text-stone-400">
            {s.note_count.toLocaleString()} entries
            {s.era_count > 0 && <> · {s.era_count} era{s.era_count === 1 ? "" : "s"}</>}
          </div>
        </button>
      );
    }
    return (
      <div className="mt-8 border-t border-stone-200 pt-6 w-[150%] -ml-[25%]">
        <p className="mb-3 text-xs uppercase tracking-wider text-stone-500">
          {label}
        </p>
        <div className="grid grid-cols-2 gap-2">
          {main.map(sampleCard)}
        </div>
        {bonus.length > 0 && (
          <div className="mt-2 grid grid-cols-2 gap-2">
            {bonus.map(sampleCard)}
          </div>
        )}
      </div>
    );
  }

  function continueHere() {
    setMagicLinkLanded(false);
    refreshSamples();
    (async () => {
      const slug = getSession();
      if (slug) {
        const r = await fetch(`${API_BASE}/corpus`, { headers: authHeaders() });
        if (r.ok) {
          const info = (await r.json()) as CorpusInfo;
          applyCorpusInfo(info);
          setCorpusMode(info.has_eras ? "ready" : "import");
          refreshUser().catch(() => {});
          return;
        }
        if (r.status === 401 || r.status === 403) clearSession();
      }
      const me = await refreshUser();
      if (!me) { setCorpusMode("login"); return; }
      setCorpusMode(me.corpora.length === 0 ? "import" : "picker");
    })().catch(() => setCorpusMode("login"));
  }

  if (magicLinkLanded) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-stone-50">
        <div className="max-w-sm text-center p-8">
          <h1 className="font-serif text-xl mb-3">Signed in</h1>
          <p className="text-stone-600 text-sm mb-6 leading-relaxed">
            You can close this tab and return to where you were — it's
            already updated.
          </p>
          <button
            onClick={continueHere}
            className="text-sm text-stone-500 underline hover:text-stone-700"
          >
            Or continue here
          </button>
        </div>
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
                  Sign in with your email to import notes or return to a
                  corpus you've already uploaded.
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
            {!loginSent && renderSamples("Sample diaries")}
          </div>
        </div>
      )}
      {corpusMode === "picker" && (
        <div className="min-h-screen flex items-start justify-center bg-stone-50 py-16">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">Welcome back</h1>
            <p className="text-stone-600 mb-6 text-sm leading-relaxed">
              Signed in as{" "}
              <span className="font-mono text-xs">{userEmail}</span>.
            </p>
            <p className="mb-3 text-xs uppercase tracking-wider text-stone-500">
              Your corpora
            </p>
            <ul className="space-y-2">
              {userCorpora.map(({ slug, title }) => (
                <li
                  key={slug}
                  className="group relative rounded border border-stone-200 bg-white hover:bg-stone-100 transition-colors"
                >
                  <button
                    onClick={() => handlePickCorpus(slug)}
                    className="block w-full text-left px-4 py-3"
                  >
                    <span className="block text-sm text-stone-900">
                      {title || slug}
                    </span>
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      renameCorpus(slug, title);
                    }}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-[11px] text-stone-400 underline opacity-0 group-hover:opacity-100 hover:text-stone-700"
                    title="Rename this corpus"
                  >
                    rename
                  </button>
                </li>
              ))}
            </ul>
            <div className="mt-6 flex items-center gap-4">
              <button
                onClick={handleImportAnother}
                className="text-sm text-stone-700 underline hover:text-stone-900"
              >
                + Import notes
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
        <div className="min-h-screen flex items-start justify-center bg-stone-50 py-16">
          <div className="max-w-md w-full p-8">
            <h1 className="font-serif text-2xl mb-2">
              {userEmail ? "Welcome back" : "Biographer"}
            </h1>
            {userEmail ? (
              <p className="text-stone-600 mb-6 text-sm leading-relaxed">
                Signed in as{" "}
                <span className="font-mono text-xs">{userEmail}</span>. Import
                notes below, or browse a public example.
              </p>
            ) : (
              <p className="text-stone-600 mb-6 text-sm leading-relaxed">
                Sign in to save imported corpora to your account, or browse a
                public example.
              </p>
            )}
            <ImportFlow
              key={corpusInfo?.slug ?? "fresh"}
              apiBase={API_BASE}
              initialInfo={corpusInfo}
              onComplete={(info) => {
                applyCorpusInfo(info);
                setCorpusMode("ready");
              }}
              onWipe={handleWipe}
            />
            {userEmail && (
              <div className="mt-6 flex items-center gap-4">
                {userCorpora.length > 0 && (
                  <button
                    onClick={() => setCorpusMode("picker")}
                    className="text-sm text-stone-700 underline hover:text-stone-900"
                  >
                    ← Back to my corpora
                  </button>
                )}
                <button
                  onClick={handleLogout}
                  className="ml-auto text-xs text-stone-500 hover:text-stone-700 underline"
                >
                  Sign out
                </button>
              </div>
            )}
            {renderSamples("Sample diaries")}
          </div>
        </div>
      )}
      {corpusMode === "ready" && corpusInfo && (
        <div className="flex flex-col h-screen">
          <header className="border-b border-stone-200 bg-white">
            <div className="mx-auto max-w-[120rem] px-6 py-4 flex items-center gap-4">
              <div className="flex flex-1 items-center gap-4 min-w-0">
                <h1 className="font-serif text-xl shrink-0">Biographer</h1>
                <div className="flex items-center gap-1 ml-2">
                  <button
                    className={
                      "font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors " +
                      (viewMode === "commonplace"
                        ? "text-stone-700 border-b-2 border-stone-700"
                        : "text-stone-400 hover:text-stone-700")
                    }
                    onClick={() => setViewMode("commonplace")}
                  >
                    Curate
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
                    Outline
                  </button>
                  <div className="relative" ref={chaptersMenuRef}>
                    <button
                      className={
                        "font-sans text-xs uppercase tracking-wider px-3 py-1.5 transition-colors " +
                        (viewMode === "eras"
                          ? "text-stone-700 border-b-2 border-stone-700"
                          : "text-stone-400 hover:text-stone-700")
                      }
                      onClick={() => {
                        if (viewMode !== "eras") {
                          setViewMode("eras");
                          return;
                        }
                        setChaptersOpen((o) => !o);
                      }}
                      aria-haspopup={viewMode === "eras" ? "menu" : undefined}
                      aria-expanded={viewMode === "eras" ? chaptersOpen : undefined}
                    >
                      Write
                      {viewMode === "eras" && (
                        <svg className="ml-1 inline-block w-3 h-3 text-stone-600" viewBox="0 0 20 20" fill="currentColor"><path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" /></svg>
                      )}
                    </button>
                    {viewMode === "eras" && chaptersOpen && eras.length > 0 && (
                      <div
                        role="menu"
                        className="absolute left-0 top-full z-20 mt-1 min-w-[20rem] rounded border border-stone-200 bg-white py-1 shadow-md"
                      >
                        {(() => {
                          const allHaveChapters = eras.filter(e => e.note_count > 0).every(e => e.has_chapter);
                          return (
                            <button
                              role="menuitem"
                              disabled={!allHaveChapters}
                              onClick={() => {
                                setSelectedEra("__preface__");
                                setChaptersOpen(false);
                              }}
                              className={
                                "block w-full px-3 py-1.5 text-left font-serif text-sm transition-colors " +
                                (!allHaveChapters
                                  ? "text-stone-300 cursor-not-allowed"
                                  : selectedEra === "__preface__"
                                    ? "text-stone-900 font-semibold"
                                    : "text-stone-700 hover:bg-stone-50")
                              }
                              title={allHaveChapters ? undefined : "Draft all chapters first"}
                            >
                              Preface
                            </button>
                          );
                        })()}
                        {eras.map((e) => {
                          const range = formatEraRange(e.start, e.end);
                          const disabled = e.note_count === 0;
                          const active = e.name === selectedEra;
                          return (
                            <button
                              key={e.name}
                              role="menuitem"
                              disabled={disabled}
                              onClick={() => {
                                setSelectedEra(e.name);
                                setChaptersOpen(false);
                              }}
                              className={
                                "block w-full px-3 py-1.5 text-left font-serif text-sm transition-colors " +
                                (disabled
                                  ? "text-stone-300 cursor-not-allowed"
                                  : active
                                    ? "text-stone-900 font-semibold"
                                    : "text-stone-700 hover:bg-stone-50")
                              }
                            >
                              <span>{e.name}</span>
                              {range && (
                                <span className="ml-2 font-sans text-[11px] text-stone-400 not-italic">
                                  {range}
                                </span>
                              )}
                              {disabled && (
                                <span className="ml-2 font-sans text-[11px] text-stone-300 not-italic">
                                  empty
                                </span>
                              )}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
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
                <HeaderMenu
                  canDelete={corpusInfo.access?.can_delete ?? !corpusInfo.is_sample}
                  canEditChapters={corpusInfo.access?.can_write ?? !corpusInfo.is_sample}
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
                  onEditChapters={openChapterEditor}
                />
              </div>
            </div>
          </header>
          <div className={viewMode === "eras" ? "flex-1 min-h-0 flex flex-col" : "hidden"}>
            <EraTab
              apiBase={API_BASE}
              wsBase={WS_BASE}
              eras={eras}
              selectedEra={selectedEra}
              model={model}
              models={MODELS}
              onModelChange={(m) => setModel(m as (typeof MODELS)[number])}
              onChapterFinalized={reloadEras}
              canCompute={corpusInfo.access?.can_compute ?? true}
            />
          </div>
          <div className={viewMode === "themes" ? "flex-1 min-h-0 flex flex-col" : "hidden"}>
            <ChatWorkspace
              key="themes"
              apiBase={API_BASE}
              wsBase={WS_BASE}
              scope={themesScope}
              model={model}
              models={MODELS.filter((m) => m !== "opus-4.7")}
              onModelChange={(m) => setModel(m as (typeof MODELS)[number])}
              canCompute={corpusInfo.access?.can_compute ?? true}
            />
          </div>
          <div className={viewMode === "commonplace" ? "flex-1 min-h-0 flex flex-col" : "hidden"}>
            <CommonplaceWorkspace
              apiBase={API_BASE}
              wsBase={WS_BASE}
              model={model}
              readOnly={!(corpusInfo.access?.can_write ?? false)}
            />
          </div>
          {chapterEditorOpen && (
            <div
              className="fixed inset-0 z-50 flex items-center justify-center bg-stone-900/40 p-4"
              onClick={(e) => {
                if (e.target === e.currentTarget) setChapterEditorOpen(false);
              }}
            >
              <div className="w-full max-w-lg max-h-[80vh] overflow-auto rounded-lg border border-stone-200 bg-white p-6 shadow-2xl">
                <ChapterEditor
                  initial={eras.map((e) => ({
                    id: crypto.randomUUID(),
                    name: e.name,
                    start: e.start || "0000-00",
                    note_count: e.note_count,
                  }))}
                  noteMonths={chapterEditorMonths}
                  onConfirm={handleChapterEditorConfirm}
                  onCancel={() => setChapterEditorOpen(false)}
                  saving={chapterEditorSaving}
                />
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}
