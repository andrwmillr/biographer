export type Note = {
  rel: string;
  date: string;
  title: string;
  label: string;
  source: string;
  body: string;
  editor_note?: string;
  sampled?: boolean;
  highlighted?: boolean;
};

export type Era = {
  name: string;
  start: string | null;
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

// Phase progression of the workspace UI. The 3-pane layout (chat | draft |
// notes) is always rendered; phase only governs prompter/draft state.
//   pre-gen    → prompter accepts a kickoff, draft pane empty
//   generating → prompter shows "thinking…", chat streams
//   iterating  → prompter accepts revisions, draft updating
//   finalized  → draft locked + prompter disabled
export type Phase = "pre-gen" | "generating" | "iterating" | "finalized";

// Determines WS path + start payload + notes endpoint. Drafting always
// includes future-era hindsight context; themes always uses the top-5
// folder-aware sample (the parameters that used to live in this scope
// are now baked into the protocol).
export type WorkspaceScope =
  | { kind: "era"; era: string; eraStart: string | null; eraEnd: string | null }
  | { kind: "themes" }
  | { kind: "preface" }
  | { kind: "commonplace" };

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
  // commonplace-only
  sampled_count?: number;
  seen_before?: number;
  total_eligible?: number;
};

export type FinalizedInfo = {
  content: string;
  location: string;
  words: number;
  overwritten: boolean;
};
