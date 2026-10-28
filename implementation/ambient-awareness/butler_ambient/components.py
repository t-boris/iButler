"""Owner-local AI inventory and validated controls. No credentials or prompts."""
from __future__ import annotations

import ast
import http.client
import importlib.util
import json
import os
import re
import shutil
from . import processes as subprocess
import sys
from pathlib import Path

from . import local_settings, catalog
from .analysis import LocalAnalyzer, DEFAULT_DIALOGUE_MODEL

WORKSPACE = Path.home()/".openclaw/workspace-butler"
ROUTING_FILE = "config/ai-settings.json"


def command(cli, *args):
    try:result = subprocess.run([cli,*args],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=12)
    except subprocess.TimeoutExpired:
        raise ValueError("Butler CLI did not confirm the result in time; refresh the job state before retrying") from None
    except OSError:
        raise ValueError("Selected Butler CLI is unavailable") from None
    if result.returncode:
        # cron run uses exit 1 for a structured, non-error refusal such as
        # already-running. Preserve that receipt without forwarding raw stderr.
        try:receipt=json.loads(result.stdout) if args[:2]==("cron","run") else None
        except ValueError:receipt=None
        if not isinstance(receipt,dict) or receipt.get("ok") is not True or receipt.get("ran") is not False:
            raise ValueError("Butler CLI request failed; check the selected executable and service")
    return result.stdout.decode(errors="replace")


def constants(path):
    result = {}
    if not path.is_file(): return result
    for node in ast.parse(path.read_text()).body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            key,value = node.targets[0].id,node.value
        elif isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name):
            key,value = node.target.id,node.value
        else: continue
        if key not in {"ROUTES","CLAUDE_BIN","CODEX_BIN_CANDIDATES","MODEL","DIM","ALLOWED_PROJECT_ROOTS"}: continue
        try: result[key] = ast.literal_eval(value)
        except (ValueError,TypeError): pass
    return result


def owner_routing(workspace=WORKSPACE):
    path = workspace/ROUTING_FILE
    if not path.exists(): return {}
    if path.is_symlink() or path.stat().st_size>65536: raise ValueError("invalid owner routing configuration")
    value = json.loads(path.read_text())
    if not isinstance(value,dict): raise ValueError("invalid owner routing configuration")
    return value


def agent_status(service):
    cli = service.config.get("openclaw_cli",shutil.which("openclaw") or "openclaw")
    data = json.loads(command(cli,"models","--agent","butler","status","--json"))
    # Never return auth profiles, tokens, provider headers or credential paths.
    return {"model":data.get("resolvedDefault") or data.get("defaultModel"),
            "allowed":data.get("allowed",[]),"cli":cli}


def running_dispatches(workspace):
    rows=[]
    for path in sorted((workspace/"state/dispatches").glob("*/meta.json"))[-100:]:
        try:
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size>1_000_000:continue
            value=json.loads(path.read_text())
            key=value.get("dispatch_id","")
            pid=value.get("monitor_pid")
            if (value.get("status")!="running" or not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",key)
                    or path.parent.name!=key or type(pid) is not int or pid<2):continue
            os.kill(pid,0)
            rows.append({k:value.get(k) for k in ("dispatch_id","task_kind","backend","model","started_at_ts")})
        except (OSError,ValueError,TypeError):continue
    return rows


def snapshot(service, workspace=WORKSPACE):
    config,root = service.config,service.store.root
    model_catalog = getattr(service,"catalog",None)
    if model_catalog is not None: model_catalog.refresh()
    core_python = sys.executable
    rows = []
    def row(key,title,model,executable,model_key=None,cli_key=None,ready=False,running=False,**extra):
        rows.append({"id":key,"title":title,"model":str(model),"executable":str(executable),
            "model_key":model_key,"cli_key":cli_key,"ready":bool(ready),"running":bool(running),**extra})
    def file(key): return bool(config.get(key) and Path(config[key]).is_file())
    readiness = {}
    def local_ready(name,vision=False,dialogue=False):
        # The dialogue model is served on its own port (DEC-073); probing the background port would misreport it.
        if (name,vision,dialogue) not in readiness:
            try: LocalAnalyzer(name,dialogue=dialogue).check_local(require_vision=vision);readiness[name,vision,dialogue]=True
            except (RuntimeError,OSError,ValueError):readiness[name,vision,dialogue]=False
        return readiness[name,vision,dialogue]
    for key,title in [("face_detector","Поиск лиц · YuNet"),("face_recognizer","Проверка владельца · SFace"),
                      ("person_detector","Присутствие людей · локальная ONNX модель")]:
        row(key,title,config.get(key,"не настроено"),core_python,key,ready=file(key) and not getattr(service.verifier,"unavailable",False),running=service.input_running,
            note="Локально. Порог/согласия не меняются; новый файл должен быть совместим с существующими эталонами.")
    spec = importlib.util.find_spec("resemblyzer")
    voice_id = config.get("voice_identity_model",str(Path(spec.origin).parent/"pretrained.pt") if spec and spec.origin else "не настроено")
    row("voice_identity","Проверка голоса · Resemblyzer",voice_id,core_python,"voice_identity_model",
        ready=Path(voice_id).is_file(),running=service.input_running,note="Эталоны владельца отдельно; смена модели не перерегистрирует владельца.")
    row("asr","Распознавание речи · Whisper",config.get("whisper_model","не настроено"),
        config.get("whisper_cli",shutil.which("whisper-cli") or "whisper-cli"),"whisper_model","whisper_cli",file("whisper_model"),
        getattr(service.voice,"phase",None)=="transcription",note="Язык: "+config.get("speech_language","ru"))
    for key,title in [("conversation_model","Диалог и намерение"),("vision_model","Зрение и фоновый анализ")]:
        name = config.get(key,DEFAULT_DIALOGUE_MODEL if key=='conversation_model' else 'gemma3:4b')
        row(key,title,name,"HTTP 127.0.0.1:11435 · Ollama" if key=="conversation_model" else "HTTP 127.0.0.1:11434 · Ollama",key,
            ready=local_ready(name,key=="vision_model",key=="conversation_model"),
            running=bool(getattr(service.voice,"busy",False)) if key=="conversation_model" else bool(service.analyzer.operations),
            note="Только проверенная локальная модель; внешний резерв запрещён.")
    voice_path = Path(config.get("voice_model_path",root/"voice/base-model"))
    image_path = Path(config.get("image_model_path",root/"images/model"))
    row("tts","Естественный голос · Qwen3-TTS Base",voice_path,config.get("voice_python","не настроено"),
        "voice_model_path","voice_python",(voice_path/"config.json").is_file() and Path(config.get("voice_python","/missing")).is_file(),bool(getattr(service.voice,"worker_busy",False)),
        note="Используется только выбранный синтетический голос; биометрические записи не используются.")
    row("image","Создание изображений · Diffusers",image_path,config.get("image_python","не настроено"),
        "image_model_path","image_python",(image_path/"model_index.json").is_file() and Path(config.get("image_python","/missing")).is_file(),bool(service.visual and service.visual.process),
        note="Изолированная офлайн-модель, только по запросу. Исходные материалы остаются локально.")
    row("capture","Устройства и резервный захват · FFmpeg",config.get("camera_size","640x480")+" · PCM16/16kHz",
        config.get("ffmpeg_cli",shutil.which("ffmpeg") or "ffmpeg"),cli_key="ffmpeg_cli",ready=bool(shutil.which(config.get("ffmpeg_cli","ffmpeg"))),
        running=service.input_running and getattr(service.inputs,"camera_helper",None) is None,note="FFmpeg перечисляет устройства и служит резервом камеры. Основной захват выполняют отдельные нативные компоненты ниже.")
    if config.get("audio_helper"):
        native=Path(config["audio_helper"])
        signal=getattr(service.inputs,"input_signal",None)
        input_fresh=type(signal) is dict and type(signal.get("at")) in (int,float) and 0<=service.clock()-signal["at"]<=2.5
        camera_at=getattr(service.inputs,"last_camera_at",None)
        camera_fresh=type(camera_at) in (int,float) and 0<=service.clock()-camera_at<=2.5
        archive=getattr(service,"archive",None)
        row("native-audio","Микрофон и динамик · AUHAL / SpeexDSP","PCM16 / 16kHz",str(native),
            ready=getattr(service.inputs,"audio_status",{}).get("ready") is True,running=service.input_running and input_fresh,
            note="Собранный компонент Butler. Микрофон выбирается в устройствах; общей громкостью других приложений не управляет.")
        row("native-camera","Камера · AVFoundation",config.get("camera_size","640x480"),str(native.with_name("butler-camera")),
            ready=native.with_name("butler-camera").is_file(),running=camera_fresh and getattr(service.inputs,"camera_helper",None) is not None,
            note="Собранный компонент Butler. Камера, разрешение и эффекты macOS настраиваются в панели устройств.")
        row("system-audio","Звук приложений · ScreenCaptureKit","PCM16 / 16kHz",str(native.with_name("butler-system-audio")),
            ready=native.with_name("butler-system-audio").is_file(),running=bool(archive and archive.enabled and 0<=service.clock()-archive.last.get("system-audio",float('-inf'))<=2.5),
            note="Отдельный поток Meet/YouTube/музыки. Его речь не запускает голосовые команды; состояние фактической записи — в диагностике архива.")
    try:
        agent = agent_status(service)
        row("agent","Главный агент Butler",agent["model"],agent["cli"],"agent_model","openclaw_cli",True,
            choices=catalog.agent_choices(workspace,agent["allowed"]) if model_catalog is not None else agent["allowed"],note="Каталог существующих провайдеров Butler; наличие в списке CLI не гарантирует выполнение в OpenClaw. Меняется только агент butler, новые платные подключения не включаются.")
    except (ValueError,OSError,subprocess.TimeoutExpired):
        row("agent","Главный агент Butler","недоступен",config.get("openclaw_cli","openclaw"),"agent_model","openclaw_cli",note="Проверьте OpenClaw/gateway; настройки других ботов не меняются.")
    data = constants(workspace/"lib/dispatch.py")
    active=running_dispatches(workspace)
    routing = owner_routing(workspace)
    defaults = data.get("ROUTES",{})
    for kind,default in defaults.items():
        value = {**default,**routing.get("routes",{}).get(kind,{})}
        backend = value.get("backend")
        candidates = data.get("CODEX_BIN_CANDIDATES",[])
        executable = routing.get("executables",{}).get(backend) or (data.get("CLAUDE_BIN","claude") if backend=="claude"
                      else next((p for p in candidates if shutil.which(p)),shutil.which("codex") or "codex"))
        choices = ([f"{backend}:{name}" for backend in ("codex","claude") for name in catalog.provider_choices(workspace,backend)]
                   if model_catalog is not None else sorted({f"{r['backend']}:{r['model']}" for r in defaults.values()}))
        row("route:"+kind,"Задачи · "+kind,f"{backend}:{value.get('model')}",executable,
            "dispatch_route","dispatch_executable",bool(shutil.which(executable)) and (model_catalog is None or f"{backend}:{value.get('model')}" in choices),any(r["task_kind"]==kind for r in active),choices=choices,
            note="Каталог CLI; наличие в списке не гарантирует доступ при выполнении. Отсутствующая модель не заменяется автоматически. Изменение — со следующего запуска; ограничения стоимости сохраняются.")
    embeddings = constants(workspace/"lib/embeddings.py")
    if embeddings:
        model = routing.get("embedding_model",embeddings.get("MODEL","embeddinggemma"))
        try: embedding_info(model); embedding_ready=True
        except (OSError,ValueError): embedding_ready=False
        row("embedding","Семантическая память",model,
            "HTTP 127.0.0.1:11434 · Ollama","embedding_model",ready=embedding_ready,
            note="Смена модели/размерности требует перестроения локального индекса; старый индекс не используется как совместимый.")
    knowledge=getattr(service,"knowledge",None)
    if knowledge is not None:
        info=knowledge.snapshot()
        row("knowledge","Знания Butler · проекты, память, история",str(knowledge.workspace),core_python,
            ready=info["state"] in {"configured", "ready"},status=info["state"],
            note="Реальный локальный lib.memory.recall; история и факты остаются на Mac. " +
                 ("Настроен; первый поиск ещё не выполнялся." if info["state"]=="configured" else "Состояние последнего поиска: "+info["state"]))
    return {"components":rows,"catalog":model_catalog.snapshot() if model_catalog is not None else {},"privacy":"owner-local; no credential or prompt content","settings_hooks":{
        "dispatch": "BUTLER_OWNER_AI_SETTINGS" in (workspace/"lib/dispatch.py").read_text() if (workspace/"lib/dispatch.py").exists() else False,
        "embedding": "BUTLER_OWNER_AI_SETTINGS" in (workspace/"lib/embeddings.py").read_text() if (workspace/"lib/embeddings.py").exists() else False}}


def save_routing(workspace,values):
    path = workspace/ROUTING_FILE
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values,indent=2)+"\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def embedding_info(model):
    if not isinstance(model,str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,150}",model):
        raise ValueError("installed local embedding model required")
    conn = http.client.HTTPConnection("127.0.0.1",11434,timeout=3)
    try:
        conn.request("POST","/api/show",json.dumps({"model":model}),{"Content-Type":"application/json"})
        response = conn.getresponse();data = json.loads(response.read(1_000_000))
        if response.status!=200 or data.get("remote_host") or data.get("remote_model") or "embedding" not in data.get("capabilities",[]):
            raise ValueError("installed local embedding model required")
        dimensions = [v for k,v in data.get("model_info",{}).items() if k.endswith("embedding_length") and type(v) is int]
        if not dimensions or not 1<=dimensions[0]<=8192: raise ValueError("unsupported embedding dimension")
        return dimensions[0]
    finally: conn.close()


def update(service,values,workspace=WORKSPACE):
    if not isinstance(values,dict) or set(values)-{"id","model","executable"} or not isinstance(values.get("id"),str):
        raise ValueError("invalid AI component setting")
    row = next((r for r in snapshot(service,workspace)["components"] if r["id"]==values["id"]),None)
    if row is None: raise ValueError("unknown AI component")
    model,cli = values.get("model",row["model"]),values.get("executable",row["executable"])
    if not isinstance(model,str) or not isinstance(cli,str) or max(len(model),len(cli))>500:
        raise ValueError("invalid AI component value")
    routed=row['id'].startswith('route:')
    if model!=row["model"] and not row.get("model_key") and not routed and row['id'] not in {'agent','embedding'}:
        raise ValueError("Модель этого встроенного компонента не переключается. Используйте его настройки устройств.")
    if cli!=row["executable"] and not row.get("cli_key") and not routed and row['id']!='agent':
        raise ValueError("CLI этого компонента встроен в Butler и не переключается отдельно.")
    if row["id"].startswith("route:"):
        if "BUTLER_OWNER_AI_SETTINGS" not in (workspace/"lib/dispatch.py").read_text():
            raise ValueError("Butler routing Settings adapter is not installed")
        if model not in row.get("choices",[]): raise ValueError("choose an existing approved CLI/backend model")
        backend,name = model.split(":",1)
        if backend!="claude" and constants(workspace/"lib/dispatch.py").get("ROUTES",{}).get(row["id"].split(":",1)[1],{}).get("tools"):
            raise ValueError("Этот маршрут ограничен (без shell, только папка задачи) и работает только на claude; выберите модель claude.")
        family = "claude" if backend=="claude" else "codex"
        if cli == row["executable"] and model.split(":",1)[0] != row["model"].split(":",1)[0]:
            defaults=constants(workspace/"lib/dispatch.py")
            cli = defaults.get("CLAUDE_BIN","claude") if backend=="claude" else next(
                (p for p in defaults.get("CODEX_BIN_CANDIDATES",[]) if shutil.which(p)),"codex")
        binary = local_settings.executable(cli,family)
        data = owner_routing(workspace)
        data.setdefault("routes",{})[row["id"].split(":",1)[1]] = {"backend":backend,"model":name}
        data.setdefault("executables",{})[backend] = binary
        save_routing(workspace,data)
        return {"saved":True,"restart_required":False,"effective":"next Butler task"}
    if row["id"]=="agent":
        status = agent_status(service)
        allowed = catalog.agent_choices(workspace,status["allowed"]) if getattr(service,"catalog",None) is not None else status["allowed"]
        if model not in allowed: raise ValueError("choose a discovered model from an existing Butler provider")
        binary = local_settings.executable(cli,"openclaw")
        agents = json.loads(command(binary,"config","get","agents.list","--json"))
        selected = [(i,a) for i,a in enumerate(agents) if a.get("id")=="butler"]
        if len(selected)!=1: raise ValueError("Butler agent configuration is ambiguous")
        index,agent = selected[0]
        key = f"agents.list.{index}.model"+(".primary" if isinstance(agent.get("model"),dict) else "")
        command(binary,"config","set",key,json.dumps(model),"--strict-json")
        if cli!=row["executable"]: local_settings.update(service,{"openclaw_cli":binary})
        return {"saved":True,"restart_required":cli!=row["executable"],"effective":"next Butler agent run"}
    if row["id"]=="embedding":
        if "BUTLER_OWNER_AI_SETTINGS" not in (workspace/"lib/embeddings.py").read_text():
            raise ValueError("Butler semantic Settings adapter is not installed")
        dimension=embedding_info(model)
        value = owner_routing(workspace);value.update(embedding_model=model,embedding_dimension=dimension);save_routing(workspace,value)
        return {"saved":True,"restart_required":False,"effective":"next semantic task; index rebuild on mismatch"}
    changes = {}
    if row.get("model_key") and model!=row["model"]: changes[row["model_key"]]=model
    if row.get("cli_key") and cli!=row["executable"]: changes[row["cli_key"]]=cli
    if not changes:
        editable = bool(row.get("model_key") or row.get("cli_key"))
        return {"saved":False,"restart_required":False,"changed":[],"reason":"unchanged" if editable else "read-only",
                "message":"Значения не изменены." if editable else "Этот компонент только для просмотра: здесь его нельзя изменить."}
    return local_settings.update(service,changes)
