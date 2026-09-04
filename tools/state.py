"""Persistent runtime state for the live engine and cockpit UI.

Stores the user's mode (offline / demo / live), the last scan tick, the
current active signal, the watch queue, and the recent signals log. Backed
by a small SQLite DB so a server restart doesn't lose state.

Why SQLite: zero-config, file-on-disk, survives restart, thread-safe.
We do NOT use this for trades (those go to the journal DB); this is just
the cockpit's view-state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent / ".cockpit_state.sqlite3"
_LOCK = threading.Lock()

# Mode constants
MODE_OFFLINE = "offline"
MODE_DEMO = "demo"
MODE_LIVE = "live"
_VALID_MODES = {MODE_OFFLINE, MODE_DEMO, MODE_LIVE}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _init_schema() -> None:
    with _LOCK, _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


_init_schema()


def get(key: str, default: Any = None) -> Any:
    """Read a value by key. Returns `default` if missing or unparseable."""
    try:
        with _LOCK, _conn() as c:
            row = c.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])
    except Exception as exc:
        logger.debug("state.get(%s) failed: %s", key, exc)
        return default


def set_(key: str, value: Any) -> None:
    """Persist a JSON-serializable value under `key`."""
    payload = json.dumps(value, default=str)
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, payload),
        )


# ─── Mode helpers ──────────────────────────────────────────────────────────────

def get_mode() -> str:
    m = get("mode", MODE_OFFLINE)
    return m if m in _VALID_MODES else MODE_OFFLINE


def set_mode(mode: str) -> str:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode: {mode!r}; expected one of {sorted(_VALID_MODES)}")
    set_("mode", mode)
    return mode


# ─── Recent signals log ───────────────────────────────────────────────────────

def append_signal(entry: dict) -> None:
    """Append a signal entry to the recent log. Keeps last 50."""
    log = get("recent_signals", [])
    entry["ts"] = int(time.time())
    log.insert(0, entry)
    set_("recent_signals", log[:50])


def get_recent_signals(n: int = 10) -> list[dict]:
    return get("recent_signals", [])[:n]


# ─── Live-engine cursor ───────────────────────────────────────────────────────

def get_engine_cursor() -> dict:
    return get("engine_cursor", {
        "last_tick": 0,
        "active_signal": None,
        "watch_queue": [],
        "watchlist": [],
    })


def set_engine_cursor(cursor: dict) -> None:
    set_("engine_cursor", cursor)


def update_engine(**kwargs: Any) -> dict:
    cur = get_engine_cursor()
    cur.update(kwargs)
    set_engine_cursor(cur)
    return cur
