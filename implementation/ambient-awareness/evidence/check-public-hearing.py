"""Labelled synthetic speaker-to-mic check; export metadata, never captured speech."""
from pathlib import Path
import datetime as dt
import json
import subprocess
import sys
import time

import butler_ambient
from butler_ambient.conversation import explicit_address, hearing_check
from butler_ambient.mcp import rpc

state = Path.home()/'.local/share/butler-ambient'
fixture = Path(butler_ambient.__file__).parent/'assets/voice-check.wav'
assert '.openclaw/tools/ambient-awareness/.venv/' in str(fixture)
def read():return rpc(state,'ui-state',{'preview':False})
def foreground():
    result = json.loads(subprocess.check_output(['/tmp/butler-background-probe']))
    return {k:result[k] for k in ('foreground_metadata_available','butler_ui_foreground')}

before = read()
assert before['enabled'] and before['voice']['listening']['enabled']
assert before['voice']['owner_setup']['stage']=='idle'
assert before['voice']['input']['working']
started = time.time()
report = {'at':dt.datetime.now(dt.timezone.utc).isoformat(),
    'synthetic_acoustic_probe':True,'actual_owner_test':False,'fixture':fixture.name,
    'before_foreground':foreground(),'before_counts':before['voice']['listening']['counts'],
    'source':before['voice']['input']['name'],'output':before['voice']['audio']['system_default_output'],
    'global_devices_volume_or_biometrics_changed':False,'samples':[]}
internal = sys.argv[1:] == ['--internal-speaker']
report['probe_output'] = 'Mac Studio Speakers (only this test process)' if internal else report['output']
player = subprocess.Popen([*(['/tmp/butler-internal-acoustic'] if internal else ['/usr/bin/afplay']),str(fixture)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for _ in range(36):
    time.sleep(.5)
    s=read();v=s['voice'];l=v['listening']
    # The ephemeral text is examined only locally for a narrow diagnostic;
    # neither text nor waveform enters the retained report or tool output.
    text=l.get('last_heard','');aliases=['Бата','Батлер','Butler']
    report['samples'].append({'elapsed':round(time.time()-started,3),
        'fresh_microphone':v['input']['working'],'rms_dbfs':v['input']['rms_dbfs'],
        'counts':l['counts'],'phase':l['phase'],'explicit':explicit_address(text,aliases),
        'exact_hearing_check':hearing_check(text,aliases),
        'playback_state':v.get('playback',{}).get('state'),
        'public_completed':v['disclosure']['completed'],'public_error':v['disclosure']['error'],
        'dialogue_allowed':v['dialogue_allowed']})
report['player_exit']=player.wait(timeout=5)
report['after_foreground']=foreground()
report['public_reply_completed']=report['samples'][-1]['counts'].get('hearing-replied',0)>report['before_counts'].get('hearing-replied',0)
report['raw_content_exported']=False
name = 'public-hearing-installed-internal-acoustic.json' if internal else 'public-hearing-installed-acoustic.json'
Path(__file__).with_name(name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='samples'},ensure_ascii=False))
