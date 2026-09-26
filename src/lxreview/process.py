"""Explicit environments and package-owned user service supervision."""

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Literal

from .config import Config
from .errors import Category, LXError
from .paths import Paths, lock, write_json


def environment(paths: Paths, *, desktop: bool = False) -> dict[str, str]:
    env = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", ""),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LXREVIEW_HOME": str(paths.root),
        "TMPDIR": str(paths.root / "cache"),
    }
    for key in (
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "SSH_AUTH_SOCK",
        "KRB5CCNAME",
        "TERM",
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    if desktop:
        env.update(
            {
                "XDG_CONFIG_HOME": str(paths.root / "state/config"),
                "XDG_CACHE_HOME": str(paths.root / "cache"),
                "XDG_DATA_HOME": str(paths.root / "state/data"),
                "AGENTIFY_DESKTOP_STATE_DIR": str(paths.root / "state/agentify"),
                "AGENTIFY_DESKTOP_BROWSER_BACKEND": "chrome-cdp",
                "npm_config_cache": str(paths.root / "cache/npm"),
                "npm_config_userconfig": str(paths.root / "config/npm-user.rc"),
                "npm_config_globalconfig": str(paths.root / "config/npm-global.rc"),
                "npm_config_prefix": str(paths.root / "runtime/node"),
                "ELECTRON_CACHE": str(paths.root / "cache/electron"),
            }
        )
    return env


def binary(name: str) -> str:
    path = shutil.which(
        name,
        path=None
        if name == "claude"
        else "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
    )
    if not path:
        raise LXError(
            Category.UNAVAILABLE, f"Missing {name}; install it or ask the host administrator"
        )
    return str(Path(path).absolute())


def run(
    argv: list[str],
    paths: Paths,
    *,
    cwd: Path | None = None,
    timeout: float = 30,
    input: str | None = None,
    check: bool = True,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env or environment(paths),
        input=input,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode:
        # Do not include arbitrary child stderr: it can contain credentials.
        raise LXError(
            Category.UNAVAILABLE,
            f"{Path(argv[0]).name} failed (exit {result.returncode}); run lxreview doctor",
        )
    return result


class Supervisor:
    def __init__(self, paths: Paths, config: Config):
        self.paths, self.config = paths, config

    def unit(self, name: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9-]+", name):
            raise LXError(Category.UNSAFE, "Invalid service name")
        return f"lxreview-{hashlib.sha256(str(self.paths.root).encode()).hexdigest()[:8]}-{name}"

    def status(self, name: str) -> bool:
        unit = self.unit(name)
        state_file = self.paths.root / f"state/services/{unit}.json"
        if not state_file.exists():
            return False
        state = json.loads(state_file.read_text())
        if state["host"] != socket.getfqdn():
            raise LXError(
                Category.UNAVAILABLE,
                f"Service belongs to {state['host']}; connect to that exact host",
            )
        kind = state.get("supervisor", self.config.runtime.supervisor)
        if kind == "launchd":
            result = run(
                [binary("launchctl"), "print", f"gui/{os.getuid()}/org.lxreview.{unit}"],
                self.paths,
                check=False,
            )
            return result.returncode == 0 and "state = running" in result.stdout
        suffix = ".scope" if kind == "tmux-scope" else ".service"
        return (
            run(
                [binary("systemctl"), "--user", "is-active", "--quiet", unit + suffix],
                self.paths,
                check=False,
            ).returncode
            == 0
        )

    def start(self, name: str, argv: list[str], *, cwd: Path | None = None) -> None:
        with lock(self.paths.root / f"state/services/{self.unit(name)}.lock"):
            self._start(name, argv, cwd=cwd)

    def _start(self, name: str, argv: list[str], *, cwd: Path | None = None) -> None:
        if self.config.runtime.host and self.config.runtime.host != socket.getfqdn():
            raise LXError(Category.UNAVAILABLE, f"Start services on {self.config.runtime.host}")
        if self.status(name):
            return
        unit = self.unit(name)
        # Persist before launching: a fast worker/status call must see its ownership.
        write_json(
            self.paths.root / f"state/services/{unit}.json",
            {
                "host": socket.getfqdn(),
                "argv": argv,
                "cwd": str(cwd or self.paths.root),
                "environment": environment(self.paths),
                "supervisor": self.config.runtime.supervisor,
            },
        )
        entry = [str(self.paths.executable), "supervised-exec", name]
        if self.config.runtime.supervisor == "launchd":
            import plistlib

            from .paths import atomic_write

            plist = self.paths.root / f"state/services/{unit}.plist"
            payload = {
                "Label": f"org.lxreview.{unit}",
                "ProgramArguments": entry,
                "WorkingDirectory": str(cwd or self.paths.root),
                "EnvironmentVariables": environment(self.paths),
                "RunAtLoad": True,
                "Umask": 0o077,
                "StandardOutPath": str(self.paths.root / f"logs/{unit}.log"),
                "StandardErrorPath": str(self.paths.root / f"logs/{unit}-error.log"),
            }
            atomic_write(plist, plistlib.dumps(payload).decode())
            run(
                [binary("launchctl"), "bootout", f"gui/{os.getuid()}/org.lxreview.{unit}"],
                self.paths,
                check=False,
            )
            run([binary("launchctl"), "bootstrap", f"gui/{os.getuid()}", str(plist)], self.paths)
            return
        cmd = [
            binary("systemd-run"),
            "--user",
            f"--unit={unit}",
            "--collect",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=20",
        ]
        if self.config.runtime.supervisor == "systemd":
            cmd += [
                "--property=UMask=0077",
                f"--working-directory={cwd or self.paths.root}",
                "--",
                *entry,
            ]
        else:
            import shlex

            # A separate server per scope keeps children in the correct cgroup.
            cmd += [
                "--scope",
                "--",
                binary("tmux"),
                "-f",
                "/dev/null",
                "-S",
                str(self.tmux_socket(name)),
                "new-session",
                "-d",
                "-s",
                unit,
                "-c",
                str(cwd or self.paths.root),
                shlex.join(entry),
            ]
        run(cmd, self.paths)

    def tmux_socket(self, name: str) -> Path:
        key = hashlib.sha256(self.unit(name).encode()).hexdigest()[:12]
        return self.paths.root / f"state/services/{key}.sock"

    def stop(self, name: str) -> None:
        with lock(self.paths.root / f"state/services/{self.unit(name)}.lock"):
            unit = self.unit(name)
            state_file = self.paths.root / f"state/services/{unit}.json"
            if not state_file.exists():
                return
            # status also checks ownership/host even for an exited job.
            active = self.status(name)
            state = json.loads(state_file.read_text())
            kind = state.get("supervisor", self.config.runtime.supervisor)
            if kind != "launchd" and not active:
                return
            if kind == "launchd":
                run(
                    [binary("launchctl"), "bootout", f"gui/{os.getuid()}/org.lxreview.{unit}"],
                    self.paths,
                    check=False,
                )
                # An exited launchd job can remain registered and must be unloaded.
                loaded = run(
                    [binary("launchctl"), "print", f"gui/{os.getuid()}/org.lxreview.{unit}"],
                    self.paths,
                    check=False,
                )
                if loaded.returncode == 0:
                    raise LXError(Category.UNAVAILABLE, "Could not unload package launchd job")
            else:
                suffix = ".scope" if kind == "tmux-scope" else ".service"
                run([binary("systemctl"), "--user", "stop", unit + suffix], self.paths)

    def exec(self, name: str) -> None:
        state = json.loads((self.paths.root / f"state/services/{self.unit(name)}.json").read_text())
        if state["host"] != socket.getfqdn():
            raise LXError(Category.UNAVAILABLE, "Supervisor state belongs to another host")
        os.umask(0o077)
        os.chdir(state["cwd"])
        os.execve(state["argv"][0], state["argv"], state["environment"])


def detect_supervisor(paths: Paths) -> Literal["systemd", "tmux-scope", "launchd"]:
    if platform.system() == "Darwin":
        result = run([binary("launchctl"), "print", f"gui/{os.getuid()}"], paths, check=False)
        if result.returncode == 0:
            return "launchd"
        raise LXError(Category.UNAVAILABLE, "A logged-in macOS GUI launchd domain is required")
    unit = "lxreview-probe-" + secrets.token_hex(6)
    base = [binary("systemd-run"), "--user", "--collect", f"--unit={unit}"]
    if run([*base, "--wait", "--", binary("true")], paths, check=False).returncode == 0:
        return "systemd"
    binary("tmux")
    if run([*base, "--scope", "--", binary("true")], paths, check=False).returncode == 0:
        return "tmux-scope"
    raise LXError(
        Category.UNAVAILABLE,
        "Neither user services nor systemd scopes are available; persistent execution cannot start",
    )
