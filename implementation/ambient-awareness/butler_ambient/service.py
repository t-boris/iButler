"""Single local authority for capture, policy, retention, analysis and delivery."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import fcntl
import io
import logging
import itertools
import json
import os
import queue
import socketserver
import sqlite3
import errno
from . import processes as subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .display import CardRefused
from .analysis import export_observations, analysis_inference_status
from .delivery import Delivery, DisplayTransport, Telegram, ZONE, advice_candidate, quiet
from .policy import Gate, DialogueGate
from .summaries import summarize, summary_text
from .camera_archive import read_samples, sample_count


@dataclass
class CaptureStage:
    epoch: int
    batch: object
    archived: bool
    hearing: object = None
    used: bool = False


class Service:
    def __init__(self, store, inputs, verifier, analyzer, config=None, clock=time.time, transport=None):
        self.store, self.inputs, self.verifier, self.analyzer = store, inputs, verifier, analyzer
        self.config, self.clock = config or {}, clock
        self.lock = threading.RLock()
        self.local_history_epoch = 0
        self.gate = Gate(store)
        self.dialogue_gate = DialogueGate()
        self.gate.enable(bool(store.get("enabled", False)), self.clock())
        self.sources = {key: "paused" for key in ("camera", "microphone", "screen")}
        self.requests = queue.Queue(maxsize=16)
        self.pending = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.capture_epoch = 0
        self.preview_frame = None
        self.check_pending = queue.Queue(maxsize=1)
        self.check_skipped = 0
        self.check_error = None
        self.notices = None     # core notices for the owner panel; created with the switched transport
        self.core_link = None   # released observations and owner signals to butler-core (W8)
        from .dialogue_state import Scene
        self.latest_scene = Scene()
        self.delivery = Delivery(store, transport or self.delivery_transport())
        self.analysis_error = None
        self.analysis_diagnostic = None
        self.camera_batch_size = 5
        self.threads = []
        self.input_running = False
        self.last_analysis = self.last_screen = float("-inf")
        self.screen_process = None
        self.screen_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.analysis_token = threading.Event()
        self.analysis_token.set()
        self.analysis_run_token = threading.Event()
        self.analysis_serial = threading.Lock()
        self.check_latency_seconds = None
        self.voice = None
        self.restart = None
        if self.config.get("voice_python"):
            from .conversation import Conversation
            self.voice = Conversation(self)
        self.visual = None
        if self.config.get("visual_enabled"):
            from .visual import Visual
            self.visual = Visual(self)
        from .schedules import Schedules
        self.schedules = Schedules(self)
        from .voice_commands import VoiceCommands
        self.commands = VoiceCommands(self)
        from .meeting import Meeting
        self.meeting = Meeting(self)
        from .attention import Attention
        self.attention = Attention(self, clock=self.clock)
        from .endpointing import SmartTurnShadow, SmartTurnEndpointer
        endpoint = SmartTurnEndpointer if self.config.get("turn_control") is True else SmartTurnShadow
        self.smart_turn = endpoint(self.config.get("turn_python"))
        from .brain import AgentBrain
        self.brain = AgentBrain(self.config, self.store, clock=self.clock)
        from .panel_chat import PanelChat
        self.panel_chat = PanelChat(self)         # the owner's written conversation in the panel; memory only
        from .calls import Calls
        self.calls = Calls(self, clock=self.clock)
        from .display import Display
        self.display = Display(self)              # local Display history until Clear; no biometric gate (DEC-009)
        self.display_transport = DisplayTransport(self)
        self.panel = None                         # the owner panel process, when this service launched it
        self.panel_launch = None                  # how to launch it again (set by the CLI)
        from .work_tasks import WorkTasks
        self.work = WorkTasks(self)
        from .archive import PassiveArchive
        self.archive = PassiveArchive(self)
        from .knowledge import Knowledge
        self.knowledge = Knowledge(self)
        from .disclosure import Disclosures
        self.disclosures = Disclosures(self)
        from .owner_setup import OwnerSetup
        self.owner_setup = OwnerSetup(self)
        self.listening = None
        if self.voice is not None:
            from .listening import Listening
            self.listening = Listening(self)
        from .dictation import Dictation
        self.dictation = Dictation(self)          # panel dictation: speech to text for the message field; memory only
        from .feedback import DialogueFeedback
        self.feedback = DialogueFeedback(self)
        from .history_summary import LocalHistorySummaries
        self.history_summaries = LocalHistorySummaries(self)

    def _set_sources(self, states, at):
        for source, state in states.items():
            previous = self.sources[source]
            if state == "unavailable" and previous != "unavailable":
                self.store.event(source, "source-unavailable", at)
            elif previous == "unavailable" and state != "unavailable":
                self.store.execute("UPDATE events SET end=? WHERE source=? AND reason='source-unavailable' AND end IS NULL", (at, source))
            self.sources[source] = state

    @property
    def voice_gate(self):
        return self.dialogue_gate if self.store.get("face_wake_dialogue", False) else self.gate

    def yield_analysis(self):
        """A dialogue turn takes the local models (whisper, Ollama): stop in-flight background
        analysis now, whatever model it runs on; the analysis loop resumes once the turn is over."""
        self.analysis_run_token.set()
        analyzer = getattr(self, "analyzer", None)
        if analyzer is not None: analyzer.cancel()

    def _end_local_history_scope(self):
        """Invalidate queued local replies and stop only a currently playing local reply."""
        with self.lock:
            self.local_history_epoch += 1
            voice = getattr(self, 'voice', None)
            token = getattr(voice, 'local_history_token', None) if voice is not None else None
            if isinstance(token, threading.Event) and getattr(voice, 'token', None) is token:
                token.set()
                if not getattr(getattr(self, 'disclosures', None), 'active_playback', None):
                    voice.inputs.cancel_playback()

    def _pause_feedback(self, *, preserve_display=False):
        if not hasattr(self, 'feedback'): return
        from .recordings import history_visible
        keep = (preserve_display and self.store.get('single_user_local', False) is True
                and history_visible(self))
        if keep: self.feedback.pause(preserve_local=True)
        else: self.feedback.pause()

    def _invalidate(self, reason="permission-paused", *, preserve_dialogue=False):
        self.analysis_token.set()
        clear_hearing = reason in {"settings-change","service-stopped","owner-paused","consent-revoked", "owner-voice-enrollment", "owner-reference-updated"}
        if clear_hearing and getattr(self, 'history_summaries', None) is not None:
            self.history_summaries.cancel()
        if clear_hearing or reason in {'source-unavailable', 'participant-check-gap', 'participant-check-unavailable', 'stale-participant-check'}:
            self._end_local_history_scope()
        if clear_hearing:self.preview_frame=None
        if clear_hearing or reason in {"source-unavailable","participant-check-gap","participant-check-unavailable"}:
            self.capture_epoch += 1
            self._discard_checks()
        if clear_hearing and getattr(self,"listening",None) is not None:
            self.listening.clear()
        if reason == "consent-revoked" and getattr(self, "panel_chat", None) is not None: self.panel_chat.clear(reason)
        if reason in {"settings-change","service-stopped"} and hasattr(self,"owner_setup"): self.owner_setup.cancel()
        if reason in {"settings-change","service-stopped"} and hasattr(self,"archive"): self.archive.invalidate()
        if reason in {"settings-change","service-stopped","owner-paused","consent-revoked"} and hasattr(self,"disclosures"):
            self.disclosures.cancel()
            tracker=self.disclosures.tracker()
            if tracker is not None:tracker.clear()
        # DEC-066: the owner session closes only for reasons that also end hearing (pause, consent, settings,
        # enrollment, stop). Check gaps and stale sensors are not company; a blink no longer cancels a command.
        close_session = clear_hearing or (not self.dialogue_gate.allowed and not preserve_dialogue)
        if close_session and hasattr(self, "commands"): self.commands.clear()
        if close_session:
            self._pause_feedback(preserve_display=not clear_hearing)
            self.dialogue_gate.invalidate(reason)
            if clear_hearing and getattr(self, "display", None) is not None:
                # REQ-011: the display history ends with the conversation, i.e. a real close. A lease lost for a moment
                # (a check that blinks) keeps the cards: the owner asked that the display is not cleared for nothing (DEC-047).
                self.display.clear(reason)
        preserve_dialogue = preserve_dialogue or not close_session
        if self.voice is not None and (not preserve_dialogue or not self.dialogue_gate.allowed):
            # DEC-062: only reasons that also end passive hearing stop general
            # dialogue; identity/check reasons end the private dialogue only.
            if not clear_hearing and self.voice.turn_committed:
                self.voice._mark("lease_loss_deferred")     # REQ-039: a check gap does not drop a committed turn
            else:
                self.voice.cancel(reason=reason, private_only=not clear_hearing)
        elif self.voice is not None:
            self.voice.vision_model.cancel()
        if self.visual is not None:
            self.visual.cancel()
        self.gate.invalidate(self.clock(), reason)
        passive_continues = hasattr(self,"archive") and self.archive.enabled and reason not in {"settings-change","service-stopped","owner-paused"}
        if hasattr(self.analyzer, "cancel") and not passive_continues:
            self.analyzer.cancel()
        if hasattr(self,"knowledge"):self.knowledge.cancel()
        if not passive_continues and self.screen_process is not None and self.screen_process.poll() is None:
            self.screen_process.terminate()
        if not passive_continues:self._discard_pending()
        self._set_sources({key: "paused" for key in self.sources}, self.clock())

    def _discard_pending(self):
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break

    def references(self):
        return {r["id"]: json.loads(r["reference"]) for r in self.store.execute("SELECT * FROM agreements WHERE active=1")}

    def trusted_ids(self):
        """The owner and household members (DEC-066): enrolled identities of kind household with an active agreement."""
        rows = self.store.execute("SELECT i.id FROM identities i JOIN agreements a ON a.id=i.id WHERE i.kind='household' AND a.active=1")
        return frozenset({"owner"} | {r["id"] for r in rows})

    def _stage_batch(self, batch):
        """Capture authorized passive media/hearing without waiting for identity."""
        with self.lock:
            self.meeting.update()
            if self.stop_event.is_set() or not self.gate.enabled:
                return None
            frame_at=batch.frame_at if batch.frame_at is not None else batch.end
            if (self.archive.enabled and batch.frames and 'camera' not in batch.gaps
                    and 0<=self.clock()-frame_at<=2.5):
                # One volatile owner-local preview, independent of disk quota.
                # Strict paused participant sensing never enters this cache.
                self.preview_frame=(batch.frames[-1],frame_at)
            if self.owner_setup.capturing:
                if self.listening is not None and self.listening.phase != 'paused': self.listening.clear()
                self.owner_setup.feed(batch)
                self._set_sources({key:'paused' for key in self.sources},batch.start)
                return None
            hearing = self.listening.feed(batch) if self.listening is not None and self.listening.enabled else None
            archived = self.archive.capture(batch)
            return CaptureStage(self.capture_epoch,batch,archived,hearing)

    @staticmethod
    def _reject_check(prepared):
        if prepared.hearing is not None: prepared.hearing.resolve()

    def _discard_checks(self):
        while True:
            try: self._reject_check(self.check_pending.get_nowait())
            except queue.Empty: break

    def _pause_checks_without_storage(self):
        # A failed event-log write must neither kill the check worker nor leave
        # its former permission live. This path performs no database writes.
        self.capture_epoch += 1
        self._discard_checks()
        self.gate.allowed = False
        self.gate.streak = 0
        self.gate.generation += 1
        self.gate.reason = 'participant-check-unavailable'
        self.dialogue_gate.invalidate()
        self.commands.clear()
        if self.voice is not None: self.voice.cancel(reason='participant-check-unavailable',private_only=True)

    def _queue_check(self, prepared):
        with self.lock:
            if self.stop_event.is_set() or not self.gate.enabled or prepared.epoch != self.capture_epoch:
                self._reject_check(prepared)
                return
            if self.check_pending.full():
                # Missing a qualifying second breaks the permission sequence.
                # Preserve captured media/transcript, reject the old inference
                # and start checks from the newest captured second.
                self.check_skipped += 1
                try: self._invalidate('participant-check-gap')
                except (OSError,sqlite3.Error):
                    self._pause_checks_without_storage()
                    self.archive.storage_error = 'write-unavailable'
                prepared.epoch = self.capture_epoch
            self.check_pending.put_nowait(prepared)

    def _check_loop(self):
        while not self.stop_event.is_set():
            try: prepared = self.check_pending.get(timeout=.2)
            except queue.Empty: continue
            try: self.accept_batch(prepared.batch,prepared=prepared)
            except Exception as error:
                with self.lock:
                    self._reject_check(prepared)
                    self.check_error = type(error).__name__
                    try: self._invalidate('participant-check-unavailable')
                    except (OSError,sqlite3.Error):
                        self._pause_checks_without_storage()
                        self.archive.storage_error = 'write-unavailable'

    def accept_batch(self, batch, *, prepared=None):
        """Check exact staged input; release hearing only after current policy."""
        prepared = self._stage_batch(batch) if prepared is None else prepared
        if prepared is None: return False
        with self.lock:
            if (prepared.batch is not batch or prepared.used or self.stop_event.is_set()
                    or not self.gate.enabled or prepared.epoch != self.capture_epoch or self.owner_setup.capturing):
                self._reject_check(prepared)
                return False
            prepared.used = True
            references = self.references()
            trusted = self.trusted_ids()
        check_started = time.monotonic()
        try:
            if getattr(self.verifier,'unavailable',False) is True:
                from .policy import Evidence
                evidence = Evidence(batch.end,bool(batch.frames),bool(batch.pcm))
            else: evidence = self.verifier.verify(batch,references)
            with self.lock:
                self.check_latency_seconds = round(time.monotonic()-check_started,3)
                self.latest_scene = evidence.scene       # the dialogue re-checks company right before it speaks
                self.attention.observe(evidence, self.clock())
                if (self.stop_event.is_set() or not self.gate.enabled
                        or prepared.epoch != self.capture_epoch or self.owner_setup.capturing):
                    self._reject_check(prepared)
                    return False
                result = self._apply_evidence(batch,evidence,references,prepared.archived,trusted=trusted)
                if prepared.hearing is None and self.listening is not None and self.listening.enabled:
                    prepared.hearing = self.listening.feed(batch, evidence)
                elif prepared.hearing is None and self.listening is not None:
                    owner_prefix = (self.store.get('face_wake_dialogue', False) and evidence.microphone_ok
                        and 0 <= self.clock()-batch.end <= Gate.MAX_AGE
                        and not DialogueGate.company(evidence, trusted)
                        and ((evidence.voice_certain and evidence.voice_ids == frozenset({'owner'}))
                             or (evidence.camera_certain and evidence.camera_ids == frozenset({'owner'}))))
                    if owner_prefix: prepared.hearing = self.listening.feed(batch, evidence, prefix=True)
                    else: self.listening.clear()
                self.check_error = None
                if prepared.hearing is not None: prepared.hearing.resolve(evidence,now=self.clock(),trusted=trusted)
                return result
        except Exception:
            self._reject_check(prepared)
            raise

    def _apply_evidence(self, batch, evidence, references, archived, trusted=None):
        # Caller holds the control lock. This entire policy update precedes the
        # exact microphone receipt, so ASR cannot reuse the previous lease.
        for source in batch.gaps:
            self.store.event(source, "capture-gap", batch.start, batch.end)
        dialogue_before = self.dialogue_gate.allowed
        route_before = self.dialogue_gate.confirmed_by
        trusted = self.trusted_ids() if trusted is None else trusted
        self.latest_evidence = evidence
        fresh_audience = type(evidence.captured_at) in (int, float) and 0 <= self.clock() - evidence.captured_at <= Gate.MAX_AGE
        if (not fresh_audience or not evidence.camera_ok or not evidence.microphone_ok
                or DialogueGate.company(evidence, trusted)):
            self._end_local_history_scope()
        if fresh_audience and DialogueGate.company(evidence, trusted) and self.voice is not None and self.voice.private_busy():
            self._pause_feedback(preserve_display=True)
            self.voice.cancel(reason="audience-changed", private_only=True)
        dialogue = self.dialogue_gate.check(evidence, self.clock(),
            self.gate.enabled and "owner" in references and self.store.get("face_wake_dialogue", False), trusted=trusted)
        if dialogue != dialogue_before and self.voice is not None:
            from .policy import CLOSE_TEXT
            reason = self.dialogue_gate.closed_reason
            self.voice._debug("session", ("Личная сессия открыта: вы подтверждены по " + ("голосу" if self.dialogue_gate.confirmed_by == "voice" else "лицу") + ".")
                              if dialogue else "Личная сессия закрыта: " + CLOSE_TEXT.get(reason, str(reason)) + ".")
        if dialogue_before and not dialogue and self.voice is not None:
            if self.dialogue_gate.closed_reason == "company":
                self._pause_feedback(preserve_display=True)
                self.voice.cancel(reason="audience-changed", private_only=True)
            elif self.voice.turn_committed:
                # The utterance was admitted under a valid lease; a cough or a turned head must not drop
                # the answer (REQ-039). The lease is re-checked as soon as the turn ends.
                self.voice._mark("lease_loss_deferred")
            else:
                self.voice.cancel(reason=getattr(self.verifier, "last_voice_failure" if route_before == "voice" else "last_failure", None) or "participant-check-failed",
                    private_only=True)
        before = self.gate.allowed
        allowed = self.gate.check(evidence, self.clock())
        if not allowed:
            if before:
                self._invalidate(preserve_dialogue=self.dialogue_gate.streak > 0)
            self._set_sources({"camera": "paused" if evidence.camera_ok else "unavailable",
                               "microphone": "paused" if evidence.microphone_ok else "unavailable", "screen": "paused"}, batch.start)
            return archived
        if not before:
            self.analysis_token = threading.Event()
        self._set_sources({"camera": "active", "microphone": "active"}, batch.start)
        # Each verified one-second segment represents continuous stream data.
        # MJPEG is a native video stream, not just a sampled photo collection.
        if not self.archive.enabled:
            frames=self.recording_frames(batch.frames)
            from .store import AnalysisBufferFull
            for source,data,suffix in (("camera",b"".join(frames),"mjpeg"),("microphone",batch.wav(),"wav")):
                try:
                    mid=self.store.media(source,batch.start,batch.end,data,suffix,temporary=True)
                    from .recordings import tag_capture
                    tag_capture(self,mid)
                except AnalysisBufferFull:
                    self.store.event(source,'analysis-buffer-full',batch.start,batch.end)
                    self._set_sources({source:'unavailable'},batch.start)
        now = self.clock()
        if not self.archive.enabled and now - self.last_screen >= 60:
            self.last_screen = now
            self.screen_worker.submit(self.capture_screen, now, self.gate.generation,temporary=True)
        if not self.archive.enabled and now - self.last_analysis >= 60 and not self.store.get("analysis_paused",False):
            self.last_analysis = now
            try:
                self.pending.put_nowait(self.gate.generation)
            except queue.Full:
                self.store.event("analysis", "analysis-busy", now, now + 60)
        self._fulfill_requests(batch)
        return True


    def recording_frames(self, frames):
        # Keep the same configured compact archive format on both recording
        # paths; identity checks and the UI still get the original fresh frame.
        if not self.config.get('recording_width'):return frames
        import cv2
        import numpy as np
        width=int(self.config['recording_width']);converted=[]
        for frame in frames:
            image=cv2.imdecode(np.frombuffer(frame,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None:raise ValueError('invalid recording camera frame')
            if image.shape[1]>width:
                size=(width,round(image.shape[0]*width/image.shape[1]))
                ok,jpeg=cv2.imencode('.jpg',cv2.resize(image,size),[cv2.IMWRITE_JPEG_QUALITY,70])
                if not ok:raise ValueError('camera encoding unavailable')
                frame=jpeg.tobytes()
            converted.append(frame)
        return converted

    def capture_screen(self, now, generation, *, archive=False, temporary=False):
        policy = self.archive if archive else self.gate
        with self.lock:
            if not policy.permits(self.clock()) or generation != policy.generation:
                return None
            helper = self.config.get("screen_helper", str(Path(__file__).parents[1] / "build/butler-screen"))
            try:
                process = subprocess.Popen([helper], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.screen_process = process
            except OSError:
                if archive:self.archive.sources["screen"]="unavailable"
                else:self._set_sources({"screen": "unavailable"}, now)
                return None
        try:
            data, _ = process.communicate(timeout=2)
            with self.lock:
                if not policy.permits(self.clock()) or generation != policy.generation:
                    return None
                if process.returncode or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("screen unavailable")
                mid = self.store.media("screen", now, now, data, "png",temporary=archive or temporary)
                if archive:
                    self.archive.sources["screen"]="active";self.archive.last["screen"]=now
                else:self._set_sources({"screen": "active"}, now)
                return mid
        except Exception:
            with self.lock:
                if archive:self.archive.sources["screen"]="unavailable"
                else:self._set_sources({"screen": "unavailable"}, now)
            return None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            with self.lock:
                if self.screen_process is process:
                    self.screen_process = None

    def request_screen_permission(self):
        # Local owner setup only, never exposed through MCP. This helper mode
        # requests macOS access without reading any screen or observation data.
        helper = self.config.get("screen_helper", str(Path(__file__).parents[1] / "build/butler-screen"))
        result = subprocess.run([helper, "--request-permission"], capture_output=True, timeout=30, check=True)
        value = json.loads(result.stdout)
        if type(value.get("screen_permission")) is not bool:
            raise ValueError("invalid permission response")
        return {"screen_permission": value["screen_permission"]}

    def _fulfill_requests(self, batch):
        for _ in range(self.requests.qsize()):
            try:
                source, requested_at, future = self.requests.get_nowait()
            except queue.Empty:
                return
            if future.cancelled():
                continue
            frame_at = batch.frame_at if batch.frame_at is not None else batch.end
            if batch.end < requested_at or (source == "camera" and frame_at < requested_at):
                self.requests.put_nowait((source, requested_at, future))
                continue
            if source == "camera":
                mid = self.store.media("camera", frame_at, frame_at, batch.frames[-1], "jpg")
            else:
                def finish(future=future, generation=self.gate.generation):
                    mid = self.capture_screen(self.clock(), generation)
                    if not future.cancelled():
                        future.set_result({"status": "captured" if mid else "unavailable", "source": "screen",
                                           "source_id": mid, "generation": generation})
                self.screen_worker.submit(finish)
                continue
            future.set_result({"status": "captured" if mid else "unavailable", "source": source,
                               "source_id": mid, "generation": self.gate.generation})

    def request_fresh(self, source):
        if source not in {"screen", "camera"}:
            raise ValueError("invalid on-demand source")
        with self.lock:
            if not self.gate.permits(self.clock()):
                return {"status": "paused", "source": source, "reason": self.gate.reason}
            future = concurrent.futures.Future()
            try:
                self.requests.put_nowait((source, self.clock(), future))
            except queue.Full:
                return {"status": "unavailable", "source": source, "reason": "capture-busy"}
        try:
            captured = future.result(timeout=8)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {"status": "unavailable", "source": source, "reason": "fresh-capture-unavailable"}
        if captured["status"] != "captured":
            return captured
        return captured

    def observe(self, source):
        captured = self.request_fresh(source)
        if captured["status"] != "captured":
            return captured
        # Local analysis may be slow. Participant sensing continues independently.
        if not self.analyze_one(captured["source_id"], captured["generation"]):
            return {"status": "unavailable", "source": source, "reason": "local-analysis-unavailable"}
        with self.lock:
            if not self.gate.permits(self.clock()) or captured["generation"] != self.gate.generation:
                return {"status": "paused", "source": source, "reason": self.gate.reason}
            row = self.store.execute("SELECT start,end FROM media WHERE id=?", (captured["source_id"],))[0]
            rows = [r for r in self.store.history(row["start"] - .001, row["end"] + .001) if r["media_id"] == captured["source_id"]]
            return {"status": "ok", "source": source, "observations": export_observations(rows)}

    def analyze_one(self, mid, generation):
        with self.analysis_serial:
            return self.analyze_group([mid], generation)

    def analyze_group(self, mids, generation, run_token=None, *, archive=False):
        policy = self.archive if archive else self.gate
        with self.lock, self.store.lock:
            if not policy.permits(self.clock()) or generation != policy.generation:
                return False
            self.store.expire(self.clock())
            rows = [r for mid in mids for r in self.store.execute("SELECT * FROM media WHERE id=? AND complete=1 AND path IS NOT NULL", (mid,))]
            if not rows:
                return False
            row = rows[0]
            path = Path(row["path"])
            data = path.read_bytes() if row['source']!='camera' else b''
            images=[];frame_times=[]
            context = self.store.execute("SELECT * FROM identities")
            for item in context:
                item["basis"] = "owner-supplied-context"
            previous = self.store.execute("SELECT id,source,start,end,facts FROM observations ORDER BY end DESC LIMIT 40")
            corrections = {r["subject"]: r["name"] for r in self.store.execute("SELECT * FROM corrections")}
            for item in previous:
                prior_facts = json.loads(item["facts"])
                for fact in prior_facts:
                    if fact.get("subject") in corrections:
                        fact["identity"] = corrections[fact["subject"]]
                        fact["identity_basis"] = "owner-confirmed"
                context.append({"observation_id": item["id"], "source": item["source"], "start": item["start"],
                                "end": item["end"], "basis": "prior-model-inferences", "facts": prior_facts})
            token = self.archive.token if archive else self.analysis_token
            if run_token is not None:
                from .conversation import Tokens
                token = Tokens(token,run_token)
            if token.is_set(): return False
            source = row["source"]
            analyzer_epoch=getattr(self.analyzer,'analysis_epoch',None)
            if source in {"microphone","system-audio"}:
                pcm = bytearray()
                for item in rows:
                    with wave.open(item["path"], "rb") as audio:
                        pcm.extend(audio.readframes(audio.getnframes()))
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16000)
                    audio.writeframes(bytes(pcm))
                data = buffer.getvalue()
            elif source == "camera":
                # Include EVERY selected frame of each covered closed clip.
                # Never truncate a clip then mark its unseen samples analyzed.
                selected=[]
                try:
                    for item in rows:
                        if len(images)+sample_count(item)>self.camera_batch_size:break
                        frames,times=read_samples(item)
                        images.extend(frames);frame_times.extend(times);selected.append(item)
                except (OSError,ValueError,KeyError,TypeError):
                    self.analysis_error='camera-samples-unavailable'
                    self.analysis_diagnostic={'source':'camera','phase':'read-samples','error_type':None}
                    return False
                rows=selected
                if not rows:return False
        try:
            phase="transcription" if row["source"] in {"microphone","system-audio"} else "inference"
            if row["source"] in {"microphone","system-audio"}:
                transcript = self.analyzer.transcribe(data, token=token)
                # Persist successful call ASR before inference: a failed fact
                # model must not discard an otherwise valid transcript.
                with self.lock:
                    if not token.is_set() and policy.permits(self.clock()) and generation == policy.generation:
                        calls = {r.get("meeting_id") for r in rows if r.get("meeting_id")}
                        if calls:
                            self.store.retain_transcript("analysis:" + rows[0]["id"], source,
                                min(r["start"] for r in rows), max(r["end"] for r in rows), transcript,
                                media_id=rows[0]["id"], meeting_id=next(iter(calls)), now=self.clock())
                phase="inference"
                facts = self.analyzer.analyze(row["source"], transcript=transcript, context=context, token=token)
            else:
                visual_args={'frame_times':frame_times} if source=='camera' else {}
                facts = self.analyzer.analyze(row["source"], images=images if source=='camera' else [data], context=context, token=token,**visual_args)
        except Exception as error:
            with self.lock:
                if not token.is_set() and policy.permits(self.clock()) and generation == policy.generation:
                    current_epoch=getattr(self.analyzer,'analysis_epoch',None)
                    if type(analyzer_epoch) is int and type(current_epoch) is int and analyzer_epoch!=current_epoch:
                        self.analysis_error=None
                        self.analysis_diagnostic={'source':source,'phase':'deferred','error_type':None}
                        return False
                    self.analysis_diagnostic={"source":row["source"],"phase":phase,"error_type":type(error).__name__}
                    diagnostic=getattr(self.analyzer,"diagnostic",None)
                    if type(diagnostic) is dict:self.analysis_diagnostic["model"]=dict(diagnostic)
                    self.store.event(row["source"], "analysis-failed", rows[0]["start"], rows[-1]["end"])
                    self.analysis_error = "local-analysis-unavailable"
                    for item in rows:
                        self.store.execute("UPDATE media SET analyzed=? WHERE id=?", (0 if archive else -1,item["id"]))
            return False
        with self.lock:
            if token.is_set() or not policy.permits(self.clock()) or generation != policy.generation:
                return False
            if source=='camera':
                metadata=analysis_inference_status(getattr(self.analyzer,'analysis_inference',None)).get('perception',{})
                count,tokens,context_size=(metadata.get(k) for k in ('images','prompt_tokens','context_tokens'))
                if (type(count) is int and count==len(images) and count>0
                        and type(tokens) is int and tokens>0 and type(context_size) is int):
                    # Treat the ENTIRE prompt cost per image as image cost,
                    # additionally reserving text/output room. Learn from the
                    # actual backend rather than hard-coding model names.
                    per_image=max(256,(tokens+count-1)//count)
                    room=max(0,min(8192,context_size)-1536-2000)
                    self.camera_batch_size=max(3,min(16,room//per_image))
                    if tokens>min(8192,context_size)-1536:
                        self.analysis_error='camera-context-budget'
                        self.analysis_diagnostic={'source':'camera','phase':'context-budget','error_type':None}
                        return False
            # A screenshot is an instant; never invent a minute of screen coverage.
            observation = self.store.save_observation(rows[0]["id"], facts, [item["id"] for item in rows])
            if self.core_link is not None:
                self.core_link.observation_released(observation, source, rows[0]["start"], rows[-1]["end"], facts)
            if source == 'camera' and images:
                self._keep_event_frame(observation, facts, rows, images[-1], frame_times[-1] if frame_times else rows[-1]['end'])
            self.analysis_error = None
            self.analysis_diagnostic={"source":row["source"],"phase":"completed","error_type":None}
        return True

    def _keep_event_frame(self, observation, facts, rows, frame, captured_at):
        """DEC-065 key frame after the observation is durably committed.

        Caller holds the control lock under the same Pause/generation check
        as the observation. A failure never loses the committed observation.
        """
        from .event_frames import WINDOW_SECONDS, important_events, local_day
        start = rows[0]['start']
        try:
            earlier = [json.loads(r['facts']) for r in self.store.execute(
                """SELECT facts FROM observations WHERE source='camera' AND id!=? AND start<?
                   ORDER BY end DESC LIMIT 1""", (observation, start))]
            recent = [json.loads(r['facts']) for r in self.store.execute(
                """SELECT facts FROM observations WHERE source='camera' AND id!=? AND end>? AND start<?
                   ORDER BY end DESC LIMIT 40""", (observation, start - WINDOW_SECONDS, start))]
            corrections = {r["subject"]: r["name"] for r in self.store.execute("SELECT * FROM corrections")}
            events = important_events(facts, recent, earlier[0] if earlier else None, corrections)
            if events:
                self.store.keep_event_frame(observation, frame, captured_at, events, local_day(captured_at))
            self.store.event_frame_error = None
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.store.event_frame_error = 'event-frame-unavailable'

    def _capture_loop(self):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    enabled = self.gate.enabled
                if not enabled:
                    if self.input_running:
                        self.inputs.stop()
                        self.input_running = False
                    self.stop_event.wait(.2)
                    continue
                if getattr(self.verifier, "unavailable", False) and not self.archive.enabled:
                    with self.lock:
                        self._set_sources({key: "unavailable" for key in self.sources}, self.clock())
                    self.stop_event.wait(1)
                    continue
                try:
                    if not self.input_running:
                        self.inputs.start()
                        self.input_running = True
                    self.inputs.recover_sources()
                    batch = self.inputs.batch()
                    prepared = self._stage_batch(batch)
                    if prepared is not None: self._queue_check(prepared)
                    # batch() already blocks for one captured PCM second.
                    # Additional wall-clock pacing slowly accumulates lag,
                    # eventually overflowing the queue and losing speech.
                except Exception as error:
                    if isinstance(error,sqlite3.Error) or (isinstance(error,OSError) and error.errno in {errno.ENOSPC,errno.EDQUOT}):
                        # Durable-storage failure must not reopen the devices or
                        # erase the separately permitted in-memory listener.
                        self.archive.storage_error='write-unavailable'
                        self.archive.sources={key:'unavailable' for key in self.archive.sources}
                        self.stop_event.wait(1)
                        continue
                    with self.lock:
                        self.capture_error = {"at": self.clock(), "error_type": type(error).__name__, "detail": str(error)[:120]}
                        self.capture_errors = getattr(self, "capture_errors", 0) + 1
                        self._invalidate("source-unavailable", preserve_dialogue=self.dialogue_gate.allowed)
                        self._set_sources({key: "unavailable" for key in self.sources}, self.clock())
                    logging.getLogger("butler.ambient").warning("capture restarted after %s: %s", type(error).__name__, str(error)[:200])
                    self.inputs.stop()
                    self.input_running = False
                    self.stop_event.wait(1)
        finally:
            if self.input_running:
                self.inputs.stop()

    def _analysis_loop(self):
        while not self.stop_event.is_set():
            try:
                generation = self.pending.get(timeout=.2)
            except queue.Empty:
                continue
            tag=generation[0] if isinstance(generation,tuple) else None
            archive=tag in {"archive","archive-request"}
            manual=tag in {"owner-request","archive-request"}
            if tag is not None:generation=generation[1]
            if not manual and self.store.get("analysis_paused",False):continue
            with self.lock:
                self.analysis_run_token = threading.Event()
                run_token = self.analysis_run_token
                self.schedules.manual["analysis"]="running"
            try:
                self.analysis_tick(generation,run_token,archive=archive)
            except Exception as error:
                self.analysis_error = "local-analysis-unavailable"
                self.analysis_diagnostic={"source":"batch","phase":"group-preparation","error_type":type(error).__name__}
                self.store.event("analysis", "analysis-failed", self.clock(), self.clock())
            finally:
                self.schedules.manual["analysis"]="stopped" if run_token.is_set() else "done"

    def analysis_tick(self, generation, run_token=None, *, archive=False):
        policy = self.archive if archive else self.gate
        with self.lock:
            if not policy.permits(self.clock()) or generation != policy.generation:
                return
            now = self.clock()
            snapshot_started=now
            if archive:self.archive.camera.finalize_stale(now)
            # Keep a recently missed tail ahead of the old backlog too. A
            # minute tick plus inference can cross the exact60s boundary;
            # those few seconds must not fall behind an hour of old input.
            pending = self.store.execute("SELECT * FROM media WHERE analyzed=0 AND complete=1 AND path IS NOT NULL AND end>=? ORDER BY (end>=?) DESC,(end>=?) DESC,(storage_class='spool') DESC,start LIMIT 720", (now - (86400 if archive else 60),now-60,now-180))
            overdue = [] if archive else self.store.execute("SELECT * FROM media WHERE analyzed=0 AND end<?", (now - 60,))
            if overdue:
                self.store.event("analysis", "analysis-missed", overdue[0]["start"], overdue[-1]["end"])
                self.store.execute("UPDATE media SET analyzed=-1 WHERE analyzed=0 AND end<?", (now - 60,))
        # Never bridge a permission/capture gap when aggregating a minute's samples.
        source_groups = []
        for source in ("camera", "microphone", "screen", "system-audio"):
            groups = []
            last_end = None
            group_start = None
            last_class = None
            last_meeting = None
            group_images=0
            for row in pending:
                if row["source"] != source:
                    continue
                if (source == "screen" or last_end is None or row['storage_class'] != last_class or row.get('meeting_id') != last_meeting
                        or source == 'camera' and group_images+sample_count(row)>self.camera_batch_size
                        or row["start"] - last_end > .2 or row['start'] < last_end-.25
                        or row["end"]-(group_start if group_start is not None else row["start"]) > 60):
                    groups.append([])
                    group_start=row["start"]
                    last_end = None
                    group_images=0
                groups[-1].append(row["id"])
                if source=='camera':group_images+=sample_count(row)
                last_end = max(last_end or row["end"], row["end"])
                last_class = row['storage_class']
                last_meeting = row.get('meeting_id')
            source_groups.append(groups)
        # Permission gaps can create many camera groups. Give each source a
        # turn before processing the next group so camera work cannot starve
        # microphone/screen analysis during short permitted intervals.
        failed_sources=set()
        failure=None
        for round_groups in itertools.zip_longest(*source_groups):
            for index,group in enumerate(round_groups):
                if self.stop_event.is_set() or (run_token is not None and run_token.is_set()):
                    return
                if not group or index in failed_sources:
                    continue
                if archive:
                    yield_for_fresh = False
                    with self.lock:
                        # Process the current minute before either kind of
                        # backlog. Old spool is old work too. Give catch-up a
                        # bounded minute, then yield BETWEEN calls for a fresh
                        # snapshot; do not turn every new PCM second into an
                        # extra model request or discard an older original.
                        old = self.store.execute('SELECT end FROM media WHERE id=?',(group[0],))
                        fresh = self.store.execute("SELECT id FROM media WHERE analyzed=0 AND complete=1 AND path IS NOT NULL AND end>=? LIMIT 1",(self.clock()-60,))
                        if (old and old[0]['end']<snapshot_started-60 and fresh and not failed_sources
                                and self.clock()-snapshot_started>=60):
                            try:self.pending.put_nowait(('archive',policy.generation))
                            except queue.Full:pass
                            yield_for_fresh = True
                    if yield_for_fresh:
                        if run_token is None or not run_token.is_set():
                            self._offer_advice(policy, generation, archive, run_token)
                        return
                # Capture and participant checks continue. Yield expensive
                # model work while an owner turn is listening/generating/playing.
                while self.voice is not None and (self.voice.busy or self.voice.turn_committed or (
                        self.listening is not None and self.listening.phase in {"listening", "transcription", "participant-check"})):
                    # The local model server is shared: a frame batch started now would queue the owner's reply behind it.
                    if self.stop_event.wait(.05) or (run_token is not None and run_token.is_set()): return
                    if generation != policy.generation: return
                with self.analysis_serial:
                    succeeded = self.analyze_group(group, generation,run_token,archive=archive)
                    if archive and not succeeded:
                        failed_sources.add(index)
                        failure=(self.analysis_error,dict(self.analysis_diagnostic))
        if failed_sources:
            # Preserve a real source error after successful work on the other
            # sources, and retry on the next scheduled tick rather than spin.
            with self.lock:
                if policy.permits(self.clock()) and generation==policy.generation:
                    self.analysis_error,self.analysis_diagnostic=failure
            # A failed source must not suppress fresh evidence from the others.
            if run_token is None or not run_token.is_set():
                self._offer_advice(policy, generation, archive, run_token)
            return
        if archive and not failed_sources and self.archive.permits(self.clock()) and not self.store.get("analysis_paused",False):
            # Catch up retained backlog in bounded minute groups, without turning
            # normal fresh one-second arrivals into continuous tiny model calls.
            if self.store.execute("SELECT id FROM media WHERE analyzed=0 AND complete=1 AND path IS NOT NULL AND end<? LIMIT 1", (self.clock()-60,)):
                try:self.pending.put_nowait(("archive",self.archive.generation))
                except queue.Full:pass
        if run_token is None or not run_token.is_set():
            self._offer_advice(policy, generation, archive, run_token)

    def _offer_advice(self, policy, generation, archive, run_token=None):
        with self.lock:
            if policy.permits(self.clock()) and generation == policy.generation:
                now = self.clock()
                self.store.expire(now)
                candidate = advice_candidate(self.store.history(now - 7200, now, expire=False), now)
                token = self.archive.token if archive else self.analysis_token
            else:
                candidate = None
        if candidate and not token.is_set() and not self.meeting.snapshot()["quiet"] and not quiet(now) and not quiet(now+45):
            from .conversation import Tokens
            from .daily_report import advice_report
            token=Tokens(token,self.schedules.tokens["advice"],run_token) if run_token is not None else Tokens(token,self.schedules.tokens["advice"])
            day=dt.datetime.fromtimestamp(now,ZONE).date().isoformat()
            with self.lock:
                if self.schedules.manual.get("advice")=="running" or self.store.get("muted",False):return
                last_attempt=getattr(self,"last_advice_attempt",None)
                if last_attempt is not None and now-last_attempt<900:return
                if self.store.execute("SELECT 1 FROM deliveries WHERE id=?",(candidate["key"],)):return
                rows=self.store.execute("SELECT COUNT(*) AS count, MAX(at) AS last FROM deliveries WHERE kind='advice' AND day=?",(day,))
                last=self.store.execute("SELECT MAX(at) AS last FROM deliveries WHERE kind='advice'")[0]["last"]
                if rows[0]["count"]>=3 or (last is not None and now-last<3600):return
                self.last_advice_attempt=now
                self.schedules.manual["advice"]="running"
            def generate():
                try:
                    text,topic=advice_report(self,now,token,with_topic=True)
                    if text and not token.is_set() and not self.meeting.snapshot()["quiet"]:
                        self.delivery.send(candidate["key"], "advice", text, self.clock(),
                                           topic=topic, created_at=candidate["created_at"], token=token,
                                           transport=self.display_transport)
                except Exception:
                    self.schedules.manual["advice"]="failed"
                    return
                self.schedules.manual["advice"]="done"
            threading.Thread(target=generate,daemon=True,name='butler-advice').start()

    def maintenance(self):
        now = self.clock()
        self.store.expire(now)
        with self.lock:
            self.meeting.update()
            voice = self.voice
            if voice is not None and getattr(voice, 'local_history_token', None) is not None:
                from .recordings import local_voice_history_allowed
                if not local_voice_history_allowed(self): self._end_local_history_scope()
            if self.voice is not None and self.voice.thread is not None:
                self.voice.ensure_running()
            if getattr(self, "calls", None) is not None: self.calls.poll(now)
            if (self.store.get("face_wake_dialogue", False) and not self.dialogue_gate.permits(now) and self.voice is not None
                    and self.voice.busy and not self.voice.turn_committed):
                self.voice.cancel(reason="stale-dialogue-check", private_only=True)
            if self.gate.allowed and not self.gate.permits(now):
                self._invalidate("stale-participant-check", preserve_dialogue=self.dialogue_gate.permits(now))
            if self.gate.permits(now):
                token = self.analysis_token
            else:
                from .recordings import history_visible
                if not history_visible(self):
                    return
                token = self.archive.token
        local = dt.datetime.fromtimestamp(now, ZONE)
        due = self.store.get("daily_time", "20:00")
        if not self.store.get("daily_paused",False) and local.strftime("%H:%M") >= due and not quiet(now):
            key = "daily:" + local.date().isoformat()
            if self.store.execute("SELECT id FROM deliveries WHERE id=?", (key,)):
                return
            from .conversation import Tokens
            from .daily_report import daily_report
            self.schedules.manual["daily"]="running"
            try:
                daily_token = Tokens(token,self.schedules.tokens["daily"])
                text = daily_report(self,now,daily_token)
                self.delivery.send(key, "daily", text, self.clock(), token=daily_token, transport=self.display_transport)
            finally:self.schedules.manual["daily"]="done"

    def _maintenance_loop(self):
        while not self.stop_event.is_set():
            try:
                if getattr(self,"catalog",None) is not None: self.catalog.refresh()
                self.maintenance()
            except Exception:
                self.store.event("service", "maintenance-failed", self.clock(), self.clock())
            self.stop_event.wait(1)

    def delivery_transport(self):
        """Core delivery while its switch is on (W3); the gateway CLI otherwise. Decided per message."""
        from .delivery import CoreNotices, SwitchedTransport, core_client
        from .components import WORKSPACE
        telegram = Telegram(self.config.get("telegram_target"), executable=self.config.get("openclaw_cli", "openclaw"))
        core = core_client(self.config.get('butler_workspace', WORKSPACE))
        self.notices = CoreNotices(core)
        from .core_link import CoreLink
        self.core_link = CoreLink(core, self.store.root, clock=self.clock)
        return SwitchedTransport(core, telegram)

    def owner_name(self):
        return str(self.config.get("owner_name") or "Владелец")

    def ensure_panel(self):
        """A presentation needs the panel: relaunch it when the process this service started has exited (REQ-005).
        Deploys never open the window; the panel itself decides to come forward when a new card appears."""
        launch = self.panel_launch
        if launch is None:
            return False
        if self.panel is not None and self.panel.poll() is None:
            return True
        try:
            self.panel = launch()
        except OSError:
            return False
        return self.panel is not None

    def start(self):
        self.feedback.start()
        if self.voice is not None:
            self.voice.start()
        if self.visual is not None:
            self.visual.start()
        if self.listening is not None:
            thread=threading.Thread(target=self.listening.loop,daemon=True)
            thread.start();self.threads.append(thread)
        for function in (self._check_loop, self._capture_loop, self._analysis_loop, self._maintenance_loop, self.archive.system_loop, self.disclosures.loop, self.commands.inventory_loop):
            thread = threading.Thread(target=function, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        if getattr(self,"catalog",None) is not None: self.catalog.stop()
        self.schedules.stop()
        self.stop_event.set()
        with self.lock:
            self._invalidate("service-stopped")
        if self.voice is not None:
            self.voice.stop()
        self.panel_chat.stop()
        if self.visual is not None:
            self.visual.stop()
        self.feedback.stop()
        for thread in self.threads:
            thread.join(timeout=5)
        self.screen_worker.shutdown(wait=True, cancel_futures=True)

    def control(self, command, args):
        # This method is exposed only on a mode-0600 local Unix socket, NEVER MCP.
        with self.lock:
            now = self.clock()
            self.meeting.update()
            if command == "enabled":
                if type(args["value"]) is not bool:
                    raise ValueError("enabled must be boolean")
                self.owner_setup.cancel()
                self.archive.invalidate()
                self._invalidate("owner-paused")
                self.gate.enable(args["value"], now)
                self.store.set("enabled", args["value"])
                if self.core_link is not None:
                    self.core_link.feedback("resume" if args["value"] else "full_pause", {"modality": "video+audio", "by": "owner"})
                return self.status()
            if command == "revoke":
                if args["id"] == "owner": self.owner_setup.cancel()
                self.store.execute("UPDATE agreements SET active=0 WHERE id=?", (args["id"],))
                self.store.set("single_user_local", False)
                if args["id"]=="owner":
                    self.archive.invalidate();self.gate.enable(False,now);self.store.set("enabled",False)
                self._invalidate("consent-revoked")
                if self.core_link is not None:
                    self.core_link.feedback("consent_revoked", {"participant": "owner" if args["id"] == "owner" else "guest"})
                return {"revoked": args["id"]}
            if command == "preserve":
                if self.gate.enabled and not self.gate.permits(now):
                    raise ValueError("preservation is blocked during observation pauses")
                return {"preserved": self.store.preserve(args["id"], now)}
            if command == "delete":
                self.store.delete(args["id"], results=bool(args.get("results", False)))
                return {"deleted": args["id"]}
            if command == "correct":
                subject, name = args["subject"], args["name"]
                if not subject or not name or len(name) > 80:
                    raise ValueError("valid subject/name required")
                self.store.execute("INSERT OR REPLACE INTO corrections VALUES (?,?)", (subject, name))
                return {"corrected": subject}
            if command == "preferences":
                if set(args) - {"muted", "disabled_topics", "daily_time", "voice_enabled", "address_aliases", "face_wake_dialogue", "meeting_mode", "continuous_archive", "general_dialogue", "voice_brain"}:
                    raise ValueError("unknown preference")
                if "voice_brain" in args and args["voice_brain"] not in ("agent", "local"):
                    raise ValueError("voice_brain must be agent or local")
                if "daily_time" in args:
                    value = args["daily_time"]
                    parsed = dt.time.fromisoformat(value)
                    if len(value) != 5 or not 8 <= parsed.hour < 22:
                        raise ValueError("daily time must be HH:MM outside quiet hours")
                if "disabled_topics" in args and (not isinstance(args["disabled_topics"], list) or
                         set(args["disabled_topics"]) - {"work", "rest", "reading", "entertainment"}):
                    raise ValueError("invalid topics")
                if "muted" in args and type(args["muted"]) is not bool:
                    raise ValueError("muted must be boolean")
                if "voice_enabled" in args and type(args["voice_enabled"]) is not bool:
                    raise ValueError("voice_enabled must be boolean")
                if "continuous_archive" in args and type(args["continuous_archive"]) is not bool:
                    raise ValueError("continuous_archive must be boolean")
                if "general_dialogue" in args and type(args["general_dialogue"]) is not bool:
                    raise ValueError("general_dialogue must be boolean")
                if "continuous_archive" in args:
                    self.history_summaries.cancel()
                    self._end_local_history_scope()
                    self.archive.invalidate()
                    self.capture_epoch += 1
                    self._discard_checks()
                if "continuous_archive" in args and self.listening is not None:self.listening.clear()
                if "meeting_mode" in args and args["meeting_mode"] not in {"auto", "quiet", "available"}:
                    raise ValueError("invalid meeting mode")
                if "face_wake_dialogue" in args and type(args["face_wake_dialogue"]) is not bool:
                    raise ValueError("face_wake_dialogue must be boolean")
                if "address_aliases" in args and (not isinstance(args["address_aliases"], list) or
                        not 1 <= len(args["address_aliases"]) <= 6 or any(not isinstance(x, str) or
                        not x.strip() or len(x) > 30 for x in args["address_aliases"])):
                    raise ValueError("invalid address aliases")
                for key, value in args.items():
                    self.store.set(key, value)
                if args.get("continuous_archive") is True:
                    self.store.execute("UPDATE media SET analyzed=0 WHERE analyzed=-1 AND path IS NOT NULL AND start>=?",(now-86400,))
                if "meeting_mode" in args:
                    self.meeting.update()
                if args.get("general_dialogue") is False and self.voice is not None:
                    self.voice.end_general("general-dialogue-disabled")
                if args.get("voice_enabled") is False and self.voice is not None:
                    self.disclosures.cancel()
                    self.voice.cancel(stop_worker=True)
                if "face_wake_dialogue" in args:
                    self.dialogue_gate.invalidate()
                    if self.voice is not None: self.voice.cancel()
                return {"saved": True}
            raise ValueError("unknown control")

    def status(self):
        from . import __version__
        with self.lock:
            if self.gate.allowed and not self.gate.permits(self.clock()):
                self._invalidate("stale-participant-check", preserve_dialogue=self.dialogue_gate.permits(self.clock()))
            gate = self.gate.status(self.clock())
            if not gate["recording_allowed"]:
                self.sources = {k: ("unavailable" if v == "unavailable" else "paused") for k, v in self.sources.items()}
            archive = self.archive.snapshot()
            voice = self.voice
            evidence = getattr(self, "latest_evidence", None)
            state, phase = getattr(voice,"state",None), getattr(voice,"phase",None)
            local_models = (getattr(voice,"model",None),getattr(voice,"vision_model",None))
            local_inference = any(isinstance(getattr(model,"operations",None),set) and bool(model.operations) for model in local_models)
            analysis_busy = ((evidence is not None and 0 <= self.clock() - evidence.captured_at <= Gate.MAX_AGE and evidence.speech_detected)
                or getattr(getattr(self,"listening",None),"phase",None) in {"listening","transcription","participant-check"}
                or state in {"listening", "synthesizing", "speaking"}
                or state == "thinking" and phase in {"transcription","address","awaiting-continuation"}
                or local_inference)
            return {**gate, "version": __version__, "analysis_busy": analysis_busy, "recording_allowed":archive["enabled"] or gate["recording_allowed"],
                    "reason":"passive-local-recording" if archive["enabled"] else gate["reason"],"action_reason":gate["reason"],
                    "action_permission":gate["recording_allowed"], "archive":archive,
                    "sources":archive["sources"] if archive["enabled"] else dict(self.sources), "analysis_error": self.analysis_error,
                    "analysis_diagnostic":self.analysis_diagnostic,
                    "analysis_inference":analysis_inference_status(getattr(self.analyzer,'analysis_inference',None)),
                    "camera_analysis_batch_size":self.camera_batch_size,
                    "check_latency_seconds": self.check_latency_seconds,
                    "capture_error": getattr(self, "capture_error", None), "capture_errors": getattr(self, "capture_errors", 0),
                    "participant_check_skipped": self.check_skipped,
                    "participant_check_error": self.check_error,
                    "participant_check": getattr(self.verifier, "last_failure", None) if isinstance(getattr(self.verifier, "last_failure", None), str) else None,
                    "participant_verifier": "unavailable" if getattr(self.verifier, "unavailable", False) else "local-enrolled-references",
                    "agreements": [{"id": r["id"], "name": r["name"], "active": bool(r["active"])}
                                   for r in self.store.execute("SELECT * FROM agreements")],
                    "gaps": self.store.execute("SELECT start,end,source,reason FROM events ORDER BY id DESC LIMIT 30")}


class RPCServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, service):
        self.service = service
        self.guard = (service.store.root / "service.lock").open("a")
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.guard.close()
            raise RuntimeError("another ambient service owns this state directory") from None
        service.store.recover_camera_clips()
        path = service.store.root / "ambient.sock"
        path.unlink(missing_ok=True)
        super().__init__(str(path), Handler)
        os.chmod(path, 0o600)

    def server_close(self):
        super().server_close()
        Path(self.server_address).unlink(missing_ok=True)
        self.guard.close()


class Handler(socketserver.StreamRequestHandler):
    def _reply(self, payload: bytes, command) -> None:
        try:
            self.wfile.write(payload)
        except OSError:
            # The client (the panel kills a slow poll) went away; nothing to answer and nothing to crash on.
            logging.getLogger("butler.ambient").info("client closed before the %s reply", command)

    def handle(self):
        self.connection.settimeout(120)
        command = None
        try:
            request = json.loads(self.rfile.readline(65537))
            command, args = request["command"], request.get("args", {})
            service = self.server.service
            if command == "status":
                result = service.status()
            elif command == "ui-feedback":
                from .recordings import history_visible
                with service.lock:
                    result = service.feedback.snapshot(history_visible(service))
                    if not history_visible(service): result = service.feedback.snapshot(False)
            elif command == "ui-attention":
                if set(args) != {"mode"}: raise ValueError("invalid attention control")
                result = service.attention.manual(args["mode"])
                service.meeting.update()
            elif command == "ui-feedback-correct":
                result = service.feedback.correct(args)
            elif command == "ui-rule":
                result = service.feedback.rule_control(args)
            elif command == "ui-rule-context":
                if set(args) != {"turn"}: raise ValueError("invalid rule context request")
                result = service.feedback.agent_context(args["turn"])
            elif command == "ui-state":
                from .local_ui import snapshot
                result = snapshot(service, want_preview=args.get("preview") is True)
            elif command == "ui-local-access-status":
                if args: raise ValueError("invalid local access status")
                from .recordings import _allowed, history_visible, local_voice_history_allowed
                with service.lock:
                    voice_ready = bool(service.voice is not None and service.voice.ready)
                    evidence = getattr(service, 'latest_evidence', None)
                    captured_at = getattr(evidence, 'captured_at', None)
                    audio = getattr(service.inputs, 'audio_status', {})
                    audio = audio if isinstance(audio, dict) else {}
                    from .voice_catalog import profiles
                    selected = service.store.get('voice_profile')
                    trusted = service.trusted_ids()
                    scene = getattr(evidence, 'scene', None)
                    faces = getattr(scene, 'faces', 0) if scene is not None and getattr(scene, 'available', False) else 0
                    result = {"single_user_local": service.store.get("single_user_local", False) is True,
                              "history_visible": history_visible(service), "raw_allowed": _allowed(service),
                              "local_feedback_restored": bool(service.feedback.local_restored),
                              "voice_sensing_ready": local_voice_history_allowed(service),
                              "call_safe": service.meeting.local_summary_safe(), "voice_output_ready": voice_ready,
                              "local_summary_ready": bool(voice_ready and service.voice.local_history_audio_permitted()),
                              "voice_enabled": service.store.get('voice_enabled', False) is True,
                              "voice_profile_available": selected in {p['id'] for p in profiles(service.store)},
                              "echo_cancellation_ready": audio.get('echo_cancellation') is True,
                              "voice_worker_warm": bool(service.voice is not None and service.voice.readiness.get('voice') == 'ready'),
                              "sensing_fresh": bool(type(captured_at) in (int, float) and 0 <= service.clock()-captured_at <= Gate.MAX_AGE),
                              "camera_healthy": getattr(evidence, 'camera_ok', False) is True,
                              "microphone_healthy": getattr(evidence, 'microphone_ok', False) is True,
                              "other_face_detected": bool(evidence and evidence.camera_ok and evidence.camera_certain and evidence.camera_ids - trusted),
                              "other_voice_detected": bool(evidence and evidence.voice_certain and evidence.voice_ids - trusted),
                              "multiple_faces_detected": faces >= 2}
            elif command == "ui-observe":
                from .local_ui import observe
                result = observe(service, args["source"])
            elif command == "ui-single-user":
                if set(args) != {"enabled"} or type(args["enabled"]) is not bool:
                    raise ValueError("invalid single-user setting")
                with service.lock:
                    if args["enabled"] and not service.store.execute("SELECT 1 FROM agreements WHERE id='owner' AND active=1"):
                        raise ValueError("active owner consent required")
                    service.history_summaries.cancel()
                    service._end_local_history_scope()
                    service.store.set("single_user_local", args["enabled"])
                    result = {"single_user_local": args["enabled"]}
            elif command == "voice-preview":
                if service.voice is None: raise ValueError("voice unavailable")
                result = service.voice.preview(args["profile"])
            elif command == "voice-select":
                # Through the running service's own store: a second process opening the live database raced its housekeeping.
                if set(args) != {"profile"} or args["profile"] not in {"calm", "baritone", "warm"}: raise ValueError("invalid voice profile")
                from .voice_catalog import select
                result = select(service.store, args["profile"])
            elif command == "ui-settings":
                from .local_settings import snapshot
                result = snapshot(service)
            elif command == "ui-video-effects":
                helper = getattr(service.inputs, "camera_helper", None)
                if helper is None or args: raise ValueError("native video settings unavailable")
                process = subprocess.Popen([str(helper), "--video-effects"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                threading.Thread(target=process.wait, daemon=True).start()
                result = {"opened": True}
            elif command == "ui-guest-setup":
                if set(args)!={"name"}:raise ValueError("invalid guest setup request")
                # The refusals of start() are fixed owner-facing sentences, never sensing or model output.
                try: result=service.owner_setup.start(guest_name=args["name"])
                except ValueError as error: result={"queued":False,"message":str(error)[:300]}
            elif command in {"ui-owner-setup","ui-owner-setup-cancel"}:
                if args: raise ValueError("invalid owner setup request")
                if command == "ui-owner-setup":
                    try: result = service.owner_setup.start()
                    except ValueError as error: result = {"queued":False,"message":str(error)[:300]}
                else:
                    service.owner_setup.cancel()
                    result = {"cancelled":True}
            elif command in {"ui-catalog", "ui-catalog-refresh"}:
                catalog = getattr(service,"catalog",None)
                if catalog is None: result = {"providers":[],"refreshing":False,"error":"catalog-unavailable"}
                else:
                    if command == "ui-catalog-refresh": catalog.refresh(force=True)
                    result = catalog.snapshot()
            elif command == "ui-components":
                from .components import snapshot
                result = snapshot(service)
            elif command == "ui-schedules":
                result = service.schedules.snapshot()
            elif command in {"ui-components-update", "ui-schedule-control"}:
                try:
                    if command == "ui-components-update":
                        from .components import update
                        if service.restart is None: raise ValueError("own-service restart unavailable")
                        result = update(service,args)
                        if result.get("restart_required"):
                            timer = threading.Timer(.5,service.restart)
                            timer.daemon = True
                            timer.start()
                    else:
                        result = service.schedules.control(args)
                except ValueError as error:
                    # These controls validate configuration, never process raw
                    # sensing/model output. Explanations stay on the owner UI.
                    result = {"saved":False,"ok":False,"message":str(error)[:500]}
            elif command in {"ui-voice-check", "ui-voice-benchmark"}:
                if args or service.voice is None: raise ValueError("fixed check unavailable")
                result = service.voice.check(benchmark=command == "ui-voice-benchmark")
            elif command == "ui-settings-update":
                from .local_settings import update
                if service.restart is None: raise ValueError("own-service restart unavailable")
                result = update(service, args)
                timer = threading.Timer(.5, service.restart)
                timer.daemon = True
                timer.start()
            elif command == "visual-request":
                if service.visual is None: raise ValueError("visual unavailable")
                # The MCP command has no supplied text/recipient. Resolve the
                # actual recent owner Telegram message inside the authority.
                result = service.visual.telegram_request()
            elif command == "ui-visual-request":
                if service.visual is None: raise ValueError("visual unavailable")
                result = service.visual.submit(args["intent"], args.get("criteria", ""), telegram=args.get("telegram") is True)
            elif command == "ui-material-add":
                result = service.visual.add_root(args["path"])
            elif command == "ui-material-remove":
                result = service.visual.remove_root(args["path"])
            elif command == "ui-visual-delete":
                from .recordings import _allowed
                if not _allowed(service):raise ValueError("verified owner required")
                result = service.visual.delete(args["id"])
            elif command == "ui-visual-cancel":
                service.visual.cancel()
                result = {"cancelled": True}
            elif command == "ui-send-text":
                # The owner's written message from the panel. Returns at once; the answer arrives through ui-state.
                if set(args) != {"text"}: raise ValueError("invalid panel message")
                try:
                    result = service.panel_chat.send(args["text"])
                except ValueError as error:
                    # The refusal is about the owner's own message (empty, too long, queue full): explained on the panel only.
                    result = {"queued": False, "message": str(error)[:300]}
            elif command == "ui-observations":
                from .observation_browser import browse
                from .recordings import _allowed, history_visible
                with service.lock:
                    result = browse(service, args) if history_visible(service) else {"hidden": True, "items": [], "days": [], "total": 0}
                    if not history_visible(service): result = {"hidden": True, "items": [], "days": [], "total": 0}
                    elif not _allowed(service):
                        for item in result.get("items", []): item.pop("frame", None)
            elif command == "ui-recordings":
                from .recordings import list_recordings
                result = list_recordings(service, args)
            elif command == "ui-history-summary":
                action = args.get('action')
                if action == 'start':
                    result = service.history_summaries.start({k: v for k, v in args.items() if k != 'action'})
                elif action == 'status' and set(args) == {'action', 'id'}:
                    result = service.history_summaries.status(args['id'])
                elif action == 'cancel' and set(args) == {'action', 'id'}:
                    result = service.history_summaries.cancel_id(args['id'])
                else: raise ValueError('invalid summary command')
            elif command == "ui-recording-play":
                from .recordings import playback
                result = playback(service, args)
            elif command == "ui-recording-meeting":
                from .recordings import mark_meeting
                result = mark_meeting(service, args)
            elif command == "ui-recording-control":
                from .recordings import control
                result = control(service, args)
            elif command == "ui-dictate":
                if set(args) != {"action"} or args["action"] not in {"start", "stop"}: raise ValueError("invalid dictation request")
                result = service.dictation.start() if args["action"] == "start" else service.dictation.stop()
            elif command == "ui-panel-chat-clear":
                if args: raise ValueError("invalid panel chat request")
                result = service.panel_chat.clear("owner")
            elif command == "display":
                # The agent's card (MCP butler_display) or any owner-local caller: validated, shown, window forward.
                # DEC-009: local Display is independent of biometric/observation checks.
                try:
                    result = service.display.present(args, origin="agent")
                except CardRefused as error:
                    # A malformed card is the agent's to correct: the reason names a field or a rule, never content.
                    result = {"shown": False, "refused": str(error)[:160]}
            elif command == "ui-display-clear":
                if args: raise ValueError("invalid display clear request")
                result = service.display.clear("owner") or {"cleared": True}
            elif command == "ui-display-select":
                result = service.display.select(args["id"])
            elif command == "ui-display-action":
                result = service.display.action(args["id"], args["action"])
            elif command == "screen-permission":
                result = service.request_screen_permission()
            elif command == "observe":
                result = service.observe(args["source"])
            elif command == "summary":
                with service.lock:
                    if not service.gate.permits(service.clock()):
                        raise ValueError("external summaries blocked during observation pauses")
                    result = summarize(service.store, float(args["start"]), float(args["end"]))
            else:
                result = service.control(command, args)
            self._reply(json.dumps({"result": result}).encode() + b"\n", command)
        except Exception as error:
            # Never forward raw capture, transcript, model, or subprocess errors to the client. The server log keeps
            # the command, the exception class and the frames only, never the message text, so a failed poll can be
            # told apart from a policy refusal without leaking content.
            import traceback
            frames = " < ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(traceback.extract_tb(error.__traceback__)[-4:]))
            logging.getLogger("butler.ambient").warning("request %s failed: %s (%s)", command, type(error).__name__, frames)
            self._reply(b'{"error":"request unavailable, invalid or prohibited by observation policy"}\n', command)
