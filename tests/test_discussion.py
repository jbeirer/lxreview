import io
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


@pytest.mark.forge
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


@pytest.mark.forge
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


def note(user, when, body, system=False, position=None):
    return {
        "system": system,
        "author": {"username": user},
        "createdAt": when,
        "body": body,
        "position": position,
    }


def thread(*notes, resolvable=False, resolved=False):
    return {"resolvable": resolvable, "resolved": resolved, "notes": {"nodes": list(notes)}}


MR = {
    "author": {"username": "owner"},
    "description": "Adds retries.",
    "approvedBy": {"nodes": [{"username": "carol"}]},
    "discussions": page(
        [
            thread(note("atlasbot", "2026-09-01T07:00:00Z", "This MR affects 1 package")),
            thread(note("owner", "2026-09-01T07:30:00Z", "added 1 commit", system=True)),
            thread(
                note(
                    "bob",
                    "2026-09-01T08:00:00Z",
                    "Unbounded loop?",
                    position={"newPath": "src/retry.py", "newLine": 12},
                ),
                note("owner", "2026-09-01T08:30:00Z", "Bounded by MAX_TRIES; kept on purpose."),
                resolvable=True,
                resolved=True,
            ),
            thread(
                note("alice", "2026-09-02T10:00:00Z", "Why not exponential backoff?"),
                resolvable=True,
            ),
        ]
    ),
}


def test_gitlab_discussion_matches_the_github_shape():
    text, counts = discussion.render_gitlab(MR)
    assert text.startswith("### Description by @owner\nAdds retries.\n\nApproved by @carol")
    assert "added 1 commit" not in text
    assert "### Review thread on src/retry.py:12 (resolved)\n@bob at" in text
    assert "### Review thread (unresolved)\n@alice at" in text
    assert text.index("This MR affects") < text.index("Unbounded") < text.index("backoff")
    assert counts == {"comments": 1, "reviews": 1, "threads": 2, "unresolved": 1}


@pytest.mark.forge
def test_fetch_reads_a_gitlab_mr_anonymously(paths, monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        body = {"data": {"project": {"mergeRequest": MR}}}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(discussion.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(discussion.shutil, "which", lambda *a, **k: pytest.fail("gh used"))
    target = "https://gitlab.cern.ch/atlas/sub/athena/-/merge_requests/7"
    text, counts = discussion.fetch(target, paths, Config())
    assert "kept on purpose" in text and counts["threads"] == 2
    (request,) = requests
    assert request.full_url == "https://gitlab.cern.ch/api/graphql"
    assert not request.has_header("Authorization") and not request.has_header("Private-token")
    variables = json.loads(request.data)["variables"]
    assert variables == {"project": "atlas/sub/athena", "iid": "7"}


@pytest.mark.forge
@pytest.mark.parametrize(
    "data,message",
    [
        ({"project": None}, "only in public projects"),
        ({"project": {"mergeRequest": None}}, "only in public projects"),
        (OSError("unreachable"), "Could not read the MR discussion from gitlab.com"),
    ],
)
def test_gitlab_fetch_refuses_what_it_cannot_read(paths, monkeypatch, data, message):
    def urlopen(request, timeout):
        if isinstance(data, Exception):
            raise data
        return io.BytesIO(json.dumps({"data": data}).encode())

    monkeypatch.setattr(discussion.urllib.request, "urlopen", urlopen)
    with pytest.raises(LXError, match=message):
        discussion.fetch("https://gitlab.com/g/p/-/merge_requests/7", paths, Config())
