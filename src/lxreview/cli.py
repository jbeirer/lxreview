import asyncio
import json
import os
import platform
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import __version__, discussion
from .config import Config, executable
from .errors import Category, LXError
from .paths import Paths, atomic_write, lock, write_json
from .process import Supervisor, binary, require_afs_token
from .process import run as run_process
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
    from importlib.metadata import version as installed

    from .config import CHROME_VERSION

    output(
        {
            "lxreview": __version__,
            "playwright": installed("playwright"),
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
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        _write_launcher(paths)
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


def _write_launcher(paths: Paths) -> None:
    """The fixed entry point that services, Claude hooks, commands and MCP call.

    It runs this installation's interpreter with its root, so upgrading the installed
    package keeps every registered path valid.
    """
    launcher = (
        f"#!{sys.executable}\nimport os\nos.environ['LXREVIEW_HOME'] = {str(paths.root)!r}\n"
        "from lxreview.cli import main\nmain()\n"
    )
    atomic_write(paths.executable, launcher, 0o700)


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
            passed = check["status"] == "PASS"
            table.add_row(
                check["check"],
                Text(check["status"], style="bold green" if passed else "bold red"),
                Text(check["detail"], style="" if passed else "red"),
            )
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


@app.command(name="options")
def choice_options(json_output: bool = typer.Option(False, "--json")):
    """The models and effort levels a run can use, and the configured ones."""
    from .backend import browser
    from .config import WORKER_EFFORTS, WORKER_MODELS

    paths, config = context()
    cache = paths.root / "state/reviewer-options.json"
    reviewer: dict = {}
    try:
        # A run's review owns the browser tab; never navigate it away mid-review.
        with lock(paths.root / "state/reviewer.lock"):
            reviewer = {**asyncio.run(browser(paths, config).options()), "live": True}
        write_json(cache, reviewer)
    except LXError as exc:
        if cache.exists():
            reviewer = {**json.loads(cache.read_text()), "live": False}
        reviewer["unavailable"] = str(exc)
    output(
        {
            "reviewer": {
                **reviewer,
                "configured": {
                    "model": config.reviewer.model,
                    "reasoning_effort": config.reviewer.reasoning_effort,
                },
            },
            "worker": {
                "models": list(WORKER_MODELS),
                "efforts": list(WORKER_EFFORTS),
                "configured": {"model": config.worker.model, "effort": config.worker.effort},
            },
        },
        json_output,
    )


def model_choices(chatgpt: str, claude: str) -> dict:
    """`--chatgpt MODEL[:EFFORT]` and `--claude MODEL[:EFFORT]` as per-run choices."""
    choices = {}
    for role, value in (("reviewer", chatgpt), ("worker", claude)):
        model, _, effort = value.partition(":")
        if model.strip():
            choices[f"{role}_model"] = model.strip()
        if effort.strip():
            choices[f"{role}_effort"] = effort.strip()
    return choices


@app.command(name="run")
def start_run(
    target: str,
    repo: Path = typer.Option(Path.cwd(), "--repo"),
    max_passes: int | None = None,
    chatgpt: str = typer.Option(
        "", help="Reviewer MODEL[:EFFORT] for this run, e.g. sol:high or :medium (see options)."
    ),
    claude: str = typer.Option("", help="Worker MODEL[:EFFORT] for this run, e.g. opus:xhigh."),
):
    """Start a detached autonomous Claude review/fix worker."""
    from .contracts import ReviewRequest
    from .git import Repository

    paths, config = context()
    choices = model_choices(chatgpt, claude)
    chosen = config.with_choices(choices)
    with lock(paths.root / "state/setup.lock"):
        repository = Repository(repo, paths)
        ReviewRequest(target=target, head_sha=repository.head())
        maximum = config.review.max_passes if max_passes is None else max_passes
        if not 1 <= maximum <= 20:
            raise LXError(Category.CONFIG, "max-passes must be 1..20")
        if config.role != "host":
            raise LXError(Category.CONFIG, "Start review workers on the repository host")
        executable(config.runtime.claude)
        require_afs_token(paths, paths.root, repo)
        identity = repository.preflight(target, settle=30)
        # Every pass reads the PR discussion; refuse now rather than after the first review.
        discussion.fetch(target, paths, config)
        with lock(repository.audit_root() / "start.lock"):
            for path in (paths.root / "state/runs").glob("*/state.json"):
                old = json.loads(path.read_text())
                if old["repo"] == str(repo.resolve()) and old["status"] not in TERMINAL:
                    raise LXError(
                        Category.BUSY,
                        f"Repository already has run {old['id']}; inspect/stop it first",
                    )
            store = RunStore.create(paths, repo, target, identity, maximum, repository.audit_root())
            if choices:
                store.update(choices=choices)
            try:
                Supervisor(paths, config).start(
                    "run-" + store.id,
                    [str(paths.executable), "worker", store.id],
                    cwd=repo.resolve(),
                )
            except Exception:
                store.finish("FAILED", "Persistent worker could not start; run doctor")
                raise
        described = _described(chosen)
        watch = f"{_launcher(paths)} watch {store.id}"
        output(
            {
                "run_id": store.id,
                "status": "QUEUED",
                "watch": watch,
                **described,
                "message": _announcement(store.id, target, described, watch),
            },
            True,
        )


def _announcement(run_id: str, target: str, described: dict[str, str], watch: str) -> str:
    """What /review-loop tells the user once the run starts, ready to relay unchanged."""
    return (
        f"Started review run `{run_id}` for {target}.\n"
        f"- Reviewer: {described['reviewer']}\n"
        f"- Worker: {described['worker']}\n\n"
        f"It runs in the background, so closing this chat does not stop it; `/review-stop {run_id}` does.\n"
        "To follow every step in full in a terminal (reviews, each finding's decision and reason,"
        " commands, tests, commits):\n\n"
        f"```bash\n{watch}\n```\n\n"
        "I'll relay the updates here as they arrive."
    )


def _described(config: Config) -> dict[str, str]:
    """The run's reviewer and worker choices in words, for people rather than parsers."""
    reviewer, worker = config.reviewer, config.worker
    chatgpt = "current model" if reviewer.model == "default" else reviewer.model
    if worker.model == worker.effort == "default":
        claude = "Claude Code's default model and effort"
    else:
        model = "default model" if worker.model == "default" else worker.model
        claude = f"Claude ({model}), {worker.effort} effort"
    return {
        "reviewer": f"ChatGPT ({chatgpt}), {reviewer.reasoning_effort} reasoning",
        "worker": claude,
    }


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
        require_afs_token(paths, paths.root, Path(state["repo"]))
        repo = Repository(Path(state["repo"]), paths)
        repo.verify_identity(state["identity"])
        repo.preflight(state["target"])
        discussion.fetch(state["target"], paths, config)
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
    chat: bool = typer.Option(
        False, "--chat", help="Compact, flushed lines for following a run from a Claude chat."
    ),
    after: int = typer.Option(0, "--after", min=0, help="Skip the first N events."),
):
    """Follow a redacted, persistent event timeline; Ctrl-C only stops watching."""
    if chat:
        if raw:
            raise LXError(Category.CONFIG, "Choose --chat or --raw")
        return _watch_chat(run_id, after)
    from .timeline import PHASES, render, style

    paths, config = context()
    store = RunStore(paths, run_id)
    offset, shown = 0, None
    shown_calls: set[str] = set()

    def show(lines: list[str]) -> None:
        if not lines:
            return
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(overflow="fold")
        for line in lines:
            clock, _, text = line.partition("  ")
            grid.add_row(clock, Text(text, style=style(text)))
        console.print(grid)

    try:
        state = store.observed(config)
        if not raw:
            for label, value in (("LXReview", store.id), ("PR", state["target"])):
                console.print(Text(f"{label:<9} ", style="bold") + Text(value))
            if state.get("observation"):
                console.print(state["observation"], markup=False)
        while True:
            events = []
            with (store.directory / "events.jsonl").open() as stream:
                stream.seek(offset)
                # A line without its newline is still being written; resume there next time.
                while (line := stream.readline()).endswith("\n"):
                    offset = stream.tell()
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
                    if raw:
                        typer.echo(line.rstrip("\n"))
            if not raw:
                show(render(events, limit=240, shown=shown_calls))
            current = (state["status"], state["pass"], state["phase"])
            if not raw and current != shown:
                colour = {
                    "RUNNING": "bold blue",
                    "CLEAN": "bold green",
                    "NO_VALID_SUBSTANTIAL_FINDINGS": "bold green",
                    "QUEUED": "bold",
                }.get(
                    state["status"],
                    "bold yellow" if state["status"] == "MAX_PASSES" else "bold red",
                )
                console.print(
                    Text(f"{'Status':<9} ", style="bold")
                    + Text(state["status"], style=colour)
                    + Text(
                        f"  pass {state['pass']}/{state['max_passes']}"
                        f"  ({PHASES.get(state['phase'], state['phase'])})",
                        style="dim",
                    )
                )
                shown = current
            if once or state["status"] in TERMINAL:
                return
            time.sleep(1)
            state = store.observed(config)
    except KeyboardInterrupt:
        return


# A Claude chat follows a run through its Monitor tool, which delivers each stdout line as a
# notification and stops a watch after 30 minutes; end sooner and name the continuation.
CHAT_WATCH_SECONDS = 25 * 60
CHAT_POLL_SECONDS = 5


def _watch_chat(run_id: str, after: int) -> None:
    from .timeline import PHASES, render

    paths, config = context()
    store = RunStore(paths, run_id)
    deadline = time.monotonic() + CHAT_WATCH_SECONDS
    offset, seen, shown = 0, 0, None
    shown_calls: set[str] = set()

    def emit(lines: list[str]) -> None:
        if lines:
            print("\n".join(lines), flush=True)

    state = store.observed(config)
    if after == 0:
        emit([f"LXReview {store.id} for {state['target']}"])
    while True:
        events = []
        with (store.directory / "events.jsonl").open() as stream:
            stream.seek(offset)
            while (line := stream.readline()).endswith("\n"):
                offset = stream.tell()
                seen += 1
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if seen <= after:
                    # Restore pending calls across Monitor watch continuations without
                    # replaying their text, so a later refusal is still explained.
                    render([event], shown=shown_calls)
                else:
                    events.append(event)
        lines = render(events, limit=240, shown=shown_calls)
        current = (state["status"], state["pass"], state["phase"])
        status = (
            f"Status {state['status']}, pass {state['pass']}/{state['max_passes']}"
            f" ({PHASES.get(state['phase'], state['phase'])})"
        )
        if current != shown:
            lines.append(status)
            shown = current
        emit(lines)
        if state["status"] in TERMINAL:
            return
        if time.monotonic() >= deadline:
            emit(
                [
                    f"Still running. Continue watching with: "
                    f"{shlex.quote(str(paths.executable))} watch {store.id} --chat --after {seen}"
                ]
            )
            return
        time.sleep(CHAT_POLL_SECONDS)
        state = store.observed(config)


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


def _launcher(paths: Paths) -> str:
    """The launcher as users can paste it: bare lxreview when PATH finds this installation."""
    found = shutil.which("lxreview")
    # The installed command runs this interpreter with the default root.
    if (
        found
        and Path(found).resolve().parent == Path(sys.executable).parent
        and paths.root == Paths(Path.home() / ".lxreview").root
    ):
        return "lxreview"
    home, path = str(Path.home()), str(paths.executable)
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


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
        note(f"Display it on {host} with:")
        command(f"{_launcher(paths)} desktop password")
    step(4, "Log in to ChatGPT in the Chrome window, as you normally would.")
    note("Complete any CAPTCHA or MFA yourself. LXReview never sees your ChatGPT password.")
    console.print()


# Seconds after which login shows its steps even if the browser has not answered yet.
LOGIN_STEPS_AFTER = 15


@app.command()
def login(timeout: int = 600):
    """Guide human ChatGPT login; never collect passwords, cookies or MFA codes."""
    from .backend import browser
    from .browser.playwright import validity
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
    local_host = config.mode == "local-browser" and config.role == "host"
    if not local_host:
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

        def service_stopped():
            # A browser service that crashed on startup must not leave login spinning.
            supervisor = Supervisor(paths, config)
            if local_host or supervisor.status("browser"):
                return
            unit = supervisor.unit("browser")
            raise LXError(
                Category.UNAVAILABLE,
                "The browser service stopped during startup; run lxreview doctor"
                + (
                    f" or read journalctl --user -u {unit}"
                    if config.runtime.supervisor == "systemd"
                    else ""
                ),
            )

        async def retry(operation, *transient):
            while True:
                try:
                    return await operation()
                except LXError as exc:
                    if exc.category not in transient or time.monotonic() >= end:
                        raise
                service_stopped()
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
            if shown:
                # After a manual login ChatGPT may land on a normal chat; test in a fresh one.
                await session.new_conversation()
                await session.ensure_ready()
            status.update(Text("Checking that ChatGPT replies…"))
            marker = "LXREVIEW_LOGIN_" + secrets.token_hex(6)
            response = await session.query(f"Reply exactly {marker}", 60)
            if response.strip() != marker:
                raise LXError(
                    Category.PROTOCOL, "Login smoke response did not match; it was not retried"
                )
        health = await session.health()
        return shown, health.metadata.get("session_expires")

    with lock(paths.root / "state/reviewer.lock"):
        prompted, expires = asyncio.run(wait())
    console.print(
        Text.assemble(
            ("✓ ChatGPT login verified", "bold green"),
            (f" · {validity(expires)}" if expires else "", "green"),
        )
    )
    if prompted and config.mode == "lxplus-browser":
        console.print(
            Text(
                f"You can close the VNC viewer and the SSH tunnel. Chrome keeps running on {config.runtime.host}.",
                style="dim",
            )
        )
    console.print(Text.assemble("Next: ", (f"{_launcher(paths)} doctor", "bold")), soft_wrap=True)


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
def update(
    source: Path | None = typer.Option(
        None, help="Install this LXReview checkout instead of the latest release."
    ),
):
    """Upgrade LXReview with uv, then set it up again and restart its services."""
    paths, config = context()
    with lock(paths.root / "state/setup.lock"), lock(paths.root / "state/reviewer.lock"):
        for file in (paths.root / "state/runs").glob("*/state.json"):
            if json.loads(file.read_text())["status"] not in TERMINAL:
                raise LXError(Category.BUSY, "Stop active runs before updating")
        uv = shutil.which("uv")
        env = _uv_environment(paths)
        tools = run_process([uv, "tool", "dir"], paths, env=env).stdout.strip() if uv else ""
        if not uv or not Path(sys.prefix).is_relative_to(tools):
            raise LXError(
                Category.CONFIG,
                "LXReview was not installed with uv tool install; upgrade it the way it was "
                f"installed, then run lxreview setup --mode {config.mode}",
            )
        supervisor = Supervisor(paths, config)
        running = [name for name in ("browser", "bridge") if supervisor.status(name)]
        # Services must not keep running code whose files the upgrade replaces.
        _stop(paths, config)
        command = (
            [uv, "tool", "install", "--reinstall", "--managed-python", str(source.resolve())]
            if source
            else [uv, "tool", "upgrade", "lxreview"]
        )
        run_process(command, paths, env=env, timeout=600)
    # From here on only the new code runs: it checks the pinned runtimes, rewrites the
    # launcher and the Claude integration, and restarts what was running.
    # These are the user's own commands, so they get the user's environment (setup
    # locates Claude Code on PATH, as when the user runs it).
    launcher = str(paths.executable)
    for step in (["setup", "--mode", config.mode], ["start"] if "browser" in running else []):
        if step and subprocess.run([launcher, *step]).returncode:
            raise LXError(
                Category.UNAVAILABLE,
                f"The new version is installed, but lxreview {' '.join(step)} failed; rerun it",
            )
    if "bridge" in running:
        Supervisor(paths, config).start("bridge", [launcher, "bridge-worker"])
    output(
        "LXReview updated."
        + (f" Restarted: {', '.join(running)}." if running else "")
        + " Start a new Claude conversation to use the new commands."
    )


def _uv_environment(paths: Paths) -> dict[str, str]:
    """The isolated environment plus the settings that tell uv where its tools live."""
    from .process import environment

    env = environment(paths)
    env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("UV_")
            or key in ("XDG_DATA_HOME", "XDG_BIN_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE")
            or key.lower() in ("https_proxy", "http_proxy", "no_proxy")
        }
    )
    return env


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
        shutil.rmtree(paths.local, ignore_errors=True)
        destination = paths.root.with_name(paths.root.name + ".uninstalled-" + secrets.token_hex(4))
        paths.root.rename(destination)
        output(
            f"Uninstalled. Recovery archive (contains private browser state): {destination}\nDelete that archive to remove all package data. Repository audit logs remain under .git/review-loop.\nFinally remove the command itself: uv tool uninstall lxreview"
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
def guard(
    repo: Path = typer.Option(..., "--repo"),
    phase: str = typer.Option("edit", "--phase"),
    scratch: Path | None = typer.Option(None, "--scratch"),
):
    from .security import hook

    raise typer.Exit(hook(repo.resolve(), phase, scratch.resolve() if scratch else None))


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
def browser_server():
    from .backend import metadata
    from .browser.service import serve

    paths, config = context()
    os.umask(0o077)

    async def work():
        task = asyncio.create_task(serve(paths, config, metadata(config)))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(work())


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
