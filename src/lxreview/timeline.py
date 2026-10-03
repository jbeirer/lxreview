"""Human-readable run timeline for `lxreview watch`; events are already redacted."""

import re
from datetime import datetime

PHASES = {
    "queued": "waiting for the worker",
    "reviewer": "reviewer generating",
    "evaluation": "Claude evaluating findings",
    "editing_testing": "Claude editing and testing",
    "publishing": "Claude committing and pushing",
    "awaiting_commit": "waiting for your approval to commit",
    "awaiting_push": "waiting for your approval to push",
    "commenting": "drafting the GitHub review",
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
    the reason and adapts. Hide the pair when possible; explain a refusal if its call
    was already displayed by a live watch."""
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


def refusal_reason(block: dict) -> str:
    text = result_text(block)
    match = re.match(r"PreToolUse:\S+ hook error: \[.*?\]: (.+)", text, re.S)
    return (match.group(1) if match else text).strip()


def claude_activity(
    event: dict,
    limit: int = 100,
    hidden: set[str] | None = None,
    shown: set[str] | None = None,
) -> list[str]:
    """Summarize one Claude stream-json event: what Claude says, edits, shell commands,
    failures, long-running commands and turn ends. Thinking never reaches the timeline;
    redaction removed it.

    `shown` collects the calls already printed. A live watch may print a call before its
    guard refusal arrives; the refusal is then named instead of silently dropped."""
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
            else:
                continue
            if shown is not None:
                shown.add(str(block.get("id")))
        elif block.get("type") == "tool_result":
            if block.get("is_error") and not guard_refusal(block):
                lines.append(f"Failed: {shorten(result_text(block), limit)}")
            elif guard_refusal(block) and str(block.get("tool_use_id")) in (shown or set()):
                lines.append(f"  refused by the guard: {shorten(refusal_reason(block), limit)}")
            if shown is not None:
                shown.discard(str(block.get("tool_use_id")))
    return lines


def describe(
    event: dict,
    limit: int = 100,
    hidden: set[str] | None = None,
    shown: set[str] | None = None,
) -> list[str]:
    kind, n = event.get("kind"), event.get("pass_number", "?")
    if kind == "claude":
        texts = claude_activity(event.get("event") or {}, limit, hidden, shown)
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
        used = ", ".join(str(event[k]) for k in ("model", "reasoning") if event.get(k))
        texts = [
            f"Reviewer pass {n} complete"
            + (f" ({used})" if used else "")
            + f", {event.get('verdict')}{counts}"
        ]
        texts += [
            f"  {finding.get('id')} {shorten(finding.get('title', ''), limit)}"
            for finding in event.get("findings", [])
            if isinstance(finding, dict)
        ]
    elif kind == "finding_evaluated":
        texts = [
            f"{event.get('decision', '?'):<9} {event.get('finding')} {shorten(event.get('title', ''), limit)}"
        ]
        if event.get("duplicate_of"):
            texts[0] += f" (duplicate of {event['duplicate_of']})"
        if event.get("reason"):
            texts.append(f"  because {shorten(event['reason'], limit)}")
    elif kind == "polish_skipped":
        texts = [
            "Accepted non-blocking findings left for you (no unreviewed final-pass edits): "
            + ", ".join(str(f) for f in event.get("findings", []))
        ]
    elif kind == "non_blocking_ignored":
        texts = [
            "Non-blocking findings left unevaluated (substantial-only run): "
            + ", ".join(str(f) for f in event.get("findings", []))
        ]
    elif kind == "evaluation_complete":
        texts = [f"Evaluation: {event.get('accepted')} accepted, {event.get('rejected')} rejected"]
    elif kind == "discussion_read":
        texts = [
            f"Discussion read: {event.get('comments', 0)} comments,"
            f" {event.get('reviews', 0)} reviews, {event.get('threads', 0)} review threads"
            f" ({event.get('unresolved', 0)} unresolved)"
        ]
    elif kind == "tests_reported":
        preexisting = event.get("preexisting", [])
        texts = [
            f"Checks {'PASS' if event.get('passed') else 'FAIL'}"
            + (f" ({len(preexisting)} pre-existing failures)" if preexisting else "")
        ]
        texts += [f"  {shorten(test, limit)}" for test in event.get("tests", [])]
        texts += [f"  pre-existing: {shorten(test, limit)}" for test in preexisting]
    elif kind == "approval_requested":
        step = event.get("step")
        if step == "push":
            what = f"push commit {str(event.get('commit', ''))[:10]} {shorten(event.get('subject', ''), limit)}"
        else:
            files = [str(f) for f in event.get("files", [])]
            what = f"commit {len(files)} changed file{'s' * (len(files) != 1)}" + (
                f": {shorten(', '.join(files), limit)}" if files else ""
            )
        run = event.get("run_id", "<run-id>")
        texts = [
            f"Waiting for your approval to {what}",
            f"  approve: lxreview approve {run} {step}, or decline: lxreview stop {run}",
        ]
    elif kind == "approval_granted":
        texts = [f"{str(event.get('step', '')).capitalize()} approved"]
    elif kind == "push_started":
        texts = [f"Pushing commit {str(event.get('commit', ''))[:10]}"]
    elif kind == "fixes_pushed":
        texts = [f"Commit {str(event.get('commit', ''))[:10]} pushed"]
    elif kind == "review_pass_summary":
        count = event.get("new_substantial", 0)
        added = (
            "no new substantial finding"
            if not count
            else f"{count} new substantial finding" + ("s" if count != 1 else "")
        )
        duplicates = event.get("duplicates", 0)
        suffix = (
            "another pass follows"
            if event.get("continues")
            else "pass limit reached"
            if event.get("limit_reached")
            else "earlier-finding limit reached"
            if event.get("title_limit_reached")
            else "drafting the review"
            if event.get("drafts")
            else "no review to draft"
        )
        texts = [
            f"Reviewer pass {n} added {added}"
            + (f" ({duplicates} duplicates)" if duplicates else "")
            + f"; {suffix}"
        ]
    elif kind == "review_posted":
        texts = [
            f"Draft review created on GitHub: {event.get('inline', 0)} inline,"
            f" {event.get('summary', 0)} in summary — {event.get('url', '')}; submit it there"
        ]
        if event.get("passes", 1) > 1:
            texts[0] += f" after {event['passes']} passes"
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


def render(events: list[dict], limit: int = 100, shown: set[str] | None = None) -> list[str]:
    """Timeline lines for a batch of events, without guard-refused calls. A live watch
    passes the same `shown` set for every batch (see `claude_activity`)."""
    hidden = refused_calls(events)
    return [line for event in events for line in describe(event, limit, hidden, shown)]


def style(text: str) -> str:
    """Terminal style for one timeline text (the part after the clock)."""
    rules = (
        ("Claude: ", "cyan"),
        ("ACCEPTED", "bold green"),
        ("REJECTED", "bold yellow"),
        ("DUPLICATE", "dim"),
        ("  because ", "dim"),
        ("  still running", "dim"),
        ("$ ", "bright_black"),
        ("Editing ", "magenta"),
        ("Failed: ", "red"),
        ("Checks PASS", "bold green"),
        ("Checks FAIL", "bold red"),
        ("  ", "dim"),
        ("Waiting for your approval", "bold yellow"),
        ("Commit ", "bold green"),
        ("Draft review created", "bold green"),
        ("Finished: CLEAN", "bold green"),
        ("Finished: NO_VALID_SUBSTANTIAL_FINDINGS", "bold green"),
        ("Finished: REVIEW_DRAFTED", "bold green"),
        ("Accepted non-blocking findings left", "yellow"),
        ("Non-blocking findings left unevaluated", "dim"),
        ("Finished: MAX_PASSES", "bold yellow"),
        ("Finished: ", "bold red"),
        ("Claude turn failed", "red"),
        ("Claude turn done", "dim"),
    )
    if re.match(r"  S\d", text):
        return "yellow"
    for prefix, value in rules:
        if text.startswith(prefix):
            return value
    if text.startswith("Reviewer pass") and "complete" in text:
        return "bold green" if "CLEAN" in text else "bold yellow"
    if text.startswith(("Reviewer pass", "Evaluation:")):
        return "bold"
    return ""
