import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lxreview.config import Config
from lxreview.paths import Paths
from lxreview.process import Supervisor

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap.py"


@pytest.fixture
def bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_root_is_accepted(bootstrap, tmp_path):
    assert bootstrap.occupied(tmp_path / "missing", tmp_path) is None
    (tmp_path / "empty").mkdir()
    assert bootstrap.occupied(tmp_path / "empty", tmp_path) is None


def test_existing_installation_points_to_update_and_uninstall(bootstrap, tmp_path):
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "bin/lxreview").write_text("")
    message = bootstrap.occupied(root, tmp_path / "checkout")
    assert "update --source" in message and "uninstall" in message
    assert "rm -rf" not in message


def test_unrelated_directory_is_never_offered_for_deletion(bootstrap, tmp_path):
    root = tmp_path / "projects"
    (root / "state").mkdir(parents=True)
    (root / "thesis.tex").write_text("")
    assert "rm -rf" not in bootstrap.occupied(root, tmp_path)


def test_leftovers_name_running_services_before_removal(bootstrap, tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "state/vnc").mkdir(parents=True)
    supervisor = Supervisor(Paths(root), Config())
    browser, desktop = supervisor.unit("browser"), supervisor.unit("desktop")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        listing = f"{browser}.service loaded active running x\n{desktop}.service loaded active running y\n"
        return subprocess.CompletedProcess(argv, 0, listing, "")

    monkeypatch.setattr(bootstrap, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(
        bootstrap, "shutil", SimpleNamespace(which=lambda name, path: "/bin/" + name)
    )
    monkeypatch.setattr(bootstrap, "subprocess", SimpleNamespace(run=run))
    message = bootstrap.occupied(root, tmp_path)
    # The unit-name prefix must match Supervisor.unit, or live services would go unnoticed.
    assert calls[0][-1] == browser.removesuffix("browser") + "*"
    assert f"systemctl --user stop {browser}.service {desktop}.service" in message
    assert message.index("systemctl") < message.index("rm -rf")
