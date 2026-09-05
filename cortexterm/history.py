from __future__ import annotations

import json

from cortexterm.config import CORTEXTERM_DIR, CORTEXTERM_HISTORY_PATH


def load_history_entries() -> list[str]:
    try:
        if not CORTEXTERM_HISTORY_PATH.exists():
            return []
        parsed = json.loads(CORTEXTERM_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = parsed.get("entries", [])
    return [str(entry) for entry in entries] if isinstance(entries, list) else []


def save_history_entries(entries: list[str]) -> None:
    try:
        CORTEXTERM_DIR.mkdir(parents=True, exist_ok=True)
        CORTEXTERM_HISTORY_PATH.write_text(
            json.dumps({"entries": entries[-200:]}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # History is a convenience feature.  A full home drive or locked config
        # directory must not make the assistant unusable.
        return
 
