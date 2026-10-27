# Инвентарь документации и доказательств

Срез: 2026-10-26. Все 45 файлов исходной feature-папки включены в [машиночитаемый инвентарь](source_index.json) с SHA-256 и числом строк. Папка прочитана целиком в ходе этой задачи; в архитектурном проходе повторно сверены решения, ответы, Resolution, discussion и реализация голосовой цепочки. Инвентарь фиксирует состав, а сам анализ подтверждается ссылками на конкретные строки кода.

## Приоритет источников

1. Ответ владельца и принятые решения, включая последующие изменения прежнего решения.
2. Ответы Q-006 и Q-001–Q-005 в discussion; именно Answer, а не Recommended.
3. Resolution F-001/F-002; исходные формулировки находок сохранены как история.
4. Требования и implementation plan с учётом этих решений.
5. Реальный установленный код и журналы — доказательство фактического поведения; исходники кандидата отдельно.
6. README и старые комментарии — справочные материалы; обнаруженные устаревания указаны в 06_contradictions.md.

High = прямое свидетельство кода/записи/команды. Medium = обоснованный механизм риска без доказанного проявления в каждом эпизоде. Low = гипотеза, которую нельзя использовать как установленную причину. Статус approved у требования означает согласование, а не выполнение. Отчёт тестов доказывает только выполненные проверки.

## Полный состав feature-папки

| Файл | Название / назначение | Тип / статус | Дата в записи | Строк |
| --- | --- | --- | --- | --- |
| [decisions/DEC-001.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-001.md) | Rebuild the dialogue layer on Pipecat with Smart Turn v3; the camera stays continuous and local | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-039.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-039.md) | Without an identity lease, unnamed speech gets a general-scope answer unless the owner is busy; the dialogue gets its own model instance; transcripts are gated on Whisper's confidence | decision / accepted | 2026-10-26 | 34 |
| [decisions/DEC-063.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-063.md) | Second-person speech with nobody around is addressed to Butler; naming Butler overrides an unsure judge | decision / accepted | 2026-10-26 | 34 |
| [decisions/DEC-064.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-064.md) | Spoken busy and free cues; one configured Ollama service; the dialogue shares the runner with two parallel slots | decision / accepted | 2026-10-26 | 34 |
| [decisions/DEC-065.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-065.md) | One Butler session across private and general modes; vision verdicts corroborated and never blocking | decision / accepted | 2026-10-26 | 36 |
| [decisions/DEC-066.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-066.md) | «Один дома»: a sticky owner session instead of a 2.5-second lease; household member; review-and-yes confirmation | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-067.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-067.md) | Butler's voice brain is the OpenClaw butler agent; the Mac keeps ears, eyes, voice and the addressee decision | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-068.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-068.md) | Calls are recorded as before and summarized into conclusions after they end | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-069.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-069.md) | Request-aware acknowledgement while the agent works; vision veto limited to its own verdict; session follow-ups skip the local judge | decision / accepted | 2026-10-26 | 36 |
| [decisions/DEC-070.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-070.md) | Interruption is decided on recognised words from the verified owner; committed turns are protected by state, not timers (from turn-taking DEC-063) | decision / accepted | 2026-10-26 | 44 |
| [decisions/DEC-071.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-071.md) | Canonical rules in the existing vault | decision / accepted | 2026-10-26 | 34 |
| [decisions/DEC-072.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-072.md) | Allow attributable unnamed follow-ups in an existing Butler session during OWNER_BUSY | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-073.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-073.md) | Isolate local dialogue inference in a dedicated Ollama service | decision / accepted | 2026-10-26 | 36 |
| [decisions/DEC-074.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-074.md) | Implement locally; Git prerequisite waived for this feature | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-075.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-075.md) | Reload applicable rules through the agent's existing memory tools | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-076.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-076.md) | Any Butler-name mention overrides an unsure negative judge | decision / accepted | 2026-10-26 | 35 |
| [decisions/DEC-077.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-077.md) | Owner can override automatic busy/free state until the situation changes | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-078.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-078.md) | Verify real end-to-end understanding against the owner's quality assessment | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-079.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-079.md) | Investigate OpenAI speech transcription rather than Wispr Flow | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-080.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-080.md) | Allow owner-audio transcription fallback when local ASR cannot understand | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-081.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-081.md) | Create a new OpenAI API key through secure provisioning | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-082.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-082.md) | Capture speech as a continuous utterance with visible draft and final text | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-083.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-083.md) | Start passive local recognition after 300 ms of speech | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-084.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-084.md) | No spoken waiting acknowledgement for ordinary conversation | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-085.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-085.md) | Prompt meaningful reaction when a substantive answer needs time | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-086.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-086.md) | One continuous owner dialogue preserves context, work and pending questions | decision / accepted | 2026-10-26 | 32 |
| [decisions/DEC-087.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-087.md) | Explain and review the complete voice architecture before further implementation | decision / accepted | 2026-10-26 | 32 |
| [discussion.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/discussion.md) | discussion | служебный / не указан | Not stated in source | 183 |
| [findings/F-001.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/findings/F-001.md) | Dedicated dialogue instance contradicts the settled shared runner | finding / resolved | 2026-10-26 | 38 |
| [findings/F-002.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/findings/F-002.md) | Busy-session follow-ups have incompatible admission rules | finding / resolved | 2026-10-26 | 46 |
| [implementation/plan.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/implementation/plan.md) | Butler conversation: attention, turn-taking and feedback learning | plan / не указан | 2026-10-26 | 127 |
| [overview.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/overview.md) | Butler conversation: attention, turn-taking and learning from feedback | feature / implementing | 2026-10-26 | 61 |
| [questions/Q-006.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/questions/Q-006.md) | DEC-063 limits the inflected-name override to second-person speech while alone and restores ordinary judging with company. REQ-056 criterion | question / answered | 2026-10-26 | 50 |
| [references/SRC-001.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/references/SRC-001.md) | Dialogue feedback and learning — full design (merged feature) | source / не указан | 2026-10-26 | 32 |
| [references/SRC-002.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/references/SRC-002.md) | Turn-taking: when to stop speaking and when to start (merged feature) | source / не указан | 2026-10-26 | 24 |
| [requirements/REQ-056.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-056.md) | Attention, addressee and private-session continuity | requirement / approved | 2026-10-26 | 83 |
| [requirements/REQ-057.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-057.md) | Reliable conversational turn-taking | requirement / approved | 2026-10-26 | 79 |
| [requirements/REQ-058.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-058.md) | Voice answers and local call conclusions | requirement / approved | 2026-10-26 | 67 |
| [requirements/REQ-059.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-059.md) | Traceable corrections and immediate repair | requirement / approved | 2026-10-26 | 37 |
| [requirements/REQ-060.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-060.md) | Durable rules and owner control | requirement / approved | 2026-10-26 | 35 |
| [requirements/REQ-061.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-061.md) | Learning evidence, evaluation and rollback | requirement / approved | 2026-10-26 | 34 |
| [requirements/REQ-062.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-062.md) | Authorized and private feedback learning | requirement / approved | 2026-10-26 | 38 |
| [requirements/REQ-063.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-063.md) | Conversation continuity during learning failures | requirement / approved | 2026-10-26 | 30 |
| [requirements/REQ-064.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-064.md) | Manual availability overrides automatic busy detection until the situation changes | requirement / approved | 2026-10-26 | 31 |
| [requirements/REQ-065.md](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-065.md) | Continuous phrase capture and observable transcript revisions | requirement / approved | 2026-10-26 | 34 |

## Уже отвеченное — не спрашивать повторно

| Запись | Обязательное решение |
| --- | --- |
| F-001 Resolution, DEC-073 | Отдельный процесс/endpoint локального диалога, не переименование общей модели |
| F-002 Resolution, DEC-072 | BUSY: имя для начала, положительно атрибутированное продолжение без имени допустимо |
| Q-001–Q-005, discussion | Butler/ в существующем vault; весь доступный vault с исключениями; 24 ч / 30 дней; без feedback-аудиопримеров; только обезличенная cloud-сводка feedback |
| DEC-074 | «skip github»: Git-предпосылка этой задачи снята |
| DEC-075 | Перечитывать правила существующим memory tool |
| Q-006 Answer, DEC-076 | Любое упоминание имени в падежах, в том числе третье лицо/компания, перевешивает неуверенное отрицание judge ниже 0.9; права сохраняются |
| DEC-077 / REQ-064 | Auto / Busy / Free; ручное решение до подтверждённой смены ситуации |
| DEC-078 | Живое понимание неудовлетворительно; минимум 80/100 не доказан |
| DEC-079–081 | OpenAI Whisper / Realtime; ограниченный audio fallback; защищённое создание нового ключа |
| DEC-082 / REQ-065 | Непрерывный input, drafts/final с общей идентичностью; draft не выполняет действие |
| DEC-083 | 300 мс для admission, последующие проверки остаются |
| DEC-084 → DEC-085 | Без повторяющегося «сейчас посмотрю»; последняя поправка разрешает одну быструю правдивую реакцию при задержке содержательного ответа |
| DEC-086 | Один постоянный диалог, контекст работы/вопроса через паузы и сегментацию |
| DEC-087 | Сначала полный архитектурный разбор; до него не продолжать код и выкладку |

Все строки таблицы ссылаются на одноимённые файлы в полном инвентаре выше; Q-001–Q-005 находятся в discussion.md, а не в отдельных файлах текущей папки. Ответы уже применены к требованиям; повторных вопросов по ним нет.

## Реализация и эксплуатационные источники

- Ambient: `service`, `sensing`, native `audio`, `listening`, `transcript`, `transcriber`, `endpointing`, `policy`, `attention`, `meeting`, `conversation`, `brain`, `voice_worker`, `voice_commands`, `local_ui`, `feedback`, `archive`, `calls`, `store`.
- Core: `learning.py`, `rules.py`; адаптеры памяти агента и релизные отчёты.
- README обоих пакетов и SemVer-правило modular-core DEC-044.
- [Runtime snapshot и source hashes](runtime_evidence.json), [traceback](playback-crash.txt), [команда сбора](collect_evidence.py).
- [Полная матрица 0.6.0](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification-0.6.0.json), [0.6.3](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification-0.6.3.json), [кандидат 0.6.4](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification.json), [последняя установка](../../implementation/ambient-awareness/evidence/attention-implementation/deployment.json).
- [Capture window](../../implementation/ambient-awareness/evidence/attention-implementation/capture-runtime-0.6.2.json), [разрывы/отмены](../../implementation/ambient-awareness/evidence/attention-implementation/continuation-runtime-before-0.6.2.json), [native agent audit](../../implementation/ambient-awareness/evidence/attention-implementation/native-agent-log-review.json), [полнота vault](../../implementation/ambient-awareness/evidence/attention-implementation/vault-coverage-after.json).

Данные runtime — снимок во времени. В этот пакет не копируются разговоры, аудио, содержимое задач и ключи. Traceback содержит только техническую ошибку и пути. Внешние ссылки SRC-001/SRC-002 — происхождение исходного дизайна; новые заявления о текущих версиях внешних продуктов по ним не делаются. Доказательств акустической точности и целевых p95 в этих источниках нет.
