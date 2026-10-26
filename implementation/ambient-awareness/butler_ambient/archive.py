"""DEC-038 passive local archive, separate from privileged participant actions."""
from __future__ import annotations
import queue
import threading
import io
import json
import select
from . import processes as subprocess
import time
import wave
import math
import shutil
import sqlite3
from array import array
from pathlib import Path
from .store import AnalysisBufferFull


class PassiveArchive:
    def __init__(self, service):
        self.service=service
        self.generation=0
        self.token=threading.Event()
        self.last_screen=self.last_analysis=float('-inf')
        self.sources={key:'paused' for key in ('camera','microphone','screen','system-audio')}
        self.last={}
        self.process=None
        self.system_error=None
        self.system_dbfs=None
        self.storage_error=None
        self.write_errors={}
        self.free_bytes=None
        from .camera_archive import CameraClips
        self.camera=CameraClips(self)

    def storage_available(self):
        try:self.free_bytes=shutil.disk_usage(self.service.store.root).free
        except OSError:self.storage_error='storage-unavailable';return False
        if self.free_bytes<256*1024*1024:
            self.storage_error='low-disk-space';return False
        if self.storage_error in {'low-disk-space','storage-unavailable'}:
            self.storage_error=next(iter(self.write_errors.values()),None)
        return True

    def write_result(self,source,start,end,error=None):
        if error is None:self.write_errors.pop(source,None)
        else:
            code='analysis-buffer-full' if isinstance(error,AnalysisBufferFull) else 'write-unavailable'
            self.write_errors[source]=code
            try:self.service.store.merge_gap(source,code,start,end)
            except sqlite3.Error:pass  # A failed diagnostic cannot disable hearing.
        self.storage_error=next(iter(self.write_errors.values()),None)

    @property
    def enabled(self):return self.service.gate.enabled and self.service.store.get('continuous_archive',False)

    def permits(self, now):return self.enabled and not self.service.stop_event.is_set() and self.storage_available()

    def invalidate(self):
        self.camera.finish()
        self.generation+=1
        self.token.set();self.token=threading.Event()
        self.sources={key:'paused' for key in self.sources}
        self.write_errors.clear()
        if self.storage_error not in {'low-disk-space','storage-unavailable'}:self.storage_error=None
        self.last.clear()
        self.system_dbfs=None
        process=self.process
        if process is not None and process.poll() is None:process.terminate()

    def _save_system(self, pcm, generation):
        s=self.service;now=s.clock()
        with s.lock:
            if not self.permits(now) or generation!=self.generation:return
            out=io.BytesIO()
            with wave.open(out,'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(pcm)
            try:s.store.media('system-audio',now-len(pcm)/32000,now,out.getvalue(),'wav',temporary=True)
            except (OSError,sqlite3.Error) as error:
                self.write_result('system-audio',now-len(pcm)/32000,now,error)
                self.sources['system-audio']='unavailable';return
            self.write_result('system-audio',now-len(pcm)/32000,now)
            samples=array('h',pcm)
            rms=math.sqrt(sum(x*x for x in samples)/max(1,len(samples)))/32768
            self.system_dbfs=max(-96,20*math.log10(max(rms,1e-8)))
            self.sources['system-audio']='active';self.last['system-audio']=now

    def system_loop(self):
        s=self.service
        helper=Path(s.config.get('audio_helper','/missing')).with_name('butler-system-audio')
        while not s.stop_event.is_set():
            if not self.permits(s.clock()):
                if self.enabled:self.sources['system-audio']='unavailable'
                s.stop_event.wait(.2);continue
            if not helper.is_file():
                self.sources['system-audio']='unavailable';self.system_error='helper-unavailable'
                s.stop_event.wait(5);continue
            generation=self.generation
            process=None;data=bytearray();diagnostic=bytearray();last=time.monotonic()
            try:
                process=subprocess.Popen([str(helper)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,bufsize=0)
                self.process=process;self.system_error=None
                while self.enabled and generation==self.generation and not s.stop_event.is_set() and process.poll() is None:
                    readable=select.select([process.stdout,process.stderr],[],[],.2)[0]
                    for stream in readable:
                        chunk=stream.read(65536)
                        if not chunk:continue
                        if stream is process.stdout:
                            last=time.monotonic();data.extend(chunk)
                            while len(data)>=32000:
                                self._save_system(bytes(data[:32000]),generation);del data[:32000]
                        else:
                            diagnostic.extend(chunk)
                            while b'\n' in diagnostic:
                                line,_,rest=diagnostic.partition(b'\n');diagnostic[:]=rest
                                if line.startswith(b'BUTLER_SYSTEM_AUDIO '):
                                    try:
                                        value=json.loads(line[20:])
                                        if value.get('state')=='unavailable':self.system_error=value.get('reason')
                                    except ValueError:pass
                            if len(diagnostic)>8192:diagnostic.clear()
                    if time.monotonic()-last>10:raise RuntimeError('system-audio-stalled')
                if self.enabled:self.sources['system-audio']='unavailable'
            except (OSError,RuntimeError):
                self.system_error='capture-unavailable';self.sources['system-audio']='unavailable'
            finally:
                data.clear();diagnostic.clear()
                if process is not None:
                    if process.poll() is None:process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                    process.stdout.close();process.stderr.close()
                if self.process is process:self.process=None
            s.stop_event.wait(3)

    def capture(self,batch):
        s=self.service;now=s.clock()
        if not self.permits(now):
            if self.enabled and self.storage_error:self.sources={key:'unavailable' for key in self.sources}
            return False
        if not 0 <= now-batch.end <= 2.5:return False
        saved=False
        if batch.frames and 'camera' not in batch.gaps and (batch.frame_at is None or 0 <= now-batch.frame_at <= 2.5):
            try:
                self.camera.capture(batch,now)
                self.sources['camera']='active';self.last['camera']=now;saved=True
                if self.camera.clip is None or not self.camera.clip['closing']:
                    self.write_result('camera',batch.start,batch.end)
            except (OSError,sqlite3.Error,ValueError) as error:
                self.write_result('camera',batch.start,batch.end,error)
                self.sources['camera']='unavailable'
        else:
            self.camera.finish()
            self.sources['camera']='unavailable'
        if batch.pcm and 'microphone' not in batch.gaps:
            try:
                s.store.media('microphone',batch.start,batch.end,batch.wav(),'wav',temporary=True)
                self.sources['microphone']='active';self.last['microphone']=now;saved=True
                self.write_result('microphone',batch.start,batch.end)
            except (OSError,sqlite3.Error) as error:
                self.write_result('microphone',batch.start,batch.end,error)
                self.sources['microphone']='unavailable'
        else:self.sources['microphone']='unavailable'
        if now-self.last_screen >= 60:
            self.last_screen=now
            s.screen_worker.submit(s.capture_screen,now,self.generation,archive=True)
        if now-self.last_analysis >= 60 and not s.store.get('analysis_paused',False):
            self.last_analysis=now
            try:s.pending.put_nowait(('archive',self.generation))
            except queue.Full:s.store.event('analysis','analysis-busy',now,now+60)
        return saved

    def snapshot(self):
        now=self.service.clock()
        states=dict(self.sources)
        if not self.enabled:states={key:'paused' for key in states}
        else:
            for key in ('camera','microphone','system-audio'):
                if states[key]=='active' and not 0 <= now-self.last.get(key,float('-inf')) <= 3:states[key]='unavailable'
        return {'enabled':self.enabled,'policy':'owner-enabled local passive archive; action identity separate',
                'sources':states,'last_capture':dict(self.last),'raw_retention_hours':24,'generation':self.generation,
                'storage_error':self.storage_error,'free_disk_bytes':self.free_bytes,'minimum_free_disk_bytes':256*1024*1024,
                'system_audio_error':self.system_error,'system_audio_dbfs':self.system_dbfs}
