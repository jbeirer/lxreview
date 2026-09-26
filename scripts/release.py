#!/usr/bin/env python3
"""Local release gate; does not publish or upgrade browser pins automatically."""

import shutil
import subprocess
from pathlib import Path


def main():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required")
    repo = Path(__file__).resolve().parents[1]
    commands = [
        [uv, "sync", "--frozen"],
        [uv, "run", "ruff", "check", "."],
        [uv, "run", "ruff", "format", "--check", "."],
        [uv, "run", "mypy", "src"],
        [uv, "run", "pytest"],
        [uv, "build"],
    ]
    for command in commands:
        subprocess.run(command, cwd=repo, check=True)
    print("Release checks passed.")


if __name__ == "__main__":
    main()
