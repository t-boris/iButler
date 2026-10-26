"""Read-only installed continuity/throughput evidence; no media or text exported."""
from pathlib import Path
import datetime as dt
import json,sqlite3,time
import butler_ambient
from butler_ambient.mcp import rpc

assert '.openclaw/tools/ambient-awareness/.venv/' in butler_ambient.__file__
root=Path.home()/'.local/share/butler-ambient'
output=Path(__file__).with_name('camera-clips-installed-live.json')
def query(sql,args=()):
    db=sqlite3.connect(f'file:{root}/ambient.sqlite3?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:return [dict(row) for row in db.execute(sql,args)]
    finally:db.close()
baseline=query('SELECT COALESCE(MAX(rowid),0) AS last FROM media')[0]['last']
started=time.monotonic();samples=[]
for index in range(181):
    now=time.time();before=time.monotonic();s=rpc(root,'ui-state',{'preview':index%5==0});v=s['voice']
    sample={'elapsed':round(time.monotonic()-started,3),'rpc_ms':round((time.monotonic()-before)*1000,1),
        'microphone_fresh':v['input']['working'],'camera_fresh':v['camera_input']['working'],
        'listener_batches':v['listening']['counts'].get('batches'),
        'analysis_error':s.get('analysis_error'),'capture_error':s['archive']['storage_error'],
        'participant_check_error':s.get('participant_check_error'),
        'spool_bytes':s['storage']['spool_bytes'],'spool_limit_bytes':s['storage']['spool_limit_bytes'],
        'collecting':s['storage']['collecting'],'preview_fresh':now-(s.get('preview') or {}).get('captured_at',0)<2.5 if index%5==0 else None,
        'new_media':query('SELECT source,analyzed,complete,COUNT(*) AS files,ROUND(MAX(end-start),3) AS max_duration,ROUND(?-MIN(end),1) AS oldest_age,MAX(COALESCE(json_array_length(samples),0)) AS selected_frames FROM media WHERE rowid>? GROUP BY source,analyzed,complete',(now,baseline))}
    samples.append(sample)
    if index%30==0:
        output.write_text(json.dumps({'at':dt.datetime.now(dt.timezone.utc).isoformat(),'samples':samples,'raw_content_exported':False,'actual_owner_dialogue_verified':False},indent=2)+'\n')
        print(json.dumps({'elapsed':sample['elapsed'],'samples':len(samples),'fresh_mic':sum(x['microphone_fresh'] for x in samples),'fresh_camera':sum(x['camera_fresh'] for x in samples),'new_media':sample['new_media']}),flush=True)
    time.sleep(max(0,1-(time.monotonic()-before)))
finished=time.time()
eligible=query('SELECT source,COUNT(*) AS files,SUM(analyzed=1) AS analyzed,SUM(analyzed=1 AND path IS NULL) AS reclaimed FROM media WHERE rowid>? AND complete=1 AND end<=? GROUP BY source',(baseline,finished-90))
summary={'samples':len(samples),'fresh_microphone':sum(x['microphone_fresh'] for x in samples),'fresh_camera':sum(x['camera_fresh'] for x in samples),
    'max_rpc_ms':max(x['rpc_ms'] for x in samples),'all_spool_bounded':all(x['spool_bytes']<=x['spool_limit_bytes'] for x in samples),
    'fresh_previews':sum(x['preview_fresh'] is True for x in samples),'requested_previews':sum(x['preview_fresh'] is not None for x in samples),
    'eligible_older_than_90s':eligible,'errors':{key:sum(x[key] is not None for x in samples) for key in ('analysis_error','capture_error','participant_check_error')}}
result={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'summary':summary,'samples':samples,'raw_content_exported':False,'actual_owner_dialogue_verified':False}
output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(summary),flush=True)
