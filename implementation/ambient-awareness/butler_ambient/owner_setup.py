"""Deliberate local owner voice renewal after native macOS authentication."""
from __future__ import annotations
import json
import os
import secrets
import select
from . import processes as subprocess
import threading
import time
import uuid
import re
from pathlib import Path
from .analysis import LocalAnalyzer
from .sensing import Batch


def challenge_matches(text, challenge):
    words = {"ноль":"0","нуль":"0","один":"1","раз":"1","два":"2","три":"3","четыре":"4",
        "пять":"5","шесть":"6","семь":"7","восемь":"8","девять":"9"}
    digits = "".join(value if value.isdigit() else words.get(value,"") for value in re.findall(r"\d+|[а-яё]+",text.lower()))
    return len(challenge) == 6 and challenge in digits


class SetupFailed(RuntimeError): pass


class OwnerSetup:
    ACTIVE = {"authenticating","recording","validating"}
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.stage, self.error = "idle", None
        self.pcm = bytearray()
        self.cancelled = threading.Event()
        self.complete = threading.Event()
        self.process = self.thread = self.analyzer = None
        self.started_at = self.last_end = None
        self.target_id,self.guest_name = "owner",None
        self.frames=[]

    @property
    def capturing(self): return self.stage in {"recording","validating"}

    def snapshot(self):
        with self.lock: return {"stage":self.stage,"seconds":round(len(self.pcm)/32000,1),"error":self.error,
            "target":"guest" if self.guest_name else "owner",
            "method":"macOS account authentication + fresh spoken challenge; local voice templates"}

    def cancel(self):
        with self.lock:
            if self.stage not in self.ACTIVE: return
            self.cancelled.set(); self.complete.set(); self.pcm.clear()
            self.frames.clear()
            self.stage, self.error = "cancelled", "owner-cancelled"
            process, analyzer = self.process, self.analyzer
        if analyzer is not None: analyzer.cancel()
        if process is not None and process.poll() is None: process.terminate()

    def start(self, *, guest_name=None):
        s = self.service
        helper = Path(s.config.get("audio_helper","/missing")).with_name("butler-owner-auth")
        with s.lock, self.lock:
            if self.stage in self.ACTIVE: raise ValueError("Проверка владельца уже открыта.")
            if guest_name is not None and (not isinstance(guest_name,str) or not 1<=len(guest_name.strip())<=80 or any(ord(x)<32 for x in guest_name)):
                raise ValueError("Нужно короткое имя гостя.")
            signal = getattr(s.inputs,"input_signal",None)
            if (not s.gate.enabled or not isinstance(signal,dict) or not 0 <= s.clock()-signal["at"] <= 2.5
                    or s.inputs.audio_status.get("input_verified") is not True):
                raise ValueError("Для регистрации нужен включённый подтверждённый микрофон. Полная Пауза сохраняется.")
            if not helper.is_file() or s.voice is None: raise ValueError("Локальная проверка владельца сейчас недоступна.")
            if not s.store.execute("SELECT id FROM agreements WHERE id='owner' AND active=1"):
                raise ValueError("Нужно действующее согласие владельца.")
            self.stage, self.error = "authenticating", None
            self.guest_name=guest_name.strip() if guest_name else None
            self.target_id="guest-"+uuid.uuid4().hex if self.guest_name else "owner"
            self.cancelled, self.complete = threading.Event(), threading.Event()
            self.pcm.clear()
            self.frames.clear()
            self.thread = threading.Thread(target=self._run,args=(helper,),daemon=True)
            self.thread.start()
        return {"queued":True,"local_authentication_required":True,"new_enrollment_started":False}

    def feed(self, batch):
        with self.lock:
            if self.stage != "recording": return
            if (len(batch.pcm) != 32000 or "microphone" in batch.gaps or
                    not 0 <= self.service.clock()-batch.end <= 2.5 or
                    self.last_end is not None and batch.start-self.last_end > .25):
                self.error = "microphone-gap"; self.complete.set(); return
            if batch.end <= self.started_at: return
            # Never enroll the part of a queued buffer preceding Start.
            offset = max(0, min(32000, int((self.started_at-batch.start)*16000)*2))
            remaining = 32000*15-len(self.pcm)
            self.pcm.extend(batch.pcm[offset:offset+remaining])
            if self.guest_name and batch.frames and (batch.frame_at is None or batch.frame_at>=self.started_at):
                self.frames.append(batch.frames[-1]);self.frames[:]=self.frames[-15:]
            self.last_end = batch.end
            if len(self.pcm) >= 32000*15: self.complete.set()

    def _run(self, helper):
        s = self.service
        code = str(secrets.randbelow(900000)+100000)
        process = None
        try:
            with self.lock:
                if self.cancelled.is_set(): raise SetupFailed("owner-auth-cancelled")
                argv=[str(helper),code]+([self.guest_name] if self.guest_name else [])
                process = subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                self.process = process
            deadline = time.monotonic()+120
            while not select.select([process.stdout],[],[],.1)[0]:
                if self.cancelled.is_set() or process.poll() is not None or time.monotonic()>deadline: raise SetupFailed("owner-auth-cancelled")
            approval = json.loads(process.stdout.readline(4096))
            if approval != {"authorized":True,"start":True} or self.cancelled.is_set(): raise SetupFailed("owner-auth-cancelled")
            with s.lock:
                rows = s.store.execute("SELECT reference FROM agreements WHERE id='owner' AND active=1")
                if not s.gate.enabled or not rows: raise SetupFailed("owner-paused-or-revoked")
                original = rows[0]["reference"]
                s._invalidate("owner-voice-enrollment")
                with self.lock:
                    self.stage = "recording"
                    self.started_at, self.last_end = s.clock(), None
            deadline = time.monotonic()+22
            while not self.complete.wait(.1):
                if self.cancelled.is_set() or process.poll() is not None or time.monotonic()>deadline: raise SetupFailed("owner-capture-cancelled")
            with self.lock:
                if self.cancelled.is_set() or self.error or len(self.pcm) < 32000*15: raise SetupFailed(self.error or "owner-capture-cancelled")
                pcm = bytes(self.pcm)
                self.stage = "validating"
            analyzer = LocalAnalyzer(whisper_model=s.config.get("whisper_model"),whisper_cli=s.config.get("whisper_cli","whisper-cli"),whisper_language="ru")
            analyzer.transcriber = s.voice.transcriber if s.voice.transcriber.available else None
            with self.lock: self.analyzer = analyzer
            transcript = analyzer.transcribe(Batch(0,15,[],pcm).wav(),token=self.cancelled)
            if not challenge_matches(transcript,code): raise SetupFailed("spoken-challenge-mismatch")
            del transcript
            vectors = s.verifier._voices(pcm)
            if not vectors: raise SetupFailed("not-enough-clear-speech")
            count = min(12,len(vectors))
            chosen = [vectors[i*(len(vectors)-1)//max(1,count-1)].tolist() for i in range(count)]
            if self.guest_name:
                reference=self._guest_reference(chosen)
            else:
                reference = json.loads(original)
                existing = reference.get("voice_samples",[reference["voice"]])
                reference["voice_samples"] = [*existing[-(30-count):],*chosen]
            with s.lock:
                if self.cancelled.is_set() or not s.gate.enabled: raise SetupFailed("owner-paused-or-revoked")
                current = s.store.execute("SELECT reference FROM agreements WHERE id='owner' AND active=1")
                if not current or current[0]["reference"] != original: raise SetupFailed("owner-references-changed")
                encoded = json.dumps(reference)
                if self.guest_name:
                    s.store.execute("INSERT INTO agreements VALUES (?,?,1,?)",(self.target_id,self.guest_name,encoded))
                else:
                    backup = s.store.root/"backups"/("owner-voice-"+uuid.uuid4().hex+".json")
                    backup.parent.mkdir(mode=0o700,exist_ok=True)
                    with backup.open("x") as stream:
                        os.chmod(backup,0o600); stream.write(original)
                    s.store.execute("UPDATE agreements SET reference=? WHERE id='owner' AND active=1 AND reference=?",(encoded,original))
                saved = s.store.execute("SELECT reference FROM agreements WHERE id=? AND active=1",(self.target_id,))
                if not saved or saved[0]["reference"] != encoded: raise SetupFailed("owner-references-changed")
                s._invalidate("owner-reference-updated")
                with self.lock: self.stage, self.error = "saved", None
        except SetupFailed as error:
            with self.lock:
                if not self.cancelled.is_set(): self.stage,self.error = "failed",str(error)
        except Exception:
            with self.lock:
                if not self.cancelled.is_set(): self.stage,self.error = "failed","local-owner-setup-unavailable"
        finally:
            with self.lock:
                self.pcm.clear(); self.frames.clear(); self.analyzer = None
            if process is not None:
                try: process.stdin.write(b"done\n"); process.stdin.flush()
                except (OSError,ValueError): pass
                try: process.communicate(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.communicate()

    def _guest_reference(self, voices):
        # Names come from the authenticated owner. Model guesses and temporary
        # anonymous fingerprints can never create a durable enrolled identity.
        s=self.service
        import numpy as np
        def similarity(left,right):
            left,right=np.asarray(left),np.asarray(right)
            value=float(np.dot(left,right)/(np.linalg.norm(left)*np.linalg.norm(right)))
            if not np.isfinite(value):raise SetupFailed("guest-reference-unavailable")
            return value
        references=s.references();faces=[]
        for jpeg in self.frames:
            detected=s.verifier._faces(jpeg,check_bodies=False)
            if len(detected)!=1:continue
            vector=detected[0].tolist()
            if any(similarity(vector,old)>=s.verifier.FACE_THRESHOLD for ref in references.values()
                    for old in ref.get('face_samples',[ref['face']])):continue
            faces.append(vector)
        if len(faces)<3 or not voices:raise SetupFailed("guest-needs-single-clear-face-and-voice")
        if any(similarity(face,faces[0])<s.verifier.FACE_THRESHOLD for face in faces):raise SetupFailed("guest-face-ambiguous")
        if any(similarity(voice,old)>=.82 for voice in voices for ref in references.values()
                for old in ref.get('voice_samples',[ref['voice']])):raise SetupFailed("guest-voice-already-enrolled-or-ambiguous")
        centroid=np.mean(np.asarray(voices),axis=0)
        if any(similarity(voice,centroid)<.82 for voice in voices):raise SetupFailed("guest-voice-ambiguous")
        return {"face":faces[0],"face_samples":faces[:15],"voice":centroid.tolist(),"voice_samples":voices}
