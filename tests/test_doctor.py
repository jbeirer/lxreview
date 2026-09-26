import subprocess

from lxreview.config import Config
from lxreview.contracts import Health
from lxreview.doctor import diagnose
from lxreview.errors import Category, LXError


class Browser:
    async def health(self):
        return Health(ready=True, detail="ready")

    async def sessions(self):
        return [{"key": "chatgpt-reviewer"}]


async def test_doctor_does_not_infer_mac_supervision_or_missing_listeners(paths, monkeypatch):
    config = Config(mode="local-browser", role="workstation")
    config.browser.placement = "local"
    config.runtime.supervisor = "launchd"
    monkeypatch.setattr("lxreview.doctor.platform.system", lambda: "Darwin")
    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr(
        "lxreview.doctor.run",
        lambda argv, *a, **k: calls.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""),
    )
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    assert checks["Persistence supervisor"]["status"] == "FAIL"
    assert calls[0][0] == "/usr/bin/launchctl"
    # The workstation's browser service uses a private socket, so no TCP listener is expected.
    assert checks["Loopback listeners"]["status"] == "PASS"
    assert "Query round trip" not in checks


async def test_doctor_fails_when_an_expected_listener_is_missing(paths, monkeypatch):
    config = Config()
    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.doctor.run",
        lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", ""),
    )
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    assert checks["Loopback listeners"]["status"] == "FAIL"


async def test_doctor_continues_after_missing_supervisor(paths, monkeypatch):
    config = Config(mode="local-browser", role="workstation")
    config.browser.placement = "local"

    def missing(name):
        raise LXError(Category.UNAVAILABLE, "missing supervisor")

    monkeypatch.setattr("lxreview.doctor.binary", missing)
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    assert checks["Persistence supervisor"]["status"] == "FAIL"
    assert checks["Reviewer single-session"]["status"] == "PASS"


async def test_doctor_checks_actual_runtimes_not_just_record(paths, monkeypatch):
    config = Config(mode="local-browser", role="workstation")
    config.browser.placement = "local"
    config.browser.chrome = str(paths.root / "runtime/chrome/chrome")
    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.doctor.run",
        lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, "wrong version", ""),
    )
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    assert checks["Chrome"]["status"] == "FAIL"
    assert checks["Playwright"]["status"] == "PASS"
    assert "Node" not in checks


async def test_doctor_reports_the_claude_login(paths, monkeypatch):
    import json

    claude = paths.root / "claude-bin"
    claude.write_text("")
    claude.chmod(0o700)
    config = Config()
    config.runtime.claude = str(claude)

    def run(argv, *a, **k):
        if argv[1:] == ["auth", "status", "--json"]:
            status = {"loggedIn": False, "authMethod": "none", "email": "hidden@example.org"}
            return subprocess.CompletedProcess(argv, 1, json.dumps(status), "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("lxreview.doctor.run", run)
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    assert checks["Claude Code"]["status"] == "FAIL"
    assert "claude auth login" in checks["Claude Code"]["detail"]
    assert "hidden@example.org" not in json.dumps(checks)


async def test_doctor_names_global_git_settings_that_block_runs(paths, monkeypatch):
    def run(argv, *a, **k):
        if argv[1:] == ["config", "--null", "--list"]:
            listing = "push.default\nmatching\0url.https://x.invalid/.insteadof\nhttps://token@github.com/\0"
            return subprocess.CompletedProcess(argv, 0, listing, "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("lxreview.doctor.run", run)
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    check = {c["check"]: c for c in await diagnose(paths, Config(), smoke=False)}[
        "Git push settings"
    ]
    # push.default no longer matters; a redirecting URL rewrite does, named by key only.
    assert check["status"] == "FAIL"
    assert "url.https://x.invalid/.insteadof" in check["detail"]
    assert "push.default" not in check["detail"] and "token@" not in check["detail"]
