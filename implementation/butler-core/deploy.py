"""Install the butler-core runtime and its launch agent, then confirm the running version.

The runtime lives in ~/.openclaw/tools/butler-core, state in ~/.local/share/butler-core.
On the first installation the initial ConfigVersion (DEC-050) is generated from
the live gateway allowlists and activated once by the daemon (bootstrap). An
existing state keeps its active configuration; a regenerated one is written
next to it for an explicit ``butler-core admin activate``. Identifiers are never
printed. This script never restarts the shared gateway and never sends messages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
RUNTIME = HOME / ".openclaw/tools/butler-core"
STATE = HOME / ".local/share/butler-core"
APP = RUNTIME / "ButlerCore.app"
LABEL = "com.boris.butler-core"
AGENT_PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
OPENCLAW_CONFIG = HOME / ".openclaw/openclaw.json"
PYTHON = "/opt/miniconda3/bin/python"
OPENCLAW = "/opt/homebrew/bin/openclaw"
NODE_BIN = HOME / ".nvm/versions/node/v22.22.3/bin"
BRIDGE = RUNTIME / "bridge"
BRIDGE_FILES = ("index.js", "package.json", "openclaw.plugin.json", "lib/bridge.js", "lib/core.js", "lib/telegram.js")
DEBOUNCE_MS = 500
WORKSPACE = HOME / ".openclaw/workspace-butler"
WORKSPACE_BINS = [WORKSPACE / "bin"]
# The core client is published into the workspace so its CLIs reach the core without a package install.
WORKSPACE_CLIENT = WORKSPACE / "lib/butler_core_client.py"


def run(*args, check=True):
    return subprocess.run([str(a) for a in args], check=check, capture_output=True, text=True)


def allowlist(config, account):
    entry = config.get("channels", {}).get("telegram", {}).get("accounts", {}).get(account, {})
    return [f"telegram:{value}" for value in entry.get("allowFrom", []) if str(value).strip()]


def initial_config(openclaw=OPENCLAW_CONFIG):
    """ConfigVersion v1: today's flows as explicit owner grants (DEC-050), nothing more."""
    config = json.loads(Path(openclaw).read_text())
    owner = allowlist(config, "butler")
    if len(owner) != 1:
        raise SystemExit("expected exactly one owner principal in the Butler bot allowlist")
    tetyana = allowlist(config, "tetyana-dyck")
    return {
        "schema": 1,
        "owner": {"principals": owner + ["terminal:owner", "voice:owner"],
                  "notify": {"channel": "telegram:butler", "recipient": owner[0]}},
        "personas": {
            "butler": {"description": "Boris's personal Butler", "principals": owner + ["voice:owner"],
                       "sources": ["vault", "memory", "sessions", "projects", "github-checkouts",
                                   "ambient-observations"],
                       "actions": ["openclaw.agent_turn"], "recipients": owner},
            "tetyana-dyck": {"description": "FamilyBuilder.io assistant for Tatyana", "principals": tetyana,
                             "sources": ["tetyana-workspace", "tetyana-repo"], "actions": ["openclaw.agent_turn"],
                             "recipients": tetyana},
            "bamberg-news": {"description": "Bamberg News channel", "principals": [],
                             "sources": ["bamberg-workspace"], "actions": []},
            "main": {"description": "OpenClaw default agent", "principals": [], "sources": ["main-workspace"],
                     "actions": []},
        },
        "grants": [],
        "exports": [
            {"persona": "butler", "category": "task-prompt", "recipient": "openai"},
            {"persona": "butler", "category": "task-prompt", "recipient": "anthropic"},
            {"persona": "butler", "category": "project-context", "recipient": "openai"},
            {"persona": "butler", "category": "project-context", "recipient": "anthropic"},
            {"persona": "butler", "category": "search-query", "recipient": "web-search"},
            {"persona": "butler", "category": "task", "recipient": "todoist"},
            {"persona": "butler", "category": "owner-message", "recipient": "telegram"},
            {"persona": "butler", "category": "video", "recipient": "youtube"},
            {"persona": "butler", "category": "narration-text", "recipient": "openai"},
            {"persona": "butler", "category": "voice-note", "recipient": "openai"},
            {"persona": "butler", "category": "feedback-summary", "recipient": "openai"},
            {"persona": "tetyana-dyck", "category": "repository", "recipient": "github"},
            {"persona": "tetyana-dyck", "category": "deployment", "recipient": "firebase"},
            {"persona": "tetyana-dyck", "category": "owner-message", "recipient": "telegram"},
            {"persona": "bamberg-news", "category": "video", "recipient": "youtube"},
        ],
        "operations": {
            "openclaw.agent_turn": {"adapter": "openclaw", "approval": "none", "reconcile": "owner",
                                    "max_attempts": 1, "timeout": 600},
        },
        "scenarios": {"chat": {"enabled": True}, "cron": {"enabled": True}},
        "delivery": {"zone": "America/Chicago", "quiet": [22, 8], "max_attempts": 3, "retry_window": 300,
                     "initiative": {"advice": {"per_day": 3, "interval": 3600},
                                    "daily": {"per_day": 1, "interval": 0}}},
        "limits": {"queue": 1000, "workers": {"default": 2, "openclaw": 1, "delivery": 2, "periodic": 6,
                                             "backup": 1, "reconcile": 2}},
        "bridge": {"accounts": {"butler": {"persona": "butler", "receipt": "👌"},
                                "tetyana-dyck": {"persona": "tetyana-dyck", "receipt": "👌"}},
                   "timeout_ms": 1200},
        "routing": {"system1": {"mode": "shadow"}},
        "approval": {"ttl": 900},
    }


def wave_dispatch(content):
    """W2 (inventory 4, 17; DEC-061): coding dispatches and owner-confirmed voice jobs as tracked operations.

    One operation kind per backend, so each export is checked against its real recipient.
    The voice switch stays off until the ambient package with the core launcher is deployed."""
    recipients = {"codex": "openai", "claude": "anthropic"}
    for family, adapter in (("dispatch", "dispatcher"), ("voice", "voice-tasks")):
        for backend, recipient in recipients.items():
            kind = f"{family}.{backend}"
            content["personas"]["butler"]["actions"].append(kind)
            content["operations"][kind] = {
                "adapter": adapter, "approval": "none", "reconcile": "auto", "max_attempts": 2, "timeout": 120,
                "export": [{"category": "task-prompt", "recipient": recipient},
                           {"category": "project-context", "recipient": recipient}]}
    content["scenarios"]["dispatch"] = {"enabled": True}
    content["scenarios"]["voice_tasks"] = {"enabled": False}
    content["limits"]["workers"].update({"dispatcher": 2, "voice-tasks": 1})


def bamberg_exports(content):
    """DEC-062: Bamberg News' existing public research services and edge-tts narration."""
    for category, recipient in (("public-news-query", "news.google.com"), ("public-news-query", "api.perplexity.ai"),
                                ("place-name", "tile.openstreetmap.org"), ("place-name", "nominatim.openstreetmap.org"),
                                ("media-search", "commons.wikimedia.org"), ("weather-location", "api.brightsky.dev"),
                                ("narration-text", "microsoft-edge-tts")):
        content["exports"].append({"persona": "bamberg-news", "category": category, "recipient": recipient})


def wave_delivery(content):
    """W3 (inventory 6; DEC-063): owner notices, the tasks board and dispatch status lines through core delivery."""
    content["scenarios"]["delivery"] = {"enabled": True}


def wave_todoist(content):
    """W4 (inventory 3, 10): Todoist writes as idempotent operations; delete and bulk import need the owner."""
    for kind, approval in (("todoist.create", "none"), ("todoist.update", "none"), ("todoist.close", "none"),
                           ("todoist.reopen", "none"), ("todoist.comment", "none"), ("todoist.delete", "required"),
                           ("todoist.import", "required")):
        content["personas"]["butler"]["actions"].append(kind)
        content["operations"][kind] = {"adapter": "todoist", "approval": approval, "reconcile": "auto",
                                       "max_attempts": 2, "timeout": 60,
                                       "export": [{"category": "task", "recipient": "todoist"}]}
    content["scenarios"]["todoist"] = {"enabled": True}
    content["limits"]["workers"]["todoist"] = 1


def wave_media(content):
    """W5 (inventory 2, 12, 13, 14; DEC-064): YouTube uploads and the narration/voice-note exports."""
    publish = {"adapter": "youtube", "reconcile": "auto", "max_attempts": 2, "timeout": 1900,
               "export": [{"category": "video", "recipient": "youtube"}]}
    content["operations"]["youtube.publish_news"] = {**publish, "approval": "none"}     # standing authorization 2026-07-19
    content["operations"]["youtube.publish"] = {**publish, "approval": "required"}
    content["operations"]["export.narration.openai"] = {"adapter": "export", "approval": "none", "reconcile": "auto",
                                                         "max_attempts": 1, "timeout": 30,
                                                         "export": [{"category": "narration-text", "recipient": "openai"}]}
    content["operations"]["export.narration.edge"] = {"adapter": "export", "approval": "none", "reconcile": "auto",
                                                       "max_attempts": 1, "timeout": 30,
                                                       "export": [{"category": "narration-text", "recipient": "microsoft-edge-tts"}]}
    content["operations"]["export.voice-note"] = {"adapter": "export", "approval": "none", "reconcile": "auto",
                                                  "max_attempts": 1, "timeout": 30,
                                                  "export": [{"category": "voice-note", "recipient": "openai"}]}
    content["personas"]["butler"]["actions"] += ["youtube.publish_news", "youtube.publish", "export.narration.openai",
                                                  "export.narration.edge", "export.voice-note"]
    content["personas"]["bamberg-news"]["actions"] += ["youtube.publish_news", "export.narration.edge"]
    content["scenarios"]["youtube"] = {"enabled": True}
    content["scenarios"]["exports"] = {"enabled": True}
    content["limits"]["workers"].update({"youtube": 1, "export": 2})


def wave_mail(content):
    """Email digest and approved replies (feature butler-email-digest-and-approved-replies, DEC-001/DEC-065)."""
    content["operations"]["mail.scan"] = {"adapter": "mail", "approval": "none", "reconcile": "owner", "max_attempts": 1,
                                          "timeout": 900}
    content["operations"]["mail.reply"] = {"adapter": "mail", "approval": "required", "reconcile": "auto",
                                           "max_attempts": 2, "timeout": 240}
    content["personas"]["butler"]["actions"] += ["mail.scan", "mail.reply"]
    content["scenarios"]["mail"] = {"enabled": True}
    content["mail"] = {"times": ["08:30", "17:30"], "zone": "America/Chicago", "lookback_hours": 24}
    content["delivery"]["initiative"]["digest"] = {"per_day": 3, "interval": 0}
    content["limits"]["workers"]["mail"] = 1


def wave_knowledge(content):
    """W6 (inventory 5, 7, 10, 11, 16; REQ-023): vault writes and recall on the operation path."""
    write = {"adapter": "vault", "reconcile": "auto", "max_attempts": 2, "timeout": 120, "sources": ["vault"]}
    content["operations"]["vault.write"] = {**write, "approval": "none"}
    content["operations"]["vault.write_confirmed"] = {**write, "approval": "required"}
    content["operations"]["knowledge.recall"] = {"adapter": "knowledge", "approval": "none", "reconcile": "auto",
                                                 "max_attempts": 1, "timeout": 30, "sources": ["vault", "memory", "sessions"]}
    content["personas"]["butler"]["actions"] += ["vault.write", "vault.write_confirmed", "knowledge.recall"]
    content["scenarios"]["vault"] = {"enabled": True}
    content["scenarios"]["knowledge"] = {"enabled": True}
    content["limits"]["workers"].update({"vault": 1, "knowledge": 2})


def wave_ingest(content):
    """W7 (inventory 8, 9): userbot and WhatsApp messages as ingest events under their stable key; text stays local."""
    content["scenarios"]["ingest"] = {"enabled": True}


def wave_ambient(content):
    """W8 (inventory 17): released observations and owner signals reach the core; voice jobs run through it."""
    content["scenarios"]["observation"] = {"enabled": True}
    content["scenarios"]["voice_tasks"] = {"enabled": True}


def wave_tetyana(content):
    """W9 (inventory 15): Tatyana's push + PR + auto-merge as one operation of her isolated persona."""
    content["operations"]["github.pr"] = {"adapter": "github", "approval": "none", "reconcile": "auto", "max_attempts": 2,
                                          "timeout": 900, "export": [{"category": "repository", "recipient": "github"}]}
    content["personas"]["tetyana-dyck"]["actions"].append("github.pr")
    content["scenarios"]["github"] = {"enabled": True}
    content["limits"]["workers"]["github"] = 1


def wave_system3(content):
    """W11 (REQ-027, DEC-058/059): slow analysis of the owner's signals into lessons, locally."""
    content["scenarios"]["system3"] = {"enabled": True}
    content["system3"] = {"questions_per_week": 2, "analysis_interval": 21600}


# Applied in order on top of ConfigVersion v1; each wave adds only what its decisions approve.
WAVES = (wave_dispatch, bamberg_exports, wave_delivery, wave_todoist, wave_media, wave_mail, wave_knowledge, wave_ingest,
         wave_ambient, wave_tetyana, wave_system3)

FAILED_QUEUE = Path.home() / ".openclaw/delivery-queue/failed"
# Errors the gateway or Telegram raised before anything was transmitted; everything else is uncertain.
REFUSED_BEFORE_SEND = ("Local media path is not under", "hostReadCapability permits only", "413:")


def failed_queue_entries(directory=FAILED_QUEUE, accounts=("butler", "tetyana-dyck")):
    """The gateway's failed deliveries as read-only import entries: metadata only, no message text."""
    entries = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        account = data.get("accountId")
        if data.get("channel") != "telegram" or account not in accounts:
            continue
        error = str(data.get("lastError") or "")
        entries.append({"key": f"gateway:{data['id']}", "recipient": f"telegram:{data.get('to')}",
                        "channel": f"telegram:{account}", "persona": account, "kind": "imported",
                        "attempts": int(data.get("retryCount") or 0) + 1,
                        "created_at": float(data.get("enqueuedAt") or 0) / 1000 or None,
                        "state": "failed" if any(m in error for m in REFUSED_BEFORE_SEND) else "uncertain",
                        "reason": "gateway failed queue: " + error[:160]})
    for entry in entries:
        if entry["created_at"] is None:
            del entry["created_at"]
    return entries


def import_failed_queue():
    """Bring the stuck gateway deliveries into accounting while the daemon is stopped (DEC-060, REQ-024).

    The import is read-only: nothing in it is ever resent. It runs offline because
    the deploy shell is not an owner terminal and must not act as one."""
    entries = failed_queue_entries()
    if not entries:
        return {"imported": 0, "entries": 0}
    listing = STATE / "failed-queue-import.json"
    listing.write_text(json.dumps(entries, indent=1))
    done = run(RUNTIME / "bin/butler-core", "admin", "import-deliveries", listing, "--state", STATE, check=False)
    if done.returncode:
        raise SystemExit(f"offline delivery import failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout) | {"entries": len(entries)}


def desired_config(openclaw=OPENCLAW_CONFIG):
    content = initial_config(openclaw)
    for wave in WAVES:
        wave(content)
    return content


def build():
    run("make", "-C", ROOT, "build")
    wheels = sorted((ROOT / "build/wheels").glob("butler_core-*.whl"), key=lambda p: p.stat().st_mtime)
    return wheels[-1]


def install(wheel):
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    (STATE / "logs").mkdir(exist_ok=True)
    run("uv", "venv", "--allow-existing", "--system-site-packages", "--python", PYTHON, RUNTIME / ".venv")
    python = RUNTIME / ".venv/bin/python"
    run("uv", "pip", "install", "--python", python, "--no-deps", "--reinstall", wheel)
    contents = APP / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": LABEL, "CFBundleName": "Butler Core", "CFBundleExecutable": "ButlerCore",
            "CFBundlePackageType": "APPL", "LSUIElement": True, "LSBackgroundOnly": True}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    shutil.copy2(ROOT / "build/ButlerCore", contents / "MacOS/ButlerCore")
    run("codesign", "--force", "--sign", "-", "--identifier", LABEL, APP)
    (RUNTIME / "bin").mkdir(exist_ok=True)
    command = RUNTIME / "bin/butler-core"
    command.write_text(f'#!/bin/sh\nexec "{python}" -m butler_core.cli "$@"\n')
    command.chmod(0o755)
    for directory in WORKSPACE_BINS:
        target = directory / "butler-core"
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(command, target)
    publish_client()
    return python, command


def publish_client():
    """Atomically replace the workspace copy of the client (CLIs may import it at any moment)."""
    temporary = WORKSPACE_CLIENT.with_name(".butler_core_client.py.tmp")
    shutil.copy2(ROOT / "butler_core/client.py", temporary)
    os.replace(temporary, WORKSPACE_CLIENT)
    return hashlib.sha256(WORKSPACE_CLIENT.read_bytes()).hexdigest()


def stage_bridge():
    """Copy the bridge plugin into the runtime; a linked plugin's code is loaded at the next gateway start."""
    (BRIDGE / "lib").mkdir(parents=True, exist_ok=True)
    for name in BRIDGE_FILES:
        shutil.copy2(ROOT / "bridge" / name, BRIDGE / name)
    modules = BRIDGE / "node_modules"
    modules.mkdir(exist_ok=True)
    link = modules / "openclaw"
    if not link.is_symlink():
        link.symlink_to("/opt/homebrew/lib/node_modules/openclaw")
    return hashlib.sha256(b"".join((BRIDGE / n).read_bytes() for n in BRIDGE_FILES)).hexdigest()


def openclaw(*args, check=True):
    env = dict(os.environ, PATH=f"{NODE_BIN}:{os.environ.get('PATH', '')}")
    return subprocess.run([OPENCLAW, *map(str, args)], check=check, capture_output=True, text=True, env=env,
                          timeout=300)


def install_bridge():
    """The single approved gateway restart (DEC-045, DEC-052): debounce first, then the plugin install."""
    config = json.loads(OPENCLAW_CONFIG.read_text())
    if "butler-core-bridge" in config.get("plugins", {}).get("entries", {}):
        return {"installed": "already"}
    backup = OPENCLAW_CONFIG.with_name("openclaw.json.pre-butler-core-bridge-2026-10-24")
    if not backup.exists():
        shutil.copy2(OPENCLAW_CONFIG, backup)
    if config.get("messages", {}).get("inbound", {}).get("byChannel", {}).get("telegram") != DEBOUNCE_MS:
        openclaw("config", "set", "messages.inbound.byChannel.telegram", DEBOUNCE_MS, "--strict-json")
    result = openclaw("plugins", "install", "--link", BRIDGE)
    return {"installed": True, "output": result.stdout.strip().splitlines()[-3:]}


def write_config():
    has_state = (STATE / "core.sqlite3").exists()
    # The very first start activates v1 exactly (DEC-050); later deployments add the waves offline (DEC-060).
    content = desired_config() if has_state else initial_config()
    text = json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    target = STATE / ("config-generated.json" if has_state else "bootstrap-config.json")
    target.write_text(text)
    target.chmod(0o600)
    return target, hashlib.sha256(text.encode()).hexdigest()


def stop_agent():
    domain = f"gui/{os.getuid()}"
    run("launchctl", "bootout", f"{domain}/{LABEL}", check=False)
    for _ in range(50):
        if run("launchctl", "print", f"{domain}/{LABEL}", check=False).returncode:
            return
        time.sleep(0.2)
    raise SystemExit("butler-core launch agent did not stop")


def activate_offline(path):
    """DEC-060: activate the generated ConfigVersion while the daemon is stopped; prints changed paths only."""
    done = run(RUNTIME / "bin/butler-core", "admin", "activate", path, "--state", STATE, check=False)
    if done.returncode:
        raise SystemExit(f"offline activation failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


def launch_agent():
    plist = {"Label": LABEL, "ProgramArguments": [str(APP / "Contents/MacOS/ButlerCore")],
             "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ExitTimeOut": 30,
             "WorkingDirectory": str(RUNTIME), "ProcessType": "Background",
             "StandardOutPath": str(STATE / "logs/launcher.log"),
             "StandardErrorPath": str(STATE / "logs/launcher-error.log")}
    AGENT_PLIST.write_bytes(plistlib.dumps(plist))
    AGENT_PLIST.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    stop_agent()
    run("launchctl", "bootstrap", domain, AGENT_PLIST)
    run("launchctl", "kickstart", f"{domain}/{LABEL}", check=False)


def wait_for_daemon(timeout=60):
    sys.path.insert(0, str(ROOT))
    from butler_core.client import Client, CoreError, CoreUnavailable
    client = Client(STATE / "core.sock")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client.call("doctor", _socket_timeout=5)
        except (CoreUnavailable, CoreError, OSError, socket.timeout):
            time.sleep(0.5)
    raise SystemExit("butler-core daemon did not answer within 60 s; see ~/.local/share/butler-core/logs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-restart", action="store_true", help="install files only")
    parser.add_argument("--install-bridge", action="store_true",
                        help="one-time: lower the Telegram debounce and install the bridge (restarts the gateway)")
    args = parser.parse_args()
    wheel = build()
    python, command = install(wheel)
    bridge_hash = stage_bridge()
    bridge = install_bridge() if args.install_bridge else None
    config_path, config_hash = write_config()
    activation = None
    if not args.no_restart:
        stop_agent()
        if config_path.name == "config-generated.json":
            activation = activate_offline(config_path)
        imported = import_failed_queue()
        launch_agent()
        doctor = wait_for_daemon()
    else:
        doctor = imported = None
    from butler_core import __version__
    result = {"version": __version__, "runtime": str(RUNTIME), "app": str(APP), "launch_agent": str(AGENT_PLIST),
              "wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "config_file": config_path.name, "config_sha256": config_hash, "config_activation": activation,
              "client_sha256": hashlib.sha256(WORKSPACE_CLIENT.read_bytes()).hexdigest(),
              "bridge_sha256": bridge_hash,
              "bridge_install": bridge, "failed_queue_import": imported,
              "running_version": doctor and doctor.get("core"), "active_config": doctor and doctor.get("config"),
              "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (STATE / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if doctor and doctor.get("core") != __version__:
        raise SystemExit("running version differs from the built version")


if __name__ == "__main__":
    main()
