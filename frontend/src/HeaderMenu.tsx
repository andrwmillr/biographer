import { useEffect, useRef, useState } from "react";

type HeaderMenuProps = {
  canDelete?: boolean;
  canEditChapters?: boolean;
  userEmail: string | null;
  onWipe: () => void;
  onLogout: () => void;
  onDeleteAccount: () => void;
  onEditChapters?: () => void;
};

export function HeaderMenu({
  canDelete = false,
  canEditChapters = false,
  userEmail,
  onWipe,
  onLogout,
  onDeleteAccount,
  onEditChapters,
}: HeaderMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Switch-corpus moved to the clickable corpus tag in the header (App.tsx).
  const showWipe = canDelete;
  const showLogout = !!userEmail;
  if (!showWipe && !showLogout) return null;

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="px-2 py-1 text-stone-400 hover:text-stone-700"
        aria-label="Menu"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="3" cy="8" r="1.5" />
          <circle cx="8" cy="8" r="1.5" />
          <circle cx="13" cy="8" r="1.5" />
        </svg>
      </button>
      {open && (
        <div
          className="absolute right-0 top-full z-20 mt-1 w-48 rounded border border-stone-200 bg-white py-1 shadow-md"
          role="menu"
        >
          {userEmail && (
            <div className="truncate px-3 py-1 font-mono text-[10px] text-stone-400">
              {userEmail}
            </div>
          )}
          {onEditChapters && canEditChapters && (
            <button
              onClick={() => {
                setOpen(false);
                onEditChapters();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-stone-700 hover:bg-stone-50"
              role="menuitem"
            >
              Edit chapters
            </button>
          )}
          {showLogout && (
            <button
              onClick={() => {
                setOpen(false);
                onLogout();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-stone-700 hover:bg-stone-50"
              role="menuitem"
            >
              Sign out
            </button>
          )}
          {showWipe && (
            <button
              onClick={() => {
                setOpen(false);
                onWipe();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-stone-500 hover:bg-stone-50 hover:text-red-600"
              role="menuitem"
            >
              Delete corpus
            </button>
          )}
          {!!userEmail && (
            <button
              onClick={() => {
                setOpen(false);
                onDeleteAccount();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-stone-500 hover:bg-stone-50 hover:text-red-600"
              role="menuitem"
            >
              Delete account
            </button>
          )}
        </div>
      )}
    </div>
  );
}
