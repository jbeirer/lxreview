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
