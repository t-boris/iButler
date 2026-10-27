# Расхождения спецификации, документации и кода

`RA-*` ниже — идентификаторы этого архитектурного отчёта, не переоткрытие разрешённых F-001/F-002. Факт несовпадения обычно имеет уверенность High; влияние на каждый конкретный голосовой эпизод не предполагается автоматически.

## Разрешённые расхождения — применить, не спрашивать снова

| Записи | Действующий исход |
| --- | --- |
| F-001 / DEC-064 → DEC-073 | Отдельный процесс и endpoint диалога. [Resolution](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/findings/F-001.md:36) |
| F-002 → DEC-072 | BUSY допускает положительно атрибутированное продолжение без имени. [Resolution](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/findings/F-002.md:44) |
| DEC-063 / Q-006 → DEC-076 | Расширенное упоминание имени, включая третье лицо/компанию, при сохранении прав. [Answer](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/questions/Q-006.md:48) |
| DEC-069 → DEC-084 → DEC-085 | Отказ от повторных filler; при задержке — одна быстрая правдивая реакция. [Последнее решение](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-085.md) |
| DEC-065 → DEC-086 | Recency не может сама закрывать непрерывный диалог. [Последнее решение](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-086.md) |

REQ-056/057/058/065 обновлены по DEC-085/086 в этом проходе. Исторические решения не переписаны задним числом.

## Сохраняющиеся расхождения и предложения

| ID | Что требуется / что реально | Результат и предложение |
| --- | --- | --- |
| RA-01 | DEC-086: один контекст. `brain.ask` после 2 s переключается на `:aside`; локальная история фильтруется по recency | Открыто. Убрать контекстный обход после введения task/dialogue coordinator. [brain.py:176](../../implementation/ambient-awareness/butler_ambient/brain.py:176), [DEC-086](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-086.md) |
| RA-02 | DEC-085: быстрая реакция. Private SSE копится до DONE, текущий fallback ожидания срабатывает через 60 s | Открыто. Потоковые события текста/реального статуса и одна политика delayed reaction. [brain.py:192](../../implementation/ambient-awareness/butler_ambient/brain.py:192), [conversation.py:1353](../../implementation/ambient-awareness/butler_ambient/conversation.py:1353) |
| RA-03 | REQ-057: завершение мысли. Рабочий endpointer — 0.45 s quiet; Pipecat Smart Turn только shadow | Частичная реализация, rollout сознательно удержан по discussion. Реальная калибровка и единый assembler; не включать модель вслепую. [listening.py:281](../../implementation/ambient-awareness/butler_ambient/listening.py:281), [runtime snapshot](runtime_evidence.json) |
| RA-04 | DEC-085/086: ответ на вопрос сохраняет смысл. Local pending, Session.question и agent history раздельны; back-channel не знает вопроса агента | Открыто. Один question/proposal object, сохранение контекста и scope. [voice_commands.py:125](../../implementation/ambient-awareness/butler_ambient/voice_commands.py:125), [conversation.py:782](../../implementation/ambient-awareness/butler_ambient/conversation.py:782) |
| RA-05 | Принятый запрос должен получить результат. Deferred output удерживается любым BUSY, дополняется «Готово» | Открыто. Разделить proactive notice и ожидаемый ответ; completion только по реальному исходу. [conversation.py:1460](../../implementation/ambient-awareness/butler_ambient/conversation.py:1460) |
| RA-06 | Живой диалог должен пережить сбой компонента. Установленный `_speak_deferred` убил `_run` исключением вывода | Исправление 0.6.4 проверено в source, не deployed; долгосрочно supervisor и независимый output lifecycle. [traceback](playback-crash.txt), [тесты](../../implementation/ambient-awareness/evidence/attention-implementation/playback-recovery-tests.log) |
| RA-07 | Reply contract допускает 1200 chars, TTS принимает 700 | Открыто. Согласованный chunked contract; без слепого обрезания предложения. [brain.py:238](../../implementation/ambient-awareness/butler_ambient/brain.py:238), [voice_worker.py:101](../../implementation/ambient-awareness/butler_ambient/voice_worker.py:101) |
| RA-08 | DEC-067/REQ-058 требуют local fallback + spoken notice при gateway failure/timeout. Timeout/agent-error возвращают фиксированную ошибку, другие ошибки вызывают local fallback без обязательного spoken notice | Открыто. Общая typed error policy: честно обозначить недоступность и ограниченные возможности локального ответа, не выдумывая исход инструментов. [conversation.py:1369](../../implementation/ambient-awareness/butler_ambient/conversation.py:1369), [REQ-058](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-058.md) |
| RA-09 | DEC-067 буквально указывает `openclaw agent ... --json`; source использует HTTP `/v1/chat/completions` | Не установлена отдельная запись принятия транспортной замены. Проверить установленный backend/эквивалентность tool/session контрактов, затем отразить в записи; не приписывать буквальное соответствие. [DEC-067](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-067.md), [brain.py:26](../../implementation/ambient-awareness/butler_ambient/brain.py:26) |
| RA-10 | REQ-065: полная фраза. Listening сегментирует >20 s с общим ID; альтернативный Conversation.feed ещё ограничивает/финализирует собственный буфер | Открыто для альтернативного режима. Перевести оба режима на один assembler, удалить дублирующий. [listening.py:286](../../implementation/ambient-awareness/butler_ambient/listening.py:286), [conversation.py:717](../../implementation/ambient-awareness/butler_ambient/conversation.py:717) |
| RA-11 | README описывает local-only/no-paid fallback, general mode как незавершённый, закрытие private на stale evidence | Устаревшая документация против DEC-066/067/080 и реального source. Переписать README после согласованного устройства компонентов; сейчас использовать этот source-backed dossier. [README:47,81,236](../../implementation/ambient-awareness/README.md:236), [инвентарь решений](00_document_inventory.md) |
| RA-12 | Overview утверждает pipeline Pipecat/Silero/SmartTurn. Реальная orchestration самописная; Pipecat внутри отдельного worker | Открытая точность описания. Указывать конкретные роли библиотеки, не всю систему как готовый framework pipeline. [overview:49](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/overview.md:49), [endpointing.py:129](../../implementation/ambient-awareness/butler_ambient/endpointing.py:129) |
| RA-13 | REQ-058 выводы только от оговорённых qualifying busy-эпизодов; `CALL_CAUSES` дополнительно включает other-voice и looking-away-talking | Потенциальное расширение классификации, High для кода, применимость к реальным эпизодам не проверена. Привести eligibility к binding DEC-068/REQ-058; не называть любой разговор звонком. [calls.py:25](../../implementation/ambient-awareness/butler_ambient/calls.py:25), [REQ-058](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-058.md) |
| RA-14 | Изоляция доказана для vision/mail/System3, но `Calls.conclude` использует `voice.model` (11435), а не фоновый клиент | Это ограничение доказательства и потенциальная конкуренция, не доказанное нарушение явного списка DEC-073. Указать маршрут честно; перенести эту фоновую работу при упорядочении очередей. [calls.py:115](../../implementation/ambient-awareness/butler_ambient/calls.py:115), [analysis.py:130](../../implementation/ambient-awareness/butler_ambient/analysis.py:130) |

## Не выдавать за противоречие

- 300 мс admission и 0.45 s quiet tail имеют разные назначения; проблема в том, кто владеет концом мысли, а не в различии чисел как таковом.
- 15 минут отсутствия владельца закрывают private authorization; это не равнозначно пятиминутному забыванию разговора.
- 24 часа временного аудиоархива и запрет отдельных feedback-аудиопримеров относятся к разным хранилищам.
- Потоковый TTS действительно существует, хотя private text до него буферизуется.

Основания и все ограничения этих различий приведены в [полной архитектуре](02_architecture_analysis.md).
