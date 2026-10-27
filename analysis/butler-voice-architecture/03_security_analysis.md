# Доступ, приватность и корректность действия

Это анализ конкретных границ голосовой цепочки, не новая процедура согласований.

## Facts From Source — факты, High

| Граница | Существующий контракт | Источник |
| --- | --- | --- |
| Локальный захват → приватный ответ | Разрешённое пассивное слышание не устанавливает личность и не даёт право на действие | [REQ-056](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-056.md), [service.py:202](../../implementation/ambient-awareness/butler_ambient/service.py:202) |
| General → private | Общий маршрут остаётся локальным, без приватной памяти и команд; owner session имеет отдельные условия открытия/закрытия | [policy.py:31](../../implementation/ambient-awareness/butler_ambient/policy.py:31), [DEC-066](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-066.md) |
| Private → agent | Только адресованная речь и согласованный краткий контекст/изображение по запросу; инструкция памяти отдельно от user content | [brain.py:154](../../implementation/ambient-awareness/butler_ambient/brain.py:154), [DEC-067](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-067.md), [DEC-075](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-075.md) |
| Локальное аудио → OpenAI | Отдельное узкое разрешение для owner-addressed fallback после неудачи local ASR; подключение отсутствует | [DEC-080](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-080.md), [проверка реализации](../../implementation/ambient-awareness/evidence/attention-implementation/specification-verification.md) |
| Предложение → действие | Конкретные параметры, digest, generation, pending confirmation; повторный текст не равен разрешению на другое действие | [voice_commands.py:294](../../implementation/ambient-awareness/butler_ambient/voice_commands.py:294) |
| Feedback → cloud | Только обезличенная сводка; не исходные эпизоды/аудио/выводы звонков | [discussion Q-004/Q-005](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/discussion.md:55), [REQ-062](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/requirements/REQ-062.md) |
| Сохранение → повторное применение | Тексты 24 ч / размеченные 30 дней; правила с ревизией, receipt и возможностью отзыва | [learning.py:86](../../implementation/butler-core/butler_core/learning.py:86), [rules.py:142](../../implementation/butler-core/butler_core/rules.py:142) |

## Problems / Risks и Proposals

| ID | Риск / доказательство | Уверенность | Конкретное предложение |
| --- | --- | --- | --- |
| SEC-1 | `deferred` содержит только время, request, text; `_cancel` и `_release_private` не очищают эту очередь, у элемента нет исходного authorization epoch. Текущая проверка перед выводом есть, но принадлежность результата прежней сессии явно не проверяется. [conversation.py:250,362,602,1338,1460](../../implementation/ambient-awareness/butler_ambient/conversation.py:1460) | High для структуры, Medium для последствий; доказанной утечки нет | Типизированный результат с task/dialogue/authorization ID; при Pause/revoke/company явно помечать недоставляемым и определять допустимое восстановление без автоматического устаревшего вывода |
| SEC-2 | Клиентский token проверяется между SSE-строками; чтение может блокироваться. Серверная отмена tool-run не подтверждается этим кодом. [brain.py:193](../../implementation/ambient-awareness/butler_ambient/brain.py:193) | High для клиента, неизвестно для backend | Отделить отмену вывода, прекращение экспорта и отмену задачи; получить backend run ID/status/cancel receipt; неизвестный исход не повторять |
| SEC-3 | Восстановить короткое «да» снятием identity/action checks было бы расширением прав | High: противоречит [DEC-085/086](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-085.md) | Связать ответ с question/proposal revision; сохранить проверку владельца и неизменности конкретного действия |
| SEC-4 | `local_ui.snapshot()` при чтении может вызвать `voice.cancel()` при утрате личного доступа. [local_ui.py:39](../../implementation/ambient-awareness/butler_ambient/local_ui.py:39) | High | Делать snapshot чистым чтением; применять изменения приватности в одном координаторе независимо от открытого окна |
| SEC-5 | Общий диалог и прямой agent route имеют разные хранилища истории; перенос их в одно состояние без scope может смешать приватное и публичное | Medium, вывод из [conversation.py:290–382](../../implementation/ambient-awareness/butler_ambient/conversation.py:290) | Единая логическая беседа с размеченным scope каждого фрагмента; projection для general не включает private |
| SEC-6 | Долгий owner-context reload может опоздать: nonce memory tool имеет срок 300 с; отсутствие receipt не доказывает применение правил | High: [feedback.py:418](../../implementation/ambient-awareness/butler_ambient/feedback.py:418) | Связать допустимое чтение с жизненным циклом запроса и актуальной авторизацией; после expiry явно unknown/renew-under-current-policy, не утверждать применение |

## Что сохраняется при очистке

Проверки Pause, согласия, аудитории, типа/границ входных данных, версии предложения и исхода операции — не архитектурные заплаты. Удалять нужно дублирующую оркестрацию, сохраняя эти контракты в одном проверяемом месте. **Предложение High**, основано на [DEC-087](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-087.md).

## Gaps / Open Questions

Протокол подтверждённой отмены задач backend и точная судьба уже готового результата после hard-close не описаны целостно: **Not stated in source**. Решение: описать события и разрешённые переходы, проверить interleavings автоматически. Никаких новых ключей, аудиозагрузок, внешних действий или повторной чистки диска в этом анализе не выполнялось.
