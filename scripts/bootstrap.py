#!/usr/bin/env python3
"""Install from this reviewed checkout without changing shell configuration."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".lxreview")
    args = parser.parse_args()
    root = Path(os.path.abspath(args.root.expanduser()))
    source = Path(__file__).resolve().parents[1]
    if any(p.is_symlink() and (p == root or p.lstat().st_uid != 0) for p in (root, *root.parents)):
        parser.error("installation root and its parents must not be symlinks")
    if (
        root == Path.home()
        or root in Path.home().parents
        or root.is_relative_to(source)
        or source.is_relative_to(root)
    ):
        parser.error("use a dedicated root outside the source checkout and home ancestors")
    uv = shutil.which("uv")
    if not uv:
        parser.error(
            "uv is required. Install a reviewed uv executable, then rerun; no shell profiles are modified"
        )
    if root.exists() and any(root.iterdir()):
        parser.error(
            "installation root must be empty; use lxreview update for an existing installation"
        )
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    checkout = root / "runtime/application"
    if checkout.exists():
        parser.error(
            "application already exists; use a new root for a reviewed upgrade, preserving the old installation"
        )
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
    env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(root / "cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "runtime/python"),
        "UV_PROJECT_ENVIRONMENT": str(root / "runtime/venv"),
    }
    subprocess.run(
        [
            uv,
            "sync",
            "--project",
            str(checkout),
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--python",
            "3.12",
            "--python-preference",
            "only-managed",
        ],
        env=env,
        check=True,
    )
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True, mode=0o700)
    launcher = bindir / "lxreview"
    python = root / "runtime/venv/bin/python"
    launcher.write_text(
        f"#!{python}\nimport os\nos.environ['LXREVIEW_HOME'] = {str(root)!r}\nfrom lxreview.cli import main\nmain()\n"
    )
    launcher.chmod(0o700)
    print(
        f"Installed {launcher}\nRun: {launcher} setup\nPATH was not changed; use the absolute path."
    )


if __name__ == "__main__":
    main()
