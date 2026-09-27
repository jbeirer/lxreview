"""The browser service: owns Chrome and serves the named reviewer operations locally.

Clients reach it through a unix socket in the node-local private runtime directory, so
neither a TCP port nor Chrome's debugging protocol is exposed to other users of a shared host.
"""

import asyncio
import json
import math
import os
import platform
import re
import secrets
from pathlib import Path

import httpx
from aiohttp import web

from ..config import Config
from ..contracts import Health
from ..errors import Category, LXError
from ..paths import Paths, private_dir, write_json
from .playwright import PlaywrightSession

NOT_RUNNING = "Browser service is not running; run lxreview start"
# Seconds a client waits beyond the operation's own deadline.
DEADLINES = {
    "new_conversation": 45,
    "ensure_ready": 30,
    "configure": 60,
    "options": 90,
    "recover": 60,
    "query": 30,
}


def directory(paths: Paths) -> Path:
    return paths.root / "state/browser"


def chrome_profile(paths: Paths, config: Config) -> Path:
    if config.browser.profile == "isolated":
        return directory(paths) / "chrome-profile"
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


async def serve(paths: Paths, config: Config, metadata: dict, session=None) -> None:
    from ..bridge.relay import application

    state = private_dir(directory(paths))
    socket_path = private_dir(paths.local) / "browser.sock"
    # sockaddr_un holds about 104 bytes on macOS and 108 on Linux.
    if len(str(socket_path).encode()) > 100:
        raise LXError(Category.CONFIG, "Installation path is too long for the browser socket")
    if session is None:
        session = PlaywrightSession(chrome_profile(paths, config), config.browser.chrome)
        # Open Chrome up front so login shows the window without waiting for a first call.
        await session.open()
    token = secrets.token_urlsafe(32)
    socket_path.unlink(missing_ok=True)
    runner = web.AppRunner(
        application(session, token, math.inf, state / "operation.lock", metadata),
        access_log=None,
        shutdown_timeout=2,
    )
    await runner.setup()
    try:
        await web.UnixSite(runner, str(socket_path)).start()
        os.chmod(socket_path, 0o600)
        write_json(state / "connection.json", {"token": token, "pid": os.getpid()})
        await asyncio.Event().wait()
    finally:
        (state / "connection.json").unlink(missing_ok=True)
        await runner.cleanup()
        socket_path.unlink(missing_ok=True)
        await session.shutdown()


class ServiceSession:
    """BrowserSessionBackend that forwards each operation to the local browser service."""

    def __init__(self, paths: Paths):
        self.paths = paths

    async def call(self, op: str, **data):
        state = directory(self.paths)
        try:
            token = json.loads((state / "connection.json").read_text())["token"]
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                raise ValueError("Invalid credential")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LXError(Category.UNAVAILABLE, NOT_RUNNING) from exc
        timeout = float(data.get("timeout", 0)) + DEADLINES.get(op, 15)
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(self.paths.local / "browser.sock")),
                base_url="http://lxreview",
                trust_env=False,
                timeout=httpx.Timeout(timeout, connect=3),
            ) as client:
                response = await client.post(
                    f"/v1/{op}", json=data, headers={"Authorization": f"Bearer {token}"}
                )
                result = response.json()
        except httpx.TimeoutException as exc:
            if op in ("query", "review"):
                raise LXError(
                    Category.TIMEOUT,
                    "Browser service did not answer in time; the prompt may have been sent, so it was not retried",
                ) from exc
            raise LXError(Category.UNAVAILABLE, "Browser service is not responding yet") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LXError(Category.UNAVAILABLE, NOT_RUNNING) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            detail = result if isinstance(result, dict) else {}
            try:
                category = Category(detail.get("category", "unavailable"))
            except ValueError:
                category = Category.UNAVAILABLE
            raise LXError(category, str(detail.get("message", "Browser service rejected the call")))
        return result["result"]

    async def new_conversation(self) -> None:
        await self.call("new_conversation")

    async def ensure_ready(self) -> None:
        await self.call("ensure_ready")

    async def query(self, prompt: str, timeout: float) -> str:
        return await self.call("query", prompt=prompt, timeout=timeout)

    async def configure(self, model: str, reasoning_effort: str) -> dict:
        return await self.call("configure", model=model, reasoning_effort=reasoning_effort)

    async def options(self) -> dict:
        return await self.call("options")

    async def health(self) -> Health:
        return Health.model_validate(await self.call("health"))

    async def sessions(self) -> list[dict]:
        return await self.call("sessions")

    async def close(self) -> None:
        await self.call("close")

    async def recover(self) -> None:
        await self.call("recover")
