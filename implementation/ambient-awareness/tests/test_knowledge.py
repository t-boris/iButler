"""Local structured memory retrieval and cancellation without private exports."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from butler_ambient.knowledge import Knowledge,message_text,session_hits,memory_requested
from butler_ambient import knowledge, processes


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'lib').mkdir();(self.root/'lib/__init__.py').touch()
        (self.root/'lib/memory.py').write_text('''
from dataclasses import dataclass
from typing import Literal
import time
@dataclass
class Hit:
    scope:str
    path:str
    line_no:int
    snippet:str
    date:str
    score:float

def recall(query,*,k,scope,semantic):
    assert scope==('all-vault','sessions','tasks')
    if query=='slow':time.sleep(10)
    return [Hit('vault','Projects/Demo.md',3,'Synthetic project fact','2026-10-23',.9)]
''')
        self.k=Knowledge(SimpleNamespace(config={'butler_workspace':str(self.root),'voice_python':'fixture'}))

    def tearDown(self):
        self.k.cancel();self.temp.cleanup()

    def test_existing_adapter_result_has_provenance_and_never_metadata_content(self):
        values=self.k.recall('Synthetic lookup',threading.Event())
        self.assertEqual(values[0]['path'],'Projects/Demo.md')
        self.assertEqual(values[0]['snippet'],'Synthetic project fact')
        self.assertEqual(self.k.snapshot()['state'],'ready')
        self.assertNotIn('Synthetic',json.dumps(self.k.snapshot()))

    def test_cancellation_stops_worker_and_drops_retrieved_content(self):
        token=threading.Event();threading.Timer(.1,token.set).start()
        self.assertEqual(self.k.recall('slow',token),[])
        self.assertEqual(self.k.snapshot()['state'],'cancelled')
        self.assertIsNone(self.k.process)

    def test_installed_memory_worker_preserves_standard_library_import_order(self):
        environment = self.root / 'installed-env'
        processes.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True, timeout=15)
        site = environment / 'lib' / ('python' + '.'.join(map(str, sys.version_info[:2]))) / 'site-packages'
        shutil.copytree(Path(knowledge.__file__).parent, site / 'butler_ambient',
            ignore=shutil.ignore_patterns('__pycache__', 'native', 'assets'))
        (site / 'typing.py').write_text('raise RuntimeError("third-party typing shadowed stdlib")\n')
        script = '''
import json,sys,threading
from types import SimpleNamespace
from butler_ambient.knowledge import Knowledge
k=Knowledge(SimpleNamespace(config={'butler_workspace':sys.argv[1],'voice_python':'fixture'}))
hits=k.recall('Synthetic lookup',threading.Event())
print(json.dumps({'state':k.state,'hits':len(hits)}))
'''
        env = dict(os.environ)
        env.pop('PYTHONPATH', None); env.pop('PYTHONHOME', None)
        result = processes.run([str(environment / 'bin/python'), '-c', script, str(self.root)],
            cwd=self.root, env=env, capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(result.stdout), {'state':'ready', 'hits':1})

    def test_ordinary_dialogue_does_not_scan_vault_but_personal_queries_do(self):
        for value in ('Как дела?','Давай обсудим погоду','Почему именно так?'):self.assertFalse(memory_requested(value))
        for value in ('Помнишь мой проект?','Что мы решили вчера?','Какие у меня проекты?','Что ты знаешь обо мне?'):self.assertTrue(memory_requested(value))

    def test_structured_sessions_read_message_text_without_tool_args_or_reasoning(self):
        sessions=self.root/'sessions';sessions.mkdir()
        events=[{'message':{'role':'user','content':[{'type':'text','text':'Synthetic project plan'}]}},
            {'message':{'role':'tool','content':'Synthetic secret tool output'}},
            {'message':{'role':'assistant','content':[{'type':'thinking','thinking':'Synthetic hidden reasoning'}, {'type':'text','text':'Synthetic answer'}]}}]
        (sessions/'one.jsonl').write_text('\n'.join(map(json.dumps,events)))
        memory=SimpleNamespace(SESSIONS_DIR=sessions,Hit=lambda **kw:kw,_extract_date_from_path=lambda x:None)
        hits=session_hits(memory,'Synthetic')
        self.assertEqual(len(hits),2)
        self.assertNotIn('secret',json.dumps(hits))
        self.assertNotIn('reasoning',json.dumps(hits))
        self.assertEqual(message_text(events[0]),'Synthetic project plan')


if __name__=='__main__':unittest.main()
