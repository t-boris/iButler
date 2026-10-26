"""Reproduce the authorized refresh without rebuilding the stable TCC parent."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_app import install

home = Path.home()
state = home / ".local/share/butler-ambient"
runtime = home / ".openclaw/tools/ambient-awareness"
os.umask(0o077)
verification = json.loads((ROOT / "evidence/results.json").read_text())
assert verification["passed"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert all(sha(ROOT / key) == value for key, value in verification["source_sha256"].items())


def audit():
    db = sqlite3.connect(f"file:{state}/ambient.sqlite3?mode=ro", uri=True)
    refs = db.execute("SELECT id,name,active,reference FROM agreements ORDER BY id").fetchall()
    preferences = db.execute("SELECT key,value FROM settings WHERE key IN ('voice_profile','voice_enabled','face_wake_dialogue','enabled') ORDER BY key").fetchall()
    db.close()
    return {"permission_parent": sha(runtime / "ButlerAwareness.app/Contents/MacOS/ButlerAwareness"),
            "configuration": sha(state / "config.json"),
            "global_configuration": sha(home / ".openclaw/openclaw.json"),
            "agreements": hashlib.sha256(json.dumps(refs).encode()).hexdigest(),
            "preferences": hashlib.sha256(json.dumps(preferences).encode()).hexdigest()}


report = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "before": audit(), "steps": [],
          "scope": "Only Butler runtime/UI; no version, gateway, global volume, enrollment or voice choice change."}


def save():
    (ROOT / "evidence/streaming-deployment.json").write_text(json.dumps(report, indent=2) + "\n")


def run(args, required=True):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    report["steps"].append({"command": args, "exit": result.returncode})
    save()
    with (ROOT / "evidence/streaming-deployment.log").open("ab") as log:
        log.write(result.stdout)
    if required:
        assert result.returncode == 0, args
    return result.returncode


save()
installed = runtime / ".venv/lib/python3.12/site-packages/butler_ambient"
backup = state / "backups" / ("contextual-streaming-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
backup.mkdir(parents=True, mode=0o700)
shutil.copytree(installed, backup / "butler_ambient")
shutil.copytree(home / "Applications/Butler.app", backup / "Butler.app")
for name in ("butler-audio", "butler-panel", "butler-camera", "butler-owner-auth", "butler-system-audio"):
    if (runtime / "native" / name).is_file(): shutil.copy2(runtime / "native" / name, backup / name)
report["backup"] = str(backup)
save()
stopped=run(["launchctl", "bootout", f"gui/{os.getuid()}/com.boris.butler-awareness"],False)
if stopped:
    # A separately audited recovery may already have unloaded a deadlocked
    # own instance. Never install over a still-running/unconfirmed service.
    assert subprocess.run(["launchctl","print",f"gui/{os.getuid()}/com.boris.butler-awareness"],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode != 0
    report["own_service_already_unloaded"] = True
time.sleep(.3)
wheel = next((ROOT / "build/wheels").glob("butler_ambient-*.whl"))
run(["uv", "pip", "install", "--python", str(runtime / ".venv/bin/python"), "--no-deps", "--reinstall", str(wheel)])
for name in ("butler-audio", "butler-panel", "butler-camera", "butler-owner-auth", "butler-system-audio"):
    target = runtime / "native" / name
    temporary = target.with_suffix(".new")
    shutil.copy2(ROOT / "build" / name, temporary)
    temporary.replace(target)
install(ROOT / "build/Butler.app", home / "Applications/Butler.app")
for delay in (.3, 1., 2.):
    time.sleep(delay)
    if run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(home / "Library/LaunchAgents/com.boris.butler-awareness.plist")], False) == 0:
        break
else:
    raise RuntimeError("Own service bootstrap failed; backup preserved in report")
report["after"] = audit()
assert report["before"] == report["after"]
report["stable_parent_config_agreements_preferences_unchanged"] = True
report["installed_sources_match"] = all(sha(path) == sha(installed / path.relative_to(ROOT / "butler_ambient")) for path in (ROOT / "butler_ambient").glob("*.py"))
assert report["installed_sources_match"]
report["source_count"] = len(list((ROOT / "butler_ambient").glob("*.py")))
report["wheel_sha256"] = sha(wheel)
app = home / "Applications/Butler.app"
assert plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleIconFile"] == "Butler.icns"
report["ui_matches"] = sha(app / "Contents/MacOS/Butler") == sha(ROOT / "build/butler-panel")
report["icon_matches"] = sha(app / "Contents/Resources/Butler.icns") == sha(ROOT / "butler_ambient/assets/Butler.icns")
assert report["ui_matches"] and report["icon_matches"]
save()
print("PASS verified contextual streaming runtime/UI/icon installed; stable parent, config, agreements and preferences unchanged")
