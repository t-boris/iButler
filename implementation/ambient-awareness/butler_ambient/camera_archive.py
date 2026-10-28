"""Continuous bounded camera clips and explicit sampled visual provenance."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def sample_count(row):
    return len(json.loads(row['samples'])) if row.get('samples') else 1


def read_samples(row):
    data=Path(row['path']).read_bytes()
    samples=json.loads(row['samples']) if row.get('samples') else [{'offset':0,'at':row['start']}]
    if not 1<=len(samples)<=3:raise ValueError('invalid camera sample count')
    frames=[];times=[];last=-1
    for sample in samples:
        offset,at=sample['offset'],sample['at']
        if type(offset) is not int or not last<offset<len(data) or not row['start']<=at<=row['end']:
            raise ValueError('invalid camera sample provenance')
        end=data.find(b'\xff\xd9',offset)
        # Old records predate sample metadata. Their first image is the
        # representative; the local model still validates the actual image.
        frame=data[offset:end+2] if end>=0 else data[offset:]
        if not frame:raise ValueError('empty camera sample')
        frames.append(frame);times.append(at);last=offset
    return frames,times


class CameraClips:
    MAX_SECONDS=10

    def __init__(self,archive):
        self.archive=archive
        self.clip=None

    @staticmethod
    def signature(frame):
        # Only a tiny, transient change score. No identity/semantic inference,
        # network request or saved image is produced by this selection step.
        import cv2
        import numpy as np
        image=cv2.imdecode(np.frombuffer(frame,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
        return cv2.resize(image,(32,18)).astype('float32') if image is not None else None

    def finish(self):
        clip=self.clip
        if clip is None:return True
        # Never keep visual signatures across a gap or Pause, even if closing
        # metadata fails. Refuse further appends until closing can be retried.
        clip['reference']=None;clip['closing']=True
        try:self.archive.service.store.finish_camera(clip['id'])
        except (OSError,sqlite3.Error,ValueError) as error:
            self.archive.write_result('camera',clip['start'],clip['end'],error)
            return False
        self.clip=None
        return True

    def finalize_stale(self,now):
        if self.clip is not None and (self.clip['closing'] or now-self.clip['arrival']>1.5):
            return self.finish()
        return True

    def capture(self,batch,now):
        store=self.archive.service.store
        frames=self.archive.service.recording_frames(batch.frames)
        times=list(batch.frame_times) if len(batch.frame_times)==len(frames) else [
            batch.start+(index+1)*(batch.end-batch.start)/len(frames) for index in range(len(frames))]
        start=min(batch.start,*times);end=max(batch.end,*times)
        clip=self.clip
        # A strict permission transition closes the old retention segment;
        # passive archive capture can continue under its existing policy.
        permission = bool(self.archive.service.gate.permits(now))
        if clip is not None and clip.get('retention_permission', permission) != permission:
            if not self.finish():raise OSError('camera clip could not close')
            clip=None
        if clip is not None and (clip['closing'] or abs(batch.start-clip['audio_end'])>.25
                or end-clip['start']>self.MAX_SECONDS or batch.end<=clip['audio_end']):
            if not self.finish():raise OSError('camera clip could not close')
            clip=None
        if end-start>self.MAX_SECONDS:raise OSError('camera batch exceeds clip limit')
        # Prepare the next metadata without mutating the committed state. A
        # quota/SQLite failure must leave both its bytes and samples unchanged.
        candidate=dict(clip) if clip else {'start':start,'size':0,'reference':None,'choices':{},'closing':False}
        choices=dict(candidate['choices']);offset=candidate['size']
        for frame,at in zip(frames,times):
            signature=self.signature(frame)
            sample={'offset':offset,'at':at}
            if not choices:
                choices[0]=(0.,sample);candidate['reference']=signature
            else:
                reference=candidate['reference']
                score=float(abs(signature-reference).mean()) if signature is not None and reference is not None else 0.
                half=1 if at-candidate['start']<self.MAX_SECONDS/2 else 2
                if score>=4. and score>choices.get(half,(-1.,None))[0]:choices[half]=(score,sample)
            offset+=len(frame)
        samples=sorted((value[1] for value in choices.values()),key=lambda x:x['offset'])
        data=b''.join(frames)
        try:
            if clip:store.append_camera(clip['id'],data,end,samples)
            else:
                candidate['id']=store.media('camera',start,end,data,'mjpeg',temporary=True,complete=False,samples=samples)
                from .recordings import tag_capture
                tag_capture(self.archive.service,candidate['id'])
        except (OSError,sqlite3.Error):
            # Allow analysis to drain a valid prefix when its next append no
            # longer fits. The rejected interval is logged by the caller.
            if clip:self.finish()
            raise
        candidate.update(size=offset,choices=choices,end=end,audio_end=batch.end,arrival=now,retention_permission=permission)
        self.clip=candidate
        if end-candidate['start']>=self.MAX_SECONDS:self.finish()
