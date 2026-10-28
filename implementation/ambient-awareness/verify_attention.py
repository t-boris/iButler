"""Reproducible paired release verification for Butler, including local Recordings."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import argparse
from pathlib import Path

AMBIENT = Path(__file__).resolve().parent
PROJECT = AMBIENT.parents[1]
CORE = AMBIENT.parent / "butler-core"
OUT = AMBIENT / "evidence/attention-implementation"
IGNORED = {".venv", "build", "evidence", "__pycache__", "node_modules", ".git", ".pytest_cache"}


def sources():
    result = {}
    for root in (AMBIENT, CORE):
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if any(part in IGNORED or part.endswith(".egg-info") for part in relative.parts): continue
            if path.is_file() and path.suffix in {".py", ".swift", ".toml", ".js", ".json", ".html", ".wav", ".icns"}:
                result[str(path.relative_to(PROJECT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def version(root, package):
    return re.search(r'__version__ = "([^"]+)"', (root / package / "__init__.py").read_text())[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-only", action="store_true",
                        help="build release artifacts without running tests (owner-waived release path)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    baseline = sources()
    report = {"started": dt.datetime.now(dt.timezone.utc).isoformat(), "git": subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True, check=True).stdout.strip(),
              "versions": {"ambient": version(AMBIENT, "butler_ambient"), "core": version(CORE, "butler_core")},
              "source_sha256": baseline, "jobs": [], "passed": False}
    node = Path.home() / ".nvm/versions/node/v22.22.3/bin/node"
    if args.build_only:
        jobs = [("ambient-build", ["make", "build", "PYTHON=/opt/miniconda3/bin/python"], AMBIENT),
                ("core-build", ["make", "build", "PYTHON=/opt/miniconda3/bin/python"], CORE)]
        report["tests_waived_by_owner"] = "DEC-089"
    else:
        jobs = [("ambient-verify", [str(AMBIENT / ".venv/bin/python"), "verify.py"], AMBIENT),
                ("core-tests", ["/opt/miniconda3/bin/python", "-m", "unittest", "discover", "-s", "tests", "-v"], CORE),
                ("core-system1", [str(Path.home() / ".openclaw/tools/butler-core/.venv/bin/python"), "-m", "unittest", "discover", "-s", "tests", "-p", "test_system1.py", "-v"], CORE),
                ("core-build", ["make", "build", "PYTHON=/opt/miniconda3/bin/python"], CORE),
                ("bridge-tests", [str(node), "--test", "test/bridge.test.js"], CORE / "bridge"),
                ("workspace-tests", ["/opt/miniconda3/bin/python", "-m", "unittest", "discover", "-s", "tests", "-v"], Path.home() / ".openclaw/workspace-butler")]
    for name, command, cwd in jobs:
        started = dt.datetime.now(dt.timezone.utc)
        log = OUT / (name + ".log")
        with log.open("w") as stream:
            stream.write("cwd: " + str(cwd) + "\ncommand: " + json.dumps(command) + "\n"); stream.flush()
            try:
                done = subprocess.run(command, cwd=cwd, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(cwd)},
                                      stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
                code = done.returncode
            except subprocess.TimeoutExpired: code = -1
        report["jobs"].append({"name": name, "command": command, "cwd": str(cwd), "exit": code,
                               "seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 2), "log": str(log.relative_to(PROJECT))})
        (OUT / "release-verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(("PASS " if code == 0 else "FAIL ") + name, flush=True)
    report["source_unchanged"] = baseline == sources()
    report["build_passed"] = (args.build_only and report["source_unchanged"]
                              and all(j["exit"] == 0 for j in report["jobs"]))
    report["passed"] = (not args.build_only and report["source_unchanged"]
                        and all(j["exit"] == 0 for j in report["jobs"]))
    if args.build_only: report["change_decisions"] = ["DEC-094"]
    report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["artifacts"] = {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in [*AMBIENT.glob("build/wheels/*.whl"), *CORE.glob("build/wheels/*.whl"),
                                     AMBIENT / "build/Butler.app/Contents/Info.plist", AMBIENT / "build/Butler.app/Contents/MacOS/Butler",
                                     CORE / "build/ButlerCore", *AMBIENT.glob("build/butler-*")] if p.is_file()}
    (OUT / "release-verification.json").write_text(json.dumps(report, indent=2) + "\n")
    label = "PASS build-only release preparation (tests waived)" if report["build_passed"] else "PASS release verification" if report["passed"] else "FAIL release verification"
    print(label, flush=True)
    return 0 if report["passed"] or report["build_passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
