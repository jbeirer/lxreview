"""Drive the real Playwright session against a local page that mirrors ChatGPT's markup.

Opt in with LXREVIEW_BROWSER_TESTS=1. Set LXREVIEW_TEST_CHROME to a Chrome or Chromium
executable, or install Playwright's Chromium (`playwright install chromium`).
"""

import os
import time
from pathlib import Path

import pytest

from lxreview.browser.playwright import LOGIN_REQUIRED, PlaywrightSession
from lxreview.errors import Category, LXError

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("LXREVIEW_BROWSER_TESTS") != "1",
        reason="needs a Chromium; opt in with LXREVIEW_BROWSER_TESTS=1",
    ),
]

PAGE = (Path(__file__).parent / "fixtures/chatgpt.html").as_uri()


async def executable() -> str:
    if os.environ.get("LXREVIEW_TEST_CHROME"):
        return os.environ["LXREVIEW_TEST_CHROME"]
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        return driver.chromium.executable_path


@pytest.fixture
async def chatgpt(tmp_path):
    sessions = []

    async def start(scenario="normal", navigate=True):
        session = PlaywrightSession(
            tmp_path / f"profile-{len(sessions)}",
            await executable(),
            home=f"{PAGE}?scenario={scenario}",
            headless=True,
            sandbox=False,
        )
        sessions.append(session)
        await session.open()
        if navigate:
            await session.new_conversation()
        return session

    yield start
    for session in sessions:
        await session.shutdown()


async def test_reply_is_returned_only_once_finished(chatgpt):
    session = await chatgpt()
    await session.ensure_ready()
    # The page pauses mid-reply for 2.5 s; stable text alone must not end the wait.
    reply = await session.query("please review this change", 30)
    assert reply == "You said: please review this change"


async def test_prompt_is_sent_as_one_line(chatgpt):
    session = await chatgpt()
    reply = await session.query("first line\nsecond line", 30)
    assert reply == "You said: first line | second line"
    assert (await session.state())["user_messages"] == 1


async def test_reply_spanning_blocks_is_joined(chatgpt):
    session = await chatgpt("multi")
    reply = await session.query("Reply exactly SUBSTANTIAL", 30)
    assert reply == "SUBSTANTIAL\n\nVERDICT: CLEAN"


@pytest.mark.parametrize("scenario", ["logged-out", "captcha"])
async def test_login_or_captcha_needs_the_human(chatgpt, scenario):
    session = await chatgpt(scenario)
    with pytest.raises(LXError) as caught:
        await session.ensure_ready()
    assert caught.value.category == Category.AUTH
    assert str(caught.value) == LOGIN_REQUIRED


async def test_chatgpt_error_is_not_a_reply(chatgpt):
    session = await chatgpt("error")
    with pytest.raises(LXError) as caught:
        await session.query("Reply exactly NEVER", 30)
    assert caught.value.category == Category.PROTOCOL


async def test_altered_prompt_is_never_sent(chatgpt):
    session = await chatgpt("mangle")
    with pytest.raises(LXError, match="readback"):
        await session.query("Reply exactly lower", 30)
    assert (await session.state())["user_messages"] == 0


async def test_github_rich_link_reads_back_as_the_typed_prompt(chatgpt):
    session = await chatgpt()
    prompt = "Review https://github.com/o/r/pull/4 at abc | Do not rely on Claude's notes."
    assert await session.query(prompt, 30) == "You said: " + prompt


async def test_prompt_is_not_submitted_until_chatgpt_accepts_it(chatgpt):
    session = await chatgpt("stuck")
    with pytest.raises(LXError, match="nothing was sent"):
        await session.query("Reply exactly NEVER", 30)
    assert (await session.state())["user_messages"] == 0


async def test_closed_tab_is_reopened(chatgpt):
    session = await chatgpt()
    await session.close()
    assert await session.sessions() == []
    await session.new_conversation()
    await session.ensure_ready()
    assert len(await session.sessions()) == 1


async def test_health_after_a_fresh_start_checks_chatgpt_itself(chatgpt):
    # The service opens Chrome on a blank tab; the login check must not judge that tab.
    session = await chatgpt(navigate=False)
    expires = time.time() + 30.5 * 86400
    await session.context.add_cookies(
        [
            {
                "name": "__Secure-next-auth.session-token.0",
                "value": "not-read",
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
                "expires": expires,
            }
        ]
    )
    health = await session.health()
    assert health.ready
    assert health.metadata["session_expires"] == pytest.approx(expires, abs=1)
    assert "(30 days)" in health.detail
    assert "not-read" not in health.detail


async def picked(session):
    page = await session._page()
    return await page.get_attribute(
        'button[aria-label="Select ChatGPT model"]', "data-selected-reasoning-effort"
    )


async def test_model_and_reasoning_are_chosen_and_read_back(chatgpt):
    session = await chatgpt()
    assert await session.configure("GPT-5.5", "high") == {
        "chatgpt_model": "GPT-5.5",
        "chatgpt_reasoning": "high",
    }
    assert await picked(session) == "high"
    assert await session.configure("default", "instant") == {"chatgpt_reasoning": "instant"}
    assert await picked(session) == "none"
    # The picker is closed again: the review goes out normally.
    assert await session.query("Reply exactly OK", 30) == "OK"


async def test_default_choices_leave_the_picker_closed(chatgpt):
    session = await chatgpt()
    assert await session.configure("default", "default") == {"chatgpt_reasoning": "medium"}
    page = await session._page()
    assert (
        await page.get_attribute('button[aria-label="Select ChatGPT model"]', "aria-expanded")
        == "false"
    )


@pytest.mark.parametrize(
    ("model", "effort", "message"),
    [
        ("GPT-9", "default", "available: GPT-5.6 Sol, GPT-5.5"),
        ("default", "heavy", "available: instant, medium, high"),
    ],
)
async def test_unavailable_choices_refuse_the_review_and_keep_the_level(
    chatgpt, model, effort, message
):
    session = await chatgpt()
    with pytest.raises(LXError, match=message) as caught:
        await session.configure(model, effort)
    assert caught.value.category == Category.CONFIG
    assert await picked(session) == "medium"
    assert (await session.state())["user_messages"] == 0


async def test_a_slider_that_does_not_move_refuses_the_review(chatgpt):
    session = await chatgpt("frozen-slider")
    with pytest.raises(LXError, match="did not move"):
        await session.configure("default", "instant")
