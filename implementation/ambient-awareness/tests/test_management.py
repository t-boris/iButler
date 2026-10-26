"""Owner Settings, scoped schedule controls and reversible integration adapters."""
import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient import components, integrations
from butler_ambient.delivery import ZONE
from butler_ambient.policy import Evidence
from butler_ambient.schedules import Schedules
from butler_ambient.service import Service
from butler_ambient.store import Store


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root/"workspace"
        (self.workspace/"lib").mkdir(parents=True)
        (self.workspace/"config").mkdir()
        self.store = Store(self.root/"ambient")
        self.now = dt.datetime(2026,10,22,12,tzinfo=ZONE).timestamp()
        self.service = Service(self.store,Mock(),Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.manager = Schedules(self.service,self.workspace)
        self.service.schedules = self.manager
        self.job = {"id":"ours","agentId":"butler","enabled":True,"sessionTarget":"isolated",
                    "name":"Daily","schedule":{"kind":"cron","expr":"0 9 * * *","tz":"America/Chicago"},
                    "state":{},"payload":{"message":"private prompt must not be exported","model":"approved"}}

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def permit(self):
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True,self.now)
        for _ in range(3):
            self.now+=.1
            self.service.gate.check(Evidence(self.now,True,True,frozenset({'owner'}),True),self.now)
        self.service.analysis_token=threading.Event()

    def cron_response(self):
        return json.dumps({"jobs":[self.job,{"id":"other","agentId":"other","payload":{"message":"other private"}}]})

    def test_jobs_export_only_butler_metadata_without_payloads(self):
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()) as cli:
            result=self.manager.snapshot()
        text=json.dumps(result)
        self.assertNotIn("private",text)
        self.assertNotIn('"id": "other"',text)
        self.assertEqual(result['jobs'][0]['model'],'approved')
        self.assertIn('--agent',cli.call_args.args)
        self.assertIn('butler',cli.call_args.args)

    def test_pause_schedule_does_not_abort_current_run_or_other_agents(self):
        self.job['state']['runningAtMs']=1000
        with patch("butler_ambient.schedules.command",side_effect=[self.cron_response(),'{}']) as cli:
            self.manager.control({'id':'ours','action':'pause'})
        self.assertEqual(cli.call_args.args[1:4],('cron','disable','ours'))
        self.assertEqual(cli.call_count,2)

    def test_run_reports_only_an_actual_queue_receipt_and_never_retries(self):
        for receipt,expected in [({'ok':True,'enqueued':True},True),({'ok':True,'ran':False,'reason':'already-running'},False)]:
            with patch('butler_ambient.schedules.command',side_effect=[self.cron_response(),json.dumps(receipt)]) as cli:
                result=self.manager.control({'id':'ours','action':'run'})
            self.assertEqual(result['ok'],expected)
            self.assertEqual(cli.call_count,2)

    def test_other_agents_and_unknown_jobs_cannot_be_controlled(self):
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()) as cli:
            with self.assertRaises(ValueError):self.manager.control({'id':'other','action':'stop'})
        self.assertEqual(cli.call_count,1)

    def test_stop_aborts_latest_isolated_run_key_only(self):
        self.job['state']['runningAtMs']=1000
        sessions={'sessions':[{'key':'agent:butler:cron:ours:run:old','updatedAt':10},
                              {'key':'agent:butler:cron:ours:run:new','updatedAt':20},
                              {'key':'agent:other:cron:ours:run:else','updatedAt':30}]}
        with patch("butler_ambient.schedules.command",side_effect=[self.cron_response(),'{}',json.dumps(sessions),'{}']) as cli:
            self.manager.control({'id':'ours','action':'stop'})
        args=cli.call_args.args
        self.assertIn('sessions.abort',args)
        params=json.loads(args[args.index('--params')+1])
        self.assertEqual(params,{'key':'agent:butler:cron:ours:run:new','agentId':'butler'})
        self.assertFalse(any('restart' in c.args for c in cli.call_args_list))

    def test_main_session_stop_only_disables_schedule(self):
        self.job.update(sessionTarget='main',state={'runningAtMs':1000})
        with patch("butler_ambient.schedules.command",side_effect=[self.cron_response(),'{}']) as cli:
            result=self.manager.control({'id':'ours','action':'stop'})
        self.assertEqual(cli.call_count,2)
        self.assertIn('не прерывается',result['message'])

    def test_builtin_pause_survives_restart_and_stop_cancels_only_analysis(self):
        self.manager.control({'id':'builtin:analysis','action':'pause'})
        self.assertTrue(self.store.get('analysis_paused'))
        self.service.analyzer.cancel.assert_not_called()
        self.manager.control({'id':'builtin:analysis','action':'stop'})
        self.assertTrue(self.service.analysis_run_token.is_set())
        self.service.analyzer.cancel.assert_called_once()
        self.service.inputs.stop.assert_not_called()
        restarted=Schedules(self.service,self.workspace)
        with patch.object(restarted,'cron_jobs',return_value=[]):
            row=next(r for r in restarted.snapshot()['jobs'] if r['id']=='builtin:analysis')
        self.assertFalse(row['enabled'])
        self.assertIsNone(row['next_run'])
        self.manager.control({'id':'builtin:analysis','action':'resume'})
        self.assertFalse(self.store.get('analysis_paused'))

    def test_manual_run_is_gated_and_advice_does_not_bypass_limits(self):
        with self.assertRaises(ValueError):self.manager.control({'id':'builtin:analysis','action':'run'})
        self.permit()
        self.manager.control({'id':'builtin:analysis','action':'run'})
        self.assertEqual(self.service.pending.get_nowait(),('owner-request',self.service.gate.generation))
        with self.assertRaises(ValueError):self.manager.control({'id':'builtin:advice','action':'run'})
        self.service.delivery.transport.send.assert_not_called()

    def test_stop_discards_late_analysis_results_without_changing_permission(self):
        self.permit()
        mid=self.store.media('camera',self.now-1,self.now,b'fake','jpg')
        token=threading.Event()
        def finish(*args,**kwargs):
            token.set()
            return []
        self.service.analyzer.analyze.side_effect=finish
        self.assertFalse(self.service.analyze_group([mid],self.service.gate.generation,token))
        self.assertEqual(self.store.execute('SELECT * FROM observations'),[])
        self.assertTrue(self.service.gate.permits(self.now))

    def test_agent_model_update_targets_only_butler_primary_preserving_fallbacks(self):
        row={'id':'agent','model':'old','executable':'openclaw','model_key':'agent_model','cli_key':'openclaw_cli'}
        agents=[{'id':'other','model':'untouched'},{'id':'butler','model':{'primary':'old','fallbacks':['kept']}}]
        with patch.object(components,'snapshot',return_value={'components':[row]}), \
             patch.object(components,'agent_status',return_value={'allowed':['approved']}), \
             patch.object(components.local_settings,'executable',return_value='openclaw'), \
             patch.object(components,'command',side_effect=[json.dumps(agents),'{}']) as cli:
            components.update(self.service,{'id':'agent','model':'approved'},self.workspace)
        self.assertEqual(cli.call_args.args,('openclaw','config','set','agents.list.1.model.primary','"approved"','--strict-json'))
        self.assertEqual(agents[1]['model']['fallbacks'],['kept'])

    def test_agent_status_strips_authentication_and_credentials(self):
        with patch.object(components,'command',return_value=json.dumps({'resolvedDefault':'approved','allowed':['approved'],
                'auth':{'token':'never return'},'profiles':{'secret':'never return'}})):
            value=components.agent_status(self.service)
        self.assertEqual(set(value),{'model','allowed','cli'})
        self.assertNotIn('never return',json.dumps(value))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'lib').mkdir()
        (self.root/'config').mkdir()
        self.dispatch='''import json, shutil
from pathlib import Path
WORKSPACE = Path(ROOT)
CLAUDE_BIN = "claude"
CODEX_BIN_CANDIDATES = ["codex"]
ROUTES = {"code":{"backend":"claude","model":"approved-c","timeout":7200},"research":{"backend":"codex","model":"approved-x","fallback":("claude","approved-c")}}
DEFAULT_TIMEOUT = 1800
UNCHANGED_COST_GUARD = True
'''
        self.embedding='''import json, re, urllib.request
from pathlib import Path
WORKSPACE = Path(ROOT)
MODEL = "original"
DIM = 768
DB_PATH = WORKSPACE / "state" / "memory-index.db"
'''

    def tearDown(self):self.temp.cleanup()

    def test_route_adapter_applies_only_existing_approved_models_and_preserves_guards(self):
        (self.root/'config/ai-settings.json').write_text(json.dumps({'routes':{'code':{'backend':'codex','model':'approved-x'}}}))
        namespace={'ROOT':str(self.root)}
        value=integrations.adapted(self.dispatch,'dispatch')
        exec(compile(value,'fixture','exec'),namespace)
        self.assertEqual(namespace['ROUTES']['code'],{'backend':'codex','model':'approved-x','timeout':7200})
        self.assertEqual(namespace['ROUTES']['research']['fallback'],('claude','approved-c'))
        self.assertTrue(namespace['UNCHANGED_COST_GUARD'])
        self.assertEqual(integrations.adapted(value,'dispatch'),value)

    def test_invalid_route_cannot_silently_enable_a_new_backend(self):
        (self.root/'config/ai-settings.json').write_text(json.dumps({'routes':{'code':{'backend':'paid-api','model':'new'}}}))
        with self.assertRaises(RuntimeError):exec(integrations.adapted(self.dispatch,'dispatch'),{'ROOT':str(self.root)})

    def test_embedding_adapter_changes_stamp_and_forces_proxy_free_local_transport(self):
        (self.root/'config/ai-settings.json').write_text(json.dumps({'embedding_model':'new-local','embedding_dimension':1024}))
        namespace={'ROOT':str(self.root)}
        exec(integrations.adapted(self.embedding,'embedding'),namespace)
        self.assertEqual((namespace['MODEL'],namespace['DIM']),('new-local',1024))
        self.assertEqual(namespace['OLLAMA_URL'],'http://127.0.0.1:11434')
        self.assertFalse(any(type(h).__name__=='ProxyHandler' and h.proxies for h in namespace['_owner_local_opener'].handlers))

    def test_installer_backs_up_exact_originals_and_refuses_unexpected_structure(self):
        (self.root/'lib/dispatch.py').write_text(self.dispatch)
        (self.root/'lib/embeddings.py').write_text(self.embedding)
        rows=integrations.install(self.root)
        self.assertEqual(Path(rows[0]['backup']).read_text(),self.dispatch)
        self.assertEqual(Path(rows[0]['backup']).stat().st_mode & 0o777,0o600)
        self.assertTrue(all(r['changed'] for r in rows))
        self.assertFalse(any(r['changed'] for r in integrations.install(self.root)))
        with self.assertRaises(ValueError):integrations.adapted('unrelated = 1','dispatch')


if __name__=='__main__':unittest.main()
