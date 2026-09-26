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


# Worker commits are the user's own: no co-author trailer, Anthropic address or Claude
# attribution line. A message may still name files such as CLAUDE.md.
ATTRIBUTION = re.compile(
    r"co-authored-by|anthropic\.com|claude\.(?:ai|com)|claude code|(?:generated|written|authored)\s+(?:with|by)\s+\S*\s*claude",
    re.IGNORECASE,
)

# Commands that run another command out of the guard's sight, or change privileges.
INDIRECT = {
    "sudo",
    "su",
    "doas",
    "pkexec",
    "env",
    "nohup",
    "setsid",
    "timeout",
    "nice",
    "ionice",
    "stdbuf",
    "time",
    "xargs",
    "exec",
    "command",
    "builtin",
    "eval",
    "source",
    ".",
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "fish",
    "csh",
    "tcsh",
    "script",
    "watch",
    "strace",
    "ltrace",
    "gdb",
    "chroot",
    "unshare",
    "nsenter",
    "systemd-run",
    "at",
    "batch",
    "crontab",
}

# Never readable, wherever they are: credentials and other people's secrets.
SECRET_NAMES = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".docker",
    ".kube",
    ".password-store",
    ".envrc",
    ".env",
}
# Committed templates such as .env.example hold no secrets; other .env.* files may.
ENV_TEMPLATES = (".example", ".sample", ".template", ".dist", ".defaults")
# Secret locations in the home directory (anchored there, so a repository's own
# Library/ or config/ stays usable).
SECRET_HOME = (
    ".config",
    ".claude",
    ".claude.json",
    ".lxreview",
    "Library",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
    ".gradle/gradle.properties",
    ".terraform.d",
    ".azure",
    ".local/share/keyrings",
    ".mozilla",
    ".thunderbird",
    ".pki",
    ".bash_history",
    ".zsh_history",
    ".python_history",
    ".node_repl_history",
    ".psql_history",
    ".mysql_history",
    ".lesshst",
    ".viminfo",
)
# Readable, but never written by the worker: Git metadata and files that make tools run
# code or change behavior later, outside the sandbox.
CONFIG_NAMES = {
    ".git",
    ".gitmodules",
    ".gitconfig",
    ".lfsconfig",
    ".claude",
    ".codex",
    ".lxreview",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
}
PROTECTED = SECRET_NAMES | CONFIG_NAMES
# Commands that modify their path arguments; those must stay inside the repository.
MODIFYING = {
    "rm",
    "rmdir",
    "unlink",
    "mv",
    "cp",
    "ln",
    "install",
    "rsync",
    "chmod",
    "chown",
    "chgrp",
    "touch",
    "mkdir",
    "truncate",
    "shred",
    "dd",
    "tee",
}
GIT_READ = {
    "status",
    "diff",
    "show",
    "log",
    "rev-parse",
    "rev-list",
    "blame",
    "ls-files",
    "grep",
    "describe",
    "merge-base",
    "cat-file",
    "shortlog",
}


def secret_locations(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / name for name in SECRET_HOME]


def secret_name(part: str) -> bool:
    if part in SECRET_NAMES:
        return True
    return part.startswith(".env.") and not part.endswith(ENV_TEMPLATES)


def resolve(value: str, repo: Path) -> tuple[Path, Path]:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    return candidate, candidate.resolve()


def secret(value: str, repo: Path) -> bool:
    candidate, resolved = resolve(value, repo)
    if any(secret_name(p) for p in (*candidate.parts, *resolved.parts)):
        return True
    return any(
        path.is_relative_to(location)
        for path in (candidate, resolved)
        for location in secret_locations()
    )


def readable(value: str, repo: Path) -> bool:
    """Anything but secrets, and Git internals (remote URLs can hold credentials)."""
    candidate, resolved = resolve(value, repo)
    inside = resolved.is_relative_to(repo.resolve())
    return not secret(value, repo) and not (inside and ".git" in resolved.parts)


def safe_path(value: str, repo: Path) -> bool:
    """Writable by the worker: inside the repository, no secret and no protected config."""
    candidate, resolved = resolve(value, repo)
    return (
        resolved.is_relative_to(repo.resolve())
        and not secret(value, repo)
        and not any(p in CONFIG_NAMES for p in (*candidate.parts, *resolved.parts))
    )


def shell_unsafe(command: str) -> bool:
    """Composition, redirection, expansion or substitution the shell would act on.

    Text in single quotes is literal to the shell, so test selectors such as
    'test_x.py::test_y[param]' or 'not (slow or gpu)' stay usable there; double quotes
    still expand $, backticks and backslashes.
    """
    if "\n" in command or "\r" in command:
        return True
    quote = None
    for character in command:
        if quote == "'":
            quote = None if character == "'" else quote
        elif quote == '"':
            if character == '"':
                quote = None
            elif character in "$`\\":
                return True
        elif character in "'\"":
            quote = character
        elif character in ";|&`$><\\*?[]{}()!":
            return True
    return quote is not None


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
    if tool in ("Write", "Edit", "MultiEdit"):
        name = data.get("file_path", "")
        return (
            isinstance(name, str) and bool(name) and safe_path(name, repo),
            "Edits must stay within the repository and outside protected paths",
        )
    if tool in ("Read", "Glob", "Grep"):
        name = data.get("file_path", data.get("path", "."))
        ok = isinstance(name, str) and readable(name, repo)
        if tool in ("Glob", "Grep"):
            pattern = data.get("pattern", "")
            ok = (
                ok
                and isinstance(pattern, str)
                and not pattern.startswith("/")
                and ".." not in pattern
                and not any(x in pattern for x in PROTECTED)
            )
        return ok, "Secrets and Git internals are not readable"
    if tool != "Bash":
        return (
            False,
            "Tool not available to autonomous workers (including subagents and external MCP tools)",
        )
    if data.get("dangerouslyDisableSandbox") or data.get("run_in_background"):
        return False, "Workers cannot bypass the sandbox or detach tool processes"
    command = data.get("command", "")
    if not isinstance(command, str) or shell_unsafe(command):
        return (
            False,
            "Use a single literal command without shell expansion, redirection, or composition;"
            " put patterns in single quotes",
        )
    try:
        args = shlex.split(command)
    except ValueError:
        return False, "Malformed command"
    # `NAME=value cmd` runs cmd: judge the command itself, keeping the assignments harmless.
    while args and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", args[0]):
        if args[0].split("=", 1)[0].upper().startswith(("GIT_", "LD_", "DYLD_", "PATH")):
            return False, "Git, loader and PATH variables cannot be set for worker commands"
        args = args[1:]
    if not args:
        return False, "Empty command"
    exe = args[0]
    if Path(exe).name == "git":
        # /usr/bin/git and friends get the same Git rules as plain git.
        exe = args[0] = "git"
    # `git -C <repo>` is plain git in the repository; any other directory stays refused.
    if (
        exe == "git"
        and len(args) > 2
        and args[1] == "-C"
        and Path(args[2]).expanduser().resolve() == repo.resolve()
    ):
        args = [exe, *args[3:]]
    # Any project tooling may run (tests, type checkers, linters, builds): it executes
    # repository code no more than a test suite does, and the sandbox confines it (no
    # network, writes only inside the repository, .git and secrets protected). Refused are
    # privilege changes and wrappers that would run a command this guard never sees.
    if Path(exe).name in INDIRECT:
        return False, "Run the command directly, without a shell, wrapper or privilege change"
    if phase == "publish" and exe != "git":
        return False, "Publication permits only Git commands"
    if exe == "git":
        if any(
            x in command.lower() for x in ("--no-verify", "--force", "--hard", "core.hookspath")
        ):
            return False, "Destructive git operations and hook bypass are blocked"
        if len(args) < 2 or args[1] not in (
            GIT_READ | ({"add", "commit"} if phase == "publish" else set())
        ):
            if len(args) > 1 and args[1] == "push":
                return False, "LXReview pushes the commit itself after this turn"
            if len(args) > 1 and args[1] in ("add", "commit"):
                return False, "Git mutation is restricted to the publication phase"
            return False, "Git operation is not allowed"
        if args[1] == "commit":
            messages, rest = [], args[2:]
            while rest:
                if rest[0] == "-m" and len(rest) > 1:
                    messages.append(rest[1])
                    rest = rest[2:]
                elif rest[0] in ("-s", "--signoff", "--no-gpg-sign"):
                    rest = rest[1:]
                else:
                    return False, "Use git commit -m <message> [-m <body>] [-s] [--no-gpg-sign]"
            if not messages:
                return False, "Use git commit -m <message> [-m <body>] [-s] [--no-gpg-sign]"
            if any(ATTRIBUTION.search(m) for m in messages):
                return False, "Commit messages carry no co-author trailer or Claude attribution"
            return True, ""
        if args[1] == "add":
            return len(args) > 2 and all(
                not x.startswith(("-", ":")) and safe_path(x, repo) and not (repo / x).is_dir()
                for x in args[2:]
            ), "Stage explicit repository paths"
        if any(
            x.startswith(
                ("--output", "--ext-diff", "--textconv", "--exec", "--open-files-in-pager")
            )
            or x == "-O"
            for x in args[2:]
        ):
            return False, "External git execution/output options are blocked"
    # Compare whole path components (also inside rev:path and --opt=path) so that
    # .gitignore, .github/ or test_credentials.py are not mistaken for .git or secrets.
    parts = [part for arg in args for part in re.split(r"[/:=]", arg)]
    if any(secret_name(part) or part in CONFIG_NAMES for part in parts):
        return False, "Secrets and protected configuration paths are blocked"
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
        if not value or value.startswith("-") or "/" not in value and not value.startswith("~"):
            continue
        if secret(value, repo):
            return False, "Secrets are not readable"
        if Path(exe).name in MODIFYING and not safe_path(value, repo):
            return False, "Files outside the repository cannot be modified"
    if Path(exe).name in MODIFYING and any(
        not x.startswith("-") and not safe_path(x, repo) for x in args[1:]
    ):
        return False, "Files outside the repository cannot be modified"
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
