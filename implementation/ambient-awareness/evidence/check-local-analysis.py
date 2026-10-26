"""Private local integration probe; writes aggregate metadata, never observations."""
from pathlib import Path
from unittest.mock import Mock
import datetime,json,sqlite3,tempfile,time,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from butler_ambient.analysis import LocalAnalyzer
from butler_ambient.service import Service
from butler_ambient.store import Store
state=Path.home()/'.local/share/butler-ambient';config=json.loads((state/'config.json').read_text())
db=sqlite3.connect(f'file:{state}/ambient.sqlite3?mode=ro',uri=True)
rows=db.execute("SELECT source,start,end,path FROM media WHERE path IS NOT NULL AND source='camera' ORDER BY end DESC LIMIT 5").fetchall()
previous=db.execute('SELECT * FROM observations ORDER BY end DESC LIMIT 40').fetchall();identities=db.execute('SELECT * FROM identities').fetchall();db.close()
with tempfile.TemporaryDirectory(prefix='private-analysis-',dir=state) as tmp:
 store=Store(Path(tmp));analyzer=LocalAnalyzer(config['vision_model'])
 service=Service(store,Mock(),Mock(),analyzer);store.set('continuous_archive',True);service.gate.enable(True,time.time())
 for row in previous:store.execute('INSERT INTO observations VALUES (?,?,?,?,?,?)',row)
 for row in identities:store.execute('INSERT INTO identities VALUES (?,?,?,?)',row)
 ids=[store.media(source,start,end,Path(path).read_bytes(),'mjpeg') for source,start,end,path in rows]
 started=time.monotonic()
 try:
  ok=service.analyze_group(ids,service.archive.generation,archive=True)
  report={'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'ok':ok,'error':service.analysis_error,
   'diagnostic':service.analysis_diagnostic,'model_diagnostic':analyzer.diagnostic,'seconds':round(time.monotonic()-started,3),
   'input_images':len(rows),'context_observations_before_bound':len(previous),'raw_content_exported':False,
   'temporary_private_copy_deleted':True,'scope':'Real local model/context integration in an isolated private store; not an installed background-analysis pass'}
 finally:service.stop();store.close()
Path(__file__).with_name('ambient-model-context-live.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
