Write one chapter of a personal-archive retrospective.

The era inputs are inlined below between INPUT-START / INPUT-END. Treat that block as the entirety of your authorized source material.

**Non-negotiable hard-content rule for every visible message.** If the notes contain hard mental-health material, keep all visible chat output indirect. Do not use the words "suicide", "suicidal", "self-harm", or related method/crisis specifics in narration, the visible chapter sketch, or any user-facing checkpoint. Do not quote crisis language. Use brief broad phrasing like "a hard stretch", "distress", or "mental-health crisis" only when it matters to the chapter shape. This applies to your very first comment.

1. Write the chapter to __RUN_DIR__/output.md, following the system prompt rules.
2. Also write __RUN_DIR__/thinking.md with real-time reasoning during drafting.
3. After final assembly, write __RUN_DIR__/threads.md — the structured per-era digest described in the DIGEST section of the system prompt.
4. Narrate major steps so I can follow along.

Do not list directories, read prior runs, look at sibling chapters/, or browse anywhere else in the corpus. The inlined block already contains every prior chapter and digest you're authorized to use; everything else is internal scratch and would only contaminate this draft. Write only to the paths in steps 1–3.

Start work immediately. Do not ask preamble questions.

**No preamble narration.** Don't announce what you're about to do ("I'll start by reading…", "Let me work through this…"). Skip straight to substantive observations.

**Narration register.** When you narrate to me, write in plain language for a non-technical reader. Don't mention file paths, file names (`output.md`, `thinking.md`, `threads.md`), the words "output directory" / "scratch notes" / "thinking notes" / "digest", or the internal checkpoint labels ("Chapter sketch", "Final assembly", etc.). Just say what you're doing in human terms — "I'm reading through the era and looking for the chapter shape," not "I'll write thinking.md and produce the Chapter sketch checkpoint." The file-writing is plumbing; the user doesn't need to hear about it.

**Before the first checkpoint, do not summarize the whole era in prose.** If you narrate while reading, keep it to small observations tied to a note or cluster. Save full-era claims, transitions, relationship labels, job/living situation claims, and arc summaries for the chapter sketch, where they can be cited and kept provisional.

<!-- CHECKPOINTS:START -->
Stop and ask me at these checkpoints, in order:

1. **Chapter sketch** — after reading inputs, checking the corpus themes, checking prior/future continuity context, and casting for quotes, before drafting. Do the chronology, thread, continuity, and quote checks privately in your working notes before this checkpoint. Do not turn the sketch into a locked outline. Before showing the checkpoint, write `plan.json` with this shape:
   ```
   {
     "summary": "short provisional chapter-summary paragraph",
     "chronology": [
       {
         "range": "date range",
         "phase": "place/life-phase",
         "notes": ["1-2 concrete beats"],
         "key_dates": ["YYYY-MM-DD"]
       }
     ],
     "state_of_mind": [
       {
         "claim": "provisional read of what the notes make the subject seem like",
         "evidence": ["2-4 separate dates or note clusters that support the read; do not merge them into one event"],
         "limit": "counterweight, uncertainty, or where not to overstate"
       }
     ],
     "likely_shape": "plain chronology-aware paragraph describing how the chapter will probably move",
     "other_paths_not_taken": ["optional alternate emphasis and why it should stay secondary"],
     "quote_candidates": [
       {
         "date": "YYYY-MM-DD",
         "title": "note title",
         "excerpt": "short verbatim excerpt or locator",
         "carries": "what this quote could show"
       }
     ],
     "continuity": "one sentence naming how this chapter extends, contrasts with, or quietly calibrates against prior/future context",
     "texture_only": ["up to 3 motifs noticed but not made load-bearing"]
   }
   ```
   The JSON is for the app; do not mention it to the user. Then show the checkpoint. Lead with one short **Summary** paragraph: the era's basic movement, 2-3 carrying threads, how it relates to surrounding chapters if that matters, and where it seems to land. Then show:
   - **Chronology map** — 4-6 compact beats with dates and concrete named material.
   - **State of mind** — 3 provisional pattern reads about what the notes make the subject seem like in this era. Each claim may synthesize multiple notes, but the evidence must stay separated by note/date. Do not merge details into one event, scene, causal chain, or agency claim. Include a limit or counterweight. Use "the notes make him seem..." / "the writing reads as..." rather than diagnostic certainty.
   - **Likely shape** — one plain, chronology-aware paragraph describing the chapter's probable organization. Do not make it clever. Prefer concrete life/writing material over thematic binaries.
   - **Other paths not taken** — optional, 1-2 short bullets naming alternate emphases you considered and why they should stay secondary.
   - **Quote candidates** — 8-12 short excerpts or locators, grouped loosely if useful. Cast wider than the obvious anchors; include a few weird or lower-certainty candidates when they might change the chapter's feel.
   - **Texture only** — optionally, up to 3 motifs you noticed but do not plan to make load-bearing.

   Do not print a finalized section outline. Do not present paragraph-by-paragraph detail. Goal: an exploratory map I can approve or redirect before prose begins. End the checkpoint by asking: "Reply \"approve\" to draft from this sketch, or tell me what to change." Do not draft until I reply with "approve". After I approve, continue in this same session and draft the chapter using the approved sketch, your thinking notes, and the era input you already read.

2. **Final assembly** — after drafting and revising the full chapter, before declaring done. Anything you cut, paraphrased, or were uncertain about.

After final assembly, expect post-write iteration — register/quote swaps, factual catches, paragraph rewrites, section rewrites.

Also stop mid-draft for any genuine ambiguity. Do not guess and move on.
<!-- CHECKPOINTS:END -->
