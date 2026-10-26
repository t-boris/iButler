"""Bounded in-memory sensing and local enrolled-reference verification.

No media files, transcripts, identities or embeddings are saved during a pause.
Sensing dependencies and model files are loaded only when the service is run.
"""
from __future__ import annotations

import io
import math
from array import array
import json
from collections import deque
import queue
from . import processes as subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .policy import Evidence


SPEECH_FRAMES = 5           # 60 ms VAD frames per second that count as speech (0.3 s)


@dataclass
class Batch:
    start: float
    end: float
    frames: list[bytes]
    pcm: bytes
    frame_at: float | None = None
    gaps: tuple[str, ...] = ()
    frame_times: tuple[float, ...] = ()

    def wav(self):
        out = io.BytesIO()
        with wave.open(out, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(self.pcm)
        return out.getvalue()


class Inputs:
    def __init__(self, camera="0", microphone="0", *, audio_helper=None, voice_root=None, expected_microphone="Jabra SPEAK", camera_size="640x480",ffmpeg_cli="ffmpeg"):
        if not str(camera).isdigit() or not str(microphone).isdigit():
            raise ValueError("AVFoundation device indexes must be numeric")
        self.camera, self.microphone = str(camera), str(microphone)
        if camera_size not in {"640x480", "1280x720", "1920x1080"}: raise ValueError("unsupported camera size")
        self.camera_size = camera_size
        self.ffmpeg_cli = ffmpeg_cli
        self.frames = queue.Queue(maxsize=15)
        self.audio = queue.Queue(maxsize=2)
        self.processes = []
        self.stopped = threading.Event()
        self.dropped = {"camera": 0, "microphone": 0}
        self.errors = {"camera": "", "microphone": ""}
        self.audio_exits = deque(maxlen=10)      # (at, code, stderr tail) of native audio helper exits: metadata only
        self.audio_helper, self.voice_root = audio_helper, voice_root
        self.expected_microphone = expected_microphone
        self.audio_status = {"ready": False, "echo_cancellation": False}
        self.playback = {"state": "idle", "id": None}
        self.input_signal = None
        self.last_camera_at = None
        self.camera_started_at = None
        self.camera_restarts = 0
        self.camera_failure = None
        self.camera_status = {}
        helper = Path(audio_helper).with_name("butler-camera") if audio_helper else None
        self.camera_helper = helper if helper is not None and helper.is_file() else None
        self.camera_name = None
        self.audio_started_at = None
        self.audio_restarts = 0
        self.recovery_thread = None
        self.camera_lock = threading.Lock()
        self.control_lock = threading.Lock()

    def _camera_command(self):
        if self.camera_helper is not None and self.camera_name:
            return [str(self.camera_helper), self.camera_name, self.camera_size]
        return [self.ffmpeg_cli, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "avfoundation",
                "-framerate", "30", "-video_size", self.camera_size, "-i", f"{self.camera}:none",
                "-an", "-vf", "fps=5", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1"]

    def recover_camera(self):
        """Retry only our stalled/closed video pipe, with a startup backoff.

        An AVFoundation process can remain alive forever without its first
        frame after device teardown/reopen. Audio and model readiness cannot
        establish camera health. Permission still requires three fresh checks.
        """
        if self.stopped.is_set() or len(self.processes) < 2: return False
        now = time.time()
        if self.camera_started_at is None or now - self.camera_started_at < (5 if self.last_camera_at else 20): return False
        if self.last_camera_at is not None and now - self.last_camera_at <= 3: return False
        with self.camera_lock:
            old = self.processes[0]
            self.camera_failure = "video-stalled" if old.poll() is None else "video-exited"
            old.terminate()
            try: old.wait(timeout=2)
            except subprocess.TimeoutExpired: old.kill(); old.wait(timeout=2)
            # The old reader exits before closing/replacing its pipe. No old
            # frame may be paired with audio after recovery.
            old.stdout.close()
            old.stderr.close()
            while True:
                try: self.frames.get_nowait()
                except queue.Empty: break
            self.last_camera_at = None
            self.camera_started_at = now
            self.camera_restarts += 1
            self.errors["camera"] = ""
            self.camera_status = {}
            if self.stopped.is_set(): return False
            process = subprocess.Popen(self._camera_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, bufsize=0)
            self.processes[0] = process
            threading.Thread(target=self._errors, args=("camera", process), daemon=True).start()
            threading.Thread(target=self._video, args=(process,), daemon=True).start()
        return True

    def recover_audio(self, force=False):
        if (self.stopped.is_set() or len(self.processes) < 2 or not self.audio_helper or not self.voice_root
                or self.audio_status.get("echo_cancellation") is True or self.audio_started_at is None
                or (not force and time.time() - self.audio_started_at < 20)): return False
        self.cancel_playback()
        with self.control_lock:
            old = self.processes[1]
            old.terminate()
            try: old.wait(timeout=2)
            except subprocess.TimeoutExpired: old.kill(); old.wait(timeout=2)
            old.stdout.close()
            old.stderr.close()
            if old.stdin is not None: old.stdin.close()
            self.input_signal = None
            while True:
                try: self.audio.get_nowait()
                except queue.Empty: break
            self.audio_started_at = time.time()
            self.audio_restarts += 1
            self.audio_status = {"ready": False, "echo_cancellation": False, "failure": "recovering-native-audio"}
            if self.stopped.is_set(): return False
            process = subprocess.Popen([str(self.audio_helper), self.expected_microphone, str(self.voice_root)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, bufsize=0)
            self.processes[1] = process
            threading.Thread(target=self._errors, args=("microphone", process), daemon=True).start()
            threading.Thread(target=self._audio, args=(process,), daemon=True).start()
        return True

    def recover_sources(self):
        # Device teardown may take seconds. It must not block the one-second
        # participant check/microphone path when only the camera is stalled.
        if self.stopped.is_set() or self.recovery_thread is not None and self.recovery_thread.is_alive(): return
        def recover():
            try:
                self.recover_camera()
                self.recover_audio()
            except (OSError, RuntimeError, ValueError): pass
        self.recovery_thread = threading.Thread(target=recover, daemon=True)
        self.recovery_thread.start()

    @staticmethod
    def _put(q, value):
        dropped = False
        try:
            q.put_nowait(value)
        except queue.Full:
            dropped = True
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            q.put_nowait(value)
        return dropped

    def start(self):
        self.stopped.clear()
        self.drain()
        if self.camera_helper is not None:
            from .local_settings import devices
            selected = [d["name"] for d in devices(self.ffmpeg_cli)["camera"] if d["id"] == self.camera]
            if len(selected) != 1: raise RuntimeError("selected camera unavailable")
            self.camera_name = selected[0]
        base = [self.ffmpeg_cli, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "avfoundation"]
        # USB cameras commonly expose only 30 fps modes. Acquire a supported
        # native stream, then downsample locally to the recorded 5 fps stream.
        commands = [self._camera_command(),
                    base + ["-i", f"none:{self.microphone}", "-vn", "-ac", "1", "-ar", "16000",
                            "-f", "s16le", "pipe:1"]]
        if self.audio_helper and self.voice_root:
            commands[1] = [str(self.audio_helper), self.expected_microphone, str(self.voice_root)]
        try:
            self.camera_started_at = time.time()
            self.audio_started_at = time.time()
            for index, command in enumerate(commands):
                self.processes.append(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                       stdin=subprocess.PIPE if index == 1 and self.audio_helper else subprocess.DEVNULL, bufsize=0))
            for name, process in zip(("camera", "microphone"), self.processes):
                threading.Thread(target=self._errors, args=(name, process), daemon=True).start()
            threading.Thread(target=self._video, args=(self.processes[0],), daemon=True).start()
            threading.Thread(target=self._audio, args=(self.processes[1],), daemon=True).start()
        except Exception:
            self.stop()
            raise RuntimeError("local capture source unavailable") from None

    def _errors(self, source, process):
        # FFmpeg loglevel=error contains device/codec diagnostics, never media.
        for line in iter(process.stderr.readline, b""):
            index = 0 if source == "camera" else 1
            if len(self.processes) > index and self.processes[index] is not process: return
            if source == "camera" and line.startswith(b"BUTLER_CAMERA "):
                try:
                    value = json.loads(line[14:])
                    stage = value.get("stage")
                    if value.get("state") == "ready":
                        self.camera_status = {k: value[k] for k in ("portrait", "studio_light", "background_replacement", "center_stage") if type(value.get(k)) is bool}
                        if type(value.get("pixel_format")) is int: self.camera_status["pixel_format"] = value["pixel_format"]
                    if value.get("state") == "unavailable" and stage in {"arguments", "size", "permission", "selected-device", "format", "input", "open", "output", "start"}:
                        self.camera_failure = "video-" + stage
                except (ValueError, TypeError): pass
                continue
            if source == "microphone" and line.startswith(b"BUTLER_AUDIO "):
                try:
                    value = json.loads(line[13:])
                    if value.get("state") == "ready":
                        self.audio_status = {"ready": True, "echo_cancellation": value.get("echo_cancellation") is True,
                                             "other_audio_ducking": value.get("other_audio_ducking", "unknown"),
                                             "echo_backend": value.get("echo_backend", "unknown"),
                                             "formats": value.get("formats",{}),
                                             "playback_reference": value.get("playback_reference") is True}
                    if value.get("state") in {"health","ready"}:
                        for key in ("input_muted","input_volume","input_verified","actual_microphone","input_route","input_backend","voice_processing_enabled","other_audio_ducking",
                                    "system_default_microphone","microphone_is_system_default","system_default_output","system_output_muted","system_output_volume"):
                            if key in value:self.audio_status[key]=value[key]
                        if type(value.get("meeting")) is dict:
                            self.audio_status["meeting"] = value["meeting"]
                            self.audio_status["meeting_at"] = time.time()
                    elif value.get("state") == "unavailable":
                        self.audio_status = {"ready": False, "echo_cancellation": False,
                                             "failure": value.get("stage", "unavailable"), "code": value.get("code"),
                                             "formats": value.get("formats", {})}
                    if value.get("playback") in {"started", "done", "failed", "cancelled"} and value.get("id") == self.playback.get("id"):
                        previous = self.playback if self.playback.get("id") == value.get("id") else {}
                        self.playback = {**previous, "state": value["playback"], "id": value.get("id")}
                    if value.get("playback") == "progress" and self.playback.get("id") == value.get("id"):
                        self.playback = {**self.playback, "completed_chunks": value.get("completed_chunks", 0)}
                except (ValueError, TypeError):
                    pass
                continue
            self.errors[source] = (self.errors[source] + line.decode(errors="replace"))[-2000:]

    def _audio_command(self, value):
        with self.control_lock:
            if not self.audio_helper or len(self.processes) < 2 or self.processes[1].stdin is None:
                raise RuntimeError("echo-safe audio unavailable")
            self.processes[1].stdin.write(json.dumps(value).encode() + b"\n")
            self.processes[1].stdin.flush()

    def play(self, path, playback_id):
        if not self.audio_status["ready"] or not self.audio_status["echo_cancellation"]:
            raise RuntimeError("echo-safe audio unavailable")
        self.playback = {"state": "queued", "id": playback_id}
        self._audio_command({"command": "play", "path": str(path), "id": playback_id})

    def cancel_playback(self):
        try:
            self._audio_command({"command": "cancel"})
        except (RuntimeError, OSError, ValueError):
            pass
        self.playback = {"state": "idle", "id": None}

    def begin_stream(self, playback_id):
        if not self.audio_status.get("ready") or not self.audio_status.get("echo_cancellation"):
            raise RuntimeError("echo-safe audio unavailable")
        self.playback = {"state": "queued", "id": playback_id, "completed_chunks": 0}
        self._audio_command({"command": "begin", "id": playback_id})

    def append_stream(self, path, playback_id, sequence):
        self._audio_command({"command": "append", "path": str(path), "id": playback_id, "sequence": str(sequence)})

    def finish_stream(self, playback_id):
        self._audio_command({"command": "finish", "id": playback_id})

    def _video(self, proc):
        buffer = bytearray()
        while not self.stopped.is_set():
            block = proc.stdout.read(16384)
            if not block:
                return
            if self.stopped.is_set() or not self.processes or self.processes[0] is not proc: return
            buffer.extend(block)
            while True:
                a, b = buffer.find(b"\xff\xd8"), buffer.find(b"\xff\xd9")
                if a < 0 or b < a:
                    break
                if self.stopped.is_set() or not self.processes or self.processes[0] is not proc: return
                self.last_camera_at = time.time()
                self.camera_failure = None
                if self._put(self.frames, (time.time(), bytes(buffer[a:b + 2]))):
                    self.dropped["camera"] += 1
                del buffer[:b + 2]
            if len(buffer) > 2_000_000:
                buffer.clear()

    def _audio(self, proc):
        buffer = bytearray()
        while not self.stopped.is_set():
            block = proc.stdout.read(32000 - len(buffer))
            if not block:
                # Native full-duplex support is optional. A failure must not
                # disable ordinary observation. Speech stays unavailable; the
                # original microphone path still passes the normal verifier.
                if self.audio_helper and not self.stopped.is_set() and len(self.processes) > 1 and self.processes[1] is proc:
                    self._native_audio_exited(proc)
                return
            buffer.extend(block)
            if self.stopped.is_set() or len(self.processes) < 2 or self.processes[1] is not proc: return
            if len(buffer) == 32000:
                self._audio_frame(bytes(buffer))
                buffer.clear()

    def _audio_frame(self, pcm):
        # Operational meter only. No words, participant data or retained audio.
        samples = array("h", pcm)
        at = time.time()
        rms = math.sqrt(sum(float(x) * x for x in samples) / max(len(samples), 1)) / 32768
        self.input_signal = {"at": at, "rms_dbfs": round(20 * math.log10(max(rms, 1e-5)), 1)}
        if self._put(self.audio, (at, pcm)): self.dropped["microphone"] += 1

    def _native_audio_exited(self, proc):
        """The native helper stopped (2026-10-25: it died unnoticed; the voice went «unavailable», replies were cancelled
        and the meeting detector counted as busy). Record the exit, restart the helper up to three times in two
        minutes, and only then fall back to FFmpeg capture without echo cancellation."""
        try: code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired: code = None
        now = time.time()
        self.audio_exits.append({"at": now, "code": code, "stderr_tail": self.errors["microphone"][-300:]})
        self.audio_status = {"ready": False, "echo_cancellation": False, "failure": "native-audio-exited", "code": code}
        recent = sum(1 for e in self.audio_exits if now - e["at"] <= 120)
        if recent <= 3 and self.recover_audio(force=True):
            return
        self._fallback_audio(proc)

    def _fallback_audio(self, previous):
        with self.control_lock:
            if self.stopped.is_set() or len(self.processes) < 2 or self.processes[1] is not previous: return
            self.audio_status.update(ready=False, echo_cancellation=False)
            previous.wait(timeout=3)
            previous.stdout.close()
            if previous.stdin is not None: previous.stdin.close()
            command = [self.ffmpeg_cli, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "avfoundation",
                       "-i", f"none:{self.microphone}", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)
            self.processes[1] = process
        threading.Thread(target=self._errors, args=("microphone", process), daemon=True).start()
        # No recursive fallback after FFmpeg EOF.
        buffer = bytearray()
        while not self.stopped.is_set():
            block = process.stdout.read(32000 - len(buffer))
            if not block: return
            buffer.extend(block)
            if self.stopped.is_set() or len(self.processes) < 2 or self.processes[1] is not process: return
            if len(buffer) == 32000:
                self._audio_frame(bytes(buffer))
                buffer.clear()

    def batch(self):
        try:
            at, pcm = self.audio.get(timeout=2)
        except queue.Empty:
            return Batch(time.time() - 1, time.time(), [], b"")
        # Recover to the freshest complete second after a slow check. Mark
        # skipped audio as a gap; do not pair old audio with newer video.
        while True:
            try:
                at, pcm = self.audio.get_nowait()
                self.dropped["microphone"] += 1
            except queue.Empty:
                break
        frames = []
        frame_times = []
        frame_at = None
        while True:
            try:
                captured, frame = self.frames.get_nowait()
            except queue.Empty:
                break
            if at - 1.5 <= captured <= at + .5:
                frames.append(frame)
                frame_times.append(captured)
                frame_at = captured
        if not 0 <= time.time() - at < 1.5:
            return Batch(at - 1, at, [], b"")
        gaps = tuple(source for source, count in self.dropped.items() if count)
        for source in gaps:
            self.dropped[source] = 0
        if (self.audio_status.get("input_muted") is True or self.audio_status.get("input_volume") == 0
                or self.audio_status.get("input_verified") is False):
            # A muted device can emit complete silent PCM buffers. It cannot
            # provide participant evidence or a dialogue microphone lease.
            pcm = b""
        return Batch(at - 1, at, frames, pcm, frame_at, gaps,tuple(frame_times))

    def drain(self):
        for q in (self.frames, self.audio):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def stop(self):
        self.stopped.set()
        if self.recovery_thread is not None: self.recovery_thread.join(timeout=3)
        self.cancel_playback()
        for proc in self.processes:
            proc.terminate()
        for proc in self.processes:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
            if proc.stdin is not None:
                proc.stdin.close()
        self.processes.clear()
        self.audio_status = {"ready": False, "echo_cancellation": False}
        self.input_signal = None
        self.last_camera_at = None
        self.camera_started_at = None
        self.audio_started_at = None
        self.drain()


class Verifier:
    """YuNet/SFace faces and Resemblyzer/VAD voices, entirely on this machine.

    This is participant evidence, not general semantic analysis. Unrecognized
    bodies, faces, speech or ambiguous matches invalidate the complete batch.
    """
    # OpenCV's documented SFace cosine verification threshold (LFW example).
    # Enrollment agreement, person/voice checks and three fresh checks are
    # separate mandatory controls; a name inferred by a VLM is never evidence.
    FACE_THRESHOLD = 0.363
    def __init__(self, detector_path, recognizer_path, person_detector_path=None, *, voice_model=None):
        import cv2
        import numpy as np
        import webrtcvad
        from resemblyzer import VoiceEncoder
        self.cv, self.np = cv2, np
        self.detector = cv2.FaceDetectorYN.create(str(detector_path), "", (640, 480), .8)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        self.bodies = cv2.HOGDescriptor()
        self.bodies.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.people = None
        if person_detector_path:
            from .people import PeopleDetector
            self.people = PeopleDetector(person_detector_path)
        self.voice = VoiceEncoder(device="cpu", verbose=False,weights_fpath=voice_model)
        self.vad = webrtcvad.Vad(3)
        from .participants import Participants
        self.participants = Participants()

    def _faces(self, jpeg, *, check_bodies=True):
        cv, np = self.cv, self.np
        image = cv.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv.IMREAD_COLOR)
        if image is None:
            raise ValueError("invalid camera frame")
        h, w = image.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(image)
        faces = [] if faces is None else faces
        bodies = []
        if check_bodies:
            if self.people is not None:
                bodies = self.people.detect(image)
            else:
                bodies, _ = self.bodies.detectMultiScale(image, winStride=(8, 8))
        # A visible body without a detected face cannot be authorized.
        used_faces = set()
        for x, y, bw, bh in bodies:
            matched = next((index for index, f in enumerate(faces) if index not in used_faces
                            and x <= f[0] + f[2] / 2 <= x + bw and y <= f[1] + f[3] / 2 <= y + bh), None)
            if matched is None:
                raise ValueError("unverified participant")
            used_faces.add(matched)
        # Geometry (box, head-orientation and mouth proxies) stays in memory for this batch only; it never leaves.
        from .dialogue_state import face_geometry
        self.last_face_geometry = [face_geometry(face) for face in faces]
        return [self.recognizer.feature(self.recognizer.alignCrop(image, face)).flatten() for face in faces]

    def _voices(self, pcm):
        np = self.np
        voiced = []
        for offset in range(0, len(pcm) - 959, 960):
            frame = pcm[offset:offset + 960]
            if self.vad.is_speech(frame, 16000):
                voiced.append(frame)
        # A cough or a click fires one or two VAD frames; speech is at least 0.3 s of voiced audio.
        self.speech_detected = len(voiced) >= SPEECH_FRAMES
        if not voiced:
            return []
        # Do not authorize short/unreliable speech. Every half-second window
        # containing speech is checked, including off-camera speakers.
        if len(voiced) < 10:
            raise ValueError("insufficient voice evidence")
        # Enrollment can contain several seconds of silence before the owner
        # starts speaking. Match actual speech windows, not only the first 1.5s.
        samples = np.frombuffer(b"".join(voiced), dtype=np.int16).astype(np.float32) / 32768
        offsets = range(0, max(1, len(samples) - 4799), 8000)
        return [self.voice.embed_utterance(samples[a:a + 16000]) for a in offsets]

    def enroll(self, jpeg, pcm):
        # Explicit owner setup selects one clear face reference. A head turn in
        # another setup frame is not evidence that this selected face is unknown.
        # Normal ambient verification checks all bodies in its fresh sample.
        faces, voices = self._faces(jpeg, check_bodies=False), self._voices(pcm)
        if len(faces) != 1 or not voices:
            raise ValueError("enrollment requires exactly one visible face and clear speech")
        return {"face": faces[0].tolist(), "voice": self.np.mean(voices, axis=0).tolist()}

    def verify(self, batch, references):
        # DEC-032: voice identification must not be skipped by a camera error.
        # Both sources are independently evaluated in transient local memory.
        camera_ok = bool(batch.frames) and "camera" not in batch.gaps
        microphone_ok = len(batch.pcm) == 32000 and "microphone" not in batch.gaps
        camera_ids, voice_ids = frozenset(), frozenset()
        camera_certain = voice_certain = False
        self.speech_detected = False
        unknown = {"face":[],"voice":[]}
        self.last_face_geometry = []
        identities = {}
        tracker=getattr(self,"participants",None)
        tracking_generation=tracker.generation if tracker is not None else None
        def match(vector, key, threshold):
            scores = []
            for pid, reference in references.items():
                # Additional samples must come from explicit owner setup;
                # ordinary ambient sensing can never expand these templates.
                samples = reference.get(key + "_samples", [reference[key]])
                if not isinstance(samples, list) or not 1 <= len(samples) <= 30:
                    raise ValueError("invalid reference")
                similarity = []
                for sample in samples:
                    ref = self.np.asarray(sample)
                    score = float(self.np.dot(vector, ref) / (self.np.linalg.norm(vector) * self.np.linalg.norm(ref)))
                    if not self.np.isfinite(score):
                        raise ValueError("invalid reference")
                    similarity.append(score)
                score = max(similarity)
                scores.append((score, pid))
            scores.sort(reverse=True)
            if not scores or scores[0][0] < threshold or (len(scores) > 1 and scores[0][0] - scores[1][0] < .1):
                # Clearly different evidence can distinguish a newcomer. Near
                # threshold/ambiguous owner evidence cannot trigger a greeting.
                if scores and scores[0][0] < threshold-.1:unknown[key].append(vector)
                raise ValueError("uncertain " + key + " participant")
            return scores[0][1]

        failures = []
        self.last_voice_failure = self.last_camera_failure = None
        if microphone_ok and references:
            try:
                matched=set();errors=[]
                for vector in self._voices(batch.pcm):
                    try:matched.add(match(vector,"voice",.82))
                    except ValueError as error:errors.append(error)
                voice_ids=frozenset(matched)
                if errors:raise errors[0]
                voice_certain = True
            except Exception as error:
                self.last_voice_failure = {"uncertain voice participant": "voice-unrecognized-or-ambiguous",
                    "insufficient voice evidence": "voice-uncertain"}.get(str(error), "voice-evidence-unavailable")
                failures.append(self.last_voice_failure)
        else:
            self.last_voice_failure = "microphone-unavailable"
            failures.append("microphone-unavailable")
        if camera_ok and references:
            try:
                faces = self._faces(batch.frames[-1])
                if not faces: raise ValueError("no verified visible participant")
                matched=set();errors=[]
                for index, vector in enumerate(faces):
                    try:
                        pid = match(vector,"face",self.FACE_THRESHOLD)
                        matched.add(pid); identities[index] = pid
                    except ValueError as error:errors.append(error); identities[index] = None
                camera_ids=frozenset(matched)
                if errors:raise errors[0]
                camera_certain = True
            except Exception as error:
                self.last_camera_failure = {"unverified participant": "unverified-presence",
                    "no verified visible participant": "face-unavailable",
                    "uncertain face participant": "face-unrecognized-or-ambiguous",
                    "invalid camera frame": "camera-unavailable"}.get(str(error), "camera-evidence-unavailable")
                failures.insert(0, self.last_camera_failure)
        else:
            self.last_camera_failure = "camera-unavailable"
            failures.insert(0, "camera-unavailable")
        self.last_failure = failures[0] if failures else None
        if tracker is not None:
            tracker.observe("face",unknown["face"],batch.end,generation=tracking_generation)
            tracker.observe("voice",unknown["voice"],batch.end,generation=tracking_generation)
        certain = camera_certain and voice_certain and not batch.gaps
        verified = camera_ids | voice_ids if certain else frozenset()
        from .dialogue_state import LipMotion, Scene, scene_from
        if not hasattr(self, "lips"): self.lips = LipMotion()
        scene = scene_from(self.last_face_geometry, identities, self.lips) if camera_ok and references else Scene()
        return Evidence(batch.end, camera_ok, microphone_ok, verified, certain, voice_ids,
                        camera_ids, camera_certain, self.speech_detected, voice_certain, scene)
