"""YouTube uploads as core operations (W5; inventory 12, 13, 14; DEC-064).

The core runs the workspace's own ``butler-youtube`` CLI, which keeps the
artifact checks, the destination allowlist and the OAuth tokens; the core only
decides, records and reconciles. An upload whose outcome is unknown is looked
up in the channel's uploads before any retry, so a video is never uploaded
twice. Credentials never pass through the core: the CLI reads them from the
persona's own credentials directory, named by the operation.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import DONE, NOT_DONE, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected

UPLOAD_TIMEOUT = 1900          # the resumable upload itself allows 1800 s
LOOKUP_TIMEOUT = 120
INDEX_GRACE = 20 * 60          # a finished upload may take minutes to appear in the channel listing
REFUSAL_MARKERS = ("AuthRequired", "ValidationError", "RenderError", "allowlist", "must be inside", "not found",
                   "checksum", "requires an explicit", "missing", "unsupported")


class YouTubeAdapter(Adapter):
    name = "youtube"
    tracked = False

    def __init__(self, workspace, path_env, extra_env=None, run=subprocess.run, clock=time.time):
        self.workspace = Path(workspace)
        self.cli = self.workspace / "bin/butler-youtube"
        self.path_env, self.extra_env = path_env, dict(extra_env or {})
        self.run, self.clock = run, clock

    # -- environment --------------------------------------------------------------------

    def environment(self, params):
        # The CLI gets only what it needs: the persona's credentials directory and runs root, no core secrets.
        env = {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
               "TMPDIR": os.environ.get("TMPDIR", "/tmp"), **self.extra_env, "PATH": self.path_env,
               "PYTHONPATH": str(self.workspace)}
        if params.get("credentials_dir"):
            env["BUTLER_YOUTUBE_CREDENTIALS"] = str(params["credentials_dir"])
        if params.get("runs_root"):
            env["BUTLER_NEWS_VIDEO_RUNS"] = str(params["runs_root"])
        return env

    def _cli(self, arguments, params, timeout):
        try:
            return self.run([str(self.cli), *arguments], capture_output=True, text=True, timeout=timeout,
                            env=self.environment(params), cwd=str(self.workspace))
        except FileNotFoundError:
            raise NotDispatched("butler-youtube is not installed") from None
        except subprocess.TimeoutExpired:
            raise Ambiguous(f"butler-youtube gave no answer within {timeout}s") from None

    @staticmethod
    def _payload(result):
        try:
            return json.loads(result.stdout)
        except (TypeError, ValueError):
            return None

    # -- execute ------------------------------------------------------------------------

    def execute(self, operation, cancel):
        params = operation["params"]
        if params.get("run_dir"):
            arguments = ["upload", str(params["run_dir"])]
        elif params.get("path"):
            if not params.get("title"):
                raise Rejected("a file upload needs a title")
            arguments = ["upload-file", str(params["path"])]
        else:
            raise Rejected("upload needs run_dir or path")
        arguments += ["--privacy", str(params.get("privacy") or "public"), "--profile", str(params.get("profile") or "default"),
                      "--core-operation", operation["id"]]
        for option in ("title", "description"):
            if params.get(option):
                arguments += [f"--{option}", str(params[option])]
        result = self._cli(arguments, params, UPLOAD_TIMEOUT)
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            if any(marker in (result.stderr or "") for marker in REFUSAL_MARKERS):
                raise Rejected(tail[0][:200])
            # The CLI may have died after the upload: never retry blindly.
            raise Ambiguous(f"butler-youtube exit {result.returncode}: {tail[0][:160]}")
        receipt = self._payload(result)
        if not isinstance(receipt, dict) or receipt.get("status") != "published" or not receipt.get("video_id"):
            raise Ambiguous("butler-youtube answered without a published receipt")
        return {"receipt": f"youtube:video:{receipt['video_id']}",
                "result": {k: receipt.get(k) for k in ("video_id", "url", "api_privacy_status", "privacy") if receipt.get(k)}}

    # -- reconcile ----------------------------------------------------------------------

    def receipt_path(self, params):
        if params.get("run_dir"):
            return Path(params["run_dir"]) / "publish" / "youtube.json"
        video = Path(params["path"])
        return video.with_suffix(video.suffix + ".youtube.json")

    def reconcile(self, operation):
        params = operation["params"]
        try:
            receipt = json.loads(self.receipt_path(params).read_text())
        except (OSError, ValueError):
            receipt = None
        if isinstance(receipt, dict) and receipt.get("status") == "published" and receipt.get("video_id"):
            return DONE, {"receipt": f"youtube:video:{receipt['video_id']}",
                          "result": {k: receipt.get(k) for k in ("video_id", "url", "api_privacy_status", "privacy") if receipt.get(k)}}
        if not isinstance(receipt, dict) or receipt.get("status") != "pending":
            return NOT_DONE, {}                      # the upload never began
        # The upload began and its receipt was never finished: the channel listing decides.
        started = float(receipt.get("started_at_ts") or 0)
        arguments = ["find", "--profile", str(params.get("profile") or "default"), "--since-ts", str(int(started)),
                     "--write-receipt", str(self.receipt_path(params))]
        if params.get("title"):
            arguments += ["--title", str(params["title"])]
        elif receipt.get("title"):
            arguments += ["--title", str(receipt["title"])]
        else:
            return UNKNOWN, {"reason": "pending receipt names no title to look for"}
        try:
            found = self._cli(arguments, params, LOOKUP_TIMEOUT)
        except (NotDispatched, Ambiguous) as error:
            return UNKNOWN, {"reason": f"channel lookup unavailable: {error.detail}"}
        if found.returncode:
            return UNKNOWN, {"reason": "channel lookup failed"}
        payload = self._payload(found) or {}
        if payload.get("video_id"):
            return DONE, {"receipt": f"youtube:video:{payload['video_id']}",
                          "result": {k: payload.get(k) for k in ("video_id", "url", "api_privacy_status") if payload.get(k)}}
        if self.clock() - started < INDEX_GRACE:
            return UNKNOWN, {"reason": "upload started recently; the channel listing may lag"}
        return NOT_DONE, {}
