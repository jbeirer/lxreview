import json
import shlex
from pathlib import Path

import pytest

from lxreview.config import Config
from lxreview.contracts import ReviewRequest, Verdict, parse_response
from lxreview.errors import LXError
from lxreview.paths import atomic_write, lock
from lxreview.process import environment
from lxreview.reviewer import prompt_for
from lxreview.security import allowed, redact


@pytest.mark.parametrize(
    "raw,verdict",
    [
        ("VERDICT: CLEAN", Verdict.CLEAN),
        ("SUBSTANTIAL [S1] Issue\nVERDICT: SUBSTANTIAL_ISSUES\n", Verdict.SUBSTANTIAL),
        ("No access\nVERDICT: ACCESS_FAILED", Verdict.ACCESS_FAILED),
        ("VERDICT: CLEAN\nmore", Verdict.INVALID),
        ("VERDICT: CLEAN\nVERDICT: CLEAN", Verdict.INVALID),
        ("I think clean", Verdict.INVALID),
        ("", Verdict.INVALID),
    ],
)
def test_verdict_fail_closed(raw, verdict):
    response = parse_response(raw)
    assert response.raw == raw
    assert response.verdict == verdict
    assert bool(response.failure) == (verdict in (Verdict.INVALID, Verdict.ACCESS_FAILED))


def test_single_line_full_review():
    request = ReviewRequest(
        target="https://github.com/org/repo/pull/1",
        head_sha="a" * 40,
        rubric="correctness\r\nregressions",
    )
    prompt = prompt_for(request)
    assert "\r" not in prompt and "\n" not in prompt
    assert request.target in prompt and request.head_sha in prompt
    assert "complete current diff" in prompt and "ACCESS_FAILED" in prompt
    assert "conversation" in prompt and "already settled" in prompt


def test_gitlab_review_points_at_plain_text_views():
    sha = "b" * 40
    request = ReviewRequest(
        target="https://gitlab.cern.ch/atlas/sub/athena/-/merge_requests/7", head_sha=sha
    )
    prompt = prompt_for(request)
    assert "\n" not in prompt and "merge request" in prompt and "complete MR" in prompt
    assert "https://gitlab.cern.ch/atlas/sub/athena/-/merge_requests/7.diff" in prompt
    assert f"https://gitlab.cern.ch/atlas/sub/athena/-/tree/{sha}" in prompt
    assert "If the MR's conversation is accessible" in prompt
    github = prompt_for(ReviewRequest(target="https://github.com/o/r/pull/1", head_sha=sha))
    assert ".diff" not in github and "complete PR at this head" in github


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/1",
        "https://gitlab.com/group/project/-/merge_requests/3",
        "https://gitlab.cern.ch/atlas/athena/-/merge_requests/91262",
        "https://gitlab.cern.ch/a/b.c/d-e/f_g/-/merge_requests/12",
        "https://gitlab.example.org/g/p/-/merge_requests/1",
    ],
)
def test_target_accepts_github_prs_and_gitlab_mrs(url):
    assert ReviewRequest(target=url, head_sha="a" * 40).target == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/o/r/pull/1",
        "https://github.com/o/r/pull/1;rm",
        "https://github.com/o/r/pull/1\n",
        "https://github.com/o/r/pull/0",
        "https://gitlab/g/p/-/merge_requests/1",
        "https://gitlab.example.org:8443/g/p/-/merge_requests/1",
        "https://user@gitlab.example.org/g/p/-/merge_requests/1",
        "https://-gitlab.example.org/g/p/-/merge_requests/1",
        "https://gitlab.example.org./g/p/-/merge_requests/1",
        "https://GitHub.com/g/p/-/merge_requests/1",
        "https://gitlab.cern.ch/g/p/merge_requests/1",
        "https://gitlab.cern.ch/p/-/merge_requests/1",
        "https://gitlab.cern.ch/g/../p/-/merge_requests/1",
        "https://gitlab.cern.ch/g/.p/-/merge_requests/1",
        "https://gitlab.cern.ch/g/p/-/merge_requests/0",
        "https://gitlab.cern.ch/g/p/-/merge_requests/1/diffs",
        "https://gitlab.cern.ch/g/p/-/merge_requests/1\n",
        "https://github.com/g/p/-/merge_requests/1",
        "https://gitlab.com/o/r/pull/1",
    ],
)
def test_target_validation(url):
    with pytest.raises(ValueError):
        ReviewRequest(target=url, head_sha="a" * 40)


def test_config_round_trip_and_unknown(paths):
    Config().save(paths)
    assert Config.load(paths).schema_version == 1
    paths.config.write_text("schema_version=1\nunknown=true\n")
    with pytest.raises(LXError):
        Config.load(paths)
    paths.config.write_text("schema_version=2\n")
    with pytest.raises(LXError, match="Unsupported schema"):
        Config.load(paths)


def test_mode_conflict(paths):
    config = Config(mode="local-browser")
    config.save(paths)
    with pytest.raises(LXError, match="placement"):
        Config.load(paths)


def test_permissions_and_atomic_replace(paths):
    destination = paths.root / "secrets/test"
    atomic_write(destination, "secret")
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    other = paths.root / "outside"
    other.write_text("unchanged")
    destination.unlink()
    destination.symlink_to(other)
    atomic_write(destination, "replacement")
    assert other.read_text() == "unchanged"
    assert not destination.is_symlink()


def test_cross_process_lock(paths):
    with lock(paths.root / "lock"):
        with pytest.raises(LXError, match="owns"):
            with lock(paths.root / "lock"):
                pass
    with lock(paths.root / "lock"):
        pass


def test_environment_does_not_leak(paths, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "/bad")
    monkeypatch.setenv("NODE_PATH", "/bad")
    env = environment(paths, desktop=True)
    assert not {"ANTHROPIC_API_KEY", "PYTHONPATH", "NODE_PATH"} & env.keys()
    assert env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"


def test_redaction_drops_hidden_reasoning():
    data = {
        "authorization": "secret",
        "content": [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "Bearer sensitive-token"},
        ],
        "nested": {"api_key": "secret"},
    }
    text = json.dumps(redact(data))
    assert "secret" not in text and "hidden" not in text and "sensitive-token" not in text


def test_redaction_keeps_usage_counts_but_not_token_values():
    usage = {"input_tokens": 3, "cache_read_input_tokens": 120, "cacheReadInputTokens": 120}
    data = {"usage": usage, "access_tokens": "private", "token": 7, "tokens": True}
    result = redact(data)
    assert result["usage"] == usage
    assert result["access_tokens"] == result["token"] == result["tokens"] == "[REDACTED]"


@pytest.mark.parametrize(
    "command",
    [
        "git push --force",
        "git push -f",
        "git push origin +HEAD",
        "git reset --hard",
        "git branch -D main",
        "git remote set-url origin x",
        "git -c core.hooksPath=/tmp commit",
        "git commit --no-verify",
        "rm -rf /tmp",
        "cat ~/.ssh/id_rsa",
        "cat .env",
        "bash -c 'git commit -m x'",
        "sh scripts/check.sh",
        "xargs rm",
        "sudo make test",
        "/usr/bin/git commit -m fix",
        "git -c core.hooksPath=x status",
        "cp src/a.py /tmp/a.py",
        "rm -rf ../other-repo",
        "cat ~/.config/gh/hosts.yml",
        "cat ~/.claude/.credentials.json",
        "cat ~/.docker/config.json",
        "cat .env.local",
        ".git/hooks/pre-commit",
        "X=1 git commit -m x",
        "A=1 bash scripts/x.sh",
        "FOO=1 sudo make",
        "GIT_DIR=elsewhere git status",
        "LD_PRELOAD=lib.so make test",
        "PATH=bin make test",
        "git status; rm file",
        "git status $(cat /etc/passwd)",
        "env git push",
        "sudo true",
        "git diff --output=/etc/foo",
        "git show HEAD:.env",
        "cat .envrc",
        "cat .git/config",
        "rg --unrestricted token",
        "rg -iL secret",
        "rg -. token",
    ],
)
def test_dangerous_commands_denied(tmp_path, command):
    assert not allowed({"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)[0]


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff",
        "git add src/code.py",
        "git commit -m 'Fix edge case'",
        "python -m pytest tests/test_example.py",
        "pytest tests",
        "rg pattern src",
        "rg -tpy -A2 pattern",
        "python -m pytest tests/test_credentials.py",
        "git diff -- src/cookie_jar.py",
        "cat .gitignore",
        "git commit -m 'Fix credential parsing'",
        "git commit -m 'Clarify CLAUDE.md wording'",
        "git add .github/workflows/ci.yml",
        "python -c 'import os'",
        ".venv/bin/mypy src",
        ".venv/bin/ruff check --output-format=concise .",
        "make -j8 test",
        "npm test",
        "cargo test --workspace",
        "ctest --test-dir build -j 8",
        "go test ./pkg/...",
        "cat /etc/os-release",
        "ls /cvmfs/sw.hsf.org",
        "cat ../other-repo/code",
        "cat .env.example",
        "ls Library/Assets",
        "pytest 'tests/test_x.py::test_y[param]' -k 'not (slow or gpu)'",
        "ctest -R '^unit$'",
        "CI=1 npm test",
    ],
)
def test_normal_work_allowed(tmp_path, command):
    assert allowed(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        tmp_path,
        "publish" if command.startswith(("git add", "git commit")) else "edit",
    )[0]


@pytest.mark.parametrize(
    "message",
    [
        "Fix efficiency weights Co-Authored-By: Claude noreply@anthropic.com",
        "Fix efficiency weights co-authored-by: someone",
        "Fix efficiency weights. Generated with Claude Code",
        "Fix efficiency weights, see https://claude.ai/code",
        "Fix efficiency weights, written by Claude",
    ],
)
def test_commit_attribution_denied(tmp_path, message):
    command = shlex.join(["git", "commit", "-m", message])
    ok, reason = allowed(
        {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path, "publish"
    )
    assert not ok and "attribution" in reason


def test_symlink_and_external_writes_denied(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link").symlink_to(tmp_path)
    for path in ("link/file", ".git/config", "../secret", ".claude/settings.json"):
        assert not allowed({"tool_name": "Write", "tool_input": {"file_path": path}}, repo)[0]
    assert not allowed({"tool_name": "Agent", "tool_input": {}}, repo)[0]


def test_scratch_directory_writable_only_when_the_worker_names_it(tmp_path):
    repo, scratch = tmp_path / "repo", tmp_path / "turn" / "work"
    repo.mkdir()
    scratch.mkdir(parents=True)
    write = {"tool_name": "Write", "tool_input": {"file_path": str(scratch / "probe.py")}}
    assert not allowed(write, repo)[0]
    assert allowed(write, repo, "edit", scratch)[0]
    for command in (f"mkdir {scratch}/site", f"touch {scratch}/x", f"cp README.md {scratch}/x"):
        event = {"tool_name": "Bash", "tool_input": {"command": command}}
        assert not allowed(event, repo)[0]
        assert allowed(event, repo, "edit", scratch)[0], command
    for path in (tmp_path / "turn" / "cache" / "x", scratch / ".git" / "config", tmp_path / "x"):
        event = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
        assert not allowed(event, repo, "edit", scratch)[0], path


def test_only_this_sessions_saved_tool_output_is_readable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    repo.mkdir()
    session, other = "74188205-2a75-4a1d-aaaa-85a4844100ee", "11111111-2a75-4a1d-aaaa-85a4844100ee"
    project = home / ".claude/projects/-repo"
    saved = project / session / "tool-results" / "blsgl8po9.txt"
    hook_input = {"session_id": session, "transcript_path": str(project / f"{session}.jsonl")}

    def event(tool, path, **extra):
        key = "file_path" if tool == "Read" else "path"
        return {"tool_name": tool, "tool_input": {key: str(path), **extra}, **hook_input}

    assert allowed(event("Read", saved), repo, "evaluate")[0]
    assert allowed(event("Grep", saved, pattern="^/usr/(local/)?bin:"), repo)[0]
    assert allowed(event("Glob", saved.parent, pattern="*.txt"), repo)[0]
    for path in (
        project / f"{session}.jsonl",
        project / other / "tool-results" / "x.txt",
        saved.parent / ".." / ".." / f"{session}.jsonl",
        home / ".claude.json",
    ):
        assert not allowed(event("Read", path), repo)[0], path
    # Without a matching transcript the directory stays as secret as the rest of ~/.claude.
    for forged in (
        {"transcript_path": str(project / f"{other}.jsonl")},
        {"transcript_path": f"{session}.jsonl"},
        {"session_id": "../../x", "transcript_path": str(project / "../../x.jsonl")},
        {"transcript_path": None},
    ):
        assert not allowed({**event("Read", saved), **forged}, repo)[0], forged
    # Commands stay unable to read it (the sandbox hides ~/.claude from them too).
    assert not allowed(
        {"tool_name": "Bash", "tool_input": {"command": f"cat {saved}"}, **hook_input}, repo
    )[0]


def test_rotating_log_redacts_credentials(paths):
    import logging

    from lxreview.observability import configure

    Config().save(paths)
    configure(paths, debug=True)
    logger = logging.getLogger("lxreview.test")
    logger.info("Authorization: Bearer sensitive-value")
    content = (paths.root / "logs/lxreview.jsonl").read_text()
    assert "sensitive-value" not in content
    assert (paths.root / "logs/lxreview.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "raw",
    [
        "Issue without IDs\nVERDICT: SUBSTANTIAL_ISSUES",
        "SUBSTANTIAL [S1] bug\nVERDICT: CLEAN",
        "SUBSTANTIAL [S1] a\nSUBSTANTIAL [S1] b\nVERDICT: SUBSTANTIAL_ISSUES",
        "SUBSTANTIAL [N1] bug\nVERDICT: SUBSTANTIAL_ISSUES",
        "SUBSTANTIAL [S1] a\n2. SUBSTANTIAL unparsed\nVERDICT: SUBSTANTIAL_ISSUES",
        "SUBSTANTIAL: cache never invalidated\nVERDICT: SUBSTANTIAL_ISSUES",
    ],
)
def test_inconsistent_or_unaddressable_findings_fail_closed(raw):
    assert parse_response(raw).verdict == Verdict.INVALID


@pytest.mark.parametrize(
    "raw,verdict,ids",
    [
        ("SUBSTANTIAL findings: none\nNON_BLOCKING: none\nVERDICT: CLEAN", Verdict.CLEAN, []),
        (
            "## SUBSTANTIAL issues\nNone.\n### NON_BLOCKING (1)\nNON_BLOCKING [N1] naming\nVERDICT: CLEAN",
            Verdict.CLEAN,
            ["N1"],
        ),
        (
            "1. SUBSTANTIAL [S1] off-by-one\n- **NON_BLOCKING [N1]** — naming\nVERDICT: SUBSTANTIAL_ISSUES",
            Verdict.SUBSTANTIAL,
            ["S1", "N1"],
        ),
    ],
)
def test_section_headings_and_markdown_findings_parse(raw, verdict, ids):
    response = parse_response(raw)
    assert response.verdict == verdict
    assert [f["id"] for f in response.findings] == ids
    assert all(f["title"] and not f["title"].startswith(("*", "—")) for f in response.findings)


@pytest.mark.parametrize(
    "command",
    [
        "cat *",
        "rm src/../../secret",
        "rm link/password",
        "rg --follow x link",
        "rg -f=../secret x",
        "cat {a,b}",
    ],
)
def test_shell_expansion_symlink_and_indirect_paths_denied(tmp_path, command):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "link").symlink_to(tmp_path)
    assert not allowed({"tool_name": "Bash", "tool_input": {"command": command}}, repo)[0]


def test_structured_output_is_allowed_without_enabling_other_tools(tmp_path):
    assert allowed({"tool_name": "StructuredOutput", "tool_input": {"findings": []}}, tmp_path)[0]
    assert not allowed({"tool_name": "NotebookEdit", "tool_input": {}}, tmp_path)[0]


def test_guard_restricts_each_phase_although_every_turn_lists_the_same_tools(tmp_path):
    def event(tool, **data):
        return {"tool_name": tool, "tool_input": data}

    write = {"file_path": str(tmp_path / "a.py"), "content": "x"}
    edit = {"file_path": str(tmp_path / "a.py"), "old_string": "x", "new_string": "y"}
    for tool, data in (("Bash", {"command": "ls"}), ("Write", write), ("Edit", edit)):
        assert not allowed(event(tool, **data), tmp_path, "evaluate")[0], tool
    assert allowed(event("Grep", pattern="x", path=str(tmp_path)), tmp_path, "evaluate")[0]
    for tool, data in (("Write", write), ("Edit", edit)):
        assert not allowed(event(tool, **data), tmp_path, "publish")[0], tool
        assert allowed(event(tool, **data), tmp_path, "edit")[0], tool


def test_atomic_export_preserves_existing_directory_permissions(tmp_path):
    tmp_path.chmod(0o755)
    atomic_write(tmp_path / "report.md", "report")
    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_private_root_refuses_nested_symlink(paths, tmp_path):
    from lxreview.paths import Paths

    outside = tmp_path / "outside"
    outside.mkdir()
    # Set the mode explicitly: the worker runs checks with umask 077.
    outside.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(LXError, match="symlink"):
        Paths(link / "root").ensure()
    assert outside.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "value,secret",
    [
        ('{"password": "hunter2"}', "hunter2"),
        ("AWS_SECRET_ACCESS_KEY=abc123", "abc123"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("https://user:private@github.com/repo", "private"),
        ("pushed with glpat-AbCdEf0123456789xyz", "AbCdEf0123456789xyz"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nprivatebytes\n-----END OPENSSH PRIVATE KEY-----",
            "privatebytes",
        ),
    ],
)
def test_known_secret_formats_redacted(value, secret):
    assert secret not in redact(value)


def test_desktop_environment_survives_supervision(paths, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setenv("XAUTHORITY", "/private/display-cookie")
    env = environment(paths)
    assert env["DISPLAY"] == ":7"
    assert env["XAUTHORITY"] == "/private/display-cookie"


def test_installation_root_is_normalized_before_safety_checks(tmp_path):
    from pathlib import Path

    from lxreview.paths import Paths

    paths = Paths(Path.home() / "unused" / "..")
    assert paths.root == Path.home()
    with pytest.raises(LXError, match="dedicated"):
        paths.ensure()


def test_browser_state_is_contained(paths):
    env = environment(paths, desktop=True)
    for name in ("PLAYWRIGHT_BROWSERS_PATH", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert env[name].startswith(str(paths.root) + "/")


@pytest.mark.parametrize(
    "phase,tool,data",
    [
        ("evaluate", "Write", {"file_path": "src/a.py"}),
        ("evaluate", "Bash", {"command": "git status"}),
        ("edit", "Bash", {"command": "git commit -m fix"}),
        ("edit", "Bash", {"command": "git push"}),
        ("publish", "Write", {"file_path": "src/a.py"}),
        ("publish", "Bash", {"command": "python -m pytest"}),
        ("publish", "Bash", {"command": "pytest tests"}),
    ],
)
def test_phase_boundaries_are_enforced_by_the_hook(tmp_path, phase, tool, data):
    assert not allowed({"tool_name": tool, "tool_input": data}, tmp_path, phase)[0]


async def test_reviewer_records_the_model_and_reasoning_that_answered():
    from lxreview.contracts import ReviewRequest
    from lxreview.reviewer import WebReviewer

    class Browser:
        choices = []

        async def new_conversation(self):
            pass

        async def ensure_ready(self):
            pass

        async def configure(self, model, reasoning_effort):
            self.choices.append((model, reasoning_effort))
            return {"chatgpt_model": "GPT-5.5", "chatgpt_reasoning": "high"}

        async def query(self, prompt, timeout):
            return "VERDICT: CLEAN"

    browser = Browser()
    response = await WebReviewer(browser, {"backend": "chatgpt-web"}).review(
        ReviewRequest(
            target="https://github.com/o/r/pull/1",
            head_sha="a" * 40,
            model="GPT-5.5",
            reasoning_effort="high",
        )
    )
    assert browser.choices == [("GPT-5.5", "high")]
    assert response.metadata == {
        "backend": "chatgpt-web",
        "chatgpt_model": "GPT-5.5",
        "chatgpt_reasoning": "high",
    }


@pytest.mark.parametrize(
    ("section", "values"),
    [
        ("reviewer", {"model": "GPT 5; rm"}),
        ("reviewer", {"reasoning_effort": "High!"}),
        ("worker", {"effort": "extreme"}),
        ("worker", {"model": "opus --dangerously"}),
    ],
)
def test_model_and_effort_settings_are_validated(section, values):
    from pydantic import ValidationError

    from lxreview.config import Config

    with pytest.raises(ValidationError):
        Config.model_validate({section: values})


def test_timeline_names_the_reviewer_model_and_reasoning():
    from lxreview.timeline import describe

    event = {
        "kind": "review_received",
        "time": "",
        "pass_number": 1,
        "verdict": "CLEAN",
        "model": "GPT-5.5",
        "reasoning": "high",
    }
    assert describe(event)[0].endswith("Reviewer pass 1 complete (GPT-5.5, high), CLEAN")


def test_timeline_summarizes_the_pr_discussion():
    from lxreview.timeline import describe

    event = {
        "kind": "discussion_read",
        "time": "",
        "comments": 3,
        "reviews": 1,
        "threads": 2,
        "unresolved": 1,
    }
    assert describe(event)[0].endswith(
        " Discussion read: 3 comments, 1 reviews, 2 review threads (1 unresolved)"
    )


def test_timeline_separates_preexisting_check_failures():
    from lxreview.timeline import describe

    event = {
        "kind": "tests_reported",
        "time": "",
        "passed": True,
        "tests": ["pytest: 1 failed, 40 passed"],
        "preexisting": ["test_io: fails identically before the edit"],
    }
    texts = [line.split("  ", 1)[-1].strip() for line in describe(event)]
    assert texts[0].endswith("Checks PASS (1 pre-existing failures)")
    assert "pre-existing: test_io: fails identically before the edit" in texts


def test_timeline_names_what_waits_for_approval_and_how_to_answer():
    from lxreview.timeline import PHASES, describe, style

    run = "lr-20261001-120000-0123abcd"
    commit = {
        "kind": "approval_requested",
        "time": "",
        "run_id": run,
        "step": "commit",
        "files": ["src/a.py", "tests/test_a.py"],
    }
    texts = [line.split("  ", 1)[-1] for line in describe(commit)]
    assert (
        texts[0] == "Waiting for your approval to commit 2 changed files: src/a.py, tests/test_a.py"
    )
    assert texts[1] == (
        f"  approve: lxreview approve {run} commit, or decline: lxreview stop {run}"
    )
    assert style(texts[0]) == "bold yellow"
    push = {**commit, "step": "push", "commit": "b" * 40, "subject": "Fix the parser"}
    texts = [line.split("  ", 1)[-1] for line in describe(push)]
    assert texts[0] == "Waiting for your approval to push commit bbbbbbbbbb Fix the parser"
    assert texts[1].endswith(f"approve {run} push, or decline: lxreview stop {run}")
    granted = {"kind": "approval_granted", "time": "", "step": "push"}
    assert describe(granted)[0].endswith("Push approved")
    assert PHASES["awaiting_push"] == "waiting for your approval to push"


def test_scratch_and_session_output_exceptions_do_not_follow_escaping_symlinks(
    tmp_path, monkeypatch
):
    home, repo, scratch = tmp_path / "home", tmp_path / "repo", tmp_path / "scratch"
    monkeypatch.setattr(Path, "home", lambda: home)
    repo.mkdir()
    scratch.mkdir()
    (scratch / "escape").symlink_to(tmp_path, target_is_directory=True)
    write = {"tool_name": "Write", "tool_input": {"file_path": str(scratch / "escape/x")}}
    assert not allowed(write, repo, "edit", scratch)[0]
    write["tool_input"]["file_path"] = str(scratch / "ok.py")
    for phase in ("evaluate", "publish"):
        assert not allowed(write, repo, phase, scratch)[0]

    session = "74188205-2a75-4a1d-aaaa-85a4844100ee"
    project = home / ".claude/projects/-repo"
    outputs = project / session / "tool-results"
    outputs.mkdir(parents=True)
    secret = home / ".claude.json"
    secret.write_text("private")
    (outputs / "escape.txt").symlink_to(secret)
    event = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(outputs / "escape.txt")},
        "session_id": session,
        "transcript_path": str(project / f"{session}.jsonl"),
    }
    assert not allowed(event, repo)[0]


def test_known_findings_prompt():
    request = ReviewRequest(target="https://github.com/org/repo/pull/1", head_sha="a" * 40)
    assert "Earlier independent" not in prompt_for(request)
    request.known_findings = ["off by one", "overflow"]
    prompt = prompt_for(request)
    assert "1. off by one; 2. overflow" in prompt
    assert "If no substantial issue exists beyond them, end with VERDICT: CLEAN." in prompt
    assert "\n" not in prompt and "\r" not in prompt


@pytest.mark.parametrize("titles", [[""], ["a\nb"], ["a\rb"], ["a|b"], ["x" * 161], ["x"] * 61])
def test_known_findings_validation(titles):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewRequest(
            target="https://github.com/org/repo/pull/1", head_sha="a" * 40, known_findings=titles
        )


def test_multi_pass_timeline():
    from lxreview.timeline import describe, style

    line = describe(
        {
            "kind": "finding_evaluated",
            "decision": "DUPLICATE",
            "finding": "S1",
            "duplicate_of": "P1-S1",
            "time": "",
        }
    )[0]
    assert "(duplicate of P1-S1)" in line and style("DUPLICATE S1") == "dim"
    line = describe(
        {
            "kind": "review_pass_summary",
            "pass_number": 2,
            "new_substantial": 0,
            "duplicates": 2,
            "time": "",
        }
    )[0]
    assert "added no new substantial finding (2 duplicates); drafting the review" in line
    line = describe(
        {
            "kind": "review_pass_summary",
            "pass_number": 1,
            "new_substantial": 61,
            "title_limit_reached": True,
            "time": "",
        }
    )[0]
    assert line.endswith("61 new substantial findings; earlier-finding limit reached")
    assert "after 2 passes" in describe({"kind": "review_posted", "passes": 2, "time": ""})[0]
