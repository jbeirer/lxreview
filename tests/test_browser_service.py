import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from lxreview.browser.playwright import LOGIN_REQUIRED, divergence
from lxreview.browser.service import NOT_RUNNING, ServiceSession, serve
from lxreview.config import Config
from lxreview.errors import Category, LXError
from lxreview.paths import Paths


class LoggedOut:
    async def ensure_ready(self):
        raise LXError(Category.AUTH, LOGIN_REQUIRED)

    async def sessions(self):
        return []

    async def shutdown(self):
        pass


@pytest.fixture
def short_paths():
    # A short root keeps the unix socket path within the macOS sockaddr limit.
    root = Path(tempfile.mkdtemp(prefix="lx", dir="/tmp"))
    paths = Paths(root / "root")
    paths.ensure()
    yield paths
    shutil.rmtree(root)


async def test_client_reports_a_stopped_service(paths):
    with pytest.raises(LXError) as caught:
        await ServiceSession(paths).sessions()
    assert caught.value.category == Category.UNAVAILABLE
    assert str(caught.value) == NOT_RUNNING


async def test_client_passes_through_the_session_error(short_paths):
    service = asyncio.create_task(serve(short_paths, Config(), {}, LoggedOut()))
    try:
        for _ in range(100):
            if (short_paths.root / "state/browser/connection.json").exists():
                break
            await asyncio.sleep(0.05)
        with pytest.raises(LXError) as caught:
            await ServiceSession(short_paths).ensure_ready()
        assert caught.value.category == Category.AUTH
        assert str(caught.value) == LOGIN_REQUIRED
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)
    assert not (short_paths.root / "state/browser/connection.json").exists()


async def test_overlong_root_is_refused_before_launching_chrome(tmp_path):
    paths = Paths(tmp_path / ("x" * 90))
    paths.ensure()
    with pytest.raises(LXError) as caught:
        await serve(paths, Config(), {}, LoggedOut())
    assert caught.value.category == Category.CONFIG


def test_readback_divergence_points_at_the_first_difference():
    prompt = "Review https://github.com/o/r/pull/4 at abc | Do not rely on Claude's notes."
    typed = prompt.replace("Claude's", "Claude’s")
    assert divergence(prompt, typed) == (
        f"{len(typed)} of {len(prompt)} characters, first difference at {prompt.index("'")}: "
        "expected \"o not rely on Claude's notes.\", composer has 'o not rely on Claude’s notes.'"
    )
    assert "first difference at 10" in divergence(prompt, prompt[:10])
    assert divergence(prompt, None) == "composer disappeared"
