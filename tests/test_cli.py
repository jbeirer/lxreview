import json

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
        "finding_evaluated", pass_number=1, finding="S1", decision="ACCEPTED", title="Off by one"
    )
    store.event(
        "claude",
        event={
            "type": "assistant",
            "message": {
                "content": [
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
        "Editing src/a.py",
        "$ python -m pytest tests",
        "Finished: CLEAN",
    ):
        assert text in result.stdout
    assert result.stdout.count("Status") == 1
    raw = runner.invoke(app, ["watch", store.id, "--raw"])
    assert all(json.loads(line)["kind"] for line in raw.stdout.splitlines())


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
    # PATH is never modified, so every pasteable command names the launcher itself.
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
