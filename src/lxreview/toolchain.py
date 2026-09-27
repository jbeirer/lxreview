"""The environment a project's own checks run in during a worker turn.

The worker never inherits the user's shell PATH or startup files. Instead it finds the
project's tools where they conventionally live: environments inside the repository, the
usual per-user toolchain directories, and an optional setup command the user configured
for this repository (for example a CVMFS software stack or a conda environment).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .errors import Category, LXError
from .paths import Paths
from .process import environment

SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# Tool directories that projects create inside the repository. Python virtual environments
# with any other name at the top level are found by their pyvenv.cfg.
REPOSITORY_BIN = (".venv/bin", "venv/bin", "env/bin", "node_modules/.bin", ".pixi/envs/default/bin")
# Per-user toolchain installers.
HOME_BIN = (
    "bin",
    ".local/bin",
    ".cargo/bin",
    "go/bin",
    ".pixi/bin",
    ".juliaup/bin",
    ".ghcup/bin",
    ".deno/bin",
    ".bun/bin",
    ".volta/bin",
    ".nix-profile/bin",
    ".dotnet/tools",
)
SHARED_BIN = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/go/bin",
    "/snap/bin",
)
# Variables a setup command may not hand to the worker: they belong to LXReview or would
# redirect the sandbox, Git or the dynamic loader of every command.
RESERVED = {
    "HOME",
    "TMPDIR",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "_",
    "SSH_AUTH_SOCK",
    "KRB5CCNAME",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
}


def repository_bins(repo: Path) -> list[Path]:
    directories = [repo / d for d in REPOSITORY_BIN]
    try:
        entries = sorted(repo.iterdir())
    except OSError:
        return directories
    for entry in entries:
        if (entry / "pyvenv.cfg").is_file() and (entry / "bin").is_dir():
            if entry / "bin" not in directories:
                directories.append(entry / "bin")
    return directories


def search_path(repo: Path, config: Config, inherited: str = "") -> str:
    home = Path.home()
    candidates = [
        *(str(d) for d in repository_bins(repo)),
        *config.verify.path,
        *[d for d in inherited.split(os.pathsep) if d],
        *(str(home / d) for d in HOME_BIN),
        *SHARED_BIN,
        *SYSTEM_PATH,
    ]
    seen: list[str] = []
    for directory in candidates:
        directory = os.path.expanduser(directory)
        if directory not in seen and (directory in SYSTEM_PATH or Path(directory).is_dir()):
            seen.append(directory)
    return os.pathsep.join(seen)


def setup_environment(repo: Path, paths: Paths, config: Config) -> dict[str, str]:
    """Variables the repository's configured setup command exports, captured once.

    The command comes from the user's own configuration and runs outside the sandbox,
    before the worker edits anything.
    """
    command = config.verify.setup.get(str(repo)) or config.verify.setup.get(str(repo.resolve()))
    if not command:
        return {}
    base = environment(paths)
    bash = shutil.which("bash", path=os.pathsep.join(SYSTEM_PATH))
    if not bash:
        raise LXError(Category.CONFIG, "The repository setup command needs bash")
    try:
        result = subprocess.run(
            [
                bash,
                "--noprofile",
                "--norc",
                "-c",
                f"{command} >/dev/null 2>&1 </dev/null && env -0",
            ],
            cwd=repo,
            env=base,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LXError(Category.CONFIG, "The repository setup command took over 10 minutes") from exc
    if result.returncode:
        raise LXError(
            Category.CONFIG,
            f"The repository setup command failed (exit {result.returncode}); check [verify.setup]",
        )
    exported = {}
    for entry in result.stdout.decode(errors="replace").split("\0"):
        name, sep, value = entry.partition("=")
        if sep and name not in RESERVED and base.get(name) != value:
            exported[name] = value
    return exported


def cache_environment(scratch: Path) -> dict[str, str]:
    """Writable caches for the turn: the real ones are read-only inside the sandbox."""
    cache = scratch / "cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    variables = {
        "XDG_CACHE_HOME": cache,
        "GOCACHE": cache / "go-build",
        "npm_config_cache": cache / "npm",
        "YARN_CACHE_FOLDER": cache / "yarn",
        "PIP_CACHE_DIR": cache / "pip",
        "MPLCONFIGDIR": cache / "matplotlib",
        "CCACHE_DIR": cache / "ccache",
        "PRE_COMMIT_HOME": cache / "pre-commit",
    }
    # pre-commit keeps installed hook environments under its home and records them in
    # db.db by absolute path: a copy of the database lets hooks run from the existing,
    # read-only installs instead of reinstalling them (which would need the network).
    existing = Path(os.environ.get("PRE_COMMIT_HOME") or Path.home() / ".cache/pre-commit")
    if (existing / "db.db").is_file():
        (cache / "pre-commit").mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(existing / "db.db", cache / "pre-commit/db.db")
    return {name: str(value) for name, value in variables.items()}


def turn_environment(
    repo: Path, paths: Paths, config: Config, scratch: Path, setup: dict[str, str]
) -> dict[str, str]:
    env = environment(paths)
    # Claude itself never needs agent or ticket access; pushes happen outside Claude.
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("KRB5CCNAME", None)
    env.update(setup)
    env.update(config.verify.env)
    env.update(cache_environment(scratch))
    env["PATH"] = search_path(repo, config, setup.get("PATH", ""))
    env["TMPDIR"] = str(scratch)
    return env


def push_environment(paths: Paths, config: Config) -> dict[str, str]:
    """For LXReview's own commit signing and push, outside the sandbox.

    Credential helpers such as gh or git-credential-osxkeychain are found in the usual
    toolchain directories, never inside the repository the worker could change.
    """
    env = environment(paths)
    home = Path.home()
    directories = [*(str(home / d) for d in HOME_BIN), *SHARED_BIN, *SYSTEM_PATH]
    env["PATH"] = os.pathsep.join(d for d in directories if d in SYSTEM_PATH or Path(d).is_dir())
    return env


CVMFS = re.compile(r"/cvmfs/([A-Za-z0-9._-]+)")


def pin_mounts(env: dict[str, str], config: Config) -> list[int]:
    """Mount and hold the CVMFS repositories a turn may use, from outside the sandbox.

    CVMFS repositories are automounted on first access and unmounted after a short idle
    time. Neither can happen inside the sandbox's mount namespace, where an unmounted
    repository reads as "Operation not permitted". An open directory keeps the mount busy
    for the whole turn; close the returned descriptors afterwards.
    """
    if not Path("/cvmfs").is_dir():
        return []
    names = set(
        CVMFS.findall(" ".join([*env.values(), *config.verify.path, *config.verify.setup.values()]))
    )
    try:
        mounted = Path("/proc/mounts").read_text()
    except OSError:
        mounted = ""
    names |= {m.group(1) for m in re.finditer(r"^cvmfs2 /cvmfs/([^ ]+) ", mounted, re.M)}
    descriptors = []
    for name in sorted(names):
        try:
            descriptors.append(os.open(f"/cvmfs/{name}", os.O_RDONLY | os.O_DIRECTORY))
        except OSError:
            continue  # A repository that does not exist or cannot mount stays unavailable.
    return descriptors
