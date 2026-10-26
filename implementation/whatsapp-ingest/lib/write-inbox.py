"""Persist one WhatsApp record using Butler's existing inbox, without an agent."""
import datetime as dt
import json
from pathlib import Path
import sys


def main():
    workspace = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(workspace))
    from lib import inbox

    # Also permits isolated test workspaces; never touches the Obsidian vault.
    inbox.WORKSPACE = workspace
    inbox.INBOX_DIR = workspace / "state" / "inbox"
    inbox.BUFFER = inbox.INBOX_DIR / "buffer.jsonl"
    record = json.load(sys.stdin)
    ts = record.pop("timestamp_ms", None)
    if isinstance(ts, (int, float)):
        record["ts"] = dt.datetime.fromtimestamp(ts / 1000).isoformat(timespec="seconds")
    buffered = inbox.buffer_message(**record)
    print(json.dumps({"buffered": buffered}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # The gateway receives only a status code, never private content/errors.
        sys.exit(1)
