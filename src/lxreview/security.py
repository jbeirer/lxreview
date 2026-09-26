"""Fail-closed PreToolUse policy; not an OS sandbox for untrusted repository code."""

import json
import re
import shlex
import sys
from pathlib import Path

SECRET_KEYS = re.compile(r"(?i)(authorization|cookie|password|token|secret|api[_-]?key)")
SECRET_VALUE = re.compile(
    r"(?i)(?:Bearer|Basic)\s+\S+|(?:sk-ant-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}"
    r"|[\"']?(?:[a-z0-9_]*secret[a-z0-9_]*|[a-z0-9_]*token[a-z0-9_]*|password|api[_-]?key)[\"']?\s*[=:]\s*[\"']?[^\s,;\"']+"
    r"|https?://[^/\s:@]+:[^/\s@]+@"
)
PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if SECRET_KEYS.search(k) else redact(v)
            for k, v in value.items()
            if k not in ("thinking", "signature")
        }
    if isinstance(value, list):
        return [
            redact(v)
            for v in value
            if not isinstance(v, dict) or v.get("type") not in ("thinking", "redacted_thinking")
        ]
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value))
    return value


PROTECTED = {
    ".git",
    ".ssh",
    ".aws",
    ".config",
    ".claude",
    ".codex",
    ".lxreview",
    ".env",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".profile",
    "Cookies",
    "Login Data",
    "Local State",
    "Library",
    ".netrc",
    ".npmrc",
    ".gitconfig",
    ".gitmodules",
    ".git-credentials",
    ".gnupg",
}


def safe_path(value: str, repo: Path) -> bool:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    resolved = candidate.resolve()
    return resolved.is_relative_to(repo.resolve()) and not any(
        p in PROTECTED or p.startswith(".env.") for p in (*candidate.parts, *resolved.parts)
    )


def allowed(event: dict, repo: Path, phase: str = "edit") -> tuple[bool, str]:
    tool, data = event.get("tool_name"), event.get("tool_input", {})
    if phase not in ("evaluate", "edit", "publish"):
        return False, "Unknown worker phase"
    if phase == "evaluate" and tool not in ("Read", "Glob", "Grep", "StructuredOutput"):
        return False, "Evaluation is read-only"
    if phase == "publish" and tool in ("Write", "Edit", "MultiEdit"):
        return False, "Publication cannot edit files"
    if not isinstance(data, dict):
        return False, "Invalid tool input"
    if event.get("cwd") and Path(event["cwd"]).resolve() != repo.resolve():
        return False, "Worker working directory changed"
    if tool == "StructuredOutput":
        return True, ""  # Claude's schema result tool performs no filesystem or shell operation.
    if tool in ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"):
        name = data.get("file_path", data.get("path", "."))
        ok = isinstance(name, str) and safe_path(name, repo)
        if tool in ("Glob", "Grep"):
            pattern = data.get("pattern", "")
            ok = (
                ok
                and ".." not in pattern
                and not pattern.startswith("/")
                and not any(x in pattern for x in PROTECTED)
            )
        return ok, "File access must stay within the repository and outside protected paths"
    if tool != "Bash":
        return (
            False,
            "Tool not available to autonomous workers (including subagents and external MCP tools)",
        )
    if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
        return False, "Workers cannot bypass the sandbox or detach tool processes"
    command = data.get("command", "")
    if not isinstance(command, str) or any(
        x in command
        for x in (
            "\n",
            "\r",
            ";",
            "|",
            "&",
            "`",
            "$",
            ">",
            "<",
            "\\",
            "*",
            "?",
            "[",
            "]",
            "{",
            "}",
            "(",
            ")",
            "!",
        )
    ):
        return (
            False,
            "Use a single literal command without shell expansion, redirection, or composition",
        )
    try:
        args = shlex.split(command)
    except ValueError:
        return False, "Malformed command"
    if not args:
        return False, "Empty command"
    if any(x in command.lower() for x in ("--no-verify", "--force", "--hard", "core.hookspath")):
        return False, "Destructive git operations and hook bypass are blocked"
    exe = args[0]
    if exe in (
        ".venv/bin/python",
        ".venv/bin/python3",
        str(repo / ".venv/bin/python"),
        str(repo / ".venv/bin/python3"),
    ):
        exe = "python"
    if exe not in ("git", "pytest", "python", "python3", "rg", "ls", "cat"):
        return False, "Command is outside the worker allowlist"
    if phase == "publish" and exe != "git":
        return False, "Publication permits only Git commands"
    if exe == "git":
        if phase != "publish" and len(args) > 1 and args[1] in ("add", "commit", "push"):
            return False, "Git mutation is restricted to the publication phase"
        if len(args) < 2 or args[1] not in (
            "status",
            "diff",
            "show",
            "log",
            "rev-parse",
            "add",
            "commit",
            "push",
        ):
            return False, "Git operation is not allowed"
        if args[1] == "push":
            if args != ["git", "push"]:
                return False, "Only a normal push to the existing upstream is allowed"
            from .git import Repository
            from .paths import Paths

            try:
                Repository(repo, Paths.default()).check_push_policy()
            except Exception:
                return False, "Unsafe Git push configuration; run preflight"
            return True, ""
        if args[1] == "commit":
            return len(args) == 4 and args[
                2
            ] == "-m", "Only git commit -m with a literal message is allowed"
        if args[1] == "add":
            return len(args) > 2 and all(
                not x.startswith(("-", ":")) and safe_path(x, repo) and not (repo / x).is_dir()
                for x in args[2:]
            ), "Stage explicit repository paths"
        if args[1] in ("diff", "show", "log") and any(
            x.startswith(("--output", "--ext-diff", "--textconv", "--exec")) for x in args[2:]
        ):
            return False, "External git execution/output options are blocked"
    if exe in ("python", "python3"):
        if args[1:3] != ["-m", "pytest"]:
            return False, "Python is allowed only as python -m pytest"
    # Compare whole path components (also inside rev:path and --opt=path) so that
    # .gitignore, .github/ or test_credentials.py are not mistaken for .git or secrets.
    if ".." in args or any(
        part in PROTECTED or part.startswith(".env")
        for arg in args[1:]
        for part in re.split(r"[/:=]", arg)
    ):
        return False, "Protected paths and parent directory access are blocked"
    if (
        exe == "rg"
        and any(
            x.startswith(
                (
                    "--pre",
                    "--hostname-bin",
                    "--no-ignore",
                    "--hidden",
                    "--unrestricted",
                    "--follow",
                    "--search-zip",
                    "--file",
                )
            )
            # Short options cluster (-iL, -.): reject any cluster containing a blocked flag.
            or (x.startswith("-") and not x.startswith("--") and set(x[1:]) & set("uLzf."))
            for x in args[1:]
        )
    ):
        return False, "Ripgrep external commands and hidden-file scanning are blocked"
    for arg in args[1:]:
        value = arg.split("=", 1)[-1] if arg.startswith("-") else arg
        if not value.startswith("-") and not safe_path(value, repo):
            return False, "External path access is blocked"
    return True, ""


def hook(repo: Path, phase: str = "edit") -> int:
    try:
        event = json.load(sys.stdin)
        ok, reason = allowed(event, repo, phase)
    except Exception:
        ok, reason = False, "Invalid guardrail input"
    if not ok:
        print(reason, file=sys.stderr)
        return 2
    return 0
