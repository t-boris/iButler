# AI Butler News Video

Локальный конвейер для ежедневных новостных и отдельных deep-research видео:

```text
готовые дайджесты/отчёты → evidence-linked episode.json → смысловые карточки
→ нейроголос edge-tts/субтитры → FFmpeg H.264/AAC → проверка → Telegram + credits.md
```

Veo не участвует в доказательной части. Он опционален только для декоративного
интро/аутро и всегда имеет графический fallback.

## Основные команды

```sh
./bin/butler-news-video doctor
./bin/butler-news-video validate <episode.json>
./bin/butler-news-video veo-plan <episode.json>
./bin/butler-news-video render <episode.json> --tts edge
./bin/butler-news-video publish <run-dir> --dry-run
```

Фактическая публикация требует `--approve-delivery`. OpenAI TTS и Veo требуют
отдельный явный approval и конечный `--max-usd`; обычный cron их не вызывает.

Голос по умолчанию — бесплатный нейроголос edge-tts (`ru-RU-DmitryNeural`,
без API-ключа и биллинга; `--voice ru-RU-SvetlanaNeural` — женский вариант).
При сбое сети рендер автоматически откатывается на офлайновую macOS Milena,
затем на caption-only с предупреждением — выпуск не теряется. `--tts macos`
принудительно включает полностью офлайновый путь.

Чтобы голос звучал менее механически, narration может содержать
`[pause:0.3]`, `[breath]` и короткие смысловые абзацы — рендер уважает эти
маркеры и вставляет паузы между фразами (для обоих голосовых движков).

Карточки объясняют материал визуально: `visual.diagram` (bars/line/donut/
timeline/flow/compare) рендерится настоящим графиком с честными шкалами,
`visual.stat` (`{value, label}`) — крупной цифрой-якорем, `visual.points`
(2–4 конкретных факта) — маркированным списком. Сверху — индикатор
прогресса выпуска (N/M).

Сцены рендерятся на живом фоне: тема выбирает стиль движения (аврора /
океан / звёзды / дым), лупы генерируются детерминированно (PIL + FFmpeg
minterpolate) и кэшируются в `assets/motion-cache/`. В блоке «ГОЛОС» —
волна аудиодорожки акцентного цвета; контент появляется с fade-in. При
любом сбое движка сцена откатывается на статичную карточку.

## Профили

- `daily`: брифинг 2 раза в день из 3 последних дайджестов, непокрытых
  завершённых исследований (`covered-research` гарантирует: отчёт попадает в
  видео ровно один раз), событий Daily Note и проектных chat-digest материалов.
- `deep-research`: отдельное видео из одного уже завершённого отчёта без
  повторного исследования.
- `lecture`: по запросу Бориса — объясняющая лекция 10–20 минут по любой теме
  из базы знаний, по отчёту или по целой папке (главы, повторы-резюме, выводы).

## Расписание

Два крона: `news-video-morning` (05:00 America/Chicago,
`state/news-video-daily-cron-prompt.txt`, publish с `--allow-quiet-hours` —
явное разрешение Бориса от 2026-07-17) и `news-video-evening` (17:00,
`state/news-video-evening-cron-prompt.txt`). Повторный запуск использует тот же
`RUN_ID`, а `publish/telegram.json` предотвращает повторную отправку.

## Проверка

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /opt/miniconda3/bin/python -m unittest discover -s tests -v
```

Тесты проверяют evidence links, traversal, безопасный overwrite, stale-run
защиту, budget gates, актуальную resolution-aware оценку Veo, quiet hours,
checksums и идемпотентную публикацию.
