"""Fixed public disclosures have no access to private dialogue or generated text."""
from __future__ import annotations
import time
import uuid

DISCLOSURES={
    'hearing':'Да, я вас слышу.',
    'recording':'Здравствуйте. Я Батлер, локальный помощник. Сейчас на этом компьютере ведётся запись и анализ.',
    'sensing':'Здравствуйте. Я Батлер, локальный помощник. Сейчас я проверяю присутствие. Запись на паузе.',
}


class Disclosures:
    def __init__(self,service):
        self.service=service
        self.state='idle';self.completed=0;self.error=None
        self.active_playback=None

    def cancel(self):
        if self.active_playback is not None:self.service.inputs.cancel_playback()

    def tracker(self):
        from .participants import Participants
        value=getattr(self.service.verifier,'participants',None)
        return value if isinstance(value,Participants) else None

    def snapshot(self):
        tracker=self.tracker()
        return {'state':self.state,'completed':self.completed,'error':self.error,
                'participants':tracker.snapshot(self.service.clock()) if tracker else None,
                'text_scope':'fixed public disclosure only; no private conversation'}

    def allowed(self, *, explicit=False):
        s=self.service
        return (s.gate.enabled and not s.stop_event.is_set() and s.store.get('voice_enabled',False)
                and (explicit or not s.meeting.snapshot()['quiet']) and not s.owner_setup.capturing)

    def attempt(self):
        s=self.service;tracker=self.tracker();voice=s.voice
        if tracker is None or voice is None:return False
        pending=tracker.pending(s.clock())
        if not pending:return False
        if not self.allowed():self.state='suppressed';return False
        people=tracker.snapshot(s.clock())
        playback_audible=(s.archive.system_dbfs is not None and s.archive.system_dbfs>-55
            and 0<=s.clock()-s.archive.last.get('system-audio',float('-inf'))<=3)
        if people['unknown_faces']==0 and playback_audible:
            self.state='media-audio';return False
        key='recording' if s.archive.enabled else 'sensing'
        return self.play_fixed(key, on_started=lambda:tracker.acknowledge(pending,s.clock()))

    def play_fixed(self, key, *, explicit=False, valid=lambda:True, on_started=lambda:None,interrupt=False):
        if key not in DISCLOSURES:raise ValueError('unknown fixed public phrase')
        if interrupt and (key!='hearing' or not explicit):raise ValueError('only an explicit public hearing check may interrupt')
        s=self.service;voice=s.voice
        permitted=lambda:self.allowed(explicit=explicit) and valid()
        if not permitted() or voice is None or not voice.ready:return False
        if interrupt:
            with s.lock:
                if not permitted():return False
                if voice.busy or not voice.jobs.empty():
                    voice.cancel(clear_context=False,reason='public-hearing-check')
            # The old private speak() finally block owns this lock until all
            # its audio cancellation is complete. Never wait with s.lock held.
            deadline=time.monotonic()+.75
            acquired=False
            while permitted() and time.monotonic()<deadline:
                if voice.output_lock.acquire(timeout=min(.05,max(0,deadline-time.monotonic()))):
                    acquired=True;break
            if not acquired:return False
        elif voice.busy or not voice.output_lock.acquire(blocking=False):return False
        playback=None;started=False
        try:
            cache=s.store.root/'voice/prepared'/str(s.store.get('voice_profile'))/('disclosure-'+key+'.wav')
            if not cache.is_file() or cache.is_symlink():self.state='preparing';return False
            with s.lock:
                if not permitted() or voice.busy:return False
                playback=uuid.uuid4().hex
                self.active_playback=playback
                s.inputs.play(cache,playback)
                self.state='speaking'
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                if not permitted() or voice.busy:break
                value=dict(s.inputs.playback)
                if value.get('id')!=playback:break
                if value.get('state') in {'started','done'} and not started:
                    on_started();started=True
                if value.get('state')=='done':
                    self.completed+=1;self.state='completed';return True
                if value.get('state') in {'failed','cancelled'}:break
                s.stop_event.wait(.05)
            self.state='interrupted';return False
        except (OSError,RuntimeError,ValueError):self.error='public-audio-unavailable';return False
        finally:
            if playback is not None and s.inputs.playback.get('id')==playback and s.inputs.playback.get('state')!='done':s.inputs.cancel_playback()
            if self.active_playback==playback:self.active_playback=None
            voice.output_lock.release()

    def loop(self):
        while not self.service.stop_event.is_set():
            self.attempt()
            self.service.stop_event.wait(.2)
