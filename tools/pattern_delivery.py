"""Private IPC to the already-running Hermes delivery worker."""

import asyncio
import json
import time


async def _request(path, payload, remaining):
    if remaining <= 0:
        return {}
    writer = None
    try:
        async with asyncio.timeout(remaining):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()
            result = json.loads(await reader.readline())
            return result if isinstance(result, dict) else {}
    except OSError, ValueError, TimeoutError:
        # An unacknowledged send has an unknown outcome. Never retry it.
        return {}
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()


async def resident_ready(path):
    result = await _request(path, {"op": "health"}, 1)
    return result.get("ready") is True


async def send_resident(path, target, message, deadline):
    result = await _request(
        path,
        {"target": target, "message": message, "deadline": deadline},
        deadline - time.monotonic(),
    )
    return result.get("success") is True
