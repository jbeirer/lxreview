"""The PR's own conversation, read outside the sandbox so Claude's evaluation can weigh it.

Findings the author and reviewers have already settled must not be reopened silently, and
the sandboxed Claude turns cannot reach GitHub themselves.
"""

import json
import shutil
import subprocess

from . import toolchain
from .config import Config
from .errors import Category, LXError
from .paths import Paths
from .process import run

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      author { login }
      body
      comments(last: 100) {
        pageInfo { hasPreviousPage }
        nodes { author { login } createdAt body }
      }
      reviews(last: 100) {
        pageInfo { hasPreviousPage }
        nodes { author { login } state submittedAt body }
      }
      reviewThreads(last: 100) {
        pageInfo { hasPreviousPage }
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 50) { nodes { author { login } createdAt body } }
        }
      }
    }
  }
}
"""
# Characters of discussion passed on; older entries are dropped first.
LIMIT = 60_000


def github_cli(paths: Paths, config: Config) -> tuple[str | None, dict[str, str]]:
    """The user's gh, from the usual toolchain directories, and the environment to run it."""
    env = toolchain.push_environment(paths, config)
    env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    return shutil.which("gh", path=env["PATH"]), env


def fetch(target: str, paths: Paths, config: Config) -> tuple[str, dict[str, int]]:
    """The PR description, comments, reviews and review threads, and their counts."""
    owner, name, _, number = target.split("/")[3:7]
    gh, env = github_cli(paths, config)
    if not gh:
        raise LXError(
            Category.UNAVAILABLE,
            "The GitHub CLI (gh) is required to read the PR discussion; install it and run gh auth login",
        )
    try:
        result = run(
            [
                gh,
                "api",
                "graphql",
                "-f",
                f"query={QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
            ],
            paths,
            env=env,
            check=False,
            timeout=60,
        )
        pr = json.loads(result.stdout)["data"]["repository"]["pullRequest"]
        if result.returncode or not pr:
            raise ValueError
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        raise LXError(
            Category.UNAVAILABLE,
            "Could not read the PR discussion with gh; check gh auth status and access to the repository",
        ) from exc
    return render(pr)


def render(pr: dict) -> tuple[str, dict[str, int]]:
    def who(node: dict) -> str:
        return "@" + ((node.get("author") or {}).get("login") or "ghost")

    entries = [
        (
            comment["createdAt"],
            f"### Comment by {who(comment)} at {comment['createdAt']}\n{comment['body']}",
        )
        for comment in pr["comments"]["nodes"]
    ]
    reviews = [r for r in pr["reviews"]["nodes"] if r["body"] or r["state"] != "COMMENTED"]
    entries += [
        (
            review["submittedAt"] or "",
            f"### Review by {who(review)} ({review['state']}) at {review['submittedAt']}\n"
            + (review["body"] or "(no summary)"),
        )
        for review in reviews
    ]
    threads = [t for t in pr["reviewThreads"]["nodes"] if t["comments"]["nodes"]]
    for thread in threads:
        status = "resolved" if thread["isResolved"] else "unresolved"
        status += ", outdated" if thread["isOutdated"] else ""
        lines = [f"### Review thread on {thread['path']}:{thread['line'] or '?'} ({status})"]
        lines += [f"{who(c)} at {c['createdAt']}: {c['body']}" for c in thread["comments"]["nodes"]]
        entries.append((thread["comments"]["nodes"][0]["createdAt"], "\n".join(lines)))
    entries.sort()
    truncated = any(
        pr[k]["pageInfo"]["hasPreviousPage"] for k in ("comments", "reviews", "reviewThreads")
    )
    description = f"### Description by {who(pr)}\n{pr['body'] or '(empty)'}"
    while entries and len(description) + sum(len(text) + 2 for _, text in entries) > LIMIT:
        entries.pop(0)
        truncated = True
    parts = [description[:LIMIT]]
    if truncated:
        parts.append("(Older discussion omitted; the most recent entries follow.)")
    parts += [text for _, text in entries]
    counts = {
        "comments": len(pr["comments"]["nodes"]),
        "reviews": len(reviews),
        "threads": len(threads),
        "unresolved": sum(not t["isResolved"] for t in threads),
    }
    return "\n\n".join(parts), counts
