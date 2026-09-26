from pathlib import Path

import pytest

from lxreview.config import Config
from lxreview.contracts import parse_response
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

    def preflight(self, target):
        return {
            "head": self.head_value,
            "branch": "feature",
            "upstream": "origin/feature",
            "remote_url": "url",
        }

    def head(self):
        return self.head_value

    def call(self, *args):
        return "diff --git a/file b/file\n+fix"


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
            return Evaluation(
                findings=[
                    Decision(
                        finding="S1", decision="ACCEPTED", reason="reproduced", evidence="file:1"
                    )
                ]
            )
        assert (audit / "evaluation.json").exists()
        if schema is EditResult:
            return EditResult(tests=["pytest: 1 passed"], tests_passed=True, summary="fixed")
        Repo.head_value = "b" * 40
        return PublishResult(pushed=True, commit=Repo.head_value)

    await execute(paths, Config(), store, reviewer, turn)
    assert store.load()["status"] == "CLEAN"
    assert calls == [True, False, False]
    assert [r.head_sha for r in reviewer.requests] == ["a" * 40, "b" * 40]
    assert (Path(store.load()["audit"]) / "pass-01/diff.patch").exists()
    assert "CLEAN" in store.report()


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
            return EditResult(tests=["pytest passes"], tests_passed=True, summary="fixed")
        Repo.head_value = "b" * 40
        return PublishResult(commit=Repo.head_value, pushed=False)

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
    assert "every substantial" in store.load()["error"]


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
        return EditResult(tests=["pytest: failed"], tests_passed=False, summary="not fixed")

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
