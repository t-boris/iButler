"""Owner Settings, scoped schedule controls and reversible integration adapters."""
import datetime as dt
import json
import re
import tempfile
import threading
import types
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
        self.real_backburner = Schedules.backburner
        patcher = patch.object(Schedules,"backburner",return_value=[])     # cron tests stay about cron; Back Burner has its own below
        self.burner = patcher.start()
        self.addCleanup(patcher.stop)
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

    def folder(self,**values):
        row={"name":"Sirius Art","slug":"sirius-art-c09dd7","status":"active","priority":3,"runs_total":0,"last_run_at":None,"last_status":"",
             "failures":0,"last_reason":"","waiting":"","next_run_at":"2026-10-23T00:30:00-05:00","window":"00:30-06:00","zone":"America/Chicago",
             "model":"claude-opus-5-5"}
        row.update(values)
        return row

    def burner_row(self,**values):
        self.burner.return_value=[self.folder(**values)]
        return next(r for r in self.manager.snapshot()["jobs"] if r["kind"]=="backburner")

    def test_back_burner_folders_are_listed_with_next_run_model_and_schedule(self):
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()):
            row=self.burner_row(runs_total=4,last_run_at="2026-10-22T00:40:00-05:00",last_status="applied")
        self.assertEqual((row["id"],row["name"],row["enabled"],row["running"],row["status"]),
                         ("backburner:sirius-art-c09dd7","Back Burner · Sirius Art",True,False,"ok"))
        self.assertEqual(row["schedule"],"каждую ночь 00:30–06:00 · America/Chicago · приоритет 3")
        self.assertEqual(row["model"],"claude-opus-5-5 · прогонов: 4")
        self.assertEqual(row["next_run"],dt.datetime(2026,10,23,0,30,tzinfo=ZONE).timestamp())
        self.assertEqual(row["last_run"],dt.datetime(2026,10,22,0,40,tzinfo=ZONE).timestamp())
        self.assertEqual(row["error"],"")

    def test_back_burner_status_tells_paused_done_waiting_failed_and_new_apart(self):
        cases=[({"status":"paused","waiting":"status is paused","next_run_at":None},"paused",False,""),
               ({"status":"done","waiting":"status is done","next_run_at":None},"paused",False,"Исследование отмечено завершённым."),
               ({"status":"","waiting":"status is missing in the brief","next_run_at":None},"paused",False,"В брифе не указан status: папка не запускается."),
               ({"waiting":"3 runs without progress; waiting for your edits","next_run_at":None,"runs_total":3},"scheduled",True,
                "Папка ждёт ваших правок: несколько прогонов подряд без прогресса."),
               ({"last_status":"failed","failures":2,"runs_total":1,"last_reason":"worker timed out"},"error",True,"Последний запуск: worker timed out"),
               ({"runs_total":0},"ready",True,"")]
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()):
            for values,status,enabled,error in cases:
                row=self.burner_row(**values)
                self.assertEqual((row["status"],row["enabled"],row["error"]),(status,enabled,error),values)
                self.assertEqual(row["next_run"] is None,values.get("next_run_at",1) is None,values)

    def test_a_skipped_last_run_shows_its_reason_and_a_long_one_is_cut(self):
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()):
            row=self.burner_row(runs_total=2,last_status="applied",last_reason="x"*900)
        self.assertEqual(row["status"],"ok")
        self.assertEqual(row["error"],"Последний запуск: "+"x"*300)

    def test_back_burner_failure_does_not_hide_cron_jobs_and_cron_failure_does_not_hide_folders(self):
        with patch("butler_ambient.schedules.command",return_value=self.cron_response()):
            self.burner.side_effect=ValueError("Butler CLI request failed")
            result=self.manager.snapshot()
        self.assertEqual([r["id"] for r in result["jobs"] if r["kind"]=="cron"],["ours"])
        self.assertEqual(result["error"],"Не удалось получить папки Back Burner.")
        self.burner.side_effect=None;self.burner.return_value=[self.folder()]
        with patch("butler_ambient.schedules.command",side_effect=ValueError("down")):
            result=self.manager.snapshot()
        self.assertEqual([r["kind"] for r in result["jobs"] if r["kind"] in {"cron","backburner"}],["backburner"])
        self.assertEqual(result["error"],"Не удалось получить задания OpenClaw. Локальные задания показаны отдельно.")

    def test_the_folder_list_comes_from_the_workspace_cli_as_json(self):
        rows=[self.folder(),self.folder(name="MyDataCenter",slug="mydatacenter-1332ab")]
        with patch("butler_ambient.schedules.command",return_value=json.dumps(rows)) as cli:
            self.assertEqual(self.real_backburner(self.manager),rows)
        self.assertEqual(cli.call_args.args,(str(self.workspace/"bin/butler-backburner"),"list","--json"))

    def test_back_burner_pause_and_resume_go_through_the_harness_cli_by_name(self):
        self.burner.return_value=[self.folder()]
        said={"pause":"Папка на паузе: ночной цикл её пропустит.","resume":"Папка включена: ночной цикл возьмёт её.",
              "stop":"Папка на паузе. Уже идущий прогон дойдёт до конца: он оформляет результат целиком."}
        for action,verb in (("pause","pause"),("resume","resume"),("stop","pause")):
            with patch("butler_ambient.schedules.command",return_value="") as cli:
                result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":action})
            self.assertEqual(result,{"ok":True,"message":said[action]})
            self.assertEqual(cli.call_args.args,(str(self.workspace/"bin/butler-backburner"),verb,"Sirius Art"))
            self.assertEqual(cli.call_count,1)

    def core(self,answer=None,fail=None,reason="no"):
        """A stand-in for the workspace's butler_core_client module: `fail` is "unavailable" or "refused"."""
        class Unavailable(ConnectionError):pass
        class Refused(RuntimeError):pass
        client=Mock()
        if fail:client.execute=Mock(side_effect={"unavailable":Unavailable("down"),"refused":Refused(reason)}[fail])
        else:client.execute=Mock(return_value=answer or {"state":"running"})
        return types.SimpleNamespace(Client=lambda:client,CoreUnavailable=Unavailable,CoreError=Refused),client

    def test_back_burner_run_starts_one_folder_as_a_tracked_core_operation(self):
        self.burner.return_value=[self.folder()]
        core,client=self.core()
        with patch("butler_ambient.schedules.core_client",return_value=core),patch("butler_ambient.schedules.command") as cli:
            result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
        self.assertEqual(result["ok"],True)
        self.assertIn("«Sirius Art» запущен в ядре",result["message"])
        args,kwargs=client.execute.call_args
        self.assertEqual(args[:2],("backburner.cycle","butler"))
        self.assertRegex(args[2],r"^backburner:[0-9a-f]{12}$")          # a hash of the name: the core takes ids only
        cycle=args[3]["cycle_id"]
        self.assertRegex(cycle,r"^bb-\d{8}-\d{6}-[a-f0-9]{6}$")
        self.assertEqual(args[3],{"cycle_id":cycle,"only":"Sirius Art"})
        self.assertEqual((args[4],kwargs["principal"]),("backburner:manual:"+cycle,"voice:owner"))
        cli.assert_not_called()

    def test_the_operation_target_is_valid_for_the_core_whatever_the_folder_is_called(self):
        """butler-core accepts a target only as an id (contracts/__init__.py: ID); a name with a space or Cyrillic is refused."""
        core_id=re.compile(r"[A-Za-z0-9][A-Za-z0-9:._@/+=-]{0,199}")
        targets={}
        for name in ("Sirius Art","Дата-центр","Дом <b>;rm","x"*80):
            self.burner.return_value=[self.folder(name=name,slug="slug-"+name)]
            core,client=self.core()
            with patch("butler_ambient.schedules.core_client",return_value=core):
                result=self.manager.control({"id":"backburner:slug-"+name,"action":"run"})
            self.assertEqual(result["ok"],True,msg=name)
            args=client.execute.call_args.args
            self.assertIsNotNone(core_id.fullmatch(args[2]),msg=("target",name,args[2]))
            self.assertIsNotNone(core_id.fullmatch(args[4]),msg=("key",name,args[4]))
            self.assertEqual(args[3]["only"],name)                      # the folder itself travels in the parameters
            targets[name]=args[2]
        self.assertEqual(len(set(targets.values())),4)                  # still one target per folder

    def test_every_press_is_its_own_operation_so_a_second_run_is_not_swallowed_as_a_duplicate(self):
        self.burner.return_value=[self.folder()]
        core,client=self.core()
        with patch("butler_ambient.schedules.core_client",return_value=core):
            for _ in range(2):self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
        keys=[call.args[4] for call in client.execute.call_args_list]
        self.assertEqual(len(set(keys)),2)

    def test_back_burner_run_says_why_it_did_not_start(self):
        self.burner.return_value=[self.folder()]
        terminal='./bin/butler-backburner run "Sirius Art"'
        with patch("butler_ambient.schedules.core_client",return_value=None):
            result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
        self.assertEqual(result["ok"],False);self.assertIn("не установлено",result["message"]);self.assertIn(terminal,result["message"])
        for fail,text in (("unavailable","недоступно"),("refused","не приняло запуск: no")):
            core,client=self.core(fail=fail)
            with patch("butler_ambient.schedules.core_client",return_value=core):
                result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
            self.assertEqual(result["ok"],False);self.assertIn(text,result["message"]);self.assertIn(terminal,result["message"])
        core,client=self.core(answer={"state":"denied","reason":"category and recipient are not approved "+"x"*300})
        with patch("butler_ambient.schedules.core_client",return_value=core):
            result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
        self.assertEqual(result["ok"],False);self.assertIn("не запустило прогон: category and recipient",result["message"]);self.assertLess(len(result["message"]),200)

    def test_back_burner_run_is_refused_for_a_folder_that_is_not_active_and_the_core_is_not_asked(self):
        for status,said in (("paused","Папка на паузе"),("done","отмечена завершённой"),("","без статуса в брифе")):
            self.burner.return_value=[self.folder(status=status)]
            core,client=self.core()
            with patch("butler_ambient.schedules.core_client",return_value=core):
                result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
            self.assertEqual(result["ok"],False,msg=status)
            self.assertIn(said,result["message"],msg=status)
            self.assertIn("«Продолжить»",result["message"],msg=status)
            client.execute.assert_not_called()

    def test_a_refusal_from_the_core_is_shown_with_its_reason_but_cut_short(self):
        self.burner.return_value=[self.folder()]
        core,client=self.core(fail="refused",reason="OperationError: invalid operation: target "+"x"*400)
        with patch("butler_ambient.schedules.core_client",return_value=core):
            result=self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"run"})
        self.assertEqual(result["ok"],False)
        self.assertIn("не приняло запуск: OperationError: invalid operation: target",result["message"])
        self.assertLess(len(result["message"]),260)
        self.assertIn('./bin/butler-backburner run "Sirius Art"',result["message"])

    def test_back_burner_update_is_not_offered(self):
        self.burner.return_value=[self.folder()]
        with patch("butler_ambient.schedules.command") as cli:
            with self.assertRaises(ValueError):self.manager.control({"id":"backburner:sirius-art-c09dd7","action":"update","cron":"0 1 * * *"})
        cli.assert_not_called()

    def test_only_listed_back_burner_folders_can_be_controlled(self):
        self.burner.return_value=[self.folder()]
        with patch("butler_ambient.schedules.command") as cli:
            for key in ("backburner:nope","backburner:","backburner:../x"):
                with self.assertRaises(ValueError,msg=key):self.manager.control({"id":key,"action":"pause"})
        cli.assert_not_called()

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
