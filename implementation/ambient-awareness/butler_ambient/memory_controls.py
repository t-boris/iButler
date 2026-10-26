"""Owner-local voice adapters for existing observation and image controls.

Only metadata enters the planner. Raw recordings and transcripts never do.
Mutations are reviewed and confirmed by VoiceCommands before reaching here.
"""
from __future__ import annotations

import datetime as dt
import errno
import math
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from .summaries import summarize


ZONE = ZoneInfo("America/Chicago")
CAPABILITIES = {
    "recordings": "Последние записи и наблюдения",
    "summary": "Сводка наблюдений за выбранный период",
    "preserve": "Сохранить выбранный оригинал отдельно от временного буфера",
    "delete": "Удалить выбранный оригинал и, по явной просьбе, результаты анализа",
    "correct": "Исправить имя объекта в наблюдениях",
    "materials": "Папки поиска личных фотографий и рисунков",
    "material-add": "Добавить выбранную папку или изображение в поиск",
    "material-remove": "Убрать папку или изображение из поиска",
    "images": "Состояние подготовки изображений и последние результаты",
    "image-delete": "Удалить выбранный результат изображения",
    "image-cancel": "Отменить подготовку изображения",
    "screen-permission": "Открыть системное разрешение записи экрана",
    "video-settings": "Открыть системные настройки камеры",
}
READ_ONLY = {"recordings", "summary", "materials", "images"}
SOURCES = {"camera": "камера", "microphone": "микрофон", "screen": "экран", "system-audio": "системный звук"}
ACTIVITIES = {"work": "работа", "browsing": "просмотр сайтов", "youtube": "YouTube",
              "rest": "отдых", "reading": "чтение", "entertainment": "развлечения", "unknown": "неопределённая деятельность"}


class MemoryControls:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def _visual(self):
        value = self.service.visual
        if value is None: raise ValueError("Изображения сейчас недоступны.")
        return value

    def _media(self, key):
        if not isinstance(key, str): raise ValueError("Уточните одну существующую запись.")
        rows = self.store.execute("SELECT * FROM media WHERE id=?", (key,))
        if not rows: raise ValueError("Выбранная запись не найдена.")
        return rows[0]

    def _image(self, key):
        self._visual()
        if not isinstance(key, str): raise ValueError("Уточните один существующий результат изображения.")
        rows = self.store.execute("SELECT id,kind,source_time FROM visual_artifacts WHERE id=?", (key,))
        if not rows: raise ValueError("Выбранное изображение не найдено.")
        return rows[0]

    @staticmethod
    def _stamp(value):
        return dt.datetime.fromtimestamp(value, ZONE).strftime("%d.%m.%Y %H:%M:%S")

    def _label(self, row):
        return SOURCES[row["source"]] + ", " + self._stamp(row["start"]) + ", номер " + row["id"][-6:]

    def _subjects(self):
        return self.store.execute("""SELECT DISTINCT json_extract(f.value,'$.subject') AS subject
            FROM (SELECT facts,start FROM observations ORDER BY start DESC LIMIT 100) o, json_each(o.facts) f
            WHERE json_type(f.value,'$.subject')='text'
            AND json_extract(f.value,'$.subject')<>'' ORDER BY o.start DESC LIMIT 12""")

    def _period(self, args):
        if set(args) == {"minutes"}:
            value = args["minutes"]
            if type(value) is not int or not 1 <= value <= 366 * 1440:
                raise ValueError("Нужен период от одной минуты до 366 дней.")
            end = self.service.clock()
            return end - value * 60, end
        if set(args) != {"start", "end"}: raise ValueError("Уточните период сводки: начало и конец или число минут.")
        values = []
        for key in ("start", "end"):
            value = args[key]
            if not isinstance(value, str): raise ValueError("Начало и конец нужны как дата и время с часовым поясом.")
            try: parsed = dt.datetime.fromisoformat(value)
            except ValueError: raise ValueError("Не удалось определить дату и время сводки.") from None
            if parsed.tzinfo is None: raise ValueError("Для времени сводки нужен часовой пояс.")
            values.append(parsed.timestamp())
        start, end = values
        if not all(math.isfinite(x) for x in values) or not 0 < end - start <= 366 * 86400:
            raise ValueError("Нужен положительный период не длиннее 366 дней.")
        return start, end

    def context(self):
        records = self.store.execute("""SELECT id,source,start,end,path IS NOT NULL AS original,
            preserved IS NOT NULL AS preserved FROM media WHERE complete=1 ORDER BY start DESC,id DESC LIMIT 12""")
        result = {"now": dt.datetime.fromtimestamp(self.service.clock(), ZONE).isoformat(),
            "timezone": str(ZONE), "recordings": records, "subjects": self._subjects(), "materials": [], "images": [], "image_work": {}}
        if self.service.visual is not None:
            result["materials"] = self.service.visual.roots()[:12]
            result["images"] = self.store.execute("SELECT id,kind,source_time FROM visual_artifacts ORDER BY created DESC,id DESC LIMIT 8")
            result["image_work"] = {k:self.service.visual.state.get(k) for k in ("status", "request_id")}
        return result

    def normalize(self, capability, args):
        if capability == "material-add" and isinstance(args.get("path"), str):
            try: return {**args, "path": str(Path(args["path"]).expanduser().resolve(strict=True))}
            except (OSError, RuntimeError): raise ValueError("Указанный путь не найден.") from None
        return args

    def validate(self, capability, args):
        allowed = {"recordings": {"source", "limit"}, "summary": {"minutes", "start", "end"},
            "preserve": {"id"}, "delete": {"id", "results"}, "correct": {"subject", "name"},
            "material-add": {"path"}, "material-remove": {"path"}, "image-delete": {"id"}, "image-cancel": {"id"}}
        if set(args) - allowed.get(capability, set()): raise ValueError("Неизвестный параметр команды.")
        if capability == "recordings":
            if type(args.get("limit", 5)) is not int or not 1 <= args.get("limit", 5) <= 12:
                raise ValueError("Можно перечислить от одной до двенадцати последних записей.")
            if "source" in args and args["source"] not in SOURCES: raise ValueError("Неизвестный источник записи.")
        if capability == "summary": self._period(args)
        if capability in {"preserve", "delete"}:
            row = self._media(args.get("id"))
            if capability == "delete" and type(args.get("results")) is not bool:
                raise ValueError("Уточните: удалить только оригинал или также результаты анализа?")
            if capability == "delete" and not (row["path"] or row["preserved"] or args["results"] and self.store.execute(
                    "SELECT 1 FROM observation_media WHERE media_id=? LIMIT 1", (row["id"],))):
                raise ValueError("Выбранная запись уже удалена.")
            if capability == "preserve":
                if not row['complete']:raise ValueError('Эта запись ещё идёт. Выберите завершённую запись.')
                available = row["preserved"] or (row["path"] if row["start"] > self.service.clock() - 86400 else None)
                if not available or not Path(available).is_file(): raise ValueError("Оригинал недоступен: удалён после анализа или истёк срок временного хранения.")
        if capability == "correct":
            name, subject = args.get("name"), args.get("subject")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
                raise ValueError("Уточните имя объекта, до 80 символов.")
            if not isinstance(subject, str) or not self.store.execute("""SELECT 1 FROM observations o,json_each(o.facts) f
                    WHERE json_extract(f.value,'$.subject')=? LIMIT 1""", (subject,)):
                raise ValueError("Уточните существующий объект наблюдения.")
        if capability in {"materials", "material-add", "material-remove", "images", "image-delete", "image-cancel"}:
            visual = self._visual()
            if capability == "material-add":
                from .visual import IMAGE_SUFFIXES
                path = args.get("path")
                if not isinstance(path, str): raise ValueError("Уточните существующий путь к папке или изображению.")
                p = Path(path)
                try: valid = p.resolve(strict=True) == p and (p.is_dir() or p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
                except (OSError, RuntimeError): valid = False
                if not valid: raise ValueError("Путь изменился или не является папкой/изображением. Повторите запрос.")
            if capability == "material-remove" and args.get("path") not in visual.roots():
                raise ValueError("Этот путь не добавлен в поиск изображений.")
            if capability == "image-delete": self._image(args.get("id"))
            if capability == "image-cancel" and (not isinstance(args.get("id"), str)
                    or visual.state.get("request_id") != args["id"] or visual.state["status"] not in {"queued", "working"}):
                raise ValueError("Эта подготовка изображения уже завершена или сменилась. Повторите запрос.")

    def review(self, capability, args):
        if capability == "preserve": return "Сохранить отдельно от временного буфера оригинал: " + self._label(self._media(args["id"]))
        if capability == "delete":
            return "Удалить оригинал и сохранённую копию: " + self._label(self._media(args["id"])) + (
                "; также удалить связанный анализ" if args["results"] else "; результаты анализа останутся")
        if capability == "correct": return "Для объекта «" + args["subject"] + "» сохранить подтверждённое вами имя «" + args["name"] + "». Это исправление наблюдений; согласие на запись оформляется отдельно"
        if capability == "material-add": return "Добавить в поиск личных изображений: " + args["path"]
        if capability == "material-remove": return "Убрать из поиска изображений: " + args["path"] + "; оригиналы останутся на месте"
        if capability == "image-delete":
            image = self._image(args["id"])
            return "Удалить результат изображения от " + self._stamp(image["source_time"]) + ", номер " + image["id"][-6:] + (
                "; созданный Butler файл будет удалён" if image["kind"] == "generated" else "; исходный файл останется")
        if capability == "image-cancel": return "Остановить подготовку изображения, номер " + args["id"][-6:]
        return {
            "screen-permission": "Открыть системный запрос разрешения записи экрана для Butler",
            "video-settings": "Открыть системные настройки камеры Butler"}[capability]

    def read(self, capability, args):
        if capability == "recordings":
            where, params = ("WHERE complete=1 AND source=?", [args["source"]]) if "source" in args else ("WHERE complete=1", [])
            rows = self.store.execute("SELECT * FROM media " + where + " ORDER BY start DESC,id DESC LIMIT ?", (*params, args.get("limit", 5)))
            return ("Последние записи: " + "; ".join(self._label(r) + (
                ", сохранена отдельно" if r["preserved"] else ", оригинал недоступен" if not r["path"] else "") for r in rows)) if rows else "Записей пока нет."
        if capability == "materials":
            roots = self._visual().roots()
            return "Пути поиска изображений: " + "; ".join(roots[:12]) if roots else "Папки изображений ещё не выбраны."
        if capability == "images":
            visual = self._visual()
            count = self.store.execute("SELECT COUNT(*) AS n FROM visual_artifacts")[0]["n"]
            return "Подготовка изображения: " + {"idle": "ожидает запроса", "queued": "в очереди", "working": "выполняется", "ready": "готово", "failed": "ошибка", "cancelled": "отменена"}.get(visual.state["status"], visual.state["status"]) + ". Результатов: " + str(count) + "."
        start, end = self._period(args)
        data = summarize(self.store, start, end)
        minutes = "; ".join(ACTIVITIES.get(k, k) + ": около " + str(round(v / 60, 1)) + " минут" for k, v in data["approximate_seconds"].items() if v)
        topics = [" ".join(t.split())[:80] for t in data["work_topics"][:3]]
        return ("Сводка с " + self._stamp(start) + " по " + self._stamp(end) + ". " + (minutes + ". " if minutes else "Деятельность не определена. ")
            + ("Возможные темы: " + "; ".join(topics) + ". " if topics else "")
            + "Покрытие наблюдениями " + str(round(data["coverage_fraction"] * 100)) + " процентов; без наблюдения около "
            + str(round((end - start - data["covered_seconds"]) / 60, 1)) + " минут. Оценки приблизительные, по " + str(len(data["evidence"])) + " наблюдениям.")

    def execute(self, capability, args):
        try: return self._execute(capability, args)
        except OSError as error:
            return {"ok": False, "message": "Операция не завершена: " + (
                "недостаточно места на диске." if error.errno == errno.ENOSPC else "локальный файл или процесс недоступен.")}
        except sqlite3.Error:
            return {"ok": False, "message": "Операция не завершена: ошибка локальной базы данных."}

    def _execute(self, capability, args):
        s = self.service
        if capability in {"preserve", "delete", "correct"}:
            result = s.control(capability, args)
            key = {"preserve": "preserved", "delete": "deleted", "correct": "corrected"}[capability]
            expected = args["subject"] if capability == "correct" else args["id"]
            if capability == "delete":
                message = "Выбранная запись удалена" + (" вместе со связанным анализом." if args["results"] else "; результаты анализа сохранены.")
            else: message = {"preserve": "Выбранный оригинал сохранён отдельно.", "correct": "Исправление наблюдений сохранено."}[capability]
            return {"ok": result.get(key) == expected, "message": message}
        if capability == "material-add": self._visual().add_root(args["path"])
        elif capability == "material-remove": self._visual().remove_root(args["path"])
        elif capability == "image-delete": self._visual().delete(args["id"])
        elif capability == "image-cancel": self._visual().cancel()
        elif capability == "screen-permission":
            result = s.request_screen_permission()
            return {"ok": result["screen_permission"], "message": "Доступ к экрану разрешён." if result["screen_permission"] else "Mac не разрешил запись экрана. Доступ можно включить в системных настройках конфиденциальности."}
        elif capability == "video-settings":
            from . import processes as subprocess
            helper = getattr(s.inputs, "camera_helper", None)
            if helper is None: raise ValueError("Системные настройки камеры недоступны.")
            process = subprocess.Popen([str(helper), "--video-effects"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import threading
            threading.Thread(target=process.wait, daemon=True).start()
        return {"ok": True, "message": {"material-add": "Путь добавлен в поиск изображений.", "material-remove": "Путь убран из поиска; оригиналы сохранены.",
            "image-delete": "Выбранный результат изображения удалён.", "image-cancel": "Подготовка изображения остановлена.", "video-settings": "Системные настройки камеры открываются."}[capability]}
