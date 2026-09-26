from mcp.server.mcpserver import MCPServer

from ..backend import browser, reviewer
from ..config import Config
from ..contracts import ReviewRequest
from ..errors import LXError
from ..paths import Paths, lock


def create_server(paths: Paths, config: Config) -> MCPServer:
    server = MCPServer("lxreview-reviewer")
    session = browser(paths, config)
    backend = reviewer(paths, config)

    async def operation(name: str):
        try:
            with lock(paths.root / "state/reviewer.lock"):
                result = await getattr(session, name)()
            return {
                "ok": True,
                "result": result.model_dump() if hasattr(result, "model_dump") else result,
            }
        except LXError as exc:
            return exc.as_dict()

    @server.tool()
    async def review_browser_query(target: str, head_sha: str, timeout: float = 600) -> dict:
        """Perform one fresh independent full review and return the verbatim assistant response."""
        try:
            request = ReviewRequest(target=target, head_sha=head_sha, timeout=timeout)
            with lock(paths.root / "state/reviewer.lock"):
                result = await backend.review(request)
            return result.model_dump(mode="json")
        except LXError as exc:
            return exc.as_dict()

    @server.tool()
    async def review_browser_navigate() -> dict:
        """Reset only the managed reviewer to a fresh conversation."""
        return await operation("new_conversation")

    @server.tool()
    async def review_browser_ensure_ready() -> dict:
        """Check login and composer readiness with a bounded timeout."""
        return await operation("ensure_ready")

    @server.tool()
    async def review_browser_status() -> dict:
        """Get reviewer health without starting an unintended browser."""
        return await operation("health")

    @server.tool()
    async def review_browser_sessions() -> dict:
        """List only LXReview-managed reviewer sessions."""
        return await operation("sessions")

    @server.tool()
    async def review_browser_close_session() -> dict:
        """Close and verify removal of only the managed reviewer session."""
        return await operation("close")

    @server.tool()
    async def review_browser_recover() -> dict:
        """Recover a wedged managed reviewer; old session closes before replacement."""
        return await operation("recover")

    return server
