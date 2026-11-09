import pytest

from tools import scan_server, state
from tools.pattern_evaluation_store import EvaluationRecorder
from tools.pattern_journal import JournalRecorder
from tools.pattern_notifications import PatternNotifier


@pytest.fixture(autouse=True)
def isolated_cockpit_state(tmp_path, monkeypatch):
    """Keep tests off the developer's real cockpit database."""
    monkeypatch.setattr(state, "_DB_PATH", tmp_path / "cockpit_state.sqlite3")
    state._init_schema()


@pytest.fixture(autouse=True)
def isolated_candle_cache(monkeypatch):
    """Each broker stub starts with its own candle history."""
    monkeypatch.setattr(scan_server, "_CANDLE_CACHE", {})


@pytest.fixture(autouse=True)
def isolated_pattern_notifications(monkeypatch):
    """Never inherit the user's real notification destination in tests."""
    monkeypatch.setattr(
        scan_server, "_PATTERN_NOTIFIER", PatternNotifier(False, "", "")
    )
    monkeypatch.setattr(scan_server, "_PATTERN_NOTIFY_TASK", None)


@pytest.fixture(autouse=True)
def isolated_pattern_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scan_server, "_PATTERN_JOURNAL", JournalRecorder(tmp_path / "pattern.sqlite3")
    )
    monkeypatch.setattr(scan_server, "_PATTERN_JOURNAL_TASK", None)


@pytest.fixture(autouse=True)
def isolated_pattern_evaluation(tmp_path, monkeypatch):
    """Keep prospective capture and comparison reports off real evidence."""
    monkeypatch.setattr(
        scan_server,
        "_PATTERN_EVALUATION",
        EvaluationRecorder(tmp_path / "evaluation.sqlite3"),
    )
