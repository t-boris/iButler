"""Adapters for the installed Butler workspace and ambient package (migration waves W2+).

Each adapter is offered only when its executor is installed, so a configuration
naming a missing adapter is rejected at validation instead of failing later.
"""
from __future__ import annotations

from pathlib import Path

from .exports import ExportAdapter
from .github import GitHubAdapter
from .mail import MailAdapter
from .todoist import TodoistAdapter
from .vault import KnowledgeAdapter, VaultAdapter
from .youtube import YouTubeAdapter
from .backburner import BackburnerAdapter
from .dispatcher import DispatchAdapter, VoiceTaskAdapter

HOME = Path.home()
WORKSPACE = HOME / ".openclaw/workspace-butler"
WORKSPACE_PYTHON = Path("/opt/miniconda3/bin/python")
AMBIENT_PYTHON = HOME / ".openclaw/tools/ambient-awareness/.venv/bin/python"
NODE_BIN = HOME / ".nvm/versions/node/v22.22.3/bin"
# The gateway's service PATH with the working Node 22 first (its node@24 entry no longer exists).
PATH = ":".join([str(NODE_BIN), str(HOME / ".local/bin"), "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


# The non-secret parts of the gateway's service environment that workers inherited before the migration.
TETYANA_WORKSPACE = HOME / ".openclaw/workspace-tetyana-dyck"
WORKER_ENV = {"TMPDIR": str(HOME / ".openclaw/tmp"), "NODE_EXTRA_CA_CERTS": "/etc/ssl/cert.pem", "NODE_USE_SYSTEM_CA": "1"}


def adapters():
    found = {"todoist": TodoistAdapter(), "export": ExportAdapter(), "knowledge": KnowledgeAdapter()}
    if (WORKSPACE / "lib/vault.py").is_file() and WORKSPACE_PYTHON.exists():
        found["vault"] = VaultAdapter(WORKSPACE, WORKSPACE_PYTHON, PATH, WORKER_ENV)
    if (WORKSPACE / "bin/butler-youtube").is_file():
        found["youtube"] = YouTubeAdapter(WORKSPACE, PATH, WORKER_ENV)
    if (WORKSPACE / "bin/butler-mail").is_file():
        found["mail"] = MailAdapter(WORKSPACE, PATH, WORKER_ENV)
    if (TETYANA_WORKSPACE / "bin/tetyana-pr").is_file():
        found["github"] = GitHubAdapter(TETYANA_WORKSPACE, PATH, WORKER_ENV)
    if (WORKSPACE / "lib/dispatch.py").is_file() and WORKSPACE_PYTHON.exists():
        found["dispatcher"] = DispatchAdapter(WORKSPACE, WORKSPACE_PYTHON, PATH, WORKER_ENV)
        if (WORKSPACE / "lib/backburner.py").is_file():
            found["backburner"] = BackburnerAdapter(WORKSPACE, WORKSPACE_PYTHON, PATH, WORKER_ENV)
        if AMBIENT_PYTHON.exists():
            found["voice-tasks"] = VoiceTaskAdapter(WORKSPACE, WORKSPACE_PYTHON, PATH, AMBIENT_PYTHON,
                                                    extra_env=WORKER_ENV)
    return found
