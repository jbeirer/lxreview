import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lxreview.config import Config


async def test_real_mcp_stdio_initialization_and_tool_contract(paths):
    config = Config(mode="local-browser")
    config.browser.placement = "local"
    config.save(paths)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lxreview.cli", "mcp"],
        env={**os.environ, "LXREVIEW_HOME": str(paths.root)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "review_browser_query",
                "review_browser_navigate",
                "review_browser_ensure_ready",
                "review_browser_status",
                "review_browser_sessions",
                "review_browser_close_session",
                "review_browser_recover",
            }
            result = await session.call_tool("review_browser_status", {})
            assert "local browser unavailable" in str(result).lower()
            assert not list((paths.root / "state/services").iterdir())


async def test_real_mcp_review_against_http_agentify_fixture(paths):
    import json
    import socket

    import httpx
    from aiohttp import web
    from test_agentify import FakeAgentify

    from lxreview.paths import atomic_write, write_json

    fake = FakeAgentify()

    async def dispatch(request):
        payload = await request.read()
        result = fake.handle(
            httpx.Request(
                request.method, str(request.url), headers=dict(request.headers), content=payload
            )
        )
        return web.json_response(result.json(), status=result.status_code)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    write_json(paths.root / "state/agentify/state.json", {"port": port, "serverId": fake.server_id})
    atomic_write(paths.root / "state/agentify/token.txt", "private-token")
    Config().save(paths)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "lxreview.cli", "mcp"],
        env={**os.environ, "LXREVIEW_HOME": str(paths.root)},
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                for _ in range(2):
                    result = await client.call_tool(
                        "review_browser_query",
                        {
                            "target": "https://github.com/o/r/pull/1",
                            "head_sha": "a" * 40,
                            "timeout": 5,
                        },
                    )
                    data = json.loads(result.content[0].text)
                    assert data["verdict"] == "CLEAN"
                    assert data["raw"] == fake.response["text"]
        assert fake.created == 1 and fake.query_count == 2
    finally:
        await runner.cleanup()
