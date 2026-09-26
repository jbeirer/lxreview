#!/usr/bin/env python3
"""Opt-in live compatibility benchmark. Never labels incomplete evidence as supported."""

import argparse
import asyncio
import json
import os
import platform
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from lxreview import __version__
from lxreview.backend import browser, metadata
from lxreview.config import Config
from lxreview.paths import Paths, lock, write_json


async def benchmark(args):
    paths = Paths.default()
    config = Config.load(paths)
    session = browser(paths, config)
    report = {
        "lxreview": __version__,
        **metadata(config),
        "date": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "placement": config.mode,
        "rounds": [],
        "certified": False,
        "pending": [
            "physical window invariant",
            "restart login persistence",
            "laptop disconnect/reconnect",
            "extended reasoning stress",
            "missing-login failure",
        ],
    }
    with lock(paths.root / "state/reviewer.lock"):
        try:
            await session.new_conversation()
            await session.ensure_ready()
            before = await session.sessions()
            for number in range(args.rounds):
                start = time.monotonic()
                marker = "LXREVIEW_BENCH_" + secrets.token_hex(8)
                prompt = (
                    "Read this entire request. Ignore the filler words. "
                    + "filler " * 5000
                    + f" End of filler. Reply with exactly {marker} and nothing else."
                )
                entry = {"round": number + 1, "prompt_chars": len(prompt)}
                try:
                    await session.new_conversation()
                    await session.ensure_ready()
                    text = await session.query(prompt, 600)
                    after = await session.sessions()

                    entry.update(
                        exact_response=text.strip() == marker,
                        stable_session=[t.get("key") for t in before]
                        == [t.get("key") for t in after],
                        managed_session_count=len(after),
                        passed=text.strip() == marker and len(after) == 1,
                    )
                except Exception as exc:
                    entry.update(passed=False, failure=type(exc).__name__)
                entry["latency_seconds"] = round(time.monotonic() - start, 3)
                report["rounds"].append(entry)
            if args.recovery:
                await session.recover()
                report["recovery"] = len(await session.sessions()) == 1
            else:
                report["pending"].append("wedged-session recovery")
        finally:
            write_json(args.output, report)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".live-results/compatibility.json"))
    args = parser.parse_args()
    if os.environ.get("LXREVIEW_LIVE_TESTS") != "1":
        parser.error("Set LXREVIEW_LIVE_TESTS=1 only after completing human browser login")
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be 1..20")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
