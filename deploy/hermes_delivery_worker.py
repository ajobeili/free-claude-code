"""Resident Hermes sender on an owner-only Unix socket; never retries sends."""

import argparse
import asyncio
import json
import os
import stat
import time
from pathlib import Path


class DeliveryServer:
    def __init__(self, target, sender):
        self.target = target
        self.sender = sender
        self.lock = asyncio.Lock()

    async def handle(self, reader, writer):
        try:
            async with asyncio.timeout(2):
                raw = await reader.readline()
            request = json.loads(raw)
            if request.get("op") == "health":
                result = {"ready": True}
            elif (
                request.get("target") != self.target
                or not isinstance(request.get("message"), str)
                or not isinstance(request.get("deadline"), (int, float))
                or len(request["message"]) > 4096
            ):
                result = {"success": False, "reason": "invalid_request"}
            else:
                async with self.lock:
                    if not time.monotonic() < request["deadline"]:
                        result = {"success": False, "reason": "expired"}
                    else:
                        started = time.monotonic()
                        output = await asyncio.to_thread(
                            self.sender, self.target, request["message"]
                        )
                        payload = json.loads(output)
                        result = {
                            "success": payload.get("success") is True
                            and not payload.get("error"),
                            "seconds": time.monotonic() - started,
                        }
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        except OSError, ValueError, TypeError, TimeoutError, RuntimeError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--trading-root", required=True)
    args = parser.parse_args()
    # Import once, before exposing readiness. Hermes retains its own credentials
    # and destination authorization; Trading receives no Telegram bot token.
    from dotenv import dotenv_values
    from gateway.config import load_gateway_config
    from hermes_cli.send_cmd import _load_hermes_env
    from model_tools import _run_async
    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

    from tools.send_message_senders import _telegram_bot, _telegram_format
    from tools.send_message_tool import (
        _authorize_relay_target,
        _resolve_platform_config,
        _resolve_tool_target,
        send_message_tool,
    )

    _load_hermes_env()
    root = Path(args.trading_root)
    values = {
        **dotenv_values(root / ".env"),
        **dotenv_values(root / "tools/.env"),
    }
    target = values["PATTERN_TELEGRAM_TARGET"]
    platform, chat, thread, error = _resolve_tool_target(target)
    if error:
        raise RuntimeError("Configured Pattern destination cannot be resolved")
    _, config, _, error = _resolve_platform_config(platform, load_gateway_config())
    if error or _authorize_relay_target(
        platform, chat, thread, native_token=getattr(config, "token", None)
    ):
        raise RuntimeError("Configured Pattern destination is not authorized")
    # Warm the Telegram formatter and SDK without issuing a network request.
    normalize_telegram_chat_id(chat)
    _telegram_format("Pattern sender preparation; no message sent")
    _telegram_bot(config.token)
    # Resolve the async bridge before the first real request as well.
    if not callable(_run_async):
        raise RuntimeError("Hermes async sender is unavailable")

    def send(destination, message):
        return send_message_tool(
            {"action": "send", "target": destination, "message": message}
        )

    async def run():
        path = Path(args.socket)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError("Delivery socket path contains a regular file")
            path.unlink()
        os.umask(0o077)
        handler = DeliveryServer(target, send)
        server = await asyncio.start_unix_server(handler.handle, str(path), limit=8192)
        path.chmod(0o600)
        print("Resident Hermes notification sender ready", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
