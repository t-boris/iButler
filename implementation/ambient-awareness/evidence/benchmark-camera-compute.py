"""Isolate local inference briefly; always restore analysis and model defaults."""
from pathlib import Path
import sqlite3,json,time,sys
from butler_ambient.analysis import LocalAnalyzer,inference_status
from butler_ambient.mcp import rpc

root=Path.home()/'.local/share/butler-ambient'
db=sqlite3.connect(f'file:{root}/ambient.sqlite3?mode=ro',uri=True);db.row_factory=sqlite3.Row
setting=db.execute("SELECT value FROM settings WHERE key='analysis_paused'").fetchone()
assert setting is None or json.loads(setting[0]) is False, 'respect an existing owner pause'
rows=[dict(r) for r in db.execute("SELECT path FROM media WHERE source='camera' AND path IS NOT NULL ORDER BY end DESC LIMIT 16")]
context=[{**dict(r),'facts':json.loads(r['facts'])} for r in db.execute('SELECT id AS observation_id,source,start,end,facts FROM observations ORDER BY end DESC LIMIT 6')]
db.close()
images=[]
for row in rows:
    data=Path(row['path']).read_bytes();a=data.find(b'\xff\xd8');b=data.find(b'\xff\xd9',a)
    if a>=0 and b>a:images.append(data[a:b+2])
assert len(images)==16
qwen=sys.argv[1:]==['--qwen']
model=LocalAnalyzer('qwen3.5:9b' if qwen else 'gemma3:4b');generate=model.generate_json
results=[];started=time.monotonic()
report={'local_only':True,'raw_content_exported':False,'model':model.model,'results':results}
destination=Path(__file__).with_name('camera-qwen-benchmark.json' if qwen else 'camera-compute-benchmark.json')
def save():destination.write_text(json.dumps(report,indent=2)+'\n')
stopped=False
try:
    result=rpc(root,'ui-schedule-control',{'id':'builtin:analysis','action':'stop'})
    assert result.get('ok') is True;stopped=True;save()
    time.sleep(.5)
    for count,batch in (((5,512),(12,512),(16,512)) if qwen else ((5,512),(16,512),(16,2048))):
        def optimized(*args,**kwargs):
            kwargs['options']={**kwargs.get('options',{}),'num_batch':batch}
            return generate(*args,**kwargs)
        model.generate_json=optimized
        start=time.monotonic()
        try:
            facts=model.analyze('camera',images=images[:count],context=context)
            item={'images':count,'num_batch':batch,'seconds':round(time.monotonic()-start,3),
                  'facts_count':len(facts),'diagnostic':inference_status(model.diagnostic)}
            item['records_per_second']=round(count/item['seconds'],3)
        except Exception as error:item={'images':count,'num_batch':batch,'error_type':type(error).__name__,'diagnostic':inference_status(model.diagnostic)}
        results.append(item);save();print(json.dumps(item),flush=True)
finally:
    try:model.warm()
    finally:
        if stopped:
            result=rpc(root,'ui-schedule-control',{'id':'builtin:analysis','action':'resume'})
            report['analysis_resumed']=result.get('ok') is True
        report['experiment_seconds']=round(time.monotonic()-started,2);save()
        print(json.dumps({'analysis_resumed':report.get('analysis_resumed'),'seconds':report['experiment_seconds']}),flush=True)
