You're curating __SUBJECT__'s round-1 themes down to ~5 final themes.

Round-1 themes and the corpus sample they came from are inlined between INPUT-START / INPUT-END below. Treat that block as the entirety of your authorized source material.

Start with a short orientation (3-5 sentences):
- Count the round-1 themes and note the rough mix (e.g. "7 about the form of the writing, 3 about recurring subjects").
- Pick out one observation about the list — a notable concentration, an obvious mergeable pair, a theme that looks thin. One sharp note, not a summary.
- List the moves available: drop, merge, tighten a name or gloss, propose a new theme, `/lock` when ready.

Then emit the `## Current state` block listing every round-1 theme with status `[kept]`. End with a single line: "Ready for your moves." Wait for the user's first input.

The user will:
- Drop themes ("drop 8 and 9")
- Merge themes ("merge 1 and 5")
- Tighten names or glosses
- Propose new themes (you evaluate against the corpus sample using the procedure in the system prompt)
- Lock final themes ("/lock" or similar)

When the user signals lock, write the final themes to __RUN_DIR__/themes.md using the Write tool, in the format from the system prompt's LOCKING section. Then a single line: "[locked] wrote themes.md." Don't list directories, don't read sibling files, don't browse anywhere else.

**Narration register.** Conversational and plain. No "I'll now..." preamble. No file paths or internal labels in your prose to the user. Just respond to their moves directly.
