# Third-party components

LXReview composes these projects.

| Component | License / terms | Distribution |
|---|---|---|
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 154.0.8037.57 | Google Chrome terms and Chromium third-party notices | Downloaded directly from Google's release bucket |
| [CPython](https://www.python.org/) | PSF license | uv-managed private runtime |
| [uv](https://github.com/astral-sh/uv) | MIT / Apache-2.0 | User-provided bootstrap prerequisite |
| [Playwright](https://github.com/microsoft/playwright) 1.63.0 | Apache-2.0 (its wheel bundles a Node.js driver under MIT) | Python dependency; drives the pinned Chrome |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | Python dependency |
| Typer, Pydantic, HTTPX, Rich, tomli-w | MIT | Python dependencies |
| aiohttp | Apache-2.0 | Python dependency |
| psutil | BSD-3-Clause | Python dependency |

Exact Python dependencies and hashes are in `uv.lock`. Preserve upstream license files when redistributing runtimes. Claude Code, TigerVNC, Xfce, systemd, tmux, SSH and macOS launchd are external installed tools, not bundled here. This software has no claimed endorsement by CERN, OpenAI, Anthropic or any upstream project.
