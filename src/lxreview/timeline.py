"""Human-readable run timeline for `lxreview watch`; events are already redacted."""

from datetime import datetime

PHASES = {
    "queued": "waiting for the worker",
    "reviewer": "reviewer generating",
    "evaluation": "Claude evaluating findings",
    "editing_testing": "Claude editing and testing",
    "publishing": "Claude committing and pushing",
    "finished": "finished",
    "interrupted": "interrupted",
}


def clock(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"


def shorten(text: object, limit: int = 100) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def claude_activity(event: dict) -> list[str]:
    """Summarize one Claude stream-json event: edits, shell commands, refusals, turn ends."""
    kind = event.get("type")
    if kind == "result":
        seconds = (event.get("duration_ms") or 0) / 1000
        return [
            "Claude turn failed" if event.get("is_error") else f"Claude turn done ({seconds:.0f}s)"
        ]
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    lines = []
    for block in content:
        if not isinstance(block, dict):
            continue
        data = block.get("input")
        if not isinstance(data, dict):
            data = {}
        if block.get("type") == "tool_use":
            name = block.get("name")
            if name in ("Edit", "Write", "MultiEdit"):
                lines.append(f"Editing {data.get('file_path', '?')}")
            elif name == "Bash":
                lines.append(f"$ {shorten(data.get('command', ''))}")
        elif block.get("type") == "tool_result" and block.get("is_error"):
            detail = block.get("content")
            if isinstance(detail, list):
                detail = " ".join(str(d.get("text", "")) for d in detail if isinstance(d, dict))
            lines.append(f"Tool refused/failed: {shorten(detail)}")
    return lines


def describe(event: dict) -> list[str]:
    kind, n = event.get("kind"), event.get("pass_number", "?")
    if kind == "claude":
        texts = claude_activity(event.get("event") or {})
    elif kind == "run_created":
        texts = ["Run created"]
    elif kind == "resumed":
        texts = [f"Resumed after completed pass {event.get('checkpoint')}"]
    elif kind == "review_started":
        texts = [f"Reviewer pass {n} started at {str(event.get('head', ''))[:10]}"]
    elif kind == "review_received":
        counts = (
            f": {event['substantial']} substantial, {event['non_blocking']} non-blocking"
            if "substantial" in event
            else ""
        )
        texts = [f"Reviewer pass {n} complete, {event.get('verdict')}{counts}"]
    elif kind == "finding_evaluated":
        texts = [
            f"{event.get('decision', '?'):<9} {event.get('finding')} {shorten(event.get('title', ''), 80)}"
        ]
    elif kind == "evaluation_complete":
        texts = [f"Evaluation: {event.get('accepted')} accepted, {event.get('rejected')} rejected"]
    elif kind == "tests_reported":
        texts = [f"Tests {'PASS' if event.get('passed') else 'FAIL'}"]
        texts += [f"  {shorten(test)}" for test in event.get("tests", [])]
    elif kind == "fixes_pushed":
        texts = [f"Commit {str(event.get('commit', ''))[:10]} pushed"]
    elif kind == "diff_capture_failed":
        texts = [f"Diff capture failed for pass {n}"]
    elif kind == "worker_output_invalid":
        texts = ["Ignored an unparseable Claude output line"]
    elif kind == "run_finished":
        error = event.get("error")
        texts = [f"Finished: {event.get('status')}" + (f" ({shorten(error)})" if error else "")]
    else:
        texts = [str(kind)]
    return [f"{clock(event.get('time', ''))}  {text}" for text in texts]
