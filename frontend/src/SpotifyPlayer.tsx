import { useEffect, useState } from "react";
import { authHeaders, getAuthToken } from "./auth";

type Playlist = {
  id: string;
  name: string;
  image: string;
  uri: string;
  external_url: string;
};

type SpotifyPlayerProps = {
  apiBase: string;
};

const SpotifyIcon = ({ className = "w-3.5 h-3.5" }: { className?: string }) => (
  <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
    <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z" />
  </svg>
);

export function SpotifyPlayer({ apiBase }: SpotifyPlayerProps) {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  // Check connection status on mount
  useEffect(() => {
    fetch(`${apiBase}/spotify/status`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : { connected: false }))
      .then((d) => setConnected(d.connected))
      .catch(() => setConnected(false));
  }, [apiBase]);

  // Fetch playlists once when connected
  useEffect(() => {
    if (!connected) return;
    setLoading(true);
    fetch(`${apiBase}/spotify/playlists`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : { playlists: [] }))
      .then((d) => setPlaylists((d.playlists || []) as Playlist[]))
      .catch(() => setPlaylists([]))
      .finally(() => setLoading(false));
  }, [apiBase, connected]);

  // Check URL hash for spotify=connected (after OAuth redirect)
  useEffect(() => {
    if (window.location.hash.includes("spotify=connected")) {
      setConnected(true);
      window.history.replaceState(null, "", window.location.pathname);
    }
  }, []);

  // ---- Not connected ----
  if (connected === false) {
    return (
      <div className="flex justify-center pb-2">
        <button
          onClick={() => {
            const t = getAuthToken();
            window.location.href = `${apiBase}/spotify/connect?token=${encodeURIComponent(t || "")}`;
          }}
          className="flex items-center gap-1.5 text-xs font-sans text-stone-400 hover:text-[#1DB954] transition-colors"
        >
          <SpotifyIcon className="w-3 h-3" />
          connect spotify
        </button>
      </div>
    );
  }

  if (connected === null) return null;
  if (loading) {
    return (
      <div className="flex justify-center pb-2 text-xs text-stone-300 font-sans">
        <SpotifyIcon className="w-3 h-3 mr-1.5 text-stone-300" />
        loading playlists...
      </div>
    );
  }

  if (!playlists.length) return null;

  // Collapsed: just the Spotify icon + current playlist name
  if (!expanded) {
    return (
      <div className="flex justify-center pb-2">
        <button
          onClick={() => {
            setExpanded(true);
            if (!activeId && playlists.length) setActiveId(playlists[0].id);
          }}
          className="flex items-center gap-1.5 text-xs font-sans text-stone-400 hover:text-[#1DB954] transition-colors"
        >
          <SpotifyIcon className="w-3 h-3" />
          {activeId
            ? playlists.find((p) => p.id === activeId)?.name ?? "play music"
            : "play music"}
        </button>
      </div>
    );
  }

  return (
    <div className="pb-2">
      <div className="mx-6 space-y-1.5">
        <div className="flex items-center justify-center gap-2">
          <SpotifyIcon className="w-3 h-3 text-[#1DB954] shrink-0" />
          <select
            className="appearance-none bg-transparent text-xs font-sans text-stone-500 hover:text-stone-700 cursor-pointer border-0 border-b border-dotted border-stone-300 focus:outline-none pr-4"
            style={{
              backgroundImage:
                "url(\"data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 10 6'%3E%3Cpath fill='none' stroke='%23a8a29e' stroke-width='1.2' d='M1 1l4 4 4-4'/%3E%3C/svg%3E\")",
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 0 center",
            }}
            value={activeId || ""}
            onChange={(e) => setActiveId(e.target.value)}
          >
            {playlists.map((pl) => (
              <option key={pl.id} value={pl.id}>
                {pl.name}
              </option>
            ))}
          </select>
          <button
            onClick={() => setExpanded(false)}
            className="text-[10px] text-stone-300 hover:text-stone-500 ml-1"
          >
            ✕
          </button>
        </div>
        {activeId && (
          <iframe
            src={`https://open.spotify.com/embed/playlist/${activeId}?theme=0`}
            width="100%"
            height="80"
            allow="encrypted-media"
            className="rounded"
            title="Spotify player"
          />
        )}
      </div>
    </div>
  );
}
