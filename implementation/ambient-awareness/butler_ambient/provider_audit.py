"""Which model providers actually answered recently: proof that the butler runs on subscriptions, never on a paid API key.

Read only. It counts assistant messages in the OpenClaw session transcripts by (provider, api, model) and prints
those counts. It never prints, keeps or returns message text. Exit status 1 means at least one message came from a
provider that is not on the allow list.

    python -m butler_ambient.provider_audit [--days 4] [--agents-dir ~/.openclaw/agents]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Subscription logins (ChatGPT Codex OAuth, Claude Code OAuth) and OpenClaw's own copy of messages it delivered.
ALLOWED = {("openai-codex", None), ("claude-cli", None), ("openclaw", "delivery-mirror")}
MAX_LINE = 4_000_000


def classify(message):
    """(provider, model) of an assistant message, or None when it carries no provider."""
    provider, model = message.get("provider"), message.get("model")
    return (str(provider), str(model)) if provider else None


def allowed(provider, model):
    return (provider, None) in ALLOWED or (provider, model) in ALLOWED


def scan(agents_dir, days=4, now=None):
    """Counts of assistant messages per (provider, api, model) in transcripts touched in the last ``days`` days."""
    cutoff = (now if now is not None else time.time()) - days * 86400
    counts, files = Counter(), 0
    for path in sorted(Path(agents_dir).expanduser().glob("*/sessions/*.jsonl")):
        if path.name.endswith((".trajectory.jsonl", ".checkpoint.jsonl")):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                continue
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        with handle:
            for line in handle:
                if len(line) > MAX_LINE:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                message = row.get("message") if isinstance(row, dict) else None
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                kind = classify(message)
                if kind is not None:
                    counts[(kind[0], str(message.get("api")), kind[1])] += 1
    return counts, files


def report(counts, files, days):
    lines = [f"assistant messages in {files} session files touched in the last {days} days"]
    bad = 0
    for (provider, api, model), count in sorted(counts.items(), key=lambda item: -item[1]):
        ok = allowed(provider, model)
        bad += 0 if ok else count
        lines.append(f"  {'ok ' if ok else 'BAD'} {count:6d}  {provider} / {api} / {model}")
    lines.append("result: " + ("only subscription providers" if not bad else f"{bad} message(s) from a provider that is not allowed"))
    return "\n".join(lines), bad


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--agents-dir", default="~/.openclaw/agents")
    args = parser.parse_args(argv)
    counts, files = scan(args.agents_dir, args.days)
    text, bad = report(counts, files, args.days)
    print(text)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
