"""Local-only existing-model throughput experiment; retained output is numeric."""
from pathlib import Path
import sqlite3,json,time
from butler_ambient.analysis import LocalAnalyzer,inference_status

root=Path.home()/'.local/share/butler-ambient'
db=sqlite3.connect(f'file:{root}/ambient.sqlite3?mode=ro',uri=True);db.row_factory=sqlite3.Row
rows=[dict(r) for r in db.execute("SELECT path FROM media WHERE source='camera' AND path IS NOT NULL ORDER BY end DESC LIMIT 16")]
context=[{**dict(r),'facts':json.loads(r['facts'])} for r in db.execute('SELECT id AS observation_id,source,start,end,facts FROM observations ORDER BY end DESC LIMIT 6')]
db.close()
images=[]
for row in rows:
    data=Path(row['path']).read_bytes();start=data.find(b'\xff\xd8');end=data.find(b'\xff\xd9',start)
    if start>=0 and end>start:images.append(data[start:end+2])
assert len(images)==16
model=LocalAnalyzer('gemma3:4b');results=[]
for count in (5,12,16):
    started=time.monotonic()
    try:
        facts=model.analyze('camera',images=images[:count],context=context)
        result={'images':count,'seconds':round(time.monotonic()-started,3),'facts_count':len(facts),'diagnostic':inference_status(model.diagnostic)}
        result['records_per_second']=round(count/result['seconds'],3)
    except Exception as error:
        result={'images':count,'error_type':type(error).__name__,'diagnostic':inference_status(model.diagnostic)}
    results.append(result)
    Path(__file__).with_name('camera-batch-benchmark.json').write_text(json.dumps({'local_only':True,'raw_content_exported':False,'no_model_device_or_authority_change':True,'results':results},indent=2)+'\n')
    print(json.dumps(result),flush=True)
