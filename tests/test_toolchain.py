import os
from pathlib import Path

import pytest

from lxreview import toolchain
from lxreview.config import Config
from lxreview.errors import LXError
from lxreview.security import shell_unsafe


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cargo/bin").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    return home


def test_project_tools_come_first_and_only_existing_directories_are_searched(tmp_path, home):
    repo = tmp_path / "repo"
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / "node_modules/.bin").mkdir(parents=True)
    config = Config()
    config.verify.path = [str(tmp_path / "missing"), str(tmp_path)]
    path = toolchain.search_path(repo, config, inherited="/cvmfs/stack/bin:/usr/bin").split(":")
    assert path[:3] == [str(repo / ".venv/bin"), str(repo / "node_modules/.bin"), str(tmp_path)]
    assert str(home / ".cargo/bin") in path
    assert str(tmp_path / "missing") not in path and "/cvmfs/stack/bin" not in path
    # The setup's own order wins; every directory appears once and the system is last.
    assert path.index("/usr/bin") < path.index(str(home / ".cargo/bin"))
    assert path[-3:] == ["/bin", "/usr/sbin", "/sbin"] and path.count("/usr/bin") == 1


def test_push_never_finds_tools_inside_the_repository(paths, home):
    path = toolchain.push_environment(paths, Config())["PATH"].split(":")
    assert str(home / ".cargo/bin") in path
    assert not any(".venv" in d or "node_modules" in d for d in path)


def test_setup_command_exports_its_environment_without_reserved_variables(paths, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "setup.sh").write_text(
        "export STACK_ROOT=/cvmfs/stack\nexport PATH=/cvmfs/stack/bin:$PATH\n"
        "export HOME=/elsewhere\nexport LD_PRELOAD=/evil.so\necho noise\n"
    )
    config = Config()
    config.verify.setup = {str(repo): f"source {tmp_path / 'setup.sh'}"}
    exported = toolchain.setup_environment(repo, paths, config)
    assert exported["STACK_ROOT"] == "/cvmfs/stack"
    assert exported["PATH"].startswith("/cvmfs/stack/bin:")
    assert "HOME" not in exported and "LD_PRELOAD" not in exported
    assert toolchain.setup_environment(tmp_path, paths, config) == {}


def test_failing_setup_command_stops_the_turn(paths, tmp_path):
    config = Config()
    config.verify.setup = {str(tmp_path): "false"}
    with pytest.raises(LXError, match="setup command failed"):
        toolchain.setup_environment(tmp_path, paths, config)


def test_caches_are_private_to_the_turn_and_pre_commit_keeps_its_hooks(tmp_path, home):
    (home / ".cache/pre-commit").mkdir(parents=True)
    (home / ".cache/pre-commit/db.db").write_bytes(b"sqlite")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = toolchain.cache_environment(scratch)
    assert all(value.startswith(str(scratch)) for value in env.values())
    assert (Path(env["PRE_COMMIT_HOME"]) / "db.db").read_bytes() == b"sqlite"


def test_turn_environment_hides_agents_and_keeps_the_configured_environment(
    paths, tmp_path, home, monkeypatch
):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/agent.sock")
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = Config()
    config.verify.env = {"CI": "1"}
    env = toolchain.turn_environment(repo, paths, config, scratch, {"STACK": "x"})
    assert "SSH_AUTH_SOCK" not in env
    assert env["CI"] == "1" and env["STACK"] == "x" and env["TMPDIR"] == str(scratch)
    assert os.pathsep in env["PATH"]


@pytest.mark.parametrize(
    ("command", "unsafe"),
    [
        ("pytest 'tests/test_x.py::test_y[param]'", False),
        ("pytest -k 'not (slow or gpu)'", False),
        ('pytest -k "a and b"', False),
        ('pytest -k "$(id)"', True),
        ("pytest tests/*.py", True),
        ("pytest 'unterminated", True),
        ("make test; rm -rf src", True),
        ("make test 'a;b'", False),
    ],
)
def test_single_quotes_keep_patterns_literal(command, unsafe):
    assert shell_unsafe(command) is unsafe
