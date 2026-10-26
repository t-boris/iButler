"""A failed fresh source must retain bounded scheduled retries, not spin."""
from pathlib import Path
import json,tempfile
from unittest.mock import Mock,patch
from butler_ambient.service import Service
from butler_ambient.sensing import Batch
from butler_ambient.store import Store

with tempfile.TemporaryDirectory() as folder, patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10)):
    store=Store(Path(folder));analyzer=Mock();analyzer.transcribe.return_value='Synthetic fixture'
    def analyze(source,**kwargs):
        if source=='camera':raise RuntimeError('injected source failure')
        return []
    analyzer.analyze.side_effect=analyze
    service=Service(store,Mock(),Mock(),analyzer,clock=lambda:1000.,transport=Mock())
    store.set('continuous_archive',True);service.control('enabled',{'value':True})
    try:
        store.media('camera',999,1000,b'\xff\xd8C\xff\xd9','mjpeg',temporary=True)
        store.media('microphone',800,801,Batch(800,801,[],b'\0'*32000).wav(),'wav')
        service.analysis_tick(service.archive.generation,archive=True)
        result={'immediate_retry_queued':not service.pending.empty(),
            'other_source_completed':bool(store.execute("SELECT id FROM media WHERE source='microphone' AND analyzed=1")),
            'synthetic_only':True}
        print(json.dumps(result));assert not result['immediate_retry_queued'] and result['other_source_completed']
    finally:service.stop();store.close()
