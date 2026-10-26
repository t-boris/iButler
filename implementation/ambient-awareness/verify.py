"""Run local verification and retain exact commands, logs, exits and artifact hashes."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
EVIDENCE = ROOT / "evidence"


def main():
    EVIDENCE.mkdir(exist_ok=True)
    test_python = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    regression_python = os.environ.get("BUTLER_TEST_PYTHON", "/opt/miniconda3/bin/python")
    if not Path(regression_python).exists():
        regression_python = sys.executable
    jobs = [
        ("feature-tests", [test_python, "-m", "unittest", "discover", "-s", "tests", "-v"], ROOT),
        ("news-video-tests", [regression_python, "-m", "unittest", "discover", "-s", "tests", "-v"], PROJECT / "implementation/news-video"),
        ("bamberg-tests", [regression_python, "-m", "unittest", "discover", "-s", "tests", "-v"], PROJECT / "implementation/bamberg-news"),
        ("whatsapp-python-tests", [sys.executable, "-m", "unittest", "discover", "-s", "implementation/whatsapp-ingest/tests", "-p", "test_*.py", "-v"], PROJECT),
        ("whatsapp-node-tests", ["node", "--test", "implementation/whatsapp-ingest/tests/ingest-only.test.mjs"], PROJECT),
        ("native-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/screenshot.swift", "-o", "build/butler-screen"], ROOT),
        ("native-launcher-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/launcher.swift", "-o", "build/ButlerAwareness"], ROOT),
        ("native-panel-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/panel.swift", "-o", "build/butler-panel"], ROOT),
        ("native-app-build", [sys.executable, "build_app.py"], ROOT),
        ("native-panel-layout-test", [str(ROOT / "build/Butler.app/Contents/MacOS/Butler"), "--test-layout", str(EVIDENCE / "ui-preview")], ROOT),
        ("native-audio-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/audio.swift", "-o", "build/butler-audio"], ROOT),
        ("native-camera-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/camera.swift", "-o", "build/butler-camera"], ROOT),
        ("native-camera-test", [str(ROOT / "build/butler-camera"), "--test-jpeg"], ROOT),
        ("native-system-audio-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/system-audio.swift", "-o", "build/butler-system-audio"], ROOT),
        ("native-system-audio-format-test", [str(ROOT / "build/butler-system-audio"), "--test-format"], ROOT),
        ("native-owner-auth-build", ["swiftc", "-warnings-as-errors", "butler_ambient/native/owner-auth.swift", "-o", "build/butler-owner-auth"], ROOT),
        ("native-owner-auth-layout-test", [str(ROOT / "build/butler-owner-auth"), "--test-layout"], ROOT),
        ("native-owner-auth-events-test", [str(ROOT / "build/butler-owner-auth"), "--test-events"], ROOT),
        ("native-aec-test", [str(ROOT / "build/butler-audio"), "--test-echo"], ROOT),
        ("wheel-build", [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", "build/wheels", "."], ROOT),
        ("doctor", [str(ROOT / "bin/butler-ambient"), *(["--config", str(Path.home() / ".local/share/butler-ambient/config.json")] if (Path.home() / ".local/share/butler-ambient/config.json").exists() else []), "doctor"], ROOT),
    ]
    (ROOT / "build").mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    report = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "jobs": [], "github": "waived by DEC-005"}
    for name, command, cwd in jobs:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        log = f"cwd: {cwd}\ncommand: {json.dumps(command)}\nexit: {result.returncode}\n\n" + result.stdout.decode(errors="replace")
        (EVIDENCE / f"{name}.log").write_text(log)
        report["jobs"].append({"name": name, "command": command, "cwd": str(cwd), "exit": result.returncode, "log": f"{name}.log"})
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {name}", flush=True)
    artifacts = [ROOT / "build/Butler.app/Contents/Info.plist", ROOT / "build/Butler.app/Contents/MacOS/Butler", ROOT / "build/Butler.app/Contents/Resources/Butler.icns", EVIDENCE / "ui-preview/native-panel-layout.json", ROOT / "build/butler-screen", ROOT / "build/ButlerAwareness", ROOT / "build/butler-panel", ROOT / "build/butler-audio", ROOT / "build/butler-camera", ROOT / "build/butler-owner-auth", ROOT / "build/butler-system-audio", *sorted((ROOT / "build/wheels").glob("*.whl"))]
    report["artifacts"] = [{"file": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                           for path in artifacts if path.exists()]
    sources = [*sorted((ROOT / "butler_ambient").rglob("*.py")), *sorted((ROOT / "butler_ambient/native").glob("*.swift")),
               *sorted((ROOT / "tests").glob("*.py")), *sorted((ROOT / "tests/fixtures").glob("*")),
               *sorted((ROOT / "butler_ambient/assets").glob("*")),
               ROOT / "Makefile", ROOT / "build_app.py", ROOT / "image-requirements.txt", ROOT / "pyproject.toml", ROOT / "verify.py", ROOT / "deploy.py", ROOT / "bin/butler-ambient"]
    report["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    report["passed"] = all(job["exit"] == 0 for job in report["jobs"])
    (EVIDENCE / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
