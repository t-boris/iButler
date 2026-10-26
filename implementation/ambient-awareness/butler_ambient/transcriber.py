"""Resident whisper.cpp, numeric loopback only, in-memory WAV input, no context.

The installed server aborts decoding when its client disconnects. No converter,
debug output, transcript file, proxy, redirect or external endpoint is used.
"""
from __future__ import annotations

import http.client
import json
import math
import re
from array import array
import shutil
import socket
from . import processes as subprocess
import threading
import time
import uuid
from pathlib import Path


# Whisper invents these on silence and noise: closing lines of subtitles and videos.
PHANTOM = re.compile(r"^\W*(?:спасибо(?:\s+(?:за\s+(?:просмотр|внимание)|большое|всем))?|продолжение\s+следует|"
                     r"субтитры(?:\s+\S+){0,4}|редактор\s+субтитров(?:\s+\S+){0,3}|подписывайтесь(?:\s+на\s+канал)?|"
                     r"до\s+(?:новых\s+)?встреч|пока(?:\s+пока)?|конец|музыка|аплодисменты|"
                     r"thank\s+you(?:\s+for\s+watching)?|thanks(?:\s+for\s+watching)?|subtitles?(?:\s+by\s+\w+)?|"
                     r"the\s+end|bye)\W*$", re.I)
VAD_MODEL = "ggml-silero-v5.1.2.bin"   # next to the Whisper model; optional, enables whisper.cpp's VAD
SPEECH_RMS_DBFS = -45.0       # a 20 ms frame louder than this counts as speech-like
PHANTOM_SPEECH_FRACTION = 0.2 # a phantom phrase on audio that is mostly quiet is dropped
LOGPROB_FLOOR = -1.0          # mean token log-probability below this is a guess, not a transcript (OpenAI's own threshold)
NO_SPEECH_CEILING = 0.6       # the decoder's own "this was not speech" probability
ASR_PROMPT = "Батлер. Бата. Разговор по-русски."   # initial prompt: the assistant's names in the vocabulary


def speech_fraction(wav):
    """Share of 20 ms frames above the speech floor in a 16 kHz mono WAV; 0 for an unreadable buffer."""
    try:
        start = wav.index(b"data") + 8
    except ValueError:
        return 0.0
    samples = array("h")
    samples.frombytes(wav[start:start + (len(wav) - start) // 2 * 2])
    frame = 320
    if len(samples) < frame:
        return 0.0
    loud = total = 0
    for offset in range(0, len(samples) - frame + 1, frame):
        total += 1
        chunk = samples[offset:offset + frame]
        rms = math.sqrt(sum(x * x for x in chunk) / frame) / 32768
        if 20 * math.log10(max(rms, 1e-6)) > SPEECH_RMS_DBFS:
            loud += 1
    return loud / total if total else 0.0


def phantom(text, wav=None):
    """A recognizer phantom: a known filler phrase and, when audio is given, hardly any speech energy behind it."""
    if not PHANTOM.match(text.strip()):
        return False
    return wav is None or speech_fraction(wav) < PHANTOM_SPEECH_FRACTION


def confident(segments):
    """True when Whisper's own numbers say the segments are a transcript: weighted mean log-probability above the
    floor and no segment the decoder itself marks as non-speech. Returns (ok, mean_logprob, max_no_speech)."""
    scored = [(s.get("avg_logprob"), s.get("no_speech_prob"), len(str(s.get("text", "")))) for s in segments if isinstance(s, dict)]
    scored = [(lp, ns, n) for lp, ns, n in scored if isinstance(lp, (int, float)) and isinstance(ns, (int, float))]
    if not scored:
        return True, None, None
    weight = sum(max(n, 1) for _, _, n in scored)
    mean_logprob = sum(lp * max(n, 1) for lp, _, n in scored) / weight
    max_no_speech = max(ns for _, ns, _ in scored)
    return mean_logprob >= LOGPROB_FLOOR and max_no_speech <= NO_SPEECH_CEILING, round(mean_logprob, 3), round(max_no_speech, 3)


def normalize_asr_text(text):
    """Remove one recognizer-added outer frame, never words or inline quotes."""
    text=text.strip()
    pairs={'«':'»','“':'”','„':'“','"':'"',"'":"'",'‘':'’'}
    if len(text)>=2 and pairs.get(text[0])==text[-1] and not any(
            char in text[1:-1] for char in '\'"«»“”„‘’'):
        return text[1:-1].strip()
    return text


class ResidentWhisper:
    def __init__(self, model, cli="whisper-cli", language="ru"):
        executable = shutil.which(cli) or cli
        self.executable = Path(executable).with_name("whisper-server")
        self.model, self.language = model, language
        self.process = None
        self.port = None
        self.path = "/" + uuid.uuid4().hex
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.operations = {}
        self.ended = threading.Event()

    @property
    def available(self):
        return self.executable.is_file() and bool(self.model)

    def warm(self):
        with self.start_lock:
            if self.ended.is_set(): raise RuntimeError("ASR stopped")
            if self.process is not None and self.process.poll() is None: return
            # Only our own loopback process is used; this is not a configurable
            # HTTP endpoint and no previously running server is trusted.
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
            arguments = [str(self.executable), "-m", self.model,
                "--host", "127.0.0.1", "--port", str(self.port), "--request-path", self.path,
                "-l", self.language, "-mc", "0", "-nt", "-sns", "-nth", "0.6", "-lpt", "-1.0", "--prompt", ASR_PROMPT, "-nlp"]
            vad = Path(self.model).with_name(VAD_MODEL)
            if vad.is_file():
                # Silence and noise never reach the decoder: whisper.cpp's own VAD (Silero) cuts them first.
                arguments += ["--vad", "-vm", str(vad), "-vt", "0.5", "-vspd", "200", "-vp", "60"]
            self.vad_enabled = vad.is_file()
            self.process = subprocess.Popen(arguments,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not self.ended.is_set():
                if self.process.poll() is not None: break
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=.3)
                try:
                    conn.request("GET", self.path + "/health")
                    response = conn.getresponse()
                    if response.status == 200 and json.loads(response.read(4096)).get("status") == "ok": return
                except (OSError, ValueError, http.client.HTTPException): pass
                finally: conn.close()
                self.ended.wait(.05)
            self.process.terminate()
            self.process.wait(timeout=3)
            raise RuntimeError("resident local ASR unavailable")

    def client(self):
        return WhisperClient(self)

    def cancel(self, scope=None):
        with self.lock:
            for conn, owner in list(self.operations.items()):
                if scope is not None and owner is not scope: continue
                if conn.sock:
                    try: conn.sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                conn.close()

    def stop(self):
        self.ended.set()
        self.cancel()
        with self.start_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()

    def transcribe(self, data, token=None, *, scope=None):
        if token is not None and token.is_set(): raise RuntimeError("ASR cancelled")
        if not data.startswith(b"RIFF") or len(data) > 32_000 * 90 + 4096: raise ValueError("invalid WAV")
        self.warm()
        boundary = uuid.uuid4().hex
        # No temperature fallback: a decoder that fails returns nothing instead of an invented phrase.
        fields = {"response_format": "verbose_json", "language": self.language, "no_context": "true",
                  "temperature": "0.0", "temperature_inc": "0.0"}
        body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                        for key, value in fields.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="utterance.wav"\r\n'
                 'Content-Type: audio/wav\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with self.lock:
                if token is not None and token.is_set(): raise RuntimeError("ASR cancelled")
                conn.connect()
                conn.auto_open = False
                self.operations[conn] = scope
            conn.request("POST", self.path + "/inference", body,
                         {"Content-Type": "multipart/form-data; boundary=" + boundary})
            response = conn.getresponse()
            value = json.loads(response.read(128_000))
            if response.status != 200 or (token is not None and token.is_set()): raise ValueError("ASR cancelled or failed")
            text = value.get("text")
            if not isinstance(text, str): raise ValueError("invalid ASR result")
            text = normalize_asr_text(text[:32_000])
            ok, mean_logprob, no_speech = confident(value.get("segments") or [])
            self.last_quality = {"mean_logprob": mean_logprob, "no_speech": no_speech, "chars": len(text)}
            if not ok:
                # A low-confidence decode is a guess («как он санжели не че»): silence is more honest than a guess.
                self.low_confidence_dropped = getattr(self, "low_confidence_dropped", 0) + 1
                return ""
            if phantom(text, data):
                self.phantoms_dropped = getattr(self, "phantoms_dropped", 0) + 1
                return ""
            return text
        except Exception:
            raise RuntimeError("resident local ASR unavailable or cancelled") from None
        finally:
            conn.close()
            with self.lock: self.operations.pop(conn, None)


class WhisperClient:
    """One resident model, independently cancellable consumers."""
    def __init__(self, resident):
        self.resident = resident

    def transcribe(self, data, token=None):
        return self.resident.transcribe(data, token, scope=self)

    def cancel(self):
        self.resident.cancel(scope=self)
