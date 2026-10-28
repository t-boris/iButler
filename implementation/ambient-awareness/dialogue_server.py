"""DEC-073: install Butler's own local dialogue Ollama service, without touching shared runners."""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

LABEL = "com.boris.butler-dialogue-ollama"
PORT = 11435


def configure_background(home=None):
    home = Path(home or Path.home())
    path = home / "Library/LaunchAgents/sh.brew.ollama.plist"
    try:
        value = plistlib.loads(path.read_bytes())
    except FileNotFoundError:
        # Shared Ollama can be managed manually instead of by Homebrew. Its
        # absent optional launch agent must not interrupt a Butler release.
        return {"changed": False, "available": False, "reason": "background-launch-agent-not-installed"}
    arguments = value.get("ProgramArguments", [])
    if len(arguments) != 2 or Path(arguments[0]).name != "ollama" or arguments[1] != "serve":
        raise RuntimeError("unexpected background Ollama service")
    expected = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0",
                "OLLAMA_MAX_LOADED_MODELS": "5", "OLLAMA_NUM_PARALLEL": "2"}
    env = value.setdefault("EnvironmentVariables", {})
    domain = f"gui/{os.getuid()}"
    if all(env.get(k) == v for k, v in expected.items()):
        if subprocess.run(["launchctl", "print", domain + "/sh.brew.ollama"], capture_output=True).returncode:
            bootstrap(domain, path)
        return {"changed": False, "environment": expected}
    backup = path.with_suffix(".plist.before-attention")
    if not backup.exists(): shutil.copy2(path, backup)
    env.update(expected)
    path.write_bytes(plistlib.dumps(value)); path.chmod(0o600)
    subprocess.run(["launchctl", "bootout", domain + "/sh.brew.ollama"], check=True, capture_output=True)
    bootstrap(domain, path)
    return {"changed": True, "environment": expected, "backup": str(backup)}


def bootstrap(domain, path):
    for delay in (.3, 1., 2., 3.):
        time.sleep(delay)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True)
        if result.returncode == 0: return
    raise RuntimeError("launch agent bootstrap failed; original plist backup retained")


def launch_agent(home, executable):
    home = Path(home)
    logs = home / ".local/share/butler-ambient/logs"
    return {"Label": LABEL, "ProgramArguments": [str(executable), "serve"], "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "EnvironmentVariables": {"HOME": str(home), "OLLAMA_HOST": f"127.0.0.1:{PORT}",
                "OLLAMA_NUM_PARALLEL": "2", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_FLASH_ATTENTION": "1",
                "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_NO_CLOUD": "1"},
            "StandardOutPath": str(logs / "dialogue-ollama.log"), "StandardErrorPath": str(logs / "dialogue-ollama-error.log")}


def install(home=None):
    home = Path(home or Path.home())
    executable = shutil.which("ollama")
    if executable is None: raise RuntimeError("local Ollama binary unavailable")
    target = home / "Library/LaunchAgents" / (LABEL + ".plist")
    target.parent.mkdir(parents=True, exist_ok=True)
    (home / ".local/share/butler-ambient/logs").mkdir(parents=True, exist_ok=True)
    content = plistlib.dumps(launch_agent(home, executable))
    service = f"gui/{os.getuid()}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0
    if target.exists() and target.read_bytes() == content and loaded:
        return {"label": LABEL, "endpoint": f"http://127.0.0.1:{PORT}", "already_configured": True}
    if loaded:
        subprocess.run(["launchctl", "bootout", service], check=True, capture_output=True)
    # Refuse to take over a port owned by an unrelated process.
    import socket
    probe = socket.socket(); probe.settimeout(.3)
    try: occupied = probe.connect_ex(("127.0.0.1", PORT)) == 0
    finally: probe.close()
    if occupied: raise RuntimeError("dialogue port belongs to another process")
    if target.exists():
        backup = target.with_suffix(".plist.before-attention")
        if not backup.exists(): shutil.copy2(target, backup)
    target.write_bytes(content); target.chmod(0o600)
    bootstrap(f"gui/{os.getuid()}", target)
    return {"label": LABEL, "endpoint": f"http://127.0.0.1:{PORT}", "plist_sha256": hashlib.sha256(content).hexdigest()}


if __name__ == "__main__": print(json.dumps({"background": configure_background(), "dialogue": install()}, indent=2))
