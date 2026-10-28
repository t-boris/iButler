import tempfile
import time
import unittest
from unittest.mock import Mock
from pathlib import Path
from types import SimpleNamespace
from butler_ambient.store import Store
from butler_ambient.display import Display
from butler_ambient.display import CardRefused
from butler_ambient.panel_chat import PanelChat
from test_panel_chat import FakeBrain, answered

class RetainedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.service=SimpleNamespace(store=self.store,config={},clock=lambda:time.time(),voice=None,owner_name=lambda:'Борис',
                                     gate=Mock(),dialogue_gate=Mock())

    def test_display_survives_real_database_reopen_and_does_not_prune(self):
        display=Display(self.service)
        ids=[display.present({'kind':'text','markdown':str(i)})['id'] for i in range(85)]
        display.select(ids[3]);display.clear('service-stopped');display.clear('owner-paused')
        self.service.store=Store(self.root)
        restored=Display(self.service)
        self.assertEqual([r['id'] for r in restored.snapshot()['cards']],ids)
        self.assertEqual(restored.current,ids[3])
        restored.clear('owner')
        self.assertEqual(Display(self.service).snapshot()['cards'],[])

    def test_replaced_card_is_durable_and_keeps_its_original_position(self):
        display=Display(self.service)
        a=display.present({'kind':'text','markdown':'first'})['id']
        b=display.present({'kind':'text','markdown':'second'})['id']
        display.present({'kind':'text','markdown':'updated','replaces':a})
        restored=Display(self.service).snapshot()['cards']
        self.assertEqual([r['id'] for r in restored],[a,b]);self.assertEqual(restored[0]['markdown'],'updated')

    def test_persisted_display_stays_hidden_and_cannot_be_selected_without_owner(self):
        display=Display(self.service)
        key=display.present({'kind':'text','markdown':'private retained card'})['id']
        self.service.dialogue_gate.permits.return_value=False
        restored=Display(self.service)
        self.assertEqual(restored.snapshot()['cards'],[])
        with self.assertRaises(CardRefused):restored.select(key)
        self.assertEqual(len(self.store.execute("SELECT * FROM local_ui_history WHERE namespace='display'")),1)
        self.service.dialogue_gate.permits.return_value=True
        self.assertEqual(restored.snapshot()['cards'][0]['id'],key)

    def test_email_body_and_draft_are_not_duplicated_in_history_database(self):
        display=Display(self.service)
        display.email_card=lambda key:{'title':'Mail','email':{'key':key,'sender':'Sender','body':'PRIVATE_BODY','draft':'PRIVATE_DRAFT'}}
        display.present({'kind':'email','key':'mail-key'})
        payload=self.store.execute("SELECT payload FROM local_ui_history WHERE namespace='display'")[0]['payload']
        self.assertNotIn('PRIVATE_BODY',payload);self.assertNotIn('PRIVATE_DRAFT',payload);self.assertIn('mail-key',payload)

    def test_access_expiring_while_materializing_history_withholds_content(self):
        display=Display(self.service)
        display.present({'kind':'text','markdown':'private retained card'})
        self.service.dialogue_gate.permits.side_effect=[True,False]
        result=display.snapshot()
        self.assertEqual(result['cards'],[]);self.assertEqual(result['hidden'],'private')

    def test_written_thread_survives_stop_days_and_restart_until_clear(self):
        brain=FakeBrain(lambda text,on_text,token:'answer')
        chat=PanelChat(self.service,brain=brain,follower=False)
        turn=chat.send('question')['id'];self.assertTrue(answered(chat,turn));chat.stop()
        self.service.store=Store(self.root);self.service.clock=lambda:time.time()+365*86400
        restored=PanelChat(self.service,brain=brain,follower=False);self.addCleanup(restored.stop)
        self.assertEqual([r['text'] for r in restored.snapshot()['entries']],['question','answer'])
        restored.clear('consent-revoked');self.assertEqual(len(restored.snapshot()['entries']),2)
        restored.clear('owner');self.assertEqual(PanelChat(self.service,brain=brain,follower=False).snapshot()['entries'],[])

    def test_interrupted_turn_is_not_reexecuted_on_restore(self):
        from butler_ambient.local_history import LocalHistory
        journal=LocalHistory(self.store,'panel-chat')
        journal.put({'id':'a','turn':'a','at':1,'who':'owner','text':'request','state':'sent'})
        journal.put({'id':'a.r','turn':'a','at':1,'who':'butler','text':'partial','state':'streaming'})
        brain=FakeBrain(lambda *args:self.fail('must not replay'))
        chat=PanelChat(self.service,brain=brain,follower=False);self.addCleanup(chat.stop)
        self.assertEqual(chat.snapshot()['entries'][1]['state'],'failed');self.assertFalse(chat.snapshot()['busy'])
