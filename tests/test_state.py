import sqlite3

import pytest

from tools import state


def test_state_round_trip_closes_every_connection(tmp_path, monkeypatch):
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(state, "_DB_PATH", tmp_path / "state.sqlite3")
    monkeypatch.setattr(state.sqlite3, "connect", tracking_connect)

    state._init_schema()
    state.set_mode("demo")
    state.append_signal({"symbol": "GOLD"})

    assert state.get_mode() == "demo"
    assert state.get_recent_signals(1)[0]["symbol"] == "GOLD"
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_state_write_is_committed_for_new_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_DB_PATH", tmp_path / "state.sqlite3")
    state._init_schema()
    state.set_("k", {"v": 1})

    with sqlite3.connect(tmp_path / "state.sqlite3") as raw:
        row = raw.execute("SELECT value FROM state WHERE key='k'").fetchone()
    raw.close()

    assert row == ('{"v": 1}',)


def test_stale_cursor_write_preserves_newer_independent_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_DB_PATH", tmp_path / "state.sqlite3")
    state._init_schema()
    state.set_engine_cursor({"last_tick": 1, "watchlist": ["EURUSD"]})
    stale_engine_cursor = state.get_engine_cursor()

    state.update_engine(
        demo_auto_trade={
            "enabled": True,
            "risk_pct": 0.005,
            "max_open_positions": 1,
        }
    )

    stale_engine_cursor["last_tick"] = 2
    state.set_engine_cursor(stale_engine_cursor)

    cursor = state.get_engine_cursor()
    assert cursor["last_tick"] == 2
    assert cursor["demo_auto_trade"] == {
        "enabled": True,
        "risk_pct": 0.005,
        "max_open_positions": 1,
    }
