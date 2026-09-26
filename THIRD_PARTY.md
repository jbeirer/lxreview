# Third-party components

LXReview composes these projects; no Agentify implementation or branding is copied.

| Component | License / terms | Distribution |
|---|---|---|
| [Agentify Desktop](https://github.com/agentify-sh/desktop) 0.2.4 | MPL-2.0; separate trademark restrictions | Pinned npm dependency, source and notices remain in private installation |
| [Electron](https://github.com/electron/electron) 39.8.7 | MIT and bundled component notices | Locked npm package and verified upstream binary |
| [Node.js](https://nodejs.org/) 22.23.3 | MIT plus bundled licenses | Official verified private archive |
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 154.0.8037.57 | Google Chrome terms and Chromium third-party notices | Downloaded directly from Google's release bucket |
| [CPython](https://www.python.org/) | PSF license | uv-managed private runtime |
| [uv](https://github.com/astral-sh/uv) | MIT / Apache-2.0 | User-provided bootstrap prerequisite |
| [Playwright](https://github.com/microsoft/playwright) | Apache-2.0 | Optional experimental benchmark dependency |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | Python dependency |
| Typer, Pydantic, HTTPX, Rich, tomli-w | MIT | Python dependencies |
| aiohttp | Apache-2.0 | Python dependency |
| psutil | BSD-3-Clause | Python dependency |

Exact Python dependencies and hashes are in `uv.lock`; the complete Node dependency tree is in `src/lxreview/resources/agentify-package-lock.json`. Preserve upstream license files when redistributing runtimes. Claude Code, TigerVNC, Xfce, systemd, tmux, SSH and macOS launchd are external installed tools, not bundled here. This software has no claimed endorsement by CERN, OpenAI, Anthropic or any upstream project.
