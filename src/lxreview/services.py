import json
import os
import socket
from pathlib import Path

from .config import Config
from .errors import Category, LXError
from .paths import Paths, atomic_write
from .process import Supervisor, binary, environment


def choose_display(start: int = 99) -> int:
    for display in range(start, min(start + 100, 501)):
        if Path(f"/tmp/.X{display}-lock").exists() or Path(f"/tmp/.X11-unix/X{display}").exists():
            continue
        try:
            with socket.socket() as vnc, socket.socket() as x11:
                vnc.bind(("127.0.0.1", 5900 + display))
                x11.bind(("127.0.0.1", 6000 + display))
            return display
        except OSError:
            continue
    raise LXError(Category.BUSY, "No free private virtual display found")


def desktop_command(paths: Paths, config: Config) -> list[str]:
    display = config.runtime.display
    for port in (5900 + display, 6000 + display):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise LXError(
                    Category.BUSY,
                    f"Display :{display} is occupied; choose another display in config",
                ) from exc
    if Path(f"/tmp/.X{display}-lock").exists():
        raise LXError(
            Category.BUSY,
            f"Display :{display} has an existing X lock; inspect it before restarting",
        )
    script = paths.root / "state/vnc/xstartup"
    atomic_write(
        script,
        f"#!/bin/sh\nunset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS\nexec {binary('dbus-run-session')} {binary('startxfce4')}\n",
        0o700,
    )
    password = paths.root / "secrets/vnc-password"
    if not password.exists():
        import secrets

        # Random VNC-only password; never ask for account credentials.
        generated = secrets.token_urlsafe(12)[:8]
        # vncpasswd emits binary; use a binary subprocess instead.
        import subprocess

        encoded = subprocess.run(
            [binary("vncpasswd"), "-f"],
            input=(generated + "\n").encode(),
            capture_output=True,
            env=environment(paths),
            check=True,
        ).stdout
        password.write_bytes(encoded)
        password.chmod(0o600)
        atomic_write(paths.root / "secrets/vnc-viewer-password", generated)
    return [
        binary("vncserver"),
        f":{display}",
        "-fg",
        "-localhost",
        "yes",
        "-geometry",
        "1440x900",
        "-depth",
        "24",
        "-xstartup",
        str(script),
        "-rfbauth",
        str(password),
    ]


def start(paths: Paths, config: Config) -> None:
    if config.mode == "local-browser" and config.role == "host":
        raise LXError(
            Category.UNAVAILABLE,
            "Start/pair the browser on the workstation; no browser will be launched on this host",
        )
    supervisor = Supervisor(paths, config)
    if config.mode == "lxplus-browser":
        supervisor.start("desktop", [str(paths.executable), "service-exec", "desktop"])
    supervisor.start("browser", [str(paths.executable), "service-exec", "browser"])


def service_exec(paths: Paths, config: Config, name: str) -> None:
    env = environment(paths, desktop=True)
    if config.mode == "lxplus-browser":
        env["XAUTHORITY"] = str(paths.root / "state/vnc/.Xauthority")
    if name == "desktop":
        # TigerVNC state stays below the package root even on versions that ignore XDG.
        env["HOME"] = str(paths.root / "state/vnc")
        argv = desktop_command(paths, config)
    elif name == "browser":
        # Never attach Agentify to an unrelated existing CDP listener.
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", config.browser.debug_port))
            except OSError as exc:
                raise LXError(
                    Category.BUSY,
                    "Configured CDP port is already occupied; inspect it before starting",
                ) from exc
        if config.mode == "lxplus-browser":
            import time

            deadline = time.monotonic() + 30
            while not Path(f"/tmp/.X11-unix/X{config.runtime.display}").exists():
                if time.monotonic() >= deadline:
                    raise LXError(
                        Category.UNAVAILABLE,
                        "Virtual desktop did not become ready; run desktop status",
                    )
                time.sleep(0.2)
        runtime = json.loads((paths.root / "state/runtime.json").read_text())
        env["AGENTIFY_DESKTOP_CHROME_BIN"] = config.browser.chrome
        env["AGENTIFY_DESKTOP_CHROME_DEBUG_PORT"] = str(config.browser.debug_port)
        env["AGENTIFY_DESKTOP_CHROME_PROFILE_MODE"] = config.browser.profile
        if config.mode == "lxplus-browser":
            env["DISPLAY"] = f":{config.runtime.display}"
        elif "DISPLAY" in os.environ:
            env["DISPLAY"] = os.environ["DISPLAY"]
        argv = [
            runtime["electron"],
            str(paths.root / "runtime/agentify/node_modules/@agentify/desktop"),
            "--browser-backend",
            "chrome-cdp",
        ]
    else:
        raise LXError(Category.CONFIG, "Unknown service")
    os.umask(0o077)
    os.execve(argv[0], argv, env)
