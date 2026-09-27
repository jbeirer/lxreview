import json
import subprocess

import pytest

from lxreview import discussion
from lxreview.config import Config
from lxreview.errors import LXError


def page(nodes):
    return {"pageInfo": {"hasPreviousPage": False}, "nodes": nodes}


def author(login, when, body):
    return {"author": {"login": login}, "createdAt": when, "body": body}


PR = {
    "author": {"login": "owner"},
    "body": "Adds retries.",
    "comments": page([author("alice", "2026-09-02T10:00:00Z", "Why not exponential backoff?")]),
    "reviews": page(
        [
            {
                "author": {"login": "bob"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-09-01T09:00:00Z",
                "body": "",
            },
            # Review bodies left empty by inline comments add nothing.
            {
                "author": None,
                "state": "COMMENTED",
                "submittedAt": "2026-09-01T09:30:00Z",
                "body": "",
            },
        ]
    ),
    "reviewThreads": page(
        [
            {
                "isResolved": True,
                "isOutdated": False,
                "path": "src/retry.py",
                "line": 12,
                "comments": {
                    "nodes": [
                        author("bob", "2026-09-01T08:00:00Z", "Unbounded loop?"),
                        author(
                            "owner",
                            "2026-09-01T08:30:00Z",
                            "Bounded by MAX_TRIES; kept on purpose.",
                        ),
                    ]
                },
            }
        ]
    ),
}


def test_discussion_is_chronological_with_thread_resolution():
    text, counts = discussion.render(PR)
    assert text.startswith("### Description by @owner\nAdds retries.")
    thread = text.index("### Review thread on src/retry.py:12 (resolved)")
    assert thread < text.index("### Review by @bob (CHANGES_REQUESTED)") < text.index("@alice")
    assert "@owner at 2026-09-01T08:30:00Z: Bounded by MAX_TRIES; kept on purpose." in text
    assert "@ghost" not in text
    assert counts == {"comments": 1, "reviews": 1, "threads": 1, "unresolved": 0}


def test_long_discussion_keeps_the_description_and_the_newest_entries(monkeypatch):
    monkeypatch.setattr(discussion, "LIMIT", 400)
    pr = dict(PR)
    pr["comments"] = page(
        [
            author("alice", f"2026-09-{day:02}T10:00:00Z", f"comment {day} " + "x" * 80)
            for day in range(1, 10)
        ]
    )
    text, _ = discussion.render(pr)
    assert text.startswith("### Description by @owner") and len(text) <= 500
    assert "Older discussion omitted" in text
    assert "comment 9 " in text and "comment 1 " not in text


@pytest.mark.github
def test_fetch_asks_gh_for_the_target_pr(paths, monkeypatch):
    calls = []

    def run(argv, *args, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"data": {"repository": {"pullRequest": PR}}}), ""
        )

    monkeypatch.setattr(discussion.shutil, "which", lambda name, path: "/home/u/bin/gh")
    monkeypatch.setattr(discussion, "run", run)
    text, counts = discussion.fetch("https://github.com/o/r/pull/7", paths, Config())
    assert "kept on purpose" in text and counts["threads"] == 1
    argv = calls[0]
    assert argv[:3] == ["/home/u/bin/gh", "api", "graphql"]
    assert "owner=o" in argv and "name=r" in argv and "number=7" in argv


@pytest.mark.github
@pytest.mark.parametrize("gh", [None, "/usr/bin/gh"])
def test_fetch_refuses_without_a_readable_discussion(paths, monkeypatch, gh):
    monkeypatch.setattr(discussion.shutil, "which", lambda name, path: gh)
    monkeypatch.setattr(
        discussion,
        "run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", "HTTP 401"),
    )
    with pytest.raises(LXError, match="gh auth login" if gh is None else "gh auth status"):
        discussion.fetch("https://github.com/o/r/pull/7", paths, Config())
