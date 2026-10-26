"""Actual Butler adapters and one-use owner-bound spoken confirmations.

No shell, model-guessed authority or generic 'yes' without a reviewed operation.
Requests and pending reviews stay in local memory and clear with the lease.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass

from .memory_controls import CAPABILITIES as MEMORY_CAPABILITIES, READ_ONLY as MEMORY_READ_ONLY, MemoryControls
from .analysis import DEFAULT_CONTEXT_TOKENS

CAPABILITIES = {
    "status": "Состояние Butler", "help": "Голосовые возможности",
    "jobs": "Задания и расписания", "components": "Модели и CLI",
    "schedule": "Запустить, остановить, приостановить или изменить задание",
    "component": "Изменить модель или CLI компонента",
    "preferences": "Изменить настройки Butler и режим встречи", "devices": "Выбрать камеру, микрофон или язык",
    "voice": "Выбрать синтетический голос", "revoke": "Отозвать согласие участника",
    "pause": "Полностью остановить наблюдение", "catalog": "Обновить каталоги моделей",
    "owner-setup": "Подтвердить владельца Mac и обновить голосовой эталон",
    "work": "Поручить задачу существующему Codex/Claude в проекте",
    "work-status": "Проверить ход фоновой работы", "work-cancel": "Остановить голосовую задачу",
    "computer": "Поручить работу на Mac через существующие инструменты Butler",
    "guest-enroll": "Представить гостя и зарегистрировать его лицо и голос локально",
    **MEMORY_CAPABILITIES,
}
PLAN_SCHEMA = {"type":"object", "additionalProperties":False,
    "required":["capability","parameters"], "properties": {
        "capability":{"type":"string","enum":[*CAPABILITIES,"unknown","silent"]},
        # Registered adapters below validate every allowed key, value and target.
        # A normal object avoids JSON-inside-a-string and oversized grammar
        # repetition rejected by the installed local sampler.
        "parameters":{"type":"object","additionalProperties":True}}}
ADDRESS_SCHEMA={"type":"object","additionalProperties":False,"required":["recipient","confidence"],
    "properties":{"recipient":{"type":"string","enum":["butler","other","quoted","uncertain"]},
                  "confidence":{"type":"number","minimum":0,"maximum":1}}}


def plan_schema(available):
    """Constrain work arguments without JSON-encoded strings or invented routes."""
    variants=[]
    for capability,required in (('work',['project','kind','task']),('computer',['kind','task']),
                                ('other',[])):
        properties={'kind':{'type':'string','enum':list(available.get('work',{}).get('routes',{})) or ['unavailable']},
                    'project':{'type':'string'},'task':{'type':'string'}}
        params=({'type':'object','additionalProperties':False,'required':required,
                    'properties':{key:properties[key] for key in required}} if required
                else PLAN_SCHEMA['properties']['parameters'])
        variant={**PLAN_SCHEMA,'properties':{**PLAN_SCHEMA['properties'],
            'capability':{'type':'string','enum':([capability] if required else
                [key for key in PLAN_SCHEMA['properties']['capability']['enum'] if key not in {'work','computer',*MEMORY_CAPABILITIES}])},
            'parameters':params}}
        variants.append(variant)
    observations = available.get('observations', {})
    def target(values):
        choices = [x for x in values if isinstance(x, str)]
        return {'type':'string', 'enum': choices or ['unavailable']}
    media = target(r.get('id') for r in observations.get('recordings', []))
    schemas = {
        'recordings': ({'source':{'type':'string','enum':['camera','microphone','screen','system-audio']},
                        'limit':{'type':'integer','minimum':1,'maximum':12}}, []),
        'summary': ({'minutes':{'type':'integer','minimum':1,'maximum':527040},
                     'start':{'type':'string'},'end':{'type':'string'}}, []),
        'preserve': ({'id':media}, ['id']),
        'delete': ({'id':media,'results':{'type':'boolean'}}, ['id','results']),
        'correct': ({'subject':target(r.get('subject') for r in observations.get('subjects', [])),
                     'name':{'type':'string','maxLength':80}}, ['subject','name']),
        'material-add': ({'path':{'type':'string','maxLength':500}}, ['path']),
        'material-remove': ({'path':target(observations.get('materials', []))}, ['path']),
        'image-delete': ({'id':target(r.get('id') for r in observations.get('images', []))}, ['id']),
        'image-cancel': ({'id':target([observations.get('image_work', {}).get('request_id')])}, ['id']),
    }
    for capability in MEMORY_CAPABILITIES:
        properties, required = schemas.get(capability, ({}, []))
        variants.append({**PLAN_SCHEMA, 'properties':{
            'capability':{'type':'string','enum':[capability]},
            'parameters':{'type':'object','additionalProperties':False,'properties':properties,'required':required}}})
    return {'anyOf':variants}


def command_words(text, aliases):
    words = " ".join(re.findall(r"[\w-]+", text.casefold()))
    for alias in aliases:
        name = " ".join(re.findall(r"[\w-]+", alias.casefold()))
        for prefix in (name, "эй "+name, "hey "+name):
            if name and words.startswith(prefix+" "):return words[len(prefix)+1:]
    return words


def confirmation(text, aliases=("Бата", "Батлер", "Butler")):
    if any(mark in text for mark in '«»"“”„'):return None
    words = command_words(text, aliases)
    if words in {"нет", "не надо", "нет не надо", "не подтверждаю", "отмена", "отменить", "отклоняю", "передумал", "стоп"}: return False
    if words in {"да", "подтверждаю", "да подтверждаю", "разрешаю", "согласен", "выполняй",
            "подтверждаю выполнение этой задачи", "подтверждаю выполнение этой операции",
            "подтверждаю выполнение этой операции с указанными параметрами"}: return True
    return None


# DEC-067: Butler's own device and mode controls stay local even when the butler agent answers everything else.
LOCAL_CONTROL = re.compile(r"\b(?:я\s+(?:на\s+встрече|не\s+на\s+встрече)|режим\s+встреч\w*|тихий\s+режим|познакомься|микрофон\w*|камер\w*|"
                           r"громкост\w*|(?:смени|поменяй|выбери)\s+голос|голос\s+(?:потише|погромче)|наблюдени\w*|постоянн\w*\s+анализ|"
                           r"(?:какие|покажи|перечисли)\s+(?:у\s+тебя\s+)?(?:модели|задания|настройки|возможности|записи|наблюдения|папки))\b", re.I)


def local_control(text):
    return bool(LOCAL_CONTROL.search(text))


def control_request(text):
    return bool(re.search(r"\b(?:поставь|приостанови|возобнови|переключи|смени|настрой|выключи|включи|"
        r"запусти|останови|измени|обнови|отзови|запрограммируй|исправь|почини|реализуй|разработай|поручи|"
        r"открой|закрой|создай|удали|переименуй|сохрани|познакомься)\b|"
        r"\bкак\s+(?:там|идёт|продвигается)\s+(?:задача|работа|кодекс)|\bя\s+(?:на\s+встрече|не\s+на\s+встрече)|"
        r"\b(?:какие|покажи|перечисли)\s+(?:у\s+тебя\s+)?(?:модели|задания|задачи|настройки|возможности|записи|наблюдения|папки)\b|"
        r"\b(?:составь|дай|сделай|подготовь)\s+(?:мне\s+)?сводку\b|"
        r"\bчто\s+ты\s+(?:умеешь|можешь)\s*[?.!]*$", text, re.I))


@dataclass(frozen=True)
class Pending:
    capability: str
    parameters: dict
    review: str
    digest: str
    generation: int
    expires: float


class VoiceCommands:
    TTL = 45

    def __init__(self, service):
        self.service = service
        self.pending = None
        self.lock = threading.RLock()
        self.revision = 0
        self.last_result = "idle"
        self.inventory={}
        self.inventory_lock=threading.RLock()
        self.memory = MemoryControls(service)

    def _inventory(self,kind,*,force=False):
        with self.inventory_lock:
            cached=self.inventory.get(kind)
            if cached and not force and 0<=self.service.clock()-cached[0]<30:return cached[1]
        if kind=='jobs':value=self.service.schedules.snapshot()['jobs']
        elif kind=='devices':
            from .local_settings import devices
            value=devices(self.service.config.get('ffmpeg_cli','ffmpeg'))
        else:
            from .components import snapshot
            value=snapshot(self.service)['components']
        with self.inventory_lock:self.inventory[kind]=(self.service.clock(),value)
        return value

    def inventory_loop(self):
        while not self.service.stop_event.is_set():
            for kind in ('jobs','components','devices'):
                if self.service.stop_event.is_set():break
                try:self._inventory(kind,force=True)
                except (OSError,ValueError,RuntimeError):pass
            self.service.stop_event.wait(30)

    def clear(self):
        with self.lock:
            self.pending = None
            self.revision += 1
            return self.revision

    def snapshot(self):
        with self.lock:
            pending = self.pending
            return {"capabilities": [{"id":key,"title":title} for key,title in CAPABILITIES.items()],
                "pending": {"review":pending.review,"expires":pending.expires} if pending else None,
                "state":self.last_result}

    def context(self, query=""):
        jobs = self._inventory('jobs')
        components = self._inventory('components')
        result={"capabilities":CAPABILITIES,
            "observations":self.memory.context(),
            "participants":self.service.store.execute("SELECT id,name FROM agreements WHERE active=1")[:20],
            "devices":self._inventory('devices'),
            "models":sorted({choice for c in components for choice in c.get("choices",[])})[:100],
            "work":{"projects":[p['name'] for p in self.service.work.projects()],
                "routes":{k:{field:v.get(field) for field in ('backend','model')} for k,v in self.service.work.routes().items()},
                "recent":[{field:j.get(field) for field in ('id','project','status')} for j in self.service.work.snapshot()['jobs'][:5]]},
            "jobs":[{k:j.get(k) for k in ("id","name","enabled","status")} for j in jobs],
            "components":[{k:c.get(k) for k in ("id","title","model")} for c in components]}
        # Catalog choices are shared instead of repeating them for every route.
        # Keep model context bounded even after years of completed work.
        text=query.casefold()
        for key in ('components','jobs'):
            result[key].sort(key=lambda item:0 if any(str(item.get(k,'')).casefold() in text for k in ('id','name','title') if item.get(k)) else 1)
        result['work']['projects'].sort(key=lambda name:0 if name.casefold() in text else 1)
        budget=lambda:len(json.dumps(result,ensure_ascii=False))>14000
        for values,minimum in ((result['models'],0),(result['jobs'],8),(result['components'],8),(result['work']['projects'],0),
                (result['observations']['materials'],0),(result['observations']['recordings'],3),(result['observations']['subjects'],3),(result['observations']['images'],3)):
            while budget() and len(values)>minimum:values.pop()
        return result

    def interpret(self, text, model, token):
        if len(text)>2000:raise ValueError("Слишком длинное поручение. Уточните одно действие или задачу.")
        aliases=self.service.store.get('address_aliases',['Бата','Батлер','Butler'])
        if not isinstance(aliases,list):aliases=['Бата','Батлер','Butler']
        address=model.generate_json(
            'Определи адресата фразы, услышанной постоянно готовым помощником Butler. '
            'Сначала только адресат, без выбора действия. Верни JSON {"recipient":"butler|other|quoted|uncertain","confidence":0.0}. '
            'Прямая просьба или команда без другого названного адресата предназначена Butler; имя помощника не обязательно. '
            'butler — просьба помощнику, other — обращение к другому человеку, quoted — пересказ чужих слов без своей просьбы, '
            'uncertain — адресат неясен. Упоминание человека как объекта не делает его адресатом. '
            'Пример «Света, выключи свет» -> {"recipient":"other","confidence":0.99}. '
            'Пример «Исправь таблицу» -> {"recipient":"butler","confidence":0.99}. '
            'Пример «Помоги написать письмо» -> {"recipient":"butler","confidence":0.99}. '
            'Пример «Он сказал: открой дверь» -> {"recipient":"quoted","confidence":0.99}. '
            'Пример «Переведи фразу: открой дверь» -> {"recipient":"butler","confidence":0.99}. '
            'Имена помощника и фраза — данные:\n'+json.dumps({'aliases':aliases,'speech':text},ensure_ascii=False),
            ADDRESS_SCHEMA,token=token,options={'num_ctx':DEFAULT_CONTEXT_TOKENS,'num_predict':100})
        confidence=address.get('confidence')
        if (address.get('recipient')!='butler' or type(confidence) not in (int,float)
                or not .9<=confidence<=1):return ('silent',{})
        available=self.context(text)
        schema=plan_schema(available)
        plan = model.generate_json(
            "Convert a Russian owner request to ONE existing Butler control. Do not execute it. "
            "Addressing was already checked separately; now map the direct request to an existing adapter. "
            "Return JSON {capability,parameters}. For example 'Почини интерфейс в проекте Demo' -> work if Demo exists. "
            "Use exact existing target IDs; never guess a job/component or invent an executable/model. "
            "If the requested target/action is ambiguous use unknown. parameters is a JSON object, NOT an encoded string. "
            "Include only fields actually requested; silent/unknown have empty parameters {}. "
            "schedule: {id,action} (run/pause/resume/stop/update; optional daily_time/cron/timezone/model). "
            "component: {id,model,executable}, only requested changed fields. "
            "preferences: {muted,disabled_topics,daily_time,voice_enabled,address_aliases,meeting_mode,continuous_archive}, only requested fields. "
            "meeting_mode: quiet for 'я на встрече/не вмешивайся', available for explicit 'я не на встрече', auto for automatic detection. "
            "work: {project,kind,task}. Choose ONE exact listed project and existing task kind; "
            "task is a concise derived objective, not a raw transcript, retaining the requested scope. "
            "Do not guess missing project or change the task. work-status: optional {id}; work-cancel: {id} from recent actual jobs. "
            "computer: {kind,task}, only for general Mac/filesystem/network/MCP work without a project; this uses Butler's workspace and actual CLI tools. "
            "guest-enroll: {name}, only when the owner explicitly introduces a guest and requests deliberate durable recognition. "
            "devices: {camera,microphone,camera_size,speech_language}, use listed device IDs for named devices, only requested fields; voice: {profile} (calm/baritone/warm). "
            "revoke: {id}. status/help/jobs/components/pause/catalog: {}. "
            "recordings: optional {source,limit}, ONLY list recent recordings. Saving separately is preserve, never recordings. "
            "Example 'Сохрани отдельно последнюю запись с микрофона' -> preserve with the exact newest microphone id. "
            "summary: {minutes} for a relative period OR {start,end} as ISO8601 timestamps with timezone, using observations.now. "
            "preserve: {id} for one actual recording. delete: {id,results}; results=false for originals only, true ONLY if explicitly asked to delete analysis too; clarify if unclear. "
            "correct: {subject,name}, only an existing observed subject, never biometric consent. materials: {} lists selected roots; material-add/material-remove: {path} only an explicitly requested path. "
            "images: {} reports image work; image-delete: {id} from actual image results; image-cancel: {id} from observations.image_work.request_id stops exactly that work. screen-permission/video-settings: {} open native controls. "
            "Recording/image targets must use exact listed IDs; never invent or guess an ambiguous target. Data:\n" +
            json.dumps({"request":text,"available":available},ensure_ascii=False), schema, token=token,
            options={"num_ctx":DEFAULT_CONTEXT_TOKENS,"num_predict":900})
        parameters=plan.get('parameters')
        if isinstance(parameters,str):
            try:parameters=json.loads(parameters)
            except (ValueError,TypeError):return None
        if not isinstance(parameters,dict): return None
        return plan.get("capability"), parameters

    def _validate(self, capability, parameters):
        if capability not in CAPABILITIES or not isinstance(parameters,dict): raise ValueError("Не понял, какую возможность Butler использовать.")
        if capability in MEMORY_CAPABILITIES:
            self.memory.validate(capability,parameters)
            if len(json.dumps(parameters,ensure_ascii=False)) > 2000: raise ValueError("Слишком сложная команда: уточните одно действие.")
            return
        allowed = {"guest-enroll":{"name"},"work":{"project","task","kind"},"computer":{"task","kind"},"work-status":{"id"},"work-cancel":{"id"},
            "schedule":{"id","action","cron","timezone","model","daily_time"},
            "component":{"id","model","executable"},
            "preferences":{"muted","disabled_topics","daily_time","voice_enabled","address_aliases","meeting_mode","continuous_archive"},
            "devices":{"camera","microphone","camera_size","speech_language"}, "voice":{"profile"}, "revoke":{"id"}}
        if set(parameters)-allowed.get(capability,set()): raise ValueError("Неизвестный параметр команды.")
        if capability=="guest-enroll" and (not isinstance(parameters.get("name"),str) or not 1<=len(parameters["name"].strip())<=80 or any(ord(x)<32 for x in parameters["name"])):
            raise ValueError("Нужно имя гостя, которое назвал владелец.")
        if capability == "work": self.service.work.prepare(parameters)
        if capability == "computer": self.service.work.prepare({**parameters,"project":str(self.service.work.workspace)})
        if capability == "work-cancel" or capability == "work-status" and "id" in parameters:
            self.service.work.status(parameters.get("id"))
        if capability in {"schedule","component","revoke"} and not isinstance(parameters.get("id"),str): raise ValueError("Нужно уточнить, что именно изменить.")
        if capability == "schedule":
            jobs = self._inventory('jobs')
            if parameters["id"] not in {j["id"] for j in jobs} or parameters.get("action") not in {"run","pause","resume","stop","update"}:
                raise ValueError("Не найдено однозначное задание или действие.")
        if capability == "component":
            if parameters["id"] not in {c["id"] for c in self._inventory('components')}: raise ValueError("Компонент не найден.")
        if capability == "voice" and parameters.get("profile") not in {"calm","baritone","warm"}: raise ValueError("Нужно выбрать существующий голос.")
        if capability == "revoke" and not self.service.store.execute("SELECT id FROM agreements WHERE id=? AND active=1", (parameters["id"],)):
            raise ValueError("Действующее согласие участника не найдено.")
        if len(json.dumps(parameters,ensure_ascii=False)) > 2000: raise ValueError("Слишком сложная команда: уточните одно действие.")

    def request(self, capability, parameters, *, owner, generation):
        revision = self.clear()
        if capability in MEMORY_CAPABILITIES and isinstance(parameters,dict):
            parameters = self.memory.normalize(capability,parameters)
        self._validate(capability, parameters)
        if capability == "owner-setup":
            self.service.owner_setup.start()
            return "Открыта локальная проверка владельца Mac. После неё нажмите «Начать» и прочтите контрольные цифры."
        if capability in {"status","help"}: return self._read(capability)
        # DEC-066: the owner session, with no other person evidenced during the request, is the authority.
        with self.service.lock:
            if not owner or generation != self.service.voice_gate.generation or not self.service.voice_gate.permits(self.service.clock()):
                return "Не могу подтвердить, что операцию поручаете вы: рядом кто-то ещё или сессия закрыта. Повторите, когда будете один."
        if capability in {"jobs","components","work-status"}: return self._read(capability,parameters)
        if capability in MEMORY_READ_ONLY: return self.memory.read(capability,parameters)
        if capability == "computer":
            capability="work";parameters={**parameters,"project":str(self.service.work.workspace)}
        if capability == "work": parameters = self.service.work.prepare(parameters)
        body = json.dumps({"capability":capability,"parameters":parameters},ensure_ascii=False,sort_keys=True)
        review = self._review(capability,parameters)
        if len(review) > 430: raise ValueError("Слишком много параметров для одного подтверждения. Уточните одно изменение.")
        with self.service.lock, self.lock:
            if revision != self.revision:return "Операция отменена: запрос изменён."
            if generation != self.service.voice_gate.generation or not self.service.voice_gate.permits(self.service.clock()):
                return "Операция отменена: потеряно подтверждение владельца."
            self.pending = Pending(capability,dict(parameters),review,hashlib.sha256(body.encode()).hexdigest(),generation,self.service.clock()+self.TTL)
            self.last_result = "awaiting-owner-confirmation"
            return review + ". Подтверждаете это изменение?"

    def _review(self, capability, args):
        if capability in MEMORY_CAPABILITIES: return self.memory.review(capability,args)
        if capability=="guest-enroll":return "Представить гостя «"+args["name"]+"» и после проверки владельца Mac записать его локальные эталоны лица и голоса. Обычный архив приостановится на 15 секунд регистрации"
        if capability == "work":
            return ("Запустить в фоне " + args["route"]["backend"] + " / " + args["route"]["model"] +
                "; проект " + args["project"] + "; задача: " + args["task"])
        if capability == "work-cancel":
            row = self.service.work.status(args["id"])
            return "Остановить фоновую задачу " + row["project"] + " / " + row["kind"] + ", " + args["id"][-6:]
        if capability == "pause": return "Полностью остановить наблюдение. Микрофон и камера выключатся; включение будет доступно в окне Butler"
        if capability == "catalog": return "Обновить доступные каталоги моделей"
        if capability == "voice":
            return "Выбрать голос: " + {"calm":"спокойный мужской","baritone":"тёплый баритон","warm":"тёплый женский"}[args["profile"]]
        if capability == "revoke":
            row = self.service.store.execute("SELECT name FROM agreements WHERE id=?", (args["id"],))[0]
            return "Отозвать согласие на запись: " + row["name"]
        if capability == "schedule":
            job = next(j for j in self._inventory('jobs') if j["id"] == args["id"])
            action = {"run":"Запустить","pause":"Приостановить будущие запуски","resume":"Возобновить расписание","stop":"Остановить текущую работу и расписание","update":"Изменить расписание"}[args["action"]]
            text = action + ": " + job["name"]
            for key,label in [("daily_time","время"),("cron","расписание"),("timezone","часовой пояс"),("model","модель")]:
                if key in args: text += "; " + label + ": " + str(args[key])
            return text
        if capability == "component":
            row = next(c for c in self._inventory('components') if c["id"] == args["id"])
            return "Изменить " + row["title"] + ": " + "; ".join(label+": "+str(args[key]) for key,label in [("model","модель"),("executable","CLI")] if key in args)
        labels = {"muted":"Приостановить инициативные советы","daily_time":"Время дневной сводки","voice_enabled":"Голос включён",
            "meeting_mode":"Режим встречи",
            "continuous_archive":"Постоянный анализ источников через временный буфер",
            "disabled_topics":"Отключённые темы советов","address_aliases":"Варианты имени",
            "camera":"Камера","microphone":"Микрофон","camera_size":"Разрешение камеры","speech_language":"Язык распознавания"}
        values = dict(args)
        if capability == "devices" and {"camera","microphone"} & args.keys():
            from .local_settings import devices
            choices = devices(self.service.config.get("ffmpeg_cli","ffmpeg"))
            for key in ("camera","microphone"):
                if key in values:
                    match = [d["name"] for d in choices[key] if d["id"] == values[key]]
                    if len(match) != 1: raise ValueError("Выбранное устройство недоступно.")
                    values[key] = match[0]
        def value(item):
            if item in ("quiet","available","auto"): return {"quiet":"я на встрече","available":"разговор со мной","auto":"автоматически"}[item]
            if type(item) is bool: return "да" if item else "нет"
            if isinstance(item,list): return ", ".join(map(str,item)) or "нет"
            return str(item)
        return "; ".join(labels[k]+": "+value(v) for k,v in values.items())

    def respond(self, text, *, owner, generation, before_execute=None):
        aliases = self.service.store.get("address_aliases",["Бата","Батлер","Butler"])
        answer = confirmation(text,aliases)
        repeat = command_words(text,aliases) in {"повтори", "что я подтверждаю", "какую команду ты выполнишь"}
        # Invalidation holds service.lock before clearing commands. Preserve
        # that order and check permission before disclosing a private review.
        with self.service.lock, self.lock:
            pending = self.pending
            if pending is None: return None
            if answer is False:
                self.clear()
                self.last_result = "cancelled"
                return "Отменено."
            if answer is None and not repeat:
                # A correction/new request invalidates the old confirmation.
                self.clear()
                self.last_result = "replaced"
                return None
            if (generation != pending.generation or generation != self.service.voice_gate.generation
                    or self.service.clock() > pending.expires
                    or not self.service.voice_gate.permits(self.service.clock())):
                self.clear()
                self.last_result = "confirmation-rejected"
                return "Подтверждение не принято: владелец или срок команды не подтверждены. Повторите сам запрос."
            body = json.dumps({"capability":pending.capability,"parameters":pending.parameters},ensure_ascii=False,sort_keys=True)
            if hashlib.sha256(body.encode()).hexdigest() != pending.digest:
                self.clear()
                raise ValueError("Команда изменилась: подтверждение сброшено.")
            if not owner:
                # A short 'да' may have insufficient biometric material. Never
                # disclose parameters, authorize it or extend the original TTL.
                self.last_result = "awaiting-owner-verification"
                return "Рядом кто-то ещё: произнесите полностью «Подтверждаю выполнение этой операции с указанными параметрами»."
            if repeat:return pending.review + ". Подтверждаете?"
            revision = self.clear()  # One-use approval, consumed before any adapter.

        def cancelled():
            # Called with service.lock then commands.lock at each boundary.
            if revision != self.revision:return "Операция отменена: запрос изменён."
            if (not self.service.voice_gate.permits(self.service.clock())
                    or generation != self.service.voice_gate.generation or self.service.clock() > pending.expires):
                self.last_result = "permission-lost"
                return "Операция отменена: потеряно подтверждение владельца или истёк срок команды."
            body = json.dumps({"capability":pending.capability,"parameters":pending.parameters},ensure_ascii=False,sort_keys=True)
            if hashlib.sha256(body.encode()).hexdigest() != pending.digest:
                raise ValueError("Команда изменилась: подтверждение сброшено.")

        if pending.capability == "work":
            current = self.service.work.prepare({k:pending.parameters[k] for k in ("project","task","kind")})
            if current != pending.parameters: raise ValueError("Маршрут задачи изменился. Повторите запрос.")
        else: self._validate(pending.capability,pending.parameters)
        with self.service.lock, self.lock:
            reason = cancelled()
            if reason:return reason
        if before_execute is not None: before_execute()
        with self.service.lock, self.lock:
            reason = cancelled()
            if reason:return reason
            # Local mutations share the service lock with invalidation and UI
            # controls. Revalidate their exact targets at the commit boundary.
            local = pending.capability in MEMORY_CAPABILITIES and pending.capability not in {"screen-permission", "video-settings"}
            if local:
                self._validate(pending.capability,pending.parameters)
                result = self._execute(pending.capability,pending.parameters)
        if not local: result = self._execute(pending.capability,pending.parameters)
        with self.lock:
            if revision == self.revision:
                self.last_result = "completed" if result.get("ok",result.get("saved",False)) else "not-completed"
        return result.get("message") or ("Настройка сохранена." if result.get("saved") else "Операция не подтверждена сервисом.")

    def _read(self, capability, parameters=None):
        if capability == "help": return "Можно поручить работу Codex или Claude в проекте; управлять заданиями, моделями, CLI, устройствами и режимом встречи. Можно получить сводку, сохранить или удалить запись, исправить наблюдение и настроить папки изображений. Изменения требуют вашего подтверждения."
        if capability == "work-status":
            rows = self.service.work.snapshot()["jobs"]
            if parameters and parameters.get("id"): rows = [self.service.work.status(parameters["id"])]
            labels = {"running":"выполняется","done":"готово","failed":"ошибка","cancelled":"отменено","interrupted":"прервано"}
            return ("; ".join(r["project"]+": "+labels.get(r["status"],r["status"])+", "+str(r["elapsed_sec"])+" секунд" for r in rows[:5]) or "Голосовых задач пока нет.")[:600]
        if capability == "status":
            s = self.service
            return "Наблюдение " + ("включено" if s.gate.enabled else "выключено") + ", разговор " + ("разрешён" if s.voice_gate.permits(s.clock()) else "на паузе проверки владельца") + "."
        if capability == "jobs":
            value = self.service.schedules.snapshot()
            return ("; ".join(j["name"]+": "+j["status"] for j in value["jobs"][:8]) or "Задания не найдены.")[:600]
        from .components import snapshot
        return "; ".join(c["title"]+": "+c["model"] for c in snapshot(self.service)["components"][:7])[:600]

    def _execute(self, capability, args):
        s = self.service
        if capability in MEMORY_CAPABILITIES: return self.memory.execute(capability,args)
        if capability=="guest-enroll":
            s.owner_setup.start(guest_name=args["name"])
            return {"ok":True,"message":"Открыта проверка владельца Mac. Затем говорит только гость, один перед камерой. Эталоны сохранятся после контрольной фразы."}
        if capability == "work": return s.work.start(args)
        if capability == "work-cancel": return s.work.cancel(args["id"])
        if capability == "schedule": return s.schedules.control(args)
        if capability == "preferences": return s.control("preferences",args)
        if capability == "revoke":
            s.control("revoke",args)
            return {"ok":True,"message":"Согласие участника отозвано."}
        if capability == "voice":
            from .voice_catalog import select
            with s.lock: select(s.store,args["profile"])
            return {"ok":True,"message":"Голос выбран."}
        if capability == "pause":
            s.control("enabled",{"value":False})
            return {"ok":True,"message":"Наблюдение остановлено. Включить прослушивание можно в окне Butler."}
        if capability == "catalog":
            if getattr(s,"catalog",None) is None: return {"ok":False,"message":"Каталог сейчас недоступен."}
            s.catalog.refresh(force=True)
            return {"ok":True,"message":"Обновление каталога запрошено."}
        if capability == "devices":
            from .local_settings import update
        else:
            from .components import update
        if s.restart is None: raise ValueError("Перезапуск Butler сейчас недоступен.")
        result = update(s,args)
        if result.get("restart_required"):
            timer = threading.Timer(.5,s.restart)
            timer.daemon = True
            timer.start()
        return result
