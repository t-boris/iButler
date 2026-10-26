#!/usr/bin/env python3
"""Keep gateway available but disable WhatsApp if an update removed its guards."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import os


def main():
    config = Path.home() / ".openclaw/openclaw.json"
    data = json.loads(config.read_text())
    whatsapp = data.get("channels", {}).get("whatsapp", {})
    if whatsapp.get("enabled") is not True:
        return
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("install.py")), "--check"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        safe_patch = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        safe_patch = False
    safe_policy = any(rule.get("action") == "deny" and rule.get("match") == {"channel": "whatsapp"}
                      for rule in data.get("session", {}).get("sendPolicy", {}).get("rules", []))
    safe_config = whatsapp.get("sendReadReceipts") is False and whatsapp.get("debounceMs") == 0
    if safe_patch and safe_policy and safe_config:
        return
    whatsapp["enabled"] = False
    with tempfile.NamedTemporaryFile(mode="w", dir=config.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        tmp = Path(f.name)
    os.chmod(tmp, 0o600)
    tmp.replace(config)
    print("[whatsapp-ingest-only] WhatsApp disabled: passive guards need repair; other channels may start.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
