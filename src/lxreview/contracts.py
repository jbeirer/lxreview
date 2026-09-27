import re
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, field_validator

from .errors import Category


class Verdict(StrEnum):
    CLEAN = "CLEAN"
    SUBSTANTIAL = "SUBSTANTIAL_ISSUES"
    ACCESS_FAILED = "ACCESS_FAILED"
    INVALID = "INVALID"


# A reviewer model as the provider names it ("GPT-5.6 Sol"), and a reasoning level as its
# picker names it ("high"); "default" leaves the provider's current choice untouched.
MODEL_NAME = r"^[\w .+()-]{1,60}$"
EFFORT_NAME = r"^[a-z][a-z -]{0,30}$"


class ReviewRequest(BaseModel):
    target: str
    head_sha: str
    rubric: str = "correctness, regressions, edge cases, unnecessary complexity, API consistency, test quality, maintainability"
    timeout: float = Field(default=1800, ge=5, le=1800)
    model: str = Field(default="default", pattern=MODEL_NAME)
    reasoning_effort: str = Field(default="default", pattern=EFFORT_NAME)

    @field_validator("target")
    @classmethod
    def valid_target(cls, value: str) -> str:
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*", value):
            raise ValueError("Expected a canonical GitHub PR URL")
        return value

    @field_validator("head_sha")
    @classmethod
    def valid_sha(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
            raise ValueError("Expected a full commit SHA")
        return value


class ReviewResponse(BaseModel):
    raw: str
    verdict: Verdict
    findings: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    failure: Category | None = None


class Health(BaseModel):
    ready: bool
    detail: str
    metadata: dict = Field(default_factory=dict)


class BrowserSessionBackend(Protocol):
    async def new_conversation(self) -> None: ...
    async def ensure_ready(self) -> None: ...
    async def configure(self, model: str, reasoning_effort: str) -> dict: ...
    async def options(self) -> dict: ...
    async def query(self, prompt: str, timeout: float) -> str: ...
    async def health(self) -> Health: ...
    async def sessions(self) -> list[dict]: ...
    async def close(self) -> None: ...
    async def recover(self) -> None: ...


class ReviewerBackend(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResponse: ...
    async def health(self) -> Health: ...


# Markdown heading, quote, list and bold prefixes. No nested quantifiers (no backtracking blowup).
FINDING = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?:>[ \t]*)?(?:(?:[*+\-]|\d{1,3}[.)])[ \t]*)?\**[ \t]*"
    r"(SUBSTANTIAL|NON_BLOCKING)[ \t]*\[([SN][1-9][0-9]*)\]\**([^\n]*)"
)
# Any line opening with a classification must be a parsed finding or a section heading.
MENTION = re.compile(r"(?m)^[ \t#>*+\-\d.)]*(?:SUBSTANTIAL|NON_BLOCKING)\b([^\n]*)")
HEADING = re.compile(
    r"(?i)[\s:*()\-–—.,]*"
    r"(?:(?:findings?|issues?|suggestions?|items?|found|identified|none|no|n/?a|\d+)[\s:*()\-–—.,]*)*"
)


def parse_response(raw: str, **metadata: object) -> ReviewResponse:
    matches = re.findall(r"^VERDICT: (CLEAN|SUBSTANTIAL_ISSUES|ACCESS_FAILED)$", raw.strip(), re.M)
    last = raw.strip().splitlines()[-1] if raw.strip() else ""
    verdict = (
        Verdict(matches[0])
        if len(matches) == 1 and last == f"VERDICT: {matches[0]}"
        else Verdict.INVALID
    )
    findings = [
        {"id": match[2], "classification": match[1], "title": match[3].strip(" \t:-–—")}
        for match in FINDING.finditer(raw)
    ]
    ids = [finding["id"] for finding in findings]
    substantial = [f for f in findings if f["classification"] == "SUBSTANTIAL"]
    # "NON_BLOCKING: none" or "SUBSTANTIAL findings" headings are not findings; anything else
    # opening with a classification but lacking a parseable ID fails closed.
    headers = [m for m in MENTION.finditer(raw) if not HEADING.fullmatch(m[1])]
    if (
        len(ids) != len(set(ids))
        or len(headers) != len(findings)
        or any(f["id"][0] != f["classification"][0] for f in findings)
        or (verdict == Verdict.SUBSTANTIAL and not substantial)
        or (verdict == Verdict.CLEAN and substantial)
    ):
        verdict = Verdict.INVALID
    failure = (
        Category.PROTOCOL
        if verdict == Verdict.INVALID
        else Category.ACCESS
        if verdict == Verdict.ACCESS_FAILED
        else None
    )
    return ReviewResponse(
        raw=raw, verdict=verdict, findings=findings, metadata=dict(metadata), failure=failure
    )
