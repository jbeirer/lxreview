import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from conftest import DISCUSSION

from lxreview.config import Config
from lxreview.contracts import parse_response
from lxreview.errors import LXError
from lxreview.runs import RunStore
from lxreview.worker import Decision, EditResult, Evaluation, PublishResult, execute


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

    fingerprint = "checked tree"

    def worktree_fingerprint(self):
        return self.fingerprint

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


async def test_a_commit_holds_exactly_the_checked_change(paths, tmp_path, waiting, monkeypatch):
    store = create(paths, tmp_path)
    prompts = []
    reviewer = Reviewer(["SUBSTANTIAL [S1] bug\nVERDICT: SUBSTANTIAL_ISSUES"])
    config = Config().with_choices({"commit": "ask"})
    run = asyncio.create_task(
        execute(paths, config, store, reviewer, fixing_turn(lambda i: "ACCEPTED", prompts))
    )
    await approval_requested(store, "commit")
    monkeypatch.setattr(Repo, "fingerprint", "edited while waiting")
    store.approve("commit")
    await run
    state = store.load()
    assert state["status"] == "FAILED"
    assert "working tree changed while waiting for approval" in state["error"]
    assert len(prompts) == 2 and not PushRecorder.pushed
