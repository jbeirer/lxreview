import asyncio

from .contracts import BrowserSessionBackend, Health, ReviewRequest, ReviewResponse, parse_response
from .errors import Category, LXError


def prompt_for(request: ReviewRequest) -> str:
    parts = [
        f"Perform a fresh independent full review of {request.target} at current head commit {request.head_sha}.",
        "Open and inspect the pull request yourself, including the complete current diff and surrounding repository code. Do not rely on Claude's description or on any previous review pass. Verify the head commit matches.",
        f"Look for substantial issues in: {request.rubric}.",
        "For each finding classify it SUBSTANTIAL or NON_BLOCKING, identify file and line or symbol, explain the concrete problem and suggest a concrete fix. Start each finding on its own line as SUBSTANTIAL [S1], SUBSTANTIAL [S2], or NON_BLOCKING [N1], using unique IDs. Subjective style preferences are not SUBSTANTIAL.",
        "If you cannot access or adequately inspect the complete PR at this head, say so and end exactly with VERDICT: ACCESS_FAILED.",
        "Otherwise end exactly with VERDICT: SUBSTANTIAL_ISSUES if at least one substantial issue exists, or VERDICT: CLEAN if none exists.",
    ]
    return " | ".join(" ".join(p.splitlines()) for p in parts)


class WebReviewer:
    def __init__(self, browser: BrowserSessionBackend, metadata: dict):
        self.browser = browser
        self.metadata = metadata

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        try:
            async with asyncio.timeout(request.timeout):
                await self.browser.new_conversation()
                await self.browser.ensure_ready()
                # Model and reasoning level are set in the fresh chat and read back before
                # the prompt goes out; the audit records what actually answered.
                chosen = await self.browser.configure(request.model, request.reasoning_effort)
                raw = await self.browser.query(prompt_for(request), request.timeout)
                return parse_response(raw, **self.metadata, **chosen)
        except TimeoutError as exc:
            raise LXError(
                Category.TIMEOUT, "Review deadline exhausted; submission was not retried"
            ) from exc

    async def health(self) -> Health:
        return await self.browser.health()
