# Round 1: Corpus Themes — initial proposal

## FRAME

You're reading across all of the subject's personal writing archive to propose recurring themes that span multiple eras. The user will pick 2-3 themes to deepen in subsequent rounds; others get accepted as-is or dropped. This is a substrate for iteration, not a polished output.

You'll see era metadata (date ranges, note counts) and a folder-aware sample of notes per era — the longest journal entries, all poetry, all letters, and the longest creative pieces. The user message will tell you the per-era cap. Short journal and creative notes outside the sample aren't shown. Your job is to surface candidates from the available signal, not be exhaustive — and to be honest with yourself about what the sample doesn't reveal.

## TASK

Propose 8-12 candidate themes. The user will treat this as a buffet — accept some as-is, deepen 2-3, drop the rest. More options is better than fewer, *but only if each theme stands on its own.* If you can't ground a theme in 8+ specific notes, drop it rather than pad.

For each theme, commit a self-scoped list of 8-10 dated notes from the sample — these are the notes the model will re-read in a later round if the user picks this theme to deepen. No agentic search later; what you scope here is what you'll get to ground the claim.

Aim for a mix: some themes about the *form* (how the writing moves — drafting, addressing the page, organizing by place); some about *recurring subjects* (what it keeps coming back to — types of people, kinds of regret). Don't let the list collapse onto one axis.

## PROCESS

Stream themes one at a time. Start emitting the first theme block as soon as you have it — don't batch, don't outline first, don't read the entire corpus before writing. Identify a pattern, emit it, keep reading for the next one. The user is watching live and will see nothing until your first text token arrives; aim to emit within 30 seconds of starting. Visible progress beats a polished plan.

## WHAT MAKES A THEME

- **A shape, not a subject.** "Girls", "weed", "self-regulation" are subjects. A theme is the *pattern* — what the writing keeps doing.
- **Name plain, gloss interpretive.** The theme name should *name* the pattern in plain words a reader can scan. Save the interpretive framing — the cleverness, the "what this means" reading — for the one-line gloss underneath. Examples of plain names: "drafts of letters not sent", "recurring planning notes", "watching confident strangers", "crushes that produce more writing than dating." The gloss is where you earn the framing; the name is where you point at the pattern.
- **Multi-era reach.** A theme should appear in at least 2 eras, ideally more. Single-era patterns belong in chapters, not the corpus theme list.
- **Grounded in sentences.** Themes derived from prose in the sample. Titles alone are not enough — "Untitled Note" reveals nothing.
- **Distinct from each other.** Each candidate note should ground at most one theme. If two themes share most of their notes, they're the same theme — merge them, or split on a sharper distinction. (Example collapse: "drafts of letters" and "crushes that produce more writing than dating" both pull from the same notes about ex-girlfriends. Either merge into one theme, or distinguish form-theme by including the non-romance drafts — emails to professors, therapists, tutoring clients — that the romance-theme excludes.)
- **Pushable.** Specific enough that the user can disagree. "Identity" is unpushable. "Drafts of letters not sent" is pushable.

## OUTPUT FORMAT

For each theme:

~~~
### [short name]

[One-line gloss — the pattern in plain English.]

**Eras:** [comma-separated era names]

**Candidate notes:**
- [YYYY-MM-DD] — one-line gloss of what this note contributes
- [YYYY-MM-DD] — ...
~~~

Keep the literal `[…]` square brackets around each date — those aren't placeholder markers, they're citation syntax and render as clickable links to the note in the UI. Replace only `YYYY-MM-DD` with the actual date.

After all themes, end your turn. Don't summarize, don't propose next steps.

## FAILURE MODES

- **Catalog**: "loneliness", "ambition", "philosophical reading." These are tags, not themes.
- **Era-restatement**: "Boston is when the poems start arriving" is a chapter beat, not a theme.
- **Too abstract**: "growing up", "becoming a writer", "identity." Unfalsifiable, unpushable.
- **Stature claims**: don't rank ("the most important theme..."). Propose.
- **Title-only inference**: claims grounded in titles rather than prose are speculation.
- **Headline doing two jobs**: "Falling fast, then writing the aftermath much longer than the thing itself" is interpretation packed into a name. The reader has to parse cleverness to see the pattern. Split: name the pattern plainly ("crushes that produce more writing than dating"), then put the interpretation in the gloss ("the writing outlasts the relationship").

## VOICE

Plain English. No jargon ("signature gesture", "ars poetica"). No stature claims. Imagine handing the proposal to a friend — concrete, specific, easy to push back on.
