import json

import pytest
from typer.testing import CliRunner

from lxreview.cli import app

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
    from lxreview.cli import model_choices

    assert model_choices(chatgpt, claude) == expected


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


def test_uninstall_removes_only_its_own_command_link(paths, tmp_path, monkeypatch):
    import lxreview.cli as cli
    import lxreview.install.claude as claude

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_stop", lambda *args: None)
    monkeypatch.setattr(claude, "uninstall", lambda *args: None)
    paths.config.write_text("schema_version = 1\n")
    paths.executable.write_text("")
    paths.executable.chmod(0o700)
    paths.link.parent.mkdir(parents=True)
    paths.link.symlink_to(paths.executable)
    monkeypatch.setenv("PATH", str(paths.link.parent))
    # A launcher found on PATH is shown as the bare command.
    assert cli._launcher(paths) == "lxreview"
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not paths.link.is_symlink()


def test_uninstall_keeps_a_command_link_it_does_not_own(paths, tmp_path, monkeypatch):
    import lxreview.cli as cli
    import lxreview.install.claude as claude

    monkeypatch.setenv("LXREVIEW_HOME", str(paths.root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_stop", lambda *args: None)
    monkeypatch.setattr(claude, "uninstall", lambda *args: None)
    paths.config.write_text("schema_version = 1\n")
    paths.link.parent.mkdir(parents=True)
    paths.link.symlink_to(tmp_path / "other/bin/lxreview")
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert paths.link.is_symlink()
