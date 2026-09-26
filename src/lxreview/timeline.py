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


def result_text(block: dict) -> str:
    detail = block.get("content")
    if isinstance(detail, list):
        detail = " ".join(str(d.get("text", "")) for d in detail if isinstance(d, dict))
    return str(detail or "")


def guard_refusal(block: dict) -> bool:
    """The worker guard declined a tool call. That is policy, not a failure: Claude reads
    the reason and adapts, so the timeline leaves both the call and the refusal out."""
    return (
        block.get("type") == "tool_result"
        and bool(block.get("is_error"))
        and result_text(block).startswith("PreToolUse:")
    )


def refused_calls(events: list[dict]) -> set[str]:
    ids = set()
    for event in events:
        content = ((event.get("event") or {}).get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and guard_refusal(block):
                ids.add(str(block.get("tool_use_id")))
    return ids


def claude_activity(event: dict, limit: int = 100, hidden: set[str] | None = None) -> list[str]:
    """Summarize one Claude stream-json event: what Claude says, edits, shell commands,
    failures, long-running commands and turn ends. Thinking never reaches the timeline;
    redaction removed it."""
    kind = event.get("type")
    if kind == "tool_progress":
        seconds = event.get("elapsed_time_seconds")
        # Claude Code reports a running command every 30 s; one line per minute is enough.
        if event.get("heartbeat") and isinstance(seconds, int) and seconds and seconds % 60 == 0:
            return [f"  still running ({seconds // 60} min)"]
        return []
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
        if block.get("type") == "text" and event.get("type") == "assistant":
            if text := " ".join(str(block.get("text", "")).split()):
                lines.append(f"Claude: {shorten(text, 3 * limit)}")
        elif block.get("type") == "tool_use":
            if str(block.get("id")) in (hidden or set()):
                continue
            name = block.get("name")
            if name in ("Edit", "Write", "MultiEdit"):
                lines.append(f"Editing {data.get('file_path', '?')}")
            elif name == "Bash":
                lines.append(f"$ {shorten(data.get('command', ''), limit)}")
        elif block.get("type") == "tool_result" and block.get("is_error"):
            if not guard_refusal(block):
                lines.append(f"Failed: {shorten(result_text(block), limit)}")
    return lines


def describe(event: dict, limit: int = 100, hidden: set[str] | None = None) -> list[str]:
    kind, n = event.get("kind"), event.get("pass_number", "?")
    if kind == "claude":
        texts = claude_activity(event.get("event") or {}, limit, hidden)
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
            f"{event.get('decision', '?'):<9} {event.get('finding')} {shorten(event.get('title', ''), limit)}"
        ]
        if event.get("reason"):
            texts.append(f"  because {shorten(event['reason'], limit)}")
    elif kind == "evaluation_complete":
        texts = [f"Evaluation: {event.get('accepted')} accepted, {event.get('rejected')} rejected"]
    elif kind == "tests_reported":
        texts = [f"Tests {'PASS' if event.get('passed') else 'FAIL'}"]
        texts += [f"  {shorten(test, limit)}" for test in event.get("tests", [])]
    elif kind == "fixes_pushed":
        texts = [f"Commit {str(event.get('commit', ''))[:10]} pushed"]
    elif kind == "diff_capture_failed":
        texts = [f"Diff capture failed for pass {n}"]
    elif kind == "worker_output_invalid":
        texts = ["Ignored an unparseable Claude output line"]
    elif kind == "run_finished":
        error = event.get("error")
        texts = [
            f"Finished: {event.get('status')}" + (f" ({shorten(error, limit)})" if error else "")
        ]
    else:
        texts = [str(kind)]
    return [f"{clock(event.get('time', ''))}  {text}" for text in texts]


def render(events: list[dict], limit: int = 100) -> list[str]:
    """Timeline lines for a batch of events, without guard-refused calls."""
    hidden = refused_calls(events)
    return [line for event in events for line in describe(event, limit, hidden)]


def style(text: str) -> str:
    """Terminal style for one timeline text (the part after the clock)."""
    rules = (
        ("Claude: ", "cyan"),
        ("ACCEPTED", "bold green"),
        ("REJECTED", "bold yellow"),
        ("  because ", "dim"),
        ("  still running", "dim"),
        ("$ ", "bright_black"),
        ("Editing ", "magenta"),
        ("Failed: ", "red"),
        ("Tests PASS", "bold green"),
        ("Tests FAIL", "bold red"),
        ("  ", "dim"),
        ("Commit ", "bold green"),
        ("Finished: CLEAN", "bold green"),
        ("Finished: MAX_PASSES", "bold yellow"),
        ("Finished: ", "bold red"),
        ("Claude turn failed", "red"),
        ("Claude turn done", "dim"),
    )
    for prefix, value in rules:
        if text.startswith(prefix):
            return value
    if text.startswith("Reviewer pass") and "complete" in text:
        return "bold green" if "CLEAN" in text else "bold yellow"
    if text.startswith(("Reviewer pass", "Evaluation:")):
        return "bold"
    return ""
