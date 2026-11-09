"""Centralized configuration loaded from environment variables.

Callers use get_settings() to read the current Settings. A `.env` file in
the repo root is loaded automatically if `python-dotenv` is available.
"""

import os
from dataclasses import dataclass

try:
    from pathlib import Path as _Path

    from dotenv import load_dotenv

    # Load .env from the workbench root (one level up from tools/) AND from
    # tools/.env — the latter wins for keys the user set via the in-app wizard.
    _here = _Path(__file__).resolve().parent
    load_dotenv(_here.parent / ".env")
    load_dotenv(_here / ".env", override=True)
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class Settings:
    capital_api_key: str = ""
    capital_login: str = ""
    capital_password: str = ""
    capital_environment: str = "demo"
    capital_account_id: str = ""
    server_host: str = "0.0.0.0"
    server_port: int = 8081
    fcc_proxy_url: str = "http://localhost:8082/v1/messages"
    # When False (default), the deterministic event calendar is purely
    # informational — the Macro/Event agent sees the calendar in its prompt
    # but the orchestrator does NOT enforce the blackout/caution veto.
    # Turn this on once a real event feed is wired up (or after the user has
    # confirmed the deterministic dates match reality for the next week).
    event_gate_enabled: bool = False
    pattern_telegram_enabled: bool = False
    pattern_telegram_target: str = ""
    pattern_hermes_executable: str = "hermes"
    pattern_evaluation_store: str = str(
        _Path(__file__).with_name(".pattern_evaluation.sqlite3")
    )


def get_settings() -> Settings:
    return Settings(
        capital_api_key=os.environ.get("CAPITAL_API_KEY", ""),
        capital_login=os.environ.get("CAPITAL_LOGIN", ""),
        capital_password=os.environ.get("CAPITAL_PASSWORD", ""),
        capital_environment="demo",
        capital_account_id=os.environ.get("CAPITAL_ACCOUNT_ID", ""),
        server_host=os.environ.get("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.environ.get("SERVER_PORT", "8081")),
        fcc_proxy_url=os.environ.get(
            "FCC_PROXY_URL", "http://localhost:8082/v1/messages"
        ),
        event_gate_enabled=os.environ.get("EVENT_GATE_ENABLED", "false").lower()
        in ("1", "true", "yes"),
        pattern_telegram_enabled=os.environ.get(
            "PATTERN_TELEGRAM_ENABLED", "false"
        ).lower()
        in ("1", "true", "yes"),
        pattern_telegram_target=os.environ.get("PATTERN_TELEGRAM_TARGET", ""),
        pattern_hermes_executable=os.environ.get("PATTERN_HERMES_EXECUTABLE", "hermes"),
        pattern_evaluation_store=os.environ.get(
            "PATTERN_EVALUATION_STORE",
            str(_Path(__file__).with_name(".pattern_evaluation.sqlite3")),
        ),
    )
