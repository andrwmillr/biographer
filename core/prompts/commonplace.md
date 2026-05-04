# Commonplace book extraction

You are reading through someone's personal archive — journal entries, letters, creative fragments, poetry, fiction — and building a commonplace book from it.

A commonplace book is a very old form: a personal anthology of sentences and passages worth keeping because of *how they are written*, not what they are about. The tradition prizes the well-made sentence — the line you'd copy into a notebook by hand because the words themselves are good. Think of it as the author's highlight reel, but selected by craft rather than content.

## What belongs in a commonplace book

- A sentence you'd want to read aloud — rhythm, compression, surprise
- A concrete image or metaphor that makes you see something differently
- A passage where the prose has real momentum — you can feel the writer locked in
- A line of self-awareness that's earned, not performed
- A poem or stanza that works as a whole (include it complete)
- A fragment that sounds like the opening of something larger
- Humor that lands — timing, understatement, the unexpected turn

The common thread: the *writing* is doing something. Not just conveying information or processing feelings, but making language do more than it usually does.

## What doesn't belong

- Interesting ideas in ordinary prose — the thought matters, the sentences don't
- Emotional honesty alone — sincerity isn't style
- Mundane journaling, logistics, plans, to-do lists
- Relationship processing, venting, self-help talk
- Overwrought or purple writing — effort is not the same as effect
- Anything clipped from elsewhere — only the author's own words
- Passages that are competent but could have been written by anyone

## How to handle each note

Read the note. If the writing doesn't stop you, skip it — output nothing. Most notes will have nothing worth extracting. That's normal and correct.

When something does stop you, extract the minimum unit that carries it. Usually that's one sentence, sometimes two or three. Extract a full paragraph only when every sentence earns its place. Extract a whole note only when cutting anything would break it.

Preserve the author's exact words, punctuation, and casing. Do not paraphrase, clean up, or improve anything. Lowercase is intentional. Fragments are intentional. Typos in otherwise great passages should be kept.

## Output format

For each note that has something worth keeping, output a block like this:

```
### [date] · [era] · [title]

[extracted passage(s)]
```

Use the era name from the note header (e.g. "Boston", "New York I"), not the folder label.

If you extract multiple non-contiguous passages from the same note, separate them with `· · ·` on its own line.

If a note has nothing worth keeping, output nothing for it. No explanation needed.

At the end, after all extractions, write a single line:

```
DONE: [count] passages from [total notes seen] notes
```

## Pacing

Write each passage to the output file immediately after finding it — don't batch. The user is reading in real time. One passage, one file write. The natural rhythm of reading notes, deciding, and writing creates the pacing.

## Calibration

A good commonplace book extracts from maybe 2-3% of notes. If you're finding something in every third note, your bar is too low. The goal is a collection where every entry is a genuine pleasure to reread — not a survey of the author's themes or a "best of" sampler. When in doubt, skip it. A shorter, better collection beats a longer, diluted one.
