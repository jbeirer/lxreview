import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from conftest import DISCUSSION

from lxreview.config import Config
from lxreview.contracts import parse_response
from lxreview.errors import Category, LXError
from lxreview.runs import RunStore
from lxreview.worker import (
    Comment,
    Decision,
    EditResult,
    Evaluation,
    PublishResult,
    Review,
    execute,
)


class Repo:
    head_value = "a" * 40

    def __init__(self, path, paths):
        self.path, self.paths = path, paths

    def audit_root(self):
        return self.paths.root / "audit"

    def verify_identity(self, identity):
        pass

    def placeholders_hidden(self):
        return contextlib.nullcontext()

    def preflight(self, target):
        return {
            "head": self.head_value,
            "branch": "feature",
            "upstream": "origin/feature",
            "remote_url": "url",
        }

    def head(self):
        return self.head_value

    worktree = "checked tree"
    committed: str | None = None

    def worktree_tree(self, env):
        return self.worktree

    def tree(self, commit):
        return self.committed or self.worktree

    def call(self, *args):
        return "diff --git a/file b/file\n+fix"

    def push_command(self):
        return ["git", "push", "origin", "HEAD:refs/heads/feature"]

    def publish(self, commit, base, env):
        if commit != self.head_value or base == commit:
            from lxreview.errors import Category, LXError

            raise LXError(Category.UNSAFE, "Publication must add exactly one commit")
        return commit


class Reviewer:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def review(self, request):
        self.requests.append(request)
        return parse_response(next(self.responses))


def create(paths, tmp_path):
    repo = Repo(tmp_path, paths)
    return RunStore.create(
        paths,
        tmp_path,
        "https://github.com/org/repo/pull/1",
        repo.preflight(""),
        5,
        repo.audit_root(),
    )


async def test_full_two_pass_loop_with_prior_raw_saved(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES", "VERDICT: CLEAN"])
    calls = []

    async def turn(paths, config, store, prompt, schema, read_only):
        calls.append(read_only)
        audit = Path(store.load()["audit"]) / "pass-01"
        assert (audit / "reviewer.md").read_text().startswith("SUBSTANTIAL")
        if read_only:
            # The evaluation weighs the PR's own discussion, also kept with the pass.
            assert "PR discussion" in prompt and prompt.endswith(DISCUSSION)
            assert (audit / "discussion.md").read_text() == DISCUSSION
            return Evaluation(
                findings=[
                    Decision(
                        finding="S1", decision="ACCEPTED", reason="reproduced", evidence="file:1"
                    )
                ]
            )
        assert (audit / "evaluation.json").exists()
        if schema is EditResult:
            # Checks run on the unmodified code first; failures already there do not block.
            assert "on the unmodified code" in prompt
            return EditResult(
                tests=["pytest: 1 failed, 40 passed"],
                tests_passed=True,
                preexisting_failures=["test_io: fails identically without the change"],
                summary="fixed",
            )
        # LXReview pushes; the worker only commits.
        assert "Do not push" in prompt
        Repo.head_value = "b" * 40
        return PublishResult(commit=Repo.head_value)

    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "CLEAN"
    assert calls == [True, False, False]
    assert [r.head_sha for r in reviewer.requests] == ["a" * 40, "b" * 40]
    assert all(not r.known_findings for r in reviewer.requests)
    assert (Path(store.load()["audit"]) / "pass-01/diff.patch").exists()
    assert "CLEAN" in store.report()
    events = (store.directory / "events.jsonl").read_text().splitlines()
    reported = [e for e in map(json.loads, events) if e["kind"] == "tests_reported"]
    assert reported[0]["preexisting"] == ["test_io: fails identically without the change"]


@pytest.mark.parametrize("raw", ["VERDICT: ACCESS_FAILED", "truncated response"])
async def test_failed_review_preserved_never_edits(paths, tmp_path, monkeypatch, raw):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("Claude must not be called")

    await execute(paths, Config(), store, Reviewer([raw]), forbidden)
    assert store.load()["status"] == "FAILED"
    assert (Path(store.load()["audit"]) / "pass-01/reviewer.md").read_text() == raw


async def test_rejected_findings_finish_without_edits(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    async def turn(*args, **kwargs):
        assert kwargs["read_only"]
        return Evaluation(
            findings=[
                Decision(
                    finding="S1", decision="REJECTED", reason="already checked", evidence="file:2"
                )
            ]
        )

    await execute(
        paths,
        Config(),
        store,
        Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"]),
        turn,
    )
    assert store.load()["status"] == "NO_VALID_SUBSTANTIAL_FINDINGS"


async def test_cancel_marker_prevents_review(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)
    (store.directory / "cancel").touch()
    reviewer = Reviewer([])
    await execute(paths, Config(), store, reviewer)
    assert store.load()["status"] == "CANCELLED"
    assert not reviewer.requests


async def test_failed_push_cannot_become_clean(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)

    async def turn(*args, **kwargs):
        if kwargs["read_only"]:
            return Evaluation(
                findings=[Decision(finding="S1", decision="ACCEPTED", reason="bug", evidence="f:1")]
            )
        if args[4] is EditResult:
            return EditResult(
                tests=["pytest passes"], tests_passed=True, preexisting_failures=[], summary="fixed"
            )
        # The worker reports a commit that is not HEAD: publication must refuse it.
        return PublishResult(commit="b" * 40)

    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "FAILED"
    assert len(reviewer.requests) == 1


async def test_cap_does_not_leave_unreviewed_edits(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)
    store.update(max_passes=1)

    async def turn(*args, **kwargs):
        assert kwargs["read_only"]
        return Evaluation(
            findings=[Decision(finding="S1", decision="ACCEPTED", reason="bug", evidence="f:1")]
        )

    await execute(
        paths,
        Config(),
        store,
        Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"]),
        turn,
    )
    assert store.load()["status"] == "MAX_PASSES"


async def test_missing_finding_evaluation_fails_before_edit(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    async def turn(*args, **kwargs):
        assert kwargs["read_only"]
        return Evaluation(
            findings=[
                Decision(finding="S1", decision="REJECTED", reason="not a bug", evidence="f:1")
            ]
        )

    reviewer = Reviewer(
        ["SUBSTANTIAL [S1] first\nSUBSTANTIAL [S2] second\nVERDICT: SUBSTANTIAL_ISSUES"]
    )
    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "FAILED"
    assert "every listed finding" in store.load()["error"]


@pytest.mark.parametrize("phase", ["review", "evaluation"])
async def test_moving_head_cannot_finish_clean_or_start_fixes(paths, tmp_path, monkeypatch, phase):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)

    class MovingReviewer:
        async def review(self, request):
            if phase == "review":
                Repo.head_value = "b" * 40
                return parse_response("VERDICT: CLEAN")
            return parse_response("SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES")

    async def turn(*args, **kwargs):
        assert kwargs["read_only"]
        Repo.head_value = "b" * 40
        return Evaluation(
            findings=[Decision(finding="S1", decision="REJECTED", reason="x", evidence="f:1")]
        )

    await execute(paths, Config(), store, MovingReviewer(), turn)
    assert store.load()["status"] == "FAILED"
    assert "HEAD changed" in store.load()["error"]


async def test_cancellation_during_review_prevents_claude(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    class CancellingReviewer:
        async def review(self, request):
            (store.directory / "cancel").touch()
            return parse_response("SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES")

    async def forbidden(*args, **kwargs):
        pytest.fail("Cancellation must prevent further tools")

    await execute(paths, Config(), store, CancellingReviewer(), forbidden)
    assert store.load()["status"] == "CANCELLED"


async def test_failed_edit_turn_preserves_diff_and_audit(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    async def turn(*args, **kwargs):
        if kwargs["read_only"]:
            return Evaluation(
                findings=[Decision(finding="S1", decision="ACCEPTED", reason="bug", evidence="f:1")]
            )
        raise RuntimeError("failed after edit")

    await execute(
        paths,
        Config(),
        store,
        Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"]),
        turn,
    )
    assert store.load()["status"] == "FAILED"
    assert (Path(store.load()["audit"]) / "pass-01/diff.patch").read_text().endswith("+fix")
    assert (Path(store.load()["audit"]) / "pass-01/evaluation.json").exists()


async def test_failed_tests_prevent_publication(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)

    async def turn(*args, **kwargs):
        if kwargs["read_only"]:
            return Evaluation(
                findings=[Decision(finding="S1", decision="ACCEPTED", reason="bug", evidence="f:1")]
            )
        assert args[4] is EditResult, "Publication must not start after failed tests"
        return EditResult(
            tests=["pytest: failed"],
            tests_passed=False,
            preexisting_failures=[],
            summary="not fixed",
        )

    await execute(
        paths,
        Config(),
        store,
        Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"]),
        turn,
    )
    assert store.load()["status"] == "FAILED"
    assert "publication refused" in store.load()["error"]


async def test_settled_preflight_retries_pull_ref_lag(monkeypatch):
    from lxreview import worker
    from lxreview.errors import Category
    from lxreview.git import PullRefPending

    attempts = []

    class Lagging:
        def preflight(self, target):
            attempts.append(target)
            if len(attempts) < 3:
                raise PullRefPending(Category.UNSAFE, "lag")
            return {"head": "c" * 40}

    async def no_sleep(_):
        pass

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)
    assert (await worker.settled_preflight(Lagging(), "t"))["head"] == "c" * 40
    assert len(attempts) == 3


@pytest.mark.parametrize(
    ("workers", "expected"), [("auto", "`-n auto`"), (8, "`-n 8`"), ("off", None)]
)
def test_edit_turn_names_the_test_worker_count(workers, expected):
    from lxreview.worker import command_guidance

    config = Config()
    config.verify.test_workers = workers
    text = command_guidance(config)
    assert "Grep, Glob and Read tools" in text
    assert (expected in text) if expected else "-n" not in text


def test_run_choices_are_validated():
    from lxreview.errors import LXError

    config = Config().with_choices({"reviewer_effort": "high", "worker_model": "opus"})
    assert config.reviewer.reasoning_effort == "high" and config.worker.model == "opus"
    for bad in ({"worker_effort": "extreme"}, {"reviewer_model": "x; y"}, {"unknown": "x"}):
        with pytest.raises(LXError):
            Config().with_choices(bad)


async def test_run_choices_reach_the_reviewer_and_claude(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    store.update(
        choices={"reviewer_model": "GPT-5.5", "reviewer_effort": "high", "worker_effort": "max"}
    )
    efforts = []

    async def turn(paths, config, store, prompt, schema, read_only):
        efforts.append(config.worker.effort)
        return Evaluation(
            findings=[Decision(finding="S1", decision="REJECTED", reason="no", evidence="f:1")]
        )

    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    await execute(paths, Config(), store, reviewer, turn)
    assert (reviewer.requests[0].model, reviewer.requests[0].reasoning_effort) == (
        "GPT-5.5",
        "high",
    )
    assert efforts == ["max"]


def test_the_reviewer_sets_the_highest_reasoning_by_default():
    config = Config()
    assert config.reviewer.reasoning_effort == "highest" and config.reviewer.model == "default"
    assert (config.worker.model, config.worker.effort) == ("default", "default")


def test_the_reviewer_level_is_always_chosen_explicitly(paths):
    # ChatGPT keeps the last level on the account; "leave it" would inherit an earlier run's.
    with pytest.raises(LXError, match="Invalid run choice"):
        Config().with_choices({"reviewer_effort": "default"})
    paths.config.write_text('schema_version = 1\n[reviewer]\nreasoning_effort = "default"\n')
    with pytest.raises(LXError, match="reviewer.reasoning_effort"):
        Config.load(paths)


def test_saved_config_holds_only_deliberate_settings(paths):
    # A saved default would pin it when a later release changes the default.
    config = Config()
    config.reviewer.reasoning_effort = "high"
    config.save(paths)
    assert (
        paths.config.read_text() == 'schema_version = 1\n\n[reviewer]\nreasoning_effort = "high"\n'
    )
    assert Config.load(paths) == config


def fixing_turn(decide, prompts):
    """A fake Claude: evaluations from decide(listed ids), then a passing fix and a commit."""

    async def turn(paths, config, store, prompt, schema, read_only):
        prompts.append(prompt)
        if read_only:
            listed = prompt.split("finding listed here against this repository: ", 1)[1]
            ids = listed.split(". ", 1)[0].split(", ")
            return Evaluation(
                findings=[
                    Decision(finding=i, decision=decide(i), reason="checked", evidence="f:1")
                    for i in ids
                ]
            )
        if schema is EditResult:
            return EditResult(
                tests=["checks: passed"],
                tests_passed=True,
                preexisting_failures=[],
                summary="fixed",
            )
        Repo.head_value = ("b" if Repo.head_value == "a" * 40 else "c") * 40
        return PublishResult(commit=Repo.head_value)

    return turn


async def test_non_blocking_findings_are_weighed_with_substantial_ones(
    paths, tmp_path, monkeypatch
):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(
        [
            "SUBSTANTIAL [S1] bug\nNON_BLOCKING [N1] misleading error text\nVERDICT: SUBSTANTIAL_ISSUES",
            "NON_BLOCKING [N1] rename a variable\nVERDICT: CLEAN",
        ]
    )
    turn = fixing_turn(lambda i: "REJECTED" if i == "S1" else "ACCEPTED", prompts)
    await execute(paths, Config(), store, reviewer, turn)
    # S1 rejected, N1 accepted: one polish pass, fixed, pushed and reviewed again.
    assert "S1, N1" in prompts[0]
    assert '"finding": "N1"' in prompts[1]
    # Polish was already done this run: the clean second review ends it untouched.
    assert len(prompts) == 3 and store.load()["status"] == "CLEAN"
    assert [r.head_sha for r in reviewer.requests] == ["a" * 40, "b" * 40]
    assert all(not r.known_findings for r in reviewer.requests)


async def test_a_clean_review_with_polish_gets_one_polish_pass(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(
        [
            "NON_BLOCKING [N1] missing test\nVERDICT: CLEAN",
            "NON_BLOCKING [N1] more polish\nVERDICT: CLEAN",
        ]
    )
    await execute(paths, Config(), store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    assert store.load()["status"] == "CLEAN"
    assert len(reviewer.requests) == 2 and len(prompts) == 3


async def test_final_pass_reports_polish_instead_of_editing(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    store.update(max_passes=1)
    prompts = []
    reviewer = Reviewer(
        ["SUBSTANTIAL [S1] bug\nNON_BLOCKING [N1] typo in docs\nVERDICT: SUBSTANTIAL_ISSUES"]
    )
    turn = fixing_turn(lambda i: "REJECTED" if i == "S1" else "ACCEPTED", prompts)
    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "NO_VALID_SUBSTANTIAL_FINDINGS"
    assert len(prompts) == 1
    events = (store.directory / "events.jsonl").read_text()
    assert '"polish_skipped"' in events and '"N1"' in events


async def test_a_substantial_only_run_leaves_non_blocking_findings_alone(
    paths, tmp_path, monkeypatch
):
    from lxreview.timeline import describe

    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    Repo.head_value = "a" * 40
    store = create(paths, tmp_path)
    store.update(substantial_only=True)
    prompts = []
    reviewer = Reviewer(
        [
            "SUBSTANTIAL [S1] bug\nNON_BLOCKING [N1] misleading error text\nVERDICT: SUBSTANTIAL_ISSUES",
            "NON_BLOCKING [N1] missing test\nVERDICT: CLEAN",
        ]
    )
    await execute(paths, Config(), store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    # Only S1 is weighed and fixed; the second review's polish ends the run without a turn.
    assert "repository: S1." in prompts[0] and '"finding": "N1"' not in prompts[1]
    assert len(prompts) == 3 and len(reviewer.requests) == 2
    assert store.load()["status"] == "CLEAN" and not store.load().get("polish_pass")
    events = [
        json.loads(line) for line in (store.directory / "events.jsonl").read_text().splitlines()
    ]
    ignored = [e for e in events if e["kind"] == "non_blocking_ignored"]
    assert [(e["pass_number"], e["findings"]) for e in ignored] == [(1, ["N1"]), (2, ["N1"])]
    (line,) = describe(ignored[0])
    assert line.endswith("  Non-blocking findings left unevaluated (substantial-only run): N1")


class PushRecorder(Repo):
    pushed: list[str] = []

    def publish(self, commit, base, env):
        PushRecorder.pushed.append(commit)
        return super().publish(commit, base, env)


async def approval_requested(store, step):
    for _ in range(500):
        if store.load().get("awaiting") == step:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"The run never asked for approval to {step}")


def kinds(store):
    lines = (store.directory / "events.jsonl").read_text().splitlines()
    return [json.loads(line)["kind"] for line in lines]


@pytest.fixture
def waiting(monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", PushRecorder)
    monkeypatch.setattr("lxreview.worker.APPROVAL_POLL", 0.01)
    monkeypatch.setattr(PushRecorder, "pushed", [])
    Repo.head_value = "a" * 40


async def test_commit_and_push_wait_for_approval(paths, tmp_path, waiting):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES", "VERDICT: CLEAN"])
    config = Config().with_choices({"commit": "ask", "push": "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, "commit")
    # Evaluated and fixed, but nothing committed yet.
    assert len(prompts) == 2 and store.load()["phase"] == "awaiting_commit"
    with pytest.raises(LXError, match="it waits for approval to commit"):
        store.approve("push")
    store.approve("commit")
    await approval_requested(store, "push")
    assert len(prompts) == 3 and not PushRecorder.pushed
    store.approve("push")
    await run
    state = store.load()
    assert state["status"] == "CLEAN" and state["awaiting"] is None
    assert PushRecorder.pushed == ["b" * 40]
    approvals = [kind for kind in kinds(store) if kind.startswith("approval")]
    assert approvals == ["approval_requested", "approval_granted"] * 2


async def test_automatic_runs_never_wait(paths, tmp_path, waiting):
    store = create(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES", "VERDICT: CLEAN"])
    await execute(paths, Config(), store, reviewer, fixing_turn(lambda i: "ACCEPTED", []))
    assert store.load()["status"] == "CLEAN" and PushRecorder.pushed == ["b" * 40]
    assert not any(kind.startswith("approval") for kind in kinds(store))


@pytest.mark.parametrize(("step", "turns"), [("commit", 2), ("push", 3)])
async def test_declining_stops_the_run_and_keeps_the_work(paths, tmp_path, waiting, step, turns):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    config = Config().with_choices({step: "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, step)
    (store.directory / "cancel").touch()
    await run
    state = store.load()
    assert state["status"] == "CANCELLED" and state["awaiting"] is None
    # The checked edits (or the local commit) stay; nothing more happens.
    assert len(prompts) == turns and not PushRecorder.pushed


async def test_approval_shows_the_tree_it_approves(paths, tmp_path, waiting, monkeypatch):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    trees = iter(["checked tree", "autosaved after the diff was captured"])
    monkeypatch.setattr(Repo, "worktree_tree", lambda self, env: next(trees))
    config = Config().with_choices({"commit": "ask"})
    await execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    state = store.load()
    assert state["status"] == "FAILED"
    assert "working tree changed after the checks" in state["error"]
    assert len(prompts) == 2 and "approval_requested" not in kinds(store)


async def test_no_commit_without_the_checked_tree(paths, tmp_path, waiting, monkeypatch):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])

    def unavailable(self, env):
        raise LXError(Category.UNAVAILABLE, "git failed")

    monkeypatch.setattr(Repo, "worktree_tree", unavailable)
    await execute(paths, Config(), store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    state = store.load()
    assert state["status"] == "FAILED"
    assert "checked change could not be captured" in state["error"]
    # Refused before the commit turn, so no local commit is left behind.
    assert len(prompts) == 2 and not PushRecorder.pushed


async def test_a_commit_holds_exactly_the_checked_change(paths, tmp_path, waiting, monkeypatch):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    config = Config().with_choices({"commit": "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, "commit")
    monkeypatch.setattr(Repo, "worktree", "edited while waiting")
    store.approve("commit")
    await run
    state = store.load()
    assert state["status"] == "FAILED"
    assert "working tree changed while waiting for approval" in state["error"]
    assert len(prompts) == 2 and not PushRecorder.pushed


async def test_a_commit_that_differs_from_the_approval_is_never_pushed(
    paths, tmp_path, waiting, monkeypatch
):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    config = Config().with_choices({"commit": "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, "commit")
    # For example a commit hook that changes and stages files during the commit turn.
    monkeypatch.setattr(Repo, "committed", "changed by a hook")
    store.approve("commit")
    await run
    state = store.load()
    assert state["status"] == "FAILED" and "differs from the checked change" in state["error"]
    assert len(prompts) == 3 and not PushRecorder.pushed


@pytest.mark.parametrize("choices", [{"push": "ask"}, {}])
async def test_a_commit_that_differs_from_the_checks_is_never_offered_or_pushed(
    paths, tmp_path, waiting, monkeypatch, choices
):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    # A commit hook changed the tree in the automatic commit turn.
    monkeypatch.setattr(Repo, "committed", "changed by a hook")
    config = Config().with_choices(choices)
    await execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    state = store.load()
    assert state["status"] == "FAILED" and "differs from the checked change" in state["error"]
    assert len(prompts) == 3 and not PushRecorder.pushed
    assert "approval_requested" not in kinds(store)


@pytest.mark.parametrize(("step", "turns"), [("commit", 2), ("push", 3)])
async def test_a_stop_wins_over_an_approval_in_the_same_poll(
    paths, tmp_path, waiting, monkeypatch, step, turns
):
    monkeypatch.setattr("lxreview.worker.APPROVAL_POLL", 0.2)
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    config = Config().with_choices({step: "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, step)
    # Both land before the sleeping worker polls again.
    store.approve(step)
    (store.directory / "cancel").touch()
    with pytest.raises(LXError, match="not waiting for approval"):
        store.approve(step)
    await run
    state = store.load()
    assert state["status"] == "CANCELLED" and state["awaiting"] is None
    assert len(prompts) == turns and not PushRecorder.pushed
    assert "approval_granted" not in kinds(store)


class Drafting(Repo):
    """A detached checkout of someone else's PR: no branch, upstream or push."""

    pr_head = "a" * 40
    lines = {"src/parse.py": 40}

    def verify_identity(self, identity):
        pytest.fail("A review-only run has no branch identity to verify")

    def preflight(self, target):
        pytest.fail("A review-only run needs no pushed branch")

    def publish(self, commit, base, env):
        pytest.fail("A review-only run never pushes")

    def review_checkpoint(self, target):
        if self.pr_head != self.head_value:
            raise LXError(Category.UNSAFE, "Local HEAD is not the PR head")
        return {"head": self.head_value}

    def line_count(self, commit, path):
        return self.lines.get(path)


PR = "https://github.com/org/repo/pull/1"
DRAFT = "https://github.com/org/repo/pull/1#pullrequestreview-7"


@pytest.fixture
def github(monkeypatch):
    """A fake GitHub: the PR's diff, the user's pending review and the reviews posted."""
    fake = {
        "ranges": {"src/parse.py": [range(10, 20)]},
        "pending": None,
        "posted": [],
        "discarded": [],
        # The PR head GitHub's API names; refs/pull/N/head (Drafting.pr_head) may lag behind it.
        "head": "a" * 40,
    }
    monkeypatch.setattr("lxreview.comments.pr_head", lambda *a: fake["head"])
    monkeypatch.setattr("lxreview.worker.Repository", Drafting)
    monkeypatch.setattr(Drafting, "pr_head", "a" * 40)
    monkeypatch.setattr(Repo, "head_value", "a" * 40)
    monkeypatch.setattr("lxreview.comments.diff_lines", lambda *a: fake["ranges"])
    monkeypatch.setattr("lxreview.comments.pending_review", lambda *a: fake["pending"])

    def post(target, payload, paths, config):
        fake["posted"].append(payload)
        return {"id": 7, "html_url": DRAFT, "inline": len(payload["comments"])}

    monkeypatch.setattr("lxreview.comments.post", post)
    monkeypatch.setattr(
        "lxreview.comments.discard", lambda target, review, *a: fake["discarded"].append(review)
    )
    return fake


def review_only(paths, tmp_path, max_passes=1):
    return RunStore.create(
        paths,
        tmp_path,
        PR,
        {"head": "a" * 40},
        max_passes,
        Drafting(tmp_path, paths).audit_root(),
        True,
    )


def reviewing(decisions, comments, prompts=None):
    """A fake Claude that only evaluates and drafts comments, as a review-only run allows."""

    async def turn(paths, config, store, prompt, schema, read_only):
        assert schema is Review and read_only, "A review-only run never edits or commits"
        if prompts is not None:
            prompts.append(prompt)
        return Review(
            findings=[
                Decision(finding=i, decision=d, reason="checked", evidence="src/parse.py:12")
                for i, d in decisions.items()
            ],
            comments=comments,
        )

    return turn


SUGGESTION = "Stop at the last bin.\n```suggestion\n    for i in range(n + 1):\n```"


async def test_review_only_drafts_accepted_findings_as_a_pending_review(paths, tmp_path, github):
    store = review_only(paths, tmp_path)
    prompts: list[str] = []
    reviewer = Reviewer(
        [
            "SUBSTANTIAL [S1] off by one\nNON_BLOCKING [N1] unclear name\n"
            "NON_BLOCKING [N2] reorder imports\nVERDICT: SUBSTANTIAL_ISSUES"
        ]
    )
    turn = reviewing(
        {"S1": "ACCEPTED", "N1": "ACCEPTED", "N2": "REJECTED"},
        [
            Comment(finding="S1", path="src/parse.py", line=12, start_line=12, body=SUGGESTION),
            # Outside the PR's diff: GitHub cannot place it inline.
            Comment(finding="N1", path="src/parse.py", line=30, body="Name it `limit`."),
        ],
        prompts,
    )
    await execute(paths, Config(), store, reviewer, turn)
    state = store.load()
    assert state["status"] == "REVIEW_DRAFTED" and state["completed_pass"] == 1
    # Every finding is weighed, without a polish pass, and the PR discussion still counts.
    assert len(prompts) == 1 and "repository: S1, N1, N2." in prompts[0]
    assert "```suggestion" in prompts[0] and prompts[0].endswith(DISCUSSION)
    (payload,) = github["posted"]
    assert "event" not in payload and payload["commit_id"] == "a" * 40
    assert payload["comments"] == [
        {"path": "src/parse.py", "line": 12, "side": "RIGHT", "body": SUGGESTION}
    ]
    assert "**src/parse.py:30**" in payload["body"]
    assert f"https://github.com/org/repo/blob/{'a' * 40}/src/parse.py#L30" in payload["body"]
    assert "Name it `limit`." in payload["body"] and "reorder" not in payload["body"]
    audit = Path(state["audit"]) / "pass-01"
    assert json.loads((audit / "review-draft.json").read_text()) == payload
    assert json.loads((audit / "review-posted.json").read_text())["html_url"] == DRAFT
    assert (audit / "evaluation.json").exists() and (audit / "discussion.md").exists()
    lines = (store.directory / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    (posted,) = [e for e in events if e["kind"] == "review_posted"]
    assert (posted["inline"], posted["summary"], posted["url"]) == (1, 1, DRAFT)
    assert "REVIEW_DRAFTED" in store.report() and DRAFT in store.report()


@pytest.mark.parametrize(
    ("path", "line", "start_line"),
    [
        ("docs/missing.md", 3, None),
        ("src/parse.py", 41, None),
        ("src/parse.py", 12, 14),
        ("/etc/passwd", 1, None),
        ("src/../src/parse.py", 12, None),
        ("./src/parse.py", 12, None),
    ],
)
async def test_an_anchor_that_is_not_in_the_file_goes_to_the_summary_without_a_link(
    paths, tmp_path, github, path, line, start_line
):
    store = review_only(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path=path, line=line, start_line=start_line, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    assert store.load()["status"] == "REVIEW_DRAFTED"
    (payload,) = github["posted"]
    # Without a valid anchor, the reviewer's title names the finding instead.
    assert payload["comments"] == [] and payload["body"] == "**off by one**\n\nFix it."


async def test_a_substantial_only_review_comments_on_substantial_findings_alone(
    paths, tmp_path, github
):
    store = review_only(paths, tmp_path)
    store.update(substantial_only=True)
    prompts: list[str] = []
    reviewer = Reviewer(
        ["SUBSTANTIAL [S1] off by one\nNON_BLOCKING [N1] unclear name\nVERDICT: SUBSTANTIAL_ISSUES"]
    )
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Stop at the last bin.")
    await execute(
        paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment], prompts)
    )
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert "repository: S1." in prompts[0]
    (payload,) = github["posted"]
    assert [c["body"] for c in payload["comments"]] == ["Stop at the last bin."]


async def test_review_only_posts_nothing_for_a_clean_review(paths, tmp_path, github):
    store = review_only(paths, tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail("A clean review needs no evaluation")

    await execute(paths, Config(), store, Reviewer(["VERDICT: CLEAN"]), forbidden)
    assert store.load()["status"] == "CLEAN" and not github["posted"]


async def test_review_only_posts_nothing_when_no_finding_is_accepted(paths, tmp_path, github):
    store = review_only(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "REJECTED"}, []))
    assert store.load()["status"] == "NO_VALID_SUBSTANTIAL_FINDINGS" and not github["posted"]
    assert not (Path(store.load()["audit"]) / "pass-01/review-draft.json").exists()


@pytest.mark.parametrize(
    ("raw", "decisions", "moment"),
    [
        ("VERDICT: CLEAN", {}, "while the reviewer was running"),
        (
            "SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES",
            {"S1": "REJECTED"},
            "while findings were evaluated",
        ),
    ],
)
async def test_a_push_the_pr_ref_does_not_show_yet_is_no_clean_result(
    paths, tmp_path, github, raw, decisions, moment
):
    store = review_only(paths, tmp_path)
    # refs/pull/1/head still names the reviewed head, but the PR has moved on.
    github["head"] = "b" * 40
    await execute(paths, Config(), store, Reviewer([raw]), reviewing(decisions, []))
    state = store.load()
    assert state["status"] == "FAILED" and f"The PR head changed {moment}" in state["error"]
    assert state["completed_pass"] == 0 and not github["posted"]


@pytest.mark.parametrize(
    "comments",
    [
        # A comment for a rejected finding, and an accepted finding without one.
        [
            Comment(finding="S1", path="src/parse.py", line=12, body="Fix it."),
            Comment(finding="S2", path="src/parse.py", line=13, body="Rejected anyway."),
        ],
        [],
        [
            Comment(finding="S1", path="src/parse.py", line=12, body="Fix it."),
            Comment(finding="S1", path="src/parse.py", line=13, body="Twice."),
        ],
    ],
)
async def test_comments_must_match_the_accepted_findings(paths, tmp_path, github, comments):
    store = review_only(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] a\nSUBSTANTIAL [S2] b\nVERDICT: SUBSTANTIAL_ISSUES"])
    turn = reviewing({"S1": "ACCEPTED", "S2": "REJECTED"}, comments)
    await execute(paths, Config(), store, reviewer, turn)
    state = store.load()
    assert state["status"] == "FAILED" and "one to one" in state["error"]
    assert not github["posted"]
    # The decisions are kept for the audit all the same.
    assert (Path(state["audit"]) / "pass-01/evaluation.json").exists()


async def test_a_pr_head_that_moved_gets_no_review(paths, tmp_path, github, monkeypatch):
    store = review_only(paths, tmp_path)

    def moved(*args):
        # The author pushes while LXReview reads the diff.
        Drafting.pr_head = "b" * 40
        return github["ranges"]

    monkeypatch.setattr("lxreview.comments.diff_lines", moved)
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "FAILED" and "not the PR head" in state["error"]
    assert not github["posted"] and state["completed_pass"] == 0
    assert not (Path(state["audit"]) / "pass-01/review-posted.json").exists()


async def test_a_pr_head_that_moved_during_creation_discards_the_review(
    paths, tmp_path, github, monkeypatch
):
    store = review_only(paths, tmp_path)

    def moved(target, payload, paths, config):
        # The author pushes after the last check, while GitHub creates the review.
        github["posted"].append(payload)
        Drafting.pr_head = "b" * 40
        return {"id": 7, "html_url": DRAFT, "inline": len(payload["comments"])}

    monkeypatch.setattr("lxreview.comments.post", moved)
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "FAILED" and "not the PR head" in state["error"]
    assert [r["id"] for r in github["discarded"]] == [7] and state["completed_pass"] == 0
    assert not (Path(state["audit"]) / "pass-01/review-posted.json").exists()


async def test_a_push_the_pr_ref_does_not_show_yet_gets_no_review(paths, tmp_path, github):
    store = review_only(paths, tmp_path)
    # refs/pull/1/head still names the reviewed head, but the PR has moved on.
    github["head"] = "b" * 40
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "FAILED"
    assert "The PR head changed before the review was posted" in state["error"]
    assert not github["posted"] and state["completed_pass"] == 0


async def test_a_push_during_creation_the_pr_ref_does_not_show_discards_the_review(
    paths, tmp_path, github, monkeypatch
):
    store = review_only(paths, tmp_path)

    def moved(target, payload, paths, config):
        github["posted"].append(payload)
        github["head"] = "b" * 40
        return {"id": 7, "html_url": DRAFT, "inline": len(payload["comments"])}

    monkeypatch.setattr("lxreview.comments.post", moved)
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "FAILED"
    assert "The PR head changed while the review was posted" in state["error"]
    assert [r["id"] for r in github["discarded"]] == [7] and state["completed_pass"] == 0
    assert not (Path(state["audit"]) / "pass-01/review-posted.json").exists()


async def test_an_existing_pending_review_is_never_replaced(paths, tmp_path, github):
    store = review_only(paths, tmp_path)
    github["pending"] = DRAFT
    reviewer = Reviewer(["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES"])
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "FAILED" and f"pending review on this PR: {DRAFT}" in state["error"]
    assert not github["posted"]


async def test_review_only_saturates_and_posts_once(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    reviewer = Reviewer(
        ["SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES", "VERDICT: CLEAN"]
    )
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Fix it.")
    await execute(paths, Config(), store, reviewer, reviewing({"S1": "ACCEPTED"}, [comment]))
    state = store.load()
    assert state["status"] == "REVIEW_DRAFTED" and state["completed_pass"] == 2
    assert [r.known_findings for r in reviewer.requests] == [[], ["off by one"]]
    assert len(github["posted"]) == 1
    audit = Path(state["audit"])
    assert (audit / "pass-02/review-posted.json").exists()
    assert not (audit / "pass-01/review-draft.json").exists()
    assert json.loads((audit / "pass-02/comments.json").read_text())["findings"] == []
    events = [
        json.loads(line) for line in (store.directory / "events.jsonl").read_text().splitlines()
    ]
    assert next(e for e in events if e["kind"] == "review_posted")["passes"] == 2


async def test_review_only_duplicates_and_new_findings(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    reviewer = Reviewer(
        [
            "SUBSTANTIAL [S1] off by one\nVERDICT: SUBSTANTIAL_ISSUES",
            "SUBSTANTIAL [S1] same cause\nSUBSTANTIAL [S2] overflow\nVERDICT: SUBSTANTIAL_ISSUES",
            "VERDICT: CLEAN",
        ]
    )

    async def turn(paths, config, store, prompt, schema, read_only):
        number = store.load()["pass"]
        if number == 1:
            return await reviewing(
                {"S1": "ACCEPTED"},
                [Comment(finding="S1", path="src/parse.py", line=12, body="First")],
            )(paths, config, store, prompt, schema, read_only)
        assert "P1-S1 (SUBSTANTIAL, ACCEPTED)" in prompt
        assert "decision DUPLICATE" in prompt
        return Review(
            findings=[
                Decision(
                    finding="S1",
                    decision="DUPLICATE",
                    duplicate_of="P1-S1",
                    reason="same",
                    evidence="code",
                ),
                Decision(finding="S2", decision="ACCEPTED", reason="new", evidence="code"),
            ],
            comments=[Comment(finding="S2", path="src/parse.py", line=13, body="Second")],
        )

    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert [c["body"] for c in github["posted"][0]["comments"]] == ["First", "Second"]
    assert reviewer.requests[2].known_findings == ["off by one", "same cause", "overflow"]


@pytest.mark.parametrize("last", ["reject", "polish", "limit"])
async def test_review_only_stopping_conditions(paths, tmp_path, github, last):
    store = review_only(paths, tmp_path, 2)
    raw = (
        "NON_BLOCKING [N1] name\nVERDICT: CLEAN"
        if last == "polish"
        else "SUBSTANTIAL [S1] new\nVERDICT: SUBSTANTIAL_ISSUES"
    )
    reviewer = Reviewer(["SUBSTANTIAL [S1] first\nVERDICT: SUBSTANTIAL_ISSUES", raw])

    async def turn(paths, config, store, prompt, schema, read_only):
        first = store.load()["pass"] == 1
        key = "N1" if not first and last == "polish" else "S1"
        accepted = first or last != "reject"
        return await reviewing(
            {key: "ACCEPTED" if accepted else "REJECTED"},
            [Comment(finding=key, path="src/parse.py", line=12, body="Fix")] if accepted else [],
        )(paths, config, store, prompt, schema, read_only)

    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert ("Pass limit reached" in store.load()["error"]) == (last == "limit")
    assert len(github["posted"][0]["comments"]) == (1 if last == "reject" else 2)


@pytest.mark.parametrize(
    "decision,duplicate_of", [("DUPLICATE", None), ("DUPLICATE", "P9-S1"), ("ACCEPTED", "P1-S1")]
)
async def test_invalid_duplicate_protocol(paths, tmp_path, github, decision, duplicate_of):
    store = review_only(paths, tmp_path)

    async def turn(*args, **kwargs):
        return Review(
            findings=[
                Decision(
                    finding="S1",
                    decision=decision,
                    duplicate_of=duplicate_of,
                    reason="x",
                    evidence="x",
                )
            ],
            comments=[],
        )

    await execute(
        paths, Config(), store, Reviewer(["SUBSTANTIAL [S1] x\nVERDICT: SUBSTANTIAL_ISSUES"]), turn
    )
    assert store.load()["status"] == "FAILED" and "duplicate" in store.load()["error"]
    assert not github["posted"]


async def test_fix_loop_refuses_duplicates_and_never_sends_titles(paths, tmp_path, monkeypatch):
    monkeypatch.setattr("lxreview.worker.Repository", Repo)
    store = create(paths, tmp_path)
    reviewer = Reviewer(["SUBSTANTIAL [S1] x\nVERDICT: SUBSTANTIAL_ISSUES"])

    async def turn(*args, **kwargs):
        return Evaluation(
            findings=[
                Decision(
                    finding="S1",
                    decision="DUPLICATE",
                    duplicate_of="P1-S1",
                    reason="x",
                    evidence="x",
                )
            ]
        )

    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "FAILED" and "duplicate" in store.load()["error"]
    assert all(r.known_findings == [] for r in reviewer.requests)


@pytest.mark.parametrize("failure", ["post", "head", "checkpoint", "missing"])
async def test_later_pass_failure_keeps_previous_checkpoint(
    paths, tmp_path, github, monkeypatch, failure
):
    store = review_only(paths, tmp_path, 3)

    class Later(Reviewer):
        async def review(self, request):
            if self.requests:
                if failure == "head":
                    github["head"] = "b" * 40
            return await super().review(request)

    reviewer = Later(["SUBSTANTIAL [S1] first\nVERDICT: SUBSTANTIAL_ISSUES", "VERDICT: CLEAN"])
    original = store.update

    def update(**changes):
        result = original(**changes)
        if changes.get("completed_pass") == 1:
            if failure == "checkpoint":
                monkeypatch.setattr(Repo, "head_value", "b" * 40)
                monkeypatch.setattr(Drafting, "pr_head", "b" * 40)
            if failure == "missing":
                (Path(result["audit"]) / "pass-01/comments.json").unlink()
        return result

    monkeypatch.setattr(store, "update", update)
    if failure == "post":

        def fail(*args):
            raise LXError(Category.UNAVAILABLE, "Posting failed")

        monkeypatch.setattr("lxreview.comments.post", fail)
    await execute(
        paths,
        Config(),
        store,
        reviewer,
        reviewing(
            {"S1": "ACCEPTED"}, [Comment(finding="S1", path="src/parse.py", line=12, body="Fix")]
        ),
    )
    assert store.load()["status"] == "FAILED" and store.load()["completed_pass"] == 1
    assert not github["posted"]
    if failure == "checkpoint":
        assert "The PR head changed between passes" in store.load()["error"]


def test_completed_findings_ignore_interrupted_and_sanitize_titles(tmp_path):
    from lxreview.worker import completed_findings, known_titles

    directory = tmp_path / "pass-01"
    directory.mkdir()
    (directory / "reviewer.md").write_text(
        "SUBSTANTIAL [S1]\nbody fallback\nSUBSTANTIAL [S2]\nVERDICT: SUBSTANTIAL_ISSUES"
    )
    findings = [
        {"key": f"P1-S{i}", "id": f"S{i}", "title": title}
        for i, title in enumerate(["", "", " a\n b|\x00c ", "x" * 200, "a bc"], 1)
    ]
    (directory / "comments.json").write_text(
        json.dumps({"head": "a", "pass": 1, "findings": findings})
    )
    interrupted = tmp_path / "pass-02-interrupted-abc"
    interrupted.mkdir()
    (interrupted / "comments.json").write_text("invalid")
    titles = known_titles(completed_findings(tmp_path, 2, "a"))
    assert titles == ["body fallback", "finding S2 of pass 1 (untitled)", "a bc", "x" * 160]
    with pytest.raises(LXError):
        completed_findings(tmp_path, 2, "b")


async def test_title_cap_finalizes_without_losing_comments(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    raw = (
        "\n".join(f"SUBSTANTIAL [S{i}] problem {i}" for i in range(1, 62))
        + "\nVERDICT: SUBSTANTIAL_ISSUES"
    )
    await execute(
        paths,
        Config(),
        store,
        Reviewer([raw]),
        reviewing(
            {f"S{i}": "ACCEPTED" for i in range(1, 62)},
            [
                Comment(finding=f"S{i}", path="src/parse.py", line=12, body=f"Fix {i}")
                for i in range(1, 62)
            ],
        ),
    )
    assert store.load()["status"] == "REVIEW_DRAFTED" and store.load()["completed_pass"] == 1
    assert len(github["posted"][0]["comments"]) == 61
    assert "More than 60 earlier findings" in store.load()["error"]
    events = [
        json.loads(line) for line in (store.directory / "events.jsonl").read_text().splitlines()
    ]
    summary = next(e for e in events if e["kind"] == "review_pass_summary")
    assert summary["title_limit_reached"] and not summary["limit_reached"]


async def test_ignored_non_blocking_findings_are_not_carried_forward(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    store.update(substantial_only=True)
    reviewer = Reviewer(
        [
            "SUBSTANTIAL [S1] off by one\nNON_BLOCKING [N1] unclear name\n"
            "VERDICT: SUBSTANTIAL_ISSUES",
            # The ignored point returns as substantial; it is new, not a duplicate.
            "SUBSTANTIAL [S1] unclear name\nVERDICT: SUBSTANTIAL_ISSUES",
            "VERDICT: CLEAN",
        ]
    )
    prompts: list[str] = []
    turn = reviewing(
        {"S1": "ACCEPTED"},
        [Comment(finding="S1", path="src/parse.py", line=12, body="Fix")],
        prompts,
    )
    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert [r.known_findings for r in reviewer.requests] == [
        [],
        ["off by one"],
        ["off by one", "unclear name"],
    ]
    assert "P1-N1" not in prompts[1]
    assert len(github["posted"][0]["comments"]) == 2


async def test_resume_uses_completed_findings_and_ignores_interrupted(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    audit = Path(store.load()["audit"])
    first = audit / "pass-01"
    first.mkdir(parents=True)
    (first / "reviewer.md").write_text("SUBSTANTIAL [S1] original\nVERDICT: SUBSTANTIAL_ISSUES")
    comment = Comment(finding="S1", path="src/parse.py", line=12, body="Retained")
    (first / "comments.json").write_text(
        json.dumps(
            {
                "head": "a" * 40,
                "pass": 1,
                "findings": [
                    {
                        "id": "S1",
                        "key": "P1-S1",
                        "title": "original",
                        "classification": "SUBSTANTIAL",
                        "decision": "ACCEPTED",
                        "duplicate_of": None,
                        "comment": comment.model_dump(),
                    }
                ],
            }
        )
    )
    interrupted = audit / "pass-02-interrupted-abc"
    interrupted.mkdir()
    (interrupted / "comments.json").write_text("invalid")
    store.update(completed_pass=1)
    reviewer = Reviewer(["VERDICT: CLEAN"])
    await execute(paths, Config(), store, reviewer)
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert reviewer.requests[0].known_findings == ["original"]
    assert github["posted"][0]["comments"][0]["body"] == "Retained"


async def test_non_blocking_acceptance_does_not_start_another_pass(paths, tmp_path, github):
    store = review_only(paths, tmp_path, 3)
    reviewer = Reviewer(["NON_BLOCKING [N1] name\nVERDICT: CLEAN"])
    await execute(
        paths,
        Config(),
        store,
        reviewer,
        reviewing(
            {"N1": "ACCEPTED"}, [Comment(finding="N1", path="src/parse.py", line=12, body="Rename")]
        ),
    )
    assert store.load()["status"] == "REVIEW_DRAFTED"
    assert len(reviewer.requests) == 1
