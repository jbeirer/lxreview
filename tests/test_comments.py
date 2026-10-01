import json
import subprocess

import pytest

from lxreview import comments
from lxreview.config import Config
from lxreview.errors import Category, LXError
from lxreview.forge import parse

TARGET = parse("https://github.com/org/repo/pull/7")
HEAD = "a" * 40


@pytest.fixture
def gh(monkeypatch):
    """A fake gh: answers in order, and the calls it received."""
    calls, answers = [], []

    def run(argv, paths, **kwargs):
        calls.append({"argv": argv, "input": kwargs.get("input")})
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        code, stdout = answer
        return subprocess.CompletedProcess(argv, code, stdout, "token ghp_secretsecretsecret")

    monkeypatch.setattr(comments, "github_cli", lambda paths, config: ("/usr/bin/gh", {}))
    monkeypatch.setattr(comments, "run", run)
    return calls, answers


def test_hunks_are_the_new_side_ranges_of_a_patch():
    patch = "@@ -1,3 +1,4 @@ def f():\n a\n+b\n c\n d\n@@ -20 +21 @@\n-x\n+y\n@@ -40,2 +42,0 @@\n-gone\n-gone\n"
    assert comments.hunks(patch) == [range(1, 5), range(21, 22)]
    # An added file, a deleted one, and a file GitHub shows without a patch.
    assert comments.hunks("@@ -0,0 +1,3 @@\n+a\n+b\n+c") == [range(1, 4)]
    assert comments.hunks("@@ -1,2 +0,0 @@\n-a\n-b") == []
    assert comments.hunks(None) == []


def test_diff_lines_reads_every_page_of_the_pr_files(paths, gh):
    calls, answers = gh
    first = [{"filename": "src/a.py", "status": "modified", "patch": "@@ -5,2 +5,3 @@\n a\n+b\n c"}]
    # A pure rename and a binary file have no patch, so no commentable line.
    second = [
        {"filename": "src/renamed.py", "status": "renamed", "previous_filename": "src/old.py"},
        {"filename": "logo.png", "status": "added"},
    ]
    answers.append((0, json.dumps(first) + "\n" + json.dumps(second)))
    assert comments.diff_lines(TARGET, paths, Config()) == {
        "src/a.py": [range(5, 8)],
        "src/renamed.py": [],
        "logo.png": [],
    }
    assert calls[0]["argv"] == [
        "/usr/bin/gh",
        "api",
        "--paginate",
        "repos/org/repo/pulls/7/files?per_page=100",
    ]


def test_pending_review_finds_only_the_users_draft(paths, gh):
    _, answers = gh
    submitted = {"state": "COMMENTED", "html_url": "https://github.com/org/repo/pull/7#r1"}
    pending = {"state": "PENDING", "html_url": "https://github.com/org/repo/pull/7#r2"}
    answers += [(0, json.dumps([submitted])), (0, json.dumps([submitted]) + json.dumps([pending]))]
    assert comments.pending_review(TARGET, paths, Config()) is None
    assert comments.pending_review(TARGET, paths, Config()) == pending["html_url"]


def test_unreadable_reviews_refuse_rather_than_guess(paths, gh):
    _, answers = gh
    answers.append((1, '{"message": "Not Found"}'))
    with pytest.raises(LXError, match="Could not read the PR's reviews with gh"):
        comments.pending_review(TARGET, paths, Config())


def comment(finding, path, line, start_line=None, body="Fix it."):
    return {"finding": finding, "path": path, "line": line, "start_line": start_line, "body": body}


RANGES = {"src/a.py": [range(10, 20), range(30, 40)]}
SUGGESTION = "Use the bound.\n```suggestion\nfor i in range(n + 1):\n```"


def test_build_places_comments_inline_only_within_one_hunk():
    payload = comments.build(
        [
            comment("S1", "src/a.py", 12),
            comment("S2", "src/a.py", 15, start_line=13, body=SUGGESTION),
            # Across two hunks: GitHub cannot place it, and its suggestion would not apply.
            comment("S3", "src/a.py", 32, start_line=18, body=SUGGESTION),
            comment("N1", "src/b c.py", 4),
            comment("N2", None, 3),
        ],
        {"S3": "Loop bound", "N2": "Missing test"},
        HEAD,
        TARGET,
        RANGES,
    )
    assert "event" not in payload and payload["commit_id"] == HEAD
    assert payload["comments"] == [
        {"path": "src/a.py", "line": 12, "side": "RIGHT", "body": "Fix it."},
        {
            "path": "src/a.py",
            "line": 15,
            "side": "RIGHT",
            "body": SUGGESTION,
            "start_line": 13,
            "start_side": "RIGHT",
        },
    ]
    entries = payload["body"].split("\n\n---\n\n")
    blob = f"https://github.com/org/repo/blob/{HEAD}"
    assert entries == [
        f"**src/a.py:18-32**\n{blob}/src/a.py#L18-L32\n\n"
        "Use the bound.\n```\nfor i in range(n + 1):\n```",
        f"**src/b c.py:4**\n{blob}/src/b%20c.py#L4\n\nFix it.",
        "**Missing test**\n\nFix it.",
    ]


def test_build_redacts_and_shortens_comment_text():
    long = "Explain. ghp_abcdefghijklmnopqrstuvwxyz\n```suggestion\n" + "x = 1\n" * 3000 + "```"
    payload = comments.build([comment("S1", "src/a.py", 12, body=long)], {}, HEAD, TARGET, RANGES)
    (placed,) = payload["comments"]
    assert "ghp_" not in placed["body"] and "[REDACTED]" in placed["body"]
    assert len(placed["body"]) < comments.LIMIT + 100
    # A shortened suggestion would replace the lines with part of the fix.
    assert "```suggestion" not in placed["body"]
    # The open fence is closed, so nothing after it renders as code.
    assert placed["body"].endswith("\n```\n\n(shortened)")
    assert "body" not in payload


def test_a_redacted_suggestion_can_no_longer_be_applied():
    body = "Read it from the config.\n```suggestion\npassword = settings.password\n```"
    payload = comments.build([comment("S1", "src/a.py", 12, body=body)], {}, HEAD, TARGET, RANGES)
    (placed,) = payload["comments"]
    assert placed["body"] == "Read it from the config.\n```\n[REDACTED]\n```"


def test_moving_comments_into_the_body_keeps_the_existing_summary():
    payload = comments.build(
        [comment("S1", "src/a.py", 12, body=SUGGESTION), comment("N1", None, 1, body="Doc.")],
        {"N1": "Docs"},
        HEAD,
        TARGET,
        RANGES,
    )
    moved = comments.in_body(payload, TARGET)
    assert moved["comments"] == [] and "event" not in moved
    assert moved["body"] == (
        "**Docs**\n\nDoc.\n\n---\n\n"
        f"**src/a.py:12**\nhttps://github.com/org/repo/blob/{HEAD}/src/a.py#L12\n\n"
        "Use the bound.\n```\nfor i in range(n + 1):\n```"
    )


def test_a_body_over_the_limit_keeps_every_finding():
    long = "Explain.\n" + "x" * (comments.LIMIT - 20)
    off_diff = [comment(f"N{i}", "src/b.py", i, body=long) for i in range(1, 8)]
    payload = comments.build(off_diff, {}, HEAD, TARGET, RANGES)
    assert len(payload["body"]) <= comments.BODY_LIMIT
    entries = payload["body"].split(comments.SEPARATOR)
    assert [e.split("\n")[0] for e in entries] == [f"**src/b.py:{i}**" for i in range(1, 8)]
    assert all("Explain." in e and e.endswith("(shortened)") for e in entries)


def test_moving_comments_into_a_full_body_keeps_every_finding():
    long = "Explain.\n" + "x" * (comments.LIMIT - 20)
    payload = comments.build(
        [comment(f"N{i}", "src/b.py", i, body=long) for i in range(1, 5)]
        + [comment(f"S{i}", "src/a.py", 10 + i, body=long) for i in range(1, 5)],
        {},
        HEAD,
        TARGET,
        RANGES,
    )
    assert len(payload["comments"]) == 4 and len(payload["body"]) <= comments.BODY_LIMIT
    moved = comments.in_body(payload, TARGET)
    assert len(moved["body"]) <= comments.BODY_LIMIT
    # The existing summary is kept whole, and every moved comment follows it.
    assert moved["body"].startswith(payload["body"] + comments.SEPARATOR)
    headings = [e.split("\n")[0] for e in moved["body"].split(comments.SEPARATOR)]
    assert headings == [f"**src/b.py:{i}**" for i in range(1, 5)] + [
        f"**src/a.py:{10 + i}**" for i in range(1, 5)
    ]
    assert moved["body"].count("Explain.") == 8


def test_discard_deletes_the_pending_review(paths, gh):
    calls, answers = gh
    answers.append((0, ""))
    comments.discard(TARGET, {"id": 99, "html_url": "https://x/r99"}, paths, Config())
    assert calls[0]["argv"] == [
        "/usr/bin/gh",
        "api",
        "-X",
        "DELETE",
        "repos/org/repo/pulls/7/reviews/99",
    ]


@pytest.mark.parametrize("answer", [(1, "{}"), subprocess.TimeoutExpired(["gh"], 120)])
def test_a_review_that_could_not_be_discarded_is_named(paths, gh, answer):
    _, answers = gh
    answers.append(answer)
    with pytest.raises(LXError, match="https://x/r99 could not be discarded"):
        comments.discard(TARGET, {"id": 99, "html_url": "https://x/r99"}, paths, Config())


ANCHOR_ERROR = json.dumps(
    {
        "message": "Unprocessable Entity",
        "errors": ["Pull request review thread line must be part of the diff"],
        "status": "422",
    }
)
CREATED = json.dumps({"id": 99, "html_url": "https://github.com/org/repo/pull/7#r99"})


def test_an_unplaceable_anchor_is_posted_once_more_in_the_body(paths, gh):
    calls, answers = gh
    answers += [(1, ANCHOR_ERROR), (0, CREATED)]
    payload = comments.build([comment("S1", "src/a.py", 12)], {}, HEAD, TARGET, RANGES)
    posted = comments.post(TARGET, payload, paths, Config())
    assert posted == {
        "id": 99,
        "html_url": "https://github.com/org/repo/pull/7#r99",
        "inline": 0,
        "moved_to_body": True,
    }
    assert len(calls) == 2
    assert calls[0]["argv"] == [
        "/usr/bin/gh",
        "api",
        "-X",
        "POST",
        "repos/org/repo/pulls/7/reviews",
        "--input",
        "-",
    ]
    first, second = (json.loads(call["input"]) for call in calls)
    assert first == payload and len(first["comments"]) == 1
    assert second["comments"] == [] and "src/a.py:12" in second["body"]
    assert "event" not in second


def test_a_second_anchor_refusal_is_not_retried_again(paths, gh):
    calls, answers = gh
    answers += [(1, ANCHOR_ERROR), (1, ANCHOR_ERROR)]
    payload = comments.build([comment("S1", "src/a.py", 12)], {}, HEAD, TARGET, RANGES)
    with pytest.raises(LXError, match="GitHub refused the review: Unprocessable Entity"):
        comments.post(TARGET, payload, paths, Config())
    assert len(calls) == 2


def test_an_ambiguous_failure_is_never_retried(paths, gh):
    calls, answers = gh
    answers.append(subprocess.TimeoutExpired(["gh"], 120))
    payload = comments.build([comment("S1", "src/a.py", 12)], {}, HEAD, TARGET, RANGES)
    with pytest.raises(LXError, match="not retried. Check the PR on GitHub") as err:
        comments.post(TARGET, payload, paths, Config())
    assert err.value.category == Category.TIMEOUT and len(calls) == 1


def test_other_refusals_name_githubs_message_but_never_stderr(paths, gh):
    calls, answers = gh
    answers.append((1, json.dumps({"message": "Resource not accessible by integration"})))
    payload = comments.build([comment("S1", "src/a.py", 12)], {}, HEAD, TARGET, RANGES)
    with pytest.raises(LXError) as err:
        comments.post(TARGET, payload, paths, Config())
    assert "Resource not accessible by integration" in str(err.value)
    assert "ghp_" not in str(err.value) and len(calls) == 1


def test_posting_needs_gh(paths, monkeypatch):
    monkeypatch.setattr(comments, "github_cli", lambda paths, config: (None, {}))
    with pytest.raises(LXError, match="gh auth login"):
        comments.post(TARGET, {"commit_id": HEAD, "comments": []}, paths, Config())
