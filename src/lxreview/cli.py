import asyncio
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import Config, executable
from .errors import Category, LXError
from .paths import Paths, atomic_write, lock, write_json
from .process import Supervisor, binary
from .runs import TERMINAL, RunStore

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="LXReview — independent AI review loops for LXPLUS development.",
)
desktop_app = typer.Typer(no_args_is_help=True)
bridge_app = typer.Typer(no_args_is_help=True)
app.add_typer(desktop_app, name="desktop")
app.add_typer(bridge_app, name="bridge")
console = Console(highlight=False)


@app.callback()
def options(
    debug: bool = typer.Option(False, "--debug", help="Enable redacted rotating diagnostic logs."),
):
    from .observability import configure

    configure(Paths.default(), debug)


def context():
    paths = Paths.default()
    return paths, Config.load(paths)


def output(value, machine=False):
    if machine:
        typer.echo(json.dumps(value, indent=2))
    else:
        console.print(value, markup=False)


@app.command()
def version():
    """Show package and pinned runtime versions."""
    from .config import AGENTIFY_VERSION, CHROME_VERSION, NODE_VERSION

    output(
        {
            "lxreview": __version__,
            "agentify": AGENTIFY_VERSION,
            "node": NODE_VERSION,
            "chrome": CHROME_VERSION,
        }
    )


@app.command()
def setup(
    mode: str = typer.Option(""),
    role: str = typer.Option(""),
    remote: str = typer.Option(""),
    profile: str = typer.Option(""),
    chrome: str = typer.Option(""),
    skip_runtime: bool = False,
    skip_integration: bool = False,
):
    """Install isolated runtimes and user-wide Claude integration."""
    from .install import claude, runtime

    paths = Paths.default()
    paths.ensure()
    if not paths.executable.is_file():
        raise LXError(
            Category.CONFIG,
            "Bootstrap the isolated application first: python3 scripts/bootstrap.py --root "
            + str(paths.root),
        )
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        first_setup = not paths.config.exists()
        config = Config.load(paths) if not first_setup else Config()
        if first_setup and platform.system() == "Darwin":
            config.mode = "local-browser"
            config.browser.placement = "local"
        if not first_setup:
            for file in (paths.root / "state/runs").glob("*/state.json"):
                if json.loads(file.read_text())["status"] not in TERMINAL:
                    raise LXError(Category.BUSY, "Stop active runs before changing setup")
        # On a first setup this stops services orphaned by a manually deleted installation.
        for name in ("bridge", "browser", "desktop"):
            Supervisor(paths, config).stop(name)
        if not mode:
            mode = typer.prompt("Browser mode (lxplus-browser/local-browser)", default=config.mode)
        role = role or (
            config.role
            if not first_setup
            else "workstation"
            if remote or (mode == "local-browser" and not socket.getfqdn().startswith("lxplus"))
            else "host"
        )
        profile = profile or config.browser.profile
        if mode == "lxplus-browser" and (role != "host" or platform.system() != "Linux"):
            raise LXError(Category.CONFIG, "LXPLUS-browser mode requires a Linux repository host")
        if mode not in ("lxplus-browser", "local-browser") or role not in ("host", "workstation"):
            raise LXError(Category.CONFIG, "Choose a valid browser mode and host/workstation role")
        config.mode = "lxplus-browser" if mode == "lxplus-browser" else "local-browser"
        config.role = "host" if role == "host" else "workstation"
        config.browser.placement = "lxplus" if mode == "lxplus-browser" else "local"
        if profile not in ("isolated", "existing"):
            raise LXError(Category.CONFIG, "Profile must be isolated or existing")
        config.browser.profile = "existing" if profile == "existing" else "isolated"
        if profile == "existing":
            output(
                "Existing-profile mode requires normal Chrome to be fully closed. LXReview will not close it for you."
            )
        if chrome:
            config.browser.chrome = str(executable(chrome))
        config.runtime.host = socket.getfqdn()
        if first_setup and mode == "lxplus-browser":
            from .services import choose_display

            config.runtime.display = choose_display()
        from .process import detect_supervisor

        config.runtime.supervisor = detect_supervisor(paths)
        if role == "host":
            config.runtime.claude = binary("claude")
        config.save(paths)
        if not skip_runtime and not (mode == "local-browser" and role == "host"):
            runtime.install_runtimes(paths, config)
        if not skip_integration and role == "host":
            claude.install(paths, config)
        output("Configuration saved. No shell startup files or global runtimes were modified.")
        if mode == "local-browser":
            output(
                "Local-browser mode needs the workstation online and awake. Run lxreview pair on LXPLUS, then the printed command on the workstation."
            )
            if remote:
                from .bridge.ssh import host_name

                host_name(remote)
                output(
                    f"Generate the pairing code on {remote}; setup does not copy authentication state."
                )
        else:
            output(
                "Run lxreview login for the one-time human ChatGPT login. Services survive ordinary disconnects; host reboot/drain needs restart."
            )
    try:
        doctor(json_output=False, verbose=False, no_smoke=True)
    except typer.Exit:
        output("Setup saved. Complete login/pairing, then rerun lxreview doctor.")


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json"), verbose: bool = False, no_smoke: bool = False
):
    """Diagnose runtime, Claude integration, browser, transport and security."""
    from .doctor import diagnose

    try:
        paths, config = context()
    except LXError as exc:
        if json_output:
            output(
                {
                    "ready": False,
                    "checks": [{"check": "Configuration", "status": "FAIL", "detail": str(exc)}],
                },
                True,
            )
            raise typer.Exit(1) from None
        raise
    if verbose and not json_output:
        output(
            f"Python {platform.python_version()} | {platform.platform()} | {paths.root} | {config.mode}/{config.role}"
        )
    checks = asyncio.run(diagnose(paths, config, smoke=not no_smoke))
    if json_output:
        output({"ready": all(c["status"] == "PASS" for c in checks), "checks": checks}, True)
    else:
        table = Table(title="LXReview doctor")
        for column in ("Check", "Status", "Details"):
            table.add_column(column)
        for check in checks:
            table.add_row(check["check"], check["status"], check["detail"])
        console.print(table)
    if any(c["status"] == "FAIL" for c in checks):
        raise typer.Exit(1)


@app.command()
def start():
    """Start managed infrastructure for the configured placement."""
    from .services import start as launch

    paths, config = context()
    with lock(paths.root / "state/setup.lock"):
        launch(paths, config)
    output("Managed service start requested. Run lxreview doctor to verify readiness.")


@app.command()
def status(run_id: str = typer.Argument(""), json_output: bool = typer.Option(False, "--json")):
    """Inspect a run or the managed infrastructure."""
    paths, config = context()
    supervisor = Supervisor(paths, config)
    if run_id:
        store = RunStore(paths, run_id)
        state = store.observed(config)
        output(state, json_output)
    else:
        output(
            {
                "mode": config.mode,
                "host": config.runtime.host,
                "services": {
                    name: supervisor.status(name) for name in ("browser", "desktop", "bridge")
                },
            },
            json_output,
        )


@app.command()
def stop(run_id: str = typer.Argument("")):
    """Explicitly cancel one run, or stop package-owned browser infrastructure."""
    paths, config = context()
    with lock(paths.root / "state/setup.lock"):
        _stop(paths, config, run_id)


def _stop(paths: Paths, config: Config, run_id: str = ""):
    supervisor = Supervisor(paths, config)
    if run_id:
        store = RunStore(paths, run_id)
        state = store.load()
        if state["host"] != socket.getfqdn():
            raise LXError(Category.UNAVAILABLE, f"Stop this run on {state['host']}")
        if state["status"] in TERMINAL and not supervisor.status("run-" + run_id):
            output(f"Run already {state['status']}")
            return
        atomic_write(store.directory / "cancel", "explicit user cancellation\n")
        supervisor.stop("run-" + run_id)
        store.finish("CANCELLED")
        output(f"Cancelled {run_id}; repository and audits retained")
    else:
        for name in ("bridge", "browser", "desktop"):
            supervisor.stop(name)
        output("Managed infrastructure stopped")


@app.command()
def restart():
    stop(run_id="")
    start()


@app.command(name="run")
def start_run(
    target: str, repo: Path = typer.Option(Path.cwd(), "--repo"), max_passes: int | None = None
):
    """Start a detached autonomous Claude review/fix worker."""
    from .contracts import ReviewRequest
    from .git import Repository

    paths, config = context()
    with lock(paths.root / "state/setup.lock"):
        repository = Repository(repo, paths)
        ReviewRequest(target=target, head_sha=repository.head())
        maximum = config.review.max_passes if max_passes is None else max_passes
        if not 1 <= maximum <= 20:
            raise LXError(Category.CONFIG, "max-passes must be 1..20")
        if config.role != "host":
            raise LXError(Category.CONFIG, "Start review workers on the repository host")
        executable(config.runtime.claude)
        identity = repository.preflight(target, settle=30)
        with lock(repository.audit_root() / "start.lock"):
            for path in (paths.root / "state/runs").glob("*/state.json"):
                old = json.loads(path.read_text())
                if old["repo"] == str(repo.resolve()) and old["status"] not in TERMINAL:
                    raise LXError(
                        Category.BUSY,
                        f"Repository already has run {old['id']}; inspect/stop it first",
                    )
            store = RunStore.create(paths, repo, target, identity, maximum, repository.audit_root())
            try:
                Supervisor(paths, config).start(
                    "run-" + store.id,
                    [str(paths.executable), "worker", store.id],
                    cwd=repo.resolve(),
                )
            except Exception:
                store.finish("FAILED", "Persistent worker could not start; run doctor")
                raise
        output(
            {"run_id": store.id, "status": "QUEUED", "watch": f"lxreview watch {store.id}"}, True
        )


@app.command()
def runs(json_output: bool = typer.Option(False, "--json")):
    paths, config = context()
    values = [
        RunStore(paths, p.parent.name).observed(config)
        for p in sorted((paths.root / "state/runs").glob("*/state.json"))
    ]
    output(
        [{k: s[k] for k in ("id", "status", "pass", "target", "host")} for s in values], json_output
    )


@app.command()
def resume(run_id: str):
    """Resume only from a completed pass with a clean, matching PR branch."""
    from .git import Repository

    paths, config = context()
    with lock(paths.root / "state/setup.lock"):
        store = RunStore(paths, run_id)
        state = store.observed(config)
        if state["host"] != socket.getfqdn():
            raise LXError(Category.UNAVAILABLE, f"Resume on {state['host']}")
        if state["status"] not in ("FAILED", "CANCELLED", "INTERRUPTED"):
            raise LXError(Category.UNSAFE, "Only failed, cancelled or interrupted runs can resume")
        if Supervisor(paths, config).status("run-" + run_id):
            raise LXError(Category.BUSY, "Worker is still active")
        repo = Repository(Path(state["repo"]), paths)
        repo.verify_identity(state["identity"])
        repo.preflight(state["target"])
        incomplete = Path(state["audit"]) / f"pass-{state['completed_pass'] + 1:02}"
        if incomplete.exists():
            # Preserve the interrupted pass; new independent pass starts from verified pushed head.
            incomplete.rename(
                incomplete.with_name(incomplete.name + "-interrupted-" + secrets.token_hex(3))
            )
        (store.directory / "cancel").unlink(missing_ok=True)
        store.update(status="QUEUED", error="")
        store.event("resumed", checkpoint=state["completed_pass"])
        try:
            Supervisor(paths, config).start(
                "run-" + run_id, [str(paths.executable), "worker", run_id], cwd=Path(state["repo"])
            )
        except Exception:
            store.finish("FAILED", "Persistent worker could not resume; run doctor")
            raise
        output(f"Resumed {run_id}")


@app.command()
def watch(
    run_id: str,
    once: bool = False,
    raw: bool = typer.Option(False, "--raw", help="Print the redacted JSON events unformatted."),
):
    """Follow a redacted, persistent event timeline; Ctrl-C only stops watching."""
    from .timeline import PHASES, describe

    paths, config = context()
    store = RunStore(paths, run_id)
    offset, shown = 0, None
    try:
        state = store.observed(config)
        if not raw:
            for label, value in (("LXReview", store.id), ("PR", state["target"])):
                console.print(f"{label:<9} {value}", markup=False)
            if state.get("observation"):
                console.print(state["observation"], markup=False)
        while True:
            with (store.directory / "events.jsonl").open() as stream:
                stream.seek(offset)
                # A line without its newline is still being written; resume there next time.
                while (line := stream.readline()).endswith("\n"):
                    offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if raw:
                        typer.echo(line.rstrip("\n"))
                    else:
                        for text in describe(event):
                            console.print(text, markup=False)
            current = (state["status"], state["pass"], state["phase"])
            if not raw and current != shown:
                console.print(
                    f"{'Status':<9} {state['status']}  pass {state['pass']}/{state['max_passes']}"
                    f"  ({PHASES.get(state['phase'], state['phase'])})",
                    markup=False,
                )
                shown = current
            if once or state["status"] in TERMINAL:
                return
            time.sleep(1)
            state = store.observed(config)
    except KeyboardInterrupt:
        return


@app.command()
def logs(run_id: str):
    paths, _ = context()
    store = RunStore(paths, run_id)
    store.load()
    typer.echo((store.directory / "stdout.log").read_text())
    typer.echo((store.directory / "stderr.log").read_text())


@app.command()
def show(
    run_id: str,
    pass_number: int = typer.Option(1, "--pass"),
    json_output: bool = typer.Option(False, "--json"),
):
    paths, _ = context()
    store = RunStore(paths, run_id)
    if pass_number < 1:
        raise LXError(Category.CONFIG, "Pass must be positive")
    directory = Path(store.load()["audit"]) / f"pass-{pass_number:02}"
    if not directory.exists():
        raise LXError(Category.CONFIG, "Pass does not exist")
    values = {
        name: (directory / name).read_text()
        for name in (
            "reviewer.md",
            "claude-evaluation.md",
            "diff.patch",
            "tests.log",
            "metadata.json",
        )
        if (directory / name).exists()
    }
    if json_output:
        output(values, True)
    else:
        for name, value in values.items():
            console.rule(name)
            output(value)


@app.command()
def report(run_id: str, output_file: Path | None = typer.Option(None, "--output")):
    paths, _ = context()
    text = RunStore(paths, run_id).report()
    if output_file:
        if output_file.exists():
            raise LXError(Category.UNSAFE, "Report output already exists")
        atomic_write(output_file, text)
    else:
        typer.echo(text)


def _login_steps(paths: Paths, config: Config) -> None:
    """Print the VNC login walkthrough. Commands are soft-wrapped so they copy as one line."""
    import getpass

    host, address = config.runtime.host, f"127.0.0.1:{5900 + config.runtime.display}"

    def step(number: int, title: str) -> None:
        console.print()
        console.print(Text.assemble((f" {number} ", "bold black on cyan"), " ", (title, "bold")))

    def command(value: str, style: str = "bold green") -> None:
        console.print(Text("    " + value, style=style), soft_wrap=True)

    def note(value: str) -> None:
        console.print(Padding(Text(value, style="dim"), (0, 0, 0, 4)))

    console.print()
    console.rule(Text("Log in to ChatGPT", style="bold cyan"))
    console.print(
        Text.assemble(
            "The reviewer's Chrome runs inside a private remote desktop on ",
            (host, "bold"),
            ". You view that desktop from your laptop with a VNC viewer (a remote-screen app)"
            " and log in to ChatGPT once. The login is kept for future reviews.",
        )
    )
    step(1, "On your laptop, open a new terminal and run:")
    command(f"ssh -N -o ExitOnForwardFailure=yes -L {address}:{address} {getpass.getuser()}@{host}")
    note("It prints nothing and keeps running. That is expected; leave it open.")
    note(f"Use exactly {host}: plain lxplus.cern.ch may pick a different machine.")
    step(2, "Open the remote desktop in a VNC viewer:")
    for system, value in (
        ("macOS (built-in Screen Sharing), run:", f"open vnc://{address}"),
        ("Linux (TigerVNC; Remmina also works), run:", f"vncviewer {address}"),
        ("Windows (install TigerVNC or RealVNC Viewer), connect to:", address),
    ):
        note(system)
        command("  " + value)
    step(3, "When the viewer asks for a password, enter:")
    password = paths.root / "secrets/vnc-viewer-password"
    if sys.stdout.isatty() and password.is_file():
        command(password.read_text().strip(), "bold yellow")
    else:
        note(f"Display it on {host} with: lxreview desktop password")
    step(4, "Log in to ChatGPT in the Chrome window, as you normally would.")
    note("Complete any CAPTCHA or MFA yourself. LXReview never sees your ChatGPT password.")
    console.print()


# Seconds after which login shows its steps even if the browser has not answered yet.
LOGIN_STEPS_AFTER = 15


@app.command()
def login(timeout: int = 600):
    """Guide human ChatGPT login; never collect passwords, cookies or MFA codes."""
    from .backend import browser
    from .services import start as launch

    paths, config = context()
    if config.mode == "lxplus-browser":
        starting = f"Starting the remote desktop and Chrome on {config.runtime.host}…"
        where = "in the VNC window"
    elif config.role == "workstation":
        starting, where = "Starting Chrome…", "in the Chrome window LXReview opened"
    else:
        starting = "Connecting to the paired workstation…"
        where = "in the Chrome window on your paired workstation"
    if not (config.mode == "local-browser" and config.role == "host"):
        with lock(paths.root / "state/setup.lock"):
            launch(paths, config)

    async def wait():
        session = browser(paths, config)
        end = time.monotonic() + timeout
        shown = False

        def show_steps():
            nonlocal shown
            if shown:
                return
            shown = True
            if config.mode == "lxplus-browser":
                _login_steps(paths, config)
            else:
                console.print(Text(f"Log in to ChatGPT {where}.", style="bold"))

        async def retry(operation, *transient):
            while True:
                try:
                    return await operation()
                except LXError as exc:
                    if exc.category not in transient or time.monotonic() >= end:
                        raise
                await asyncio.sleep(3)

        with console.status(Text(starting)) as status:
            # A cold browser can stall for minutes, so the steps never wait for it.
            reminder = asyncio.get_running_loop().call_later(LOGIN_STEPS_AFTER, show_steps)
            try:
                await retry(session.sessions, Category.UNAVAILABLE)
                # Opening the keyed tab and navigating it are idempotent and send no prompt.
                await retry(session.new_conversation, Category.UNAVAILABLE, Category.TIMEOUT)
                status.update(Text("Checking whether ChatGPT is already logged in…"))
                # A freshly navigated page can briefly look logged out; only a second miss asks the human.
                misses = 0
                while True:
                    try:
                        await session.ensure_ready()
                        break
                    except LXError as exc:
                        if exc.category not in (
                            Category.AUTH,
                            Category.UNAVAILABLE,
                            Category.TIMEOUT,
                        ):
                            raise
                        if time.monotonic() >= end:
                            if exc.category != Category.AUTH:
                                raise
                            raise LXError(
                                Category.AUTH,
                                "Login deadline reached; finish login and rerun lxreview login",
                            ) from None
                        if exc.category == Category.AUTH:
                            misses += 1
                    if misses >= 2:
                        show_steps()
                        left = end - time.monotonic()
                        status.update(
                            Text.assemble(
                                f"Waiting for you to log in {where} ",
                                (f"({int(left // 60) + 1} min left; Ctrl-C stops waiting)", "dim"),
                            )
                        )
                    await asyncio.sleep(3)
            finally:
                reminder.cancel()
            status.update(Text("Checking that ChatGPT replies…"))
            marker = "LXREVIEW_LOGIN_" + secrets.token_hex(6)
            response = await session.query(f"Reply exactly {marker}", 60)
            if response.strip() != marker:
                raise LXError(
                    Category.PROTOCOL, "Login smoke response did not match; it was not retried"
                )
        return shown

    with lock(paths.root / "state/reviewer.lock"):
        prompted = asyncio.run(wait())
    console.print(Text("✓ ChatGPT login verified", style="bold green"))
    if prompted and config.mode == "lxplus-browser":
        console.print(
            Text(
                f"You can close the VNC viewer and the SSH tunnel. Chrome keeps running on {config.runtime.host}.",
                style="dim",
            )
        )
    console.print(Text.assemble("Next: ", ("lxreview doctor", "bold")))


@app.command()
def pair(code: str = typer.Argument("")):
    from .bridge.ssh import decode_code, pairing_code

    paths, config = context()
    if not code:
        if config.mode != "local-browser" or config.role != "host":
            raise LXError(
                Category.CONFIG, "Generate pairing codes on the local-browser repository host"
            )
        output("On your workstation (within 10 minutes):\nlxreview pair " + pairing_code(paths))
    else:
        if config.mode != "local-browser" or config.role != "workstation":
            raise LXError(
                Category.CONFIG,
                "On the workstation run setup --mode local-browser --role workstation first",
            )
        decoded = decode_code(code)
        Supervisor(paths, config).stop("bridge")
        write_json(paths.root / "state/bridge/local-pairing.json", decoded)
        start()
        Supervisor(paths, config).start("bridge", [str(paths.executable), "bridge-worker"])
        output(
            "Bridge supervisor started; verify with lxreview doctor on LXPLUS. Pairing lasts up to eight hours."
        )


@desktop_app.command("start")
def desktop_start():
    paths, config = context()
    if config.mode != "lxplus-browser":
        raise LXError(Category.CONFIG, "Desktop is only available in lxplus-browser mode")
    Supervisor(paths, config).start("desktop", [str(paths.executable), "service-exec", "desktop"])


@desktop_app.command("stop")
def desktop_stop():
    paths, config = context()
    Supervisor(paths, config).stop("desktop")


@desktop_app.command("status")
def desktop_status():
    paths, config = context()
    output({"running": Supervisor(paths, config).status("desktop")})


@desktop_app.command("connect")
def desktop_connect():
    paths, config = context()
    _login_steps(paths, config)


@desktop_app.command("password")
def desktop_password():
    """Display only the generated VNC viewer password in an interactive terminal."""
    if not sys.stdout.isatty():
        raise LXError(Category.UNSAFE, "VNC password display requires an interactive terminal")
    output((Paths.default().root / "secrets/vnc-viewer-password").read_text())


@bridge_app.command("status")
def bridge_status():
    paths, config = context()
    output({"supervisor_running": Supervisor(paths, config).status("bridge"), "role": config.role})


@bridge_app.command("stop")
def bridge_stop():
    paths, config = context()
    Supervisor(paths, config).stop("bridge")
    (paths.root / "state/bridge/connection.json").unlink(missing_ok=True)


@app.command()
def cleanup(yes: bool = False):
    """Remove retained runtime versions and downloaded archives."""
    paths, _ = context()
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        targets = [*(paths.root / "versions").iterdir(), *(paths.root / "downloads").iterdir()]
        output([str(p) for p in targets])
        if not yes:
            typer.confirm("Remove these package-owned cached artifacts?", abort=True)
        for target in targets:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()


@app.command()
def update(source: Path | None = None, rollback: bool = False):
    """Verify/reinstall the release's locked runtimes; never follow upstream latest."""
    from .install.runtime import install_runtimes
    from .install.updater import application_rollback, application_update

    paths, config = context()
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        for file in (paths.root / "state/runs").glob("*/state.json"):
            if json.loads(file.read_text())["status"] not in TERMINAL:
                raise LXError(Category.BUSY, "Stop active runs before updating")
        if source and rollback:
            raise LXError(Category.CONFIG, "Choose --source or --rollback")
        if source:
            application_update(paths, source)
            output(
                "Application updated atomically. Run doctor; update --rollback restores the prior launcher."
            )
            return
        if rollback:
            application_rollback(paths)
            output("Previous application launcher restored. Run doctor.")
            return
        _stop(paths, config)
        if config.mode == "local-browser" and config.role == "host":
            raise LXError(Category.CONFIG, "Browser runtimes belong on the paired workstation")
        install_runtimes(paths, config)
        output(
            "Pinned runtimes verified. Use update --source /path/to/reviewed/checkout for an application upgrade."
        )


@app.command()
def uninstall(yes: bool = False):
    """Remove owned integration and move the private root to a recoverable backup."""
    from .install.claude import uninstall as remove_integration

    paths, config = context()
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        active = [
            json.loads(p.read_text())
            for p in (paths.root / "state/runs").glob("*/state.json")
            if json.loads(p.read_text())["status"] not in TERMINAL
        ]
        if active:
            raise LXError(Category.BUSY, "Stop all active runs explicitly before uninstalling")
        if not yes:
            typer.confirm(
                "Remove LXReview integration and archive the private installation?", abort=True
            )
        _stop(paths, config)
        remove_integration(paths, config)
        destination = paths.root.with_name(paths.root.name + ".uninstalled-" + secrets.token_hex(4))
        paths.root.rename(destination)
        output(
            f"Uninstalled. Recovery archive (contains private browser state): {destination}\nDelete that archive to remove all package data. Repository audit logs remain under .git/review-loop."
        )


@app.command(hidden=True)
def worker(run_id: str):
    from .backend import reviewer
    from .worker import execute

    paths, config = context()
    os.umask(0o077)
    store = RunStore(paths, run_id)

    async def work():
        task = asyncio.create_task(execute(paths, config, store, reviewer(paths, config)))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(work())


@app.command(hidden=True)
def guard(repo: Path = typer.Option(..., "--repo"), phase: str = typer.Option("edit", "--phase")):
    from .security import hook

    raise typer.Exit(hook(repo.resolve(), phase))


@app.command(hidden=True)
def supervised_exec(name: str):
    paths, config = context()
    Supervisor(paths, config).exec(name)


@app.command(hidden=True)
def service_exec(name: str):
    from .services import service_exec as launch

    paths, config = context()
    launch(paths, config, name)


@app.command(hidden=True)
def bridge_worker():
    from .bridge.ssh import serve

    paths, config = context()
    os.umask(0o077)

    async def work():
        task = asyncio.create_task(serve(paths, config))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(work())


@app.command(hidden=True)
def bridge_register():
    from .bridge.ssh import register

    paths, config = context()
    if config.role != "host" or config.mode != "local-browser":
        raise LXError(Category.CONFIG, "Remote host is not configured for local-browser mode")
    output(register(paths, json.load(sys.stdin)), True)


@app.command(hidden=True)
def bridge_revoke():
    paths, _ = context()
    token = sys.stdin.read(256)
    file = paths.root / "state/bridge/connection.json"
    with lock(paths.root / "state/bridge/pair.lock"):
        if file.exists() and secrets.compare_digest(
            json.loads(file.read_text())["token"].encode(), token.encode()
        ):
            file.unlink()


@app.command()
def mcp():
    """Serve the package-owned reviewer MCP over stdio."""
    from .mcp.server import create_server

    paths, config = context()
    create_server(paths, config).run()


def main():
    try:
        app()
    except LXError as exc:
        Console(stderr=True).print(f"{exc.category.value}: {exc}", style="red", markup=False)
        raise SystemExit(1) from None
    except (OSError, ValueError, TimeoutError) as exc:
        Console(stderr=True).print(
            f"{type(exc).__name__}: operation failed; run lxreview doctor", style="red"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
