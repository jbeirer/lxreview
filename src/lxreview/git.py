import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import process
from .errors import Category, LXError
from .forge import parse, remote_project
from .paths import Paths, atomic_write
from .process import binary, run

# An empty helper resets every configured credential helper, URL-scoped ones included.
ANONYMOUS = ("-c", "credential.helper=")


class PullRefPending(LXError):
    """Local HEAD is pushed, but the forge's PR/MR head ref still lags behind it."""


# Configuration that redirects a push, pushes extra refs, or runs code during Git commands.
FORBIDDEN = re.compile(
    r"^(remote\..*\.(pushurl|mirror)|branch\..*\.pushremote|"
    r"remote\.pushdefault|url\..*\.(pushinsteadof|insteadof)|"
    r"push\.(followtags|recursesubmodules)|core\.hookspath|diff\.external|diff\..*\.(command|textconv))$"
)


# Claude Code's Linux sandbox mounts /dev/null at each of these repository paths that does
# not exist yet, so a sandboxed command cannot create them; the empty placeholders appear on
# the host while the command runs (observed with Claude Code 2.1.283).
SANDBOX_PLACEHOLDERS = (
    ".bash_profile",
    ".bashrc",
    ".claude",
    ".claude/agents",
    ".claude/commands",
    ".claude/hooks",
    ".claude/launch.json",
    ".claude/loop.md",
    ".claude/output-styles",
    ".claude/routines",
    ".claude/scheduled_tasks.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/skills",
    ".claude/workflows",
    ".codex",
    ".gitconfig",
    ".gitmodules",
    ".idea",
    ".lfsconfig",
    ".mcp.json",
    ".profile",
    ".ripgreprc",
    ".vscode",
    ".zprofile",
    ".zshrc",
)
EXCLUDE_BEGIN = "# >>> lxreview: sandbox placeholders during a run (removed when it ends)"
EXCLUDE_END = "# <<< lxreview"


def without_exclude_block(text: str) -> str:
    lines, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == EXCLUDE_BEGIN:
            inside = True
        elif inside and line.rstrip("\n") == EXCLUDE_END:
            inside = False
        elif not inside:
            lines.append(line)
    return "".join(lines)


def placeholder_leftover(path: Path) -> bool:
    """An empty read-only file at a placeholder path, as a killed sandbox command leaves it."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        path.is_file() and not path.is_symlink() and info.st_size == 0 and not info.st_mode & 0o222
    )


def unsafe_push_keys(listing: str) -> list[str]:
    """Forbidden key names in `git config --null --list` output; values may hold credentials."""
    keys = {entry.partition("\n")[0].lower() for entry in listing.split("\0") if entry}
    return sorted(key for key in keys if FORBIDDEN.fullmatch(key))


class Repository:
    def __init__(self, path: Path, paths: Paths):
        self.path, self.paths = path.resolve(), paths
        self.git = binary("git")

    def call(self, *args: str, env: dict[str, str] | None = None) -> str:
        return run([self.git, *args], self.paths, cwd=self.path, timeout=60, env=env).stdout.strip()

    def head(self) -> str:
        return self.call("rev-parse", "HEAD")

    def worktree_tree(self) -> str:
        """The Git tree a commit of every uncommitted change would hold: tracked edits and
        untracked, non-ignored files with their modes and types. It is built in a temporary
        index, so the real index stays untouched."""
        with tempfile.TemporaryDirectory() as scratch:
            env = {**process.environment(self.paths), "GIT_INDEX_FILE": f"{scratch}/index"}
            self.call("read-tree", "HEAD", env=env)
            self.call("add", "--all", env=env)
            return self.call("write-tree", env=env)

    def tree(self, commit: str) -> str:
        return self.call("rev-parse", f"{commit}^{{tree}}")

    def audit_root(self) -> Path:
        return (
            Path(self.call("rev-parse", "--path-format=absolute", "--git-common-dir"))
            / "review-loop"
        )

    def _exclude_file(self) -> Path:
        return Path(self.call("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))

    def hide_sandbox_placeholders(self) -> None:
        """Keep the sandbox's temporary placeholders out of git status for this run.

        Only paths that do not exist yet (or hold a leftover placeholder) are listed, so an
        untracked file of the user's own stays visible.
        """
        names = [
            name
            for name in SANDBOX_PLACEHOLDERS
            if not (self.path / name).exists() or placeholder_leftover(self.path / name)
        ]
        exclude = self._exclude_file()
        text = without_exclude_block(exclude.read_text() if exclude.exists() else "")
        if text and not text.endswith("\n"):
            text += "\n"
        block = [EXCLUDE_BEGIN, *(f"/{name}" for name in names), EXCLUDE_END]
        mode = exclude.stat().st_mode & 0o777 if exclude.exists() else 0o644
        atomic_write(exclude, text + "\n".join(block) + "\n", mode)

    def show_sandbox_placeholders(self) -> None:
        exclude = self._exclude_file()
        if exclude.exists():
            text = exclude.read_text()
            if (kept := without_exclude_block(text)) != text:
                atomic_write(exclude, kept, exclude.stat().st_mode & 0o777)

    @contextmanager
    def placeholders_hidden(self) -> Iterator[None]:
        self.hide_sandbox_placeholders()
        try:
            yield
        finally:
            self.show_sandbox_placeholders()

    def check_push_policy(self) -> None:
        result = run([self.git, "config", "--null", "--list"], self.paths, cwd=self.path)
        problems = unsafe_push_keys(result.stdout)
        if problems:
            raise LXError(
                Category.UNSAFE,
                "Git configuration can redirect a push or run extra code: " + "; ".join(problems),
            )
        upstream = self.call("rev-parse", "--abbrev-ref", "@{upstream}")
        branch = self.call("symbolic-ref", "--short", "HEAD")
        if upstream.split("/", 1)[-1] != branch:
            raise LXError(
                Category.UNSAFE, "Local and upstream branch names must match for a normal push"
            )

    def configured(self, key: str) -> bool:
        result = run(
            [self.git, "config", "--type=bool", "--get", key],
            self.paths,
            cwd=self.path,
            timeout=60,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def publish(self, commit: str, base: str, env: dict[str, str]) -> str:
        """Push the worker's commit from outside the sandbox and return what was pushed.

        The worker commits inside the sandbox (running the repository's commit hooks
        there); LXReview then signs the commit if the user's Git config asks for signed
        commits, uploads Git LFS objects, and pushes the exact branch refspec with the
        user's normal credentials (SSH agent, keychain or credential helper). Pre-push
        hooks are not run: they could execute files the worker was able to change.
        """
        if self.head() != commit:
            raise LXError(Category.UNSAFE, "HEAD is not the commit the worker reported")
        if self.call("rev-list", "--count", f"{base}..{commit}") != "1" or (
            self.call("rev-parse", f"{commit}^") != base
        ):
            raise LXError(
                Category.UNSAFE, "Publication must add exactly one commit to the reviewed head"
            )
        if self.call("status", "--porcelain"):
            raise LXError(Category.UNSAFE, "Uncommitted changes remain after publication")
        self.check_push_policy()
        if self.configured("commit.gpgsign"):
            run(
                [self.git, "commit", "--amend", "--no-edit", "--no-verify", "--gpg-sign"],
                self.paths,
                cwd=self.path,
                timeout=120,
                env=env,
            )
            commit = self.head()
        _, remote, refspec = self.push_command()[1:]
        try:
            if self._lfs() and self.call("lfs", "ls-files", "--name-only"):
                run(
                    [self.git, "lfs", "push", remote, commit],
                    self.paths,
                    cwd=self.path,
                    timeout=1800,
                    env=env,
                )
            run(
                [self.git, "push", "--no-verify", remote, refspec],
                self.paths,
                cwd=self.path,
                timeout=600,
                env=env,
            )
        except LXError as exc:
            raise LXError(
                Category.UNAVAILABLE,
                f"Push failed; commit {commit[:10]} exists only locally. "
                "Run lxreview logs for details, push it yourself, then resume",
            ) from exc
        return commit

    def _lfs(self) -> bool:
        attributes = self.path / ".gitattributes"
        return attributes.is_file() and "filter=lfs" in attributes.read_text(errors="replace")

    def push_command(self) -> list[str]:
        """The only push the worker may run: the current branch to its upstream, by refspec.

        An explicit refspec pushes exactly one branch whatever push.default or
        remote.*.push say, so no user Git configuration has to change.
        """
        remote, ref = self.call("rev-parse", "--abbrev-ref", "@{upstream}").split("/", 1)
        return ["git", "push", remote, f"HEAD:refs/heads/{ref}"]

    def preflight(self, target: str, *, settle: float = 0) -> dict:
        """Verify the review checkpoint; wait up to `settle` s for the PR/MR ref to catch up."""
        deadline = time.monotonic() + settle
        while True:
            try:
                return self._preflight(target)
            except PullRefPending:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def _preflight(self, target: str) -> dict:
        if self.call("status", "--porcelain"):
            raise LXError(
                Category.UNSAFE,
                "Working tree is dirty; commit or stash intended changes before starting/resuming",
            )
        self.check_push_policy()
        branch = self.call("symbolic-ref", "--short", "HEAD")
        upstream = self.call("rev-parse", "--abbrev-ref", "@{upstream}")
        remote, ref = upstream.split("/", 1)
        remote_url = self.call("remote", "get-url", remote)
        forge = parse(target)
        pushed_project = remote_project(remote_url, forge.host)
        if pushed_project is None:
            raise LXError(
                Category.UNSAFE,
                f"Use a {forge.forge} remote on {forge.host} without embedded credentials"
                " or custom remote helpers",
            )
        # A fork's main branch may carry a PR; the base repository's main never may.
        if ref in ("main", "master") and pushed_project.lower() == forge.project.lower():
            raise LXError(
                Category.UNSAFE,
                f"Use a {forge.noun} feature branch, not the base repository's main",
            )
        # Query the target project's PR/MR ref, not a potentially unrelated local upstream.
        head = self.head()
        try:
            advertised = self.call(
                # No stored credentials either: ChatGPT reads the project signed out.
                *ANONYMOUS,
                "ls-remote",
                forge.clone_url,
                forge.ref,
                # A project that needs a login fails here instead of prompting on the terminal;
                # an empty GIT_ASKPASS also skips core.askPass and SSH_ASKPASS.
                env={
                    **process.environment(self.paths),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ASKPASS": "",
                },
            )
        except LXError:
            raise LXError(
                Category.ACCESS,
                f"{forge.project} on {forge.host} cannot be read without signing in. The"
                f" repository must be public, because ChatGPT reviews the {forge.noun} signed"
                " out; if it is public, check the network",
            ) from None
        pushed = self.call("ls-remote", remote, f"refs/heads/{ref}")
        if pushed.startswith(head + "\t") and not advertised.startswith(head + "\t"):
            # GitHub and GitLab update the PR/MR head ref asynchronously after a push.
            raise PullRefPending(
                Category.UNSAFE,
                f"Pushed branch matches HEAD but {forge.forge} has not updated the"
                f" {forge.noun} head yet; retry shortly",
            )
        if not advertised.startswith(head + "\t") or not pushed.startswith(head + "\t"):
            raise LXError(
                Category.UNSAFE,
                f"Local HEAD, pushed feature branch, and {forge.noun} head must match before review",
            )
        return {"head": head, "branch": branch, "upstream": upstream, "remote_url": remote_url}

    def verify_identity(self, identity: dict) -> None:
        remote = identity["upstream"].split("/", 1)[0]
        if (
            self.call("symbolic-ref", "--short", "HEAD") != identity["branch"]
            or self.call("remote", "get-url", remote) != identity["remote_url"]
            or self.call("rev-parse", "--abbrev-ref", "@{upstream}") != identity["upstream"]
        ):
            raise LXError(Category.UNSAFE, "Branch, upstream, or remote changed during the run")
