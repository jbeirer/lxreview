import hashlib
import json
import os
import platform
import secrets
import tarfile
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path

import httpx

from ..config import AGENTIFY_VERSION, Config
from ..errors import Category, LXError
from ..paths import Paths, atomic_write, private_dir, write_json
from ..process import environment, run


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
    manifest = json.loads(
        files("lxreview.resources").joinpath("runtime-manifest.json").read_text()
    )[platform_key()]
    for name in ("node", "chrome"):
        managed_chrome = paths.root / "runtime/chrome"
        if (
            name == "chrome"
            and config.browser.chrome
            and not Path(config.browser.chrome).is_relative_to(managed_chrome)
        ):
            continue
        item = manifest[name]
        destination = paths.root / "runtime" / name
        marker = destination / ".lxreview-sha256"
        relative_binary = (
            "bin/node"
            if name == "node"
            else (
                "chrome"
                if platform.system() == "Linux"
                else "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
        native = destination / relative_binary
        if (
            marker.exists()
            and marker.read_text() == item["sha256"]
            and native.is_file()
            and os.access(native, os.X_OK)
        ):
            continue
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
                old = (
                    paths.root
                    / "versions"
                    / f"{name}-{marker.read_text()[:12] if marker.exists() else 'unknown'}"
                )
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
    if not config.browser.chrome:
        chrome = paths.root / "runtime/chrome"
        config.browser.chrome = str(
            chrome
            / (
                "chrome"
                if platform.system() == "Linux"
                else "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            )
        )
    install_agentify(paths)
    config.save(paths)


def install_agentify(paths: Paths) -> None:
    with tempfile.TemporaryDirectory(dir=paths.root / "runtime") as directory:
        stage = private_dir(Path(directory) / "agentify")
        _install_agentify(paths, stage)
        destination = paths.root / "runtime/agentify"
        previous = paths.root / "versions" / ("agentify-" + secrets.token_hex(6))
        if destination.exists():
            destination.rename(previous)
        try:
            stage.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    native = (
        destination
        / "node_modules/electron"
        / (
            "dist/electron"
            if platform.system() == "Linux"
            else "dist/Electron.app/Contents/MacOS/Electron"
        )
    )
    write_json(
        paths.root / "state/runtime.json",
        {
            "agentify": AGENTIFY_VERSION,
            "electron": str(native),
            "node": str(paths.root / "runtime/node/bin/node"),
            "platform": platform_key(),
        },
    )


def _install_agentify(paths: Paths, prefix: Path) -> None:
    node = paths.root / "runtime/node/bin/node"
    npm = paths.root / "runtime/node/lib/node_modules/npm/bin/npm-cli.js"
    # The package-lock fixes transitive dependencies as well as the Agentify release.
    for name in ("package.json", "package-lock.json"):
        atomic_write(
            prefix / name, files("lxreview.resources").joinpath("agentify-" + name).read_text()
        )
    env = environment(paths, desktop=True)
    env["PATH"] = str(node.parent) + os.pathsep + env["PATH"]
    result = run(
        [str(node), str(npm), "ci", "--no-audit", "--no-fund"],
        paths,
        cwd=prefix,
        env=env,
        timeout=600,
        check=False,
    )
    if result.returncode:
        result = run(
            [str(node), str(npm), "ci", "--no-audit", "--no-fund"],
            paths,
            cwd=prefix,
            env=env,
            timeout=600,
            check=False,
        )
    if result.returncode:
        # The locked graph has only one lifecycle script: Electron's downloader.
        # Rebuild a complete, integrity-checked tree without that script, then repair
        # the native runtime explicitly below. Never certify a failed npm tree.
        result = run(
            [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            paths,
            cwd=prefix,
            env=env,
            timeout=600,
            check=False,
        )
    electron = prefix / "node_modules/electron"
    native = electron / (
        "dist/electron"
        if platform.system() == "Linux"
        else "dist/Electron.app/Contents/MacOS/Electron"
    )
    if result.returncode:
        raise LXError(
            Category.UNAVAILABLE,
            "Locked npm installation and script-free recovery failed; previous runtime retained",
        )
    if not native.exists():
        # Upstream's installer verifies the release archive against SHASUMS256.
        run([str(node), str(electron / "install.js")], paths, cwd=electron, env=env, timeout=600)
    if not native.is_file():
        raise LXError(
            Category.UNAVAILABLE,
            "Electron host runtime repair failed; inspect runtime dependencies with doctor",
        )
    run(
        [
            str(node),
            "--input-type=module",
            "-e",
            "await import('@modelcontextprotocol/sdk/server/mcp.js'); await import('zod');",
        ],
        paths,
        cwd=prefix,
        env=env,
    )
    host_env = {**env, "ELECTRON_RUN_AS_NODE": "1"}
    run([str(native), "-e", "process.stdout.write(process.versions.electron)"], paths, env=host_env)
    package = json.loads((prefix / "node_modules/@agentify/desktop/package.json").read_text())
    if package["version"] != AGENTIFY_VERSION:
        raise LXError(Category.CONFIG, "Installed Agentify version differs from compatibility pin")
