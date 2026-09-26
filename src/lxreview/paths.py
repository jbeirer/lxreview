import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import Category, LXError


def private_dir(path: Path) -> Path:
    if any(p.is_symlink() and (p == path or p.lstat().st_uid != 0) for p in (path, *path.parents)):
        raise LXError(Category.UNSAFE, f"Refusing symlink directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    # Exporting a report must not chmod the user's existing destination directory.
    if not path.parent.exists():
        private_dir(path.parent)
    elif path.parent.is_symlink():
        raise LXError(Category.UNSAFE, "Refusing symlink output directory")
    fd, name = tempfile.mkstemp(prefix=".lxreview-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def append_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
        stream.flush()


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, indent=2) + "\n")


@contextlib.contextmanager
def lock(path: Path, *, blocking: bool = False) -> Iterator[None]:
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LXError(Category.BUSY, "Another LXReview operation owns this resource") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Paths:
    root: Path

    def __post_init__(self):
        object.__setattr__(self, "root", Path(os.path.abspath(self.root.expanduser())))

    @classmethod
    def default(cls) -> "Paths":
        return cls(Path(os.environ.get("LXREVIEW_HOME", "~/.lxreview")).expanduser().absolute())

    @property
    def config(self) -> Path:
        return self.root / "config/config.toml"

    @property
    def executable(self) -> Path:
        return self.root / "bin/lxreview"

    def ensure(self) -> None:
        if self.root == Path.home() or self.root in Path.home().parents or self.root == Path("/"):
            raise LXError(Category.UNSAFE, "Use a dedicated installation directory")
        private_dir(self.root)
        for part in (
            "bin",
            "config",
            "runtime",
            "state",
            "state/runs",
            "state/services",
            "state/browser",
            "state/bridge",
            "state/vnc",
            "secrets",
            "logs",
            "cache",
            "downloads",
            "versions",
            "claude/commands",
        ):
            private_dir(self.root / part)
