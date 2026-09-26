"""Immutable release directories plus an atomic launcher switch and rollback."""

import json
import secrets
import shutil
from pathlib import Path

from ..errors import Category, LXError
from ..paths import Paths, atomic_write, private_dir, write_json
from ..process import environment, run


def application_update(paths: Paths, source: Path) -> None:
    source = source.resolve()
    if source == paths.root or paths.root.is_relative_to(source):
        raise LXError(Category.UNSAFE, "Upgrade source must not contain the installation root")
    if not (source / "uv.lock").is_file() or not (source / "pyproject.toml").is_file():
        raise LXError(
            Category.CONFIG, "Upgrade source must be a reviewed LXReview checkout with uv.lock"
        )
    import tomllib

    project = tomllib.loads((source / "pyproject.toml").read_text())
    if project.get("project", {}).get("name") != "lxreview":
        raise LXError(Category.CONFIG, "Upgrade source is not LXReview")
    version = str(project["project"]["version"])
    # Root-owned immutable path: venv scripts never need rewriting after installation.
    release = private_dir(paths.root / "runtime/releases" / secrets.token_hex(8))
    checkout = release / "application"
    shutil.copytree(
        source,
        checkout,
        ignore=lambda directory, names: [
            name
            for name in names
            if (
                Path(directory) == source
                and name not in {"src", "pyproject.toml", "uv.lock", "README.md", "LICENSE"}
            )
            or name == "__pycache__"
            or name.endswith(".pyc")
        ],
    )
    env = environment(paths)
    env.update(
        {
            "UV_NO_CONFIG": "1",
            "UV_CACHE_DIR": str(paths.root / "cache/uv"),
            "UV_PYTHON_INSTALL_DIR": str(paths.root / "runtime/python"),
            "UV_PROJECT_ENVIRONMENT": str(release / "venv"),
        }
    )
    # uv is a bootstrap tool, not a global runtime installed or changed by LXReview.

    old = paths.executable.read_text()
    current_python = old.splitlines()[0].removeprefix("#!")
    if not Path(current_python).is_relative_to(paths.root) or not Path(current_python).is_file():
        raise LXError(Category.UNSAFE, "Current launcher does not use a private interpreter")
    uv = shutil.which("uv")
    if not uv:
        raise LXError(Category.CONFIG, "A reviewed uv executable is required for package upgrades")
    run(
        [
            str(Path(uv).resolve()),
            "sync",
            "--project",
            str(checkout),
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--python",
            current_python,
        ],
        paths,
        env=env,
        timeout=600,
    )
    python = release / "venv/bin/python"
    run([str(python), "-m", "lxreview.cli", "version"], paths, timeout=30)
    launcher = f"#!{python}\nimport os\nos.environ['LXREVIEW_HOME'] = {str(paths.root)!r}\nfrom lxreview.cli import main\nmain()\n"
    write_json(
        paths.root / "state/application-versions.json",
        {"version": version, "current": launcher, "previous": old, "release": str(release)},
    )
    atomic_write(paths.executable, launcher, 0o700)


def application_rollback(paths: Paths) -> None:
    record = paths.root / "state/application-versions.json"
    if not record.exists():
        raise LXError(Category.CONFIG, "No prior application launcher is recorded")
    state = json.loads(record.read_text())
    previous = state["previous"]
    interpreter = Path(previous.splitlines()[0].removeprefix("#!"))
    if not interpreter.is_relative_to(paths.root) or not interpreter.is_file():
        raise LXError(Category.UNSAFE, "Previous private interpreter is missing; rollback refused")
    atomic_write(paths.executable, previous, 0o700)
    state["previous"], state["current"] = state["current"], previous
    write_json(record, state)
