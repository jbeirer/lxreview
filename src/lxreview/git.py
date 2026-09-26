import re
import time
from pathlib import Path

from .errors import Category, LXError
from .paths import Paths
from .process import binary, run


class PullRefPending(LXError):
    """Local HEAD is pushed, but GitHub's refs/pull/N/head still lags behind it."""


# Configuration that redirects a push, pushes extra refs, or runs code during Git commands.
FORBIDDEN = re.compile(
    r"^(remote\..*\.(pushurl|mirror)|branch\..*\.pushremote|"
    r"remote\.pushdefault|url\..*\.(pushinsteadof|insteadof)|"
    r"push\.(followtags|recursesubmodules)|core\.hookspath|diff\.external|diff\..*\.(command|textconv))$"
)


def unsafe_push_keys(listing: str) -> list[str]:
    """Forbidden key names in `git config --null --list` output; values may hold credentials."""
    keys = {entry.partition("\n")[0].lower() for entry in listing.split("\0") if entry}
    return sorted(key for key in keys if FORBIDDEN.fullmatch(key))


class Repository:
    def __init__(self, path: Path, paths: Paths):
        self.path, self.paths = path.resolve(), paths
        self.git = binary("git")

    def call(self, *args: str) -> str:
        return run([self.git, *args], self.paths, cwd=self.path, timeout=60).stdout.strip()

    def head(self) -> str:
        return self.call("rev-parse", "HEAD")

    def audit_root(self) -> Path:
        return (
            Path(self.call("rev-parse", "--path-format=absolute", "--git-common-dir"))
            / "review-loop"
        )

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

    def push_command(self) -> list[str]:
        """The only push the worker may run: the current branch to its upstream, by refspec.

        An explicit refspec pushes exactly one branch whatever push.default or
        remote.*.push say, so no user Git configuration has to change.
        """
        remote, ref = self.call("rev-parse", "--abbrev-ref", "@{upstream}").split("/", 1)
        return ["git", "push", remote, f"HEAD:refs/heads/{ref}"]

    def preflight(self, target: str, *, settle: float = 0) -> dict:
        """Verify the review checkpoint; wait up to `settle` s for GitHub's PR ref to catch up."""
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
        if branch in ("main", "master"):
            raise LXError(Category.UNSAFE, "Use a PR feature branch")
        upstream = self.call("rev-parse", "--abbrev-ref", "@{upstream}")
        remote, ref = upstream.split("/", 1)
        remote_url = self.call("remote", "get-url", remote)
        if not re.fullmatch(
            r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            remote_url,
        ):
            raise LXError(
                Category.UNSAFE,
                "Use a GitHub remote without embedded credentials or custom remote helpers",
            )
        repo_slug = "/".join(target.split("/")[3:5])
        # Query the target repo's PR ref, not a potentially unrelated local upstream.
        pr = target.rsplit("/", 1)[1]
        head = self.head()
        advertised = self.call(
            "ls-remote", f"https://github.com/{repo_slug}.git", f"refs/pull/{pr}/head"
        )
        pushed = self.call("ls-remote", remote, f"refs/heads/{ref}")
        if pushed.startswith(head + "\t") and not advertised.startswith(head + "\t"):
            # GitHub updates refs/pull/N/head asynchronously after a push.
            raise PullRefPending(
                Category.UNSAFE,
                "Pushed branch matches HEAD but GitHub has not updated the PR head yet; retry shortly",
            )
        if not advertised.startswith(head + "\t") or not pushed.startswith(head + "\t"):
            raise LXError(
                Category.UNSAFE,
                "Local HEAD, pushed feature branch, and PR head must match before review",
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
