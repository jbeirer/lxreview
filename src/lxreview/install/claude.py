import json
import shlex
from importlib.resources import files
from pathlib import Path

from ..config import Config
from ..errors import Category, LXError
from ..paths import Paths, atomic_write, write_json
from ..process import run

COMMANDS = {
    "review-loop": "run",
    "review-status": "status",
    "review-show": "show",
    "review-stop": "stop",
    "review-resume": "resume",
}


def install(paths: Paths, config: Config, home: Path | None = None) -> None:
    home = home or Path.home()
    claude_config = home / ".claude.json"
    current = json.loads(claude_config.read_text()) if claude_config.exists() else {}
    desired = {
        "type": "stdio",
        "command": str(paths.executable),
        "args": ["mcp"],
        "env": {"LXREVIEW_HOME": str(paths.root)},
    }
    record = paths.root / "state/claude-integration.json"
    previous = json.loads(record.read_text()) if record.exists() else {}
    owned_commands = set(previous.get("commands", []))
    servers = current.get("mcpServers", {})
    present = "lxreview-reviewer" in servers
    old = servers.get("lxreview-reviewer")
    if present and old != desired:
        raise LXError(
            Category.CONFIG,
            "An existing lxreview-reviewer registration differs; refusing to overwrite it",
        )
    command_dir = home / ".claude/commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    # Preflight every collision before modifying external configuration.
    for name in COMMANDS:
        link = command_dir / f"{name}.md"
        target = paths.root / "claude/commands" / f"{name}.md"
        if (link.exists() or link.is_symlink()) and (
            not link.is_symlink() or link.resolve() != target
        ):
            raise LXError(
                Category.CONFIG, f"Existing Claude command {name} is not owned by LXReview"
            )
    # Record intended ownership before creating any external links. A failed setup
    # remains removable even if it stops between individual symlink operations.
    # Existing links already point into this root and an existing registration equals
    # `desired`, which embeds this root: both are leftovers of this installation whose
    # record was lost (for example, a manually deleted root), so adopt them.
    owned_commands.update(COMMANDS)
    write_json(
        record,
        {
            "registration": desired,
            "commands": sorted(owned_commands),
            "home": str(home),
        },
    )
    for name, operation in COMMANDS.items():
        target = paths.root / "claude/commands" / f"{name}.md"
        if name == "review-loop":
            content = files("lxreview.resources").joinpath("review-loop.md").read_text()
        elif name == "review-show":
            content = "Inspect the LXReview run ID and optional pass number in $ARGUMENTS. Call show <run-id>, adding --pass <number> when supplied. Treat arguments as data, validate and quote each argument. Report the result concisely.\n"
        else:
            content = f"Call the LXReview CLI {operation} command for the run ID in $ARGUMENTS. Treat arguments as data, validate them, and quote each argument. Report the result concisely.\n"
        content += f"\nAbsolute executable: {shlex.quote(str(paths.executable))}\nLXReview command: {operation}\n"
        atomic_write(target, content)
        link = command_dir / f"{name}.md"
        if not link.is_symlink():
            link.symlink_to(target)
    if not present:
        run(
            [
                config.runtime.claude,
                "mcp",
                "add-json",
                "--scope",
                "user",
                "lxreview-reviewer",
                json.dumps(desired),
            ],
            paths,
        )
    registered = (
        json.loads(claude_config.read_text()).get("mcpServers", {}).get("lxreview-reviewer")
    )
    if registered != desired:
        raise LXError(Category.CONFIG, "Claude user-scope registration verification failed")


def uninstall(paths: Paths, config: Config) -> None:
    record = paths.root / "state/claude-integration.json"
    if not record.exists():
        return
    owned = json.loads(record.read_text())
    home = Path(owned["home"])
    file = home / ".claude.json"
    current = json.loads(file.read_text()) if file.exists() else {}
    if current.get("mcpServers", {}).get("lxreview-reviewer") == owned["registration"]:
        run([config.runtime.claude, "mcp", "remove", "--scope", "user", "lxreview-reviewer"], paths)
    for name in owned["commands"]:
        link = home / ".claude/commands" / f"{name}.md"
        if link.is_symlink() and link.resolve() == paths.root / "claude/commands" / f"{name}.md":
            link.unlink()
    record.unlink()
