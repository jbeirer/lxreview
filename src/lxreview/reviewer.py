import asyncio

from . import forge
from .contracts import BrowserSessionBackend, Health, ReviewRequest, ReviewResponse, parse_response
from .errors import Category, LXError


def prompt_for(request: ReviewRequest) -> str:
    target = forge.parse(request.target)
    if target.kind == "github":
        where = [
            "Open and inspect the pull request yourself, including the complete current diff and surrounding repository code. Do not rely on Claude's description or on any previous review pass. Verify the head commit matches.",
            "Read the PR's conversation too, including review threads and whether they are resolved. Do not report a point the discussion already settled with a reason that still holds for the current code; if you think a settled reason is wrong, report it and name the comment you disagree with.",
        ]
    else:
        # GitLab's merge request pages render with scripts; point at plain-text views too.
        code = f"https://{target.host}/{target.project}/-"
        where = [
            f"Open and inspect the merge request yourself, including the complete current diff and surrounding repository code. The complete diff is at {request.target}.diff and the repository at this head is at {code}/tree/{request.head_sha} (raw files under {code}/raw/{request.head_sha}/). Do not rely on Claude's description or on any previous review pass. Verify the head commit matches.",
            "If the MR's conversation is accessible, read it too, including discussion threads and whether they are resolved. Do not report a point the discussion already settled with a reason that still holds for the current code; if you think a settled reason is wrong, report it and name the comment you disagree with.",
        ]
    parts = [
        f"Perform a fresh independent full review of {request.target} at current head commit {request.head_sha}.",
        *where,
        f"Look for substantial issues in: {request.rubric}.",
        "For each finding classify it SUBSTANTIAL or NON_BLOCKING, identify file and line or symbol, explain the concrete problem and suggest a concrete fix. Start each finding on its own line as SUBSTANTIAL [S1], SUBSTANTIAL [S2], or NON_BLOCKING [N1], using unique IDs. Subjective style preferences are not SUBSTANTIAL.",
        f"If you cannot access or adequately inspect the complete {target.noun} at this head, say so and end exactly with VERDICT: ACCESS_FAILED.",
        "Otherwise end exactly with VERDICT: SUBSTANTIAL_ISSUES if at least one substantial issue exists, or VERDICT: CLEAN if none exists.",
    ]
    if request.known_findings:
        parts.insert(
            4,
            "Earlier independent review passes of this same head already reported these issues, listed only so you do not repeat them; they say nothing about their validity: "
            + "; ".join(f"{i}. {title}" for i, title in enumerate(request.known_findings, 1))
            + ". Do not report them again in any form, nor a variant or another symptom of the same cause; review everything else as thoroughly as a first pass. If no substantial issue exists beyond them, end with VERDICT: CLEAN.",
        )
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
