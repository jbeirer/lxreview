import hashlib
import json
import os
import platform
import tarfile
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path

import httpx

from ..config import Config
from ..errors import Category, LXError
from ..paths import Paths, atomic_write, private_dir


def platform_key() -> str:
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    key = f"{system}-{arch}"
    if key not in ("linux-x64", "darwin-x64", "darwin-arm64"):
        raise LXError(Category.CONFIG, f"No verified private runtime for {key}")
    return key


def download(url: str, digest: str, destination: Path) -> None:
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return
    private_dir(destination.parent)
    temporary = destination.with_suffix(".part")
    try:
        checksum = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with temporary.open("wb") as out:
                for chunk in response.iter_bytes():
                    checksum.update(chunk)
                    out.write(chunk)
        if checksum.hexdigest() != digest:
            raise LXError(
                Category.UNSAFE, "Runtime archive checksum mismatch; installation aborted"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def extract(archive: Path, destination: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise LXError(Category.UNSAFE, "Unsafe archive member")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    link = bundle.read(member).decode()
                    if Path(link).is_absolute() or not (
                        target.parent / link
                    ).resolve().is_relative_to(destination.resolve()):
                        raise LXError(Category.UNSAFE, "Unsafe archive symlink")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(link)
                    continue
                bundle.extract(member, destination)
                mode = member.external_attr >> 16
                if mode and target.is_file():
                    target.chmod(mode & 0o777)
    else:
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")


def install_runtimes(paths: Paths, config: Config) -> None:
    """Install or verify the pinned Chrome unless setup chose an external executable."""
    destination = paths.root / "runtime/chrome"
    if config.browser.chrome and not Path(config.browser.chrome).is_relative_to(destination):
        return
    item = json.loads(files("lxreview.resources").joinpath("runtime-manifest.json").read_text())[
        platform_key()
    ]["chrome"]
    marker = destination / ".lxreview-sha256"
    native = destination / (
        "chrome"
        if platform.system() == "Linux"
        else "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )
    installed = (
        marker.exists()
        and marker.read_text() == item["sha256"]
        and native.is_file()
        and os.access(native, os.X_OK)
    )
    if not installed:
        archive = paths.root / "downloads" / item["url"].rsplit("/", 1)[1]
        download(item["url"], item["sha256"], archive)
        with tempfile.TemporaryDirectory(dir=paths.root / "runtime") as tmp:
            stage = Path(tmp)
            extract(archive, stage)
            children = list(stage.iterdir())
            if len(children) != 1:
                raise LXError(Category.PROTOCOL, "Unexpected runtime archive layout")
            old = None
            if destination.exists():
                retained = marker.read_text()[:12] if marker.exists() else "unknown"
                old = paths.root / "versions" / f"chrome-{retained}"
                if old.exists():
                    raise LXError(
                        Category.CONFIG, "Old version already retained; run cleanup before updating"
                    )
                destination.rename(old)
            try:
                children[0].rename(destination)
            except OSError:
                if old is not None:
                    old.rename(destination)
                raise
            atomic_write(marker, item["sha256"])
    config.browser.chrome = str(native)
    config.save(paths)
