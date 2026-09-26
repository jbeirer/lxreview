"""A narrow operation relay, never an arbitrary HTTP/CDP proxy."""

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path

import httpx
from aiohttp import web

from ..contracts import Health, ReviewRequest, ReviewResponse
from ..errors import Category, LXError
from ..paths import Paths, lock
from ..reviewer import WebReviewer

OPERATIONS = {
    "new_conversation",
    "ensure_ready",
    "configure",
    "query",
    "health",
    "sessions",
    "close",
    "recover",
    "review",
}


def application(
    session, token: str, expires: float, lock_path: Path, metadata: dict
) -> web.Application:
    async def dispatch(request: web.Request) -> web.Response:
        if time.time() >= expires or not hmac.compare_digest(
            request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
        ):
            return web.json_response({"error": "unauthorized"}, status=401)
        op = request.match_info["op"]
        if op not in OPERATIONS:
            return web.json_response({"error": "operation_not_allowed"}, status=404)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) - (
                set(ReviewRequest.model_fields)
                if op == "review"
                else {"prompt", "timeout"}
                if op == "query"
                else {"model", "reasoning_effort"}
                if op == "configure"
                else set()
            ):
                raise ValueError("Invalid arguments")
            with lock(lock_path):
                if op == "review":
                    result = await WebReviewer(session, metadata).review(
                        ReviewRequest.model_validate(data)
                    )
                elif op == "query":
                    prompt, timeout = data["prompt"], float(data["timeout"])
                    if (
                        not isinstance(prompt, str)
                        or len(prompt) > 200000
                        or not 5 <= timeout <= 1800
                    ):
                        raise ValueError("Invalid query")
                    result = await session.query(prompt, timeout)
                elif op == "configure":
                    choice = ReviewRequest.model_validate(
                        {"target": "https://github.com/o/r/pull/1", "head_sha": "0" * 40, **data}
                    )
                    result = await session.configure(choice.model, choice.reasoning_effort)
                else:
                    result = await getattr(session, op)()
            payload = (
                result.model_dump(mode="json")
                if isinstance(result, (Health, ReviewResponse))
                else result
            )
            return web.json_response({"ok": True, "result": payload})
        except LXError as exc:
            return web.json_response(
                exc.as_dict(), status=409 if exc.category == Category.BUSY else 503
            )
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except TimeoutError:
            return web.json_response(
                {"ok": False, "category": "timeout", "message": "Reviewer deadline exceeded"},
                status=504,
            )

    async def proof(request: web.Request) -> web.Response:
        # Prove possession before a client sends the bearer to a possibly stale port.
        try:
            data = await request.json()
            nonce = data.get("nonce") if isinstance(data, dict) else None
            if time.time() >= expires:
                return web.json_response({"error": "expired"}, status=401)
            if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
                raise ValueError("Invalid nonce")
            digest = hmac.new(
                token.encode(), ("lxreview-relay-v1:" + nonce).encode(), hashlib.sha256
            ).hexdigest()
            return web.json_response({"proof": digest})
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_request"}, status=400)

    app = web.Application(client_max_size=250000)
    app.router.add_post("/v1/proof", proof)
    app.router.add_post("/v1/{op}", dispatch)
    return app


class RelaySession:
    def __init__(self, paths: Paths):
        self.paths = paths

    async def call(self, op: str, **data):
        try:
            state = json.loads((self.paths.root / "state/bridge/connection.json").read_text())
            import socket

            if state["host"] != socket.getfqdn() or time.time() >= state["expires"]:
                raise ValueError("Expired or wrong host")
            port = int(state["port"])
            if not 1024 <= port <= 65535:
                raise ValueError("Invalid port")
            token = state["token"]
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                raise ValueError("Invalid credential")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LXError(
                Category.UNAVAILABLE,
                "Local browser unavailable; reconnect using lxreview pair on the workstation",
            ) from exc
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(float(data.get("timeout", 10)) + 15, connect=2),
            ) as client:
                # Probe before a potentially long query; a lost tunnel fails quickly.
                url = f"http://127.0.0.1:{port}/v1/"
                nonce = secrets.token_hex(32)
                proof = await client.post(url + "proof", json={"nonce": nonce}, timeout=3)
                proof.raise_for_status()
                expected = hmac.new(
                    token.encode(), ("lxreview-relay-v1:" + nonce).encode(), hashlib.sha256
                ).hexdigest()
                proof_data = proof.json()
                received = proof_data.get("proof", "") if isinstance(proof_data, dict) else ""
                if not isinstance(received, str) or not hmac.compare_digest(
                    expected.encode(), received.encode()
                ):
                    raise LXError(
                        Category.AUTH, "Bridge server identity could not be verified; pair again"
                    )
                headers = {"Authorization": f"Bearer {token}"}
                if op in ("query", "review"):
                    probe = await client.post(url + "sessions", json={}, headers=headers, timeout=3)
                    probe.raise_for_status()
                response = await client.post(url + op, json=data, headers=headers)
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("Invalid relay response")
                if response.status_code == 401:
                    raise LXError(Category.AUTH, "Bridge token expired; recreate pairing")
                if result.get("ok") is not True:
                    raise LXError(
                        Category(result.get("category", "unavailable")),
                        result.get("message", "Bridge unavailable"),
                    )
                return result["result"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LXError(
                Category.UNAVAILABLE,
                "Local browser unavailable; keep the laptop awake and reconnect the bridge",
            ) from exc

    async def new_conversation(self):
        await self.call("new_conversation")

    async def ensure_ready(self):
        await self.call("ensure_ready")

    async def query(self, prompt: str, timeout: float) -> str:
        return await self.call("query", prompt=prompt, timeout=timeout)

    async def configure(self, model: str, reasoning_effort: str) -> dict:
        return await self.call("configure", model=model, reasoning_effort=reasoning_effort)

    async def health(self) -> Health:
        return Health.model_validate(await self.call("health"))

    async def sessions(self) -> list[dict]:
        return await self.call("sessions")

    async def close(self):
        await self.call("close")

    async def recover(self):
        await self.call("recover")


class RelayReviewer:
    """Keep fresh navigation, readiness and submission under one workstation lock."""

    def __init__(self, paths: Paths):
        self.session = RelaySession(paths)

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        return ReviewResponse.model_validate(
            await self.session.call("review", **request.model_dump())
        )

    async def health(self) -> Health:
        return await self.session.health()
