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


def test_the_worker_never_pushes_itself(repo):
    repository, _ = repo
    repository.check_push_policy()
    assert repository.push_command() == ["git", "push", "origin", "HEAD:refs/heads/feature"]
    for command in (
        "git push origin HEAD:refs/heads/feature",
        "git push",
        "git push --force origin HEAD:refs/heads/feature",
    ):
        assert not push(command, repository)


@pytest.fixture
def remote(repo, tmp_path):
    repository, git = repo
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    git("remote", "set-url", "origin", str(bare))
    git("push", "-q", "origin", "HEAD:refs/heads/feature")
    return bare


def test_publish_pushes_exactly_the_one_worker_commit(repo, remote, paths):
    from lxreview import process

    repository, git = repo
    base = repository.head()
    (repository.path / "fix.py").write_text("fixed = True\n")
    git("add", "fix.py")
    git("commit", "-q", "-m", "Fix it")
    commit = repository.head()
    assert repository.publish(commit, base, process.environment(paths)) == commit
    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/feature"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert pushed == commit


@pytest.mark.parametrize("problem", ["two commits", "dirty tree", "wrong commit"])
def test_publish_refuses_anything_but_one_clean_commit(repo, remote, paths, problem):
    from lxreview import process

    repository, git = repo
    base = repository.head()
    for name in ("a", "b") if problem == "two commits" else ("a",):
        (repository.path / name).write_text(name)
        git("add", name)
        git("commit", "-q", "-m", name)
    if problem == "dirty tree":
        (repository.path / "a").write_text("changed")
    reported = base if problem == "wrong commit" else repository.head()
    with pytest.raises(LXError):
        repository.publish(reported, base, process.environment(paths))


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
def test_push_configuration_cannot_redirect_the_push(repo, key, value):
    repository, git = repo
    git("config", key, value)
    with pytest.raises(LXError):
        repository.check_push_policy()


def test_preflight_compares_actual_head_with_both_remote_refs(repo, monkeypatch):
    repository, git = repo
    head = git("rev-parse", "HEAD")
    original = repository.call
    probes = []

    def call(*args, **kwargs):
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


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://:@gitlab.cern.ch:8443/g/sub/p.git",
        "https://gitlab.cern.ch/g/sub/p",
        "ssh://git@gitlab.cern.ch:7999/g/sub/p.git",
        "git@gitlab.cern.ch:g/sub/p.git",
    ],
)
def test_preflight_reads_the_merge_request_ref_of_the_target_project(repo, monkeypatch, remote_url):
    repository, git = repo
    git("remote", "set-url", "origin", remote_url)
    head = git("rev-parse", "HEAD")
    original = repository.call
    probes = []

    def call(*args, **kwargs):
        if args[0] == "ls-remote":
            env = kwargs.get("env") or {}
            probes.append((args, (env.get("GIT_TERMINAL_PROMPT"), env.get("GIT_ASKPASS"))))
            return head + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    target = "https://gitlab.cern.ch/g/sub/p/-/merge_requests/12"
    assert repository.preflight(target)["head"] == head
    assert probes == [
        (
            ("ls-remote", "https://gitlab.cern.ch/g/sub/p.git", "refs/merge-requests/12/head"),
            ("0", ""),
        ),
        (("ls-remote", "origin", "refs/heads/feature"), (None, None)),
    ]


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/g/p.git",
        "https://user:token@gitlab.cern.ch/g/sub/p.git",
        "https://gitlab.cern.ch.evil.com/g/sub/p.git",
        "https://gitlab.com/g/sub/p.git",
        "ext::ssh git@gitlab.cern.ch g/sub/p",
    ],
)
def test_preflight_refuses_remotes_off_the_target_gitlab(repo, remote_url):
    repository, git = repo
    git("remote", "set-url", "origin", remote_url)
    with pytest.raises(LXError, match="GitLab remote on gitlab.cern.ch"):
        repository.preflight("https://gitlab.cern.ch/g/sub/p/-/merge_requests/12")


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

    def call(*args, **kwargs):
        if args[0] == "ls-remote":
            stale = ("pull" in args[-1] or "merge-requests" in args[-1]) and next(lagging)
            return ("b" * 40 if stale else head) + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    monkeypatch.setattr(git_module.time, "sleep", lambda _: None)
    with pytest.raises(git_module.PullRefPending, match="GitHub has not updated the PR head"):
        repository.preflight("https://github.com/org/repo/pull/12")
    assert repository.preflight("https://github.com/org/repo/pull/12", settle=60)["head"] == head
    git("remote", "set-url", "origin", "https://gitlab.com/org/repo.git")
    lagging = iter([True])
    with pytest.raises(git_module.PullRefPending, match="GitLab has not updated the MR head"):
        repository.preflight("https://gitlab.com/org/repo/-/merge_requests/12")


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
    assert command(f"git -C {repository.path} log --oneline -3")
    assert not command(f"git -C {tmp_path} diff")
    assert not command(f"git -C {repository.path} reset --hard")


@pytest.mark.parametrize(
    ("target", "remote_url", "allowed_main"),
    [
        ("https://github.com/org/repo/pull/12", "https://github.com/org/repo.git", False),
        ("https://github.com/org/repo/pull/12", "git@github.com:contributor/repo.git", True),
        (
            "https://gitlab.cern.ch/atlas/athena/-/merge_requests/12",
            "https://:@gitlab.cern.ch:8443/atlas/athena.git",
            False,
        ),
        (
            "https://gitlab.cern.ch/atlas/athena/-/merge_requests/12",
            "ssh://git@gitlab.cern.ch:7999/someone/athena.git",
            True,
        ),
    ],
)
def test_main_branch_is_refused_only_on_the_base_repository(
    repo, monkeypatch, target, remote_url, allowed_main
):
    repository, git = repo
    git("branch", "-m", "main")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
    git("branch", "--set-upstream-to=origin/main")
    git("remote", "set-url", "origin", remote_url)
    head = git("rev-parse", "HEAD")
    original = repository.call
    monkeypatch.setattr(
        repository,
        "call",
        lambda *args, **kwargs: (
            head + "\t" + args[-1] if args[0] == "ls-remote" else original(*args)
        ),
    )
    if allowed_main:
        assert repository.preflight(target)["branch"] == "main"
    else:
        with pytest.raises(LXError, match="feature branch"):
            repository.preflight(target)
