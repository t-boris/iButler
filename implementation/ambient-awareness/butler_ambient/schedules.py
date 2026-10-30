"""Owner controls for Butler jobs only; pause schedules separately from stopping runs."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from .components import command, running_dispatches, WORKSPACE
from .delivery import ZONE, core_client


class Schedules:
    def __init__(self,service,workspace=WORKSPACE):
        self.service,self.workspace = service,workspace
        self.manual = {}
        self.tokens = {"daily":threading.Event(),"advice":threading.Event()}

    @property
    def cli(self): return self.service.config.get("openclaw_cli","openclaw")

    def cron_jobs(self):
        data = json.loads(command(self.cli,"cron","list","--agent","butler","--all","--json","--timeout","8000"))
        return [j for j in data.get("jobs",[]) if j.get("agentId")=="butler"]

    def backburner(self):
        return json.loads(command(str(self.workspace/"bin/butler-backburner"),"list","--json"))

    def nightly_state(self):
        """Read the active core switch, never infer execution from an active brief."""
        core=core_client(self.workspace)
        if core is None:return None
        try:
            content=core.Client(timeout=2).call("config.active")["content"]
            enabled=content.get("scenarios",{}).get("backburner",{}).get("enabled",False)
            if type(enabled) is not bool:raise ValueError("invalid nightly switch")
            rule=content.get("backburner",{})
            return {"enabled":enabled,"time":rule.get("time","00:30"),"last_start":rule.get("last_start","06:00"),
                    "zone":rule.get("zone","America/Chicago")}
        except (OSError,RuntimeError,ValueError,KeyError,TypeError):return None

    def _backburner_row(self,value,nightly):
        active=value["status"]=="active";stamp=lambda text:dt.datetime.fromisoformat(text).timestamp() if text else None
        note={"paused":"","done":"Исследование отмечено завершённым."}.get(value["status"],"В брифе не указан status: папка не запускается.")
        error=note if not active else "Папка ждёт ваших правок: несколько прогонов подряд без прогресса." if value["waiting"] else ""
        if not error and value["last_reason"]:error="Последний запуск: "+value["last_reason"][:300]
        enabled=nightly is not None and nightly["enabled"]
        plan=(nightly["time"]+"–"+nightly["last_start"]+" · "+nightly["zone"] if nightly else value["window"].replace("-","–")+" · "+value["zone"])
        next_run=None
        if active and enabled and not value["waiting"]:
            zone=ZoneInfo(nightly["zone"]);local=dt.datetime.fromtimestamp(self.service.clock(),zone)
            start=dt.datetime.combine(local.date(),dt.time.fromisoformat(nightly["time"]),zone)
            last=dt.datetime.fromisoformat(value["last_run_at"]).astimezone(zone) if value["last_run_at"] else None
            next_run=(start+dt.timedelta(days=1) if (last and last.date()==local.date()) or local.strftime("%H:%M")>=nightly["last_start"]
                      else start if local<start else local).timestamp()
        if active and not enabled:
            error=("Ночная автоматика выключена в ядре." if nightly is not None else "Состояние ночной автоматики не подтверждено: ядро недоступно.")+ (" "+error if error else "")
        return {"id":"backburner:"+value["slug"],"kind":"backburner","name":"Back Burner · "+value["name"],"enabled":active and enabled,"running":False,
            "status":"paused" if not active else "unknown" if nightly is None else "disabled" if not enabled else "error" if value["last_status"]=="failed" else "scheduled",
            "schedule":("план: каждую ночь " if enabled else "автоматика выключена · окно " if nightly is not None else "автоматика не подтверждена · окно ")+plan+" · приоритет "+str(value["priority"]),
            "next_run":next_run,"last_run":stamp(value["last_run_at"]),"last_result":value["last_status"] or "прогонов нет",
            "model":(value["model"] or "—")+" · подтверждённых прогонов: "+str(value["runs_total"]),"error":error}

    def snapshot(self):
        rows=[];error=None
        try:
            for job in self.cron_jobs():
                state,schedule = job.get("state",{}),job.get("schedule",{})
                kind = schedule.get("kind")
                expression = (schedule.get("expr","")+" · "+schedule.get("tz","local") if kind=="cron"
                              else str(schedule.get("at",schedule.get("everyMs",""))))
                rows.append({"id":job["id"],"kind":"cron","name":job.get("name",job["id"]),
                    "enabled":bool(job.get("enabled")),"running":bool(state.get("runningAtMs")),
                    "status":"running" if state.get("runningAtMs") else "paused" if not job.get("enabled") else state.get("lastRunStatus","ready"),
                    "schedule":expression,"cron":schedule.get("expr",""),"timezone":schedule.get("tz","America/Chicago"),
                    "next_run":state.get("nextRunAtMs",0)/1000 or None,"last_run":state.get("lastRunAtMs",0)/1000 or None,
                    "model":job.get("payload",{}).get("model") or "модель агента Butler",
                    "error":state.get("lastError",state.get("lastDiagnosticSummary",""))[:1500]})
        except Exception:
            error="Не удалось получить задания OpenClaw. Локальные задания показаны отдельно."
        try:
            folders=self.backburner()
            nightly=self.nightly_state() if folders else None
            rows+=[self._backburner_row(value,nightly) for value in folders]
        except Exception:error=(error+" " if error else "")+"Не удалось получить папки Back Burner."
        s=self.service;now=s.clock();local=dt.datetime.fromtimestamp(now,ZONE)
        daily=s.store.get("daily_time","20:00")
        due=dt.datetime.combine(local.date(),dt.time.fromisoformat(daily),ZONE)
        if due.timestamp()<=now:due+=dt.timedelta(days=1)
        for key,name,schedule,enabled,model,next_run in [
            ("daily","Дневная сводка окружения",daily+" · America/Chicago",not s.store.get("daily_paused",False),"typed local summary",due.timestamp()),
            ("advice","Инициативные советы","≤3/день · ≥60 мин · 08–22 America/Chicago",not s.store.get("muted",False),"grounded local rules",None),
            ("analysis","Фоновый анализ наблюдений","каждую минуту при разрешении",not s.store.get("analysis_paused",False),s.config.get("vision_model","gemma3:4b"),max(now,(s.archive.last_analysis if s.archive.enabled else s.last_analysis)+60))]:
            running=self.manual.get(key)=="running"
            rows.append({"id":"builtin:"+key,"kind":"builtin","name":name,"enabled":enabled,"running":running,
                "status":"running" if running else "error" if self.manual.get(key)=="failed" else "ready" if enabled else "paused","schedule":schedule,"next_run":next_run if enabled else None,
                "model":model,"error":"Не удалось подготовить или показать результат. Проверьте доступ к наблюдениям и повторите запуск." if self.manual.get(key)=="failed" else "","daily_time":daily if key=="daily" else "",
                "telegram_enabled":s.store.get("schedule_telegram_"+key,False) is True if key in {"daily","advice"} else None})
        for value in running_dispatches(self.workspace):
            if value["dispatch_id"].startswith("voice-"):continue
            rows.append({"id":"dispatch:"+value["dispatch_id"],"kind":"dispatch","name":"Задача · "+str(value.get("task_kind","")),
                "enabled":True,"running":True,"status":"running","schedule":"уже запущена",
                "model":str(value.get("backend",""))+":"+str(value.get("model","")),
                "last_run":value.get("started_at_ts"),"next_run":None,"error":""})
        for value in s.work.snapshot()["jobs"]:
            running=value["status"]=="running"
            rows.append({"id":"voice:"+value["id"],"kind":"voice-work","name":value["project"]+" · "+value["kind"],
                "enabled":running,"running":running,"status":value["status"],"schedule":"в фоне · "+str(value["elapsed_sec"])+" с",
                "model":value["backend"]+":"+value["model"],"next_run":None,"error":"CLI завершился с ошибкой" if value["status"]=="failed" else ""})
        return {"jobs":rows,"error":error,"scope":"Butler only; no prompts or credentials",
                "pause_semantics":"Пауза отключает будущие запуски. Остановить прерывает текущую работу и отключает расписание."}

    def control(self,args):
        if not isinstance(args,dict) or set(args)-{"id","action","cron","timezone","model","daily_time","telegram_enabled"}:
            raise ValueError("invalid scheduled-run control")
        key,action=args.get("id"),args.get("action")
        if not isinstance(key,str) or action not in {"run","pause","resume","stop","update"}:
            raise ValueError("invalid scheduled-run action")
        if "telegram_enabled" in args and (action != "update" or key not in {"builtin:daily", "builtin:advice"}):
            raise ValueError("Telegram delivery is not configurable for this job here")
        if key.startswith("builtin:"):return self._builtin(key.split(":",1)[1],action,args)
        if key.startswith("backburner:"):return self._backburner_control(key.split(":",1)[1],action)
        if key.startswith("voice:"):
            if action!="stop":raise ValueError("Для текущей голосовой задачи доступна остановка.")
            return self.service.work.cancel(key.split(":",1)[1])
        if key.startswith("dispatch:"):
            if action!="stop":raise ValueError("Running CLI tasks can be stopped; only schedules can be paused/resumed")
            if key not in {j["id"] for j in self.snapshot()["jobs"] if j["kind"]=="dispatch"}:
                raise ValueError("No matching running Butler task")
            output=command(str(self.workspace/"bin/butler-dispatch"),"kill",key.split(":",1)[1])
            return {"ok":True,"message":"Остановка задачи запрошена; обновите состояние."}
        job=next((j for j in self.cron_jobs() if j.get("id")==key),None)
        if job is None:raise ValueError("Only an existing Butler job can be controlled")
        if action in {"pause","resume"}:
            command(self.cli,"cron","disable" if action=="pause" else "enable",key,"--timeout","8000")
        elif action=="run":
            value=json.loads(command(self.cli,"cron","run",key,"--timeout","8000"))
            if value.get("ok") is not True or not (value.get("ran") or value.get("enqueued")):
                return {"ok":False,"message":"Запуск не принят. Обновите состояние задания; повтор автоматически не выполняется."}
        elif action=="stop":
            command(self.cli,"cron","disable",key,"--timeout","8000")
            if not job.get("state",{}).get("runningAtMs"):
                return {"ok":True,"message":"Расписание выключено; активного запуска нет."}
            if job.get("sessionTarget")!="isolated":
                return {"ok":True,"message":"Расписание выключено. Общий главный сеанс не прерывается."}
            base="agent:butler:cron:"+key
            # Cron runs use :run:<session-id>; discover matching session keys
            # without requesting message/transcript contents.
            sessions=json.loads(command(self.cli,"sessions","--agent","butler","--limit","all","--json"))
            values=sessions.get("sessions",[]) if isinstance(sessions,dict) else sessions
            matches=[r for r in values if isinstance(r.get("key"),str) and r["key"].startswith(base+":run:")]
            session=max(matches,key=lambda r:r.get("updatedAt",0))["key"] if matches else base
            command(self.cli,"gateway","call","sessions.abort","--json","--params",json.dumps({"key":session,"agentId":"butler"}),"--timeout","8000")
        elif action=="update":
            parameters=[]
            if "cron" in args:
                if not isinstance(args["cron"],str) or len(args["cron"].split()) not in {5,6} or len(args["cron"])>150:
                    raise ValueError("valid cron expression required")
                parameters += ["--cron",args["cron"]]
            if "timezone" in args:
                from zoneinfo import ZoneInfo
                try:ZoneInfo(args["timezone"])
                except Exception:raise ValueError("valid timezone required") from None
                parameters += ["--tz",args["timezone"]]
            if "model" in args:
                from .components import agent_status
                allowed = agent_status(self.service)["allowed"]
                if getattr(self.service,"catalog",None) is not None:
                    from .catalog import agent_choices
                    allowed = agent_choices(self.workspace,allowed)
                if args["model"] not in allowed:raise ValueError("choose a discovered Butler model")
                parameters += ["--model",args["model"]]
            if not parameters:raise ValueError("no supported schedule setting supplied")
            command(self.cli,"cron","edit",key,*parameters,"--timeout","8000")
        return {"ok":True,"message":{"run":"Запуск поставлен в очередь.","pause":"Будущие запуски на паузе.","resume":"Расписание включено.","stop":"Остановка текущего запуска запрошена; расписание выключено.","update":"Настройки задания сохранены."}[action]}

    def _backburner_control(self,slug,action):
        row=next((v for v in self.backburner() if v["slug"]==slug),None)
        if row is None:raise ValueError("Only an existing Back Burner folder can be controlled")
        name=row["name"]
        cli=str(self.workspace/"bin/butler-backburner")
        if action=="update":raise ValueError("Расписание Back Burner здесь не редактируется: приоритет и статус задаются в _brief.md папки.")
        if action=="run":return self._backburner_run(name,row["status"])
        command(cli,"resume" if action=="resume" else "pause",name)
        return {"ok":True,"message":{"resume":"Папка активна; ночной запуск зависит от включённой автоматики ядра.","pause":"Папка на паузе: ночной цикл её пропустит.",
            "stop":"Папка на паузе. Уже идущий прогон дойдёт до конца: он оформляет результат целиком."}[action]}

    def _backburner_run(self,name,status):
        """One folder now, as a tracked butler-core operation: the core starts the cycle and follows it to its result.

        A manual run ignores the folder's pause, so the button asks for an explicit «Продолжить» first (owner's choice)."""
        if status!="active":
            state={"paused":"на паузе","done":"отмечена завершённой"}.get(status,"без статуса в брифе")
            return {"ok":False,"message":"Папка "+state+": сначала нажмите «Продолжить» (папка снова станет активной). Тогда запуск и ночной цикл её возьмут."}
        terminal="Из терминала: ./bin/butler-backburner run \""+name+"\" (идёт до 45 минут)."
        core=core_client(self.workspace)
        if core is None:return {"ok":False,"message":"Ядро Butler не установлено. "+terminal}
        cycle=dt.datetime.now(ZONE).strftime("bb-%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:6]
        try:
            # The core takes a target only as an id (letters, digits, :._@/+=-): a name with a space or Cyrillic is refused,
            # so the folder travels in the parameters and the target is a short hash of its name.
            target="backburner:"+hashlib.sha256(name.encode()).hexdigest()[:12]
            op=core.Client().execute("backburner.cycle","butler",target,{"cycle_id":cycle,"only":name},
                                     "backburner:manual:"+cycle,principal="voice:owner",timeout=60)
        except core.CoreUnavailable:return {"ok":False,"message":"Ядро Butler недоступно: запуск не принят. Повторите через минуту. "+terminal}
        except core.CoreError as error:return {"ok":False,"message":"Ядро Butler не приняло запуск: "+str(error)[:120]+". "+terminal}
        if op.get("state") in ("running","requested","dispatching"):
            return {"ok":True,"message":"Прогон «"+name+"» запущен в ядре. Он идёт до 45 минут; короткая заметка придёт по итогу."}
        return {"ok":False,"message":"Ядро Butler не запустило прогон: "+str(op.get("reason") or op.get("state"))[:120]}

    def _builtin(self,key,action,args):
        if key not in {"daily","advice","analysis"}:raise ValueError("unknown built-in job")
        s=self.service
        setting={"daily":"daily_paused","advice":"muted","analysis":"analysis_paused"}[key]
        if action=="update":
            if key not in {"daily","advice"} or not ({"daily_time","telegram_enabled"} & args.keys()):
                raise ValueError("no supported setting supplied")
            if "telegram_enabled" in args and type(args["telegram_enabled"]) is not bool:
                raise ValueError("telegram_enabled must be boolean")
            if "daily_time" in args:
                if key!="daily":raise ValueError("only the daily summary time is editable here")
                s.control("preferences",{"daily_time":args["daily_time"]})
            if "telegram_enabled" in args:
                s.store.set("schedule_telegram_"+key,args["telegram_enabled"])
        elif action in {"pause","stop","resume"}:
            with s.lock:
                s.store.set(setting,action!="resume")
                if key=="analysis":
                    s.last_analysis=s.clock()
                    if action in {"stop","pause"}:s._discard_pending()
                    if action=="stop":
                        s.analysis_run_token.set()
                        s.analyzer.cancel()
                if action=="stop" and key in self.tokens:self.tokens[key].set()
                if action=="resume" and key in self.tokens:self.tokens[key]=threading.Event()
        elif action=="run":
            if self.manual.get(key)=="running":raise ValueError("this job is already running")
            policy=s.archive if key=="analysis" and s.archive.enabled else s.gate
            permitted = policy.permits(s.clock())
            if not permitted and key in {"daily","advice"}:
                from .recordings import history_visible
                permitted = history_visible(s)
            if not permitted:raise ValueError("observation permission is paused")
            if key=="analysis":
                try:s.pending.put_nowait(("archive-request" if s.archive.enabled else "owner-request",policy.generation))
                except Exception:raise ValueError("analysis is already queued") from None
                return {"ok":True,"message":"Локальный анализ поставлен в очередь."}
            from .conversation import Tokens
            from .delivery import quiet
            from .daily_report import daily_report, advice_report
            token=threading.Event();self.tokens[key]=token;self.manual[key]="running"
            policy_token = s.analysis_token if s.gate.permits(s.clock()) else s.archive.token
            def run():
                try:
                    now=s.clock();local=dt.datetime.fromtimestamp(now,ZONE)
                    if key=="daily":
                        text=daily_report(s,now,Tokens(token,policy_token))
                    else:
                        text=advice_report(s,now,Tokens(token,policy_token))
                    s.display_transport.send(text,token=Tokens(token,policy_token),key="manual:"+uuid.uuid4().hex,
                                             kind=key,allow_telegram=not quiet(s.clock()),
                                             require_local_history=True)
                    self.manual[key]="done"
                except Exception:self.manual[key]="failed"
            threading.Thread(target=run,daemon=True).start()
            return {"ok":True,"message":"Готовлю результат для Display."}
        return {"ok":True,"message":"Настройка локального задания применена."}

    def stop(self):
        for token in list(self.tokens.values()):token.set()
        self.service.analysis_run_token.set()
