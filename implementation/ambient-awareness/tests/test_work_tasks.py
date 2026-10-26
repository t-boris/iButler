"""Actual dispatcher monitor with a harmless worker in an isolated project."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from butler_ambient.components import WORKSPACE
from butler_ambient.work_tasks import WorkTasks
from butler_ambient import processes, work_tasks
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.policy import Evidence
from unittest.mock import Mock


@unittest.skipUnless((WORKSPACE/'lib/dispatch.py').is_file(),'existing local Butler dispatcher required')
class WorkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()
        self.workspace=self.root/'workspace'; (self.workspace/'lib').mkdir(parents=True)
        (self.workspace/'lib/__init__.py').touch()
        self.project=self.root/'projects/Demo'; self.project.mkdir(parents=True)
        source=(WORKSPACE/'lib/dispatch.py').read_text()
        # Only test dependencies/roots/command are substituted. The actual monitor
        # and its notification sites run unchanged; no owner project or CLI runs.
        source=source.replace(str(WORKSPACE),str(self.workspace))
        source+='\nALLOWED_PROJECT_ROOTS = '+repr([str(self.project.parent)])+'\n'
        source+='''
def _build_command(backend, model, prompt, *, streaming):
    import sys
    if prompt == 'controlled failure':
        return [sys.executable, '-c', 'raise SystemExit(7)']
    if prompt == 'controlled wait':
        return [sys.executable, '-c', 'import time;print("controlled ready",flush=True);time.sleep(30)']
    return [sys.executable, '-c', 'print("controlled completion")']
'''
        (self.workspace/'lib/dispatch.py').write_text(source)
        (self.workspace/'lib/cost.py').write_text('''
from types import SimpleNamespace
def estimate(*a):return SimpleNamespace(estimated_usd=0)
def log(*a,**kw):raise AssertionError('no test billing or notifications')
''')
        self.tasks=WorkTasks(SimpleNamespace(config={}),self.workspace)
        self.keys=[]

    def tearDown(self):
        for key in self.keys:
            try:
                if self.tasks.status(key)['status']=='running':self.tasks.cancel(key)
            except (ValueError,OSError):pass
        self.temp.cleanup()

    def start(self, text):
        prepared=self.tasks.prepare({'project':'Demo','kind':'code','task':text})
        value=self.tasks.start(prepared); self.keys.append(value['id']);return value['id']

    def wait(self,key):
        until=time.monotonic()+8
        while time.monotonic()<until:
            value=self.tasks.status(key)
            if value['status']!='running':return value
            time.sleep(.05)
        self.fail('controlled worker did not finish')

    def test_real_monitor_completes_without_external_notifications_and_persists_receipt(self):
        started=time.monotonic();key=self.start('controlled success')
        self.assertLess(time.monotonic()-started,1)
        result=self.wait(key)
        self.assertEqual(result['status'],'done');self.assertEqual(result['exit_code'],0)
        self.assertGreater(result['output_bytes'],0)
        self.assertEqual(result['project'],'Demo')
        path=self.tasks.root/key
        self.assertEqual((path/'meta.json').stat().st_mode&0o777,0o600)
        self.assertFalse((self.workspace/'state/pending-wakes.jsonl').exists())
        self.assertNotIn('controlled success',json.dumps(self.tasks.snapshot()))

    def test_backend_failure_not_reported_as_success(self):
        result=self.wait(self.start('controlled failure'))
        self.assertEqual(result['status'],'failed');self.assertEqual(result['exit_code'],7)

    def test_in_flight_dispatcher_metadata_write_does_not_lose_a_live_job(self):
        key = self.start('controlled wait')
        # Wait for the real child to start, rather than truncating its initial
        # startup input before the monitor has read it.
        deadline = time.monotonic() + 4
        while b'controlled ready' not in (self.tasks.root / key / 'output.log').read_bytes():
            if time.monotonic() >= deadline: self.fail('controlled worker did not start')
            time.sleep(.01)
        path = self.tasks.root / key / 'meta.json'
        body = path.read_text()
        writing = threading.Event()
        def update():
            with path.open('w') as stream:
                stream.write('{'); stream.flush()
                writing.set()
                time.sleep(.025)
                stream.seek(0); stream.write(body); stream.truncate()
        writer = threading.Thread(target=update)
        writer.start()
        self.assertTrue(writing.wait(1))
        try:
            result = self.tasks.status(key)
            self.assertEqual(result['status'], 'running')
            self.assertEqual(result['id'], key)
        finally: writer.join()
        self.assertTrue(self.tasks.cancel(key)['ok'])

    def test_persistently_broken_receipt_is_unavailable_without_invented_completion(self):
        key = self.start('controlled wait')
        time.sleep(.15)
        path = self.tasks.root / key / 'voice-launch.json'
        body = path.read_text()
        try:
            path.write_text('{')
            with self.assertRaisesRegex(ValueError, 'временно недоступно'):
                self.tasks.status(key)
        finally: path.write_text(body)
        self.assertEqual(self.tasks.status(key)['status'], 'running')
        self.assertTrue(self.tasks.cancel(key)['ok'])

    def test_installed_package_does_not_put_third_party_modules_before_stdlib(self):
        # A real isolated installed layout reproduces the app-only failure that
        # source-checkout tests missed. No pip/network or provider CLI is used.
        environment = self.root / 'installed-env'
        processes.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True, timeout=15)
        site = environment / 'lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
        shutil.copytree(Path(work_tasks.__file__).parent, site / 'butler_ambient',
            ignore=shutil.ignore_patterns('__pycache__', 'native', 'assets'))
        (site / 'typing.py').write_text('raise RuntimeError("third-party typing shadowed stdlib")\n')
        script = '''
import json,sys,time
from types import SimpleNamespace
from butler_ambient.work_tasks import WorkTasks
tasks=WorkTasks(SimpleNamespace(config={}),sys.argv[1])
job=tasks.start(tasks.prepare({'project':'Demo','kind':'code','task':'controlled success'}))
deadline=time.monotonic()+6
while time.monotonic()<deadline:
    result=tasks.status(job['id'])
    if result['status']!='running':break
    time.sleep(.02)
print(json.dumps({'status':result['status'],'exit_code':result['exit_code']}))
'''
        env = dict(os.environ)
        env.pop('PYTHONPATH', None); env.pop('PYTHONHOME', None)
        result = processes.run([str(environment / 'bin/python'), '-c', script, str(self.workspace)],
            cwd=self.root, env=env, capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(result.stdout), {'status':'done', 'exit_code':0})

    def test_cancel_stops_only_exact_detached_worker(self):
        key=self.start('controlled wait')
        time.sleep(.3)
        self.assertTrue(self.tasks.cancel(key)['ok'])
        self.assertEqual(self.wait(key)['status'],'cancelled')
        self.assertFalse(self.tasks.cancel(key)['ok'])

    def test_missing_ambiguous_project_route_drift_and_arbitrary_pid_are_rejected(self):
        for project in ('missing','/etc','Demo/../elsewhere'):
            with self.assertRaises(ValueError):self.tasks.prepare({'project':project,'kind':'code','task':'controlled success'})
        prepared=self.tasks.prepare({'project':'Demo','kind':'code','task':'controlled success'})
        prepared['route']['model']='unreviewed'
        with self.assertRaises(ValueError):self.tasks.start(prepared)
        with self.assertRaises(ValueError):self.tasks.cancel('../unrelated')

    def test_reviewed_voice_request_starts_only_after_fresh_owner_confirmation(self):
        store=Store(self.root/'voice-state');now=time.time()
        s=Service(store,Mock(),Mock(),Mock(),clock=lambda:now,transport=Mock());s.work=self.tasks
        store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        s.gate.enable(True,now)
        for _ in range(3):
            now+=.1;s.gate.check(Evidence(now,True,True,frozenset({'owner'}),True),now)
        args={'project':'Demo','kind':'code','task':'controlled success'};generation=s.voice_gate.generation
        try:
            s.commands.request('work',args,owner=False,generation=generation)
            self.assertIsNone(s.commands.pending)
            review=s.commands.request('work',args,owner=True,generation=generation)
            self.assertIn(str(self.project),review)
            self.assertIn('controlled success',review)
            self.assertEqual(self.tasks.snapshot()['jobs'],[])
            result=s.commands.respond('Батлер, подтверждаю',owner=True,generation=generation)
            self.assertIn('в фоне',result)
            key=self.tasks.snapshot()['jobs'][0]['id'];self.keys.append(key)
            self.assertEqual(self.wait(key)['status'],'done')
            self.assertIsNone(s.commands.respond('да',owner=True,generation=generation))
        finally:s.stop();store.close()

    def fake_core(self, mode='ok'):
        """A stand-in for the deployed butler-core client that records calls; the core itself is tested in butler-core."""
        (self.workspace/'lib/mode').write_text(mode)
        (self.workspace/'lib/butler_core_client.py').write_text(FAKE_CORE_CLIENT)
        return self.workspace/'lib/calls.jsonl'

    def test_switched_voice_job_is_recorded_in_the_core_which_starts_it(self):
        calls=self.fake_core()
        prepared=self.tasks.prepare({'project':'Demo','kind':'code','task':'controlled success'})
        result=self.tasks.start(prepared)
        self.assertIn('в фоне',result['message'])
        call=json.loads(calls.read_text())
        self.assertEqual(call['kind'],'voice.'+prepared['route']['backend'])
        self.assertEqual((call['persona'],call['principal'],call['key']),('butler','voice:owner','voice:'+result['id']))
        self.assertEqual(call['target'],'project:'+prepared['project'])
        self.assertEqual(call['params']['voice_id'],result['id'])
        self.assertFalse((self.tasks.root/result['id']).exists())  # only the core's launch creates the job

    def test_switched_voice_job_is_refused_while_the_core_is_down(self):
        self.fake_core('down')
        prepared=self.tasks.prepare({'project':'Demo','kind':'code','task':'controlled success'})
        with self.assertRaisesRegex(ValueError,'недоступно'):self.tasks.start(prepared)
        self.assertFalse(self.tasks.root.exists() and any(self.tasks.root.iterdir()))

    def test_core_started_job_is_stopped_only_through_the_core(self):
        calls=self.fake_core()
        prepared=self.tasks.prepare({'project':'Demo','kind':'code','task':'controlled wait'})
        key='voice-20261024-100000-0123456789'
        process=self.tasks.spawn(key,prepared,'op-9')
        try:
            until=time.monotonic()+5
            while self.tasks.status(key)['status']!='running' and time.monotonic()<until:time.sleep(.05)
            self.assertEqual(self.tasks.cancel(key)['message'],'Фоновая задача остановлена.')
            self.assertEqual(json.loads(calls.read_text()),{'method':'cancel','operation':'op-9'})
        finally:
            os.killpg(process.pid,15);process.wait()


FAKE_CORE_CLIENT = '''
import json
from pathlib import Path
HERE = Path(__file__).parent
MODE = (HERE/'mode').read_text().strip()
class CoreUnavailable(ConnectionError):pass
class CoreError(RuntimeError):
    def __init__(self, code, message):super().__init__(code);self.code,self.message=code,message
def switched(name):return name=='voice_tasks'
def _record(value):
    with (HERE/'calls.jsonl').open('a') as stream:stream.write(json.dumps(value)+'\\n')
class Client:
    def execute(self, kind, persona, target, params, key, principal=None, timeout=60, task=None, wait=None):
        if MODE=='down':raise CoreUnavailable('down')
        _record({'kind':kind,'persona':persona,'target':target,'params':params,'key':key,'principal':principal})
        return {'id':'op-1','state':'running'}
    def call_waiting(self, method, wait=None, **params):
        _record({'method':method,**params});return {'state':'cancelled'}
'''


if __name__=='__main__':unittest.main()
