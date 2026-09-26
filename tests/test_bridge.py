import time

import httpx
import pytest
from aiohttp import web

from lxreview.bridge.relay import application
from lxreview.bridge.ssh import host_name, tunnel_command
from lxreview.errors import LXError


class Session:
    def __init__(self):
        self.calls = 0

    async def sessions(self):
        self.calls += 1
        return [{"key": "chatgpt-reviewer"}]


async def test_real_http_relay_auth_expiry_and_allowlist(paths):
    session = Session()
    runner = web.AppRunner(
        application(session, "relay-secret", time.time() + 60, paths.root / "relay.lock", {})
    )
    await runner.setup()
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            response = await client.post("/v1/sessions", json={})
            assert response.status_code == 401 and session.calls == 0
            headers = {"Authorization": "Bearer relay-secret"}
            response = await client.post("/v1/sessions", json={}, headers=headers)
            assert response.json()["result"][0]["key"] == "chatgpt-reviewer"
            response = await client.post("/v1/http://evil", json={}, headers=headers)
            assert response.status_code == 404
            response = await client.post(
                "/v1/sessions", json={"url": "http://evil"}, headers=headers
            )
            assert response.status_code == 400
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "host",
    ["lxplus.cern.ch", "evil.com", "-oProxyCommand=x", "lxplus1.cern.ch;rm", "lxplus1.cern.ch\n"],
)
def test_invalid_hosts(host):
    with pytest.raises(LXError):
        host_name(host)


def test_tunnel_is_loopback_only(monkeypatch):
    monkeypatch.setattr("lxreview.bridge.ssh.binary", lambda _: "/usr/bin/ssh")
    argv = tunnel_command("lxplus8s01.cern.ch", 23456, 23457)
    assert "127.0.0.1:23456:127.0.0.1:23457" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "BatchMode=yes" in argv
    assert "0.0.0.0" not in str(argv)


def test_pairing_is_one_time_host_bound_and_private(paths, monkeypatch):
    import socket
    import time

    from lxreview.bridge.ssh import decode_code, pairing_code, register

    monkeypatch.setattr(socket, "getfqdn", lambda: "lxplus123.cern.ch")
    code = decode_code(pairing_code(paths))
    payload = {
        "host": code["host"].split("@")[-1],
        "nonce": code["nonce"],
        "token": "x" * 40,
        "expires": time.time() + 100,
    }
    result = register(paths, payload)
    assert result["host"] == code["host"].split("@")[-1]
    assert (paths.root / "state/bridge/connection.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(LXError, match="pending pairing"):
        register(paths, payload)


def test_expired_pairing_rejected(paths, monkeypatch):
    import socket
    import time

    from lxreview.bridge.ssh import decode_code, pairing_code

    monkeypatch.setattr(socket, "getfqdn", lambda: "lxplus123.cern.ch")
    code = pairing_code(paths)
    original = time.time()
    monkeypatch.setattr(time, "time", lambda: original + 700)
    with pytest.raises(LXError, match="expired"):
        decode_code(code)


async def test_entire_remote_review_excludes_local_control(paths):
    import asyncio
    import socket

    from lxreview.paths import lock

    entered, proceed = asyncio.Event(), asyncio.Event()
    events = []

    class Browser:
        async def new_conversation(self):
            events.append("new")
            entered.set()
            await proceed.wait()

        async def ensure_ready(self):
            events.append("ready")

        async def configure(self, model, reasoning_effort):
            return {}

        async def query(self, prompt, timeout):
            events.append("query")
            return "VERDICT: CLEAN"

    lockfile = paths.root / "state/reviewer.lock"
    runner = web.AppRunner(
        application(Browser(), "secret", time.time() + 60, lockfile, {"backend": "chatgpt-web"})
    )
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.create_task(
                client.post(
                    f"http://127.0.0.1:{port}/v1/review",
                    headers={"Authorization": "Bearer secret"},
                    json={
                        "target": "https://github.com/o/r/pull/1",
                        "head_sha": "a" * 40,
                        "timeout": 5,
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(LXError):
                with lock(lockfile):
                    pass
            proceed.set()
            response = await request
            assert response.json()["result"]["verdict"] == "CLEAN"
            assert events == ["new", "ready", "query"]
    finally:
        proceed.set()
        await runner.cleanup()


async def test_unresponsive_tunnel_is_killed_after_grace_period(monkeypatch):
    import asyncio

    from lxreview.bridge.ssh import terminate

    class Process:
        returncode = None
        killed = False

        def terminate(self):
            pass

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if not self.killed:
                raise TimeoutError
            return self.returncode

    process = Process()
    await asyncio.wait_for(terminate(process), 1)
    assert process.killed


def test_pairing_includes_remote_account(paths, monkeypatch):
    from lxreview.bridge.ssh import decode_code, pairing_code

    monkeypatch.setattr("lxreview.bridge.ssh.getpass.getuser", lambda: "cernuser")
    monkeypatch.setattr("lxreview.bridge.ssh.socket.getfqdn", lambda: "lxplus123.cern.ch")
    assert decode_code(pairing_code(paths))["host"] == "cernuser@lxplus123.cern.ch"


@pytest.mark.parametrize("rogue", [False, True])
async def test_relay_proves_identity_before_receiving_bearer(paths, rogue):
    import socket

    from lxreview.bridge.relay import RelaySession
    from lxreview.paths import write_json

    token = "x" * 40
    seen = []
    if rogue:
        app = web.Application()

        async def impersonator(request):
            seen.append(request.headers.get("Authorization"))
            return web.json_response({"proof": "not-the-relay"})

        app.router.add_route("*", "/{path:.*}", impersonator)
    else:
        app = application(Session(), token, time.time() + 60, paths.root / "relay.lock", {})
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    write_json(
        paths.root / "state/bridge/connection.json",
        {"host": socket.getfqdn(), "port": port, "expires": time.time() + 60, "token": token},
    )
    try:
        if rogue:
            with pytest.raises(LXError, match="identity"):
                await RelaySession(paths).sessions()
            assert seen == [None]
        else:
            assert (await RelaySession(paths).sessions())[0]["key"] == "chatgpt-reviewer"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("healthy,expected", [(True, 20), (False, 8)])
async def test_tunnel_reconnect_budget_resets_after_healthy_connection(
    monkeypatch, healthy, expected
):
    import time

    from lxreview.bridge import ssh

    clock = [0.0]
    starts = []

    class Tunnel:
        returncode = 0

        async def wait(self):
            clock[0] += 120 if healthy else 1  # connection lifetime before it drops
            return 0

    async def start():
        starts.append(1)
        return Tunnel()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(ssh.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ssh.asyncio, "sleep", no_sleep)
    real_time = time.time
    # Healthy connections keep reconnecting until the credential expires (20 drops here).
    monkeypatch.setattr(ssh.time, "time", lambda: real_time() + (1e6 if len(starts) >= 20 else 0))
    await ssh.supervise_tunnel(start, real_time() + 3600)
    assert len(starts) == expected


async def test_configure_operation_validates_its_choices(paths):
    class Picker(Session):
        chosen: list = []

        async def configure(self, model, reasoning_effort):
            self.chosen.append((model, reasoning_effort))
            return {"chatgpt_model": model, "chatgpt_reasoning": reasoning_effort}

    session = Picker()
    runner = web.AppRunner(
        application(session, "relay-secret", time.time() + 60, paths.root / "relay.lock", {})
    )
    await runner.setup()
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    headers = {"Authorization": "Bearer relay-secret"}
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            ok = await client.post(
                "/v1/configure",
                json={"model": "GPT-5.5", "reasoning_effort": "high"},
                headers=headers,
            )
            assert ok.json()["result"] == {"chatgpt_model": "GPT-5.5", "chatgpt_reasoning": "high"}
            for bad in (
                {"model": "x; rm -rf /", "reasoning_effort": "high"},
                {"model": "GPT-5.5", "reasoning_effort": "HIGH!"},
                {"model": "GPT-5.5", "reasoning_effort": "high", "extra": 1},
            ):
                response = await client.post("/v1/configure", json=bad, headers=headers)
                assert response.status_code == 400
            assert session.chosen == [("GPT-5.5", "high")]
    finally:
        await runner.cleanup()
