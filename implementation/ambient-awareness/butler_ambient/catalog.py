"""Metadata-only CLI model discovery. Never sends a prompt or reads credentials."""
from __future__ import annotations
import json
import os
import re
import selectors
import shutil
import signal
from . import processes as subprocess
import tempfile
import threading
import time
from pathlib import Path

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\[\]-]{0,149}\Z")
FILE = 'config/model-catalog.json'
INTERVAL = 3600
MAX_AGE = 7 * 86400
SOURCES = {'codex': 'OpenAI · Codex model/list (CLI may cache its catalog)',
           'claude': 'Anthropic · Claude initialize models (CLI/account catalog)'}


def normalized(backend, values):
    if not isinstance(values, list) or len(values) > 500: raise ValueError('invalid model catalog')
    rows = {}
    for value in values:
        if not isinstance(value, dict): raise ValueError('invalid model entry')
        if backend == 'codex' and value.get('hidden'): continue
        key = value.get('model') if backend == 'codex' else value.get('resolvedModel') or value.get('value')
        if not isinstance(key, str) or not MODEL.fullmatch(key): continue
        label = value.get('displayName', key)
        label = label[:120] if isinstance(label, str) else key
        row = rows.setdefault(key, {'id': key, 'name': label, 'aliases': []})
        alias = value.get('value') if backend == 'claude' else None
        if isinstance(alias, str) and alias != key and MODEL.fullmatch(alias) and alias not in row['aliases']:
            row['aliases'].append(alias)
    if not rows: raise ValueError('empty model catalog')
    return list(rows.values())


class MetadataProcess:
    def __init__(self, args, cwd, timeout=20):
        self.process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.deadline = time.monotonic() + timeout
        self.total = 0
    def send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode()); self.process.stdin.flush()
    def receive(self):
        while time.monotonic() < self.deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                value = json.loads(line)
                if not isinstance(value, dict): raise ValueError('invalid metadata response')
                return value
            if not self.selector.select(.2): continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk: raise ValueError('CLI closed metadata channel')
            self.total += len(chunk)
            if self.total > 4_000_000: raise ValueError('metadata response exceeds limit')
            self.buffer += chunk
        raise TimeoutError('model metadata timeout')
    def close(self):
        self.selector.close()
        if self.process.poll() is None:
            try: os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                self.process.wait(timeout=2)
        self.process.stdin.close(); self.process.stdout.close()


def discover(backend, executable):
    if backend not in SOURCES or not shutil.which(executable): raise ValueError('provider CLI unavailable')
    with tempfile.TemporaryDirectory(prefix='butler-model-metadata-') as cwd:
        args = [executable] + (['app-server', '--listen', 'stdio://'] if backend == 'codex' else
            ['--print', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
             '--no-session-persistence', '--safe-mode', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', ''])
        channel = MetadataProcess(args, cwd)
        try:
            if backend == 'codex':
                channel.send({'method': 'initialize', 'id': 1, 'params': {'clientInfo': {'name': 'butler_catalog', 'title': 'Butler model catalog', 'version': '0.1.0'}}})
                while channel.receive().get('id') != 1: pass
                channel.send({'method': 'initialized', 'params': {}})
                rows, cursor, seen = [], None, set()
                for page in range(10):
                    params = {'limit': 100, 'includeHidden': False}
                    if cursor: params['cursor'] = cursor
                    request = page + 2
                    channel.send({'method': 'model/list', 'id': request, 'params': params})
                    while True:
                        response = channel.receive()
                        if response.get('id') == request: break
                    if 'error' in response: raise ValueError('CLI rejected catalog request')
                    result = response.get('result', {})
                    rows.extend(result.get('data', []))
                    cursor = result.get('nextCursor')
                    if not cursor: return normalized(backend, rows)
                    if not isinstance(cursor, str) or cursor in seen: raise ValueError('invalid catalog pagination')
                    seen.add(cursor)
                raise ValueError('catalog pagination exceeds limit')
            channel.send({'type': 'control_request', 'request_id': 'catalog', 'request': {'subtype': 'initialize', 'hooks': None, 'skills': []}})
            while True:
                value = channel.receive()
                response = value.get('response', {})
                if value.get('type') == 'control_response' and response.get('request_id') == 'catalog':
                    if response.get('subtype') != 'success': raise ValueError('CLI rejected initialization')
                    return normalized(backend, response.get('response', {}).get('models'))
        finally: channel.close()


def read_cache(workspace):
    path = Path(workspace) / FILE
    try:
        if path.is_symlink() or path.stat().st_size > 1_000_000: return {}
        data = json.loads(path.read_text())
        if data.get('version') != 1 or not isinstance(data.get('providers'), dict): return {}
        # Copy only sanitized metadata, even when a cache has been edited locally.
        result = {}
        for backend in SOURCES:
            row = data['providers'].get(backend, {})
            if not isinstance(row, dict): continue
            models = row.get('models', [])
            if not isinstance(models, list) or len(models) > 500: continue
            clean = []
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get('id'), str) or not MODEL.fullmatch(model['id']): continue
                clean.append({'id': model['id'], 'name': str(model.get('name', model['id']))[:120],
                    'aliases': [a for a in model.get('aliases', []) if isinstance(a, str) and MODEL.fullmatch(a)][:30]})
            result[backend] = {'models': clean, 'fetched_at': row.get('fetched_at') if type(row.get('fetched_at')) in (int, float) else 0,
                'attempted_at': row.get('attempted_at') if type(row.get('attempted_at')) in (int, float) else 0,
                'error': row.get('error') if row.get('error') in ('provider-unavailable', None) else 'provider-unavailable', 'source': SOURCES[backend]}
        return result
    except (OSError, ValueError, TypeError, AttributeError): return {}


def provider_choices(workspace, backend, *, canonical=False, now=None):
    row = read_cache(workspace).get(backend, {})
    age = (time.time() if now is None else now) - row.get('fetched_at', 0)
    if not 0 <= age <= MAX_AGE: return []
    return list(dict.fromkeys(value for model in row.get('models', []) for value in
        ([model['id']] if canonical else [model['id'], *model['aliases']])))


def runtime_cache(workspace, now=None):
    path = Path(workspace)/FILE
    try:
        if path.is_symlink() or path.stat().st_size > 1_000_000: return {}
        row = json.loads(path.read_text()).get('runtime',{})
        age = (time.time() if now is None else now)-row.get('fetched_at',0)
        if not 0 <= age <= MAX_AGE: return {}
        models = row.get('models',[])
        if not isinstance(models,list) or len(models)>1000: return {}
        return {'models':[v for v in models if isinstance(v,str) and '/' in v and MODEL.fullmatch(v)],
            'fetched_at':row.get('fetched_at',0),'error': 'provider-unavailable' if row.get('error') else None,
            'source':'OpenClaw provider registry · runtime compatibility'}
    except (OSError,ValueError,TypeError,AttributeError):return {}


def discover_runtime(executable):
    result=[]
    for provider in ('openai','anthropic'):
        response=subprocess.run([executable,'models','--agent','butler','list','--all','--provider',provider,'--json'],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=12,check=True)
        if len(response.stdout)>1_000_000:raise ValueError('runtime metadata exceeds limit')
        rows=json.loads(response.stdout).get('models',[])
        for row in rows:
            key=row.get('key')
            if isinstance(key,str) and key.startswith(provider+'/') and MODEL.fullmatch(key) and row.get('available') is True:
                result.append(key)
    return list(dict.fromkeys(result))


def agent_choices(workspace, allowed):
    # Existing provider integrations only. No provider/key or global allowlist is added.
    runtime = runtime_cache(workspace)
    if not runtime: return list(allowed)
    compatible = set(runtime["models"])
    rows = []
    for provider in dict.fromkeys(value.split('/', 1)[0] for value in allowed if '/' in value):
        backend = {'openai': 'codex', 'openai-codex': 'codex', 'anthropic': 'claude'}.get(provider)
        if backend: rows += [provider + '/' + model for model in provider_choices(workspace, backend, canonical=True) if provider + '/' + model in compatible]
    existing = []
    for value in allowed:
        provider, separator, name = value.partition('/')
        backend = {'openai': 'codex', 'openai-codex': 'codex', 'anthropic': 'claude'}.get(provider)
        discovered = provider_choices(workspace, backend, canonical=True) if backend else []
        if not backend or ((not discovered or name in discovered) and value in compatible): existing.append(value)
    return list(dict.fromkeys([*existing, *rows]))


class Catalog:
    def __init__(self, workspace, executables=None, clock=time.time, fetch=discover, runtime_cli=None):
        self.workspace = Path(workspace)
        self.executables = executables or {key: shutil.which(key) or key for key in SOURCES}
        self.clock, self.fetch = clock, fetch
        self.runtime_cli = runtime_cli
        self.runtime = runtime_cache(workspace)
        self.lock = threading.RLock()
        self.busy = False
        self.stopped = threading.Event()
        cached = read_cache(workspace)
        self.data = {key:cached.get(key,{"models":[],"fetched_at":0,"attempted_at":0,"error":None}) for key in SOURCES}
        self.last_attempt = max((v.get('attempted_at', 0) for v in self.data.values()), default=0)
    def snapshot(self):
        with self.lock:
            return {'refreshing': self.busy, 'runtime':self.runtime, 'providers': [{**row, 'backend': key,
                'stale': self.clock() - row.get('fetched_at', 0) > INTERVAL,
                'source': SOURCES[key]} for key, row in self.data.items()],
                'notice': 'CLI model catalog; listing does not verify inference access. No prompts or API keys sent by Butler.'}
    def refresh(self, force=False):
        with self.lock:
            if self.stopped.is_set() or self.busy or (not force and self.clock() - self.last_attempt < INTERVAL): return False
            settings=self.workspace/'config/ai-settings.json'
            if settings.is_file() and not settings.is_symlink() and settings.stat().st_size<=65536:
                try:
                    selected=json.loads(settings.read_text()).get('executables',{})
                    for backend in SOURCES:
                        value=selected.get(backend)
                        if isinstance(value,str) and Path(value).name==backend and shutil.which(value):self.executables[backend]=value
                except (OSError,ValueError,TypeError,AttributeError):pass
            self.busy = True; self.last_attempt = self.clock()
        threading.Thread(target=self._refresh, daemon=True).start()
        return True
    def _refresh(self):
        try:
            for backend in SOURCES:
                if self.stopped.is_set(): return
                try: models = self.fetch(backend, self.executables.get(backend, backend)); error = None
                except (OSError, ValueError, TypeError, AttributeError, TimeoutError, subprocess.SubprocessError): models = None; error = 'provider-unavailable'
                with self.lock:
                    row = self.data.setdefault(backend, {'models': [], 'fetched_at': 0})
                    row.update(attempted_at=self.clock(), error=error, source=SOURCES[backend])
                    if models is not None: row.update(models=models, fetched_at=self.clock())
            if self.runtime_cli and not self.stopped.is_set():
                try: runtime_models=discover_runtime(self.runtime_cli); error=None
                except (OSError,ValueError,TypeError,AttributeError,subprocess.SubprocessError):runtime_models=None;error='provider-unavailable'
                with self.lock:
                    self.runtime['error']=error
                    if runtime_models is not None:self.runtime.update(models=runtime_models,fetched_at=self.clock(),source='OpenClaw provider registry · runtime compatibility')
            with self.lock:
                if not self.stopped.is_set(): self._save()
        finally:
            with self.lock: self.busy = False
    def _save(self):
        path = self.workspace / FILE
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'providers': self.data, 'runtime':self.runtime}, indent=2) + '\n')
        temporary.chmod(0o600); temporary.replace(path)
    def stop(self): self.stopped.set()
