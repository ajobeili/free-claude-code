"""The server-owned Pattern stream starts without requiring a browser visit."""

import asyncio
import runpy
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace


def test_pattern_starts_inside_original_lifespan_and_preserves_shutdown(monkeypatch):
    events = []
    application = SimpleNamespace(router=SimpleNamespace())
    scanner = ModuleType("tools.scan_server")

    @asynccontextmanager
    async def original(app):
        assert app is application
        events.append("startup")
        try:
            yield
        finally:
            events.append("shutdown")

    async def activate(module):
        events.append(module)

    scanner.app = application
    scanner._lifespan = original
    scanner._activate_module = activate
    monkeypatch.setitem(sys.modules, "tools.scan_server", scanner)
    runpy.run_path(str(Path(__file__).parents[1] / "deploy/trading_server_runner.py"))

    async def run():
        async with application.router.lifespan_context(application):
            assert events == ["startup", "pattern"]
            events.append("serving")

    asyncio.run(run())
    assert events == ["startup", "pattern", "serving", "shutdown"]
