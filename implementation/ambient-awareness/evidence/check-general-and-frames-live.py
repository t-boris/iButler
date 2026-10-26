"""Installed-runtime metadata for DEC-062/DEC-065/DEC-066: counters only.

Usage: <installed python> check-general-and-frames-live.py LABEL MINUTES

No transcript, reply, fact detail, frame or path is read into the report. It
records the general/private dialogue mode, anonymous turn outcomes, event-frame
counters, model roles and the spool backlog/observation growth over a window,
so background-analysis throughput can be compared before and after the
vision model switch.
"""
from pathlib import Path
import collections
import datetime as dt
import json
import sqlite3
import sys
import time

import butler_ambient
from butler_ambient.mcp import rpc

assert '.openclaw/tools/ambient-awareness/.venv/' in butler_ambient.__file__
label, minutes = sys.argv[1], float(sys.argv[2])
state = Path.home() / '.local/share/butler-ambient'
config = json.loads((state / 'config.json').read_text())


def database():
    db = sqlite3.connect(f'file:{state}/ambient.sqlite3?mode=ro', uri=True)
    now = time.time()
    observations = dict(db.execute('SELECT source,COUNT(*) FROM observations GROUP BY source').fetchall())
    backlog = {}
    for source, count, oldest in db.execute("""SELECT source,COUNT(*),MIN(start) FROM media
            WHERE storage_class='spool' AND complete=1 AND analyzed=0 GROUP BY source"""):
        backlog[source] = {'items': count, 'oldest_age_seconds': round(now - oldest, 1) if oldest else None}
    frames = db.execute("""SELECT COUNT(*) FROM media WHERE storage_class='event-frame'
            AND preserved IS NOT NULL""").fetchone()[0]
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_frames'").fetchone()
    kinds = dict(db.execute('SELECT kind,COUNT(*) FROM event_frames GROUP BY kind').fetchall()) if has_table else None
    db.close()
    return {'at': now, 'observations': observations, 'backlog': backlog,
            'event_frame_files': frames, 'event_frame_kinds': kinds}


def service():
    s = rpc(state, 'ui-state', {'preview': False})
    v = s['voice']; d = v['diagnostics']
    turns = collections.Counter((t.get('mode', 'private'), t.get('outcome'), t.get('rule'))
                                for t in d.get('turns', []))
    return {'dialogue_mode': v.get('dialogue_mode'), 'dialogue_allowed': v['dialogue_allowed'],
            'voice_ready': v['ready'], 'token_mode': d.get('mode'),
            'general': {k: d.get('general', {}).get(k) for k in ('permitted', 'active', 'context_turns', 'private_released')},
            'turn_outcomes': [{'mode': m, 'outcome': o, 'rule': r, 'count': c} for (m, o, r), c in sorted(turns.items(), key=str)],
            'listening_counts': v['listening']['counts'], 'listening_phase': v['listening']['phase'],
            'readiness': d.get('readiness'), 'event_frames': {k: s.get('storage', {}).get('event_frames', {}).get(k)
                for k in ('files', 'today', 'daily_limit', 'spacing_seconds', 'error')},
            'recording_allowed': s.get('recording_allowed')}


first = {'database': database(), 'service': service()}
time.sleep(minutes * 60)
last = {'database': database(), 'service': service()}
growth = {source: last['database']['observations'].get(source, 0) - first['database']['observations'].get(source, 0)
          for source in set(first['database']['observations']) | set(last['database']['observations'])}
report = {'label': label, 'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'window_minutes': minutes,
          'models': {'vision_model': config.get('vision_model'), 'conversation_model': config.get('conversation_model')},
          'first': first, 'last': last, 'observation_growth': growth,
          'raw_content_exported': False, 'actual_owner_spoken_conversation_verified': False}
target = Path(__file__).with_name(f'general-frames-live-{label}.json')
target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'label': label, 'models': report['models'], 'observation_growth': growth,
                  'backlog_last': last['database']['backlog'], 'dialogue_mode': last['service']['dialogue_mode'],
                  'event_frames': last['service']['event_frames'], 'turn_outcomes': last['service']['turn_outcomes']},
                 ensure_ascii=False))
