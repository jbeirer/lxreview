import pytest

from lxreview.paths import Paths

DISCUSSION = "### Description by @author\nKeeps the retry loop on purpose."


@pytest.fixture
def paths(tmp_path):
    value = Paths(tmp_path / "lxreview")
    value.ensure()
    return value


@pytest.fixture(autouse=True)
def pr_discussion(request, monkeypatch):
    # No test may reach GitHub; tests marked github exercise fetch with a fake gh.
    if "github" not in request.keywords:
        counts = {"comments": 0, "reviews": 0, "threads": 0, "unresolved": 0}
        monkeypatch.setattr("lxreview.discussion.fetch", lambda *a: (DISCUSSION, counts))
