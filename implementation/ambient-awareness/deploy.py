"""Install a private local runtime and a disabled-by-default launch agent.

Deployment is explicit. This script never enrolls, enables observation, sends a
message, restarts the shared gateway, or changes another agent's configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from build_app import install as install_ui

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
RUNTIME = HOME / ".openclaw/tools/ambient-awareness"
STATE = HOME / ".local/share/butler-ambient"
APP = RUNTIME / "ButlerAwareness.app"


def run(*args):
    subprocess.run(args, check=True)


def main():
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "logs").mkdir(exist_ok=True)
    run("uv", "venv", "--allow-existing", "--system-site-packages", "--python", "/opt/miniconda3/bin/python", str(RUNTIME / ".venv"))
    python = RUNTIME / ".venv/bin/python"
    # Install the tested wheel and the exact local sensing distributions used by QA.
    def wheel_version(path):
        # butler_ambient-0.3.10-py3-none-any.whl: compare as numbers, not text (0.3.10 sorts before 0.3.9 as text).
        return tuple(int(part) if part.isdigit() else 0 for part in path.name.split("-")[1].split("."))
    wheel = max((ROOT / "build/wheels").glob("*.whl"), key=wheel_version)
    run("uv", "pip", "install", "--python", str(python), "--no-deps", "--reinstall", str(wheel),
        "opencv-python-headless==4.11.0.86", "resemblyzer==0.1.4", "webrtcvad-wheels==2.0.14", "typing==3.7.4.3")
    # Smart Turn v3 (pipecat-ai, BSD-2) in its own environment: its dependency set (torch, transformers) must not
    # touch the runtime venv. Idempotent; the service runs it as a resident worker (butler_ambient/endpointing.py).
    turn_env = RUNTIME / "turn-env"
    run("uv", "venv", "--allow-existing", "--python", "/opt/miniconda3/bin/python", str(turn_env))
    run("uv", "pip", "install", "--python", str(turn_env / "bin/python"), "pipecat-ai[local-smart-turn,silero]==1.14.0")
    (RUNTIME / "native").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "build/butler-screen", RUNTIME / "native/butler-screen")
    shutil.copy2(ROOT / "build/butler-panel", RUNTIME / "native/butler-panel")
    shutil.copy2(ROOT / "build/butler-audio", RUNTIME / "native/butler-audio")
    shutil.copy2(ROOT / "build/butler-system-audio", RUNTIME / "native/butler-system-audio")
    shutil.copy2(ROOT / "build/butler-camera", RUNTIME / "native/butler-camera")
    shutil.copy2(ROOT / "build/butler-owner-auth", RUNTIME / "native/butler-owner-auth")
    aec_library = next((p for p in (Path("/opt/homebrew/lib/libspeexdsp.1.dylib"), Path("/usr/local/lib/libspeexdsp.1.dylib")) if p.is_file()), None)
    if aec_library is None:
        raise RuntimeError("Local echo processing requires speexdsp 1.2.1 (brew install speexdsp)")
    aec_target = RUNTIME / "native/libspeexdsp.1.dylib"
    aec_temporary = aec_target.with_suffix(".dylib.tmp")
    shutil.copy2(aec_library, aec_temporary)
    aec_temporary.replace(aec_target)
    contents = APP / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    info = {"CFBundleIdentifier": "com.boris.butler-awareness", "CFBundleName": "Butler Awareness",
            "CFBundleExecutable": "ButlerAwareness", "CFBundlePackageType": "APPL", "LSUIElement": True,
            "NSCameraUsageDescription": "Butler проверяет согласованных участников и записывает окружение только при разрешении.",
            "NSMicrophoneUsageDescription": "Butler проверяет участников по голосу и анализирует звук только локально."}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    run("swiftc", "-warnings-as-errors", str(ROOT / "butler_ambient/native/launcher.swift"), "-o", str(contents / "MacOS/ButlerAwareness"))
    run("codesign", "--force", "--sign", "-", "--identifier", "com.boris.butler-awareness", str(APP))
    (RUNTIME / "bin").mkdir(exist_ok=True)
    command = RUNTIME / "bin/butler-ambient"
    command.write_text('#!/bin/sh\nexec "' + str(python) + '" -m butler_ambient.cli --state "' + str(STATE) + '" --config "' + str(STATE / 'config.json') + '" "$@"\n')
    command.chmod(0o700)
    ui_executable = install_ui(ROOT / "build/Butler.app", HOME / "Applications/Butler.app")
    configuration = STATE / "config.json"
    if configuration.exists():
        values = json.loads(configuration.read_text())
        values["panel_helper"] = str(ui_executable)
        values.setdefault("owner_cli", str(command))
        values.setdefault("audio_helper", str(RUNTIME / "native/butler-audio"))
        values.setdefault("voice_python", str(RUNTIME / "voice-env/bin/python"))
        values.setdefault("expected_microphone", "Jabra SPEAK")
        values.setdefault("visual_enabled", True)
        values.setdefault("owner_name", "Борис")      # shown as the author of the owner's lines in the panel feed
        values.setdefault("image_python", str(RUNTIME / "image-env/bin/python"))
        values.setdefault("turn_python", str(RUNTIME / "turn-env/bin/python"))
        values.setdefault("voice_brain", "agent")      # DEC-067: the butler agent answers the owner's addressed words
        configuration.write_text(json.dumps(values, indent=2) + "\n")
        configuration.chmod(0o600)
    plist = {"Label": "com.boris.butler-awareness", "ProgramArguments": [str(contents / "MacOS/ButlerAwareness")],
             "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ExitTimeOut": 30,
             "WorkingDirectory": str(RUNTIME), "ProcessType": "Interactive",
             "StandardOutPath": str(STATE / "logs/launcher.log"), "StandardErrorPath": str(STATE / "logs/launcher-error.log")}
    target = HOME / "Library/LaunchAgents/com.boris.butler-awareness.plist"
    target.write_bytes(plistlib.dumps(plist))
    target.chmod(0o600)
    # Workspace-scoped MCP is intentional: the other two bots must not inherit
    # access to Boris's personal observations through a global MCP declaration.
    workspace = HOME / ".openclaw/workspace-butler"
    config_path = workspace / "config/mcporter.json"
    config_path.parent.mkdir(exist_ok=True)
    if config_path.exists():
        backup = STATE / "mcporter-before-deployment.json"
        if not backup.exists(): shutil.copy2(config_path, backup)
        config = json.loads(config_path.read_text())
    else:
        config = {"mcpServers": {}}
    config.setdefault("mcpServers", {})["butler-ambient"] = {"command": str(command), "args": ["mcp"]}
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    config_path.chmod(0o600)
    workspace_command = workspace / "bin/butler-ambient"
    if workspace_command.exists() and workspace_command.read_text() != command.read_text():
        backup = STATE / "butler-ambient-before-deployment"
        if not backup.exists(): shutil.copy2(workspace_command, backup)
    shutil.copy2(command, workspace_command)
    result = {"runtime": str(RUNTIME), "app": str(APP), "launch_agent": str(target),
              "mcp_config": str(config_path), "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "observation_enabled": False, "note": "Installed. Launch/enrollment/enablement are separate explicit steps."}
    (STATE / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
