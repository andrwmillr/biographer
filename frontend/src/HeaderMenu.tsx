import { useEffect, useRef, useState } from "react";

type HeaderMenuProps = {
  isLegacy: boolean;
  userEmail: string | null;
  hasMultipleCorpora: boolean;
  onWipe: () => void;
  onSwitchCorpus: () => void;
  onLogout: () => void;
};

export function HeaderMenu({
  isLegacy,
  userEmail,
  hasMultipleCorpora,
  onWipe,
  onSwitchCorpus,
  onLogout,
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

  const showWipe = !isLegacy;
  const showSwitch = !!userEmail && hasMultipleCorpora;
  const showLogout = !!userEmail;
  if (!showWipe && !showSwitch && !showLogout) return null;

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
          {showSwitch && (
            <button
              onClick={() => {
                setOpen(false);
                onSwitchCorpus();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-stone-700 hover:bg-stone-50"
              role="menuitem"
            >
              Switch corpus
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
          {showWipe && (showSwitch || showLogout) && (
            <div className="my-1 border-t border-stone-100" />
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
              Wipe corpus
            </button>
          )}
        </div>
      )}
    </div>
  );
}
