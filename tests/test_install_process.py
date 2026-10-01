import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from lxreview.config import Config
from lxreview.errors import LXError
from lxreview.install.runtime import extract
from lxreview.paths import write_json
from lxreview.process import Supervisor
from lxreview.runs import RunStore
from lxreview.services import desktop_command


@pytest.mark.parametrize("member", ["../outside", "/absolute"])
def test_zip_traversal_rejected(tmp_path, member):
    bundle = tmp_path / "bad.zip"
    with zipfile.ZipFile(bundle, "w") as out:
        out.writestr(member, "bad")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(LXError):
        extract(bundle, destination)


def test_supervisor_idempotence_and_scoped_stop(paths, monkeypatch):
    calls = []
    active = False

    def run(argv, *a, **kw):
        nonlocal active
        calls.append(argv)
        code = 0
        if "is-active" in argv:
            code = 0 if active else 3
        if argv[0] == "/usr/bin/systemd-run":
            active = True
        if "stop" in argv:
            active = False
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("lxreview.process.run", run)
    supervisor = Supervisor(paths, Config())
    supervisor.start("browser", ["/owned/browser"])
    supervisor.start("browser", ["/owned/browser"])
    assert len([c for c in calls if c[0].endswith("systemd-run")]) == 1
    supervisor.stop("browser")
    supervisor.stop("browser")
    assert len([c for c in calls if "stop" in c]) == 1
    assert "--property=KillMode=control-group" in calls[0]
    assert not any("pkill" in str(c) for c in calls)


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_stop_reaches_unit_orphaned_by_deleted_installation(paths, monkeypatch, system):
    calls = []

    def run(argv, *a, **kw):
        calls.append(argv)
        loaded = argv[1] == "print" or argv[-1].endswith(".service")
        return subprocess.CompletedProcess(argv, 0 if loaded else 3, "", "")

    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("lxreview.process.run", run)
    monkeypatch.setattr("lxreview.process.platform.system", lambda: system)
    supervisor = Supervisor(paths, Config())
    unit = supervisor.unit("browser")
    # No service record: it was deleted together with the installation that started the unit.
    supervisor.stop("browser")
    if system == "Linux":
        assert ["/usr/bin/systemctl", "--user", "stop", unit + ".service"] in calls
        assert not any("stop" in argv and argv[-1].endswith(".scope") for argv in calls)
    else:
        assert calls[-1][1:] == ["bootout", f"gui/{__import__('os').getuid()}/org.lxreview.{unit}"]


def test_stale_service_on_another_host_refuses(paths):
    supervisor = Supervisor(paths, Config())
    write_json(
        paths.root / f"state/services/{supervisor.unit('browser')}.json", {"host": "another-host"}
    )
    with pytest.raises(LXError, match="another-host"):
        supervisor.stop("browser")


def test_vnc_uses_owned_config_and_loopback(paths, monkeypatch):
    monkeypatch.setattr("lxreview.services.binary", lambda name: "/usr/bin/" + name)
    (paths.root / "secrets/vnc-password").write_bytes(b"password")
    config = Config()
    # A test-only high display avoids the user's real :99 desktop.
    config.runtime.display = 411
    command = desktop_command(paths, config)
    assert command[command.index("-localhost") + 1] == "yes"
    assert str(paths.root / "state/vnc/xstartup") in command
    assert "-fg" in command


def test_run_id_path_traversal_rejected(paths):
    with pytest.raises(LXError):
        RunStore(paths, "../../secret")


def test_reversible_claude_integration_preserves_unrelated_entries(paths, tmp_path, monkeypatch):
    from lxreview.install import claude

    home = tmp_path / "account"
    home.mkdir()
    original = {"mcpServers": {"other": {"command": "/other"}}, "unrelated": True}
    file = home / ".claude.json"
    file.write_text(json.dumps(original))
    config = Config()
    config.runtime.claude = "/fake/claude"
    calls = []

    def run(argv, *args, **kwargs):
        calls.append(argv)
        assert "--scope" in argv and "user" in argv
        data = json.loads(file.read_text())
        if "add-json" in argv:
            data["mcpServers"]["lxreview-reviewer"] = json.loads(argv[-1])
        else:
            data["mcpServers"].pop("lxreview-reviewer")
        file.write_text(json.dumps(data))

    monkeypatch.setattr(claude, "run", run)
    claude.install(paths, config, home)
    claude.install(paths, config, home)
    assert len(calls) == 1
    assert (home / ".claude/commands/review-loop.md").is_symlink()
    claude.uninstall(paths, config)
    assert json.loads(file.read_text()) == original
    assert not (home / ".claude/commands/review-loop.md").exists()


def test_reinstall_adopts_integration_left_by_deleted_root(paths, tmp_path, monkeypatch):
    import shutil

    from lxreview.install import claude

    home = tmp_path / "account"
    home.mkdir()
    file = home / ".claude.json"
    file.write_text(json.dumps({"mcpServers": {}}))
    config = Config()
    config.runtime.claude = "/fake/claude"

    def run(argv, *args, **kwargs):
        data = json.loads(file.read_text())
        if "add-json" in argv:
            data["mcpServers"]["lxreview-reviewer"] = json.loads(argv[-1])
        else:
            data["mcpServers"].pop("lxreview-reviewer")
        file.write_text(json.dumps(data))

    monkeypatch.setattr(claude, "run", run)
    claude.install(paths, config, home)
    # A manual rm -rf of the root loses the ownership record and the link targets.
    (paths.root / "state/claude-integration.json").unlink()
    shutil.rmtree(paths.root / "claude")
    claude.install(paths, config, home)
    claude.uninstall(paths, config)
    assert json.loads(file.read_text()) == {"mcpServers": {}}
    assert not any((home / ".claude/commands").iterdir())


def test_loop_and_watch_commands_follow_the_run_in_the_chat(paths, tmp_path, monkeypatch):
    from lxreview.install import claude

    home = tmp_path / "account"
    home.mkdir()
    file = home / ".claude.json"
    file.write_text(json.dumps({"mcpServers": {}}))
    config = Config()
    config.runtime.claude = "/fake/claude"

    def run(argv, *args, **kwargs):
        file.write_text(json.dumps({"mcpServers": {"lxreview-reviewer": json.loads(argv[-1])}}))

    monkeypatch.setattr(claude, "run", run)
    claude.install(paths, config, home)
    commands = home / ".claude/commands"
    for name in ("review-loop", "review-watch"):
        text = (commands / f"{name}.md").read_text()
        assert "## Follow the run in this chat" in text
        assert f"Absolute executable: {paths.executable}" in text
    assert "watch <quoted-run-id> --chat" in (commands / "review-watch.md").read_text()
    loop = (commands / "review-loop.md").read_text()
    assert "Do not ask the user" in loop and "--chatgpt MODEL[:EFFORT]" in loop
    assert "output's `message` value, copied exactly" in loop
    assert "Follow the run" not in (commands / "review-status.md").read_text()
    assert "[--max-passes N] [--commit ask] [--push ask] [--substantial-only]" in loop
    assert "only after the user explicitly approves" in loop
    approve = (commands / "review-approve.md").read_text()
    assert "approve <run-id> <step>" in approve and "commit or push" in approve
    assert f"Absolute executable: {paths.executable}" in approve


def test_claude_collision_refused_before_mutation(paths, tmp_path):
    from lxreview.install.claude import install

    home = tmp_path / "account"
    directory = home / ".claude/commands"
    directory.mkdir(parents=True)
    custom = directory / "review-show.md"
    custom.write_text("user's own command")
    with pytest.raises(LXError, match="not owned"):
        install(paths, Config(), home)
    assert custom.read_text() == "user's own command"
    assert not (directory / "review-loop.md").exists()


def test_existing_identical_mcp_is_adopted_not_re_added(paths, tmp_path, monkeypatch):
    from lxreview.install import claude

    home = tmp_path / "account"
    home.mkdir()
    original = {
        "mcpServers": {
            "lxreview-reviewer": {
                "type": "stdio",
                "command": str(paths.executable),
                "args": ["mcp"],
                "env": {"LXREVIEW_HOME": str(paths.root)},
            }
        }
    }
    file = home / ".claude.json"
    file.write_text(json.dumps(original))

    calls = []
    monkeypatch.setattr(claude, "run", lambda argv, *args, **kwargs: calls.append(argv))
    claude.install(paths, Config(), home)
    assert calls == []
    # The entry embeds this root, so only this installation can have created it.
    claude.uninstall(paths, Config())
    assert [argv[1:] for argv in calls] == [
        ["mcp", "remove", "--scope", "user", "lxreview-reviewer"]
    ]


def test_launchd_exited_service_is_not_reported_running(paths, monkeypatch):
    import socket

    config = Config()
    config.runtime.supervisor = "launchd"
    supervisor = Supervisor(paths, config)
    write_json(
        paths.root / f"state/services/{supervisor.unit('browser')}.json", {"host": socket.getfqdn()}
    )
    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.process.run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "state = not running", ""),
    )
    assert not supervisor.status("browser")


@pytest.mark.parametrize("name", ["desktop", "browser"])
def test_vnc_and_browser_share_private_xauthority(paths, monkeypatch, name):
    from lxreview.services import service_exec

    monkeypatch.setattr("lxreview.services.desktop_command", lambda *a: ["/fake/vnc"])
    monkeypatch.setattr("lxreview.services.Path.exists", lambda p: True)
    captured = []
    monkeypatch.setattr("lxreview.services.os.execve", lambda *args: captured.append(args))
    old_mask = __import__("os").umask(0o077)
    try:
        service_exec(paths, Config(), name)
    finally:
        __import__("os").umask(old_mask)
    assert captured[0][2]["XAUTHORITY"] == str(paths.root / "state/vnc/.Xauthority")
    if name == "browser":
        assert captured[0][1] == [str(paths.executable), "browser-server"]


def test_supervisor_preserves_launch_environment_and_uses_distinct_tmux_servers(paths, monkeypatch):
    config = Config()
    config.runtime.supervisor = "tmux-scope"
    monkeypatch.setenv("SSH_AUTH_SOCK", "/safe/agent.sock")
    calls = []
    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.process.run",
        lambda argv, *a, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    supervisor = Supervisor(paths, config)
    supervisor.start("browser", ["/owned/browser"])
    supervisor.start("desktop", ["/owned/desktop"])
    assert supervisor.tmux_socket("browser") != supervisor.tmux_socket("desktop")
    assert all("-L" not in argv and "/dev/null" in argv for argv in calls)
    state = json.loads(
        (paths.root / f"state/services/{supervisor.unit('browser')}.json").read_text()
    )
    assert state["environment"]["SSH_AUTH_SOCK"] == "/safe/agent.sock"
    assert "supervised-exec" in str(calls)
    # Sockets and temporary files stay node-local: AFS homes cannot hold unix sockets.
    assert supervisor.tmux_socket("browser").parent == paths.local
    assert state["environment"]["TMPDIR"] == str(paths.local)
    assert paths.local.is_dir()


def test_runtime_directory_is_node_local_when_the_host_provides_one(paths, tmp_path, monkeypatch):
    import os

    assert paths.local == paths.root / "run"
    (tmp_path / "run-user" / str(os.getuid())).mkdir(parents=True)
    monkeypatch.setattr("lxreview.paths.RUN_USER", tmp_path / "run-user")
    assert paths.local == tmp_path / f"run-user/{os.getuid()}/lxreview-{paths.key}"


@pytest.mark.parametrize(
    ("listing", "refused"),
    [
        ("User's (AFS ID 1) rxkad tokens for cern.ch [Expires {when}]\n   --End of list--", False),
        ("User's (AFS ID 1) rxkad tokens for cern.ch [>> Expired <<]\n", True),
        ("   --End of list--\n", True),
    ],
)
def test_runs_need_an_afs_token_that_outlasts_the_loop(paths, monkeypatch, listing, refused):
    from datetime import datetime, timedelta

    from lxreview.process import afs_token, require_afs_token

    later = datetime.now() + timedelta(hours=5)
    # tokens prints ctime's space-padded day, e.g. "Sep  8 01:39".
    text = listing.format(when=later.strftime("%b ") + f"{later.day:2} " + later.strftime("%H:%M"))
    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.process.run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0, text, ""),
    )
    home = Path("/afs/cern.ch/user/j/jdoe/.lxreview")
    # Only locations on AFS are checked.
    assert afs_token(paths.root, paths) is None
    require_afs_token(paths, paths.root)
    if refused:
        with pytest.raises(LXError, match="kinit and aklog"):
            require_afs_token(paths, paths.root, home)
    else:
        assert afs_token(home, paths) == ("cern.ch", later.replace(second=0, microsecond=0))
        require_afs_token(paths, home)


@pytest.mark.parametrize("failure", ["missing", "exit"])
def test_runs_refuse_an_afs_location_whose_token_cannot_be_inspected(paths, monkeypatch, failure):
    from lxreview.errors import Category
    from lxreview.process import afs_token, require_afs_token

    def binary(name):
        if failure == "missing":
            raise LXError(Category.UNAVAILABLE, f"Missing {name}")
        return "/usr/bin/" + name

    listing = "User's (AFS ID 1) rxkad tokens for cern.ch [Expires Dec 31 23:59]\n"
    monkeypatch.setattr("lxreview.process.binary", binary)
    monkeypatch.setattr(
        "lxreview.process.subprocess.run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, listing, ""),
    )
    home = Path("/afs/cern.ch/user/j/jdoe/.lxreview")
    assert afs_token(paths.root, paths) is None
    require_afs_token(paths, paths.root)
    assert afs_token(home, paths) == ("cern.ch", None)
    with pytest.raises(LXError, match="kinit and aklog"):
        require_afs_token(paths, paths.root, home)


def test_runs_refuse_an_afs_token_that_expires_mid_loop(paths, monkeypatch):
    from datetime import datetime, timedelta

    from lxreview.process import require_afs_token

    soon = datetime.now() + timedelta(minutes=30)
    text = f"User's (AFS ID 1) rxkad tokens for cern.ch [Expires {soon:%b %d %H:%M}]"
    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.process.run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 0, text, ""),
    )
    with pytest.raises(LXError, match=f"expires at {soon:%H:%M}"):
        require_afs_token(paths, Path("/afs/cern.ch/user/j/jdoe/repo"))


def test_supervisor_uses_recorded_kind_after_config_changes(paths, monkeypatch):
    import socket

    config = Config()
    supervisor = Supervisor(paths, config)
    write_json(
        paths.root / f"state/services/{supervisor.unit('browser')}.json",
        {"host": socket.getfqdn(), "supervisor": "launchd"},
    )
    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    calls = []

    def run(argv, *args, **kwargs):
        stopped = any("bootout" in call for call in calls)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3 if stopped else 0, "state = running", "")

    monkeypatch.setattr("lxreview.process.run", run)
    supervisor.stop("browser")
    assert all(argv[0] == "/usr/bin/launchctl" for argv in calls)


@pytest.mark.parametrize("entry", [{}, None, ""])
def test_existing_empty_registration_is_preserved(paths, tmp_path, entry):
    from lxreview.install.claude import install

    home = tmp_path / "account"
    home.mkdir()
    config = home / ".claude.json"
    original = json.dumps({"mcpServers": {"lxreview-reviewer": entry}})
    config.write_text(original)
    with pytest.raises(LXError, match="refusing to overwrite"):
        install(paths, Config(), home)
    assert config.read_text() == original
    assert not (home / ".claude/commands/review-loop.md").exists()


def test_partial_command_install_remains_removable(paths, tmp_path, monkeypatch):
    from pathlib import Path

    from lxreview.install import claude

    home = tmp_path / "account"
    home.mkdir()
    original = Path.symlink_to

    def fail_second(path, *args, **kwargs):
        if path.name == "review-status.md":
            raise OSError("simulated failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "symlink_to", fail_second)
    with pytest.raises(OSError):
        claude.install(paths, Config(), home)
    assert (home / ".claude/commands/review-loop.md").is_symlink()
    claude.uninstall(paths, Config())
    assert not (home / ".claude/commands/review-loop.md").is_symlink()


def test_stop_unloads_exited_launchd_job(paths, monkeypatch):
    import socket

    supervisor = Supervisor(paths, Config())
    write_json(
        paths.root / f"state/services/{supervisor.unit('browser')}.json",
        {
            "host": socket.getfqdn(),
            "supervisor": "launchd",
        },
    )
    calls = []
    loaded = True

    def run(argv, *a, **kw):
        nonlocal loaded
        calls.append(argv)
        if "bootout" in argv:
            loaded = False
        return subprocess.CompletedProcess(argv, 0 if loaded else 3, "state = not running", "")

    monkeypatch.setattr("lxreview.process.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("lxreview.process.run", run)
    supervisor.stop("browser")
    supervisor.stop("browser")
    assert not loaded
    assert any("bootout" in argv for argv in calls)
