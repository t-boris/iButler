# Состояние исходников, тестов, релиза и эксплуатации

## Facts From Source — High

| Объект | Проверенный результат | Доказательство |
| --- | --- | --- |
| Workspace Git | `.git` отсутствует; владелец снял требование «skip github», DEC-074 | [Snapshot](runtime_evidence.json), [DEC-074](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-074.md) |
| Рабочий ambient/UI | Установка 0.6.3; status RPC на момент среза сообщает 0.6.3 | [deployment.json](../../implementation/ambient-awareness/evidence/attention-implementation/deployment.json), [snapshot](runtime_evidence.json) |
| Core | Установлен и отвечает 0.15.0 | Те же документы |
| Кандидат в source | 0.6.4, source hash manifest совпадает; не установлен | [snapshot](runtime_evidence.json), [version](../../implementation/ambient-awareness/butler_ambient/__init__.py:3) |
| Последний полный ambient suite | 590 tests, 199.351 s, OK для 0.6.3 до финальной корреляционной поправки | [interruption-handoff-tests.log](../../implementation/ambient-awareness/evidence/attention-implementation/interruption-handoff-tests.log), [архив отчёта](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification-0.6.3-pre-correlation.json) |
| Финальная поправка 0.6.3 | 71 focused tests + wheel; unchanged coverage переиспользовано по hash | [0.6.3 report](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification-0.6.3.json) |
| Изменение 0.6.4 | 64 tests, 29.400 s, OK: streaming, attention, turn-taking | [playback-recovery-tests.log](../../implementation/ambient-awareness/evidence/attention-implementation/playback-recovery-tests.log) |
| Build 0.6.4 | UI packaging PASS; wheel `butler_ambient-0.6.4-py3-none-any.whl` успешно собран | [UI log](../../implementation/ambient-awareness/evidence/attention-implementation/playback-recovery-ui.log), [wheel log](../../implementation/ambient-awareness/evidence/attention-implementation/playback-recovery-wheel.log) |
| Неизменённые пакеты/проверки | Архивная полная матрица: 227 core, System1/build, bridge, workspace, native/regression; повторно используются только совпавшие sources/artifacts | [0.6.0 matrix](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification-0.6.0.json), [текущий manifest](../../implementation/ambient-awareness/evidence/attention-implementation/release-verification.json) |
| Живой capture window | 302 PCM-блока за 303.52 s, ноль microphone gaps/capture errors | [capture-runtime-0.6.2.json](../../implementation/ambient-awareness/evidence/attention-implementation/capture-runtime-0.6.2.json) |
| Сбой установленной версии | `RuntimeError: local audio deadline` завершил thread `_run`; старое условие остаётся в установленном файле | [traceback](playback-crash.txt), [snapshot](runtime_evidence.json) |

Полный suite заново на 0.6.4 не заявляется: для изменённого source прошли 64 профильных проверки; остальное — цепочка проверенных неизменённых хешей. Это нужно видеть отдельно от качества реального разговора. Текущий архитектурный проход не менял продуктовый код и не выполнял новый deploy.

## Команды и воспроизводимость

Последние выполненные команды, cwd и exit code сохранены в `latest_checks` [runtime_evidence.json](runtime_evidence.json). Для повторения профильных тестов из `implementation/ambient-awareness` следует добавить пакет и каталог тестов: `PYTHONPATH=tests:. .venv/bin/python -m unittest test_streaming test_attention test_turn_taking -v`. Ниже — результат уже состоявшегося запуска из лога; исторические release-скрипты старых версий не следует запускать вместо актуального verification.

```text
python -m unittest test_streaming test_attention test_turn_taking -v
Ran 64 tests in 29.400s
OK

python build_app.py
PASS Butler.app packaging; separate UI identity; no permission-parent changes

python -m pip wheel --no-deps --no-build-isolation --wheel-dir build/wheels .
Successfully built butler-ambient
```

Срез архитектуры воспроизводится без управления устройствами:

```sh
python3 analysis/butler-voice-architecture/collect_evidence.py
```

Команда читает файлы, `status`/`doctor` RPC и сохраняет только отобранные метаданные. Она не проверяет акустику и не запускает пользовательские задачи. [Скрипт](collect_evidence.py).

## Как проект выпускается

`release_attention.py` проверяет версии/хеши, делает локальную резервную копию, устанавливает пакеты/интерфейс, перезапускает собственные ambient/core LaunchAgents и сверяет установленные/работающие версии, UI binary, активную конфигурацию и permission parent. Последний отчёт подтверждает 0.6.3. Общие сторонние сервисы не требуется перезапускать для этого релиза. [release_attention.py](../../implementation/ambient-awareness/release_attention.py), [deployment.json](../../implementation/ambient-awareness/evidence/attention-implementation/deployment.json).

0.6.4 — patch bump для исправления, соответствует принятому SemVer по пакетам; он был сделан до просьбы об архитектурном разборе. В этой workspace коммиты не делаются. [DEC-044](../../docs/features/butler-modular-core-and-reliable-tasks/decisions/DEC-044.md), [version](../../implementation/ambient-awareness/butler_ambient/__init__.py:3), [DEC-074](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-074.md).

## Problems / Risks → Proposals

- **Сервис жив, разговорный thread погиб. High.** Общий status не отражает liveness координатора. Добавить отдельные heartbeat/failed-state и восстановление обработчика, сохраняя идентичность уже отправленных задач. Основание: [traceback](playback-crash.txt), [service.status](../../implementation/ambient-awareness/butler_ambient/service.py:1040).
- **Build/test success воспринимается как готовность диалога. High.** Разделить release verification и acoustic/product acceptance; не закрывать последнее по первому. Основание: [DEC-078](../../docs/features/butler-attention-architecture-knowing-whom-the-owner-is/decisions/DEC-078.md).
- **Очередь готовых результатов — `deque(maxlen=10)`. High.** Переполнение может вытеснить старый результат без отдельного task delivery outcome. Заменить на реестр результатов с явным состоянием backlog/expired/cancelled; не давать техническому лимиту скрывать завершённую работу. Основание: [conversation.py:250](../../implementation/ambient-awareness/butler_ambient/conversation.py:250).
- **Масштабные реальные acceptance ещё открыты. High.** Нужны сценарии и измерения по существующим критериям; не выдавать владельцу ещё один ручной чеклист. См. [автоматические последовательности](02_architecture_analysis.md#13-как-переносить-не-подменяя-проверку-обещанием).

## Обязательные пункты исходной цели на момент разбора

- ✓ Git-предпосылка снята ответом владельца: DEC-074. Fetch/branch не выполнялись и не выдаются за выполненные.
- ✓ Спецификация актуализируется: DEC-085/086/087, REQ-056/057/058/065 и discussion; завершение функции не объявлено.
- ✓ Автотесты и build имеют приведённые результаты; границы полного/профильного покрытия указаны выше. Продуктовая acceptance остаётся открытой.
- ✓ Версия исправления повышена до 0.6.4 по SemVer.
- ✗ Последний кандидат 0.6.4 не установлен. До завершения запрошенного разбора deploy удержан согласно DEC-087; рабочая версия всё ещё 0.6.3 с подтверждённым дефектом. Исходная цель не завершена.

Никаких PR, merge, GitHub-sync, CI-проверки, MarkView browser-проверки или ручных заданий владельцу в рамках этого разбора не добавлено.
