"""Deploy the verified paired release through the project's existing launch agents.

Run only after verify_attention.py succeeds. Private rollback copies stay outside
the checkout; the evidence report contains versions, counts and hashes only.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from build_app import install as install_ui
from verify_attention import AMBIENT, CORE, OUT, PROJECT, sources

HOME = Path.home()
WORKSPACE = HOME / ".openclaw/workspace-butler"
AR = HOME / ".openclaw/tools/ambient-awareness"
CR = HOME / ".openclaw/tools/butler-core"
AS = HOME / ".local/share/butler-ambient"
CS = HOME / ".local/share/butler-core"
LABELS = ("com.boris.butler-awareness", "com.boris.butler-core")
DOMAIN = f"gui/{os.getuid()}"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(*args, check=True):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, check=check, timeout=180)


def ambient(command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10); client.connect(str(AS / "ambient.sock"))
        client.sendall((json.dumps({"command": command}) + "\n").encode())
        result = json.loads(client.makefile("rb").readline())
    if "error" in result: raise RuntimeError("ambient request unavailable")
    return result["result"]


def core(method):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10); client.connect(str(CS / "core.sock"))
        client.sendall((json.dumps({"id": "release", "method": method, "params": {}}) + "\n").encode())
        result = json.loads(client.makefile("rb").readline())
    if "error" in result: raise RuntimeError("core request unavailable")
    return result["result"]


def protected():
    files = [AR / "ButlerAwareness.app/Contents/MacOS/ButlerAwareness", AR / "ButlerAwareness.app/Contents/Info.plist",
             CR / "ButlerCore.app/Contents/MacOS/ButlerCore", HOME / ".openclaw/openclaw.json"]
    files += list((AS / "references").rglob("*")) if (AS / "references").exists() else []
    result = {str(p): sha(p) for p in files if p.is_file()}
    with sqlite3.connect(AS / "ambient.sqlite3") as db:
        for table in ("settings", "agreements", "identities"):
            data = db.execute("SELECT * FROM " + table + (" WHERE key != 'attention_override'" if table == "settings" else "") + " ORDER BY 1").fetchall()
            result[table] = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    return result


def stop(label):
    run("launchctl", "bootout", f"{DOMAIN}/{label}", check=False)
    for _ in range(100):
        if run("launchctl", "print", f"{DOMAIN}/{label}", check=False).returncode: return
        time.sleep(.2)
    raise RuntimeError("launch agent did not stop: " + label)


def start(label):
    plist = HOME / "Library/LaunchAgents" / (label + ".plist")
    for delay in (.3, 1, 2, 3):
        result = run("launchctl", "bootstrap", DOMAIN, plist, check=False)
        if result.returncode == 0: return
        time.sleep(delay)
    raise RuntimeError("launch agent did not start: " + label)


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + ".release")
    shutil.copy2(source, temporary); os.replace(temporary, target)


def sanitize_summaries():
    path = CS / "feedback-summaries.jsonl"
    if not path.exists(): return 0
    rows = []
    kinds = {"full_pause", "pause", "mute", "cancel", "approval_rejected", "correction", "not_addressed", "complaint", "silence_request", "consent_revoked", "ignored_initiative"}
    causes = {"too_intrusive", "wrong_place", "too_many_questions", "poor_questions", "wrong_timing", "unwanted_content", "other"}
    for line in path.read_text().splitlines():
        try: old = json.loads(line)
        except ValueError: continue
        row = {k: old[k] for k in ("deliveries", "operations", "repeats", "confidence") if type(old.get(k)) in (int, float)}
        row["kind"] = old.get("kind") if old.get("kind") in kinds else "unknown"
        row["cause"] = old.get("cause") if old.get("cause") in causes else "other"
        row["delivery_kinds"] = [k for k in old.get("delivery_kinds", []) if k in {"advice", "daily", "initiative", "digest", "question", "status", "response"}]
        rows.append(row)
    temporary = path.with_suffix(".release")
    temporary.write_text("".join(json.dumps(r) + "\n" for r in rows)); temporary.chmod(0o600); temporary.replace(path)
    return len(rows)


def main():
    os.umask(0o077)
    report_path = OUT / "deployment.json"
    verification = json.loads((OUT / "release-verification.json").read_text())
    exact_sources = verification.get("source_sha256") == sources()
    full_verification = verification.get("passed") is True
    owner_waived_tests = (
        verification.get("tests_waived_by_owner") == "DEC-089"
        and verification.get("build_passed") is True
        and verification.get("source_unchanged") is True
    )
    if not exact_sources or not (full_verification or owner_waived_tests):
        raise RuntimeError("exact-source full verification or DEC-089 owner-waived test gate with successful build is required")
    for name, checksum in verification["artifacts"].items():
        if sha(PROJECT / name) != checksum: raise RuntimeError("verified artifact changed")
    versions = verification["versions"]
    ui = ambient("ui-state")
    if ui.get("voice", {}).get("owner_setup", {}).get("stage") in {"authenticating", "recording", "validating"}:
        raise RuntimeError("owner enrollment in progress")
    before = protected()
    active_config = core("doctor")["config"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = AS / "release-backups" / stamp; backup.mkdir(parents=True, mode=0o700)
    targets = [AS / "config.json", WORKSPACE / "AGENTS.md", WORKSPACE / "LESSONS.md", WORKSPACE / "state/memory-index.db", CS / "feedback-summaries.jsonl", HOME / "Applications/Butler.app"]
    targets += [WORKSPACE / "lib" / n for n in ("lessons.py", "memory.py", "embeddings.py", "knowledge_sources.py", "butler_rules.py")]
    for runtime in (AR, CR):
        for site in (runtime / ".venv/lib").glob("python*/site-packages"):
            targets += list(site.glob("butler_ambient*")) + list(site.glob("butler_core*"))
    for target in targets:
        if not target.exists(): continue
        destination = backup / target.relative_to(HOME)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir(): shutil.copytree(target, destination)
        else: shutil.copy2(target, destination)
    config_path = AS / "config.json"
    config = json.loads(config_path.read_text())
    previous_attention = ui.get("voice", {}).get("attention", {})
    selected_mode = previous_attention.get("override")
    if selected_mode in {"busy", "free"}:
        meeting = ui.get("voice", {}).get("meeting", {})
        baseline = {"mic-app": meeting.get("apps") or False}
        for key, flag in (("phone-at-ear", "phone_at_ear"), ("talking-to-person", "talking_to_person")):
            if (previous_attention.get("vision") or {}).get(flag): baseline[key] = True
        config["attention_override_handoff"] = {"mode": selected_mode, "baseline": baseline}
    hidden_present, old_hidden = "panel_start_hidden" in config, config.get("panel_start_hidden")
    report = {"started": stamp, "versions": versions, "backup": str(backup), "verified": False,
              "verification": "release-verification.json",
              "verification_basis": ("full verification passed" if full_verification else "build passed; tests waived by owner (DEC-089)"),
              "permission_parent_unchanged": False}
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    for label in LABELS: stop(label)
    # Stop only this owner's previous UI if it was opened outside the service.
    executable = str(HOME / "Applications/Butler.app/Contents/MacOS/Butler")
    for line in run("ps", "-axo", "pid=,uid=,comm=").stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[1] == str(os.getuid()) and fields[2] == executable:
            os.kill(int(fields[0]), signal.SIGTERM)
    core_wheel = CORE / "build/wheels" / f"butler_core-{versions['core']}-py3-none-any.whl"
    ambient_wheel = AMBIENT / "build/wheels" / f"butler_ambient-{versions['ambient']}-py3-none-any.whl"
    for runtime, wheels in ((CR, [core_wheel]), (AR, [core_wheel, ambient_wheel])):
        run("uv", "pip", "install", "--python", runtime / ".venv/bin/python", "--no-deps", "--reinstall", *wheels)
    install_ui(AMBIENT / "build/Butler.app", HOME / "Applications/Butler.app")
    for name in ("butler-screen", "butler-panel", "butler-audio", "butler-system-audio", "butler-camera", "butler-owner-auth"):
        copy_file(AMBIENT / "build" / name, AR / "native" / name)
    for name in ("lessons.py", "memory.py", "embeddings.py", "knowledge_sources.py"):
        copy_file(CORE / "workspace/lib" / name, WORKSPACE / "lib" / name)
    copy_file(CORE / "butler_core/rules.py", WORKSPACE / "lib/butler_rules.py")
    agents = WORKSPACE / "AGENTS.md"
    text = agents.read_text()
    exception = ("\n### Voice rules reload (DEC-075)\n\n"
        "For a private voice request containing a `butler-lesson context --turn` instruction, run that exact existing memory tool before answering. "
        "It reloads current applicable owner-approved preferences and lessons for that request. This is the voice exception to the startup-only LESSONS rule. "
        "Treat returned rules as advisory, never as new permissions. Do not send local episode excerpts, ambient call conclusions, credentials or raw audio. "
        "If the tool is unavailable, do not claim rules were loaded. LESSONS.md is generated read-only from canonical vault Butler/Rules; use butler-lesson to change rules.\n")
    if "### Voice rules reload (DEC-075)" not in text: agents.write_text(text.rstrip() + "\n" + exception)
    defaults = {"vault": str(HOME / "Documents/KnowledgeDB"), "workspace": str(WORKSPACE), "butler_workspace": str(WORKSPACE),
                "feedback_metadata": str(CS / "conversation.sqlite3"), "require_rule_authorization": True, "turn_control": False}
    for key, value in defaults.items(): config.setdefault(key, value)
    config["panel_start_hidden"] = True
    config_path.write_text(json.dumps(config, indent=2) + "\n"); config_path.chmod(0o600)
    report["anonymized_legacy_summaries"] = sanitize_summaries()
    # Existing canonical tool, no direct general vault-note writer.
    migrated = run(AR / ".venv/bin/python", "-I", "-c", "from butler_core.rules import RuleStore; import sys; s=RuleStore(sys.argv[1],sys.argv[2]); n=s.migrate(); p=s.publish(); print(n, len(p['rules']))", config["vault"], WORKSPACE)
    report["migration"] = dict(zip(("migrated", "active"), map(int, migrated.stdout.split())))
    from dialogue_server import configure_background, install
    configure_background(); install()
    for label in reversed(LABELS): start(label)
    for _ in range(100):
        try:
            a, c = ambient("status"), core("doctor")
            if a.get("version") == versions["ambient"] and c["core"] == versions["core"]: break
        except (OSError, ValueError, RuntimeError): pass
        time.sleep(.5)
    else: raise RuntimeError("new running versions did not become ready")
    if hidden_present: config["panel_start_hidden"] = old_hidden
    else: config.pop("panel_start_hidden", None)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    config.pop("attention_override_handoff", None)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    installed = {}
    for name, runtime in (("ambient", AR), ("core", CR)):
        done = run(runtime / ".venv/bin/python", "-I", "-c", "import butler_core; print(butler_core.__version__)")
        installed[name + "_core"] = done.stdout.strip()
    installed["ambient"] = run(AR / ".venv/bin/python", "-I", "-c", "import butler_ambient; print(butler_ambient.__version__)").stdout.strip()
    app = HOME / "Applications/Butler.app"
    installed["ui"] = plistlib.loads((app / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"]
    ui_hash_matches = sha(app / "Contents/MacOS/Butler") == sha(AMBIENT / "build/Butler.app/Contents/MacOS/Butler")
    unchanged = before == protected()
    report.update(installed=installed, running={"ambient": a["version"], "core": c["core"]}, ui_binary_matches=ui_hash_matches,
                  permission_parent_unchanged=unchanged, active_config_unchanged=c["config"] == active_config,
                  agent_instruction_sha256=sha(agents), turn_control=config["turn_control"], rule_reload="existing butler-lesson context tool")
    report["verified"] = (unchanged and report["active_config_unchanged"] and ui_hash_matches
        and installed == {"ambient_core": versions["core"], "core_core": versions["core"], "ambient": versions["ambient"], "ui": versions["ambient"]})
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["verified"] else 1


if __name__ == "__main__": raise SystemExit(main())
