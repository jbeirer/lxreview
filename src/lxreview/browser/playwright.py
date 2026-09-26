"""ChatGPT driven through Playwright and the pinned Chrome.

Chrome runs over Playwright's private pipe, so no debugging port is opened. All page
inspection goes through one script, PAGE_STATE, so the selectors can be checked against
saved page fixtures without a ChatGPT account.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from ..contracts import Health
from ..errors import Category, LXError

KEY = "chatgpt-reviewer"
# Temporary Chat keeps ChatGPT memory and history out of every independent review.
HOME = "https://chatgpt.com/?temporary-chat=true"
LOGIN_REQUIRED = "Human browser login or CAPTCHA completion required; run lxreview login"

SELECTORS = {
    "composer": 'div.ProseMirror[contenteditable="true"]',
    "send": 'button[aria-label="Send"]',
    "stop": 'button[aria-label*="stop" i], button[data-testid*="stop" i]',
    "reply": '[data-markdown-text-style="assistant-message"]',
    "user_message": "[data-user-message-bubble]",
    "turn": "[data-turn-key]",
    # ChatGPT renders a reply's action bar only once the reply is complete.
    "reply_actions": 'button[aria-label="Copy"], button[aria-label="Regenerate response"]',
    "account": 'button[aria-label="Open profile menu"]',
    "login": '[data-testid="login-button"], [data-testid="signup-button"], a[href*="/auth/login"], input[type="password"]',
    "captcha": 'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile" i], iframe[src*="arkose" i], #challenge-form',
}

PAGE_STATE = r"""(s) => {
  const visible = (n) => {
    const r = n.getBoundingClientRect();
    const style = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const outermost = (nodes) => nodes.filter((n) => !nodes.some((o) => o !== n && o.contains(n)));
  const composer = all(s.composer).find(visible) || null;
  const root = (composer && composer.closest('form, [data-chatgpt-composer]')) || document;
  const send = all(s.send, root).find(visible) || null;
  const replies = outermost(all(s.reply));
  const last = replies[replies.length - 1] || null;
  const turn = last ? last.closest(s.turn) : null;
  // A reply can span several blocks; keep every block of the last exchange.
  const blocks = turn ? outermost(all(s.reply, turn)) : last ? [last] : [];
  const reply = blocks.map((n) => n.innerText.trim()).filter(Boolean).join('\n\n');
  const turnText = turn ? turn.innerText : '';
  const buttonText = all('button, a').filter(visible).map((b) => (b.textContent || '').trim());
  const page = document.title + ' ' + (document.body ? document.body.innerText.slice(0, 3000) : '');
  return {
    url: location.href,
    composer: !!composer,
    prompt: composer ? composer.innerText.trim() : null,
    send: send ? (send.disabled || send.getAttribute('aria-disabled') === 'true' ? 'disabled' : 'enabled') : null,
    generating: all(s.stop, root).some(visible),
    replies: replies.length,
    reply: reply || null,
    finished: !!turn && all(s.reply_actions, turn).length > 0,
    failed: !!turn && /something went wrong|error generating|network error/i.test(turnText.replace(reply, '')),
    user_messages: outermost(all(s.user_message)).length,
    account: all(s.account).some(visible),
    login: all(s.login).some(visible) || buttonText.some((t) => /^(log in|sign up|sign up for free)$/i.test(t)),
    captcha: all(s.captcha).length > 0 || /verify you are human|just a moment\.\.\./i.test(page),
  };
}"""


def divergence(expected: str, actual: str | None) -> str:
    """Where the composer text first departs from the prompt, for the readback error."""
    if actual is None:
        return "composer disappeared"
    at = next(
        (i for i, (a, b) in enumerate(zip(expected, actual, strict=False)) if a != b),
        min(len(expected), len(actual)),
    )
    start = max(0, at - 20)
    return (
        f"{len(actual)} of {len(expected)} characters, first difference at {at}: "
        f"expected {expected[start : at + 20]!r}, composer has {actual[start : at + 20]!r}"
    )


def validity(expires: float) -> str:
    days = int((expires - time.time()) // 86400)
    when = time.strftime("%Y-%m-%d", time.localtime(expires))
    return f"valid until {when} ({days} day{'s' if days != 1 else ''})"


class PlaywrightSession:
    """One ChatGPT tab in an LXReview-owned Chrome; the browser service serializes calls."""

    def __init__(
        self, profile: Path, chrome: str, *, home: str = HOME, headless=False, sandbox=True
    ):
        # headless and sandbox exist for the fixture tests; the service uses the defaults.
        self.profile, self.chrome, self.home = profile, chrome, home
        self.headless, self.sandbox = headless, sandbox
        self.driver: Any = None
        self.context: Any = None
        self.page: Any = None

    async def open(self) -> None:
        from playwright.async_api import async_playwright

        if self.driver is None:
            self.driver = await async_playwright().start()
        self.context = await self.driver.chromium.launch_persistent_context(
            str(self.profile),
            executable_path=self.chrome,
            headless=self.headless,
            chromium_sandbox=self.sandbox,
            no_viewport=True,
            # No automation banner or webdriver flag. macOS keeps the login in the real
            # Keychain; Linux keeps Playwright's basic store so no keyring dialog can block
            # Chrome inside the virtual desktop (the profile lives in the private root).
            ignore_default_args=["--enable-automation", "--use-mock-keychain"],
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
        )
        self.context.on("close", lambda _: self._forget())
        pages = self.context.pages
        blank = [p for p in pages if p.url in ("about:blank", "chrome://newtab/")]
        self.page = blank[0] if len(pages) == 1 and blank else await self.context.new_page()

    def _forget(self) -> None:
        self.context = self.page = None

    async def _page(self) -> Any:
        # The human may close the tab or the whole window in the desktop; reopen on demand.
        if self.context is None:
            await self.open()
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
        return self.page

    async def state(self) -> dict:
        page = await self._page()
        return await page.evaluate(PAGE_STATE, SELECTORS)

    async def new_conversation(self) -> None:
        page = await self._page()
        state = await self.state()
        if state["generating"]:
            raise LXError(Category.BUSY, "ChatGPT is still generating; navigation refused")
        await page.goto(self.home, wait_until="domcontentloaded", timeout=30000)

    async def ensure_ready(self) -> None:
        deadline = time.monotonic() + 15
        loaded = None
        while True:
            state = await self.state()
            if state["captcha"] or state["login"]:
                raise LXError(Category.AUTH, LOGIN_REQUIRED)
            if state["composer"] and state["account"]:
                return
            # ChatGPT also serves a composer to logged-out visitors; give the account menu
            # a moment to render before calling the page logged out.
            if state["composer"]:
                loaded = loaded or time.monotonic()
                if time.monotonic() - loaded >= 5:
                    raise LXError(Category.AUTH, LOGIN_REQUIRED)
            if time.monotonic() >= deadline:
                raise LXError(
                    Category.AUTH, "ChatGPT composer did not appear; check the browser window"
                )
            await asyncio.sleep(0.5)

    async def query(self, prompt: str, timeout: float) -> str:
        end = time.monotonic() + timeout
        # One physical line, matching the prompt the reviewer audits.
        prompt = " | ".join(prompt.splitlines())
        await self.ensure_ready()
        page = await self._page()
        before = await self.state()
        if before["generating"]:
            raise LXError(Category.BUSY, "ChatGPT is still generating; submission refused")
        composer = page.locator(SELECTORS["composer"]).locator("visible=true").first
        await composer.fill(prompt, timeout=10000)
        # The editor may apply a long insertion after fill() returns; allow it to settle.
        settled = time.monotonic() + 2
        while (typed := (await self.state())["prompt"]) != prompt.strip():
            if time.monotonic() >= settled:
                raise LXError(
                    Category.PROTOCOL,
                    "Prompt readback differs; submission refused ("
                    + divergence(prompt.strip(), typed)
                    + ")",
                )
            await asyncio.sleep(0.25)
        # An enabled Send button shows ChatGPT has registered the typed prompt.
        ready = time.monotonic() + 10
        while (await self.state())["send"] != "enabled":
            if time.monotonic() >= ready:
                raise LXError(
                    Category.PROTOCOL, "ChatGPT did not accept the typed prompt; nothing was sent"
                )
            await asyncio.sleep(0.25)
        # Enter in the focused composer, not a click: ChatGPT swaps the Send button as soon as
        # it fires, which made click retries report failure after the prompt had been sent.
        # From here on only the prompt appearing in the chat decides the outcome.
        await composer.press("Enter", no_wait_after=True)
        accepted = time.monotonic() + 15
        while (await self.state())["user_messages"] <= before["user_messages"]:
            if time.monotonic() >= accepted:
                raise LXError(
                    Category.TIMEOUT,
                    "ChatGPT did not show the prompt; it may still have been sent, so it was not retried",
                )
            await asyncio.sleep(0.25)
        previous, stable_since = None, time.monotonic()
        while time.monotonic() < end:
            state = await self.state()
            if state["failed"]:
                raise LXError(Category.PROTOCOL, "ChatGPT reported an error instead of a reply")
            if state["captcha"]:
                raise LXError(Category.AUTH, LOGIN_REQUIRED)
            if state["replies"] > before["replies"] and state["reply"]:
                if state["reply"] != previous:
                    previous, stable_since = state["reply"], time.monotonic()
                elif (
                    state["finished"]
                    and not state["generating"]
                    and time.monotonic() - stable_since >= 2
                ):
                    return state["reply"]
            await asyncio.sleep(0.5)
        raise LXError(
            Category.TIMEOUT, "ChatGPT reply did not finish before the deadline; it was not retried"
        )

    async def health(self) -> Health:
        # A freshly started service shows a blank tab; judge the login on ChatGPT itself.
        if not (await self._page()).url.startswith(self.home.split("?")[0]):
            await self.new_conversation()
        await self.ensure_ready()
        expires = await self.session_expires()
        detail = "ChatGPT logged in and composer ready"
        if expires:
            detail += "; session " + validity(expires)
        return Health(ready=True, detail=detail, metadata={"session_expires": expires})

    async def session_expires(self) -> float | None:
        """When ChatGPT's session cookie expires; only the expiry is read, never the value."""
        cookies = await self.context.cookies("https://chatgpt.com")
        times = [
            c["expires"]
            for c in cookies
            if c["name"].startswith("__Secure-next-auth.session-token") and c["expires"] > 0
        ]
        return min(times) if times else None

    async def sessions(self) -> list[dict]:
        if self.page is None or self.page.is_closed():
            return []
        return [{"key": KEY, "vendorId": "chatgpt", "url": self.page.url}]

    async def close(self) -> None:
        if self.page is not None and not self.page.is_closed():
            await self.page.close()
        self.page = None

    async def recover(self) -> None:
        await self.close()
        await self.new_conversation()
        await self.ensure_ready()

    async def shutdown(self) -> None:
        if self.context is not None:
            await self.context.close()
        if self.driver is not None:
            await self.driver.stop()
        self.driver = self.context = self.page = None
