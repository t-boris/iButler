"""DEC-035 lifetime is hard-bounded and cannot authorize owner actions."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
from butler_ambient.participants import Participants
from butler_ambient.disclosure import Disclosures
from butler_ambient.service import Service
from butler_ambient.store import Store


class ParticipantTests(unittest.TestCase):
    def test_expiry_is_hard_not_extended_by_continuous_presence(self):
        p=Participants()
        for now in (0,1,2,30,119):p.observe('face',[[1,0]],now)
        self.assertEqual(len(p.pending(119)),1)
        self.assertEqual(p.pending(120),[])
        self.assertFalse(p.entries)

    def test_new_people_deduplicate_and_departure_deletes_vectors(self):
        p=Participants()
        for now in (10,11,12):p.observe('face',[[1,0],[0,1]],now)
        ids=p.pending(12);self.assertEqual(len(ids),2)
        p.acknowledge(ids,12)
        p.observe('face',[[1,0],[0,1]],13);self.assertEqual(p.pending(13),[])
        p.observe('face',[],14);self.assertFalse(p.entries)
        p.observe('face',[[1,0]],15);self.assertEqual(p.snapshot(15)['unknown_faces'],1)
        self.assertEqual(p.snapshot(15)['pending_disclosures'],0)
        self.assertNotIn('vector',json.dumps(p.snapshot(15)))

    def test_reused_sample_zero_invalid_and_pause_generation_cannot_supply_identity(self):
        p=Participants()
        for _ in range(4):p.observe('voice',[[1,0]],10)
        self.assertEqual(p.pending(10),[])
        epoch=p.generation;p.clear();p.observe('voice',[[1,0]],11,generation=epoch)
        self.assertFalse(p.entries)
        p.observe('voice',[[0,0],[float('nan'),0]],12)
        self.assertFalse(p.entries)


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.now=100.
        self.tracker=Participants()
        self.s=Service(self.store,Mock(audio_status={}),Mock(participants=self.tracker),Mock(),clock=lambda:self.now,transport=Mock())
        self.s.gate.enable(True,self.now);self.store.set('voice_enabled',True);self.store.set('voice_profile','baritone');self.store.set('continuous_archive',True)
        self.s.voice=SimpleNamespace(busy=False,ready=True,output_lock=threading.Lock(),cancel=Mock(),stop=Mock())
        for _ in range(3):
            self.now+=1;self.tracker.observe('face',[[1,0]],self.now)
        self.d=self.s.disclosures
        cache=self.store.root/'voice/prepared/baritone';cache.mkdir(parents=True);(cache/'disclosure-recording.wav').write_bytes(b'synthetic fixed cache')
        self.s.inputs.play.side_effect=lambda path,key:setattr(self.s.inputs,'playback',{'state':'done','id':key})
        self.s.inputs.playback={'state':'idle'}

    def tearDown(self):
        self.s.stop();self.store.close();self.temp.cleanup()

    def test_unknown_can_hear_only_fixed_disclosure_with_no_private_gate_or_dialogue_access(self):
        self.assertFalse(self.s.gate.permits(self.now))
        self.assertTrue(self.d.attempt())
        self.assertTrue(str(self.s.inputs.play.call_args.args[0]).endswith('disclosure-recording.wav'))
        self.assertFalse(self.d.attempt())
        self.assertEqual(self.d.completed,1)
        self.assertEqual(self.store.execute('SELECT * FROM observations'),[])
        self.assertEqual(self.store.execute('SELECT * FROM agreements'),[])
        self.assertNotIn('vector',json.dumps(self.d.snapshot()))

    def test_meeting_pause_busy_and_missing_cache_never_play(self):
        self.s.control('preferences',{'meeting_mode':'quiet'});self.assertFalse(self.d.attempt())
        self.s.control('preferences',{'meeting_mode':'available'})
        self.s.voice.busy=True;self.assertFalse(self.d.attempt());self.s.voice.busy=False
        (self.store.root/'voice/prepared/baritone/disclosure-recording.wav').unlink();self.assertFalse(self.d.attempt())
        self.s.inputs.play.assert_not_called()
        self.s.control('enabled',{'value':False});self.assertFalse(self.tracker.entries)

    def test_revocation_erases_fingerprints_and_owner_revocation_stops_archive(self):
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.s.control('revoke',{'id':'owner'})
        self.assertFalse(self.tracker.entries)
        self.assertFalse(self.s.archive.enabled)
        self.assertFalse(self.store.get('enabled'))

    def test_playing_media_voice_alone_does_not_trigger_a_newcomer_greeting(self):
        self.tracker.clear()
        for _ in range(3):
            self.now+=1;self.tracker.observe('voice',[[1,0]],self.now)
        self.s.archive.system_dbfs=-20;self.s.archive.last['system-audio']=self.now
        self.assertFalse(self.d.attempt())
        self.assertEqual(self.d.state,'media-audio')
        self.s.inputs.play.assert_not_called()


if __name__=='__main__':unittest.main()
