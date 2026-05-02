"""Append-only JSONL telemetry.

One function: `log(event, **fields)`. Appends a single JSON line to
_telemetry/events.jsonl. Never blocks the caller on failure — if the
write fails (permissions, disk full), it's silently dropped.

Read with: cat _telemetry/events.jsonl | jq .
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from api import config


def log(event: str, **fields) -> None:
    """Append one event to the telemetry log."""
    record = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": time.time(),
        **fields,
    }
    try:
        config.TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.TELEMETRY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
