"""Dispatch — route a project task to the right backend.

Routing table (per Boris's choice 2026-05-26):
    architecture  → codex (gpt-6.1-sol)
    code          → claude opus 4.8
    review        → claude sonnet 5
    research      → codex (with opus 4.8 fallback when codex unavailable)
    documentation → codex
    tests         → claude sonnet 5
    video         → claude sonnet 5 (evidence-linked storyboard + local renderer)
    backburner    → claude opus 5.5, CONFINED (no shell, staging folder only) and SILENT

Two execution modes:

  1. SYNC (default) — `dispatch(...)` blocks until done, returns DispatchResult.
     Good for quick tasks (review of 1 file, doc tweak).

  2. ASYNC — `start_dispatch(...)` spawns detached, returns dispatch_id immediately.
     Butler can poll `status(id)` / `tail(id, n)` / `wait(id, timeout)`.
     Good for long tasks (architecture design, large refactor) — butler stays
     responsive, user can ask "как там?" any time.

State for async dispatches lives under state/dispatches/<id>/.

The butler agent calls this via Bash:
    # quick
    butler-dispatch code --project ~/github.com/X --prompt "..."
    # long-running
    ID=$(butler-dispatch start code --project ~/github.com/X --prompt "...")
    butler-dispatch status $ID
    butler-dispatch tail $ID 30
    butler-dispatch wait $ID 600
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from lib import cost as cost_mod

TaskKind = Literal["architecture", "code", "review", "research", "documentation", "tests", "curate", "classify", "news", "video", "backburner"]

CLAUDE_BIN = "/Users/boris/.local/bin/claude"
CODEX_BIN_CANDIDATES = [
    "/opt/homebrew/bin/codex",
    "/Users/boris/.nvm/versions/node/v22.22.3/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
]
OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
# Node runtime for the openclaw CLI. Must match the Node the gateway itself runs on
# (launchd: ~/.nvm/versions/node/v22.22.3/bin/node). Do NOT use Homebrew node@24:
# it is linked against libsimdjson.33 and died on the 2026-10 simdjson upgrade
# (every dispatch wake exited -6 from 2026-10-10 to 2026-10-23).
NODE_BIN = "/Users/boris/.nvm/versions/node/v22.22.3/bin"
WORKSPACE  = Path("/Users/boris/.openclaw/workspace-butler")
DISPATCH_ROOT = WORKSPACE / "state" / "dispatches"

# ── Task-completion wake (autonomous continuation) ──────────────────
# On completion the monitor wakes the butler agent in its live DM session so it
# can react / continue the goal. Bounded by CHAIN_DEPTH_CAP to stop runaway
# dispatch→wake→dispatch loops. (Decisions: Boris 2026-05-30 — autonomous, all
# async tasks, silent in quiet hours.)
BUTLER_SESSION_KEY = "agent:butler:telegram:direct:253483520"
CHAIN_DEPTH_CAP = 20
QUIET_START_HOUR = 22   # 22:00 local (CT) — inclusive
QUIET_END_HOUR   = 8    # 08:00 local (CT) — exclusive

# Wake is BEST-EFFORT and non-blocking under contention (Boris 2026-05-30 fix):
#   * a single global lock means at most ONE wake runs at a time — concurrent
#     completions skip-fast instead of stacking long-blocking `openclaw agent`
#     processes against a busy butler session;
#   * a bounded timeout caps a busy-session wait at WAKE_TIMEOUT_SECONDS (was 920s,
#     which hung ~15 min when the butler ran a parallel pipeline).
# A skipped/failed wake is recorded to PENDING_WAKES so the butler can reconcile
# (the ✅ to Boris already went out via _notify).
WAKE_TIMEOUT_SECONDS = 180
WAKE_LOCK = WORKSPACE / "state" / "wake.lock"
PENDING_WAKES = WORKSPACE / "state" / "pending-wakes.jsonl"

# Per-kind default timeouts (seconds). Coding a multi-file feature can take well over
# 30 min, so `code`/`architecture` get generous budgets; quick reviews stay short.
# Override per-call with --timeout.
ROUTES: dict[str, dict] = {
    "architecture":  {"backend": "codex",  "model": "gpt-6.1-sol", "timeout": 3600},
    "code":          {"backend": "claude", "model": "claude-opus-4-8",  "timeout": 7200},
    "review":        {"backend": "claude", "model": "claude-sonnet-5",  "timeout": 1800},
    "research":      {"backend": "codex",  "model": "gpt-6.1-sol", "fallback": ("claude", "claude-opus-4-8"), "timeout": 2700},
    "documentation": {"backend": "codex",  "model": "gpt-6.1-sol", "timeout": 2400},
    "tests":         {"backend": "claude", "model": "claude-sonnet-5",  "timeout": 2700},
    # Research CURATOR (①, Boris 2026-05-30): reads the whole context and fills the
    # research queue. Opus 4.8 max (cron overrides --model claude-opus-4-8). Produces
    # NO report — its completion wake is acknowledged, not run through research-NOTIFY.
    "curate":        {"backend": "claude", "model": "claude-opus-4-8",  "timeout": 2700},
    # Inbox CLASSIFIER (Phase 3, Boris 2026-05-30): reads the message buffer and
    # auto-routes updates into the vault (People/Daily/Tasks/Interests/Research/…).
    # Opus 4.8 (cron overrides --model). Produces NO report — its completion wake is
    # acknowledged like `curate`, not run through research-NOTIFY.
    "classify":      {"backend": "claude", "model": "claude-opus-4-8",  "timeout": 2700},
    # News digest (Boris 2026-06-01): claude WebSearch → categorized digest → vault.
    # Moved off codex (gpt-5.5) which intermittently hit the app-server idle timeout
    # on long Perplexity searches. No report → completion wake is acknowledged.
    "news":          {"backend": "claude", "model": "claude-opus-4-8",  "timeout": 2400},
    # News/deep-research video story editor. It only authors a cited episode JSON
    # and calls the deterministic local renderer. Paid TTS/Veo remain separately
    # approval-gated inside butler-news-video.
    "video":         {"backend": "claude", "model": "claude-sonnet-5",  "timeout": 3600},
    # Back Burner researcher (feature butler-back-burner-researcher, owner 2026-10-27): improves a STAGING
    # COPY of one vault folder overnight (lib/backburner.py applies the checked result). CONFINED: no Bash,
    # only the listed tools, file tools limited to the working directory, no MCP, no permission prompt.
    # SILENT: the harness reports to Boris, so no status line or butler wake. Opus 5.5.
    # ai-settings.json may change the model of this route, never `tools` or `silent`.
    "backburner":    {"backend": "claude", "model": "claude-opus-5-5",  "timeout": 2700, "silent": True,
                      "tools": ["Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch"]},
}

DEFAULT_TIMEOUT = 1800

# BUG-018: the Claude CLI writes its final stream-json "result" and then may not exit (a background task of its own keeps
# it alive). 8 of 18 video dispatches since 2026-11-02 were recorded as failed by the one-hour timeout although that result
# was a success. A worker whose last record is a "result" and that stayed silent this long is finished and is ended; output
# after a result (a new turn started by a finished background task) resets the wait.
RESULT_IDLE_GRACE = 300
MONITOR_POLL = 5
TERMINATE_GRACE = 15


# BUTLER_OWNER_AI_SETTINGS — local Settings; existing cost guards remain below.
def _owner_ai_settings():
    path = WORKSPACE / "config/ai-settings.json"
    if not path.exists():
        return
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(path.read_text())
        approved = {(v["backend"], v["model"]) for v in ROUTES.values()}
        catalog_path = WORKSPACE / "config/model-catalog.json"
        if catalog_path.exists():
            import time as _owner_time
            import re as _owner_re
            if catalog_path.is_symlink() or catalog_path.stat().st_size > 1000000:
                raise ValueError()
            catalog = json.loads(catalog_path.read_text())
            if catalog.get("version") != 1:
                raise ValueError()
            legacy_approved = approved
            approved = set()
            for backend, provider in catalog.get("providers", {}).items():
                if backend not in {"claude", "codex"}:
                    continue
                if not provider.get("fetched_at"):
                    approved.update(pair for pair in legacy_approved if pair[0] == backend)
                    continue
                if not 0 <= _owner_time.time() - provider.get("fetched_at", 0) <= 604800:
                    continue
                for model in provider.get("models", []):
                    for name in [model.get("id"), *model.get("aliases", [])]:
                        if isinstance(name, str) and _owner_re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\[\]-]{0,149}", name):
                            approved.add((backend, name))
        routes = data.get("routes", {})
        for kind, value in routes.items():
            if kind not in ROUTES or set(value) != {"backend", "model"} or (value["backend"], value["model"]) not in approved:
                raise ValueError()
        executables = data.get("executables", {})
        for backend, binary in executables.items():
            if backend not in {"claude", "codex"} or not isinstance(binary, str) or Path(binary).name != backend or not shutil.which(binary):
                raise ValueError()
        for kind, value in routes.items():
            ROUTES[kind] = {**ROUTES[kind], **value}
        global CLAUDE_BIN, CODEX_BIN_CANDIDATES
        CLAUDE_BIN = executables.get("claude", CLAUDE_BIN)
        if "codex" in executables:
            CODEX_BIN_CANDIDATES = [executables["codex"]]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError("Invalid owner Butler AI Settings; fix local configuration") from None

_owner_ai_settings()
# END BUTLER_OWNER_AI_SETTINGS



def _codex_bin() -> str:
    for candidate in CODEX_BIN_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    found = shutil.which("codex")
    if found:
        return found
    raise RuntimeError(f"codex CLI not found; tried: {', '.join(CODEX_BIN_CANDIDATES)}")

ALLOWED_PROJECT_ROOTS = [
    "/Users/boris/Projects",
    "/Users/boris/github.com",
    "/Users/boris/.openclaw/workspace-butler",
]


@dataclasses.dataclass
class DispatchResult:
    task_kind: str
    backend: str
    model: str
    project_path: str
    success: bool
    exit_code: int
    duration_sec: float
    stdout: str
    stderr: str
    estimated_cost_usd: float
    audit_id: str


# ───────────────────────────────────────────────────────────────────
# Path safety
# ───────────────────────────────────────────────────────────────────
def _resolve_project_path(p: str) -> Path:
    target = Path(p).expanduser().resolve()
    for root in ALLOWED_PROJECT_ROOTS:
        try:
            target.relative_to(root)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"project path {target} is outside allowed roots: {ALLOWED_PROJECT_ROOTS}")
    if not target.exists():
        raise ValueError(f"project path does not exist: {target}")
    if not target.is_dir():
        raise ValueError(f"project path is not a directory: {target}")
    return target


NOTIFY_PREAMBLE = """\
You are a dispatched worker. Boris is waiting in Telegram for progress.

You can send live status updates to his Telegram chat by calling the shell command:
  butler-notify "your status text here"

Required updates (use Bash tool to call butler-notify):
  1. AT START: butler-notify "starting — <one-line plan>"
  2. AT EACH MILESTONE (~every 2-3 min or after major sub-task): butler-notify "<what you found/did, 1 line>"
  3. AT END: butler-notify "done — <one-line summary, key paths>"

Keep messages short (<160 chars). Do not spam — at least 60s between updates unless something critical happens.

────────────────────────────────────────
TASK:

"""

def _wrap_prompt(prompt: str) -> str:
    return NOTIFY_PREAMBLE + prompt


# Every butler-* CLI a worker calls (recall, people, task, daily, notify, ...) talks to butler-core over the Unix socket
# ~/.local/share/butler-core/core.sock. Codex's `workspace-write` seatbelt denies that connect(): PermissionError
# [Errno 1], so from 2026-10-28 on every Codex worker (classify, curate, news, research) could write nothing and left its
# inbox/queue untouched. A socket-only allowance (`permissions.*.network.unix_sockets`) is accepted by codex 0.167.0 but
# never reaches the macOS policy (openai/codex#25416); `network_access` is the switch that does (verified 2026-11-07 with a
# live `codex exec`: connect() fails without it, succeeds with it). The filesystem sandbox stays `workspace-write`.
# Owner approved this trade-off 2026-11-07: Claude workers already run unsandboxed (`bypassPermissions`).
CODEX_CORE_SOCKET_OVERRIDE = "sandbox_workspace_write.network_access=true"


def _build_command(backend: str, model: str, prompt: str, *, streaming: bool) -> list[str]:
    """Build the subprocess argv for a backend invocation."""
    prompt = _wrap_prompt(prompt)
    if backend == "claude":
        if not shutil.which(CLAUDE_BIN):
            raise RuntimeError(f"claude CLI not found at {CLAUDE_BIN}")
        # stream-json for async (so output.log fills line-by-line); json for sync (parse usage)
        out_fmt = "stream-json" if streaming else "json"
        cmd = [CLAUDE_BIN, "-p", prompt, "--model", model,
               "--output-format", out_fmt,
               "--permission-mode", "bypassPermissions"]
        if streaming:
            cmd += ["--include-partial-messages", "--verbose"]
        return cmd
    elif backend == "codex":
        return [
            _codex_bin(),
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-c",
            CODEX_CORE_SOCKET_OVERRIDE,
            "-m",
            model,
            prompt,
        ]
    else:
        raise ValueError(f"unknown backend: {backend}")


def _check_confined(task_kind: Optional[str], backend: str) -> None:
    """A route with `tools` is confined by claude's own flags; no other backend can honour that."""
    if ROUTES.get(task_kind or "", {}).get("tools") and backend != "claude":
        raise ValueError(f"route {task_kind} is confined and runs only on claude, not {backend}")


def _confined_command(task_kind: str, backend: str, model: str, prompt: str, *, streaming: bool) -> list[str]:
    """argv for a CONFINED route: claude --restricted (no Bash or other code-running tool, file tools limited
    to the working directory, user/project settings ignored), exactly the listed tools, no MCP servers, and
    nothing may prompt. The prompt is passed bare: the worker has no shell to call butler-notify from."""
    _check_confined(task_kind, backend)
    if not shutil.which(CLAUDE_BIN):
        raise RuntimeError(f"claude CLI not found at {CLAUDE_BIN}")
    tools = ",".join(ROUTES[task_kind]["tools"])
    out_fmt = "stream-json" if streaming else "json"
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", model,
           "--output-format", out_fmt,
           "--restricted", "--tools", tools, "--allowedTools", tools,
           "--strict-mcp-config", "--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    if streaming:
        cmd += ["--include-partial-messages", "--verbose"]
    return cmd


def _command(task_kind: Optional[str], backend: str, model: str, prompt: str, *, streaming: bool) -> list[str]:
    """argv for a task. Only a route with `tools` is built differently; every other route goes through
    `_build_command` with exactly the arguments it always had."""
    if ROUTES.get(task_kind or "", {}).get("tools"):
        return _confined_command(task_kind, backend, model, prompt, streaming=streaming)
    return _build_command(backend, model, prompt, streaming=streaming)


def _model_key(backend: str, model: str) -> str:
    if backend == "claude":
        # Explicit IDs pass through; bare aliases resolve to the pinned generation
        # (Boris 2026-07-20: sonnet → Sonnet 5, opus → Opus 4.8 with 1M context).
        aliases = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
        return f"anthropic/{aliases.get(model, model)}"
    return f"openai/{model}"


# ───────────────────────────────────────────────────────────────────
# SYNC dispatch
# ───────────────────────────────────────────────────────────────────
def dispatch(
    task_kind: str,
    project_path: str,
    prompt: str,
    *,
    backend_override: Optional[str] = None,
    model_override: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DispatchResult:
    if task_kind not in ROUTES:
        raise ValueError(f"unknown task_kind: {task_kind}. Valid: {sorted(ROUTES.keys())}")
    cwd = _resolve_project_path(project_path)
    route = ROUTES[task_kind]
    if timeout is None:
        timeout = route.get("timeout", DEFAULT_TIMEOUT)
    backend = backend_override or route["backend"]
    model   = model_override   or route["model"]

    started = time.time()
    cmd = _command(task_kind, backend, model, prompt, streaming=False)
    # Backend auth hygiene (mirror of start_dispatch): force codex onto Boris's
    # logged-in ChatGPT subscription at ~/.codex and drop any stale OPENAI_API_KEY
    # inherited from the login shell, so a dispatched `codex exec` never falls back
    # to per-token API billing. Harmless for the claude backend.
    run_env = os.environ.copy()
    run_env["CODEX_HOME"] = os.path.expanduser("~/.codex")
    run_env.pop("OPENAI_API_KEY", None)
    # NO API KEYS (Boris hard rule 2026-05-30): the claude backend must run on the
    # logged-in Claude Code subscription (OAuth), never a per-token API key. Mirrors
    # the gateway's cliBackends.claude-cli.clearEnv. Harmless for codex.
    run_env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=run_env)
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return DispatchResult(task_kind=task_kind, backend=backend, model=model,
                              project_path=str(cwd), success=False, exit_code=-1,
                              duration_sec=timeout, stdout="", stderr=f"timeout after {timeout}s",
                              estimated_cost_usd=0.0, audit_id="")
    except RuntimeError as e:
        # Backend binary missing — try fallback if defined
        fb = route.get("fallback")
        if fb and not backend_override:
            backend, model = fb
            sys.stderr.write(f"{e}; falling back to {backend}/{model}\n")
            cmd = _command(task_kind, backend, model, prompt, streaming=False)
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=run_env)
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        else:
            raise

    # Parse usage from claude JSON output
    usage, text_out = {}, stdout
    if backend == "claude":
        try:
            parsed = json.loads(stdout)
            usage, text_out = parsed.get("usage", {}), parsed.get("result", stdout)
        except (json.JSONDecodeError, AttributeError):
            pass

    duration = time.time() - started
    est = cost_mod.estimate(backend, _model_key(backend, model),
                            usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                            usage.get("output_tokens", 0))
    if usage:
        cost_mod.log(est, job_kind=f"dispatch:{task_kind}", project_id=cwd.name,
                     source=f"dispatch:{backend}")
    return DispatchResult(task_kind=task_kind, backend=backend, model=model,
                          project_path=str(cwd), success=(exit_code == 0), exit_code=exit_code,
                          duration_sec=duration, stdout=text_out, stderr=stderr,
                          estimated_cost_usd=est.estimated_usd,
                          audit_id=f"dispatch-{int(started)}")


# ───────────────────────────────────────────────────────────────────
# ASYNC dispatch (background + status polling)
# ───────────────────────────────────────────────────────────────────
def _dispatch_dir(dispatch_id: str) -> Path:
    return DISPATCH_ROOT / dispatch_id


DISPATCH_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{6}")
# A monitor that has not recorded its pid this long after start is treated as gone.
START_GRACE_SECONDS = 120
CANCELLED_EXIT = -15


def new_dispatch_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _derived_status(meta: dict, result: Optional[dict], alive: bool) -> str:
    """What the job really is: a monitor that died without a result is `interrupted`,
    never `running` forever."""
    if result is not None:
        code = result.get("exit_code")
        return "done" if code == 0 else "cancelled" if code == CANCELLED_EXIT else "failed"
    if alive:
        return "running"
    if meta.get("status") != "running":
        return meta.get("status") or "unknown"
    if not meta.get("monitor_pid") and time.time() - meta.get("started_at_ts", 0) < START_GRACE_SECONDS:
        return "running"
    return "interrupted"


def start_dispatch(
    task_kind: str,
    project_path: str,
    prompt: str,
    *,
    backend_override: Optional[str] = None,
    model_override: Optional[str] = None,
    timeout: Optional[int] = None,
    dispatch_id: Optional[str] = None,
) -> str:
    """Spawn dispatch detached. Returns dispatch_id immediately. State at state/dispatches/<id>/.
    timeout=None → use the per-kind default from ROUTES (e.g. code=7200s, review=1800s).
    dispatch_id is given by butler-core, which records the operation before it starts."""
    if task_kind not in ROUTES:
        raise ValueError(f"unknown task_kind: {task_kind}. Valid: {sorted(ROUTES.keys())}")
    cwd = _resolve_project_path(project_path)
    route = ROUTES[task_kind]
    if timeout is None:
        timeout = route.get("timeout", DEFAULT_TIMEOUT)
    backend = backend_override or route["backend"]
    model   = model_override   or route["model"]
    _check_confined(task_kind, backend)

    if dispatch_id is None:
        dispatch_id = new_dispatch_id()
    elif not DISPATCH_ID_RE.fullmatch(dispatch_id):
        raise ValueError("invalid dispatch id")
    d = _dispatch_dir(dispatch_id)
    if d.exists():
        raise ValueError(f"dispatch {dispatch_id} already exists")
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.log").touch()
    (d / "stderr.log").touch()

    # Chain depth: 0 for a dispatch Boris/butler starts fresh; the butler advances it
    # by prefixing `BUTLER_CHAIN_DEPTH=<n>` when it dispatches a follow-up after a wake.
    try:
        chain_depth = max(0, int(os.environ.get("BUTLER_CHAIN_DEPTH", "0")))
    except ValueError:
        chain_depth = 0

    meta = {
        "dispatch_id": dispatch_id,
        "task_kind": task_kind,
        "backend": backend,
        "model": model,
        "project_path": str(cwd),
        "prompt": prompt,
        "timeout": timeout,
        "chain_depth": chain_depth,
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "started_at_ts": int(time.time()),
        "status": "running",
        "monitor_pid": None,
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2))

    # Spawn the monitor as a detached subprocess.
    # Pass BUTLER_NOTIFY_* so worker can post status updates to user's TG via butler-notify CLI.
    monitor_cmd = [sys.executable, "-m", "lib.dispatch", "_monitor", dispatch_id]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE)
    env["BUTLER_NOTIFY_TARGET"]  = env.get("BUTLER_NOTIFY_TARGET",  "253483520")
    env["BUTLER_NOTIFY_ACCOUNT"] = env.get("BUTLER_NOTIFY_ACCOUNT", "butler")
    env["BUTLER_NOTIFY_PREFIX"]  = f"[{task_kind}:{dispatch_id[-6:]}]"
    env["PATH"] = f"{WORKSPACE}/bin:" + env.get("PATH", "")
    # codex auth: the gateway sets CODEX_HOME to an agent-specific dir with no auth.json,
    # so a dispatched `codex exec` gets 401. Force codex to use Boris's logged-in ChatGPT
    # credentials at ~/.codex. Stale OPENAI_API_KEY also causes 401 → drop it.
    env["CODEX_HOME"] = os.path.expanduser("~/.codex")
    env.pop("OPENAI_API_KEY", None)
    # NO API KEYS (Boris hard rule 2026-05-30): claude backend on OAuth subscription only.
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        monitor_cmd, cwd=str(WORKSPACE),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    meta["monitor_pid"] = proc.pid
    (d / "meta.json").write_text(json.dumps(meta, indent=2))
    return dispatch_id


def _notify(msg: str) -> None:
    """Best-effort: post status update to user's TG. Never raises."""
    try:
        subprocess.run(
            [str(WORKSPACE / "bin" / "butler-notify"), msg],
            timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _in_quiet_hours() -> bool:
    """True during local quiet hours (22:00–08:00 CT). Wake still fires, but silently."""
    h = dt.datetime.now().hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def _output_summary(d: Path, backend: str, limit: int = 400) -> str:
    """Short human summary of what the worker produced, for the wake message.
    For claude stream-json, prefer the final `result` text; else tail of output.log."""
    out = d / "output.log"
    if not out.exists():
        return ""
    try:
        text = out.read_text()
    except Exception:
        return ""
    if backend == "claude":
        for line in reversed(text.splitlines()):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "result" and isinstance(e.get("result"), str):
                text = e["result"]
                break
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text[-limit:] if len(text) > limit else text


def _wake_butler(dispatch_id: str, meta: dict, exit_code: int, chain_depth: int) -> None:
    """Best-effort: wake the butler agent in its live DM session with the completion
    event so it can react / continue the goal autonomously. NEVER raises — a failed
    wake is logged, not propagated to the worker. Mirrors butler-notify's retry policy.

    Daytime: `--deliver` so the butler's reply pings Boris.
    Quiet hours: no `--deliver` (no ping); butler is told to surface anything via the
    always-silent butler-notify.
    """
    proj = Path(meta["project_path"]).name
    kind = meta["task_kind"]
    backend, model = meta["backend"], meta["model"]
    ok = exit_code == 0
    status_word = "OK" if ok else f"FAIL(exit={exit_code})"
    summary = _output_summary(_dispatch_dir(dispatch_id), backend) or "(no output captured)"
    quiet = _in_quiet_hours()
    next_depth = chain_depth + 1
    at_cap = next_depth > CHAIN_DEPTH_CAP

    lines = [
        f"[task-done] dispatch {dispatch_id} — {kind} via {backend}/{model} on {proj}: {status_word}.",
        f"Summary: {summary}",
        f"Chain depth {chain_depth}/{CHAIN_DEPTH_CAP}.",
        "Autonomous continuation is ON: assess this result and take the next step toward "
        "the original goal. Full output: `butler-dispatch tail " + dispatch_id + " 80`.",
        "When you dispatch the next step, prefix it with "
        f"`BUTLER_CHAIN_DEPTH={next_depth}` so the chain counter advances.",
        "HARD LIMITS (unchanged): deploy / push / delete / secrets / $0.10+ direct API "
        'spend still require Boris\'s explicit "да".',
    ]
    if at_cap:
        lines.append(
            f"⛔ CHAIN CAP REACHED ({CHAIN_DEPTH_CAP}): do NOT dispatch further. "
            "Summarize where the chain stands and ask Boris how to proceed."
        )
    if quiet:
        lines.append(
            "Quiet hours (22:00–08:00): do NOT expect auto-delivery. If you must surface "
            "something to Boris, use `butler-notify` (silent). Otherwise just continue the work."
        )
    message = "\n".join(lines)

    cmd = [OPENCLAW_BIN, "agent", "--session-key", BUTLER_SESSION_KEY,
           "--message", message, "--timeout", str(WAKE_TIMEOUT_SECONDS)]
    if not quiet:
        cmd.append("--deliver")

    env = os.environ.copy()
    env["PATH"] = f"{NODE_BIN}:" + env.get("PATH", "")
    fail_log = Path.home() / ".openclaw" / "wrappers" / "locks" / "notify-failures.log"

    # Single global wake lock — NON-BLOCKING. If another wake is already running
    # (butler session busy), skip-fast and record for catch-up rather than stacking
    # a long-blocking `openclaw agent` against the busy session. The bounded
    # --timeout caps any single wait at WAKE_TIMEOUT_SECONDS.
    WAKE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(WAKE_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        _record_pending_wake(dispatch_id, kind, exit_code, "skipped: another wake in flight (butler busy)")
        _log_wake_failure(fail_log, f"skipped {dispatch_id}: another wake in flight")
        lock_fh.close()
        return
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=WAKE_TIMEOUT_SECONDS + 15, env=env)
        if proc.returncode == 0:
            return
        out = (proc.stdout or "") + (proc.stderr or "")
        _record_pending_wake(dispatch_id, kind, exit_code, f"wake exit {proc.returncode}")
        _log_wake_failure(fail_log, f"non-transient (exit {proc.returncode}) for {dispatch_id}: {out.strip()[-200:]}")
    except subprocess.TimeoutExpired:
        _record_pending_wake(dispatch_id, kind, exit_code, "timeout (butler busy)")
        _log_wake_failure(fail_log, f"timeout waking butler for {dispatch_id} after {WAKE_TIMEOUT_SECONDS}s")
    except Exception as e:  # noqa: BLE001 — best-effort, never propagate
        _record_pending_wake(dispatch_id, kind, exit_code, f"crash: {e}")
        _log_wake_failure(fail_log, f"crash waking butler for {dispatch_id}: {e}")
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


def _record_pending_wake(dispatch_id: str, kind: str, exit_code: int, reason: str) -> None:
    """Append a dropped/failed wake to the catch-up log so the butler can reconcile
    later (e.g. via `butler-dispatch list`). Best-effort; never raises."""
    try:
        PENDING_WAKES.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_WAKES.open("a") as f:
            f.write(json.dumps({
                "dispatch_id": dispatch_id, "task_kind": kind, "exit_code": exit_code,
                "reason": reason, "at": dt.datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log_wake_failure(fail_log: Path, msg: str) -> None:
    try:
        fail_log.parent.mkdir(parents=True, exist_ok=True)
        with fail_log.open("a") as f:
            f.write(f"[{dt.datetime.now().strftime('%H:%M:%S')}] wake: {msg}\n")
    except Exception:
        pass


def _final_result(log: Path) -> Optional[dict]:
    """The last stream-json record of ``log`` when it is a ``result`` and the worker has written nothing since.

    A record that is still being written does not parse and so counts as "not a result yet"."""
    try:
        if time.time() - log.stat().st_mtime < RESULT_IDLE_GRACE:
            return None
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().splitlines()
    except OSError:
        return None
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get("type") == "result" else None


def _end_worker(proc: subprocess.Popen, *, politely: bool) -> None:
    """Stop a worker the monitor gave up on. ``politely``: SIGTERM first, SIGKILL if it ignores it."""
    if politely:
        proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_GRACE)
            return
        except subprocess.TimeoutExpired:
            pass
    proc.kill()
    proc.wait()


def _monitor(dispatch_id: str) -> int:
    """Run inside the detached process. Executes the backend, streams to output.log,
    writes result.json on completion. Updates meta.json status. Notifies TG at boundaries."""
    d = _dispatch_dir(dispatch_id)
    meta = json.loads((d / "meta.json").read_text())
    backend, model = meta["backend"], meta["model"]
    # A silent route (Back Burner) leaves reporting to its harness: no status lines, no butler wake.
    silent = bool(ROUTES.get(meta["task_kind"], {}).get("silent"))
    say = (lambda _message: None) if silent else _notify
    wake = (lambda *_args: None) if silent else _wake_butler
    try:
        cmd = _command(meta["task_kind"], backend, model, meta["prompt"], streaming=True)
    except Exception as e:
        (d / "stderr.log").write_text(f"[monitor] command setup failed: {e}\n")
        result = {
            "exit_code": -2,
            "duration_sec": 0,
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "finished_at_ts": int(time.time()),
            "estimated_cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        (d / "result.json").write_text(json.dumps(result, indent=2))
        meta["status"] = "failed"
        meta["finished_at"] = result["finished_at"]
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        say(f"❌ {meta['task_kind']} ({dispatch_id[-6:]}) — setup failed: {e}")
        wake(dispatch_id, meta, -2, int(meta.get("chain_depth", 0)))
        return 0
    started = time.time()

    # Initial notification — user knows worker is running
    proj_name = Path(meta["project_path"]).name
    say(f"▶ {meta['task_kind']} on {proj_name} ({backend}/{model}) — id {dispatch_id[-6:]}")

    try:
        with open(d / "output.log", "w") as fout, open(d / "stderr.log", "w") as ferr:
            proc = subprocess.Popen(cmd, cwd=meta["project_path"], stdout=fout, stderr=ferr)
            limit = meta.get("timeout", 1800)
            deadline = time.monotonic() + limit
            while True:
                try:
                    exit_code = proc.wait(timeout=MONITOR_POLL)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() >= deadline:
                    _end_worker(proc, politely=False)
                    ferr.write(f"\n[monitor] timeout after {limit}s\n")
                    exit_code = -1
                    say(f"⏱ {meta['task_kind']} ({dispatch_id[-6:]}) — TIMEOUT after {limit}s")
                    break
                final = _final_result(d / "output.log")
                if final is not None:
                    _end_worker(proc, politely=True)
                    finished = final.get("subtype") == "success" and not final.get("is_error")
                    exit_code = 0 if finished else 1
                    ferr.write(f"\n[monitor] the worker wrote its final result ({final.get('subtype')}) and stayed silent for "
                               f"{RESULT_IDLE_GRACE}s without exiting; ended by the monitor, outcome taken from the result\n")
                    break
    except Exception as e:
        (d / "stderr.log").write_text(f"[monitor] crash: {e}")
        exit_code = -2
        say(f"❌ {meta['task_kind']} ({dispatch_id[-6:]}) — crashed: {e}")

    duration = time.time() - started
    # Try to extract usage if backend=claude & last event in stream-json has usage
    usage = {}
    reported_cost = None
    if backend == "claude":
        try:
            for line in reversed((d / "output.log").read_text().splitlines()):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "result" and "usage" in e:
                    usage = e["usage"]
                    reported_cost = e.get("total_cost_usd")
                    break
        except Exception:
            pass
    est = cost_mod.estimate(backend, _model_key(backend, model),
                            usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                            usage.get("output_tokens", 0))
    # The price table lags the model lineup (Sonnet 5 and Opus 4.8 are not in it, so every run read $0.00): the CLI's own
    # figure for the run is the better number when it reports one.
    if isinstance(reported_cost, (int, float)) and reported_cost > 0:
        est = dataclasses.replace(est, estimated_usd=round(float(reported_cost), 6))
    if usage:
        cost_mod.log(est, job_kind=f"dispatch:{meta['task_kind']}",
                     project_id=Path(meta["project_path"]).name,
                     source=f"dispatch:{backend}")

    result = {
        "exit_code": exit_code,
        "duration_sec": round(duration, 2),
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "finished_at_ts": int(time.time()),
        "estimated_cost_usd": est.estimated_usd,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
    (d / "result.json").write_text(json.dumps(result, indent=2))

    meta["status"] = "done" if exit_code == 0 else "failed"
    meta["finished_at"] = result["finished_at"]
    (d / "meta.json").write_text(json.dumps(meta, indent=2))

    # Final notification with summary (factual line to Boris — always silent)
    icon = "✅" if exit_code == 0 else "❌"
    cost_str = f" ~${est.estimated_usd:.2f}" if est.estimated_usd else ""
    say(f"{icon} {meta['task_kind']} ({dispatch_id[-6:]}) done in {duration:.0f}s{cost_str}")

    # Wake the butler agent so it reacts / continues the goal autonomously.
    # Best-effort, bounded by CHAIN_DEPTH_CAP. Never affects the worker's exit.
    chain_depth = int(meta.get("chain_depth", 0))
    wake(dispatch_id, meta, exit_code, chain_depth)
    return 0


def status(dispatch_id: str) -> dict:
    d = _dispatch_dir(dispatch_id)
    if not d.exists():
        raise FileNotFoundError(f"no dispatch: {dispatch_id}")
    meta = json.loads((d / "meta.json").read_text())
    out_log = d / "output.log"
    err_log = d / "stderr.log"
    result_file = d / "result.json"

    out_lines = sum(1 for _ in out_log.open()) if out_log.exists() else 0
    err_lines = sum(1 for _ in err_log.open()) if err_log.exists() else 0

    # Process alive?
    alive = False
    mpid = meta.get("monitor_pid")
    if mpid:
        try:
            os.kill(mpid, 0)
            alive = True
        except (ProcessLookupError, PermissionError):
            alive = False

    result = json.loads(result_file.read_text()) if result_file.exists() else None
    meta["status"] = _derived_status(meta, result, alive)

    elapsed = (result["finished_at_ts"] - meta["started_at_ts"]) if result else (int(time.time()) - meta["started_at_ts"])
    return {
        "dispatch_id": dispatch_id,
        "task_kind": meta["task_kind"],
        "backend": meta["backend"],
        "model": meta["model"],
        "project_path": meta["project_path"],
        "status": meta["status"],
        "alive": alive,
        "elapsed_sec": elapsed,
        "output_lines": out_lines,
        "stderr_lines": err_lines,
        "started_at": meta["started_at"],
        "result": result,
    }


def tail(dispatch_id: str, n: int = 20, *, stream: str = "output") -> str:
    d = _dispatch_dir(dispatch_id)
    f = d / ("output.log" if stream == "output" else "stderr.log")
    if not f.exists():
        return ""
    lines = f.read_text().splitlines()
    return "\n".join(lines[-n:])


def wait(dispatch_id: str, timeout: int = 600, poll_interval: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = status(dispatch_id)
        if s["status"] in ("done", "failed", "cancelled", "interrupted"):
            return s
        time.sleep(poll_interval)
    s = status(dispatch_id)
    s["wait_timeout"] = True
    return s


def list_recent(n: int = 10) -> list[dict]:
    if not DISPATCH_ROOT.exists():
        return []
    dirs = [p for p in DISPATCH_ROOT.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.name, reverse=True)
    out = []
    for d in dirs[:n]:
        try:
            meta = json.loads((d / "meta.json").read_text())
            result_file = d / "result.json"
            result = json.loads(result_file.read_text()) if result_file.exists() else None
            if result is None and meta.get("status") == "running":
                meta["status"] = _derived_status(meta, None, _pid_alive(meta.get("monitor_pid")))
            out.append({
                "dispatch_id": d.name,
                "task_kind": meta.get("task_kind"),
                "backend": meta.get("backend"),
                "model": meta.get("model"),
                "project": Path(meta.get("project_path", "?")).name,
                "status": meta.get("status"),
                "started_at": meta.get("started_at"),
                "exit_code": result.get("exit_code") if result else None,
                "cost_usd": result.get("estimated_cost_usd") if result else None,
            })
        except Exception:
            continue
    return out


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill(dispatch_id: str) -> bool:
    """Best-effort terminate a running dispatch's monitor (and its children)."""
    d = _dispatch_dir(dispatch_id)
    if not d.exists():
        raise FileNotFoundError(f"no dispatch: {dispatch_id}")
    meta = json.loads((d / "meta.json").read_text())
    mpid = meta.get("monitor_pid")
    if not mpid:
        return False
    try:
        os.killpg(os.getpgid(mpid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ───────────────────────────────────────────────────────────────────
# butler-core path (switch `dispatch` in the active core ConfigVersion)
# ───────────────────────────────────────────────────────────────────
class CoreRefused(RuntimeError):
    """butler-core denied, failed or could not record the dispatch; nothing was started."""


def _core():
    """The deployed core client, or None when the core was never installed."""
    try:
        from lib import butler_core_client
    except ImportError:
        return None
    return butler_core_client


def core_switched() -> bool:
    core = _core()
    return bool(core and core.switched("dispatch"))


def core_start(task_kind: str, project_path: str, prompt: str, *, backend_override: Optional[str] = None,
               model_override: Optional[str] = None, timeout: Optional[int] = None) -> str:
    """Record the dispatch as a butler-core operation; the core starts and follows the worker.

    Validation happens here first so errors read exactly as on the legacy path.
    The core waits up to 30 s for a restarting daemon, then refuses (DEC-061)."""
    if task_kind not in ROUTES:
        raise ValueError(f"unknown task_kind: {task_kind}. Valid: {sorted(ROUTES.keys())}")
    cwd = _resolve_project_path(project_path)
    route = ROUTES[task_kind]
    backend = backend_override or route["backend"]
    if backend not in ("codex", "claude"):
        raise ValueError(f"unknown backend: {backend}")
    _check_confined(task_kind, backend)
    try:
        chain_depth = max(0, int(os.environ.get("BUTLER_CHAIN_DEPTH", "0")))
    except ValueError:
        chain_depth = 0
    dispatch_id = new_dispatch_id()
    params = {"dispatch_id": dispatch_id, "task_kind": task_kind, "project": str(cwd), "prompt": prompt,
              "backend": backend, "model": model_override or route["model"],
              "timeout": timeout or route.get("timeout", DEFAULT_TIMEOUT), "chain_depth": chain_depth}
    core = _core()
    client = core.Client()
    try:
        op = client.execute(f"dispatch.{backend}", "butler", f"project:{cwd}", params, f"dispatch:{dispatch_id}",
                            principal="agent:butler", timeout=90)
    except core.CoreUnavailable:
        raise CoreRefused("butler-core is unavailable; the dispatch was not started. Retry in a minute.") from None
    except core.CoreError as error:
        raise CoreRefused(f"butler-core refused the dispatch: {error.message or error.code}") from None
    state = op.get("state")
    if state == "running":
        return dispatch_id
    if state in ("requested", "dispatching"):
        sys.stderr.write(f"butler-core accepted the dispatch ({op.get('id')}); the worker is starting.\n")
        return dispatch_id
    reason = op.get("reason") or state
    raise CoreRefused(f"butler-core did not start the dispatch ({state}): {reason}")


def core_cancel(dispatch_id: str) -> Optional[dict]:
    """Stop a core-tracked dispatch through the core; None when the core does not know it."""
    core = _core()
    client = core.Client()
    try:
        op = client.call_waiting("operation.get", idempotency_key=f"dispatch:{dispatch_id}")
    except core.CoreError as error:
        if error.code == "not_found":
            return None
        raise CoreRefused(f"butler-core refused: {error.message or error.code}") from None
    except core.CoreUnavailable:
        raise CoreRefused("butler-core is unavailable; nothing was stopped. Retry in a minute.") from None
    return client.call("cancel", operation=op["id"])


def final_output(dispatch_id: str) -> str:
    """The worker's final answer: the claude `result` text, else the whole output log."""
    d = _dispatch_dir(dispatch_id)
    meta = json.loads((d / "meta.json").read_text())
    text = (d / "output.log").read_text() if (d / "output.log").exists() else ""
    if meta.get("backend") == "claude":
        for line in reversed(text.splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                return event["result"]
    return text


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────
def _print_routes_help():
    print("\n  Sync (block until done):", file=sys.stderr)
    print("    butler-dispatch <task_kind> --project <path> --prompt 'text' [opts]", file=sys.stderr)
    print("\n  Async (returns dispatch_id, monitor in background):", file=sys.stderr)
    print("    butler-dispatch start <task_kind> --project <path> --prompt 'text' [opts]", file=sys.stderr)
    print("    butler-dispatch status <dispatch_id>", file=sys.stderr)
    print("    butler-dispatch tail   <dispatch_id> [n_lines]   (default 20)", file=sys.stderr)
    print("    butler-dispatch wait   <dispatch_id> [timeout_sec]   (default 600)", file=sys.stderr)
    print("    butler-dispatch list   [n]   (default 10)", file=sys.stderr)
    print("    butler-dispatch kill   <dispatch_id>", file=sys.stderr)
    print("\n  Task kinds:    " + ", ".join(ROUTES.keys()), file=sys.stderr)
    print("  Options:       --backend claude|codex  --model <name>  --timeout <sec>  --json", file=sys.stderr)
    print("\n  Routes:", file=sys.stderr)
    for k, v in ROUTES.items():
        print(f"    {k:14}  → {v['backend']}/{v['model']}", file=sys.stderr)


def _parse_kwargs(args: list[str]) -> dict:
    out = {"project": None, "prompt": None, "backend": None, "model": None,
           "timeout": None, "as_json": False, "id": None}
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if   a == "--project":  out["project"] = rest.pop(0)
        elif a == "--prompt":   out["prompt"]  = rest.pop(0)
        elif a == "--backend":  out["backend"] = rest.pop(0)
        elif a == "--model":    out["model"]   = rest.pop(0)
        elif a == "--timeout":  out["timeout"] = int(rest.pop(0))
        elif a == "--json":     out["as_json"] = True
        elif a == "--id":       out["id"]      = rest.pop(0)  # internal: set by butler-core
        else:
            raise ValueError(f"unknown flag: {a}")
    return out


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        _print_routes_help()
        return 2

    sub = argv[0]

    # Internal: monitor subcommand (not user-facing)
    if sub == "_monitor":
        return _monitor(argv[1])

    # ASYNC subcommands
    if sub == "start":
        if len(argv) < 2:
            sys.stderr.write("usage: start <task_kind> --project P --prompt 'text' [opts]\n"); return 2
        task_kind = argv[1]
        try:
            kw = _parse_kwargs(argv[2:])
        except ValueError as e:
            sys.stderr.write(f"{e}\n"); return 2
        if not kw["project"] or not kw["prompt"]:
            sys.stderr.write("--project and --prompt are required\n"); return 2
        try:
            if kw["id"] is None and core_switched():
                did = core_start(task_kind, kw["project"], kw["prompt"],
                                 backend_override=kw["backend"], model_override=kw["model"],
                                 timeout=kw["timeout"])
            else:
                did = start_dispatch(task_kind, kw["project"], kw["prompt"],
                                     backend_override=kw["backend"], model_override=kw["model"],
                                     timeout=kw["timeout"], dispatch_id=kw["id"])
        except CoreRefused as e:
            sys.stderr.write(f"ERROR: {e}\n"); return 3
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"ERROR: {e}\n"); return 1
        print(did)  # stdout: just the id
        sys.stderr.write(f"started {kw['backend'] or ROUTES[task_kind]['backend']}/"
                         f"{kw['model'] or ROUTES[task_kind]['model']} "
                         f"in {Path(kw['project']).name}. "
                         f"Poll: butler-dispatch status {did}\n")
        return 0

    if sub == "status":
        if len(argv) < 2: sys.stderr.write("usage: status <id>\n"); return 2
        try:
            s = status(argv[1])
        except FileNotFoundError as e:
            sys.stderr.write(f"{e}\n"); return 1
        if "--json" in argv:
            print(json.dumps(s, indent=2))
        else:
            print(f"dispatch {s['dispatch_id']}")
            print(f"  task:       {s['task_kind']} → {s['backend']}/{s['model']}")
            print(f"  project:    {Path(s['project_path']).name}")
            print(f"  status:     {s['status']}  (alive={s['alive']}, {s['elapsed_sec']}s elapsed)")
            print(f"  output:     {s['output_lines']} lines  (stderr: {s['stderr_lines']})")
            if s["result"]:
                r = s["result"]
                print(f"  exit:       {r['exit_code']}")
                print(f"  cost est.:  ${r['estimated_cost_usd']:.4f}")
                print(f"  tokens:     {r.get('input_tokens',0)} in / {r.get('output_tokens',0)} out")
        return 0 if s["status"] != "failed" else 1

    if sub == "tail":
        if len(argv) < 2: sys.stderr.write("usage: tail <id> [n]\n"); return 2
        n = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 20
        stream = "stderr" if "--stderr" in argv else "output"
        sys.stdout.write(tail(argv[1], n, stream=stream))
        sys.stdout.write("\n")
        return 0

    if sub == "wait":
        if len(argv) < 2: sys.stderr.write("usage: wait <id> [timeout_sec]\n"); return 2
        t = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 600
        s = wait(argv[1], timeout=t)
        if "--json" in argv:
            print(json.dumps(s, indent=2))
        else:
            print(f"status: {s['status']}  (elapsed {s['elapsed_sec']}s)")
            if s["result"]:
                print(f"  exit={s['result']['exit_code']}  cost=${s['result']['estimated_cost_usd']:.4f}")
            if s.get("wait_timeout"):
                print(f"  [wait timed out after {t}s — process still running; poll again]")
        return 0 if s["status"] != "failed" else 1

    if sub == "list":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 10
        items = list_recent(n)
        if "--json" in argv:
            print(json.dumps(items, indent=2))
        else:
            if not items:
                print("(no dispatches)")
                return 0
            print(f"{'id':24}  {'task':14}  {'backend/model':22}  {'project':20}  {'status':8}  cost")
            for i in items:
                bm = f"{i['backend']}/{i['model']}"
                cost = f"${i['cost_usd']:.4f}" if i['cost_usd'] is not None else "-"
                print(f"  {i['dispatch_id']:22}  {i['task_kind']:14}  {bm:22}  {i['project']:20}  {i['status']:8}  {cost}")
        return 0

    if sub == "kill":
        if len(argv) < 2: sys.stderr.write("usage: kill <id>\n"); return 2
        try:
            stopped = core_cancel(argv[1]) if core_switched() else None
            if stopped is not None:
                print(f"stopped: {stopped.get('state')} ({stopped.get('mode')})")
                return 0
            ok = kill(argv[1])
        except CoreRefused as e:
            sys.stderr.write(f"ERROR: {e}\n"); return 3
        except FileNotFoundError as e:
            sys.stderr.write(f"{e}\n"); return 1
        print(f"killed: {ok}")
        return 0

    # SYNC default — sub is a task_kind
    if sub in ROUTES:
        try:
            kw = _parse_kwargs(argv[1:])
        except ValueError as e:
            sys.stderr.write(f"{e}\n"); return 2
        if not kw["project"] or not kw["prompt"]:
            sys.stderr.write("--project and --prompt are required\n"); return 2
        if core_switched():
            try:
                did = core_start(sub, kw["project"], kw["prompt"], backend_override=kw["backend"],
                                 model_override=kw["model"], timeout=kw["timeout"])
            except CoreRefused as e:
                sys.stderr.write(f"ERROR: {e}\n"); return 3
            except (ValueError, RuntimeError) as e:
                sys.stderr.write(f"ERROR: {e}\n"); return 1
            limit = (kw["timeout"] or ROUTES[sub].get("timeout", DEFAULT_TIMEOUT)) + 120
            deadline = time.time() + 60
            while not (_dispatch_dir(did) / "meta.json").exists() and time.time() < deadline:
                time.sleep(1)
            if not (_dispatch_dir(did) / "meta.json").exists():
                sys.stderr.write(f"ERROR: butler-core has not started dispatch {did} within 60 s; "
                                 f"check `butler-dispatch status {did}` later\n")
                return 3
            s = wait(did, timeout=limit)
            if kw["as_json"]:
                print(json.dumps(s, indent=2))
            else:
                sys.stdout.write(final_output(did))
                sys.stderr.write(f"\n--- dispatched via butler-core: {sub} → {s['backend']}/{s['model']} "
                                 f"({s['elapsed_sec']}s, status={s['status']}, id={did}) ---\n")
            return 0 if s["status"] == "done" else 1
        try:
            r = dispatch(sub, kw["project"], kw["prompt"],
                         backend_override=kw["backend"], model_override=kw["model"],
                         timeout=kw["timeout"])
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"ERROR: {e}\n"); return 1
        if kw["as_json"]:
            print(json.dumps(dataclasses.asdict(r), indent=2))
        else:
            sys.stdout.write(r.stdout)
            if r.stderr:
                sys.stderr.write(f"\n--- stderr ---\n{r.stderr}\n")
            sys.stderr.write(f"\n--- dispatched: {r.task_kind} → {r.backend}/{r.model} "
                             f"({r.duration_sec:.1f}s, ~${r.estimated_cost_usd:.4f}, exit={r.exit_code}) ---\n")
        return 0 if r.success else r.exit_code

    sys.stderr.write(f"unknown subcommand: {sub}\n")
    _print_routes_help()
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
