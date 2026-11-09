import asyncio

from tests.test_structure_scan import scan_server


def test_switching_modules_cancels_workbench_tasks(monkeypatch):
    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(scan_server, "_ACTIVE_MODULE", "home")
    monkeypatch.setattr(scan_server, "_MODULE_LOCK", asyncio.Lock())
    monkeypatch.setattr(scan_server, "_LIVE_ENGINE_TASK", None)
    monkeypatch.setattr(scan_server, "_CAPITAL_STREAM_TASK", None)
    monkeypatch.setattr(scan_server, "_live_engine_loop", idle)
    monkeypatch.setattr(scan_server, "_capital_stream_loop", idle)

    async def run():
        await scan_server._activate_module("workbench")
        engine = scan_server._LIVE_ENGINE_TASK
        stream = scan_server._CAPITAL_STREAM_TASK
        await scan_server._activate_module("workbench")
        assert scan_server._LIVE_ENGINE_TASK is engine
        await scan_server._activate_module("planner")
        assert engine is not None and stream is not None
        assert engine.cancelled() and stream.cancelled()
        for module in ("structure", "home", "planner"):
            await scan_server._activate_module(module)
            assert scan_server._LIVE_ENGINE_TASK is None
            assert scan_server._CAPITAL_STREAM_TASK is None

    asyncio.run(run())
