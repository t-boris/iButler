"""Synthetic real-store recovery grouping; no actual owner/media/authority used."""
from pathlib import Path
import io,json,tempfile,wave
from unittest.mock import Mock,patch
from butler_ambient.service import Service
from butler_ambient.sensing import Batch
from butler_ambient.store import Store

with tempfile.TemporaryDirectory() as folder, patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10)):
    store=Store(Path(folder));analyzer=Mock();analyzer.transcribe.return_value='Synthetic recovery fixture';analyzer.analyze.return_value=[]
    service=Service(store,Mock(),Mock(),analyzer,clock=lambda:1000.,transport=Mock())
    store.set('continuous_archive',True);service.control('enabled',{'value':True})
    try:
        for end in (999,1000,800,801):
            mid=store.media('microphone',end-1,end,Batch(end-1,end,[],b'\0'*32000).wav(),'wav',temporary=end>900)
            if end<900:store.execute("UPDATE media SET storage_class='legacy' WHERE id=?",(mid,))
        service.analysis_tick(service.archive.generation,archive=True)
        durations=[]
        for call in analyzer.transcribe.call_args_list:
            with wave.open(io.BytesIO(call.args[0]),'rb') as f:durations.append(f.getnframes()/f.getframerate())
        print(json.dumps({'new_then_legacy_seconds':durations,'expected':[2.,2.],'synthetic_only':True}))
        assert durations==[2.,2.], 'new/legacy boundary must reset aggregation timestamps'
    finally:service.stop();store.close()
