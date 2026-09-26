import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import Category, LXError
from .paths import Paths, atomic_write

AGENTIFY_VERSION = "0.2.4"
NODE_VERSION = "22.23.3"
CHROME_VERSION = "154.0.8037.57"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ReviewerConfig(Strict):
    backend: Literal["agentify-web"] = "agentify-web"
    provider: Literal["chatgpt"] = "chatgpt"
    model: Literal["default"] = "default"
    reasoning_effort: Literal["default"] = "default"


class BrowserConfig(Strict):
    backend: Literal["agentify"] = "agentify"
    placement: Literal["local", "lxplus"] = "lxplus"
    profile: Literal["isolated", "existing"] = "isolated"
    chrome: str = ""
    debug_port: int = Field(default=19222, ge=1024, le=65535)


class ReviewConfig(Strict):
    max_passes: int = Field(default=5, ge=1, le=20)
    timeout: float = Field(default=600, ge=5, le=1800)
    worker_timeout: float = Field(default=3600, ge=30, le=14400)


class RuntimeConfig(Strict):
    claude: str = ""
    host: str = ""
    display: int = Field(default=99, ge=1, le=500)
    supervisor: Literal["systemd", "tmux-scope", "launchd"] = "systemd"


class Config(Strict):
    schema_version: Literal[1] = 1
    mode: Literal["lxplus-browser", "local-browser"] = "lxplus-browser"
    role: Literal["host", "workstation"] = "host"
    reviewer: ReviewerConfig = Field(default_factory=ReviewerConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

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
            if config.browser.placement != (
                "lxplus" if config.mode == "lxplus-browser" else "local"
            ):
                raise LXError(Category.CONFIG, "Browser placement conflicts with mode")
            return config
        except (OSError, ValueError, ValidationError) as exc:
            raise LXError(
                Category.CONFIG,
                "Invalid or missing config; run lxreview setup (unknown keys are rejected)",
            ) from exc

    def save(self, paths: Paths) -> None:
        atomic_write(paths.config, tomli_w.dumps(self.model_dump()))


def executable(path: str) -> Path:
    result = Path(path)
    if not result.is_absolute() or not result.is_file() or not os.access(result, os.X_OK):
        raise LXError(
            Category.CONFIG, "Configured executable must be an existing absolute executable path"
        )
    return result
