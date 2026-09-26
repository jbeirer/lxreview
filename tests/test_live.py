"""Never logs into a browser or runs a real query without explicit opt-in."""

import os

import pytest

from lxreview.backend import browser
from lxreview.config import Config
from lxreview.paths import Paths, lock

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LXREVIEW_LIVE_TESTS") != "1",
        reason="human login and explicit live opt-in required",
    ),
]


async def test_live_two_fresh_passes_reuse_session():
    paths = Paths.default()
    config = Config.load(paths)
    session = browser(paths, config)
    with lock(paths.root / "state/reviewer.lock"):
        await session.new_conversation()
        await session.ensure_ready()
        before = await session.sessions()
        for _ in range(2):
            await session.new_conversation()
            await session.ensure_ready()
            result = await session.query("Reply exactly LXREVIEW_LIVE_OK", 120)
            assert result.strip() == "LXREVIEW_LIVE_OK"
            assert await session.sessions() == before
