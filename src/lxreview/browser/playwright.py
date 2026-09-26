"""Experimental benchmark-only ChatGPT adapter. Not selectable as production backend."""

import asyncio
import time
from pathlib import Path
from typing import Any

from ..contracts import Health
from ..errors import Category, LXError


class PlaywrightSession:
    def __init__(self, profile: Path, chrome: Path):
        self.profile, self.chrome = profile, chrome
        self.context: Any = None
        self.page: Any = None
        self.driver: Any = None

    async def open(self):
        from playwright.async_api import async_playwright

        self.driver = await async_playwright().start()
        self.context = await self.driver.chromium.launch_persistent_context(
            str(self.profile),
            executable_path=str(self.chrome),
            headless=False,
            chromium_sandbox=True,
        )
        pages = self.context.pages
        if len(pages) > 1:
            raise LXError(
                Category.UNSAFE, "Experimental profile contains multiple pages; refusing takeover"
            )
        self.page = pages[0] if pages else await self.context.new_page()

    async def new_conversation(self) -> None:
        if self.page is None:
            await self.open()
        await self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)

    async def ensure_ready(self) -> None:
        try:
            await self.page.locator("#prompt-textarea").wait_for(state="visible", timeout=5000)
        except Exception as exc:
            raise LXError(Category.AUTH, "Complete login in the experimental browser") from exc

    async def sessions(self) -> list[dict]:
        return (
            [{"key": "chatgpt-reviewer", "url": p.url} for p in self.context.pages]
            if self.context
            else []
        )

    async def health(self) -> Health:
        await self.ensure_ready()
        return Health(
            ready=True, detail="Experimental composer visible; live compatibility not certified"
        )

    async def query(self, prompt: str, timeout: float) -> str:
        await self.ensure_ready()
        composer = self.page.locator("#prompt-textarea")
        turns = self.page.locator('[data-message-author-role="assistant"]')
        before = await turns.count()
        await composer.fill(prompt)
        actual = await composer.inner_text()
        if actual.replace("\r\n", "\n") != prompt.replace("\r\n", "\n"):
            raise LXError(Category.PROTOCOL, "Atomic prompt readback differs; submission refused")
        # Exactly one submit; ambiguous timeout is never retried.
        await self.page.locator('[data-testid="send-button"]').click(timeout=5000)
        start = time.monotonic()
        previous, stable_since = "", time.monotonic()
        while time.monotonic() - start < timeout:
            generating = await self.page.locator('[data-testid="stop-button"]').count() > 0
            if await turns.count() > before:
                last = turns.last
                text = await last.inner_text()
                # A completed turn must expose its response action bar, not merely stable thinking text.
                finished = (
                    await last.locator("xpath=ancestor::article")
                    .locator('[data-testid="copy-turn-action-button"]')
                    .count()
                    > 0
                )
                if text != previous:
                    previous, stable_since = text, time.monotonic()
                elif text and finished and not generating and time.monotonic() - stable_since >= 3:
                    return text
            await asyncio.sleep(0.5)
        raise LXError(Category.TIMEOUT, "Experimental response completion deadline exceeded")

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.driver:
            await self.driver.stop()
        self.context = self.page = self.driver = None

    async def recover(self) -> None:
        await self.close()
        await self.new_conversation()
        await self.ensure_ready()
