"""Persistent runtime state for the live engine and cockpit UI.

Stores the user's mode (offline / demo), the last scan tick, the
current active signal, the watch queue, and the recent signals log. Backed
by a small SQLite DB so a server restart doesn't lose state.

Why SQLite: zero-config, file-on-disk, survives restart, thread-safe.
We do NOT use this for trades (those go to the journal DB); this is just
the cockpit's view-state.
"""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent / ".cockpit_state.sqlite3"
_LOCK = threading.Lock()

# Mode constants
MODE_OFFLINE = "offline"
MODE_DEMO = "demo"
_VALID_MODES = {MODE_OFFLINE, MODE_DEMO}
_INDEPENDENT_CURSOR_KEYS = frozenset({"demo_auto_trade"})


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    # sqlite3's own context manager only commits; it never closes.
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=5.0)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


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
        raise ValueError(
            f"invalid mode: {mode!r}; expected one of {sorted(_VALID_MODES)}"
        )
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
    return get(
        "engine_cursor",
        {
            "last_tick": 0,
            "active_signal": None,
            "watch_queue": [],
            "watchlist": [],
        },
    )


def set_engine_cursor(cursor: dict) -> None:
    """Merge an engine snapshot without overwriting endpoint-owned fields."""
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT value FROM state WHERE key = ?", ("engine_cursor",)
        ).fetchone()
        current = json.loads(row["value"]) if row is not None else {}
        current.update(
            (key, value)
            for key, value in cursor.items()
            if key not in _INDEPENDENT_CURSOR_KEYS
        )
        c.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("engine_cursor", json.dumps(current, default=str)),
        )


def update_engine(**kwargs: Any) -> dict:
    """Atomically update fields owned by API endpoints or UI controls."""
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT value FROM state WHERE key = ?", ("engine_cursor",)
        ).fetchone()
        current = json.loads(row["value"]) if row is not None else {}
        current.update(kwargs)
        c.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("engine_cursor", json.dumps(current, default=str)),
        )
    return current
