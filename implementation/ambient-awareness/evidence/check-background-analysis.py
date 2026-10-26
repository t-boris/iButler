"""Aggregate installed capture/analysis evidence without reading source contents."""
from pathlib import Path
import datetime,hashlib,json,time,sqlite3,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from butler_ambient.mcp import rpc
state=Path.home()/'.local/share/butler-ambient';started=time.time();samples=[]
def read(sql,args=()):
 db=sqlite3.connect(f'file:{state}/ambient.sqlite3?mode=ro',uri=True)
 try:return db.execute(sql,args).fetchall()
 finally:db.close()
base=read('SELECT COALESCE(MAX(rowid),0) FROM observations')[0][0]
verified=hashlib.sha256(Path(__file__).with_name('results.json').read_bytes()).hexdigest()
for _ in range(45):
 s=rpc(state,'ui-state',{'preview':False})
 new={source:{'count':count,'latest':last} for source,count,last in read('SELECT source,COUNT(*),MAX(end) FROM observations WHERE rowid>? GROUP BY source',(base,))}
 samples.append({'analysis_error':s.get('analysis_error'),'analysis_diagnostic':s.get('analysis_diagnostic'),'new_observations':new,
  'archive_sources':s.get('archive',{}).get('sources'),'meeting_quiet':s['voice'].get('meeting',{}).get('quiet'),
  'disclosure':s['voice'].get('disclosure'),'voice_ready':s['voice'].get('ready')})
 time.sleep(2)
r={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'seconds':round(time.time()-started,2),'samples':len(samples),
 'verification_sha256':verified,'baseline_row':base,'last':samples[-1],'quiet_samples':sum(x['meeting_quiet'] for x in samples),
 'analysis_error_samples':sum(x['analysis_error'] is not None for x in samples),'private_content_exported':False}
Path(__file__).with_name(sys.argv[1] if len(sys.argv)>1 else 'continuous-analysis-context-fix-live.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False))
