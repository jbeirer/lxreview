"""Composition root. Orchestration imports only the reviewer protocol."""

from .bridge.relay import RelayReviewer, RelaySession
from .browser.agentify import AgentifyAPI, AgentifySession
from .config import AGENTIFY_VERSION, Config
from .paths import Paths
from .reviewer import WebReviewer


def browser(paths: Paths, config: Config):
    if config.mode == "local-browser" and config.role == "host":
        return RelaySession(paths)
    return AgentifySession(AgentifyAPI(paths.root / "state/agentify"))


def reviewer(paths: Paths, config: Config):
    if config.mode == "local-browser" and config.role == "host":
        return RelayReviewer(paths)
    return WebReviewer(
        browser(paths, config),
        {
            "backend": config.reviewer.backend,
            "provider": config.reviewer.provider,
            "agentify": AGENTIFY_VERSION,
        },
    )
