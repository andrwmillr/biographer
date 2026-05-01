export type Note = {
  rel: string;
  date: string;
  title: string;
  label: string;
  source: string;
  body: string;
  editor_note?: string;
};

export type Era = {
  name: string;
  start: string;
  end: string | null;
  note_count: number;
  has_chapter: boolean;
};

export type LogItem =
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

// Phase progression of the workspace UI:
//   pre-gen   → top prompter + notes grid below
//   generating → same layout, prompter shows "thinking…"
//   iterating → three-pane: chat | draft | notes
//   finalized → same three-pane, draft locked + prompter disabled
export type Phase = "pre-gen" | "generating" | "iterating" | "finalized";

// Determines WS path + start payload + notes endpoint.
export type WorkspaceScope =
  | { kind: "era"; era: string; future: boolean }
  | { kind: "themes"; topN: number };

// Server-emitted spawned event. Common fields plus scope-specific extras.
export type SpawnedInfo = {
  run_dir: string;
  model: string;
  input_chars: number;
  // eras-only
  era?: string;
  notes?: number;
  prior_chapters?: number;
  prior_digests?: number;
  future_chapters?: number;
  future_digests?: number;
  // themes-only
  top_n?: number;
};

export type FinalizedInfo = {
  content: string;
  location: string;
  words: number;
  overwritten: boolean;
};
