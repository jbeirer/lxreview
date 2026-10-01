import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import EFFORT_NAME, MODEL_NAME
from .errors import Category, LXError
from .paths import Paths, atomic_write

CHROME_VERSION = "154.0.8037.57"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ReviewerConfig(Strict):
    backend: Literal["chatgpt-web"] = "chatgpt-web"
    provider: Literal["chatgpt"] = "chatgpt"
    # As ChatGPT's model picker names them: a model such as "GPT-5.6 Sol" (or a unique part
    # of its name, "sol") and a level your plan offers, such as instant, medium or high.
    # "highest" is the top level offered; model "default" keeps ChatGPT's current model.
    model: str = Field(default="default", pattern=MODEL_NAME)
    reasoning_effort: str = Field(default="highest", pattern=EFFORT_NAME)

    @field_validator("reasoning_effort")
    @classmethod
    def explicit_level(cls, value: str) -> str:
        # ChatGPT keeps the last level on the account, so leaving it untouched would
        # inherit whatever an earlier run or the user last chose.
        if value == "default":
            raise ValueError("choose a reasoning level; ChatGPT would keep the last one used")
        return value


class WorkerConfig(Strict):
    """The Claude model and effort for the evaluate, edit and commit turns."""

    # A Claude Code model alias or full name ("opus", "sonnet", "claude-opus-5-5").
    model: str = Field(default="default", pattern=r"^[\w.\[\]-]{1,60}$")
    effort: Literal["default", "low", "medium", "high", "xhigh", "max"] = "default"


class BrowserConfig(Strict):
    backend: Literal["playwright"] = "playwright"
    placement: Literal["local", "host"] = "host"
    profile: Literal["isolated", "existing"] = "isolated"
    chrome: str = ""


class ReviewConfig(Strict):
    max_passes: int = Field(default=5, ge=1, le=20)
    # ChatGPT's extended reasoning on a large PR can take well over ten minutes.
    timeout: float = Field(default=1800, ge=5, le=1800)
    # One Claude turn, including the project's full checks.
    worker_timeout: float = Field(default=7200, ge=30, le=28800)


class VerifyConfig(Strict):
    """How the worker runs a project's own checks (see toolchain.py)."""

    # Extra tool directories, searched before the usual per-user toolchains.
    path: list[str] = Field(default_factory=list)
    # Extra environment variables for every check.
    env: dict[str, str] = Field(default_factory=dict)
    # Repository path -> shell command that prepares its environment, for example
    # "source /cvmfs/sw.hsf.org/key4hep/setup.sh" or "source ~/miniforge3/bin/activate ana".
    # It runs outside the sandbox before the worker edits anything; it must not source
    # files from the repository, which the worker may change.
    setup: dict[str, str] = Field(default_factory=dict)
    # Parallelism for full test runs when the project's runner supports it: "auto" (all
    # CPUs), a worker count, or "off".
    test_workers: Literal["auto", "off"] | Annotated[int, Field(ge=1, le=1024)] = "auto"


class PublishConfig(Strict):
    """Whether the worker commits and LXReview pushes on their own, or wait for the user's
    approval first (`lxreview approve`)."""

    commit: Literal["auto", "ask"] = "auto"
    push: Literal["auto", "ask"] = "auto"


class RuntimeConfig(Strict):
    claude: str = ""
    host: str = ""
    display: int = Field(default=99, ge=1, le=500)
    supervisor: Literal["systemd", "tmux-scope", "launchd"] = "systemd"


# Per-run choices (`lxreview run --chatgpt MODEL:EFFORT --claude MODEL:EFFORT --commit ask
# --push ask`) and the settings they override.
CHOICES = {
    "reviewer_model": ("reviewer", "model"),
    "reviewer_effort": ("reviewer", "reasoning_effort"),
    "worker_model": ("worker", "model"),
    "worker_effort": ("worker", "effort"),
    "commit": ("publish", "commit"),
    "push": ("publish", "push"),
}
# Claude Code's model aliases and effort levels (claude --help); full names also work.
WORKER_MODELS = ("opus", "sonnet", "haiku", "fable")
WORKER_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class Config(Strict):
    schema_version: Literal[1] = 1
    mode: Literal["host-browser", "local-browser"] = "host-browser"
    role: Literal["host", "workstation"] = "host"
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    def with_choices(self, choices: dict) -> "Config":
        """This configuration with a run's own choices, validated."""
        chosen = self.model_copy(deep=True)
        try:
            for key, value in choices.items():
                section, field = CHOICES[key]
                setattr(getattr(chosen, section), field, value)
        except (KeyError, ValidationError) as exc:
            raise LXError(Category.CONFIG, f"Invalid run choice: {key}={value!r}") from exc
        return chosen

    @classmethod
    def load(cls, paths: Paths) -> "Config":
        try:
            data = tomllib.loads(paths.config.read_text())
            if data.get("schema_version", 1) != 1:
                raise LXError(
                    Category.CONFIG, "Unsupported schema; install a compatible LXReview version"
                )
            if "LXREVIEW_MAX_PASSES" in os.environ:
                data.setdefault("review", {})["max_passes"] = int(os.environ["LXREVIEW_MAX_PASSES"])
            config = cls.model_validate(data)
            if config.browser.placement != ("host" if config.mode == "host-browser" else "local"):
                raise LXError(Category.CONFIG, "Browser placement conflicts with mode")
            return config
        except ValidationError as exc:
            error = exc.errors()[0]
            where = ".".join(str(part) for part in error["loc"])
            raise LXError(
                Category.CONFIG, f"Invalid setting {where} in {paths.config}: {error['msg']}"
            ) from exc
        except (OSError, ValueError) as exc:
            raise LXError(
                Category.CONFIG,
                "Invalid or missing config; run lxreview setup (unknown keys are rejected)",
            ) from exc

    def save(self, paths: Paths) -> None:
        # Only deliberate settings: a saved default would pin it when a later release changes it.
        data = {"schema_version": self.schema_version, **self.model_dump(exclude_defaults=True)}
        atomic_write(paths.config, tomli_w.dumps(data))


def executable(path: str) -> Path:
    result = Path(path)
    if not result.is_absolute() or not result.is_file() or not os.access(result, os.X_OK):
        raise LXError(
            Category.CONFIG, "Configured executable must be an existing absolute executable path"
        )
    return result
