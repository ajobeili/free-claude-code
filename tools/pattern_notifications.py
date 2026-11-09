"""Event-only research alerts through the user's existing Hermes delivery CLI."""

import asyncio
import json
import math
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import TypedDict

from tools.pattern_trigger import PatternAlert


class AlertPlan(TypedDict):
    action: str
    entry: str
    stop_loss: str
    take_profit: str
    status: str


def alert_plan(alert: PatternAlert) -> AlertPlan:
    """One price/status projection shared by Telegram and the website."""
    action = (
        "BUY"
        if alert.direction == "UP"
        else "SELL"
        if alert.direction == "DOWN"
        else "UNKNOWN"
    )
    entry = stop = target = None
    if (
        alert.breakout_extreme is not None
        and action != "UNKNOWN"
        and all(
            math.isfinite(value) and value > 0
            for value in (alert.return_price, alert.spread, alert.breakout_extreme)
        )
    ):
        midpoint, spread, extreme = (
            Decimal(str(value))
            for value in (alert.return_price, alert.spread, alert.breakout_extreme)
        )
        sign = Decimal(1) if action == "BUY" else Decimal(-1)
        entry = midpoint + sign * spread / 2
        stop = extreme - sign * spread / 2
        risk = sign * (entry - stop)
        target = entry + sign * 2 * risk
        if risk <= 0 or min(entry, stop, target) <= 0:
            entry = stop = target = None
    valid = entry is not None and stop is not None and target is not None
    status = (
        "WAIT — invalid price levels"
        if not valid
        else "WAIT — delayed quote"
        if alert.quote_delay_seconds > 5 or alert.data_mode == "DELAYED RESEARCH"
        else ""
    )

    def price(value: Decimal | None) -> str:
        if value is None:
            return "unavailable"
        result = format(value, ".6f")
        return result.rstrip("0").rstrip(".") if "." in result else result

    return {
        "action": action,
        "entry": price(entry),
        "stop_loss": price(stop),
        "take_profit": price(target),
        "status": status,
    }


def format_alert(alert: PatternAlert) -> str:
    plan = alert_plan(alert)

    return "\n".join(
        [
            f"{alert.market.upper()} | {plan['action']} | DEMO",
            *([plan["status"]] if plan["status"] else []),
            f"Entry: {plan['entry']}",
            f"Stop loss: {plan['stop_loss']}",
            f"Take profit: {plan['take_profit']} (2R)",
            f"Delay {alert.quote_delay_seconds:.1f}s | signal prices",
            "Unvalidated demo. No trade placed.",
        ]
    )


async def send_via_hermes(executable: str, target: str, message: str) -> bool:
    """Use Hermes' own credential and destination checks; never copy its bot token."""
    if not executable or not target.startswith("telegram:"):
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "send",
            "--to",
            target,
            "--file",
            "-",
            "--json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    try:
        async with asyncio.timeout(45):
            output, _ = await process.communicate(message.encode("utf-8"))
    except (TimeoutError, asyncio.CancelledError) as error:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        if isinstance(error, asyncio.CancelledError):
            raise
        return False
    # CLI output can contain private transport details: expose only the verdict.
    try:
        payload = json.loads(output)
    except ValueError, UnicodeError:
        return False
    return (
        process.returncode == 0
        and isinstance(payload, dict)
        and payload.get("success") is True
        and not payload.get("error")
    )


@dataclass(frozen=True)
class NotificationState:
    enabled: bool
    status: str
    sent: int = 0
    failed: int = 0
    last_sent_event: str | None = None


class PatternNotifier:
    def __init__(
        self,
        enabled: bool,
        executable: str,
        target: str,
        sender: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self._enabled = enabled and bool(executable) and target.startswith("telegram:")
        self._state = NotificationState(
            self._enabled,
            "Ready — Hermes Telegram" if self._enabled else "Telegram alerts disabled",
        )
        self._queue: asyncio.Queue[tuple[PatternAlert, float]] = asyncio.Queue(
            maxsize=20
        )
        self._recent: deque[tuple[str, str]] = deque()
        self._seen: set[tuple[str, str]] = set()

        async def deliver(message: str) -> bool:
            return await send_via_hermes(executable, target, message)

        self._send = sender or deliver

    def snapshot(self) -> dict[str, object]:
        return {**asdict(self._state), "pending": self._queue.qsize()}

    def enqueue(self, alert: PatternAlert) -> None:
        key = alert.epic, alert.trigger_time
        if not self._enabled or key in self._seen:
            return
        try:
            self._queue.put_nowait((alert, time.monotonic()))
        except asyncio.QueueFull:
            self._state = replace(
                self._state,
                status="Telegram queue full; alert not sent",
                failed=self._state.failed + 1,
            )
            return
        if len(self._recent) == 200:
            self._seen.remove(self._recent.popleft())
        self._recent.append(key)
        self._seen.add(key)
        self._state = replace(self._state, status="Telegram alert queued")

    async def run(self) -> None:
        while True:
            alert, queued_at = await self._queue.get()
            try:
                sent = False
                if time.monotonic() - queued_at <= 60:
                    try:
                        sent = await self._send(format_alert(alert))
                    except OSError, ValueError, RuntimeError:
                        sent = False
                self._state = replace(
                    self._state,
                    status="Last alert sent through Hermes Telegram"
                    if sent
                    else "Telegram delivery failed or expired; not retried to avoid duplicates",
                    sent=self._state.sent + int(sent),
                    failed=self._state.failed + int(not sent),
                    last_sent_event=alert.trigger_time
                    if sent
                    else self._state.last_sent_event,
                )
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        await self._queue.join()
