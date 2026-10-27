"""Owner-local retrieval from the real Butler vault, sessions and task memory."""
from __future__ import annotations
import json
import hashlib
import os
import select
from . import processes as subprocess
import sys
import time
import re
from pathlib import Path
from .components import WORKSPACE


RECALL_BUDGET = 6.0     # seconds: the vault, sessions and the semantic index through the core, on a busy Mac


def memory_requested(text):
    return bool(re.search(r"\b(?:помнишь|вспомни|помни|знаешь|проект\w*|мо[йяёеих]\w*|наш\w*|истори\w*|заметк\w*)\b|что\s+мы\s+решили|обо\s+мне",text,re.I))


def message_text(value):
    if isinstance(value,str):return value
    if isinstance(value,list):return '\n'.join(message_text(x) for x in value if isinstance(x,(str,dict)))
    if isinstance(value,dict):
        # Only human message content: never stringify tool arguments/results,
        # authorization metadata, credentials, reasoning or arbitrary JSON.
        if value.get('type') in {'text','input_text','output_text'}:return value.get('text','')
        if value.get('role') in {'user','assistant'}:return message_text(value.get('content',value.get('text','')))
        if 'message' in value:return message_text(value['message'])
    return ''


def session_hits(memory, query):
    hits=[];words=[x.casefold() for x in query.split() if len(x)>3][:10]
    deadline=time.monotonic()+.25
    if not words:return hits
    for path in sorted(memory.SESSIONS_DIR.glob('*.jsonl'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]:
        if time.monotonic()>deadline:break
        if path.is_symlink() or path.stat().st_size>4_000_000:continue
        per_file=0
        try:
            with path.open() as stream:
                for number,line in enumerate(stream,1):
                    if time.monotonic()>deadline:break
                    if not any(word in line.casefold() for word in words):continue
                    try:event=json.loads(line)
                    except ValueError:continue
                    text=message_text(event)
                    if not text or not any(word in text.casefold() for word in words):continue
                    hits.append(memory.Hit(scope='sessions',path=str(path),line_no=number,
                        snippet=text[:300],date=memory._extract_date_from_path(path.name),score=.6))
                    per_file+=1
                    if per_file>=2:break
        except OSError:continue
    return hits


class Knowledge:
    def __init__(self,service):
        self.service=service
        self.workspace=Path(service.config.get('butler_workspace',WORKSPACE))
        self.enabled=bool(service.config.get('voice_python'))
        self.state='configured' if self.enabled and (self.workspace/'lib/memory.py').is_file() else 'not-configured'
        self.last_count=0
        self.last_seconds=None
        self.process=None
        self.excluded_hashes=set()

    def cancel(self):
        process=self.process
        if process is not None and process.poll() is None:process.terminate()

    def snapshot(self):
        return {'state':self.state,'adapter':'existing Butler lib.memory.recall',
                'scopes':['all-vault','sessions','tasks'],'last_hits':self.last_count,
                'last_seconds':self.last_seconds,'raw_content':'owner-local memory only'}

    def recall(self,query,token):
        if not self.enabled or not (self.workspace/'lib/memory.py').is_file():return []
        env=subprocess.module_environment()
        env['BUTLER_CORE_PRINCIPAL']='voice:owner'      # a recall from the voice path is recorded as the owner's
        started=time.monotonic();process=None
        try:
            process=subprocess.Popen([sys.executable,'-m','butler_ambient.knowledge','recall',str(self.workspace)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
            self.process=process
            process.stdin.write(json.dumps({'query':query[:600]},ensure_ascii=False).encode());process.stdin.close()
            data=bytearray()
            while time.monotonic()-started<RECALL_BUDGET and not token.is_set():
                if select.select([process.stdout],[],[],.05)[0]:
                    chunk=os.read(process.stdout.fileno(),8192)
                    if not chunk:break
                    data.extend(chunk)
                    if len(data)>32768:raise ValueError('retrieval-size')
            if token.is_set() or process.poll() is None and time.monotonic()-started>=RECALL_BUDGET:
                self.state='cancelled' if token.is_set() else 'timeout';return []
            process.wait(timeout=.2)
            if process.returncode:raise ValueError('retrieval-unavailable')
            result=json.loads(data)
            if not isinstance(result,list) or len(result)>8:raise ValueError('retrieval-invalid')
            result=[m for m in result if hashlib.sha256(m.get('snippet','').encode()).hexdigest() not in self.excluded_hashes]
            self.state='ready';self.last_count=len(result)
            return result
        except (OSError,ValueError,subprocess.TimeoutExpired):self.state='unavailable';return []
        finally:
            self.last_seconds=round(time.monotonic()-started,3)
            if process is not None:
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=.5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                process.stdout.close()
                if not process.stdin.closed:process.stdin.close()
            if self.process is process:self.process=None


def recall_worker(workspace):
    sys.path.insert(0,str(workspace))
    from lib import memory
    from dataclasses import asdict
    value=json.load(sys.stdin)
    query=value['query']
    if not isinstance(query,str) or not 1<=len(query)<=600:raise ValueError('query')
    # Existing session parser expects a text string, whereas current OpenClaw
    # uses a structured message object. Adapt only this local worker, not the
    # shared Butler workspace or its session files.
    memory._search_sessions=lambda query,**kw:session_hits(memory,query)
    original_search=getattr(memory,'_search_with_ripgrep',None)
    if original_search is not None:
        def bounded_search(*args,**kwargs):
            kwargs['timeout_sec']=.2
            return original_search(*args,**kwargs)
        memory._search_with_ripgrep=bounded_search
    hits=memory.recall(query,k=6,scope=('all-vault','sessions','tasks'),semantic=True)
    result=[{k:asdict(hit)[k] for k in ('scope','path','line_no','snippet','date','score')} for hit in hits]
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='recall':raise SystemExit(2)
    try:recall_worker(Path(sys.argv[2]))
    except Exception:raise SystemExit(1)
