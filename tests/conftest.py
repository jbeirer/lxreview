import pytest

from lxreview.paths import Paths

DISCUSSION = "### Description by @author\nKeeps the retry loop on purpose."


@pytest.fixture(autouse=True)
def node_local(tmp_path, monkeypatch):
    # Runtime directories fall back into each test's root instead of the real /run/user.
    monkeypatch.setattr("lxreview.paths.RUN_USER", tmp_path / "no-run-user")


@pytest.fixture
def paths(tmp_path):
    value = Paths(tmp_path / "lxreview")
    value.ensure()
    return value


@pytest.fixture(autouse=True)
def pr_discussion(request, monkeypatch):
    # No test may reach GitHub or GitLab; tests marked forge exercise fetch with fakes.
    if "forge" not in request.keywords:
        counts = {"comments": 0, "reviews": 0, "threads": 0, "unresolved": 0}
        monkeypatch.setattr("lxreview.discussion.fetch", lambda *a: (DISCUSSION, counts))
