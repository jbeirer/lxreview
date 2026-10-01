"""Accepted findings as a pending GitHub review, posted outside the sandbox with the user's gh.

A review created without an event stays a draft that only its author sees until they submit
it on GitHub, so nothing reaches the PR's participants without the user's own decision.
"""

import json
import re
import subprocess
from urllib.parse import quote

from . import forge
from .config import Config
from .discussion import github_cli
from .errors import Category, LXError
from .paths import Paths
from .process import run
from .security import redact

# Characters per comment text, and for the whole review body (GitHub accepts 65536).
LIMIT = 10_000
BODY_LIMIT = 60_000
# The new-side start and length of a diff hunk; a missing length means one line.
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
SUGGESTION = re.compile(r"(?m)^([ \t]*)(`{3,}|~{3,})[ \t]*suggestion\b[^\n]*$")
FENCE = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,})")
# GitHub's 422 for a comment it cannot place on the diff; the review is then not created.
ANCHOR = re.compile(
    r"(?i)review.?thread|could not be resolved|part of the diff|diff.?hunk|same hunk"
)


def gh(paths: Paths, config: Config) -> tuple[str, dict[str, str]]:
    command, env = github_cli(paths, config)
    if not command:
        raise LXError(
            Category.UNAVAILABLE,
            "The GitHub CLI (gh) is required to create the review; install it and run gh auth login",
        )
    return command, env


def pages(text: str) -> list:
    """The items of `gh api --paginate` output: one JSON array per page, concatenated."""
    decoder, items, index = json.JSONDecoder(), [], 0
    text = text.strip()
    while index < len(text):
        page, index = decoder.raw_decode(text, index)
        if not isinstance(page, list):
            raise ValueError("Expected a JSON array")
        items += page
        while index < len(text) and text[index].isspace():
            index += 1
    return items


def listing(target: forge.Target, paths: Paths, config: Config, resource: str) -> list:
    command, env = gh(paths, config)
    endpoint = f"repos/{target.project}/pulls/{target.number}/{resource}?per_page=100"
    try:
        result = run(
            [command, "api", "--paginate", endpoint], paths, env=env, check=False, timeout=120
        )
        if result.returncode:
            raise ValueError
        return pages(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
        raise LXError(
            Category.UNAVAILABLE,
            f"Could not read the PR's {resource} with gh; check gh auth status and access to"
            " the repository",
        ) from exc


def pending_review(target: forge.Target, paths: Paths, config: Config) -> str | None:
    """The link to the user's own pending review of the PR, if any.

    GitHub shows a pending review only to its author and allows one per user and PR.
    """
    for review in listing(target, paths, config, "reviews"):
        if review.get("state") == "PENDING":
            return review.get("html_url") or target.url
    return None


def hunks(patch: str | None) -> list[range]:
    """The new-side line ranges of a file's diff hunks: the lines a RIGHT comment can use."""
    found = []
    for match in HUNK.finditer(patch or ""):
        start, count = int(match[1]), int(match[2] or 1)
        if count:
            found.append(range(start, start + count))
    return found


def diff_lines(target: forge.Target, paths: Paths, config: Config) -> dict[str, list[range]]:
    """Commentable lines per file of the PR's diff. A file GitHub shows without a patch (too
    large, or binary) has none."""
    return {
        file["filename"]: hunks(file.get("patch"))
        for file in listing(target, paths, config, "files")
    }


def cap(text: str, limit: int) -> str:
    """Text shortened to limit; a shortened suggestion could be applied, so it becomes code."""
    if len(text) <= limit:
        return text
    text = SUGGESTION.sub(r"\1\2", text[:limit]).rstrip()
    if len(fences := FENCE.findall(text)) % 2:
        text += "\n" + fences[-1].strip()
    return text + "\n\n(shortened)"


def entry(target: forge.Target, head: str, comment: dict, title: str) -> str:
    """A comment in the review body: GitHub applies suggestions only inline."""
    text = SUGGESTION.sub(r"\1\2", comment["body"])
    if not comment.get("path"):
        return f"**{title}**\n\n{text}" if title else text
    start, end = comment.get("start_line") or comment["line"], comment["line"]
    lines = f"{start}-{end}" if start != end else f"{end}"
    anchor = f"L{start}-L{end}" if start != end else f"L{end}"
    link = f"https://github.com/{target.project}/blob/{head}/{quote(comment['path'])}#{anchor}"
    return f"**{comment['path']}:{lines}**\n{link}\n\n{text}"


def assemble(head: str, inline: list[dict], entries: list[str]) -> dict:
    """The review payload. It never has an event, so GitHub keeps the review pending."""
    payload: dict = {"commit_id": head, "comments": inline}
    if entries:
        payload["body"] = cap("\n\n---\n\n".join(entries), BODY_LIMIT)
    return payload


def build(
    comments: list[dict],
    titles: dict[str, str],
    head: str,
    target: forge.Target,
    ranges: dict[str, list[range]],
) -> dict:
    """Inline comments where GitHub can place them, everything else in the review body.

    Each comment has finding, path, line, start_line and body; a None path marks an anchor
    that is not a file and lines at head, which goes to the body without a link.
    """
    inline, entries = [], []
    for comment in comments:
        text = redact(comment["body"])
        if text != comment["body"]:
            # Applying a suggestion with a redacted value would put the placeholder in the code.
            text = SUGGESTION.sub(r"\1\2", text)
        comment = {**comment, "body": cap(text, LIMIT)}
        start, end = comment.get("start_line") or comment["line"], comment["line"]
        hunk = next(
            (r for r in ranges.get(comment.get("path") or "", []) if start in r and end in r),
            None,
        )
        if hunk is None:
            entries.append(entry(target, head, comment, redact(titles.get(comment["finding"], ""))))
            continue
        placed = {"path": comment["path"], "line": end, "side": "RIGHT", "body": comment["body"]}
        if start != end:
            placed.update(start_line=start, start_side="RIGHT")
        inline.append(placed)
    return assemble(head, inline, entries)


def in_body(payload: dict, target: forge.Target) -> dict:
    """The same review with every inline comment moved into its body."""
    head = payload["commit_id"]
    entries = [payload["body"]] if payload.get("body") else []
    entries += [entry(target, head, comment, "") for comment in payload["comments"]]
    return assemble(head, [], entries)


def problem(stdout: str) -> str:
    """GitHub's own error message from gh's output; stderr may hold credentials."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    errors = [
        error if isinstance(error, str) else str(error.get("message") or error.get("code") or "")
        for error in data.get("errors") or []
        if isinstance(error, str | dict)
    ]
    text = "; ".join(part for part in [str(data.get("message") or ""), *errors] if part)
    return redact(text)[:500]


def create(target: forge.Target, payload: dict, paths: Paths, config: Config) -> dict | str:
    """The created review, or GitHub's message when it was definitely refused."""
    command, env = gh(paths, config)
    endpoint = f"repos/{target.project}/pulls/{target.number}/reviews"
    unknown = (
        "; it was not retried. Check the PR on GitHub for a pending review of yours before resuming"
    )
    try:
        result = run(
            [command, "api", "-X", "POST", endpoint, "--input", "-"],
            paths,
            env=env,
            input=json.dumps(payload),
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise LXError(Category.TIMEOUT, "Creating the review timed out" + unknown) from None
    except OSError as exc:
        raise LXError(Category.UNAVAILABLE, "Could not run gh to create the review") from exc
    if result.returncode:
        message = problem(result.stdout)
        if message and ANCHOR.search(message):
            return message
        raise LXError(
            Category.UNAVAILABLE,
            "Creating the review failed" + (f" ({message})" if message else "") + unknown,
        )
    try:
        created = json.loads(result.stdout)
        return {"id": created["id"], "html_url": created["html_url"]}
    except (ValueError, KeyError, TypeError):
        raise LXError(
            Category.PROTOCOL, "GitHub's answer to the review was unreadable" + unknown
        ) from None


def post(target: forge.Target, payload: dict, paths: Paths, config: Config) -> dict:
    """Create the pending review; returns its id, link and how many comments are inline.

    An anchor GitHub cannot place is refused before anything is created, so that review is
    posted once more with every comment in its body. Anything ambiguous is never retried.
    """
    created, moved = create(target, payload, paths, config), False
    if isinstance(created, str):
        if not payload["comments"]:
            raise LXError(Category.PROTOCOL, f"GitHub refused the review: {created}")
        payload, moved = in_body(payload, target), True
        created = create(target, payload, paths, config)
        if isinstance(created, str):
            raise LXError(Category.PROTOCOL, f"GitHub refused the review: {created}")
    return {**created, "inline": len(payload["comments"]), "moved_to_body": moved}
