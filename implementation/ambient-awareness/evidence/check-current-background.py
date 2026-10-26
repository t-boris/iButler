"""Whitelisted installed-runtime metadata while native Butler UI is background."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import subprocess
import time

import butler_ambient
from butler_ambient.mcp import rpc

assert '.openclaw/tools/ambient-awareness/.venv/' in butler_ambient.__file__
state=Path.home()/'.local/share/butler-ambient'
samples=[]
for _ in range(10):
    front=json.loads(subprocess.check_output(['/tmp/butler-background-probe']))
    s=rpc(state,'ui-state',{'preview':False});v=s['voice'];l=v['listening'];a=v['audio']
    samples.append({'foreground_metadata_available':front['foreground_metadata_available'],
        'butler_ui_foreground':front['butler_ui_foreground'],
        'microphone':v['input'],'camera_working':v['camera_input']['working'],
        'listening':{k:l[k] for k in ('enabled','phase','counts','timings')},
        'models':v['diagnostics']['readiness'],'dialogue_allowed':v['dialogue_allowed'],
        'participant':v['participant'],'native_playback':v.get('playback'),
        'archive_sources':s['archive']['sources'],'storage_error':s['archive'].get('storage_error'),
        'participant_check_error':s.get('participant_check_error'),
        'voice_processing_enabled':a.get('voice_processing_enabled'),
        'other_audio_ducking':a.get('other_audio_ducking'),
        'system_input':a.get('system_default_microphone'),'system_output':a.get('system_default_output')})
    time.sleep(1)
result={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'samples':samples,
    'actual_owner_spoken_conversation_verified':False,'raw_content_exported':False}
Path(__file__).with_name('current-background-live.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'samples':len(samples),'ui_background':sum(x['butler_ui_foreground'] is False for x in samples),
    'fresh_microphone':sum(x['microphone']['working'] for x in samples),
    'fresh_camera':sum(x['camera_working'] for x in samples),
    'first_listener_batches':samples[0]['listening']['counts'].get('batches'),
    'last_listener_batches':samples[-1]['listening']['counts'].get('batches'),
    'last_listener_counts':samples[-1]['listening']['counts'],'last_readiness':samples[-1]['models'],
    'source':samples[-1]['microphone']['name'],'raw_content_exported':False}))
