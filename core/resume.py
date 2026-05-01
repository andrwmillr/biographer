"""Loose generic resume helper — read whatever artifacts exist in a
run_dir and build a kickoff string that lets a fresh SDK session pick
up from where the prior one left off.

Two workflow shapes today:
  - era    : run_dir has user.md (original input) + output.md (current
             draft) + optionally thinking.md / threads.md (process notes)
  - themes : run_dir has input.md (corpus sample) + state.md (most
             recent agent response, captured per turn)

This module is intentionally not a full Workflow abstraction — just
the minimum scaffolding both flows need. When a third workflow type
arrives, this will likely need a real interface; for now, two
shape-specific branches are clearer than premature generalization.
"""
from __future__ import annotations

from pathlib import Path

from core import corpus as wb


def _read_safe(path: Path) -> str:
    """Read a file if it exists, return '' otherwise. Saves callers from
    sprinkling `if path.exists():` guards."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def build_era_resume_kickoff(run_dir: Path, corpus_id: str | None) -> str:
    """Reconnect kickoff for an era chapter session. Reads the original
    user.md (era notes + prior context, written at session start) and
    the agent's current output.md / thinking.md drafts."""
    original_input = _read_safe(run_dir / "user.md")
    current_draft = _read_safe(run_dir / "output.md")
    thinking = _read_safe(run_dir / "thinking.md")

    parts: list[str] = [wb.subject_context_for(corpus_id)]
    parts.append(
        "**This is a resumed session.** A previous session was working on this "
        "era's chapter and disconnected before locking. Pick up from where the "
        "draft left off.\n\n"
    )
    if current_draft.strip():
        parts.append(
            "Current draft on disk (output.md). Continue editing this — don't "
            "start over:\n\n--- CURRENT DRAFT ---\n\n"
            + current_draft
            + "\n\n--- END CURRENT DRAFT ---\n\n"
        )
    else:
        parts.append(
            "No draft has been written yet — start a fresh chapter using the "
            "era inputs below.\n\n"
        )
    if thinking.strip():
        parts.append(
            "Prior session's thinking notes (thinking.md):\n\n"
            "--- PRIOR THINKING ---\n\n"
            + thinking
            + "\n\n--- END PRIOR THINKING ---\n\n"
        )
    if original_input.strip():
        parts.append(
            "Original era inputs (notes + prior chapters):\n\n"
            "--- ORIGINAL INPUT ---\n\n"
            + original_input
            + "\n\n--- END ORIGINAL INPUT ---\n"
        )
    parts.append(
        "\nWait for the user's next message — they may have a specific "
        "direction in mind. If they don't, give a one-line orientation "
        "of where the draft currently stands and ask what they want next."
    )
    return "".join(parts)


def build_themes_resume_kickoff(run_dir: Path, corpus_id: str | None) -> str:
    """Reconnect kickoff for a themes curate session. Reads the original
    input.md (corpus sample) and the captured state.md (most recent
    agent response from prior session, including the `## Current state`
    block)."""
    original_input = _read_safe(run_dir / "input.md")
    state = _read_safe(run_dir / "state.md")

    parts: list[str] = [wb.subject_context_for(corpus_id)]
    parts.append(
        "**This is a resumed session.** A previous curate session was "
        "working on this corpus's themes and disconnected before locking. "
        "Pick up from where it left off.\n\n"
    )
    if state.strip():
        parts.append(
            "Most recent agent response from the prior session (which "
            "starts with the `## Current state` block — this IS your "
            "starting state):\n\n"
            "--- PRIOR STATE ---\n\n"
            + state
            + "\n\n--- END PRIOR STATE ---\n\n"
        )
    else:
        parts.append(
            "No prior state captured — the previous session disconnected "
            "before producing any output. Start fresh: generate round-1 "
            "themes from the corpus sample below, then enter curate mode.\n\n"
        )
    if original_input.strip():
        parts.append(
            "Original corpus sample (your full source material):\n\n"
            "--- INPUT-START ---\n\n"
            + original_input
            + "\n\n--- INPUT-END ---\n"
        )
    parts.append(
        "\nEmit your next response starting with the `## Current state` "
        "block reflecting where the curation stands now, then end with "
        '"Ready for your moves." Wait for the user.'
    )
    return "".join(parts)
