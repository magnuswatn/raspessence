from secrets import compare_digest
from typing import Awaitable, Callable

from aiohttp import web


async def start_server(
    power_off_callback: Callable[..., Awaitable], auth_secret: str
) -> None:
    auth_secret_b = auth_secret.encode()

    async def power_off_handler(request: web.Request) -> web.Response:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return web.Response(status=401)

        if not compare_digest(auth_header[7:].encode(), auth_secret_b):
            return web.Response(status=401)

        await power_off_callback()
        return web.Response(status=200)

    app = web.Application()
    app.add_routes([web.get("/off", power_off_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
