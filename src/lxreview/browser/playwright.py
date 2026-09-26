"""ChatGPT driven through Playwright and the pinned Chrome.

Chrome runs over Playwright's private pipe, so no debugging port is opened. All page
inspection goes through one script, PAGE_STATE, so the selectors can be checked against
saved page fixtures without a ChatGPT account.
"""

import asyncio
import re
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
    # Model picker next to the composer (observed 2026-09-26): a menu with a reasoning slider
    # ("Power", arrow keys, status "Medium, 2 of 3.") and, behind a view toggle, the models.
    "picker": 'button[aria-label="Select ChatGPT model"]',
    "picker_menu": '[role="menu"]',
    "effort_slider": "[data-reasoning-slider]",
    "model_view": "[data-model-picker-view-toggle]",
    "model_option": '[role="menuitemradio"]',
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
  // The editor's document text, not its rendering: ChatGPT decorates GitHub URLs with an
  // icon widget that innerText turns into a line break. Paragraphs and hard breaks still
  // count as line breaks, so a prompt the editor actually split never reads back equal.
  const documentText = (editor) => {
    const copy = editor.cloneNode(true);
    copy.querySelectorAll('.ProseMirror-widget').forEach((n) => n.remove());
    copy.querySelectorAll('br:not(.ProseMirror-trailingBreak)').forEach((n) => n.replaceWith('\n'));
    const lines = [];
    let inline = null;
    for (const n of copy.childNodes) {
      if (n.nodeType === 1 && /^(P|DIV|PRE|H[1-6]|UL|OL|BLOCKQUOTE)$/.test(n.tagName)) {
        if (inline !== null) lines.push(inline);
        lines.push(n.textContent);
        inline = null;
      } else {
        inline = (inline || '') + n.textContent;
      }
    }
    if (inline !== null) lines.push(inline);
    return lines.join('\n');
  };
  return {
    url: location.href,
    composer: !!composer,
    prompt: composer ? documentText(composer).trim() : null,
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

    async def configure(self, model: str, reasoning_effort: str) -> dict:
        """Choose the model and reasoning level in the fresh chat, then read both back.

        "default" leaves a choice untouched. A choice ChatGPT does not offer, or one that
        does not stick, refuses the review before any prompt is typed.
        """
        page = await self._page()
        picker = page.locator(SELECTORS["picker"]).locator("visible=true").first
        chosen: dict = {}
        value = None
        # The level first: a fresh chat opens the picker on the slider, and within a chat
        # the picker reopens on the view last used, while the model list has no way back.
        if reasoning_effort != "default":
            picker = await self._open_view(page, picker, "simple")
            slider = page.locator(SELECTORS["effort_slider"]).first
            await slider.focus()
            level = await self._effort(page, slider)
            start, levels = level, {level[1]: level[0]}
            # Walk to the lowest level, then up until the wanted one appears.
            while level[1] > 1:
                moved = await self._step(page, slider, "ArrowLeft", level)
                if moved == level:
                    raise LXError(Category.PROTOCOL, "ChatGPT's reasoning slider did not move")
                level = moved
                levels[level[1]] = level[0]
            while level[0].lower() != reasoning_effort.lower() and level[1] < level[2]:
                moved = await self._step(page, slider, "ArrowRight", level)
                if moved == level:
                    break
                level = moved
                levels[level[1]] = level[0]
            if level[0].lower() != reasoning_effort.lower():
                # Leave the user's level as it was.
                while level[1] > start[1]:
                    moved = await self._step(page, slider, "ArrowLeft", level)
                    if moved == level:
                        break
                    level = moved
                await self._close_picker(page)
                offered = ", ".join(levels[i].lower() for i in sorted(levels))
                raise LXError(
                    Category.CONFIG,
                    f"ChatGPT offers no reasoning level {reasoning_effort!r} here; available: {offered}",
                )
            await self._close_picker(page)
            # Read the level back from a freshly opened picker.
            picker = await self._open_view(page, picker, "simple")
            actual = await self._effort(page, page.locator(SELECTORS["effort_slider"]).first)
            value = await picker.get_attribute("data-selected-reasoning-effort")
            await self._close_picker(page)
            if actual[0].lower() != reasoning_effort.lower():
                raise LXError(Category.PROTOCOL, "ChatGPT did not keep the reasoning level")
            chosen["chatgpt_reasoning"] = actual[0].lower()
        if model != "default":
            picker = await self._open_view(page, picker, "advanced")
            options = page.locator(SELECTORS["picker_menu"]).locator(SELECTORS["model_option"])
            # The name is the option's first span; a note such as "Leaving on October 14"
            # follows in its own span (innerText joins them depending on layout).
            names = [
                (await options.nth(i).locator("span").first.inner_text()).strip()
                for i in range(await options.count())
            ]
            wanted = [i for i, name in enumerate(names) if name.lower() == model.lower()]
            if not wanted:
                await self._close_picker(page)
                raise LXError(
                    Category.CONFIG,
                    f"ChatGPT offers no model named {model!r} here; available: {', '.join(names)}",
                )
            option = options.nth(wanted[0])
            if await option.get_attribute("aria-checked") != "true":
                await option.click()
            await self._close_picker(page)
            picker = await self._open_view(page, picker, "advanced")
            checked = page.locator(SELECTORS["picker_menu"]).locator(
                f'{SELECTORS["model_option"]}[aria-checked="true"]'
            )
            actual_model = (
                (await checked.first.locator("span").first.inner_text()).strip()
                if await checked.count()
                else ""
            )
            await self._close_picker(page)
            if actual_model.lower() != model.lower():
                raise LXError(
                    Category.PROTOCOL, f"ChatGPT did not switch to {model}; review refused"
                )
            chosen["chatgpt_model"] = actual_model
        current = await picker.get_attribute("data-selected-reasoning-effort")
        if value is not None and current != value:
            raise LXError(
                Category.PROTOCOL,
                "ChatGPT changed the reasoning level with the model; review refused",
            )
        if "chatgpt_reasoning" not in chosen and current:
            chosen["chatgpt_reasoning"] = current
        return chosen

    async def options(self) -> dict:
        """The models and reasoning levels ChatGPT offers, and the current ones.

        The slider shows only its current level's name, so this steps through the levels
        and puts it back where it was. Nothing is selected and nothing is typed.
        """
        await self.new_conversation()
        await self.ensure_ready()
        page = await self._page()
        picker = await self._open_view(
            page, page.locator(SELECTORS["picker"]).locator("visible=true").first, "simple"
        )
        slider = page.locator(SELECTORS["effort_slider"]).first
        await slider.focus()
        level = start = await self._effort(page, slider)
        levels = {level[1]: level[0]}
        for key in ("ArrowLeft", "ArrowRight"):
            while 1 < level[1] if key == "ArrowLeft" else level[1] < level[2]:
                moved = await self._step(page, slider, key, level)
                if moved == level:
                    break
                level = moved
                levels[level[1]] = level[0]
        while level[1] != start[1]:
            moved = await self._step(
                page, slider, "ArrowLeft" if level[1] > start[1] else "ArrowRight", level
            )
            if moved == level:
                raise LXError(Category.PROTOCOL, "ChatGPT's reasoning level could not be restored")
            level = moved
        await self._close_picker(page)
        picker = await self._open_view(page, picker, "advanced")
        options = page.locator(SELECTORS["picker_menu"]).locator(SELECTORS["model_option"])
        models, current = [], ""
        for i in range(await options.count()):
            name = (await options.nth(i).locator("span").first.inner_text()).strip()
            models.append(name)
            if await options.nth(i).get_attribute("aria-checked") == "true":
                current = name
        await self._close_picker(page)
        return {
            "models": models,
            "model": current,
            "reasoning": [levels[i].lower() for i in sorted(levels)],
            "reasoning_effort": start[0].lower(),
        }

    async def _open_view(self, page: Any, picker: Any, view: str) -> Any:
        """Open the picker on the slider ("simple") or the model list ("advanced")."""
        await self._open_picker(page, picker)
        views = page.locator(SELECTORS["picker_menu"]).locator("[data-model-picker-view]")
        current = await views.first.get_attribute("data-model-picker-view")
        if current == view:
            return picker
        if view == "advanced":
            await page.locator(SELECTORS["model_view"]).first.click()
            for _ in range(30):
                if await views.first.get_attribute("data-model-picker-view") == "advanced":
                    return picker
                await page.wait_for_timeout(100)
            raise LXError(Category.PROTOCOL, "ChatGPT's model list did not open")
        # Only a fresh chat reopens the picker on the slider.
        await self._close_picker(page)
        await self.new_conversation()
        await self.ensure_ready()
        picker = page.locator(SELECTORS["picker"]).locator("visible=true").first
        await self._open_picker(page, picker)
        if await views.first.get_attribute("data-model-picker-view") != "simple":
            await self._close_picker(page)
            raise LXError(Category.PROTOCOL, "ChatGPT's reasoning slider is not reachable")
        return picker

    async def _open_picker(self, page: Any, picker: Any) -> None:
        await picker.click()
        await page.locator(SELECTORS["picker_menu"]).first.wait_for(state="visible", timeout=5000)

    async def _close_picker(self, page: Any) -> None:
        for _ in range(3):
            if not await page.locator(SELECTORS["picker_menu"]).locator("visible=true").count():
                return
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        raise LXError(Category.PROTOCOL, "ChatGPT's model picker did not close")

    async def _step(
        self, page: Any, slider: Any, key: str, level: tuple[str, int, int]
    ) -> tuple[str, int, int]:
        """Move the slider one level; an unchanged status after a moment means the end."""
        await page.keyboard.press(key)
        for _ in range(15):
            moved = await self._effort(page, slider)
            if moved[1] != level[1]:
                return moved
            await page.wait_for_timeout(100)
        return level

    async def _effort(self, page: Any, slider: Any) -> tuple[str, int, int]:
        """The reasoning level the slider shows, from its status line ("High, 3 of 3.")."""
        described = (await slider.get_attribute("aria-describedby") or "").split()
        for _ in range(20):
            for identifier in described:
                status = page.locator(f'[id="{identifier}"]')
                if await status.count():
                    match = re.fullmatch(
                        r"(.+?), (\d+) of (\d+)\.?", (await status.first.inner_text()).strip()
                    )
                    if match:
                        return match.group(1), int(match.group(2)), int(match.group(3))
            await page.wait_for_timeout(100)
        raise LXError(Category.PROTOCOL, "ChatGPT's reasoning level could not be read")

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
