import asyncio
import json
import os
import platform
import socket
import stat
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import psutil

from . import __version__
from .backend import browser
from .config import CHROME_VERSION, Config
from .errors import LXError
from .install.claude import COMMANDS
from .paths import Paths, lock
from .process import Supervisor, binary, environment, run


async def diagnose(paths: Paths, config: Config, smoke: bool = True) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add("Package", True, f"LXReview {__version__}; schema {config.schema_version}")
    add(
        "Private root",
        paths.root.exists() and stat.S_IMODE(paths.root.stat().st_mode) == 0o700,
        "Installation root must be 0700",
    )
    violations = [
        str(p.relative_to(paths.root))
        for d in ("secrets", "state/browser", "state/bridge")
        for p in (paths.root / d).rglob("*")
        if not p.is_symlink() and p.stat().st_mode & 0o077
    ]
    add(
        "Secret permissions",
        not violations,
        "Private state modes" if not violations else ", ".join(violations[:5]),
    )
    if config.role == "workstation":
        add("Claude Code", True, "Not required on the workstation")
    elif not (Path(config.runtime.claude).is_file() and os.access(config.runtime.claude, os.X_OK)):
        add("Claude Code", False, "Run setup to discover the installed Claude executable")
    else:
        try:
            result = run(
                [config.runtime.claude, "auth", "status", "--json"], paths, check=False, timeout=20
            )
            auth = json.loads(result.stdout)
            # Claude Code renews its token itself, so there is no expiry to report.
            add(
                "Claude Code",
                auth.get("loggedIn") is True,
                f"Logged in via {auth.get('authMethod')} ({auth.get('subscriptionType')})"
                if auth.get("loggedIn") is True
                else "Not logged in; run claude auth login",
            )
        except (LXError, OSError, ValueError, TimeoutError, AttributeError):
            add("Claude Code", False, "Could not read Claude login status; run claude auth status")
    integration = paths.root / "state/claude-integration.json"
    try:
        owned = json.loads(integration.read_text())
        actual = json.loads((Path(owned["home"]) / ".claude.json").read_text())
        add(
            "Claude user-scope MCP",
            actual.get("mcpServers", {}).get("lxreview-reviewer") == owned["registration"],
            "Start a new Claude conversation after setup",
        )
        commands = all(
            (Path(owned["home"]) / ".claude/commands" / f"{name}.md").resolve()
            == paths.root / "claude/commands" / f"{name}.md"
            and (Path(owned["home"]) / ".claude/commands" / f"{name}.md").is_file()
            for name in COMMANDS
        )
        add("Claude commands", commands, "User-wide command links")
    except (OSError, ValueError, KeyError):
        add(
            "Claude user-scope MCP",
            config.role == "workstation",
            "Not required on workstation" if config.role == "workstation" else "Run lxreview setup",
        )
    try:
        if config.runtime.supervisor == "launchd":
            command = [binary("launchctl"), "print", f"gui/{os.getuid()}"]
        else:
            command = [binary("systemctl"), "--user", "show-environment"]
            if config.runtime.supervisor == "tmux-scope":
                binary("tmux")
        result = run(command, paths, check=False)
        add(
            "Persistence supervisor",
            result.returncode == 0,
            f"{config.runtime.supervisor} user supervisor available",
        )
    except (LXError, OSError, TimeoutError) as exc:
        add("Persistence supervisor", False, str(exc))
    if config.role == "host":
        try:
            if platform.system() == "Linux":
                binary("socat")
                result = run(
                    [
                        binary("bwrap"),
                        "--ro-bind",
                        "/",
                        "/",
                        "--unshare-user",
                        "--unshare-pid",
                        "--",
                        binary("true"),
                    ],
                    paths,
                    check=False,
                    timeout=10,
                )
                add(
                    "Worker sandbox",
                    result.returncode == 0,
                    "bubblewrap namespace probe; Claude must also enforce its run settings",
                )
            else:
                binary("sandbox-exec")
                add("Worker sandbox", True, "macOS sandbox executable available")
        except (LXError, OSError, TimeoutError) as exc:
            add("Worker sandbox", False, str(exc))
        try:
            from .git import unsafe_push_keys

            # Outside any repository: only system and global settings, which affect every run.
            result = run(
                [binary("git"), "config", "--null", "--list"], paths, cwd=paths.root, check=False
            )
            problems = unsafe_push_keys(result.stdout)
            add(
                "Git push settings",
                not problems,
                "Global Git settings allow the worker's push"
                if not problems
                else "Global Git settings block every run: " + ", ".join(problems),
            )
        except (LXError, OSError, TimeoutError) as exc:
            add("Git push settings", False, str(exc))
        try:
            event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}})
            result = run(
                [str(paths.executable), "guard", "--repo", str(paths.root)],
                paths,
                input=event,
                check=False,
            )
            # A syntax error also exits 2: require the policy's own denial, not just that code.
            add(
                "Worker deny hook",
                result.returncode == 2
                and ("blocked" in result.stderr or "not allowed" in result.stderr),
                "Run-scoped destructive Git denial",
            )
        except (LXError, OSError, TimeoutError) as exc:
            add("Worker deny hook", False, str(exc))
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            async with asyncio.timeout(15):
                params = StdioServerParameters(
                    command=str(paths.executable), args=["mcp"], env=environment(paths)
                )
                with open(os.devnull, "w") as errors:
                    async with stdio_client(params, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as connection:
                            await connection.initialize()
                            available = await connection.list_tools()
                            add(
                                "MCP connection",
                                "review_browser_query" in {t.name for t in available.tools},
                                "New stdio connection initialized; reopen Claude to discover registration",
                            )
        except Exception:
            add(
                "MCP connection",
                False,
                "Package MCP initialization failed; check launcher and user registration",
            )
    local_host = config.mode == "local-browser" and config.role == "host"
    if not local_host:
        chrome = Path(config.browser.chrome)
        expected = CHROME_VERSION if chrome.is_relative_to(paths.root / "runtime/chrome") else ""
        try:
            result = run(
                [str(chrome), "--version"],
                paths,
                check=False,
                timeout=10,
                env=environment(paths, desktop=True),
            )
            add(
                "Chrome",
                result.returncode == 0 and (not expected or expected in result.stdout),
                f"Executable version matches {expected}"
                if expected
                else "External browser executable responds",
            )
        except (LXError, OSError, TimeoutError):
            add(
                "Chrome", False, "Executable failed; run lxreview update to repair managed runtimes"
            )
        try:
            add("Playwright", True, f"Playwright {version('playwright')} drives Chrome over a pipe")
        except PackageNotFoundError:
            add("Playwright", False, "Playwright is missing; reinstall LXReview")
        try:
            add(
                "Browser service",
                Supervisor(paths, config).status("browser"),
                "Run lxreview start",
            )
        except LXError as exc:
            add("Browser service", False, str(exc))
        if platform.system() == "Linux" and Path(config.browser.chrome).is_file():
            try:
                result = run([binary("ldd"), config.browser.chrome], paths, check=False)
                missing = [
                    line.strip() for line in result.stdout.splitlines() if "not found" in line
                ]
                add(
                    "Chrome shared libraries",
                    result.returncode == 0 and not missing,
                    "; ".join(missing) or "Check completed",
                )
            except (LXError, OSError, TimeoutError) as exc:
                add("Chrome shared libraries", False, str(exc))
    if config.mode == "lxplus-browser":
        try:
            add(
                "Virtual desktop",
                Supervisor(paths, config).status("desktop"),
                "Run lxreview desktop start; node drain/reboot requires restart",
            )
        except LXError as exc:
            add("Virtual desktop", False, str(exc))
    try:
        with lock(paths.root / "state/reviewer.lock"):
            session = browser(paths, config)
            health = await session.health()
            add("SSH bridge" if local_host else "Browser / ChatGPT", health.ready, health.detail)
            tabs = await session.sessions()
            add(
                "Reviewer single-session",
                len(tabs) == 1,
                f"{len(tabs)} managed reviewer session(s)",
            )
            if smoke and health.ready:
                import secrets

                marker = "LXREVIEW_" + secrets.token_hex(8)
                await session.new_conversation()
                await session.ensure_ready()
                response = await session.query(f"Reply with exactly {marker} and nothing else.", 60)
                add(
                    "Query round trip",
                    response.strip() == marker,
                    "Final assistant turn exact-match smoke test",
                )
    except (LXError, TimeoutError) as exc:
        add("Reviewer", False, str(exc))
    # Inspect only managed ports, and never infer safety when the OS denies inspection.
    # The browser service listens on a private unix socket, never on TCP.
    ports = set()
    if config.mode == "lxplus-browser":
        ports.add(5900 + config.runtime.display)
    if local_host:
        try:
            ports.add(json.loads((paths.root / "state/bridge/connection.json").read_text())["port"])
        except (OSError, ValueError, KeyError):
            pass
    if not ports:
        add("Loopback listeners", True, "No managed TCP listeners on this machine")
    else:
        try:
            listeners = [
                c
                for c in psutil.net_connections(kind="tcp")
                if c.status == "LISTEN" and c.laddr and c.laddr.port in ports
            ]
            unsafe = [
                c.laddr.ip for c in listeners if c.laddr and c.laddr.ip not in ("127.0.0.1", "::1")
            ]
            add(
                "Loopback listeners",
                {c.laddr.port for c in listeners if c.laddr} == ports and not unsafe,
                "All managed listeners must be present and loopback-only"
                if not unsafe
                else "Unsafe network binding detected; stop services",
            )
        except (psutil.AccessDenied, PermissionError):
            add(
                "Loopback listeners",
                False,
                "OS denied socket inspection; bindings could not be verified",
            )
    add(
        "Exact host",
        config.role == "workstation" or config.runtime.host == socket.getfqdn(),
        f"Configured {config.runtime.host}; current {socket.getfqdn()}",
    )
    return checks
