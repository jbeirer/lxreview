import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from lxreview import worker
from lxreview.config import Config
from lxreview.paths import private_dir
from lxreview.runs import RunStore
from lxreview.worker import Evaluation, claude_turn


@pytest.fixture(autouse=True)
def scratch(monkeypatch):
    # Turns use /tmp in production; a sandboxed test run may write only inside its TMPDIR.
    assert worker.SCRATCH == "/tmp"
    monkeypatch.setattr(worker, "SCRATCH", tempfile.gettempdir())


@pytest.fixture
def store(paths, tmp_path):
    value = RunStore.create(
        paths, tmp_path, "https://github.com/o/r/pull/1", {"head": "a" * 40}, 2, tmp_path / "audit"
    )
    value.update(**{"pass": 1})
    private_dir(Path(value.load()["audit"]) / "pass-01")
    return value


async def test_real_child_stream_capture_and_guard_configuration(paths, store, tmp_path):
    executable = tmp_path / "fake-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + f"SCRATCH = {os.path.join(tempfile.gettempdir(), 'lxreview-')!r}\n"
        + """import sys,json,uuid
assert '--dangerously-skip-permissions' in sys.argv
assert '--strict-mcp-config' in sys.argv
assert '--setting-sources' in sys.argv
settings=json.load(open(sys.argv[sys.argv.index('--settings')+1]))
assert settings['sandbox']['enabled']
assert settings['sandbox']['failIfUnavailable']
assert not settings['sandbox']['allowUnsandboxedCommands']
assert settings['hooks']['PreToolUse'][0]['matcher']=='.*'
# Glob Read rules expand into one sandbox mount per file on Linux (E2BIG with a large .git).
assert not any(rule.startswith('Read(') for rule in settings['permissions']['deny'])
assert settings['attribution'] is False
import os
# The sandbox proxy socket lives in TMPDIR; it must be short and outside the read-only root.
assert os.environ['TMPDIR'].startswith(SCRATCH) and os.path.isdir(os.environ['TMPDIR'])
open(os.path.join(os.environ['LXREVIEW_HOME'], 'turn-tmpdir'), 'w').write(os.environ['TMPDIR'])
sys.stdin.read()
print(json.dumps({'type':'system','session_id':str(uuid.uuid4())}),flush=True)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'PRIVATE_REASONING'},{'type':'text','text':'Checking evidence; Bearer private-token'}]}}),flush=True)
print(json.dumps({'type':'result','is_error':False,'structured_output':{'evaluation':{'findings':[{'finding':'S1','decision':'REJECTED','reason':'guard already exists','evidence':'file.py:3'}]}}}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    result = await claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    assert result.findings[0].decision == "REJECTED"
    assert (store.directory / "claude-session-id").read_text()
    log = (store.directory / "stdout.log").read_text()
    assert "PRIVATE_REASONING" not in log and "private-token" not in log
    assert store.load()["claude_pid"] is None
    assert not Path((paths.root / "turn-tmpdir").read_text()).exists()


async def test_edit_turn_guard_accepts_the_scratch_directory_the_prompt_names(
    paths, store, tmp_path
):
    executable = tmp_path / "fake-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,re,shlex,sys
settings=json.load(open(sys.argv[sys.argv.index('--settings')+1]))
hook=shlex.split(settings['hooks']['PreToolUse'][0]['hooks'][0]['command'])
work=re.search(r'Writable scratch directory for build output and other temporary files: (\\S+) ',sys.stdin.read()).group(1)
assert hook[hook.index('--scratch')+1]==work and os.path.isdir(work)
print(json.dumps({'type':'result','is_error':False,'structured_output':{'edit':{'tests':['pytest: PASSED'],'tests_passed':True,'preexisting_failures':[],'summary':'ok'}}}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    result = await claude_turn(paths, config, store, "edit", worker.EditResult, read_only=False)
    assert result.tests_passed


async def test_cancel_terminates_owned_child(paths, store, tmp_path):
    executable = tmp_path / "sleeping-claude"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,time\nsys.stdin.read()\ntime.sleep(120)\n"
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    task = asyncio.create_task(
        claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    )
    for _ in range(100):
        pid = store.load().get("claude_pid")
        if pid:
            break
        await asyncio.sleep(0.01)
    assert pid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert store.load()["claude_pid"] is None


async def test_cancel_kills_child_which_ignores_term(paths, store, tmp_path):
    import psutil

    executable = tmp_path / "forking-claude"
    pidfile = tmp_path / "child.pid"
    child_code = f"import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(120)"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "sys.stdin.read()\ntime.sleep(120)\n"
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    task = asyncio.create_task(
        claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    )
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    child = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 12)
    for _ in range(100):
        try:
            if psutil.Process(child).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Owned child survived cancellation")


@pytest.mark.parametrize("publish", [False, True])
async def test_real_worker_phase_configuration(paths, store, tmp_path, monkeypatch, publish):
    from lxreview.worker import EditResult, PublishResult

    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/auth-agent")
    executable = tmp_path / "phase-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import sys,json,os,shlex
settings=json.load(open(sys.argv[sys.argv.index('--settings')+1]))
command=shlex.split(settings['hooks']['PreToolUse'][0]['hooks'][0]['command'])
phase=command[command.index('--phase')+1]
assert phase in ('edit','publish')
# LXReview pushes itself: no Claude phase has network or agent access.
assert settings['sandbox']['network']['allowedDomains'] == []
assert 'SSH_AUTH_SOCK' not in os.environ
assert 'blockReadsOutsideWorkingDirectories' not in settings['permissions']
assert any(path.endswith('/.ssh') for path in settings['sandbox']['filesystem']['denyRead'])
# The turn's private directory holds the caches and scratch area: it must be writable, and
# the prompt names the scratch area literally (commands expand no variables).
assert os.environ['TMPDIR'] in settings['sandbox']['filesystem']['allowWrite']
prompt = sys.stdin.read()
assert os.environ['TMPDIR'] + '/work' in prompt and os.path.isdir(os.environ['TMPDIR'] + '/work')
if phase=='edit':
    assert any(path.endswith('.git') for path in settings['sandbox']['filesystem']['denyWrite'])
    result={'edit':{'tests':['pytest: passed'],'tests_passed':True,'preexisting_failures':[],'summary':'fixed'}}
else:
    result={'publish':{'commit':'a'*40}}
sys.stdin.read()
print(json.dumps({'type':'result','is_error':False,'structured_output':result}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    schema = PublishResult if publish else EditResult
    result = await claude_turn(paths, config, store, "work", schema, read_only=False)
    assert isinstance(result, schema)


async def test_worker_model_and_effort_reach_claude_only_when_set(paths, store, tmp_path):
    executable = tmp_path / "model-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import sys,json
open(sys.argv[0] + '.argv', 'w').write(json.dumps(sys.argv))
sys.stdin.read()
print(json.dumps({'type':'result','is_error':False,'structured_output':{'evaluation':{'findings':[{'finding':'S1','decision':'REJECTED','reason':'r','evidence':'e'}]}}}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    await claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    argv = json.loads(Path(str(executable) + ".argv").read_text())
    assert "--model" not in argv and "--effort" not in argv
    config.worker.model, config.worker.effort = "opus", "xhigh"
    await claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    argv = json.loads(Path(str(executable) + ".argv").read_text())
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "xhigh"


async def test_every_turn_shares_tools_and_schema_so_the_prompt_cache_survives(paths, tmp_path):
    from lxreview.worker import EditResult, PublishResult

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    store = RunStore.create(
        paths,
        repo,
        "https://github.com/o/r/pull/1",
        {"head": "a" * 40},
        2,
        repo / ".git/review-loop",
    )
    store.update(**{"pass": 1})
    private_dir(Path(store.load()["audit"]) / "pass-01")
    executable = tmp_path / "recording-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,re,subprocess,sys
settings=json.load(open(sys.argv[sys.argv.index('--settings')+1]))
hook=settings['hooks']['PreToolUse'][0]['hooks'][0]
command=hook['command']
phase=re.search(r'--phase (\\w+)',command).group(1)
prompt=sys.stdin.read()
# The guard executable does not exist here: the hook must block rather than fail open.
write={'tool_name':'Write','tool_input':{'file_path':'x'},'cwd':os.getcwd()}
blocked=subprocess.run(['/bin/sh','-c',command],input=json.dumps(write),text=True,capture_output=True).returncode
record={'tools':sys.argv[sys.argv.index('--tools')+1],'schema':sys.argv[sys.argv.index('--json-schema')+1],
        'deny_write':settings['sandbox']['filesystem']['denyWrite'],'blocked':blocked,'prompt':prompt,
        'hook_timeout':hook.get('timeout')}
open(sys.argv[0]+'.'+phase,'w').write(json.dumps(record))
parts={'evaluate':{'evaluation':{'findings':[{'finding':'S1','decision':'REJECTED','reason':'r','evidence':'e'}]}},
       'edit':{'edit':{'tests':['pytest: passed'],'tests_passed':True,'preexisting_failures':[],'summary':'s'}},
       'publish':{'publish':{'commit':'a'*40}}}
print(json.dumps({'type':'result','is_error':False,'structured_output':parts[phase]}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    assert not paths.executable.exists()
    await claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
    await claude_turn(paths, config, store, "edit", EditResult, read_only=False)
    await claude_turn(paths, config, store, "publish", PublishResult, read_only=False)
    records = {
        phase: json.loads(Path(f"{executable}.{phase}").read_text())
        for phase in ("evaluate", "edit", "publish")
    }
    assert len({(r["tools"], r["schema"]) for r in records.values()}) == 1
    assert all(r["blocked"] == 2 for r in records.values())
    # A timed-out hook lets the tool run, so the turn's own timeout must end it first.
    assert all(r["hook_timeout"] > config.review.worker_timeout for r in records.values())
    assert str(repo) in records["evaluate"]["deny_write"]
    assert str(repo) not in records["edit"]["deny_write"] + records["publish"]["deny_write"]
    assert "use only the Read, Glob and Grep tools" in records["evaluate"]["prompt"]
    for phase, part in (("evaluate", "evaluation"), ("edit", "edit"), ("publish", "publish")):
        assert f"as the `{part}` field" in records[phase]["prompt"]


async def test_result_in_another_turns_field_is_refused(paths, store, tmp_path):
    from lxreview.errors import LXError

    executable = tmp_path / "confused-claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
sys.stdin.read()
print(json.dumps({'type':'result','is_error':False,'structured_output':{'edit':{'tests':['t'],'tests_passed':True,'preexisting_failures':[],'summary':'s'}}}),flush=True)
"""
    )
    executable.chmod(0o700)
    config = Config()
    config.runtime.claude = str(executable)
    with pytest.raises(LXError, match="structured result"):
        await claude_turn(paths, config, store, "evaluate", Evaluation, read_only=True)
