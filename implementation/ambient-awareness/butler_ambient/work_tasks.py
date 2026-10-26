"""Voice-owned background jobs using the existing Butler dispatcher and routes.

With the ``voice_tasks`` switch on in the active butler-core configuration, a
reviewed job is recorded as a core operation and the core starts it through
``launch`` below; otherwise this service starts it directly (legacy path).
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import signal
from . import processes as subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from .components import WORKSPACE, constants, owner_routing

CORE_CLIENT = 'lib/butler_core_client.py'


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.new')
    with temporary.open('w') as stream:
        os.chmod(temporary,0o600)
        json.dump(value,stream,ensure_ascii=False,indent=2)
    temporary.replace(path)


def json_snapshot(path):
    """The shared dispatcher updates JSON in place; tolerate its short write gap.

    Never substitute a guessed state if a complete snapshot remains unavailable.
    """
    for attempt in range(6):
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict): raise ValueError('Invalid task state')
            return value
        except (ValueError, UnicodeError):
            if attempt == 5: raise ValueError('Состояние задачи временно недоступно. Повторите запрос.') from None
            time.sleep(.01)


class WorkTasks:
    def __init__(self, service, workspace=None):
        self.service = service
        self.workspace = Path(workspace or service.config.get('butler_workspace',WORKSPACE)).resolve()
        self.root = self.workspace/'state/dispatches'
        self.processes = {}
        self._core = None

    def core(self):
        """The butler-core client deployed into the Butler workspace, or None before the core is installed."""
        if self._core is None:
            from .delivery import core_client
            self._core = core_client(self.workspace)
        return self._core

    def core_switched(self):
        core = self.core()
        return bool(core and core.switched('voice_tasks'))

    def routes(self):
        data = constants(self.workspace/'lib/dispatch.py')
        settings = owner_routing(self.workspace)
        return {key:{**value,**settings.get('routes',{}).get(key,{})}
                for key,value in data.get('ROUTES',{}).items()}

    def projects(self):
        roots = constants(self.workspace/'lib/dispatch.py').get('ALLOWED_PROJECT_ROOTS',[])
        found = {}
        for name in roots:
            root = Path(name).expanduser()
            if not root.is_dir():continue
            found[str(root.resolve())] = {'name':root.name,'path':str(root.resolve())}
            for path in sorted(root.iterdir()):
                if path.name.startswith('.') or not path.is_dir() or path.is_symlink():continue
                found[str(path.resolve())] = {'name':path.name,'path':str(path.resolve())}
        return list(found.values())[:200]

    def prepare(self, args):
        if not isinstance(args,dict) or set(args)!={'project','task','kind'}:
            raise ValueError('Нужны конкретный проект, задача и тип работы.')
        if not all(isinstance(args.get(k),str) and args[k].strip() for k in args):
            raise ValueError('Уточните проект и задачу.')
        if not 5 <= len(args['task']) <= 1200:raise ValueError('Опишите одну задачу короче и конкретнее.')
        matches = [p for p in self.projects() if args['project'].casefold() in (p['path'].casefold(),p['name'].casefold())]
        if len(matches)!=1:raise ValueError('Проект не найден однозначно. Назовите его точное имя или путь.')
        route = self.routes().get(args['kind'])
        if not route or route.get('backend') not in {'codex','claude'}:raise ValueError('Такой тип работы не настроен в Butler.')
        return {**args,'project':matches[0]['path'],'route':{k:route[k] for k in ('backend','model')},'timeout':route.get('timeout',1800)}

    def _path(self, key):
        if not isinstance(key,str) or not re.fullmatch(r'voice-[0-9]{8}-[0-9]{6}-[a-f0-9]{10}',key):
            raise ValueError('Неизвестная голосовая задача.')
        path = self.root/key
        if path.is_symlink():raise ValueError('Неверный путь задачи.')
        return path

    def start(self, reviewed):
        current = self.prepare({k:reviewed[k] for k in ('project','task','kind')})
        if current != reviewed:raise ValueError('Проект или модель изменились после просмотра. Повторите запрос.')
        key = 'voice-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:10]
        if self.core_switched():return self._start_core(key,current)
        process = self.spawn(key,current)
        self.processes[key] = process
        def reap():
            process.wait()
            self.processes.pop(key,None)
        threading_reaper = threading.Thread(target=reap,daemon=True)
        threading_reaper.start()
        return self._started(current,key)

    def _started(self, current, key):
        return {'ok':True,'id':key,'message':'Задача запущена в фоне: '+Path(current['project']).name+'. Можно продолжать разговор или спросить о ходе работы.'}

    def _start_core(self, key, current):
        """Record the owner-confirmed job in butler-core; the core starts and follows it (DEC-061: no bypass)."""
        core = self.core()
        params = {'voice_id':key,'project':current['project'],'task':current['task'],'task_kind':current['kind'],
                  'backend':current['route']['backend'],'model':current['route']['model'],'timeout':current['timeout']}
        try:
            op = core.Client().execute('voice.'+current['route']['backend'],'butler','project:'+current['project'],params,
                                       'voice:'+key,principal='voice:owner',timeout=60)
        except core.CoreUnavailable:
            raise ValueError('Ядро Butler недоступно; задача не запущена. Повторите через минуту.') from None
        except core.CoreError:
            raise ValueError('Ядро Butler не приняло задачу; она не запущена.') from None
        if op.get('state') in ('running','requested','dispatching'):return self._started(current,key)
        raise ValueError('Ядро Butler не запустило задачу: '+str(op.get('reason') or op.get('state'))[:120])

    def spawn(self, key, current, operation=None):
        """Write the job's files and start its detached worker; the worker owns later metadata writes."""
        path = self._path(key);path.mkdir(parents=True,mode=0o700)
        meta = {'dispatch_id':key,'task_kind':current['kind'],**current['route'],
                'project_path':current['project'],'prompt':current['task'],'timeout':current['timeout'],
                'chain_depth':0,'started_at':dt.datetime.now().isoformat(timespec='seconds'),
                'started_at_ts':int(time.time()),'status':'running','monitor_pid':None,'channel':'owner-local-voice'}
        private_json(path/'meta.json',meta)
        for name in ('output.log','stderr.log'):
            (path/name).touch(mode=0o600)
        env = dict(os.environ)
        env.pop('OPENAI_API_KEY',None);env.pop('ANTHROPIC_API_KEY',None)
        env['CODEX_HOME'] = str(Path.home()/'.codex')
        env['PATH']=str(self.workspace/'bin')+os.pathsep+env.get('PATH','')
        env = subprocess.module_environment(self.workspace, env=env)
        try:
            process = subprocess.Popen([sys.executable,'-m','butler_ambient.work_tasks','worker',str(self.workspace),key],
                cwd=self.workspace,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            # Worker owns subsequent metadata writes. This launch receipt is separate
            # to avoid overwriting a fast worker's completed state.
            receipt = {'pid':process.pid,'project':current['project'],'route':current['route']}
            if operation:receipt['operation'] = operation
            private_json(path/'voice-launch.json',receipt)
        except OSError:
            private_json(path/'result.json',{'exit_code':-2,'duration_sec':0,'finished_at_ts':int(time.time())})
            meta['status']='failed';private_json(path/'meta.json',meta)
            raise ValueError('CLI работник не запущен. Ошибка сохранена локально.') from None
        return process

    def status(self, key):
        path = self._path(key)
        try:
            meta = json_snapshot(path/'meta.json')
            receipt = json_snapshot(path/'voice-launch.json')
        except FileNotFoundError:raise ValueError('Задача не найдена.') from None
        except OSError:raise ValueError('Состояние задачи временно недоступно. Повторите запрос.') from None
        if meta.get('channel')!='owner-local-voice' or meta.get('dispatch_id')!=key:raise ValueError('Это не голосовая задача Butler.')
        result = json_snapshot(path/'result.json') if (path/'result.json').is_file() else None
        pid = receipt['pid'];alive=False
        try:
            os.kill(pid,0);alive=True
        except (OSError,TypeError):pass
        state = ('done' if result.get('exit_code')==0 else 'cancelled' if result.get('exit_code')==-15 else 'failed') if result else 'running' if alive else 'interrupted'
        return {'id':key,'project':Path(meta['project_path']).name,'kind':meta['task_kind'],
                'backend':meta['backend'],'model':meta['model'],'status':state,'pid':pid,
                'elapsed_sec':max(0,int((result or {}).get('finished_at_ts',time.time())-meta['started_at_ts'])),
                'output_bytes':(path/'output.log').stat().st_size,
                'exit_code':result.get('exit_code') if result else None}

    def snapshot(self):
        rows=[]
        for path in sorted(self.root.glob('voice-*/meta.json'),reverse=True)[:20]:
            try:rows.append({k:v for k,v in self.status(path.parent.name).items() if k!='pid'})
            except (OSError,ValueError,KeyError):continue
        return {'jobs':rows,'available':(self.workspace/'lib/dispatch.py').is_file(),
                'notification_channel':'local application/voice only'}

    def cancel(self, key):
        row = self.status(key)
        if row['status']!='running':return {'ok':False,'message':'Задача уже завершилась.'}
        operation = json_snapshot(self._path(key)/'voice-launch.json').get('operation')
        if operation:
            # Started through butler-core: only the core stops it, so its record stays exact.
            core = self.core()
            if core is None:raise ValueError('Ядро Butler не установлено; отмена не выполнена.')
            try:stopped = core.Client().call_waiting('cancel',operation=operation)
            except core.CoreUnavailable:raise ValueError('Ядро Butler недоступно; отмена не выполнена. Повторите через минуту.') from None
            except core.CoreError:raise ValueError('Ядро Butler не выполнило отмену.') from None
            if stopped.get('state')=='cancelled':return {'ok':True,'message':'Фоновая задача остановлена.'}
            return {'ok':False,'message':'Задача уже завершилась.'}
        # Never kill an arbitrary stored PID: require the actual worker argv and its
        # detached process-group identity to still belong to this exact job.
        proc = subprocess.run(['/bin/ps','-p',str(row['pid']),'-o','command='],capture_output=True,text=True,timeout=2)
        marker = '-m butler_ambient.work_tasks worker '+str(self.workspace)+' '+key
        if marker not in proc.stdout or os.getpgid(row['pid'])!=row['pid']:
            raise ValueError('Процесс задачи уже изменился; отмена не выполнена.')
        os.killpg(row['pid'],signal.SIGTERM)
        path=self._path(key)
        private_json(path/'result.json',{'exit_code':-15,'duration_sec':row['elapsed_sec'],'finished_at_ts':int(time.time())})
        return {'ok':True,'message':'Остановка фоновой задачи запрошена.'}


def worker(workspace, key):
    workspace=Path(workspace).resolve()
    sys.path.insert(0,str(workspace))
    from lib import dispatch
    path=workspace/'state/dispatches'/key
    meta=json.loads((path/'meta.json').read_text())
    if meta.get('channel')!='owner-local-voice' or meta.get('dispatch_id')!=key:raise ValueError('Invalid voice dispatch')
    if dispatch._resolve_project_path(meta['project_path']) != Path(meta['project_path']):raise ValueError('Project changed')
    route=dispatch.ROUTES.get(meta['task_kind'],{})
    if any(route.get(k)!=meta[k] for k in ('backend','model')):raise ValueError('Route changed')
    # Reuse the actual dispatcher, cost accounting and CLI command builder, with
    # this voice-owned notification channel instead of Telegram/waking its session.
    dispatch._notify=lambda *a,**kw:None
    dispatch._wake_butler=lambda *a,**kw:None
    dispatch._wrap_prompt=lambda text:(
        'Owner-approved task from the local Butler voice interface. Work only in the reviewed project and scope. '
        'Existing Butler knowledge/CLI adapters are in '+str(workspace/'bin')+'. Use its existing recall/tasks/project tools when relevant. '
        'Shared MCP/network tools are available through the configured OpenClaw gateway tools.catalog/tools.invoke and your existing CLI MCP connections. '
        'Use the configured subscriptions and cost/access rules; do not provision new credentials or paid services. '
        'Do not send messages, call butler-notify, deploy, merge, change global settings or contact others. '
        'Handle routine implementation choices yourself. Report unresolved scope/approval questions in the result; '
        'never answer them as the owner or bypass an approval. Keep output useful for local progress. TASK:\n'+text)
    meta['monitor_pid']=os.getpid();private_json(path/'meta.json',meta)
    return dispatch._monitor(key)


def launch(workspace, key, operation):
    """Started by butler-core for a recorded ``voice.*`` operation; the reviewed job arrives on stdin."""
    tasks = WorkTasks(None,workspace)
    reviewed = json.loads(sys.stdin.read())
    current = tasks.prepare({k:reviewed[k] for k in ('project','task','kind')})
    if current != reviewed:
        sys.stderr.write('project or route changed after review\n');return 2
    if tasks._path(key).exists():return 0  # already launched for this operation
    tasks.spawn(key,current,operation)
    print(key);return 0


if __name__=='__main__':
    if len(sys.argv)==5 and sys.argv[1]=='launch':
        os.umask(0o077)
        try:raise SystemExit(launch(sys.argv[2],sys.argv[3],sys.argv[4]))
        except ValueError as error:
            # Validation messages name the project or route, never the task text.
            sys.stderr.write(str(error)[:200]+'\n');raise SystemExit(2)
    if len(sys.argv)!=4 or sys.argv[1]!='worker':raise SystemExit(2)
    os.umask(0o077)
    try:raise SystemExit(worker(sys.argv[2],sys.argv[3]))
    except Exception:
        # Exception text can contain owner paths/prompts; keep a fixed local error.
        path=Path(sys.argv[2])/'state/dispatches'/sys.argv[3]
        if path.is_dir():
            (path/'stderr.log').write_text('Butler CLI worker unavailable; check configured project/model/CLI.\n')
            private_json(path/'result.json',{'exit_code':-2,'duration_sec':0,'finished_at_ts':int(time.time())})
        raise SystemExit(1)
