import asyncio
import base64
import getpass
import json
import math
import re
import secrets
import shlex
import socket
import time

from aiohttp import web

from ..config import Config
from ..errors import Category, LXError
from ..paths import Paths, atomic_write, lock, write_json
from ..process import binary, environment
from .relay import application


def host_name(value: str) -> str:
    if not re.fullmatch(r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?lxplus[a-zA-Z0-9]+\.cern\.ch", value):
        raise LXError(
            Category.CONFIG, "Pair with an exact lxplusNN.cern.ch host, not the lxplus alias"
        )
    return value


def tunnel_command(host: str, remote: int, local: int) -> list[str]:
    host_name(host)
    if not all(1024 <= p <= 65535 for p in (remote, local)):
        raise LXError(Category.CONFIG, "Invalid tunnel port")
    return [
        binary("ssh"),
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ConnectTimeout=8",
        "-R",
        f"127.0.0.1:{remote}:127.0.0.1:{local}",
        host,
    ]


def pairing_code(paths: Paths) -> str:
    code = {
        "host": host_name(f"{getpass.getuser()}@{socket.getfqdn()}"),
        "root": str(paths.root),
        "nonce": secrets.token_urlsafe(24),
        "expires": time.time() + 600,
    }
    with lock(paths.root / "state/bridge/pair.lock"):
        write_json(paths.root / "state/bridge/pairing.json", code)
    return base64.urlsafe_b64encode(json.dumps(code).encode()).decode()


def decode_code(value: str) -> dict:
    try:
        code = json.loads(base64.urlsafe_b64decode(value))
        host_name(code["host"])
        if (
            not isinstance(code["expires"], (int, float))
            or not math.isfinite(code["expires"])
            or time.time() >= code["expires"]
            or not isinstance(code.get("nonce"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", code["nonce"])
            or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", code["root"])
            or ".." in code["root"].split("/")
        ):
            raise ValueError("Expired or invalid path")
        return code
    except (ValueError, KeyError, TypeError) as exc:
        raise LXError(
            Category.CONFIG, "Invalid or expired pairing code; generate a new code on LXPLUS"
        ) from exc


def register(paths: Paths, payload: dict) -> dict:
    with lock(paths.root / "state/bridge/pair.lock"):
        pending_file = paths.root / "state/bridge/pairing.json"
        try:
            pending = json.loads(pending_file.read_text())
        except (OSError, ValueError) as exc:
            raise LXError(Category.AUTH, "No valid pending pairing; generate a new code") from exc
        nonce = payload.get("nonce", "")
        if (
            not isinstance(nonce, str)
            or time.time() >= pending["expires"]
            or not secrets.compare_digest(nonce.encode(), pending["nonce"].encode())
        ):
            raise LXError(Category.AUTH, "Pairing expired or nonce mismatch")
        if payload.get("host") != socket.getfqdn():
            raise LXError(Category.AUTH, "Pairing reached the wrong LXPLUS host")
        token = payload["token"]
        expires = float(payload["expires"])
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
            or not time.time() < expires <= time.time() + 28860
        ):
            raise LXError(Category.AUTH, "Invalid bridge credential")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        write_json(
            paths.root / "state/bridge/connection.json",
            {"host": socket.getfqdn(), "port": port, "token": token, "expires": expires},
        )
        pending_file.unlink()
        return {"port": port, "host": socket.getfqdn()}


async def terminate(process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), 2)
    except ProcessLookupError:
        pass
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), 3)


async def supervise_tunnel(start, expires: float) -> None:
    """Reconnect until the credential expires; stop after 8 consecutive short-lived tries.

    A connection that stayed up for a minute resets both the failure budget and the backoff.
    """
    failures = 0
    while failures < 8 and time.time() < expires:
        started = time.monotonic()
        tunnel = await start()
        try:
            await asyncio.wait_for(tunnel.wait(), max(1, expires - time.time()))
        except TimeoutError:
            break
        finally:
            await terminate(tunnel)
        failures = 0 if time.monotonic() - started >= 60 else failures + 1
        await asyncio.sleep(min(2 ** max(failures - 1, 0), 30))


async def serve(paths: Paths, config: Config) -> None:
    code = json.loads((paths.root / "state/bridge/local-pairing.json").read_text())
    token = secrets.token_urlsafe(32)
    expires = time.time() + 8 * 3600
    from ..backend import browser, metadata

    runner = web.AppRunner(
        application(
            browser(paths, config),
            token,
            expires,
            paths.root / "state/reviewer.lock",
            metadata(config),
        ),
        access_log=None,
        shutdown_timeout=2,
    )
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    local = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    ssh = binary("ssh")
    remote_cli = code["root"] + "/bin/lxreview"
    register_cmd = [
        ssh,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        code["host"],
        shlex.join([remote_cli, "bridge-register"]),
    ]
    process = await asyncio.create_subprocess_exec(
        *register_cmd,
        env=environment(paths),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    payload = {
        "nonce": code["nonce"],
        "token": token,
        "expires": expires,
        "host": code["host"].split("@")[-1],
    }
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 20)
        if process.returncode:
            raise LXError(
                Category.UNAVAILABLE,
                "SSH pairing failed; verify login and regenerate the pairing code",
            )
        registered = json.loads(output)
        if registered["host"] != payload["host"]:
            raise LXError(Category.AUTH, "Remote hostname mismatch")
        write_json(
            paths.root / "state/bridge/status.json",
            {"host": registered["host"], "expires": expires, "status": "connecting"},
        )
        await supervise_tunnel(
            lambda: asyncio.create_subprocess_exec(
                *tunnel_command(code["host"], registered["port"], local),
                env=environment(paths),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            expires,
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await runner.cleanup()
        # Remote invalidation is authenticated by the current token delivered through stdin.
        revoke = await asyncio.create_subprocess_exec(
            ssh,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            code["host"],
            shlex.join([remote_cli, "bridge-revoke"]),
            env=environment(paths),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(revoke.communicate(token.encode()), 10)
        except TimeoutError:
            revoke.kill()
            await revoke.wait()
        atomic_write(paths.root / "state/bridge/status.json", json.dumps({"status": "stopped"}))
