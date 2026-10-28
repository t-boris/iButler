"""Provider catalog regression: metadata-only protocols, new IDs and effective routes."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from butler_ambient import catalog, components, integrations


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'config').mkdir()
        self.now = time.time()
    def tearDown(self): self.tmp.cleanup()
    def cache(self, models=None, backend='codex', **extra):
        row = {'models':models or [{'id':'future-model','name':'Future','aliases':[]}], 'fetched_at':self.now,'attempted_at':self.now,**extra}
        (self.root/catalog.FILE).write_text(json.dumps({'version':1,'providers':{backend:row},'runtime':{'fetched_at':self.now,'models':['openai/'+m['id'] for m in row['models']]}}))
    def test_new_models_and_aliases_are_discovered_without_copying_auth_or_prompts(self):
        rows = catalog.normalized('claude',[{'value':'next','resolvedModel':'claude-next-99','displayName':'Next', 'auth':{'secret':'no'},'prompt':'never'}, {'value':'claude-next-99','resolvedModel':'claude-next-99','displayName':'Next'}])
        self.assertEqual(rows,[{'id':'claude-next-99','name':'Next','aliases':['next']}])
        self.assertNotIn('never',json.dumps(rows))
        with self.assertRaises(ValueError):catalog.normalized('codex',[{'model':'bad\nname'}])
        self.assertEqual(catalog.normalized('codex',[{'model':'new-visible'},{'model':'hidden','hidden':True}])[0]['id'],'new-visible')
    def test_success_failure_and_expiration_preserve_metadata_but_not_old_selection_lease(self):
        manager=catalog.Catalog(self.root,clock=lambda:self.now,fetch=lambda *_:[{'id':'new','name':'New','aliases':[]}])
        manager._refresh()
        self.assertEqual(catalog.provider_choices(self.root,'codex',now=self.now),['new'])
        self.assertEqual((self.root/catalog.FILE).stat().st_mode & 0o777,0o600)
        self.now += catalog.INTERVAL+1
        for error in (TimeoutError(),ValueError(),TypeError(),AttributeError()):
            manager.fetch=Mock(side_effect=error)
            manager._refresh()
            self.assertTrue(all(v['error']=='provider-unavailable' for v in manager.snapshot()['providers']))
        row=next(v for v in manager.snapshot()['providers'] if v['backend']=='codex')
        self.assertTrue(row['stale']);self.assertEqual(row['error'],'provider-unavailable')
        self.assertEqual(catalog.provider_choices(self.root,'codex',now=self.now),['new'])
        self.now += catalog.MAX_AGE
        self.assertEqual(catalog.provider_choices(self.root,'codex',now=self.now),[])
    def test_refresh_is_nonblocking_rate_limited_and_never_starts_twice(self):
        entered,release=threading.Event(),threading.Event()
        def fetch(*_):entered.set();release.wait(2);return [{'id':'next','name':'Next','aliases':[]}]
        manager=catalog.Catalog(self.root,clock=lambda:self.now,fetch=fetch)
        self.assertTrue(manager.refresh());self.assertTrue(entered.wait(1))
        self.assertTrue(manager.snapshot()['refreshing']);self.assertFalse(manager.refresh(force=True))
        release.set()
        deadline=time.monotonic()+2
        while manager.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(manager.busy);self.assertFalse(manager.refresh());manager.stop();self.assertFalse(manager.refresh(force=True))
    def test_cache_rejects_symlinks_and_strips_unrecognized_data(self):
        self.cache();data=json.loads((self.root/catalog.FILE).read_text());data['providers']['codex']['auth']='private';(self.root/catalog.FILE).write_text(json.dumps(data))
        self.assertNotIn('private',json.dumps(catalog.read_cache(self.root)))
        (self.root/catalog.FILE).unlink();target=self.root/'elsewhere';target.write_text('{"private":true}');(self.root/catalog.FILE).symlink_to(target)
        self.assertEqual(catalog.read_cache(self.root),{})
    def test_runtime_catalog_is_scoped_to_butler_and_filters_unavailable_models(self):
        def response(args,**_):
            self.assertEqual(args[:5],['openclaw','models','--agent','butler','list'])
            provider=args[args.index('--provider')+1]
            return Mock(stdout=json.dumps({'models':[{'key':provider+'/new','available':True,'auth':'discard'}, {'key':provider+'/blocked','available':False}, {'key':'other/no','available':True}]}).encode())
        with patch.object(catalog.subprocess,'run',side_effect=response):
            self.assertEqual(catalog.discover_runtime('openclaw'),['openai/new','anthropic/new'])
    def test_new_cli_id_is_not_offered_to_an_incompatible_runtime(self):
        self.cache()
        path=self.root/catalog.FILE;value=json.loads(path.read_text());value['runtime']['models']=['openai/older'];path.write_text(json.dumps(value))
        self.assertIn('future-model',catalog.provider_choices(self.root,'codex'))
        self.assertNotIn('openai/future-model',catalog.agent_choices(self.root,['openai/older']))
    def test_removed_id_disappears_and_agent_choices_use_existing_provider_only(self):
        self.cache()
        self.assertEqual(catalog.agent_choices(self.root,['openai/obsolete','other/kept']),['other/kept','openai/future-model'])
        self.cache([{'id':'newer','name':'Newer','aliases':[]}])
        self.assertNotIn('future-model',catalog.provider_choices(self.root,'codex'))
        self.assertNotIn('anthropic/newer',catalog.agent_choices(self.root,['openai/current']))
    def fake_cli(self, backend, body):
        path=self.root/backend
        path.write_text('#!'+sys.executable+'\n'+body)
        path.chmod(0o700)
        return str(path)
    def test_codex_handshake_paginates_without_threads_or_inference(self):
        sent=self.root/'requests.jsonl'
        cli=self.fake_cli('codex',f'''import json,sys
for line in sys.stdin:
 value=json.loads(line)
 with open({str(sent)!r},'a') as out:out.write(line)
 if value.get('id')==1:print(json.dumps({{'id':1,'result':{{}}}}),flush=True)
 elif value.get('method')=='model/list':
  second='cursor' in value['params']
  print(json.dumps({{'id':value['id'],'result':{{'data':[{{'model':'future-2' if second else 'future-1'}}],'nextCursor':None if second else 'page2'}}}}),flush=True)
''')
        rows=catalog.discover('codex',cli)
        self.assertEqual([r['id'] for r in rows],['future-1','future-2'])
        requests=[json.loads(x) for x in sent.read_text().splitlines()]
        self.assertEqual([v['method'] for v in requests],['initialize','initialized','model/list','model/list'])
    def test_claude_only_initializes_and_drops_account_metadata(self):
        cli=self.fake_cli('claude','''import json,sys
value=json.loads(sys.stdin.readline())
assert value['type']=='control_request' and value['request']['subtype']=='initialize'
print(json.dumps({'type':'control_response','response':{'request_id':value['request_id'],'subtype':'success','response':{'models':[{'value':'future','resolvedModel':'claude-future-99','displayName':'Future'}],'account':{'secret':'never'}}}}),flush=True)
sys.stdin.read()
''')
        self.assertEqual(catalog.discover('claude',cli),[{'id':'claude-future-99','name':'Future','aliases':['future']}])
    def test_protocol_response_is_bounded_and_child_is_reaped(self):
        cli=self.fake_cli('codex',"import sys,time\nsys.stdout.write('x'*4000001);sys.stdout.flush();time.sleep(30)\n")
        with self.assertRaisesRegex(ValueError,'exceeds limit'):catalog.discover('codex',cli)
    def dispatch_source(self):
        return '''import json,shutil
from pathlib import Path
WORKSPACE=Path(ROOT)
CLAUDE_BIN='claude'
CODEX_BIN_CANDIDATES=['codex']
ROUTES={'code':{'backend':'codex','model':'legacy','timeout':7200,'fallback':('claude','legacy-claude')}}
DEFAULT_TIMEOUT = 1800
COST_GUARD_UNCHANGED=True
'''
    def test_new_catalog_model_is_effective_and_removed_model_is_rejected(self):
        self.cache()
        path=self.root/'config/ai-settings.json'
        path.write_text(json.dumps({'routes':{'code':{'backend':'codex','model':'future-model'}}}))
        source=integrations.adapted(self.dispatch_source(),'dispatch');namespace={'ROOT':str(self.root)}
        exec(source,namespace)
        self.assertEqual(namespace['ROUTES']['code']['model'],'future-model')
        self.assertEqual(namespace['ROUTES']['code']['timeout'],7200)
        self.assertEqual(namespace['ROUTES']['code']['fallback'],('claude','legacy-claude'))
        self.assertTrue(namespace['COST_GUARD_UNCHANGED'])
        self.cache([{'id':'replacement','name':'Replacement','aliases':[]}])
        with self.assertRaises(RuntimeError):exec(source,{'ROOT':str(self.root)})
    def test_first_provider_outage_does_not_disable_existing_default_routes(self):
        self.cache(backend='codex')
        path=self.root/catalog.FILE;value=json.loads(path.read_text());value['providers']['codex']['fetched_at']=0;value['providers']['codex']['models']=[];path.write_text(json.dumps(value))
        (self.root/'config/ai-settings.json').write_text(json.dumps({'routes':{'code':{'backend':'codex','model':'legacy'}}}))
        namespace={'ROOT':str(self.root)}
        exec(integrations.adapted(self.dispatch_source(),'dispatch'),namespace)
        self.assertEqual(namespace['ROUTES']['code']['model'],'legacy')
    def test_legacy_adapter_migrates_only_its_owned_block(self):
        original=self.dispatch_source().replace('DEFAULT_TIMEOUT = 1800\n','DEFAULT_TIMEOUT = 1800\n\n'+integrations.LEGACY_DISPATCH)
        updated=integrations.adapted(original,'dispatch')
        self.assertIn(integrations.DISPATCH,updated)
        self.assertEqual(updated.replace(integrations.DISPATCH,''),original.replace(integrations.LEGACY_DISPATCH,''))
        self.assertEqual(integrations.adapted(updated,'dispatch'),updated)
    def test_dynamic_route_save_targets_only_selected_route(self):
        (self.root/'lib').mkdir();(self.root/'lib/dispatch.py').write_text(integrations.adapted(self.dispatch_source(),'dispatch'))
        row={'id':'route:code','model':'codex:legacy','executable':'codex','choices':['codex:future-model']}
        with patch.object(components,'snapshot',return_value={'components':[row]}),patch.object(components.local_settings,'executable',return_value='/synthetic/codex'):
            components.update(Mock(),{'id':'route:code','model':'codex:future-model'},self.root)
            with self.assertRaises(ValueError):components.update(Mock(),{'id':'route:code','model':'codex:invented'},self.root)
        data=json.loads((self.root/'config/ai-settings.json').read_text())
        self.assertEqual(data['routes'],{'code':{'backend':'codex','model':'future-model'}})
    def test_confined_route_accepts_only_claude_models_and_a_refusal_writes_nothing(self):
        (self.root/'lib').mkdir()
        confined="ROUTES={'backburner':{'backend':'claude','model':'claude-opus-5-5','timeout':2700,'silent':True,'tools':['Read','Edit']},'code':"
        source=self.dispatch_source().replace("ROUTES={'code':",confined)
        (self.root/'lib/dispatch.py').write_text(integrations.adapted(source,'dispatch'))
        row={'id':'route:backburner','model':'claude:claude-opus-5-5','executable':'claude','choices':['codex:future-model','claude:claude-future']}
        with patch.object(components,'snapshot',return_value={'components':[row]}),patch.object(components.local_settings,'executable',return_value='/synthetic/claude'):
            with self.assertRaisesRegex(ValueError,'только на claude'):components.update(Mock(),{'id':'route:backburner','model':'codex:future-model'},self.root)
            self.assertFalse((self.root/'config/ai-settings.json').exists())
            components.update(Mock(),{'id':'route:backburner','model':'claude:claude-future'},self.root)
        data=json.loads((self.root/'config/ai-settings.json').read_text())
        self.assertEqual(data['routes'],{'backburner':{'backend':'claude','model':'claude-future'}})
    def test_unconfined_route_may_still_use_codex(self):
        (self.root/'lib').mkdir()
        confined="ROUTES={'backburner':{'backend':'claude','model':'claude-opus-5-5','timeout':2700,'tools':['Read']},'code':"
        (self.root/'lib/dispatch.py').write_text(integrations.adapted(self.dispatch_source().replace("ROUTES={'code':",confined),'dispatch'))
        row={'id':'route:code','model':'claude:x','executable':'claude','choices':['codex:future-model']}
        with patch.object(components,'snapshot',return_value={'components':[row]}),patch.object(components.local_settings,'executable',return_value='/synthetic/codex'):
            components.update(Mock(),{'id':'route:code','model':'codex:future-model'},self.root)
        self.assertEqual(json.loads((self.root/'config/ai-settings.json').read_text())['routes'],{'code':{'backend':'codex','model':'future-model'}})
    def test_new_agent_model_changes_only_butler_primary(self):
        self.cache()
        service=Mock();service.catalog=object()
        row={'id':'agent','model':'openai/old','executable':'openclaw'}
        agents=[{'id':'other','model':'keep'},{'id':'butler','model':{'primary':'openai/old','fallbacks':['kept']}}]
        with patch.object(components,'snapshot',return_value={'components':[row]}),patch.object(components,'agent_status',return_value={'allowed':['openai/old']}),patch.object(components.local_settings,'executable',return_value='openclaw'),patch.object(components,'command',side_effect=[json.dumps(agents),'{}']) as cli:
            components.update(service,{'id':'agent','model':'openai/future-model'},self.root)
        self.assertEqual(cli.call_args.args,('openclaw','config','set','agents.list.1.model.primary','"openai/future-model"','--strict-json'))
