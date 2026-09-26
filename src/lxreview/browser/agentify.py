"""Published @agentify/desktop 0.2.4 HTTP contract. No upstream code is vendored."""

import asyncio
import json
from pathlib import Path

import httpx

from ..contracts import Health
from ..errors import Category, LXError

KEY = "chatgpt-reviewer"
HOME = "https://chatgpt.com/"
NOT_RESPONDING = "Agentify is not responding yet; the browser may still be starting"


class AgentifyAPI:
    def __init__(self, state_dir: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.state_dir = state_dir
        self.transport = transport

    async def call(
        self, method: str, endpoint: str, body: dict | None = None, timeout: float = 8
    ) -> dict:
        try:
            state = json.loads((self.state_dir / "state.json").read_text())
            if not isinstance(state, dict):
                raise ValueError("Invalid state")
            token = (self.state_dir / "token.txt").read_text().strip()
            port = int(state["port"])
            if not 1 <= port <= 65535 or not token or not state.get("serverId"):
                raise ValueError("Invalid state")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LXError(Category.UNAVAILABLE, "Agentify unavailable; run lxreview start") from exc
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                transport=self.transport,
                trust_env=False,
                timeout=httpx.Timeout(timeout, connect=3),
            ) as client:
                try:
                    health = await client.get("/health", timeout=3)
                except httpx.TimeoutException as exc:
                    # Nothing was sent yet; a cold browser can stall for minutes.
                    raise LXError(Category.UNAVAILABLE, NOT_RESPONDING) from exc
                health_data = health.json()
                if (
                    not isinstance(health_data, dict)
                    or health.is_error
                    or health_data.get("serverId") != state["serverId"]
                ):
                    raise LXError(
                        Category.AUTH,
                        "Agentify server identity mismatch; restart the managed browser",
                    )
                response = await client.request(
                    method, endpoint, json=body, headers={"Authorization": f"Bearer {token}"}
                )
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Not an object")
                error = str(result.get("error", ""))
                if response.status_code == 401:
                    raise LXError(
                        Category.AUTH,
                        "Agentify authentication mismatch; restart the managed service",
                    )
                if error == "already_generating":
                    raise LXError(Category.BUSY, "already_generating")
                if error:
                    category = (
                        Category.AUTH
                        if any(x in error for x in ("login", "captcha", "auth"))
                        else Category.TIMEOUT
                        if "timeout" in error
                        else Category.PROTOCOL
                    )
                    raise LXError(category, f"Agentify {category.value}; run lxreview doctor")
                if result.get("ok") is False:
                    raise LXError(Category.PROTOCOL, "Agentify operation was not successful")
                if response.is_error:
                    raise LXError(Category.UNAVAILABLE, f"Agentify HTTP {response.status_code}")
                return result
        except httpx.TimeoutException as exc:
            if method == "GET":
                raise LXError(Category.UNAVAILABLE, NOT_RESPONDING) from exc
            path = endpoint.split("?")[0]
            raise LXError(
                Category.TIMEOUT,
                "Agentify deadline exceeded; submission may have occurred, so it was not retried"
                if path == "/query"
                else f"Agentify did not answer {path} in time; it may still have acted, so it was not retried",
            ) from exc
        except httpx.RequestError as exc:
            raise LXError(
                Category.UNAVAILABLE, "Agentify disconnected; run lxreview start"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise LXError(Category.PROTOCOL, "Malformed Agentify response") from exc


class AgentifySession:
    def __init__(self, api: AgentifyAPI):
        self.api = api

    async def sessions(self) -> list[dict]:
        result = await self.api.call("GET", "/tabs")
        tabs = result.get("tabs")
        if not isinstance(tabs, list) or any(not isinstance(t, dict) for t in tabs):
            raise LXError(Category.PROTOCOL, "Malformed Agentify session list")
        own = [t for t in tabs if t.get("key") == KEY]
        if len(own) > 1:
            raise LXError(
                Category.UNSAFE,
                "Multiple reviewer sessions detected; refusing to create or close windows",
            )
        if own and own[0].get("vendorId", "chatgpt") != "chatgpt":
            raise LXError(Category.UNSAFE, "Reviewer key belongs to another provider")
        return own

    async def require_idle(self) -> None:
        status = await self.api.call("GET", f"/status?key={KEY}")
        runtime = status.get("runtime")
        if (
            not isinstance(runtime, dict)
            or type(runtime.get("inflightQueries")) is not int
            or "activeQuery" not in status
        ):
            raise LXError(Category.PROTOCOL, "Cannot establish whether the reviewer is idle")
        if status["activeQuery"] or runtime["inflightQueries"]:
            raise LXError(Category.BUSY, "Reviewer has an active query; reset/close refused")

    async def new_conversation(self) -> None:
        own = await self.sessions()
        if own:
            await self.require_idle()
        else:
            await self.api.call(
                "POST", "/tabs/create", {"key": KEY, "vendorId": "chatgpt", "show": True}
            )
        await self.api.call("POST", "/navigate", {"key": KEY, "url": HOME}, timeout=30)

    async def ensure_ready(self) -> None:
        status = await self.api.call("GET", f"/status?key={KEY}")
        if status.get("kind") in ("login", "captcha"):
            raise LXError(
                Category.AUTH,
                "Human browser login or CAPTCHA completion required; run lxreview login",
            )
        ready = await self.api.call(
            "POST", "/ensure-ready", {"key": KEY, "timeoutMs": 5000}, timeout=8
        )
        state = ready.get("state")
        if not isinstance(state, dict) or not state.get("promptVisible") or state.get("blocked"):
            raise LXError(Category.AUTH, "Reviewer composer is not ready; complete browser login")

    async def health(self) -> Health:
        sessions = await self.sessions()
        if not sessions:
            return Health(ready=False, detail="Reviewer session missing; run lxreview login")
        await self.ensure_ready()
        return Health(
            ready=True,
            detail="Reviewer authenticated and ready",
            metadata={"sessions": len(sessions)},
        )

    async def close(self) -> None:
        for tab in await self.sessions():
            await self.require_idle()
            ident = tab.get("id") or tab.get("tabId")
            if not ident:
                raise LXError(Category.PROTOCOL, "Session has no identity")
            await self.api.call("POST", "/tabs/close", {"tabId": ident})
        if await self.sessions():
            raise LXError(Category.PROTOCOL, "Reviewer did not close; replacement refused")

    async def recover(self) -> None:
        await self.close()
        await self.new_conversation()
        await self.ensure_ready()

    async def query(self, prompt: str, timeout: float) -> str:
        try:
            return await self._query(prompt, timeout)
        except TimeoutError as exc:
            raise LXError(
                Category.TIMEOUT, "Review deadline exhausted; no ambiguous submission was retried"
            ) from exc

    async def _query(self, prompt: str, timeout: float) -> str:
        prompt = " | ".join(prompt.splitlines())
        async with asyncio.timeout(timeout + 10):
            for attempt in range(3):
                try:
                    data = await self.api.call(
                        "POST",
                        "/query",
                        {"key": KEY, "prompt": prompt, "timeoutMs": int(timeout * 1000)},
                        timeout=timeout + 2,
                    )
                    result = data.get("result", {})
                    if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
                        raise LXError(Category.PROTOCOL, "Malformed Agentify assistant response")
                    meta = result["meta"]
                    text = result.get("text")
                    if (
                        not isinstance(text, str)
                        or not text.strip()
                        or type(meta.get("count")) is not int
                        or meta["count"] < 1
                        or meta.get("hasError") is not False
                    ):
                        raise LXError(
                            Category.PROTOCOL,
                            "No finalized assistant turn; generic page text is not a review",
                        )
                    return text
                except LXError as exc:
                    if exc.category != Category.BUSY or attempt == 2:
                        raise
                    await self.require_idle()
                    if attempt == 0:
                        await self.new_conversation()
                        await self.ensure_ready()
                    else:
                        await self.recover()
        raise LXError(Category.PROTOCOL, "Reviewer recovery exhausted")
