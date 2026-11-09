"""Start Pattern monitoring with the server, including while no browser is open."""

from contextlib import asynccontextmanager

from tools.scan_server import _activate_module, _lifespan, app


@asynccontextmanager
async def lifespan(application):
    async with _lifespan(application):
        await _activate_module("pattern")
        yield


app.router.lifespan_context = lifespan
