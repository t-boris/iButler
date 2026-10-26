"""Run every suite that guards the core and its migrated scenarios; write evidence with hashes.

Controlled evidence only: unit, contract and compatibility suites of butler-core,
the bridge plugin, the ambient module and the Butler workspace libraries. The
installed-system and live checks are recorded separately in VERIFICATION.md.
Results go to ``evidence/results.json`` (suite outcomes, counts, durations,
artefact hashes); no private content is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "evidence"
HOME = Path.home()
WORKSPACE = HOME / ".openclaw/workspace-butler"
AMBIENT = ROOT.parent / "ambient-awareness"
NODE = HOME / ".nvm/versions/node/v22.22.3/bin"
RUNTIME_PYTHON = HOME / ".openclaw/tools/butler-core/.venv/bin/python"

SUITES = [
    ("butler-core-unit", [sys.executable, "-m", "unittest", "discover", "-s", "tests"], ROOT, {}),
    ("butler-core-system1", [str(RUNTIME_PYTHON), "-m", "unittest", "discover", "-s", "tests", "-p", "test_system1.py"], ROOT, {}),
    ("bridge-node", [str(NODE / "node"), "--test", "test/bridge.test.js"], ROOT / "bridge", {"PATH": f"{NODE}:{os.environ.get('PATH', '')}"}),
    ("ambient-unit", [str(AMBIENT / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests"], AMBIENT, {}),
    ("workspace-butler", ["/opt/miniconda3/bin/python", "-m", "unittest", "discover", "-s", "tests"], WORKSPACE, {"PYTHONPATH": str(WORKSPACE)}),
]
ARTEFACTS = ["butler_core/__init__.py", "deploy.py", "bridge/index.js", "bridge/lib/bridge.js", "CHANGELOG.md"]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def counts(text):
    ran = re.search(r"Ran (\d+) tests?", text)
    skipped = re.search(r"skipped=(\d+)", text)
    node_pass = re.search(r"^# pass (\d+)", text, re.M)
    node_fail = re.search(r"^# fail (\d+)", text, re.M)
    return {"ran": int(ran.group(1)) if ran else (int(node_pass.group(1)) + int(node_fail.group(1)) if node_pass else None),
            "skipped": int(skipped.group(1)) if skipped else 0,
            "failed": int(node_fail.group(1)) if node_fail else None}


def run(name, command, cwd, env):
    started = time.monotonic()
    try:
        done = subprocess.run(command, cwd=cwd, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
                              capture_output=True, text=True, timeout=1800)
        output = (done.stdout or "") + (done.stderr or "")
        passed = done.returncode == 0 and ("OK" in output or "# fail 0" in output)
        code = done.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        output, passed, code = f"{type(error).__name__}: {error}", False, -1
    result = {"suite": name, "passed": passed, "exit": code, "seconds": round(time.monotonic() - started, 1), **counts(output),
              "tail": output.strip().splitlines()[-3:]}
    print(("PASS" if passed else "FAIL"), name, f"ran={result['ran']} skipped={result['skipped']} {result['seconds']}s")
    return result


def main():
    EVIDENCE.mkdir(exist_ok=True)
    results = [run(*suite) for suite in SUITES]
    from butler_core import __version__
    report = {"version": __version__, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "python": sys.version.split()[0],
              "suites": results, "artefacts": {name: sha256(ROOT / name) for name in ARTEFACTS if (ROOT / name).is_file()},
              "passed": all(r["passed"] for r in results)}
    (EVIDENCE / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print("evidence:", EVIDENCE / "results.json", "| all passed:", report["passed"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
