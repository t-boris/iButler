"""Installed storage migration/continuity evidence; metadata only."""
from pathlib import Path
import datetime as dt
import json,sqlite3,time
import butler_ambient
from butler_ambient.mcp import rpc

assert '.openclaw/tools/ambient-awareness/.venv/' in butler_ambient.__file__
root=Path.home()/'.local/share/butler-ambient'
def query(sql,args=()):
    db=sqlite3.connect(f'file:{root}/ambient.sqlite3?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:return [dict(row) for row in db.execute(sql,args)]
    finally:db.close()
baseline=query('SELECT COALESCE(MAX(rowid),0) AS row FROM observations')[0]['row']
started=time.monotonic();samples=[]
for _ in range(30):
    now=time.monotonic();s=rpc(root,'ui-state',{'preview':False});v=s['voice']
    samples.append({'elapsed':round(time.monotonic()-started,3),'rpc_ms':round((time.monotonic()-now)*1000,1),
        'storage':s['storage'],'microphone_fresh':v['input']['working'],'camera_fresh':v['camera_input']['working'],
        'listener_batches':v['listening']['counts'].get('batches'),
        'models':v['diagnostics']['readiness'],'capture_sources':s['archive']['sources'],
        'capture_error':s['archive']['storage_error'],'participant_check_error':s.get('participant_check_error'),
        'analysis_error':s.get('analysis_error'),
        'new_observations':query('SELECT source,COUNT(*) AS count,MAX(end) AS last FROM observations WHERE rowid>? GROUP BY source',(baseline,)),
        'new_spool':query("SELECT source,analyzed,COUNT(*) AS files FROM media WHERE storage_class='spool' GROUP BY source,analyzed")})
    time.sleep(1)
result={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'samples':samples,
    'raw_content_exported':False,'actual_owner_dialogue_verified':False}
Path(__file__).with_name('storage-installed-live.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'samples':len(samples),'fresh_microphone':sum(s['microphone_fresh'] for s in samples),
    'fresh_camera':sum(s['camera_fresh'] for s in samples),
    'max_rpc_ms':max(s['rpc_ms'] for s in samples),'last_storage':samples[-1]['storage'],
    'last_new_observations':samples[-1]['new_observations'],'last_spool_analysis_counts':samples[-1]['new_spool'],
    'capture_errors':sum(s['capture_error'] is not None for s in samples),
    'analysis_errors':sum(s['analysis_error'] is not None for s in samples)},ensure_ascii=False))
