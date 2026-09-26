"""Composition root. Orchestration imports only the reviewer protocol."""

from importlib.metadata import version

from .bridge.relay import RelayReviewer, RelaySession
from .browser.service import ServiceSession
from .config import CHROME_VERSION, Config
from .paths import Paths
from .reviewer import WebReviewer


def browser(paths: Paths, config: Config):
    if config.mode == "local-browser" and config.role == "host":
        return RelaySession(paths)
    return ServiceSession(paths)


def metadata(config: Config) -> dict:
    return {
        "backend": config.reviewer.backend,
        "provider": config.reviewer.provider,
        "playwright": version("playwright"),
        "chrome": CHROME_VERSION,
    }


def reviewer(paths: Paths, config: Config):
    if config.mode == "local-browser" and config.role == "host":
        return RelayReviewer(paths)
    return WebReviewer(browser(paths, config), metadata(config))
