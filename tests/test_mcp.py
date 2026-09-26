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


class FakeChatGPT:
    """Stands in for the Playwright session behind the real browser service."""

    def __init__(self):
        self.navigations = self.queries = 0
        self.stopped = False

    async def new_conversation(self):
        self.navigations += 1

    async def ensure_ready(self):
        pass

    async def query(self, prompt, timeout):
        self.queries += 1
        return "Full review\nVERDICT: CLEAN"

    async def sessions(self):
        return [{"key": "chatgpt-reviewer"}]

    async def shutdown(self):
        self.stopped = True


async def test_real_mcp_review_through_browser_service():
    import asyncio
    import json
    import shutil
    import stat
    import tempfile
    from pathlib import Path

    from lxreview.browser.service import serve
    from lxreview.paths import Paths

    # A short root keeps the unix socket path within the macOS sockaddr limit.
    paths = Paths(Path(tempfile.mkdtemp(prefix="lx", dir="/tmp")) / "root")
    paths.ensure()
    Config().save(paths)
    fake = FakeChatGPT()
    service = asyncio.create_task(serve(paths, Config(), {"backend": "chatgpt-web"}, fake))
    socket_path = paths.root / "state/browser/api.sock"
    try:
        for _ in range(100):
            if (paths.root / "state/browser/connection.json").exists():
                break
            await asyncio.sleep(0.05)
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lxreview.cli", "mcp"],
            env={**os.environ, "LXREVIEW_HOME": str(paths.root)},
        )
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
                    assert data["metadata"]["backend"] == "chatgpt-web"
        assert fake.navigations == 2 and fake.queries == 2
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
        assert fake.stopped and not socket_path.exists()
    finally:
        service.cancel()
        shutil.rmtree(paths.root.parent)
