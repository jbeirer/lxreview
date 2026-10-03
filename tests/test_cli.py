import json

import pytest
from typer.testing import CliRunner

from lxreview.cli import app
from lxreview.errors import Category, LXError

runner = CliRunner()


def test_doctor_invalid_config_is_machine_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("LXREVIEW_HOME", str(tmp_path / "missing"))
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ready"] is False


def test_cli_help_does_not_offer_shell_mutation():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "install-completion" not in result.stdout
    for command in ("setup", "doctor", "watch", "show", "report", "resume", "uninstall"):
        assert command in result.stdout


def test_guard_cli_matches_worker_hook_invocation(paths, tmp_path, monkeypatch):
    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    result = runner.invoke(
        app,
        ["guard", "--repo", str(tmp_path)],
        input=json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "code.py")}}
        ),
    )
    assert result.exit_code == 0, result.output
    denied = runner.invoke(
        app,
        ["guard", "--repo", str(tmp_path)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}}),
    )
    assert denied.exit_code == 2
    scratch = tmp_path.parent / f"{tmp_path.name}-work"
    scratch.mkdir()
    write = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(scratch / "x")}})
    for extra, code in (([], 2), (["--scratch", str(scratch)], 0)):
        result = runner.invoke(
            app, ["guard", "--repo", str(tmp_path), "--phase", "edit", *extra], input=write
        )
        assert result.exit_code == code, result.output


def test_restart_calls_stop_with_concrete_argument(monkeypatch):
    import lxreview.cli as cli

    calls = []
    monkeypatch.setattr(cli, "stop", lambda run_id: calls.append(run_id))
    monkeypatch.setattr(cli, "start", lambda: calls.append("start"))
    cli.restart()
    assert calls == ["", "start"]


def test_watch_renders_readable_timeline(paths, tmp_path, monkeypatch):
    from lxreview.config import Config
    from lxreview.runs import RunStore

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    Config().save(paths)
    identity = {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}
    store = RunStore.create(
        paths, tmp_path, "https://github.com/org/repo/pull/1", identity, 5, tmp_path / "audit"
    )
    store.event(
        "review_received",
        pass_number=1,
        verdict="SUBSTANTIAL_ISSUES",
        substantial=1,
        non_blocking=0,
    )
    store.event(
        "finding_evaluated",
        pass_number=1,
        finding="S1",
        decision="ACCEPTED",
        title="Off by one",
        reason="The loop skips the last bin",
    )
    store.event(
        "claude",
        event={
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                    {"type": "text", "text": "The range stops one short;\nfixing it."},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "python -m pytest tests"},
                    },
                ]
            },
        },
    )
    store.finish("CLEAN")
    result = runner.invoke(app, ["watch", store.id])
    assert result.exit_code == 0, result.output
    for text in (
        "1 substantial",
        "ACCEPTED  S1 Off by one",
        "because The loop skips the last bin",
        "Claude: The range stops one short; fixing it.",
        "Editing src/a.py",
        "$ python -m pytest tests",
        "Finished: CLEAN",
    ):
        assert text in result.stdout
    assert result.stdout.count("Status") == 1
    raw = runner.invoke(app, ["watch", store.id, "--raw"])
    assert all(json.loads(line)["kind"] for line in raw.stdout.splitlines())
    assert "PRIVATE_REASONING" not in result.stdout
    chat = runner.invoke(app, ["watch", store.id, "--chat"])
    assert chat.exit_code == 0, chat.output
    lines = chat.stdout.splitlines()
    assert lines[0] == f"LXReview {store.id} for https://github.com/org/repo/pull/1"
    assert any(line.endswith("Claude: The range stops one short; fixing it.") for line in lines)
    assert lines[-1].startswith("Status CLEAN, pass")
    assert "PRIVATE_REASONING" not in chat.stdout
    # Continuing after all but the last event shows only what is new.
    total = len((store.directory / "events.jsonl").read_text().splitlines())
    tail = runner.invoke(app, ["watch", store.id, "--chat", "--after", str(total - 1)])
    assert [line.split("  ", 1)[-1] for line in tail.stdout.splitlines()] == [
        "Finished: CLEAN",
        tail.stdout.splitlines()[-1],
    ]


def test_timeline_hides_guard_refusals_and_names_real_failures():
    from lxreview.timeline import render, style

    def claude(role, *blocks):
        return {
            "kind": "claude",
            "time": "",
            "event": {"type": role, "message": {"content": list(blocks)}},
        }

    events = [
        claude(
            "assistant",
            {
                "type": "tool_use",
                "id": "t1",
                "name": "Bash",
                "input": {"command": "grep -rn x src"},
            },
        ),
        claude(
            "user",
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "is_error": True,
                "content": "PreToolUse:Bash hook error: [guard]: Command is outside the worker allowlist",
            },
        ),
        claude(
            "assistant",
            {
                "type": "tool_use",
                "id": "t2",
                "name": "Bash",
                "input": {"command": "git push origin HEAD:refs/heads/f"},
            },
        ),
        claude(
            "user",
            {
                "type": "tool_result",
                "tool_use_id": "t2",
                "is_error": True,
                "content": "Exit code 128 fatal: unable to access",
            },
        ),
        {
            "kind": "claude",
            "time": "",
            "event": {"type": "tool_progress", "heartbeat": True, "elapsed_time_seconds": 30},
        },
        {
            "kind": "claude",
            "time": "",
            "event": {"type": "tool_progress", "heartbeat": True, "elapsed_time_seconds": 120},
        },
    ]
    texts = [line.split("  ", 1)[1] for line in render(events)]
    assert texts == [
        "$ git push origin HEAD:refs/heads/f",
        "Failed: Exit code 128 fatal: unable to access",
        "  still running (2 min)",
    ]
    assert style("Failed: x") == "red" and style("ACCEPTED  S1 x") == "bold green"
    assert style("Reviewer pass 1 complete, CLEAN") == "bold green"


def test_live_timeline_names_a_refusal_that_arrives_after_its_call():
    from lxreview.timeline import render

    def claude(role, block):
        return {
            "kind": "claude",
            "time": "",
            "event": {"type": role, "message": {"content": [block]}},
        }

    call = claude(
        "assistant",
        {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/tmp/x.py"}},
    )
    refusal = claude(
        "user",
        {
            "type": "tool_result",
            "tool_use_id": "w1",
            "is_error": True,
            "content": "PreToolUse:Write hook error: [/x/lxreview guard --repo /r --phase edit]:"
            " Edits must stay within the repository\n",
        },
    )
    shown: set[str] = set()
    first = render([call], shown=shown)
    second = render([refusal], shown=shown)
    assert [line.split("  ", 1)[1] for line in first + second] == [
        "Editing /tmp/x.py",
        "  refused by the guard: Edits must stay within the repository",
    ]
    # In one batch the refused call is left out entirely, as before.
    assert render([call, refusal], shown=set()) == []


def test_chat_watch_ends_before_the_monitor_limit_and_names_the_continuation(
    paths, tmp_path, monkeypatch
):
    import lxreview.cli as cli
    from lxreview.config import Config
    from lxreview.runs import RunStore

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    Config().save(paths)
    identity = {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}
    store = RunStore.create(
        paths, tmp_path, "https://github.com/org/repo/pull/1", identity, 5, tmp_path / "audit"
    )
    store.event("review_started", pass_number=1, head="a" * 40)
    monkeypatch.setattr(RunStore, "observed", lambda self, config: self.load())
    monkeypatch.setattr(cli, "CHAT_WATCH_SECONDS", 0)
    result = runner.invoke(app, ["watch", store.id, "--chat"])
    assert result.exit_code == 0, result.output
    total = len((store.directory / "events.jsonl").read_text().splitlines())
    assert result.stdout.splitlines()[-1] == (
        f"Still running. Continue watching with: {paths.executable} watch {store.id}"
        f" --chat --after {total}"
    )
    assert "Reviewer pass 1 started" in result.stdout


class LoginSession:
    def __init__(self, misses, stalls=0):
        self.misses = misses
        self.stalls = stalls

    async def sessions(self):
        from lxreview.errors import Category, LXError

        if self.stalls > 0:
            self.stalls -= 1
            raise LXError(Category.UNAVAILABLE, "Browser service is not responding yet")
        return []

    async def new_conversation(self):
        from lxreview.errors import Category, LXError

        if self.stalls == 0:
            self.stalls = -1
            raise LXError(Category.TIMEOUT, "Browser service did not answer in time")

    async def ensure_ready(self):
        from lxreview.errors import Category, LXError

        if self.misses:
            self.misses -= 1
            raise LXError(Category.AUTH, "Human browser login or CAPTCHA completion required")

    async def query(self, prompt, timeout):
        return prompt.removeprefix("Reply exactly ")

    async def health(self):
        import time

        from lxreview.contracts import Health

        return Health(
            ready=True, detail="ready", metadata={"session_expires": time.time() + 90.5 * 86400}
        )


def run_login(paths, monkeypatch, misses, stalls=None, running=True):
    import asyncio

    from lxreview.config import Config
    from lxreview.paths import atomic_write

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    config = Config()
    config.runtime.host = "lxplus8s01.cern.ch"
    config.save(paths)
    atomic_write(paths.root / "secrets/vnc-viewer-password", "vncsecret")
    monkeypatch.setattr("lxreview.services.start", lambda *a: None)
    session = LoginSession(misses, stalls) if stalls is not None else LoginSession(misses, -1)
    monkeypatch.setattr("lxreview.backend.browser", lambda *a: session)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0))
    monkeypatch.setattr("lxreview.cli.Supervisor.status", lambda self, name: running)
    return runner.invoke(app, ["login"])


def login_output(paths, monkeypatch, misses, stalls=None):
    result = run_login(paths, monkeypatch, misses, stalls)
    assert result.exit_code == 0, result.output
    return result.stdout


def test_login_shows_copyable_steps_once(paths, monkeypatch):
    text = login_output(paths, monkeypatch, misses=5)
    assert text.count("ssh -N") == 1
    assert "CAPTCHA completion required" not in text
    ssh = "ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:5999:127.0.0.1:5999 "
    assert any(
        line.strip().startswith(ssh) and line.endswith("@lxplus8s01.cern.ch")
        for line in text.splitlines()
    )
    assert "open vnc://127.0.0.1:5999" in text
    # Only an interactive terminal may display the VNC password.
    assert "vncsecret" not in text
    # Commands stay on one physical line even when the launcher path is long.
    assert any(line.strip() == f"{paths.executable} desktop password" for line in text.splitlines())
    assert "ChatGPT login verified · valid until" in text and "(90 days)" in text
    # PATH does not find this installation, so pasteable commands name the launcher.
    assert f"Next: {paths.executable} doctor" in text


def test_login_skips_steps_when_already_logged_in(paths, monkeypatch):
    text = login_output(paths, monkeypatch, misses=1)
    assert "ssh " not in text
    assert "ChatGPT login verified" in text


def test_login_shows_steps_while_a_cold_browser_stalls(paths, monkeypatch):
    import lxreview.cli as cli

    # The browser never reports a login page, yet the human still gets the steps in time.
    monkeypatch.setattr(cli, "LOGIN_STEPS_AFTER", 0)
    text = login_output(paths, monkeypatch, misses=0, stalls=3)
    assert text.count("ssh -N") == 1
    assert "ChatGPT login verified" in text


def test_login_stops_waiting_when_the_browser_service_died(paths, monkeypatch):
    result = run_login(paths, monkeypatch, misses=0, stalls=50, running=False)
    assert result.exit_code == 1
    assert "browser service stopped during startup" in str(result.exception)


def test_options_are_read_live_and_fall_back_to_the_last_check_while_busy(paths, monkeypatch):
    import lxreview.backend as backend
    from lxreview.config import Config
    from lxreview.paths import lock

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    Config().save(paths)

    class Browser:
        async def options(self):
            return {
                "models": ["GPT-5.5"],
                "model": "GPT-5.5",
                "reasoning": ["high"],
                "reasoning_effort": "high",
            }

    monkeypatch.setattr(backend, "browser", lambda paths, config: Browser())
    live = json.loads(runner.invoke(app, ["options", "--json"]).stdout)
    assert live["reviewer"]["live"] is True and live["reviewer"]["models"] == ["GPT-5.5"]
    assert live["reviewer"]["configured"] == {"model": "default", "reasoning_effort": "highest"}
    assert "xhigh" in live["worker"]["efforts"] and "opus" in live["worker"]["models"]
    # A run's review owns the browser: report the last check instead of navigating away.
    with lock(paths.root / "state/reviewer.lock"):
        busy = json.loads(runner.invoke(app, ["options", "--json"]).stdout)
    assert busy["reviewer"]["live"] is False and busy["reviewer"]["models"] == ["GPT-5.5"]
    assert "unavailable" in busy["reviewer"]


@pytest.mark.parametrize(
    ("chatgpt", "claude", "expected"),
    [
        (
            "sol:high",
            "opus:xhigh",
            {
                "reviewer_model": "sol",
                "reviewer_effort": "high",
                "worker_model": "opus",
                "worker_effort": "xhigh",
            },
        ),
        (":medium", "", {"reviewer_effort": "medium"}),
        ("GPT-5.6 Sol", "opus", {"reviewer_model": "GPT-5.6 Sol", "worker_model": "opus"}),
        ("", ":max", {"worker_effort": "max"}),
        ("", "", {}),
    ],
)
def test_model_and_effort_options_split_into_run_choices(chatgpt, claude, expected):
    from lxreview.cli import run_choices

    assert run_choices(chatgpt, claude) == expected


def test_the_stream_lists_findings_and_colors_a_clean_outcome_green():
    from lxreview.timeline import describe, style

    event = {
        "kind": "review_received",
        "time": "",
        "pass_number": 1,
        "verdict": "SUBSTANTIAL_ISSUES",
        "substantial": 1,
        "non_blocking": 1,
        "findings": [{"id": "S1", "title": "Off by one"}, {"id": "N1", "title": "Typo"}],
    }
    texts = [line.split("  ", 1)[1] for line in describe(event)]
    assert texts[1:] == ["  S1 Off by one", "  N1 Typo"]
    assert style("  S1 Off by one") == "yellow" and style("  N1 Typo") == "dim"
    assert style("Finished: NO_VALID_SUBSTANTIAL_FINDINGS") == "bold green"


def test_setup_launcher_runs_this_interpreter_with_its_root(paths):
    import sys

    from lxreview.cli import _write_launcher

    _write_launcher(paths)
    text = paths.executable.read_text()
    assert text.splitlines()[0] == f"#!{sys.executable}"
    assert f"os.environ['LXREVIEW_HOME'] = {str(paths.root)!r}" in text
    assert paths.executable.stat().st_mode & 0o777 == 0o700


def test_pasteable_commands_use_the_bare_name_only_for_this_installation(tmp_path, monkeypatch):
    import sys

    import lxreview.cli as cli
    from lxreview.paths import Paths

    # uv tool install puts the command next to the tool environment's interpreter.
    venv = tmp_path / "tools/lxreview/bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("")
    (venv / "lxreview").write_text("")
    (venv / "lxreview").chmod(0o700)
    bindir = tmp_path / "home/.local/bin"
    bindir.mkdir(parents=True)
    (bindir / "lxreview").symlink_to(venv / "lxreview")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    default = Paths(tmp_path / "home/.lxreview")
    assert cli._launcher(default) == "lxreview"
    # Another root, or another installation's command, needs the explicit launcher.
    other = Paths(tmp_path / "scratch")
    assert cli._launcher(other) == str(other.executable)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "elsewhere/python"))
    assert cli._launcher(default) == "~/.lxreview/bin/lxreview"


def test_uninstall_clears_the_runtime_directory_and_names_the_package(paths, monkeypatch):
    import lxreview.cli as cli
    import lxreview.install.claude as claude

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    monkeypatch.setattr(cli, "_stop", lambda *args: None)
    monkeypatch.setattr(claude, "uninstall", lambda *args: None)
    paths.config.write_text("schema_version = 1\n")
    paths.local.mkdir()
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not paths.local.exists() and "uv tool uninstall lxreview" in result.output


@pytest.fixture
def update_calls(paths, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    import lxreview.cli as cli

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    paths.config.write_text("schema_version = 1\n")
    calls = []
    tools = {"dir": str(Path(sys.prefix).parent)}

    def run_process(argv, *args, **kwargs):
        calls.append(argv[1:])
        return subprocess.CompletedProcess(argv, 0, tools["dir"] + "\n", "")

    class Services:
        def __init__(self, *args):
            pass

        def status(self, name):
            return name == "browser"

        def start(self, name, argv):
            calls.append(["start " + name])

    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: "/usr/bin/" + name)
    monkeypatch.setattr(cli, "run_process", run_process)
    monkeypatch.setattr(cli, "Supervisor", Services)
    monkeypatch.setattr(cli, "_stop", lambda *args: calls.append(["stop services"]))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv, **k: calls.append(argv[1:]) or subprocess.CompletedProcess(argv, 0),
    )
    return calls, tools


def test_update_upgrades_with_uv_then_sets_up_and_restarts_with_the_new_code(update_calls):
    calls, _ = update_calls
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["tool", "dir"],
        # Stop first: services must not run code whose files the upgrade replaces.
        ["stop services"],
        ["tool", "upgrade", "lxreview"],
        ["setup", "--mode", "host-browser"],
        ["start"],
    ]
    assert "Restarted: browser" in result.output


def test_update_from_a_checkout_and_refusal_outside_uv_tools(update_calls, tmp_path):
    calls, tools = update_calls
    assert runner.invoke(app, ["update", "--source", str(tmp_path)]).exit_code == 0
    assert ["tool", "install", "--reinstall", "--managed-python", str(tmp_path)] in calls
    calls.clear()
    tools["dir"] = str(tmp_path / "other-tools")
    result = runner.invoke(app, ["update"])
    assert "uv tool install" in str(result.exception)
    assert calls == [["tool", "dir"]]


@pytest.mark.parametrize(
    ("choices", "reviewer", "worker"),
    [
        (
            {},
            "ChatGPT (current model), highest reasoning",
            "Claude Code's default model and effort",
        ),
        (
            {"reviewer_model": "sol", "reviewer_effort": "high", "worker_model": "opus"},
            "ChatGPT (sol), high reasoning",
            "Claude (opus), default effort",
        ),
        ({"worker_effort": "xhigh"}, None, "Claude (default model), xhigh effort"),
    ],
)
def test_run_describes_its_choices_in_words(choices, reviewer, worker):
    from lxreview.cli import _described
    from lxreview.config import Config

    described = _described(Config().with_choices(choices))
    assert described["worker"] == worker
    assert reviewer is None or described["reviewer"] == reviewer
    # No MODEL:EFFORT codes such as default:default reach the user.
    assert ":" not in "".join(described.values())


@pytest.mark.parametrize(
    ("choices", "publishing"),
    [
        ({}, "automatic"),
        ({"push": "ask"}, "commits automatically, pushes after your approval"),
        ({"commit": "ask"}, "commits after your approval, then pushes automatically"),
        ({"commit": "ask", "push": "ask"}, "each after your approval"),
    ],
)
def test_run_describes_commit_and_push_approval(choices, publishing):
    from lxreview.cli import _described
    from lxreview.config import Config

    assert _described(Config().with_choices(choices))["publishing"] == publishing


def test_commit_and_push_options_become_run_choices():
    from lxreview.cli import run_choices
    from lxreview.config import Config

    choices = run_choices("", "opus", commit="ask", push="auto")
    assert choices == {"worker_model": "opus", "commit": "ask", "push": "auto"}
    assert Config().with_choices(choices).publish.commit == "ask"
    with pytest.raises(LXError, match="Invalid run choice: push='later'"):
        Config().with_choices(run_choices("", "", push="later"))


def test_approve_names_the_step_the_run_waits_for(paths, tmp_path, monkeypatch):
    from lxreview.config import Config
    from lxreview.runs import RunStore

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    Config().save(paths)
    monkeypatch.setattr(RunStore, "observed", lambda self, config: self.load())
    identity = {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}
    store = RunStore.create(
        paths, tmp_path, "https://github.com/org/repo/pull/1", identity, 5, tmp_path / "audit"
    )
    store.update(status="RUNNING")
    result = runner.invoke(app, ["approve", store.id, "commit"])
    assert "is not waiting for approval to commit" in str(result.exception)
    store.update(awaiting="push")
    # A repeated or stale approval of the commit never lets the push through.
    result = runner.invoke(app, ["approve", store.id, "commit"])
    assert "it waits for approval to push" in str(result.exception)
    assert "approved" not in store.load()
    result = runner.invoke(app, ["approve", store.id, "merge"])
    assert "Approve commit or push" in str(result.exception)
    result = runner.invoke(app, ["approve", store.id, "push"])
    assert result.exit_code == 0 and f"Approved the push for {store.id}" in result.output
    assert store.load()["approved"] == "push"
    # A stop that kills the waiting worker withdraws its request, so a resumed run starts clean.
    store.finish("CANCELLED")
    assert store.load()["awaiting"] is None and store.load()["approved"] is None
    result = runner.invoke(app, ["approve", store.id, "push"])
    assert "is not waiting for approval to push" in str(result.exception)


def test_one_active_run_at_a_time_across_repositories(paths, tmp_path, monkeypatch):
    import lxreview.process as process
    from lxreview.cli import _refuse_while_active
    from lxreview.config import Config
    from lxreview.runs import RunStore

    identity = {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}
    other = tmp_path / "other-repository"
    store = RunStore.create(
        paths, other, "https://github.com/org/other/pull/3", identity, 5, tmp_path / "audit"
    )
    store.update(status="RUNNING")
    alive = {"value": True}
    monkeypatch.setattr(process.Supervisor, "status", lambda self, name: alive["value"])
    with pytest.raises(LXError, match=f"Run {store.id} for .*/other/pull/3 is still active") as err:
        _refuse_while_active(paths, Config())
    assert err.value.category == Category.BUSY
    assert f"lxreview stop {store.id}" in str(err.value)
    # A run whose worker is gone no longer blocks; it is marked interrupted instead.
    alive["value"] = False
    _refuse_while_active(paths, Config())
    assert store.load()["status"] == "INTERRUPTED"


def test_run_announcement_is_ready_to_relay():
    from lxreview.cli import _announcement

    described = {
        "reviewer": "ChatGPT (sol), medium reasoning",
        "worker": "Claude (opus), xhigh effort",
        "publishing": "commits automatically, pushes after your approval",
    }
    text = _announcement(
        "lr-1", "https://github.com/org/repo/pull/1", described, "lxreview watch lr-1"
    )
    assert text.startswith("Started review run `lr-1` for https://github.com/org/repo/pull/1.\n")
    assert (
        "- Reviewer: ChatGPT (sol), medium reasoning\n- Worker: Claude (opus), xhigh effort\n"
        "- Commits and pushes: commits automatically, pushes after your approval\n" in text
    )
    assert "`/review-stop lr-1`" in text
    assert "```bash\nlxreview watch lr-1\n```" in text


def test_chat_watch_continuation_explains_refusal_without_replaying_call(
    paths, tmp_path, monkeypatch
):
    import lxreview.cli as cli
    from lxreview.config import Config
    from lxreview.runs import RunStore

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    Config().save(paths)
    identity = {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}
    store = RunStore.create(
        paths, tmp_path, "https://github.com/org/repo/pull/1", identity, 5, tmp_path / "audit"
    )
    store.event(
        "claude",
        event={
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/tmp/x.py"},
                    }
                ]
            },
        },
    )
    after = len((store.directory / "events.jsonl").read_text().splitlines())
    store.event(
        "claude",
        event={
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "w1",
                        "is_error": True,
                        "content": "PreToolUse:Write hook error: [guard]: Outside scratch directory",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(RunStore, "observed", lambda self, config: self.load())
    monkeypatch.setattr(cli, "CHAT_WATCH_SECONDS", 0)
    result = runner.invoke(app, ["watch", store.id, "--chat", "--after", str(after)])
    assert result.exit_code == 0, result.output
    assert "refused by the guard: Outside scratch directory" in result.stdout
    assert "Editing" not in result.stdout


@pytest.mark.parametrize(("ssh", "role"), [(True, "host"), (False, "workstation")])
def test_local_browser_setup_over_ssh_defaults_to_the_repository_host(
    paths, monkeypatch, ssh, role
):
    from lxreview import cli
    from lxreview.config import Config

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    if ssh:
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 10.0.0.5 22")
    else:
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(cli.Supervisor, "stop", lambda self, name: None)
    monkeypatch.setattr("lxreview.process.detect_supervisor", lambda paths: "tmux-scope")
    monkeypatch.setattr(cli, "binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "doctor", lambda **kwargs: None)
    result = runner.invoke(
        app, ["setup", "--mode", "local-browser", "--skip-runtime", "--skip-integration"]
    )
    assert result.exit_code == 0, result.output
    config = Config.load(paths)
    assert (config.mode, config.role, config.browser.placement) == ("local-browser", role, "local")


PR = "https://github.com/org/repo/pull/1"
DRAFT = "https://github.com/org/repo/pull/1#pullrequestreview-7"


@pytest.fixture
def checkout(paths, tmp_path, monkeypatch):
    """`run` and `resume` against a fake checkout, gh and supervisor."""
    import lxreview.cli as cli
    from lxreview.config import Config

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o700)
    config = Config()
    config.runtime.claude = str(claude)
    config.save(paths)
    calls: list[str] = []
    github = {"pending": None}

    class Checkout:
        def __init__(self, path, paths):
            pass

        def head(self):
            return "a" * 40

        def audit_root(self):
            return tmp_path / "audit"

        def verify_identity(self, identity):
            calls.append("verify_identity")

        def preflight(self, target, settle=0):
            calls.append("preflight")
            return {"head": "a" * 40, "branch": "f", "upstream": "origin/f", "remote_url": "u"}

        def review_checkpoint(self, target):
            calls.append("review_checkpoint")
            return {"head": "a" * 40}

    def pending_review(target, paths, config):
        calls.append("pending_review")
        return github["pending"]

    monkeypatch.setattr("lxreview.git.Repository", Checkout)
    monkeypatch.setattr("lxreview.comments.pending_review", pending_review)
    monkeypatch.setattr(
        cli.Supervisor, "start", lambda self, name, argv, cwd=None: calls.append(name)
    )
    monkeypatch.setattr(cli.Supervisor, "status", lambda self, name: False)
    return calls, github


def test_a_review_only_run_needs_only_a_clean_checkout_at_the_pr_head(checkout, paths, tmp_path):
    from lxreview.runs import RunStore

    calls, _ = checkout
    result = runner.invoke(app, ["run", PR, "--repo", str(tmp_path), "--review-only"])
    assert result.exit_code == 0, result.output
    started = json.loads(result.stdout)
    # No branch, upstream or push policy: other people's PRs and detached checkouts work.
    assert calls == ["review_checkpoint", "pending_review", "run-" + started["run_id"]]
    state = RunStore(paths, started["run_id"]).load()
    assert state["review_only"] is True and state["max_passes"] == 5
    assert started["message"].startswith(f"Started review-only run `{started['run_id']}` for {PR}.")
    assert (
        "- Accepted findings will be added to a pending review on the PR, visible only to you"
        " until you submit it." in started["message"]
    )
    assert "Commits and pushes" not in started["message"]


def test_a_fix_run_keeps_its_preflight(checkout, paths, tmp_path):
    from lxreview.runs import RunStore

    calls, _ = checkout
    result = runner.invoke(app, ["run", PR, "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    started = json.loads(result.stdout)
    assert calls == ["preflight", "run-" + started["run_id"]]
    state = RunStore(paths, started["run_id"]).load()
    assert state["review_only"] is False and state["substantial_only"] is False
    assert "Findings:" not in started["message"]


def test_a_substantial_only_run_says_so_when_it_starts(checkout, paths, tmp_path):
    from lxreview.runs import RunStore

    result = runner.invoke(app, ["run", PR, "--repo", str(tmp_path), "--substantial-only"])
    assert result.exit_code == 0, result.output
    started = json.loads(result.stdout)
    assert RunStore(paths, started["run_id"]).load()["substantial_only"] is True
    assert (
        "- Findings: substantial only; non-blocking findings are left alone\n"
        "- Commits and pushes: automatic\n" in started["message"]
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["https://gitlab.com/g/p/-/merge_requests/3"],
            "Review-only runs post GitHub review comments; GitLab merge requests are not supported",
        ),
        ([PR, "--push", "ask"], "neither commits nor pushes"),
    ],
)
def test_review_only_refuses_what_it_cannot_do(checkout, tmp_path, arguments, message):
    calls, _ = checkout
    result = runner.invoke(app, ["run", *arguments, "--repo", str(tmp_path), "--review-only"])
    assert result.exit_code == 1 and message in str(result.exception)
    assert calls == []


def test_review_only_refuses_while_the_user_has_a_pending_review(checkout, tmp_path):
    calls, github = checkout
    github["pending"] = DRAFT
    result = runner.invoke(app, ["run", PR, "--repo", str(tmp_path), "--review-only"])
    assert f"You already have a pending review on this PR: {DRAFT}" in str(result.exception)
    assert not any(call.startswith("run-") for call in calls)


@pytest.mark.parametrize(
    ("completed", "pending", "error"),
    [
        (0, None, None),
        # A run interrupted while posting may have created the review it never recorded.
        (0, DRAFT, "already have a pending review"),
        (1, None, None),
    ],
)
def test_resuming_a_review_only_run_checks_for_its_review(
    checkout, paths, tmp_path, completed, pending, error
):
    from lxreview.runs import RunStore

    calls, github = checkout
    github["pending"] = pending
    store = RunStore.create(paths, tmp_path, PR, {"head": "a" * 40}, 1, tmp_path / "audit", True)
    store.update(status="FAILED", completed_pass=completed)
    result = runner.invoke(app, ["resume", store.id])
    if error:
        assert error in str(result.exception)
        assert "run-" + store.id not in calls
    else:
        assert result.exit_code == 0, result.output
        assert calls == ["review_checkpoint", "pending_review", "run-" + store.id]


@pytest.mark.parametrize("directory", ["pass-01", "pass-02", "pass-02-interrupted-abc"])
def test_a_review_only_run_that_posted_before_recording_its_pass_is_not_resumed(
    checkout, paths, tmp_path, directory
):
    from pathlib import Path

    from lxreview.runs import RunStore

    calls, _ = checkout
    store = RunStore.create(paths, tmp_path, PR, {"head": "a" * 40}, 1, tmp_path / "audit", True)
    # The worker stopped after creating the review; the user has since submitted it.
    store.update(status="INTERRUPTED", completed_pass=0)
    posted = Path(store.load()["audit"]) / directory / "review-posted.json"
    posted.parent.mkdir(parents=True)
    posted.write_text(json.dumps({"id": 7, "html_url": DRAFT}))
    result = runner.invoke(app, ["resume", store.id])
    assert f"already created its review: {DRAFT}" in str(result.exception)
    assert calls == [] and posted.exists()


def test_the_timeline_links_the_draft_review():
    from lxreview.timeline import PHASES, describe, style

    event = {"kind": "review_posted", "time": "", "inline": 2, "summary": 1, "url": DRAFT}
    (line,) = [text.split("  ", 1)[1] for text in describe(event)]
    assert (
        line == f"Draft review created on GitHub: 2 inline, 1 in summary — {DRAFT}; submit it there"
    )
    assert style(line) == "bold green" and style("Finished: REVIEW_DRAFTED") == "bold green"
    assert PHASES["commenting"] == "drafting the GitHub review"


def test_review_only_custom_pass_limit_and_announcement(checkout, paths, tmp_path):
    from lxreview.runs import RunStore

    result = runner.invoke(
        app, ["run", PR, "--repo", str(tmp_path), "--review-only", "--max-passes", "3"]
    )
    assert result.exit_code == 0
    started = json.loads(result.stdout)
    assert RunStore(paths, started["run_id"]).load()["max_passes"] == 3
    assert "up to 3 review passes on the same PR head" in started["message"]


def test_resume_review_only_refuses_changed_starting_head(checkout, paths, tmp_path):
    from lxreview.runs import RunStore

    store = RunStore.create(paths, tmp_path, PR, {"head": "b" * 40}, 3, tmp_path / "audit", True)
    store.update(status="FAILED", completed_pass=1)
    result = runner.invoke(app, ["resume", store.id])
    assert "The PR head changed between passes" in str(result.exception)


def test_show_defaults_to_latest_pass_with_comments(checkout, paths, tmp_path):
    from pathlib import Path

    from lxreview.runs import RunStore

    store = RunStore.create(paths, tmp_path, PR, {"head": "a" * 40}, 3, tmp_path / "audit", True)
    store.update(**{"pass": 2})
    directory = Path(store.load()["audit"]) / "pass-02"
    directory.mkdir(parents=True)
    (directory / "comments.json").write_text('{"findings": []}')
    result = runner.invoke(app, ["show", store.id, "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"comments.json": '{"findings": []}'}
