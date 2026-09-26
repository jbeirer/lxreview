#!/usr/bin/env python3
"""Install from this reviewed checkout without changing shell configuration."""

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# Every top-level name LXReview creates below its installation root.
OWNED = {
    "bin",
    "cache",
    "claude",
    "config",
    "downloads",
    "logs",
    "runtime",
    "secrets",
    "state",
    "versions",
}


def leftover_services(root):
    """Return commands that stop services still running from a deleted installation."""
    # Mirrors Supervisor.unit: unit names embed a hash of the installation root.
    prefix = "lxreview-" + hashlib.sha256(str(root).encode()).hexdigest()[:8] + "-"
    if platform.system() == "Darwin":
        launchctl = shutil.which("launchctl", path="/bin:/usr/bin")
        if not launchctl:
            return []
        listing = subprocess.run([launchctl, "list"], capture_output=True, text=True).stdout
        labels = [line.split()[-1] for line in listing.splitlines() if line.strip()]
        return [
            f"launchctl bootout gui/{os.getuid()}/{label}"
            for label in labels
            if label.startswith("org.lxreview." + prefix)
        ]
    systemctl = shutil.which("systemctl", path="/usr/bin:/bin")
    if not systemctl:
        return []
    listing = subprocess.run(
        [systemctl, "--user", "list-units", "--plain", "--no-legend", prefix + "*"],
        capture_output=True,
        text=True,
    ).stdout
    units = [line.split()[0] for line in listing.splitlines() if line.strip()]
    return ["systemctl --user stop " + " ".join(units)] if units else []


def occupied(root, source):
    """Explain how to proceed when the installation root is not empty, else None."""
    if not root.exists() or not any(root.iterdir()):
        return None
    launcher = root / "bin/lxreview"
    if launcher.exists():
        return (
            f"LXReview is already installed in {root}.\n"
            f"  To upgrade it:  {launcher} update --source {source}\n"
            f"  To remove it:   {launcher} uninstall"
        )
    if any(entry.name not in OWNED for entry in root.iterdir()):
        return f"{root} is not empty and is not an LXReview installation; choose an empty --root."
    lines = [f"{root} holds leftovers of an installation deleted without lxreview uninstall."]
    stops = leftover_services(root)
    if stops:
        lines += ["Its services are still running and keep writing there. Stop them with:"]
        lines += ["  " + command for command in stops]
    lines += [
        "Then remove the leftovers and rerun bootstrap:",
        "  rm -rf " + shlex.quote(str(root)),
    ]
    return "\n".join(lines)


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
    problem = occupied(root, source)
    if problem:
        sys.exit(problem)
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
