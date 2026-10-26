"""Anonymous participant distinction only; never an authentication source.

Vectors are volatile, hard-expire at120s from creation (not a sliding expiry),
are discarded on departure/Pause/revocation and are absent from all snapshots.
"""
from __future__ import annotations
import math
import threading
import uuid


class Participants:
    TTL=120
    LIMIT=8

    def __init__(self):
        self.entries={}
        self.lock=threading.RLock()
        self.last_time=float('-inf')
        self.generation=0

    def clear(self):
        with self.lock:self.entries.clear();self.generation+=1

    def expire(self, now):
        with self.lock:
            if now<self.last_time:self.entries.clear()
            self.last_time=now
            self.entries={key:e for key,e in self.entries.items() if 0<=now-e['created']<self.TTL}

    def observe(self, kind, vectors, now, *, generation=None):
        if kind not in {'face','voice'}:raise ValueError('participant modality')
        with self.lock:
            if generation is not None and generation!=self.generation:return
            self.expire(now)
            seen=set()
            for vector in list(vectors)[:self.LIMIT]:
                values=tuple(float(x) for x in vector)
                if not 1<=len(values)<=512 or not all(math.isfinite(x) for x in values):continue
                norm=math.sqrt(sum(x*x for x in values))
                if norm<=0:continue
                values=tuple(x/norm for x in values)
                candidates=[(sum(x*y for x,y in zip(values,e['vector'])),key) for key,e in self.entries.items()
                    if e['kind']==kind and len(e['vector'])==len(values)]
                score,key=max(candidates,default=(-1,None))
                if score<(.65 if kind=='face' else .82):
                    if sum(e['kind']==kind for e in self.entries.values())>=self.LIMIT:continue
                    key=uuid.uuid4().hex
                    self.entries[key]={'kind':kind,'vector':values,'created':now,'last':now,'checks':0,'announced':False}
                entry=self.entries[key]
                if key not in seen and (entry['checks']==0 or now>entry['last']):entry['checks']+=1
                entry['last']=now;seen.add(key)
            # Absence is not retained as a long-lived identity or encounter log.
            self.entries={key:e for key,e in self.entries.items() if e['kind']!=kind or key in seen}

    def pending(self, now):
        with self.lock:
            self.expire(now)
            return [key for key,e in self.entries.items() if not e['announced'] and e['checks']>=3 and 0<=now-e['last']<=2.5]

    def acknowledge(self, keys, now):
        with self.lock:
            self.expire(now)
            for key in keys:
                if key in self.entries:self.entries[key]['announced']=True

    def snapshot(self, now):
        with self.lock:
            self.expire(now)
            return {'unknown_faces':sum(e['kind']=='face' for e in self.entries.values()),
                    'unknown_voices':sum(e['kind']=='voice' for e in self.entries.values()),
                    'pending_disclosures':len(self.pending(now)), 'volatile_only':True,'max_lifetime_seconds':self.TTL}
