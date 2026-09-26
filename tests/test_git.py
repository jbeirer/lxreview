import subprocess

import pytest

from lxreview.errors import LXError
from lxreview.git import Repository
from lxreview.security import allowed


@pytest.fixture
def repo(paths, tmp_path, monkeypatch):
    directory = tmp_path / "repo"
    directory.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "feature")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "initial")
    git("remote", "add", "origin", "https://github.com/org/repo.git")
    git("update-ref", "refs/remotes/origin/feature", git("rev-parse", "HEAD"))
    git("branch", "--set-upstream-to=origin/feature")
    # Avoid consulting the real account's Git configuration during policy checks.
    from lxreview import process

    original = process.environment
    monkeypatch.setattr(
        process,
        "environment",
        lambda paths: {
            **original(paths),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return Repository(directory, paths), git


def push(command, repository):
    return allowed(
        {"tool_name": "Bash", "tool_input": {"command": command}}, repository.path, "publish"
    )[0]


def test_only_the_explicit_upstream_push_is_allowed(repo):
    repository, _ = repo
    repository.check_push_policy()
    assert repository.push_command() == ["git", "push", "origin", "HEAD:refs/heads/feature"]
    assert push("git push origin HEAD:refs/heads/feature", repository)
    for command in (
        "git push",
        "git push origin HEAD:refs/heads/main",
        "git push --force origin HEAD:refs/heads/feature",
    ):
        assert not push(command, repository)


@pytest.mark.parametrize(
    "key,value", [("push.default", "matching"), ("remote.origin.push", "+HEAD:main")]
)
def test_settings_that_only_widen_a_bare_push_do_not_block(repo, key, value):
    # The explicit refspec pushes one branch whatever these say, so users keep their config.
    repository, git = repo
    git("config", key, value)
    repository.check_push_policy()
    assert push("git push origin HEAD:refs/heads/feature", repository)


@pytest.mark.parametrize(
    "key,value",
    [
        ("remote.origin.mirror", "true"),
        ("remote.origin.pushurl", "https://github.com/other/repo"),
        ("remote.pushDefault", "other"),
        ("branch.feature.pushRemote", "other"),
        ("url.https://other.invalid/.pushInsteadOf", "https://github.com/"),
        ("diff.external", "/tmp/command"),
    ],
)
def test_push_configuration_cannot_bypass_hook(repo, key, value):
    repository, git = repo
    git("config", key, value)
    with pytest.raises(LXError):
        repository.check_push_policy()
    assert not push("git push origin HEAD:refs/heads/feature", repository)


def test_preflight_compares_actual_head_with_both_remote_refs(repo, monkeypatch):
    repository, git = repo
    head = git("rev-parse", "HEAD")
    original = repository.call
    probes = []

    def call(*args):
        if args[0] == "ls-remote":
            probes.append(args)
            return head + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    assert repository.preflight("https://github.com/org/repo/pull/12")["head"] == head
    assert probes == [
        ("ls-remote", "https://github.com/org/repo.git", "refs/pull/12/head"),
        ("ls-remote", "origin", "refs/heads/feature"),
    ]
    (repository.path / "unrelated.txt").write_text("uncommitted")
    with pytest.raises(LXError, match="dirty"):
        repository.preflight("https://github.com/org/repo/pull/12")


def test_worktree_audit_stays_under_common_git_directory(repo, paths, tmp_path):
    repository, git = repo
    worktree = tmp_path / "worktree"
    git("worktree", "add", "-b", "another", str(worktree))
    assert Repository(worktree, paths).audit_root() == repository.path / ".git/review-loop"
    assert (worktree / ".git").is_file()


def test_preflight_waits_only_for_lagging_pull_ref(repo, monkeypatch):
    from lxreview import git as git_module

    repository, git = repo
    head = git("rev-parse", "HEAD")
    original = repository.call
    lagging = iter([True, True, False])

    def call(*args):
        if args[0] == "ls-remote":
            stale = "pull" in args[-1] and next(lagging)
            return ("b" * 40 if stale else head) + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    monkeypatch.setattr(git_module.time, "sleep", lambda _: None)
    with pytest.raises(git_module.PullRefPending):
        repository.preflight("https://github.com/org/repo/pull/12")
    assert repository.preflight("https://github.com/org/repo/pull/12", settle=60)["head"] == head


def test_sandbox_placeholders_stay_out_of_git_status_only_during_a_run(repo):
    repository, git = repo
    exclude = repository.path / ".git/info/exclude"
    exclude.write_text("# the user's own rule\n*.log\n")
    (repository.path / ".vscode").mkdir()
    (repository.path / ".vscode/settings.json").write_text("{}")
    leftover = repository.path / ".zshrc"
    leftover.touch()
    leftover.chmod(0o444)

    def untracked():
        return {line[3:] for line in git("status", "--porcelain").splitlines()}

    with repository.placeholders_hidden():
        # What the sandbox mounts while a command runs.
        (repository.path / ".bashrc").touch()
        (repository.path / ".claude").mkdir()
        (repository.path / ".claude/settings.json").touch()
        (repository.path / ".mcp.json").touch()
        # The user's own untracked .vscode stays visible; placeholders do not.
        assert untracked() == {".vscode/"}
        for name in (".bashrc", ".claude/settings.json", ".mcp.json"):
            (repository.path / name).unlink()
    assert exclude.read_text() == "# the user's own rule\n*.log\n"
    assert untracked() == {".vscode/", ".zshrc"}


def test_git_dash_c_is_accepted_only_for_the_repository(repo, tmp_path):
    repository, _ = repo

    def command(text, phase="edit"):
        return allowed(
            {"tool_name": "Bash", "tool_input": {"command": text}}, repository.path, phase
        )[0]

    assert command(f"git -C {repository.path} diff")
    assert command(f"git -C {repository.path} status --porcelain", "publish")
    assert push(f"git -C {repository.path} push origin HEAD:refs/heads/feature", repository)
    assert not command(f"git -C {tmp_path} diff")
    assert not command(f"git -C {repository.path} reset --hard")
