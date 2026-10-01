import subprocess

import pytest

from lxreview.errors import Category, LXError
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
    assert repository.push_command("c" * 40, "b" * 40) == [
        "git",
        "push",
        f"--force-with-lease=refs/heads/feature:{'b' * 40}",
        "origin",
        f"{'c' * 40}:refs/heads/feature",
    ]
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


@pytest.fixture
def signing(repo, tmp_path):
    """commit.gpgsign with a stand-in for gpg that signs anything."""
    _, git = repo
    gpg = tmp_path / "fake-gpg"
    gpg.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf '%s\\n' '-----BEGIN PGP SIGNATURE-----' 'fake' '-----END PGP SIGNATURE-----'\n"
        "printf '\\n[GNUPG:] SIG_CREATED D 1 8 00 0 FAKE\\n' >&2\n"
    )
    gpg.chmod(0o755)
    git("config", "gpg.program", str(gpg))
    git("config", "commit.gpgsign", "true")


@pytest.mark.parametrize("moved", [False, True])
def test_publish_pushes_only_a_signed_commit_of_the_validated_change(
    repo, remote, paths, monkeypatch, signing, moved
):
    from lxreview import process

    repository, git = repo
    base = repository.head()
    (repository.path / "fix.py").write_text("fixed = True\n")
    git("add", "fix.py")
    git("commit", "-q", "--no-gpg-sign", "-m", "Fix it")
    commit = repository.head()
    if moved:
        check = repository.check_push_policy

        def commit_meanwhile():
            # Another process commits after publish() validated HEAD, before signing.
            check()
            (repository.path / "unchecked.py").write_text("unchecked = True\n")
            git("add", "unchecked.py")
            git("commit", "-q", "--no-gpg-sign", "-m", "Unchecked")

        monkeypatch.setattr(repository, "check_push_policy", commit_meanwhile)
        with pytest.raises(LXError, match="HEAD moved while the commit was signed"):
            repository.publish(commit, base, process.environment(paths))
        assert git("ls-remote", "origin", "refs/heads/feature").split()[0] == base
        return
    signed = repository.publish(commit, base, process.environment(paths))
    assert signed != commit and repository.tree(signed) == repository.tree(commit)
    assert "PGP SIGNATURE" in git("cat-file", "commit", signed)
    assert git("ls-remote", "origin", "refs/heads/feature").split()[0] == signed


def test_publish_pushes_the_commit_even_when_head_moves(repo, remote, paths, monkeypatch):
    from lxreview import process

    repository, git = repo
    base = repository.head()
    (repository.path / "fix.py").write_text("fixed = True\n")
    git("add", "fix.py")
    git("commit", "-q", "-m", "Fix it")
    commit = repository.head()
    check = repository.check_push_policy

    def moved():
        # Another process commits after publish() validated HEAD.
        check()
        git("commit", "-q", "--allow-empty", "-m", "Unchecked")

    monkeypatch.setattr(repository, "check_push_policy", moved)
    assert repository.publish(commit, base, process.environment(paths)) == commit
    assert git("ls-remote", "origin", "refs/heads/feature").split()[0] == commit


@pytest.mark.parametrize("change", ["deleted", "reset"])
def test_publish_fails_when_the_upstream_changed_while_waiting(repo, remote, paths, change):
    from lxreview import process

    repository, git = repo
    older = repository.head()
    git("commit", "-q", "--allow-empty", "-m", "Reviewed")
    git("push", "-q", "origin", "HEAD:refs/heads/feature")
    base = repository.head()
    # While the run waits, someone deletes the branch or resets it to an older commit;
    # a plain push would recreate it or fast-forward over the reset.
    reference = ["update-ref", "-d", "refs/heads/feature"]
    if change == "reset":
        reference = ["update-ref", "refs/heads/feature", older]
    subprocess.run(["git", "--git-dir", str(remote), *reference], check=True)
    (repository.path / "fix.py").write_text("fixed = True\n")
    git("add", "fix.py")
    git("commit", "-q", "-m", "Fix it")
    with pytest.raises(LXError, match="Push failed"):
        repository.publish(repository.head(), base, process.environment(paths))


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
        if "ls-remote" in args:
            probes.append(args)
            return head + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    assert repository.preflight("https://github.com/org/repo/pull/12")["head"] == head
    assert probes == [
        (
            "-c",
            "credential.helper=",
            "ls-remote",
            "https://github.com/org/repo.git",
            "refs/pull/12/head",
        ),
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
        if "ls-remote" in args:
            env = kwargs.get("env") or {}
            probes.append((args, (env.get("GIT_TERMINAL_PROMPT"), env.get("GIT_ASKPASS"))))
            return head + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    target = "https://gitlab.cern.ch/g/sub/p/-/merge_requests/12"
    assert repository.preflight(target)["head"] == head
    assert probes == [
        (
            (
                "-c",
                "credential.helper=",
                "ls-remote",
                "https://gitlab.cern.ch/g/sub/p.git",
                "refs/merge-requests/12/head",
            ),
            ("0", ""),
        ),
        (("ls-remote", "origin", "refs/heads/feature"), (None, None)),
    ]


@pytest.mark.parametrize(
    ("target", "noun"),
    [
        ("https://github.com/org/repo/pull/12", "PR"),
        ("https://gitlab.com/org/repo/-/merge_requests/12", "MR"),
    ],
)
def test_preflight_explains_a_repository_that_needs_a_login(repo, monkeypatch, target, noun):
    repository, git = repo
    if "gitlab" in target:
        git("remote", "set-url", "origin", "https://gitlab.com/org/repo.git")
    original = repository.call

    def call(*args, **kwargs):
        if "ls-remote" in args:
            raise LXError(Category.UNAVAILABLE, "git failed (exit 128); run lxreview doctor")
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    with pytest.raises(LXError, match=f"must be public, because ChatGPT reviews the {noun}") as err:
        repository.preflight(target)
    assert err.value.category == Category.ACCESS
    assert "org/repo on " in str(err.value)


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
        if "ls-remote" in args:
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


def test_worktree_tree_sees_every_uncommitted_change(repo):
    from lxreview import process

    repository, git = repo
    env = process.environment(repository.paths)
    (repository.path / "tracked.txt").write_text("one\n")
    (repository.path / ".gitignore").write_text("*.log\n")
    git("add", "tracked.txt", ".gitignore")
    git("commit", "-m", "tracked")
    clean = repository.worktree_tree(env)
    assert clean == repository.tree("HEAD")
    (repository.path / "build.log").write_text("ignored output")
    assert repository.worktree_tree(env) == clean
    (repository.path / "tracked.txt").write_text("two\n")
    edited = repository.worktree_tree(env)
    assert edited != clean
    git("add", "tracked.txt")
    assert repository.worktree_tree(env) == edited
    new = repository.path / "new.py"
    new.write_text("a = 1\n")
    added = repository.worktree_tree(env)
    assert added != edited
    # Building the tree leaves the real index alone.
    assert "?? new.py" in git("status", "--porcelain").splitlines()
    new.write_text("a = 2\n")
    assert repository.worktree_tree(env) != added
    new.write_text("a = 1\n")
    new.chmod(0o755)
    assert repository.worktree_tree(env) != added
    new.unlink()
    new.symlink_to("a = 1\n")
    assert repository.worktree_tree(env) != added
    new.unlink()
    git("checkout", "HEAD", "--", "tracked.txt")
    assert repository.worktree_tree(env) == clean
    new.write_text("a = 1\n")
    git("add", "--all")
    git("commit", "-m", "everything")
    assert repository.tree("HEAD") == repository.worktree_tree(env)


def test_worktree_tree_runs_clean_filters_from_the_given_environment(repo, tmp_path):
    from lxreview import process

    repository, git = repo
    # A required clean filter installed outside the system PATH, as git-lfs often is.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "upper-filter").write_text("#!/bin/sh\nexec tr a-z A-Z\n")
    (tools / "upper-filter").chmod(0o755)
    git("config", "filter.upper.clean", "upper-filter")
    git("config", "filter.upper.required", "true")
    (repository.path / ".gitattributes").write_text("*.txt filter=upper\n")
    (repository.path / "notes.txt").write_text("checked\n")
    env = process.environment(repository.paths)
    with pytest.raises(LXError):
        repository.worktree_tree(env)
    tree = repository.worktree_tree({**env, "PATH": f"{tools}:{env['PATH']}"})
    assert git("cat-file", "-p", f"{tree}:notes.txt") == "CHECKED"


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
        lambda *args, **kwargs: head + "\t" + args[-1] if "ls-remote" in args else original(*args),
    )
    if allowed_main:
        assert repository.preflight(target)["branch"] == "main"
    else:
        with pytest.raises(LXError, match="feature branch"):
            repository.preflight(target)


def test_review_only_checkpoint_accepts_any_checkout_at_the_pr_head(repo, monkeypatch):
    repository, git = repo
    head = git("rev-parse", "HEAD")
    # Another person's PR, fetched without a branch: detached, no upstream, no push policy.
    git("checkout", "-q", "--detach")
    git("config", "remote.origin.pushurl", "https://github.com/other/repo")
    monkeypatch.setattr(
        repository, "check_push_policy", lambda: pytest.fail("Nothing is pushed in this mode")
    )
    advertised = {"value": head}
    original = repository.call
    probes = []

    def call(*args, **kwargs):
        if "ls-remote" in args:
            env = kwargs.get("env") or {}
            probes.append((args, env.get("GIT_TERMINAL_PROMPT"), env.get("GIT_ASKPASS")))
            return advertised["value"] + "\t" + args[-1]
        return original(*args)

    monkeypatch.setattr(repository, "call", call)
    target = "https://github.com/org/repo/pull/12"
    assert repository.review_checkpoint(target) == {"head": head}
    assert probes == [
        (
            (
                "-c",
                "credential.helper=",
                "ls-remote",
                "https://github.com/org/repo.git",
                "refs/pull/12/head",
            ),
            "0",
            "",
        )
    ]
    advertised["value"] = "b" * 40
    with pytest.raises(LXError, match="gh pr checkout 12") as err:
        repository.review_checkpoint(target)
    assert err.value.category == Category.UNSAFE
    advertised["value"] = head
    (repository.path / "notes.txt").write_text("uncommitted")
    with pytest.raises(LXError, match="dirty"):
        repository.review_checkpoint(target)


def test_line_count_reads_the_file_at_the_commit(repo):
    repository, git = repo
    (repository.path / "src").mkdir()
    (repository.path / "src/a.py").write_text("one\ntwo\nthree\n")
    (repository.path / "data.bin").write_bytes(b"\x00\xff\n\x80")
    git("add", "src/a.py", "data.bin")
    git("commit", "-q", "-m", "files")
    head = repository.head()
    (repository.path / "src/a.py").write_text("one\n")
    assert repository.line_count(head, "src/a.py") == 3
    assert repository.line_count(head, "data.bin") == 2
    assert repository.line_count(head, "src") is None
    assert repository.line_count(head, "missing.py") is None
