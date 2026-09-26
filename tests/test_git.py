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


def test_normal_push_policy(repo):
    repository, _ = repo
    repository.check_push_policy()
    assert allowed(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}}, repository.path, "publish"
    )[0]


@pytest.mark.parametrize(
    "key,value",
    [
        ("remote.origin.push", "+HEAD:main"),
        ("remote.origin.mirror", "true"),
        ("remote.origin.pushurl", "https://github.com/other/repo"),
        ("push.default", "matching"),
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
    assert not allowed(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}}, repository.path, "publish"
    )[0]


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


def test_effective_push_default_is_checked_and_named(repo, monkeypatch):
    repository, git = repo
    git("config", "--add", "push.default", "matching")
    with pytest.raises(LXError, match="push.default=matching"):
        repository.check_push_policy()
    # A later (higher-precedence) value is what git uses; earlier ones must not block.
    git("config", "--add", "push.default", "simple")
    repository.check_push_policy()


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
