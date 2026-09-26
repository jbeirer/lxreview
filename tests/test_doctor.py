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
    assert checks["Loopback listeners"]["status"] == "FAIL"
    assert "Query round trip" not in checks


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
    from lxreview.config import AGENTIFY_VERSION
    from lxreview.paths import write_json

    config = Config(mode="local-browser", role="workstation")
    config.browser.placement = "local"
    config.browser.chrome = str(paths.root / "runtime/chrome/chrome")
    write_json(
        paths.root / "state/runtime.json",
        {
            "agentify": AGENTIFY_VERSION,
            "electron": "/missing/electron",
        },
    )
    package = paths.root / "runtime/agentify/node_modules/@agentify/desktop/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"0.0.0"}')
    monkeypatch.setattr("lxreview.doctor.binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        "lxreview.doctor.run",
        lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, "wrong version", ""),
    )
    monkeypatch.setattr("lxreview.doctor.browser", lambda *a: Browser())
    monkeypatch.setattr("lxreview.doctor.psutil.net_connections", lambda **k: [])
    checks = {c["check"]: c for c in await diagnose(paths, config, smoke=False)}
    for name in ("Node", "Chrome", "Electron host", "Agentify installation"):
        assert checks[name]["status"] == "FAIL"
