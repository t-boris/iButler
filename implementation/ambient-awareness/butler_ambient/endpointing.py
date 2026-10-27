"""End-of-thought detection with Smart Turn v3 (feature butler-attention-architecture, REQ-006).

Smart Turn (pipecat-ai, BSD-2) lives in its own environment, ``turn-env``, like
the voice and image models, and runs as a resident worker: one JSON line in
(16 kHz mono PCM, base64), one JSON line out (probability that the thought is
complete, inference time). For now it runs in **shadow**: the current
endpointer still decides, and only Smart Turn's numbers are kept in the turn
audit next to what the old rule did. No audio and no words leave the worker
process or this module. When the environment is absent the shadow is off.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_SECONDS = 8.0
WORKER_TIMEOUT = 3.0


class SmartTurnShadow:
    def __init__(self, python=None, *, clock=time.monotonic):
        self.python = Path(python) if python else None
        self.clock = clock
        self.lock = threading.Lock()
        self.process = None
        self.state = "configured" if self.python and self.python.is_file() else "not-configured"
        self.error = None
        self.samples = self.failures = 0
        self.last = None

    # -- worker lifecycle ----------------------------------------------------------------------

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return self.process
        if not self.python or not self.python.is_file():
            self.state = "not-configured"
            return None
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
        # The worker is this file run by turn-env's interpreter. Putting the service's site-packages on PYTHONPATH
        # shadowed turn-env's stdlib (a stale typing.py there broke numpy's import) and killed the worker at start.
        try:
            self.process = subprocess.Popen([str(self.python), str(Path(__file__).resolve()), "worker"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0)
        except OSError as error:
            self.state, self.error = "unavailable", type(error).__name__
            return None
        self.state = "starting"
        return self.process

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None: stream.close()

    # -- judgement -----------------------------------------------------------------------------

    def judge(self, pcm, timeout=WORKER_TIMEOUT):
        """Probability (0..1) that the thought in the last eight seconds of PCM is complete, or None."""
        tail = bytes(pcm[-int(MAX_SECONDS * 16000) * 2:])
        tail = tail[: len(tail) - len(tail) % 2]
        if len(tail) < 16000:
            return None
        with self.lock:
            process = self._start()
            if process is None:
                return None
            request = json.dumps({"pcm": base64.b64encode(tail).decode("ascii")}) + "\n"
            started = self.clock()
            try:
                process.stdin.write(request.encode("ascii"))
                line = self._readline(process, timeout)
                verdict = json.loads(line)
            except Exception as error:
                self.failures += 1
                self.error = type(error).__name__
                if process.poll() is None:
                    process.terminate()
                self.process = None
                self.state = "unavailable" if self.failures >= 3 else "restarting"
                return None
            if "error" in verdict:
                self.failures += 1
                self.error = str(verdict["error"])[:60]
                self.state = "unavailable"
                return None
            result = {"complete_p": round(float(verdict.get("probability", 0.0)), 3), "ms": verdict.get("ms"),
                      "round_trip_ms": round((self.clock() - started) * 1000)}
            if "voiced_ms" in verdict: result["voiced_ms"] = verdict["voiced_ms"]
            self.state, self.samples, self.last = "ready", self.samples + 1, result
            return result

    @staticmethod
    def _readline(process, timeout):
        import select
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.05)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("worker closed")
                buffer.extend(chunk)
                if b"\n" in buffer:
                    return bytes(buffer.split(b"\n", 1)[0]).decode("utf-8")
            if process.poll() is not None:
                raise RuntimeError("worker exited")
        raise TimeoutError("worker deadline")

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "error": self.error, "samples": self.samples, "failures": self.failures,
                    "last": self.last, "mode": "shadow: numbers in the turn audit only"}


def worker():
    """Resident Smart Turn process: reads JSON lines with base64 PCM, writes JSON lines with the probability."""
    import numpy as np
    try:
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        analyzer = LocalSmartTurnAnalyzerV3()
        analyzer._log_data = False  # feedback v1 never retains acoustic examples
        vad = SileroVADAnalyzer(sample_rate=16000)
    except Exception as error:
        sys.stdout.write(json.dumps({"error": "load:" + type(error).__name__}) + "\n"); sys.stdout.flush()
        return 1
    for line in sys.stdin:
        try:
            pcm = base64.b64decode(json.loads(line)["pcm"])
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            started = time.monotonic()
            verdict = analyzer._predict_endpoint(audio)
            # Silero works on 512-sample (32 ms) windows at 16 kHz. Recreate
            # its state per utterance; no speaker/audio state crosses requests.
            vad._model.reset_states()
            voiced = sum(vad.voice_confidence(pcm[i:i + 1024]) >= .7
                         for i in range(0, len(pcm) - 1023, 1024))
            out = {"probability": float(verdict.get("probability", 0.0)), "prediction": int(verdict.get("prediction", 0)),
                   "voiced_ms": voiced * 32, "ms": round((time.monotonic() - started) * 1000)}
        except Exception as error:
            out = {"error": type(error).__name__}
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
    return 0


class SmartTurnEndpointer(SmartTurnShadow):
    """Active Pipecat/Silero/Smart Turn admission, opt-in after owner-speech calibration.

    The transport still supplies complete PCM seconds. Endpoint decisions run
    on the hearing worker, never the capture thread; ASR runs only after a
    completed thought. Failure degrades to the previous bounded endpointer.
    """
    def complete(self, pcm):
        verdict = self.judge(pcm)
        if verdict is None: return None
        return verdict["complete_p"] >= .5

    def snapshot(self):
        return {**super().snapshot(), "mode": "active: Pipecat Silero + Smart Turn v3"}


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "worker":
        # Silence the framework's banner so stdout stays a clean JSON channel.
        os.environ.setdefault("LOGURU_LEVEL", "ERROR")
        raise SystemExit(worker())
    raise SystemExit(2)
