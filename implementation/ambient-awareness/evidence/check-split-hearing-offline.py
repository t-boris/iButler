"""Actual local Whisper; synthetic PCM only; no sound or owner identity claim."""
from pathlib import Path
import datetime as dt
import io,json,time,wave,tempfile
from unittest.mock import Mock
import sys
ROOT=Path(__file__).resolve().parents[1]
if '--installed' not in sys.argv:sys.path.insert(0,str(ROOT))
import butler_ambient
if '--installed' in sys.argv:assert '.openclaw/tools/ambient-awareness/.venv/' in butler_ambient.__file__
from butler_ambient.transcriber import ResidentWhisper
from butler_ambient.conversation import Conversation,hearing_vocative,hearing_check
from butler_ambient.listening import Listening
from butler_ambient.service import Service
from butler_ambient.store import Store

config=json.loads((Path.home()/'.local/share/butler-ambient/config.json').read_text())
def wav(data):
    output=io.BytesIO()
    with wave.open(output,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(data)
    return output.getvalue()
resident=ResidentWhisper(config['whisper_model'],config.get('whisper_cli','whisper-cli'),'ru')
report={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'synthetic':True,'actual_owner_test':False,'audio_played':False,'raw_text_or_media_exported':False,'cuts':[],'installed_package':'--installed' in sys.argv}
try:
    resident.warm();client=resident.client();selected=None
    for fixture,cuts in (('tests/fixtures/wake-bata.wav',(.35,.4,.45,.5,.55,.6)),('butler_ambient/assets/voice-check.wav',(.5,.6,.7,.8,1.,1.2))):
        with wave.open(str(ROOT/fixture),'rb') as stream:
            assert (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())==(1,2,16000)
            pcm=stream.readframes(stream.getnframes())
        whole=client.transcribe(wav(pcm))
        whole_matches=hearing_check(whole,['Бата','Батлер','Butler'])
        for seconds in cuts:
            cut=int(seconds*16000)*2
            # Native capture provides surrounding quiet PCM as well. Padding
            # avoids treating a cropped phoneme as an entire real utterance.
            first_pcm=b'\0'*16000+pcm[:cut]+b'\0'*16000
            second_pcm=b'\0'*16000+pcm[cut:]+b'\0'*16000
            before=time.monotonic();first=client.transcribe(wav(first_pcm));second=client.transcribe(wav(second_pcm))
            alias=hearing_vocative(first,['Бата','Батлер','Butler'])
            matched=bool(alias and hearing_check(alias+' '+second,['Бата','Батлер','Butler']))
            item={'fixture':fixture,'whole_is_exact_hearing_check':whole_matches,'cut_seconds':seconds,'asr_ms':round((time.monotonic()-before)*1000,1),'first_is_exact_vocative':bool(alias),'full_join_is_exact_hearing_check':matched}
            report['cuts'].append(item);print(json.dumps(item),flush=True)
            if matched:selected=(first,second,first_pcm,second_pcm);break
        if selected:break
    if selected:
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp));now=[100.];s=Service(store,Mock(audio_status={},playback={'state':'idle'}),Mock(),Mock(),clock=lambda:now[0],transport=Mock())
            s.voice=Conversation(s);s.listening=Listening(s);listener=s.listening
            store.set('continuous_archive',True);store.set('voice_enabled',True);store.set('voice_profile','baritone');store.set('face_wake_dialogue',True);s.gate.enable(True,now[0])
            # The ASR inputs/results above are real; this second stage tests
            # routing without speaking at night or claiming microphone input.
            listener.model=Mock();listener.model.transcribe.side_effect=selected[:2]
            s.disclosures.play_fixed=Mock(return_value=True)
            listener.process(selected[2],listener.token,now[0],False,time.monotonic())
            now[0]+=2
            listener.process(selected[3],listener.token,now[0],False,time.monotonic())
            report['fixed_public_route_calls']=s.disclosures.play_fixed.call_count
            report['private_authority_granted']=s.voice_gate.permits(now[0])
            report['private_jobs_queued']=not s.voice.jobs.empty()
            report['passed']=s.disclosures.play_fixed.call_count==1 and not report['private_authority_granted'] and not report['private_jobs_queued']
            s.stop();store.close()
    else:report['passed']=False
finally:
    resident.stop()
    (ROOT/('evidence/split-hearing-installed-offline.json' if '--installed' in sys.argv else 'evidence/split-hearing-resident-offline.json')).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='cuts'}),flush=True)
