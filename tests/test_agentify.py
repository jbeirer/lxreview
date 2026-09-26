import json

import httpx
import pytest

from lxreview.browser.agentify import AgentifyAPI, AgentifySession
from lxreview.errors import Category, LXError
from lxreview.paths import atomic_write, write_json


class FakeAgentify:
    """Published 0.2.4 envelope; unrelated sessions must never be touched."""

    def __init__(self):
        self.tabs = [{"id": "unrelated", "key": "user-work"}]
        self.calls = []
        self.busy = 0
        self.inflight = False
        self.query_count = 0
        self.created = 0
        self.response = {
            "text": "Full review\nVERDICT: CLEAN",
            "meta": {"count": 1, "hasError": False},
        }
        self.server_id = "server-one"
        self.bad_auth = False
        self.query_timeout = False

    def handle(self, request):
        path = request.url.path
        data = json.loads(request.content) if request.content else {}
        self.calls.append((path, data))
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "serverId": self.server_id})
        if request.headers.get("authorization") != "Bearer private-token" or self.bad_auth:
            return httpx.Response(401, json={"error": "unauthorized"})
        if path == "/tabs":
            result = {"ok": True, "tabs": self.tabs.copy()}
        elif path == "/tabs/create":
            self.created += 1
            self.tabs.append({"id": f"review-{self.created}", "key": data["key"]})
            result = {"ok": True, "tabId": self.tabs[-1]["id"]}
        elif path == "/tabs/close":
            self.tabs = [t for t in self.tabs if t["id"] != data["tabId"]]
            result = {"ok": True}
        elif path == "/status":
            result = {
                "activeQuery": {} if not self.inflight else {"id": "active"},
                "runtime": {"inflightQueries": int(self.inflight)},
            }
        elif path == "/ensure-ready":
            result = {"ok": True, "state": {"promptVisible": True, "blocked": False}}
        elif path == "/query":
            self.query_count += 1
            if self.query_timeout:
                raise httpx.ReadTimeout("Do not leak Bearer private-token")
            if self.busy:
                self.busy -= 1
                return httpx.Response(409, json={"error": "already_generating"})
            result = {"ok": True, "result": self.response}
        else:
            result = {"ok": True}
        return httpx.Response(200, json=result)


@pytest.fixture
def fake(paths):
    fake = FakeAgentify()
    state = paths.root / "state/agentify"
    write_json(state / "state.json", {"port": 12345, "serverId": fake.server_id})
    atomic_write(state / "token.txt", "private-token")
    session = AgentifySession(AgentifyAPI(state, httpx.MockTransport(fake.handle)))
    return fake, session


async def test_two_passes_one_session_and_prompt_fidelity(fake):
    service, session = fake
    for _ in range(2):
        await session.new_conversation()
        await session.ensure_ready()
        assert "VERDICT: CLEAN" in await session.query("line one\r\nline two", 5)
    assert service.created == 1
    assert len([x for x in service.calls if x[0] == "/navigate"]) == 2
    queries = [data for path, data in service.calls if path == "/query"]
    assert all(q["prompt"] == "line one | line two" for q in queries)
    assert service.tabs[0]["id"] == "unrelated"


@pytest.mark.parametrize("busy,creates", [(1, 1), (2, 2)])
async def test_false_busy_bounded_recovery(fake, busy, creates):
    service, session = fake
    await session.new_conversation()
    service.busy = busy
    await session.query("review", 5)
    assert service.created == creates
    assert len(await session.sessions()) == 1
    if creates == 2:
        operations = [p for p, _ in service.calls]
        assert operations.index("/tabs/close") < len(operations) - 1 - operations[::-1].index(
            "/tabs/create"
        )


async def test_real_query_never_reset(fake):
    service, session = fake
    await session.new_conversation()
    service.busy = 1
    service.inflight = True
    before = len(service.calls)
    with pytest.raises(LXError, match="active query"):
        await session.query("review", 5)
    assert not any(
        path in ("/navigate", "/tabs/close", "/tabs/create") for path, _ in service.calls[before:]
    )


async def test_timeout_does_not_submit_twice(fake):
    service, session = fake
    service.query_timeout = True
    with pytest.raises(LXError) as caught:
        await session.query("review", 5)
    assert caught.value.category == Category.TIMEOUT
    assert service.query_count == 1
    assert "private-token" not in str(caught.value)


async def test_generic_page_text_rejected(fake):
    service, session = fake
    service.response = {"text": "VERDICT: CLEAN", "meta": {"count": 0}}
    with pytest.raises(LXError, match="finalized"):
        await session.query("review", 5)


async def test_auth_and_server_identity(fake):
    service, session = fake
    service.bad_auth = True
    with pytest.raises(LXError) as caught:
        await session.sessions()
    assert caught.value.category == Category.AUTH
    service.server_id = "different-server"
    with pytest.raises(LXError, match="identity"):
        await session.sessions()


async def test_duplicates_fail_closed(fake):
    service, session = fake
    service.tabs += [
        {"id": "one", "key": "chatgpt-reviewer"},
        {"id": "two", "key": "chatgpt-reviewer"},
    ]
    with pytest.raises(LXError, match="Multiple"):
        await session.new_conversation()
    assert service.created == 0


async def test_readiness_login_is_actionable(paths):
    state = paths.root / "state/agentify"
    write_json(state / "state.json", {"port": 12345, "serverId": "id"})
    atomic_write(state / "token.txt", "private-token")

    def handle(request):
        return httpx.Response(
            200,
            json={"serverId": "id"}
            if request.url.path == "/health"
            else {"kind": "login", "blocked": True},
        )

    session = AgentifySession(AgentifyAPI(state, httpx.MockTransport(handle)))
    with pytest.raises(LXError) as error:
        await session.ensure_ready()
    assert error.value.category == Category.AUTH


async def test_recovery_exhaustion_stops_without_extra_reviewer(fake):
    service, session = fake
    await session.new_conversation()
    service.busy = 10
    with pytest.raises(LXError):
        await session.query("review", 5)
    assert service.query_count == 3
    assert service.created == 2
    assert len(await session.sessions()) == 1


@pytest.mark.parametrize("operation", ["new_conversation", "close", "recover"])
async def test_active_query_cannot_be_reset_or_closed(fake, operation):
    service, session = fake
    await session.new_conversation()
    service.inflight = True
    before = len(service.calls)
    with pytest.raises(LXError, match="active query"):
        await getattr(session, operation)()
    assert not any(
        path in ("/navigate", "/tabs/close", "/tabs/create") for path, _ in service.calls[before:]
    )


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"text": "review", "meta": []},
        {"text": "review", "meta": {"count": "1", "hasError": False}},
        {"text": "review", "meta": {"count": 1}},
    ],
)
async def test_malformed_query_result_is_typed_failure(fake, response):
    service, session = fake
    service.response = response
    with pytest.raises(LXError) as error:
        await session.query("review", 5)
    assert error.value.category == Category.PROTOCOL
    assert service.query_count == 1
