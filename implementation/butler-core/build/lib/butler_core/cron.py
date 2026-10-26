"""Read-only accounting of OpenClaw cron runs (REQ-026, plan W1).

The gateway persists cron outcomes only in ``cron/runs/<jobId>.jsonl``
(``finished`` records) and ``cron/jobs-state.json`` (running markers). This
reader turns them into stable run keys ``cron:<jobId>:<runAtMs>``. It never
writes to the cron directory and never starts a run.
"""
from __future__ import annotations

import json
from pathlib import Path

CRON = Path.home() / ".openclaw" / "cron"


def agent_of(session_key, fallback):
    if isinstance(session_key, str) and session_key.startswith("agent:"):
        return session_key.split(":", 2)[1]
    return fallback


class CronReader:
    def __init__(self, root=CRON):
        self.root = Path(root)
        self.offsets = {}

    def jobs(self):
        try:
            data = json.loads((self.root / "jobs.json").read_text())
        except (OSError, ValueError):
            return {}
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        return {j["id"]: {"name": j.get("name") or j["id"], "agent": j.get("agentId"), "enabled": j.get("enabled")}
                for j in jobs if isinstance(j, dict) and j.get("id")}

    def finished(self, since=0):
        """New finished records since the last call (all of them, newer than ``since``, on the first call)."""
        found = []
        runs = self.root / "runs"
        if not runs.is_dir():
            return found
        for path in sorted(runs.glob("*.jsonl")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = self.offsets.get(path.name, 0)
            if size < offset:
                offset = 0          # rotated or rewritten: re-read; keys deduplicate
            if size == offset:
                continue
            with open(path, "rb") as stream:
                stream.seek(offset)
                data = stream.read()
            complete = data.rfind(b"\n") + 1
            self.offsets[path.name] = offset + complete
            for line in data[:complete].splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("action") != "finished" or not record.get("jobId") or record.get("runAtMs") is None:
                    continue
                if int(record["runAtMs"]) / 1000 < since:
                    continue
                found.append({"key": f"cron:{record['jobId']}:{int(record['runAtMs'])}", "job": record["jobId"],
                              "run_at": int(record["runAtMs"]) / 1000, "status": str(record.get("status", "unknown")),
                              "duration_ms": record.get("durationMs"), "session": record.get("sessionKey"),
                              "delivery": record.get("deliveryStatus")})
        return found

    def running(self):
        try:
            data = json.loads((self.root / "jobs-state.json").read_text())
        except (OSError, ValueError):
            return []
        found = []
        for job, entry in (data.get("jobs") or {}).items():
            state = (entry or {}).get("state") or {}
            started = state.get("runningAtMs")
            if isinstance(started, (int, float)):
                found.append({"key": f"cron:{job}:{int(started)}", "job": job, "run_at": started / 1000,
                              "status": "running"})
        return found
