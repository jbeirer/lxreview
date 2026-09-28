"""The PR's or MR's own conversation, read outside the sandbox so Claude's evaluation can weigh it.

Findings the author and reviewers have already settled must not be reopened silently, and
the sandboxed Claude turns cannot reach GitHub or GitLab themselves.
"""

import json
import shutil
import subprocess
import urllib.error
import urllib.request

from . import forge, toolchain
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
# GitLab answers this anonymously for public projects (its REST notes API needs a login)
# and returns a null project or merge request when the MR is not public.
GITLAB_QUERY = """
query($project: ID!, $iid: String!) {
  project(fullPath: $project) {
    mergeRequest(iid: $iid) {
      author { username }
      description
      approvedBy { nodes { username } }
      discussions(last: 100) {
        pageInfo { hasPreviousPage }
        nodes {
          resolvable
          resolved
          notes(first: 50) {
            nodes { system author { username } createdAt body position { newPath newLine } }
          }
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
    """The PR/MR description, comments, reviews and review threads, and their counts."""
    parsed = forge.parse(target)
    if parsed.kind == "gitlab":
        return render_gitlab(gitlab_merge_request(parsed))
    owner, name = parsed.project.split("/")
    number = parsed.number
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


def gitlab_merge_request(target: forge.Target) -> dict:
    """The MR from GitLab's GraphQL API, read anonymously like the reviewer reads it."""
    request = urllib.request.Request(
        f"https://{target.host}/api/graphql",
        data=json.dumps(
            {
                "query": GITLAB_QUERY,
                "variables": {"project": target.project, "iid": str(target.number)},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)["data"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LXError(
            Category.UNAVAILABLE, f"Could not read the MR discussion from {target.host}"
        ) from exc
    merge_request = ((data or {}).get("project") or {}).get("mergeRequest")
    if not merge_request:
        raise LXError(
            Category.UNAVAILABLE,
            f"Could not read {target.url} without signing in: the MR does not exist or its"
            " project is not public. LXReview supports GitLab merge requests only in public"
            " projects, because ChatGPT reads them anonymously",
        )
    return merge_request


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
    truncated = any(
        pr[k]["pageInfo"]["hasPreviousPage"] for k in ("comments", "reviews", "reviewThreads")
    )
    description = f"### Description by {who(pr)}\n{pr['body'] or '(empty)'}"
    counts = {
        "comments": len(pr["comments"]["nodes"]),
        "reviews": len(reviews),
        "threads": len(threads),
        "unresolved": sum(not t["isResolved"] for t in threads),
    }
    return assemble(description, entries, truncated), counts


def render_gitlab(mr: dict) -> tuple[str, dict[str, int]]:
    def who(node: dict) -> str:
        return "@" + ((node.get("author") or {}).get("username") or "ghost")

    entries, comments, threads, unresolved = [], 0, 0, 0
    for discussion in mr["discussions"]["nodes"]:
        # System notes ("added 1 commit", "changed the description") are not discussion.
        notes = [n for n in discussion["notes"]["nodes"] if not n["system"]]
        if not notes:
            continue
        position = notes[0].get("position")
        if position or discussion["resolvable"]:
            threads += 1
            open_thread = discussion["resolvable"] and not discussion["resolved"]
            unresolved += open_thread
            where = f" on {position['newPath']}:{position['newLine'] or '?'}" if position else ""
            status = (
                (" (unresolved)" if open_thread else " (resolved)")
                if discussion["resolvable"]
                else ""
            )
            lines = [f"### Review thread{where}{status}"]
            lines += [f"{who(n)} at {n['createdAt']}: {n['body']}" for n in notes]
            entries.append((notes[0]["createdAt"], "\n".join(lines)))
        else:
            comments += len(notes)
            entries += [
                (n["createdAt"], f"### Comment by {who(n)} at {n['createdAt']}\n{n['body']}")
                for n in notes
            ]
    approvers = ["@" + node["username"] for node in mr["approvedBy"]["nodes"]]
    description = f"### Description by {who(mr)}\n{mr['description'] or '(empty)'}"
    if approvers:
        description += "\n\nApproved by " + ", ".join(approvers)
    counts = {
        "comments": comments,
        "reviews": len(approvers),
        "threads": threads,
        "unresolved": unresolved,
    }
    truncated = mr["discussions"]["pageInfo"]["hasPreviousPage"]
    return assemble(description, entries, truncated), counts


def assemble(description: str, entries: list[tuple[str, str]], truncated: bool) -> str:
    """The description, then entries oldest first, dropping the oldest beyond LIMIT."""
    entries = sorted(entries)
    while entries and len(description) + sum(len(text) + 2 for _, text in entries) > LIMIT:
        entries.pop(0)
        truncated = True
    parts = [description[:LIMIT]]
    if truncated:
        parts.append("(Older discussion omitted; the most recent entries follow.)")
    parts += [text for _, text in entries]
    return "\n\n".join(parts)
