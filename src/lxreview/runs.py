import json
import logging
import re
import secrets
import socket
from datetime import UTC, datetime
from pathlib import Path

from .errors import Category, LXError
from .paths import Paths, append_private, atomic_write, lock, private_dir, write_json
from .security import redact

TERMINAL = {
    "CLEAN",
    "NO_VALID_SUBSTANTIAL_FINDINGS",
    "MAX_PASSES",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


class RunStore:
    def __init__(self, paths: Paths, run_id: str):
        if not re.fullmatch(r"lr-\d{8}-\d{6}-[0-9a-f]{8}", run_id):
            raise LXError(Category.CONFIG, "Invalid run ID")
        self.paths, self.id = paths, run_id
        self.directory = paths.root / "state/runs" / run_id

    @classmethod
    def create(
        cls, paths: Paths, repo: Path, target: str, identity: dict, max_passes: int, audit: Path
    ):
        run_id = datetime.now(UTC).strftime("lr-%Y%m%d-%H%M%S-") + secrets.token_hex(4)
        store = cls(paths, run_id)
        private_dir(store.directory)
        private_dir(audit / run_id)
        store.save(
            {
                "id": run_id,
                "repo": str(repo.resolve()),
                "target": target,
                "host": socket.getfqdn(),
                "status": "QUEUED",
                "pass": 0,
                "max_passes": max_passes,
                "identity": identity,
                "starting_sha": identity["head"],
                "audit": str(audit / run_id),
                "created": now(),
                "phase": "queued",
                "completed_pass": 0,
            }
        )
        for filename in ("stdout.log", "stderr.log", "events.jsonl", "claude-session-id"):
            atomic_write(store.directory / filename, "")
        store.event("run_created", target=target)
        return store

    def load(self) -> dict:
        try:
            return json.loads((self.directory / "state.json").read_text())
        except (OSError, ValueError) as exc:
            raise LXError(Category.CONFIG, "Run not found or state is invalid") from exc

    def save(self, state: dict) -> None:
        state["updated"] = now()
        write_json(self.directory / "state.json", state)
        if "audit" in state:
            write_json(Path(state["audit"]) / "run.json", state)

    def update(self, **changes) -> dict:
        with lock(self.directory / "state.lock", blocking=True):
            state = self.load()
            if changes.get("status") == "INTERRUPTED" and state["status"] in TERMINAL:
                return state
            state.update(changes)
            if (self.directory / "cancel").exists():
                state["status"] = "CANCELLED"
            self.save(state)
            return state

    def observed(self, config) -> dict:
        from .process import Supervisor

        state = self.load()
        if state["host"] != socket.getfqdn():
            return {**state, "observation": f"Connect to {state['host']} to inspect the worker"}
        # Startup publication precedes supervisor activation; don't declare that gap a crash.
        age = (datetime.now(UTC) - datetime.fromisoformat(state["updated"])).total_seconds()
        if state["status"] not in TERMINAL and not (state["status"] == "QUEUED" and age < 60):
            if not Supervisor(self.paths, config).status("run-" + self.id):
                return self.update(
                    status="INTERRUPTED",
                    phase="interrupted",
                    error="Worker supervisor is no longer active; inspect artifacts before resume",
                )
        return state

    def event(self, kind: str, **data) -> None:
        event = {"time": now(), "kind": kind, **redact(data)}
        logging.getLogger("lxreview.runs").info("run=%s event=%s", self.id, kind)
        line = json.dumps(event) + "\n"
        with lock(self.directory / "events.lock", blocking=True):
            for path in (
                self.directory / "events.jsonl",
                Path(self.load()["audit"]) / "events.jsonl",
            ):
                append_private(path, line)

    def report(self) -> str:
        state = self.load()
        lines = [
            f"# LXReview {self.id}",
            f"PR: {state['target']}",
            f"Status: {state['status']}",
            f"Host: {state['host']}",
            "",
        ]
        for directory in sorted(Path(state["audit"]).glob("pass-*")):
            lines += [f"## {directory.name}"]
            for name in ("reviewer.json", "evaluation.json", "metadata.json"):
                path = directory / name
                if path.exists():
                    data = json.loads(path.read_text())
                    data.pop("raw", None)
                    lines += ["```json", json.dumps(redact(data), indent=2), "```"]
        if state.get("error"):
            lines += [f"Stopped: {state['error']}"]
        return "\n".join(lines) + "\n"

    def finish(self, status: str, error: str = "") -> None:
        state = self.update(status=status, phase="finished", error=redact(error))
        self.event("run_finished", status=state["status"], error=error)
        atomic_write(Path(state["audit"]) / "summary.md", self.report())
