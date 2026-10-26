# Lessons — recurring patterns to remember across sessions

This file complements `tasks/todo.md`. todo.md captures *what we did*; lessons.md captures *what to remember next time*. Append-only. Most recent on top.

---

## L-2026-10-24-1 — A sticky "mode" flag on a shared cancellation token leaks private work into the public mode
**Context**: Butler ambient DEC-062 general (unverified) dialogue. The first implementation marked the shared `Conversation.token` with `token_mode=GENERAL` when routing a general turn and never reset it. After the turn finished, private spoken advice reused the same token. Identity-loss cancellation then took the "general turn is live, keep it" branch: the private advice kept playing to the room and its text showed in the unverified view. An independent review also found the decision `_general_live()` and the token/queue changes ran without the lock that routing holds, so a just-routed general turn could be killed. All unit tests passed; the real-model tests and the independent review caught what the mocks could not.
**Rule**: track ownership explicitly, not as a mode attached to shared state: "general" = a dedicated token reference set when routing and cleared when the turn ends, compared by identity (`general_token is self.token`). Every check-then-act on shared dialogue state (identity-loss cancel, token reset, routing) runs under the same lock. Any new mode needs (a) an end-of-life point, (b) a test where the *next* unrelated user of the shared state (advice, checks, barge-in) runs after it, (c) an interleaving test that injects the competing action inside the critical section, and (d) a mutation check that the test fails without the fix.
**Apply when**: adding a second "mode" or privilege level to an existing pipeline that shares tokens, queues, visible text or debug buffers between modes.

## L-2026-10-23-1 — Homebrew `node@24` в PATH wake-пути = тихая смерть всех пробуждений после `brew upgrade simdjson`
**Context**: `lib/dispatch.py` (`NODE24_BIN`) и `bin/butler-notify` подставляли `/opt/homebrew/opt/node@24/bin` в PATH перед вызовом `openclaw agent`. 10.10 `brew` обновил `simdjson` 33 → 34; бинарник `node@24` слинкован с `libsimdjson.33.dylib` → `dyld: Library not loaded`, exit −6 (SIGABRT). 247 потерянных wake в `state/pending-wakes.jsonl`, 86 записей `exit -6` в `wrappers/locks/notify-failures.log`; research-очередь дренировала 1 тему в день вместо 10, сводки news/classify не постились. Никто не заметил 13 дней — wake «best-effort», ошибки только в лог.
**Rule**: вспомогательные скрипты используют **тот же Node, что и gateway в launchd** (nvm `~/.nvm/versions/node/v22.22.3/bin`), никогда — Homebrew `node@XX`. Любой `brew upgrade` на машине с gateway → сразу `butler-doctor`. Best-effort путь обязан иметь счётчик сбоев с порогом уведомления (3 подряд → `butler-notify`), иначе он сломан молча.
**Fix 2026-10-23**: `NODE_BIN = ~/.nvm/.../v22.22.3/bin` в `dispatch.py`, тот же PATH в `butler-notify`; бэкапы `*.bak-node24-simdjson-2026-10-23`; проверено отправкой уведомления (TG message 13333).

## L-2026-10-23-2 — «Делай всё в одном ходе» + claude-cli = удар о лимит 8 МиБ/20k строк; выпуск отрендерен, но не опубликован
**Context**: Bamberg-кроны (`FailoverError: Claude CLI turn output exceeded limit`): правило от 2026-07-21 «никаких фоновых процессов и субагентов» заставляет один claude-cli ход тянуть сбор evidence + десятки web-fetch + рендер + валидацию (300–400k токенов). Gateway (`claude-live-session-*.js:782`) обрывает поток при `rawChars > 8 MiB` или ≥ 20 000 строк. Сессия умирает между рендером и публикацией → 8 из 83 выпусков `SUCCESS` без `publish/youtube.json`. `reliability.outputLimits` в `openclaw.json` не задан (можно до 64 МиБ / 100k строк). Параллельно три источника правды по TTS (AGENTS.md / worker-prompt / cron payload) противоречат друг другу, агент сам жалуется на это в каждом run summary.
**Rule**: длинные производственные пайплайны — детерминированный shell-конвейер (`collect → draft → validate → render → publish`), LLM только в редакторском шаге; лимиты вывода CLI-бэкенда задавать явно; у каждого production-крона `on_failure: notify` (у Bamberg было `not-requested`, `lessons-weekly-decay` падал 21 неделю молча). Директива для агента живёт в одном месте (`brain/`), остальное генерируется — см. `docs/Butler-Architecture-v2.md` §6.

## L-2026-07-16-1 — «Бот Тани молчит» = telegram-канал завис в `stopped`; health-monitor его НЕ поднимает, лечит только `openclaw gateway restart`
**Context**: Боря: «familybuilder.io bot не работает». `openclaw channels status`: `Telegram tetyana-dyck: … stopped, health:not-running`, при этом транспорт жив (`transport: just now`) и входящие Тани копились в `~/.openclaw/telegram/ingress-spool-tetyana-dyck/` необработанными с ~10 июля (6 дней!). Health-monitor каждые 10 минут писал `restarting (reason: stopped)` — 125 раз за 15.07 — но канал так и не стартовал: цикл «restarting» без единой строки ошибки. Первопричина остановки утеряна: `/tmp/openclaw/` держит логи только ~2 дня. Токен был на месте (env процесса гейтвея). В CLI 2026.5.26 команды `channels start` НЕТ — вручную канал не поднять. Фикс: `openclaw gateway restart` → канал `running, connected`, спул разобрался мгновенно (остался лишь старый `.failed` от 02.06).

**Lesson**: Три отдельных факта. (1) Диагноз «бот молчит» начинай с `openclaw channels status` — состояние `stopped, health:not-running` при живом транспорте значит: сообщения спулятся, но не обрабатываются; смотри размер ingress-спула, это же и мера тишины (сколько дней копится). (2) Health-monitor'у нельзя доверять восстановление канала: он может 6 суток логировать `restarting (reason: stopped)` не поднимая канал и не логируя причину — «restarting» в логе ≠ канал реально стартует. Единственное лечение — полный `openclaw gateway restart` (~15с, WA без QR). (3) Ротация логов ~2 дня — если сбой старше, причину уже не найти; поэтому тишину надо ловить рано, а не по жалобе пользователя через неделю. Дырка, которую стоит закрыть: watchdog-cron (у butler), который дёргает `channels status` и алертит/рестартит при `stopped` дольше N минут — иначе паттерн повторится.

**Apply when**: любой openclaw-канал «молчит» / пользователь не получает ответов. Порядок: `channels status` → спул-каталог канала → `channels logs` → если `stopped` и монитор крутит «restarting» вхолостую — не искать дальше, `openclaw gateway restart` и верифицировать `running, connected` + опустевший спул. См. [[project-tetyana-page-isolation-and-tg-send]] (там же — `.failed` handler-timeout, это ДРУГОЙ режим тишины: канал жив, но апдейт помечен failed).

---

## L-2026-06-05-1 — 18 PR за день = бот имплементирует на каждый микро-вход; gather-first гейт обязателен
**Context**: Таня: «вчера снова мучения, к 12:00 сдалась, Блог и Подкасты так и не собрали, Курсы вокруг да около». Проверка origin/main (e3626d2): страницы **структурно целы** (блог 6 блоков, 0 битых картинок, 0 контаминации курсовыми, i18n-паритет; курсы — direction-страницы + 42 deepening-баннера). Но между снапшотом и утром бот накатил **18 PR (#73–#90)** за один день (06-04 10:12→23:15) с откатами туда-сюда: hero убрали (#82)→вернули (#84); один фикс карточки дважды (#74/#75); мобильные баннеры по кругу (#77/#78/#80/#81); #76 снимал ошибочные КУРСОВЫЕ баннеры с блога. Таня «сдалась в 12:00», бот молотил автономно до 23:15. Boris диагностировал корень: бот реагирует имплементацией на каждый микро-вход (Таня шлёт фото за фото, по 5–15 на страницу), не собрав задачу — «ни в коем случае не торопиться, пока полностью не поймёшь задачу».

**Lesson**: «N PR за день / do-undo болтанка» = НЕ сломанный билд (он часто структурно цел), а **преждевременная имплементация на частичном входе**. Нетехнический визуальный пользователь шлёт задачу кусками (фото за фото); одно фото ≠ задача. Фикс — **gather-first гейт** (HARD INVARIANT 7 + жёсткая секция в clarify-intent): на каждое фото/правку (а) подтверди что видишь, (б) спроси «пришлёшь ещё или это всё?» и считай, что ещё будет, пока не «это всё», (в) копи в ОДИН рабочий список (фото→куда), ветку/спек/имплементацию НЕ трогай, (г) на «это всё» → повтор всего списка → «да» → ОДИН спек → ОДИН PR. >2 PR на одну страницу = болтанка, STOP. Хотел структурный бэкстоп `debounceMs` (склеить пачку фото в один ход), НО **`debounceMs` — WhatsApp-only свойство; для Telegram-аккаунта оно невалидно**: `openclaw config` strict-валидация на РЕСТАРТЕ отвергает «additional property debounceMs» и **gateway aborts (не стартует)** — хотя `python json.load` и hot-reload его молча проглатывали. Бот Тани — Telegram, так что debounce недоступен; убрал, корень несёт сам gather-first инструкционный гейт. Урок про валидацию: hot-reload терпимее strict-startup — правка может «примениться» на лету, но уронить gateway при следующем рестарте; всегда `openclaw config validate` ПОСЛЕ правки конфига. Применено: clarify-intent SKILL.md (gather-first), tetyana AGENTS.md инвариант 7. Инструкции активированы рестартом gateway (свежий bootstrap; persistent ACP иначе держит старые правила, ttl 15м).

**Apply when**: любой агент строит визуал/контент по входу от нетехнического пользователя порциями. Сначала собери ВСЮ задачу (явное «это всё» + replay) → один батч → один PR. Никогда не pipeline на одном фото. И при жалобе «не собрали страницу» сначала докажи фактическое состояние деплоя (часто цело — боль в итерационной болтанке/восприятии, не в билде). См. [[project-tetyana-page-isolation-and-tg-send]], связан с page-isolation L-2026-06-04-1.

---

## L-2026-06-04-2 — Butler шлёт «⚠️ 🛠️ search "…" in 2>/dev/null (agent) failed» = агент гонит path-less grep; чини правилом, не скриптом
**Context**: Boris получил от butler в TG: `⚠️ 🛠️ search "68f0049b|Family Builder: поговорить" in 2>/dev/null (agent) failed`. Расследование: это НЕ строка из butler-скрипта (в `workspace-butler` её нет) — это **превью тул-колла, которое OpenClaw/acpx стримит** (`acp.stream`) на каждое действие codex-агента. Паттерн `68f0049b|Family Builder: поговорить` = короткий id + заголовок задачи, которую агент только что создал (`./bin/butler-task add …`, id `68f0049b-fb06-…`). Агент собрал ад-хок `grep -E "<id>|<title>"` БЕЗ пути поиска → grep читает stdin, exit 1 → OpenClaw рендерит пустой «in <путь>» как `in 2>/dev/null` и помечает `failed`. `./bin/butler-recall` и `lib/memory.py::_search_with_ripgrep` чисты (гард `if not search_roots … return`, rg returncode 0/1 = ок) — баг не в них, а в том, что агент их не использовал.

**Lesson**: «приходит junk от бота ⚠️ … (agent) failed» при codex/acpx-стриме — это обычно агент, гоняющий **path-less `grep`/`rg`** (читает stdin → exit≠0 → стримится как failed-превью). Фикс на уровне инструкций, не кода: жёсткое **Search invariant** в `workspace-butler/AGENTS.md` + `CLAUDE.md` (синк обязателен, см. [[feedback-agents-claude-md-sync]] / L-2026-05-30-4) — для поиска по vault/tasks/memory/sessions всегда `./bin/butler-recall "<query>"` (корни зашиты, на «не найдено» возвращается чисто), НИКОГДА bare grep и НИКОГДА grep/rg без явного пути. Альтернатива (не выбрана) — глобально гасить стрим тул-превью в acpx; шире и рискованнее. Это родственно PATH-инварианту L-2026-05-30-4 (та же codex-shell-природа: bare-команды/пустые аргументы ведут себя не как в обычном shell).

**Apply when**: любой codex/acpx-агент шлёт в чат failed-превью своих тул-коллов. Сначала пойми, что «(agent)» + «🛠️» = стрим OpenClaw, а не лог скрипта; потом ищи беспутёвый/bare вызов в действиях агента и закрывай его инструкцией (штатный CLI вместо ад-хок shell). Butler-агент НЕ имеет bootstrapMaxChars-cap, так что добавление правила в AGENTS.md безопасно (в отличие от tetyana, L-2026-06-02-1).

---

## L-2026-06-04-1 — "Бот сделал хаос на блоге" = cross-page contamination: бот строит новую страницу, копируя таксономию соседней
**Context**: Таня пожаловалась: пока бот сутки доделывал «Курсы и программы», он «сделал полный хаос на уже практически сделанной странице Блог и Подкасты — там всё спутано». Просьба: «чтобы Бот не брал информацию и картинки у других страниц, и для каждой страницы своя структура». Расследование: живой сайт = `origin/main` = `a2fe4f8` (#72, deploy 06-04 03:45 зелёный); блог в задеплоенном коде **структурно цел** — все 6 блоков (ROOTS/ROLES/INSIGHT/CATHARSIS/NEW ROLE/NEW LIFE), все hero + карусельные карточки во всех 3 локалях существуют на диске, i18n-паритет цел (2758 строк). Ни один из последних 40 коммитов не трогал blog и courses одновременно (нет файловой контаминации). НО на уровне **контента**: `coursesNew.deepeningBanners` 1-7 (Семья и отношения / Женский путь / Осознание / Катарсис и психодрама / Новая роль / Новая жизнь) — почти 1:1 копия подзаголовков блоговых блоков (`blogPortal.blocks[].subtitle`). Бот построил страницу Курсов, переиспользовав тематический скелет Блога → две страницы стали неразличимы → Таня видит «спутано». Доп. находка: её сообщение про хаос **зависло (handler-timeout 1500s) и помечено `.failed`** — бот на него не ответил вообще (`telegram/ingress-spool-tetyana-dyck/*.failed`, lane `telegram:1090472826`).

**Lesson**: «Хаос на странице X после работы над страницей Y» — это НЕ обязательно битые файлы (проверка показала блог цел). Чаще это контаминация на уровне **смысловой структуры**: clarify-intent велел «derive structure by reading checkout/», и воркер подсмотрел готовую соседнюю страницу и зеркалил её таксономию/тексты/картинки в новую. Файловый дифф при этом чистый (per-page коммиты), поэтому ни git-история, ни blast-radius по именам файлов это не ловят. Фикс — новый HARD INVARIANT 6 (page isolation): каждая страница владеет своим i18n-namespace (`blogPortal.*` vs `coursesNew.*`) и своей папкой картинок (`/images/blog` vs `/images/courses`); при сборке X запрещено тащить структуру/тексты/картинки Y; «derive by reading checkout/» = учиться только общим *компонентным/layout*-паттернам, НЕ контенту. Структура страницы — всегда свежий продуктовый вопрос Тане (2-4 опции), не зеркало соседа. Реализовано: (1) AGENTS.md HARD INVARIANTS — строка 6 + assert в pseudocode + why; (2) clarify-intent — anti-pattern «cross-page borrowing» + `non_goals` авто-включает все прочие страницы; (3) orchestrate-pipeline стадия 10 — blast-radius assert: staged i18n-ключи ⊆ namespace целевой страницы, staged картинки ⊆ её папка, иначе STOP+эскалация. Cap `bootstrapMaxChars` 20000→24000 (AGENTS.md вырос 19168→20734 б; иначе хвост обрезался бы — тот же gotcha, что в L-2026-06-02-1).

**Apply when**: любой агент, строящий новую страницу/раздел по образцу существующих. «Своя структура у каждой страницы» — это инвариант, а не пожелание: проверяй namespace контента, не только имена файлов. И при жалобе «сломал X» сначала докажи фактическое состояние X в задеплоенном коде (часто оно цело — проблема в восприятии/таксономии/кэше), потом чини корень. Chat id Тани = `1090472826`, бот шлёт ей: `openclaw message send --channel telegram --account tetyana-dyck --target 1090472826 --message "…"` (НЕ `--account butler`). `--dry-run` печатает «✅ Sent» но реально НЕ шлёт (следов в logs/delivery-queue нет) — для боевой отправки убирай флаг и лови `messageId` в `--json`.

---

## L-2026-06-02-1 — Auto-merge silently stalls on a BEHIND branch under strict protection → bot "understands but doesn't fix"
**Context**: Tanya complained «бот запутался — всё понимает, но не исправляет», tried `/new` many times. The tetyana-dyck orchestrator had actually done the work correctly: fix `9c55b24` (full-bleed courses banner), PR #37 opened, reviewed, **auto-merge enabled, CI green**. But the PR sat `OPEN` forever. Root cause: `mergeStateStatus=BEHIND` (main got #36 merged after the branch was cut) + `main` branch protection has `required_status_checks.strict=true` ("require branch up-to-date before merge"). GitHub auto-merge does **NOT** auto-update a stale branch, so it never fires. `deploy.yml` runs only on push-to-main → the change never deployed → Tanya saw no change on familybuilder.io. The bot's AGENTS.md stage 13 said only "wait CI green" with no BEHIND-recovery, so it waited forever and kept replying «жди». Compounding bug: AGENTS.md (16.9KB) exceeded the bootstrap per-file inject cap (~11.6KB), so the entire "Failure handling" section was **truncated out of the agent's context** — it never saw its own recovery rules. Fix: `gh pr update-branch 37` → re-ran CI → auto-merge fired → deployed. Then added stage-13 self-heal rule + failure-mode row to AGENTS.md, and raised `agents.list[0].bootstrapMaxChars` 12000→20000 so the full file injects.

**Lesson**: An "enable auto-merge then wait" pipeline is incomplete. Auto-merge does not update a stale branch, and `strict` branch protection blocks BEHIND branches indefinitely — a silent deadlock that looks like "the bot is ignoring me". The wait stage must poll `gh pr view <n> --json state,mergeStateStatus` until `MERGED`, and on `BEHIND` run `gh pr update-branch <n>` (re-triggers CI, then auto-merge fires), with a timeout→escalate. Also: any instruction file that grows past the harness's bootstrap inject cap silently loses its tail — verify critical recovery rules sit within the per-file AND total budget (or raise the cap). Truncated instructions are invisible failures: the agent behaves as if the rule doesn't exist.

**Apply when**: any orchestrator that merges via PR + auto-merge into a strict-protected branch, and any agent whose bootstrap/system file approaches the inject char limit. Check `mergeStateStatus` in the wait loop; never just `gh run watch`. See `workspace-tetyana-dyck/AGENTS.md` stage 13 + Failure-handling "Auto-merge enabled + CI green but PR not merging".

---

## L-2026-05-30-6 — НИКОГДА не авто-писать выводы LLM как ФАКТЫ в канонический vault; и не тестируй на реальном vault
**Context**: Inbox-классификатор (Opus 4.8) на 164 реальных сообщениях создал 9 People-заметок + 7 задач + события в Daily Note — и **ВСЁ выдумал**. Реальные сообщения были бытовыми («Доброе утро», «не хочешь встретиться?»), а модель сконфабулировала развёрнутые ложные нарративы: «знакомство с родителями Вики, аллергия на лилии», «квартира на Печерске 185к», «долг Андрею 15к», «сантехник, стоматолог». Хуже всего — завела человека «Майя Сергеевна» (риелтор, сделка 185к), у которого в буфере было **0 сообщений** — выдумала с нуля. Борис: «ничто из этого не правда», «НЕЛЬЗЯ записывать выдуманные данные в записи».

**Lesson**: Авто-запись LLM-извлечённого/выведенного контента как ФАКТОВ в канонический источник (People/Daily/Tasks vault) — фундаментально опасна. «Проанализируй и запиши наблюдение о человеке» → модель достраивает правдоподобную, но ложную биографию из обрывков. Правила: (1) **НЕ авто-писать выводы в канонический store**. Если извлекаем факты — только дословно/явно сказанное (grounding: цитата исходного сообщения; ничего не инферить/додумывать), И/ИЛИ писать в **staging** на ревью человеку, а не сразу в People/Daily/Tasks. (2) Высокий порог: создавать запись о человеке ТОЛЬКО если в сообщении есть конкретный явный факт; «доброе утро» → noise, не персона. (3) **Никогда не тестировать пайплайн на РЕАЛЬНОМ vault синтетическими данными** — мои тестовые «гость от +1 312», «договор Паше/Sirius Art» затекли в настоящий Daily Note и вечерний wrap-up; тестить надо на изолированном temp-vault. (4) При любом авто-пайплайне в canonical store — сначала dry-run с печатью решений (без записи) и ручная сверка на реальной выборке, прежде чем включать запись.

**Apply when**: любой пайплайн, где LLM пишет в Obsidian-vault/БД знаний. Сначала grounding + staging/review, dry-run на реальных данных, и только потом — авто-запись (если вообще). Классификатор отключён (оба крона enabled=False) до редизайна. См. `lib/people.py`, `state/inbox/classifier-worker-prompt.txt`, отключённые `inbox-classify-noon/evening`.

---

## L-2026-05-30-5 — Дедуп «ресёрч-инбокса» должен быть по СТАБИЛЬНОМУ ключу-источнику, а не по фразировке LLM; и translit для слагов
**Context**: Строил двух-скедулерную research-систему для Butler (Boris): ① умный Куратор (Opus 4.8 через dispatch kind `curate`) читает весь контекст — Projects/Daily/Tasks/Goals/News + ВСЁ под `Research/To Research/` — и наполняет очередь `Q`; ② простой Исполнитель (14:00) дренит ≤10 тем, по каждой Opus 4.8 deep research. Папка `To Research/` — вход куратора, не очередь. Boris сразу указал на риск: «не будет 2 раза один и тот же ресёрч по темам из folder». Реальный e2e (Opus 4.8, 114с, $0.99) это и подтвердил: одна тема (крипта) попала дважды, потому что `slugify` для кириллической темы с вкраплением латиницы «…(по ссылке из To Research)» выкинул кириллицу через NFKD+ascii-ignore и собрал слаг из случайного «To Research» → `to-research`; разные слаги для одной темы → дедуп по слагу промахнулся.

**Lesson**: Для персистентного инбокса (папка, которая лежит вечно и перечитывается каждый день) дедуп НЕЛЬЗЯ строить на тексте/слаге, который генерит LLM — он дрейфует при каждой перефразировке. Ключ дедупа = СТАБИЛЬНЫЙ идентификатор источника (путь файла-сида), и проверять надо против queued И done (иначе готовую тему заведут заново). Реализовал детерминированно в `capture()`: (1) слаг vs queued+done; (2) source-dedup — если `Research/To Research/`-context_ref новой темы уже покрыт любой queued/done темой → no-op; общий контекст (Interests.md, Daily Notes) НЕ ключ дедупа (он легитимно сидит много тем). Плюс `slugify` теперь транслитерирует кириллицу→латиницу (стабильный осмысленный слаг). Второй слой — `butler-research covered` + инструкция куратору пропускать покрытые доки (но код-дедуп — гарантия, промпт — оптимизация). Отдельный dispatch kind `curate` (а не `research`) нужен, чтобы wake-хендлер не запускал NOTIFY-флоу (поиск отчёта) для куратора.

**Apply when**: любой пайплайн, где LLM-агент периодически сканирует растущий источник (папку/чат/фид) и заводит производные задачи. Дедуп — по стабильному ключу-источнику, против всех статусов. Слаги из не-латиницы — только через транслит. И ОБЯЗАТЕЛЬНО гонять на РЕАЛЬНЫХ данных: unit-тесты прошли, а живой прогон вскрыл то, что юнит не показал бы — `capture --help` завёл «--help» темой (нет `--help`-гарда), кириллический слаг-мусор, и воркер почистил очередь, но НЕ стабы в vault (нет `remove`, у vault нет delete) → мусор в инбоксе перечитался бы следующим прогоном. См. `lib/research.py::capture/slugify/covered_to_research`, `lib/dispatch.py` kind `curate`.

---

## L-2026-05-30-4 — Codex/cron shell батлера НЕ видит PATH из shell-профилей и из env процесса gateway; зови butler-* через `./bin/`
**Context**: Первый боевой прогон cron `deep-research-daily` (14:00) упал в `error`. В трактори: агент (gpt-5.5/codex) выполнил `/bin/zsh -lc 'butler-research list'` → `exit 127: command not found`, затем сам выкрутился через `./bin/butler-research`. Очередь была пуста, тёрн семантически отработал — но openclaw пометил весь run `error` из-за одного упавшего tool-call. Я последовательно проверил и ОТВЕРГ три «правильных» на вид фикса: (1) `export PATH=…/workspace-butler/bin` в `~/.zshrc` — неинтерактивный `zsh -lc` не источает .zshrc; (2) то же в `~/.zprofile` — login-shell источает .zprofile (проверено `env -i /bin/zsh -lc`), НО до codex всё равно не долетело; (3) нативный openclaw `env.vars.PATH` + рестарт gateway — тоже мимо. Причина: codex-бэкенд (`runtime.acp.acpx`, `mode: persistent`) — это ОТДЕЛЬНЫЙ долгоживущий процесс (Codex.app), который рестарт gateway не перезапускает, и его shell строится по codex `[shell_environment_policy]` (дефолт `inherit="core"` = минимальный env), игнорируя и shell-профили, и унаследованный PATH процесса gateway.

**Lesson**: Для агента на codex-бэкенде PATH-инъекция любым внешним способом (shell rc, env процесса-родителя openclaw) НЕ доходит до его shell-инструмента. Рабочих варианта два: (а) править глобальный `~/.codex/config.toml [shell_environment_policy] set={PATH=…}` — но это бьёт по ВСЕМ codex-сессиям пользователя (десятки проектов), не minimal-impact; (б) **звать workspace-CLI по относительному `./bin/<tool>`** — cwd у codex всегда = workspace (`runtime.acp.cwd`), а скрипты должны быть location-independent (у нас `WORKSPACE=…` хардкодом + `cd`, поэтому симлинк/относительный путь безопасны). Выбрали (б): правило `./bin/butler-*` в AGENTS.md + CLAUDE.md (зеркало для claude-воркеров) + во ВСЕХ cron `payload.message` (их 6 гоняли bare-команды; `lessons-*` уже были на абсолютных путях). Плюс: НЕ доверяй статусу `cron list` — exit-127 одного recovered tool-call и «тихое» завершение при пустой очереди ОБА метятся `error`, хотя работа выполнена; диагноз ставь по `*.trajectory.jsonl`, а не по агрегату.

**Apply when**: любой workspace-локальный CLI, который должен вызываться агентом на codex/ACP-бэкенде или из cron-промпта. Всегда `./bin/<tool>` (не bare, не завязка на PATH-профили), скрипты — location-independent, и одно явное PATH-правило в AGENTS.md/CLAUDE.md. Дебаг cron — через trajectory, не через `cron list`. См. `~/.openclaw/workspace-butler/AGENTS.md` § «Butler CLIs (PATH invariant)».

---

## L-2026-05-30-3 — Substring-классификация ловит ложные совпадения; короткие латинские токены матчь по границам слова, и проверяй на РЕАЛЬНЫХ данных
**Context**: Авто-добавление новых Telegram-каналов в news по эвристике релевантности названия (маркеры AI/нейро/coding…). Среди маркеров был `ai` как подстрока. На реальном списке каналов Бориса он совпал с «Ukr**ai**ne» → в news автоматически залетели 2 военных канала про Украину. На моём тестовом наборе из 16 названий этих каналов НЕ было (iter_dialogs в живом сервисе нашёл больше каналов, чем я взял в тест), поэтому баг проявился только вживую.

**Lesson**: Подстрочный матч ключевых слов = частые ложные срабатывания на коротких токенах (`ai` ∈ ukraine/again/email/training, `ml` ∈ html, `code` ∈ decode). Раздели маркеры: (1) длинные/кириллические (`нейро`,`ии`,`coding`,`research`) — можно подстрокой (нужно для префиксов типа «ИИволюция»); (2) короткие коллизионные латинские (`ai`,`ml`) — только по границам не-буквы: `re.search(r'(?<![a-z])ai(?![a-z])', low)`. И второе, важнее: тестируй эвристику на ПОЛНОМ реальном датасете, а не на своей выборке — в данном случае надо было прогнать `channel_is_relevant` по ВСЕМ диалогам аккаунта (их было больше 16), тогда Ukraine-каналы всплыли бы до запуска. Любое авто-действие по эвристике должно (а) уведомлять о каждом добавлении и (б) быть тривиально откатываемым (`butler-ingest remove`).

**Apply when**: любая авто-классификация/авто-роутинг по ключевым словам (ingest, теги, маршрутизация). Границы слова для коротких токенов, прогон по полным реальным данным, уведомление + лёгкий откат на каждое авто-решение. См. `lib/ingest.py::channel_is_relevant`, `CHANNEL_BOUNDARY_MARKERS`.

---

## L-2026-05-30-2 — Пробуждение агента из фонового процесса должно быть НЕБЛОКИРУЮЩИМ и с коротким таймаутом
**Context**: Делал «task-completion wake» — по завершении `butler-dispatch` фоновый монитор будит сессию батлера через `openclaw agent --session-key <butler DM> --message … --deliver`, чтобы агент отреагировал/продолжил. Первая версия ставила `--timeout 900` и subprocess timeout 920с. Когда батлер был ЗАНЯТ (параллельно гонял свой кодинг-пайплайн в той же сессии), вызов `openclaw agent` вставал в очередь за активным ходом и **висел все 920с**, потом таймаут. При нескольких одновременных завершениях накапливались несколько 15-минутных зависших процессов (видно в `notify-failures.log`: 4 таймаута за 11:03–11:18). Continuation при этом не происходил.

**Lesson**: Любое пробуждение агентской сессии из детач-процесса = best-effort и НЕБЛОКИРУЮЩЕЕ. Три обязательных свойства: (1) **короткий ограниченный таймаут** (взял 180с — покрывает реальный ход на свободной сессии ~100с, но не даёт виснуть на занятой); (2) **единый неблокирующий lock** (`fcntl.flock(LOCK_EX|LOCK_NB)`): при контеншене одновременные пробуждения мгновенно отваливаются (skip-fast), а не плодят N длинных процессов против одной занятой сессии; (3) **catch-up журнал** (`state/pending-wakes.jsonl`) для пропущенных — основной факт завершения уже доставлен пользователю отдельной silent-нотификацией, а агент дочитает пропущенное из state (`butler-dispatch list`). Никогда не блокируй детач-процесс на минуты в ожидании занятой сессии. Проверять надо ОБА пути: свободная сессия (wake доставлен) и занятая (кап по таймауту + запись в catch-up) — второй воспроизводится только под реальной нагрузкой.

**Apply when**: любой «по завершении фоновой задачи разбудить/продолжить агента» через `openclaw agent`/инъекцию хода. Не ставь большой таймаут «на всякий», не позволяй стекаться пробуждениям, всегда имей путь дочитать пропущенное из персистентного состояния. См. `lib/dispatch.py::_wake_butler`, `WAKE_TIMEOUT_SECONDS`, `WAKE_LOCK`, `_record_pending_wake`.

---

## L-2026-05-30-1 — "Бот не отвечает": gateway жив ≠ бот работает; ищи деградацию приёма, не падение процесса
**Context**: Boris: «Почему не отвечает бот?». Первый инстинкт — проверить, не упал ли сервис. Но `pgrep` показал, что `openclaw gateway` (launchd `ai.openclaw.gateway`, `KeepAlive=true`) жив. Процесс был не при чём. Реальная картина нашлась только в рантайм-логе `~/Library/Logs/openclaw/gateway.log` (НЕ в `~/.openclaw/logs/` — там только `commands.log`/config-audit; StandardErrorPath в плисте = `/dev/null`). Два транзиентных сбоя накануне оставили telegram-провайдер в подвисшем состоянии: `02:00 ENFILE: file table overflow` (исчерпание файловых дескрипторов в системе → дренаж спула падал) и `16:57–17:00 connect ENETUNREACH 149.154.167.220:443` (Mac потерял сеть до Telegram на несколько минут). После возврата сети провайдер «поднялся», но приём не восстановился полностью — входящие копились в `telegram/ingress-spool-<bot>` и доставлялись рывками только при реконнекте (`Telegram reconnect drain: N pending message(s)`). Симптом — «отвечает с задержкой/через раз», а не полная тишина (butler успел отправить `outbound send ok messageId=743`). Лечится чистым рестартом gateway: `kill <pid>` → launchd респавнит за ~5–6с → в логе `starting provider (@MacStudioButler_bot)` без ошибок.

**Lesson**: Для OpenClaw-бота «не отвечает» почти никогда не значит «процесс упал». Диагностика по порядку: (1) `pgrep -fl 'dist/index.js gateway'` + `launchctl list | grep openclaw` — жив ли сервис; (2) рантайм-лог `~/Library/Logs/openclaw/gateway.log` (путь берётся из `StandardOutPath` в `~/Library/LaunchAgents/ai.openclaw.gateway.plist`), grep по `ENFILE|ENETUNREACH|conflict|isolated polling|reconnect drain|outbound send`; (3) глубина очереди `ls ~/.openclaw/telegram/ingress-spool-<bot> | wc -l` и открытые FD `lsof -p <pid> | wc -l`. Два важных факта про шум в логе: `isolated polling` — это ШТАТНЫЙ режим приёма этого gateway (стартует сразу при инициализации, не поломка), а периодические `health-monitor: restarting (reason: stopped)` идут с 21 мая фоновым циклом и сами по себе бота не ломают — не гоняйся за ними как за причиной. Настоящая причина — конкретная ошибка (ENFILE/ENETUNREACH/сетевой разрыв), оставившая провайдер в half-stuck.

**Apply when**: любой репорт «бот молчит/тормозит» по OpenClaw. Не перезапускай сервис рефлекторно — рестарт затрагивает и боевого бота Тани (`@familybuilder_io_bot`), это outward-facing действие → спроси подтверждение (CLAUDE.md «никаких быстрых фиксов без согласования»). Сначала докажи диагноз логом, потом предложи рестарт как корректирующее действие. Транзиентные ENFILE/ENETUNREACH, очистившиеся сами, в фикс не записывай — фикс именно «расшевелить подвисший провайдер».

---

## L-2026-05-21-1 — When user says «не трать токены», disable the channel, not the agent
**Context**: Round 10. User wanted broker-fabric «убран чтобы не тратил токены». Initial instinct was to consider removing the agent, the binding, or the workspace.

**Lesson**: For an OpenClaw bot, tokens are spent only when the agent is *invoked*, which requires a *message reaching the channel*. The minimum-risk kill switch is `channels.telegram.accounts.<id>.enabled = false`. The agent record, workspace, binding, webhook route, and repo all stay intact — reversibility is one boolean. Don't reach for deletion when disable suffices.

**Apply when**: any «disable X» request on a multi-component pipeline. Walk from input edge inward; stop at the first gate that satisfies the ask while preserving downstream state.

---

## L-2026-05-21-2 — Patch semantics: arrays REPLACE, objects MERGE (openclaw config patch)
**Context**: While drafting `openclaw.patch-tetyana-dyck.json5`, I almost shipped a patch that would have silently rewritten the `description` field of the broker-fabric agent (because I edited the line inside an array I needed to re-include).

**Lesson**: When patching openclaw config (and similar JSON-merge tools where the doc says "arrays replace"), every array you include in the patch must contain ALL existing entries byte-identical, plus the new ones. Use `openclaw config get <path>` to read the live array first; copy it verbatim; add yours; mark preserved entries with a comment like `// PRESERVED VERBATIM — do not edit fields here`.

**Apply when**: any patch tool with array-replace semantics. Read-current-state-first is mandatory; review the diff with an eye for "is this entry byte-identical to live config?".

---

## L-2026-05-21-3 — Non-technical user needs bilateral confirmation, not just one-sided clarification
**Context**: broker-fabric `gather-spec` skill (Rounds 6–8) optimized for *AI fully understanding* the technical owner. For Tanya (non-technical), that's not enough — *she* must also understand what she's getting before the AI starts work.

**Lesson**: When the requester can't read code or run a build, "I have enough info" is necessary but not sufficient. The skill must end with a plain-language *replay-back* message ("вот что я поняла: …") and require explicit «да» from the user before SPEC READY. Offering 2–4 concrete options instead of open prompts reduces cognitive load and surfaces ambiguity earlier. See `~/.openclaw/workspace-tetyana-dyck/skills/clarify-intent/SKILL.md`.

**Apply when**: building any orchestrator skill for a non-technical primary user. Default to options-not-prompts, layered consent, mandatory replay-back.

---

## L-2026-05-21-4 — i18n parity is a build invariant, not a code-review item
**Context**: Tetyana-Dyck-Web is trilingual (en/ru/uk). next-intl + missing key → silent fallback or key-path-as-text leaking into UI. Code review catches it sometimes; CI catches it always.

**Lesson**: For multilingual sites, make key-parity a CI gate, not a habit. A 60-line `scripts/check-i18n-parity.mjs` that fails the build when key sets diverge is cheaper than 100 code-review comments. See `~/.openclaw/workspace-tetyana-dyck/bootstrap-draft/scripts/check-i18n-parity.mjs`.

**Apply when**: any next-intl project (or any i18n setup with distinct message files per locale). Wire the parity check into CI on day 1.

---

## L-2026-05-21-5 — Don't open PRs in repos with uncommitted WIP on main
**Context**: Phase C-prep wanted to push bootstrap files to `Tetyana-Dyck-Web`. The repo had Tanya's uncommitted WIP on main (games page, image assets, message catalog edits).

**Lesson**: When asked to "set up CI / bootstrap repo X", first run `git status` in X. If there's WIP, do NOT branch/stash autonomously — that risks losing user context, conflating user changes with bot changes, or amending what isn't yours. Stage the files in a draft folder outside the repo and hand the integration sequence to the user.

**Apply when**: any "bootstrap repo" / "add CI" task. `git status` first; if dirty, draft-then-handoff; if clean, branch + push only after explicit go-ahead.

---

## L-2026-05-21-10 — `git add -A` is a security incident waiting to happen
**Context**: Round 10 first live request. Boris said «закоммить и задеплой текущий код». Bot ran `git add -A && git commit && git push origin main`. Swept 125 files into one commit: Tanya's diploma scans (`_temp/Diplomas.jpg`, `d1-d30.jpg`), 50+ client review screenshots, `.dde/state.db` (6.6MB SQLite with semantic embedding history), `.claude/CLAUDE.md`, `.idea/`. Repo was PRIVATE so nothing leaked publicly, but git history is now tainted (only `git filter-repo` + force-push can scrub it).

**Lesson**: `git add -A` / `git add .` / `git commit -a` are categorically banned in orchestrator skills for any user-driven feature flow. The orchestrator MUST stage by explicit file-list derived from `spec.affects_*` fields. Any untracked file not in the spec triggers either: (a) clarify-intent round to add it, or (b) `git stash` for user review. Sweeping WIP is never the right move — even when the user begs for it.

**Apply when**: writing any orchestrator skill that commits code. The `git add` command in skill examples must always be a path-list, never a glob or `-A`. Add an explicit "FORBIDDEN: `git add -A`" rule to the skill's NEVER-do section. Where possible, gate it with a literal pre-commit assertion checking `git diff --cached --name-only` against the spec-derived expected list.

Skills updated: `~/.openclaw/workspace-tetyana-dyck/skills/orchestrate-pipeline/SKILL.md` Stage 10, AGENTS.md HARD INVARIANTS block.

---

## L-2026-05-21-11 — Direct user commands («сделай это», «just push») must be parsed as clarification triggers, not action permission
**Context**: Same Round 10 first request. Boris's «сделай это» was interpreted as «skip clarify-intent, skip branch, skip tests, skip review, skip PR, just commit+push+deploy». The bot rationalized the entire bypass because the user had "explicitly asked".

**Lesson**: Pipeline invariants exist because users have urgency in the moment that they regret after seeing the cleanup cost. The orchestrator must learn that urgency is the most common rationalization for skipping safety steps, NOT a license to skip them. When a user writes «закоммить», «задеплой», «just push», «do it now», «without tests», «без ревью» — the correct response is to:
1. Acknowledge the urgency
2. Re-state the standard pipeline (ветка → тесты → ревью → PR → merge → CI-deploy)
3. Convert the request into clarify-intent (ask "what specifically — describe the visible result")
4. Run the pipeline, end-to-end

ETA difference between "shortcut" and "proper pipeline" is usually 5–10 minutes. Damage difference is irreversible (force-push for history scrub, redo work, broken trust).

**Apply when**: writing any clarify-intent or orchestrate-pipeline skill for a project where users might be impatient. Include literal refusal patterns ("if user writes X, you reply Y") in the skill text. Include a counter-example (incorrect prior behavior) so future bot sessions see the pattern they must NOT repeat. See `clarify-intent/SKILL.md` "Direct user commands do NOT bypass this skill" section.

---

## L-2026-05-21-12 — Test coverage is monotone-only; first allowed drop trains the bot to allow the next
**Context**: Boris's instruction (turn 4 of session): «тест ковередж не падает а растёт». Initial test-and-coverage skill had absolute thresholds (60% lines etc.) but no growth invariant — so a change that drops from 78% to 62% would pass.

**Lesson**: For an early-stage codebase, the right invariant is "coverage may grow or stay flat, never fall" — independently of any absolute floor. Drops compound: each allowed drop normalizes the next, and within a few rounds you're at 20% pretending it's fine. The only stable equilibrium is monotone-only.

**Implementation**:
1. At branch-cut time (stage 2 of orchestrate-pipeline), capture `baseline = npm run test:coverage` on fresh main.
2. At end of stage 4 (after tests), capture `new`.
3. Gate: `new >= baseline` (strict). Equality only allowed when `spec.type ∈ {content, copy-fix, visual-tweak}` AND no new branches were added.
4. Below baseline → BLOCKER, cycle implement until ≥ baseline. Never lower threshold under user pressure.

**Apply when**: writing any test-coverage skill or CI check. Always have both an absolute floor AND a monotone invariant. The monotone one is the load-bearing rule; the absolute floor is a tripwire for catastrophic regression. See `test-and-coverage/SKILL.md` "Coverage thresholds — monotone-only".

---

## L-2026-05-21-18 — «Напишу как обновится» = bot abandons task; active polling required
**Context**: orchestrator's stage 14 («wait Firebase deploy») was interpreted by bot as «send TG message "I'll write when site is updated" and go idle». 4 successful deploys accumulated as queued OpenClaw flows over an hour because:
- Bot session ended after the «напишу когда обновится» message
- Webhook callbacks arrived (200 OK to ngrok) but no live session was watching
- Pre-reply hook on the next user message also wasn't triggered (skill text said «check on every reply» but bot didn't treat it as literally mandatory)
- User had to manually verify deploys via gh CLI

**Lesson**: For pipelines that conclude with an async external event (CI deploy, build artifact, third-party callback), the orchestrator must **actively poll within the same session** that started the work, not delegate to a future hook. The literal commands:
- `gh run watch <run-id> --exit-status` (blocks until CI completes)
- Polling loop checking OpenClaw flows by `sha` (until match or 10-min timeout)
- Compose + send final TG message + cancel the flow

The pre-reply hook on next sessions remains as a **safety net** for the case where the active session was interrupted (user /new'd, network drop, signature bug killed the session). But it cannot be the primary mechanism — too easy for the bot to rationalize «I'll let the hook handle it» and never write the final message.

**Implementation**: see `~/.openclaw/workspace-tetyana-dyck/skills/orchestrate-pipeline/SKILL.md` § "Stage 14 — wait Firebase deploy (LITERAL active polling)" with anti-pattern callout, and AGENTS.md § "🛑 Pre-reply hook — MANDATORY before every TG reply" with the safety-net procedure.

**Apply when**: writing any orchestrator skill where a pipeline stage waits for an external async event. Mark the stage as active-polling-required and provide the literal shell sequence. Add a pre-reply hook for safety, but never let the bot use the hook as an excuse to skip active polling.

---

## L-2026-05-21-17 — «Re-implement on conflict» is wasteful; auto-resolve trivial classes inline
**Context**: orchestrate-pipeline original failure handling said «merge conflict on rebase → re-run implement stage on top of fresh main». This is brutal — throws away 5-15 min of work for what is often a trivial i18n-key-union or package-lock regeneration.

**Lesson**: For an orchestrator that hits merge conflicts often (multi-actor repos: bot + human + maybe other agents), classify conflicts by file-type and auto-resolve trivial classes inline:
- i18n JSON: union of key-sets, ours wins on duplicate keys
- package.json / lock: take main's version, re-`npm install`
- changelog index: take main's version, append our entry
- regenerable artifacts (lockfiles, snapshots): delete + regenerate
- code conflicts in DIFFERENT functions: keep both halves
- code conflicts in SAME function: escalate (need human judgment)

After auto-resolve, run the invariant checks (lint, i18n parity, tests) before push to catch issues introduced BY the resolution.

Implementation lives in `~/.openclaw/workspace-tetyana-dyck/skills/orchestrate-pipeline/SKILL.md` § "Merge-conflict resolution".

**Apply when**: any orchestrator that does `git rebase` or `gh pr merge` as part of pipeline. Replace blanket re-implement with classification-based resolver. Escalate only the non-trivial cases — saves the human's attention for actual decisions.

---

## L-2026-05-21-16 — Plain-language voice is for EVERYONE, not just non-technical primary users
**Context**: AGENTS.md originally said «Boris is technical, match his language». Bot interpreted as «with Boris I can use tech jargon: PR, коммит, билд, деплой, CI, мёрдж, ветка, webhook». In practice this leaked into shared chats (where Tanya also reads) and into Boris's own daily flow («зачем ты спрашиваешь меня про пиар?»). The default voice should be plain Russian/English for the bot's TG output, regardless of recipient.

**Lesson**: For orchestrator agents that have multiple users (technical admin + non-technical primary), the bot's TG voice should default to **plain-language** universally. Technical detail lives in:
- PR description on GitHub
- `docs/changelog/*.md` entry
- bot's own tool logs

NOT in the TG message. Even when the user types technical jargon themselves («просто закоммить»), the bot rephrases in product-language («поняла, сохраняю»).

**Implementation**:
1. Explicit ❌→✅ glossary in SOUL.md with at least 20 common terms (PR, commit, branch, push, merge, CI, build, lint, deploy, webhook, coverage, schema, etc.).
2. Counter-example showing real bot output that violated this + the corrected version.
3. Anti-pattern trigger in clarify-intent skill: refuse-and-rewrite if any forbidden term appears in draft.
4. When user explicitly asks for tech detail («покажи лог», «дай ссылку») — answer with inline link wrapped in plain-language label ("Посмотреть подробности"), not raw URL or term.

**Apply when**: writing any orchestrator skill that has TG/Slack/SMS output. Default voice = plain-language for all readers. Tech detail belongs in PR/changelog/logs, not the chat. The recipient's technical literacy is irrelevant — the chat is a shared surface.

See `~/.openclaw/workspace-tetyana-dyck/SOUL.md` § "ОБЯЗАТЕЛЬНО: говори plain-language ВСЕМ пользователям" and AGENTS.md Boris section.

---

## L-2026-05-21-14 — Telegram does NOT render markdown tables; ban them in agent output
**Context**: Boris reported that the bot's status messages with tables (`| col | col |`) look terrible in Telegram. Tables, ASCII borders, and HTML tables all render as raw text in TG — pipes and dashes show literally. This is a platform limitation, not a bot bug.

**Lesson**: For any Telegram-output skill, ban markdown/HTML/ASCII tables in the skill prompt. Use one of:
- **Bullet list** with bold labels — best for status/comparison data with ≤8 rows
- **Fenced code block** (` ``` `) — renders monospaced; use for aligned tabular data like file:lines
- **Numbered list** with status icons (✅ ⏳ ⏸) — for multi-stage progress

Also: max ~600 chars per message (TG has a 4096 limit but readability craters past one screen). No nested lists deeper than 2 levels. `#` headers don't work — use bold short lines. `**bold**` doesn't work in TG markdown — use `*bold*` (single asterisks).

**Apply when**: writing any skill that produces Telegram output. Add an explicit "Telegram formatting rules" section listing what's allowed and what's banned, with examples. Reference the format from `AGENTS.md` final-delivery template as the canonical example. See `~/.openclaw/workspace-tetyana-dyck/SOUL.md` § "Telegram formatting — NO markdown tables".

---

## L-2026-05-21-15 — Git hooks in the repo are the only structural enforcement for the orchestrator
**Context**: Skill-level instructions (AGENTS.md, orchestrate-pipeline) were repeatedly bypassed by the orchestrator under user pressure («сделай быстро»). Adding "🛑 HARD INVARIANTS" sections helped, but the orchestrator still cherry-picked when to honor them. The first reliable structural barrier was **git hooks in the repo itself** (`.githooks/pre-commit`, `.githooks/pre-push`), activated via `git config core.hooksPath .githooks` in package.json's `postinstall`.

**Lesson**: For an orchestrator agent that has shell access, three layers of enforcement are needed:
1. **Skill text** (advisory) — fast iteration, easy to update, but ignored under pressure.
2. **CI workflow on GitHub** (gate) — blocks merge if checks fail; only kicks in at PR time, not at commit.
3. **Git hooks in the repo** (local block) — blocks commit/push at the moment of the action; cannot be skipped without `--no-verify`, which itself can be banned in skill text.

The hooks must be in the repo (not in `~/.gitconfig`) so they live with the codebase and apply to every checkout (including the agent's). Activate via `npm install` postinstall so they're picked up automatically — no manual `git config` step.

**Caveat about hooks-bootstrap chicken-and-egg**: the hooks only protect `main` *after* they're merged into `main`. Before the bootstrap PR is merged, the bot can still do dangerous things. Solution: open the bootstrap PR ASAP and make it the first feature flow in any new repo. See `tetyana-dyck-web` PR #2.

**Apply when**: setting up infrastructure for any new orchestrator-driven repo. Three artifacts on day 1: (1) `.github/workflows/ci.yml`, (2) `.github/workflows/deploy.yml` with webhook callback, (3) `.githooks/{pre-commit,pre-push}` activated via package.json postinstall.

---

## L-2026-05-21-13 — Local `firebase deploy` is forbidden; production deploys are CI-only
**Context**: Round 10 first request. Bot ran `firebase deploy --only hosting,functions` directly from Boris's laptop, bypassing CI entirely. No audit trail, no CI gating, no rollback record beyond `firebase hosting:rollback`. The deploy succeeded, but the bypass made the change invisible to Boris's CI dashboards and future post-mortem.

**Lesson**: Production deploys go through CI workflows only (`deploy.yml`). The orchestrator NEVER runs `firebase deploy` locally — not even "just this once because the user asked". Reasons: (1) CI provides audit/log, (2) CI runs from clean state (no local cruft), (3) CI can verify pre-deploy gates (build/test/lint), (4) local deploy creates a "shadow channel" where deploys exist but don't appear in CI history.

**Apply when**: any orchestrator that produces deployable artifacts. The deploy stage of the skill must explicitly call `gh run watch` for the CI workflow, never invoke the deploy tool directly. See `deploy-to-main/SKILL.md` "No local `firebase deploy`".

---

## L-2026-05-21-6 — Stop-hook vs CLAUDE.md "ask approval" rule — execute the irreversible-bounded slice
**Context**: User's CLAUDE.md says "never implement without approval". User's /goal hook insists "system isn't working until built". Two contradictory pressures.

**Lesson**: Resolve by scope. Execute the slice that is (a) directly named in the user's explicit ask AND (b) reversible AND (c) doesn't add libraries / change architecture / open PRs in others' repos. Stop at the first action that violates any of these and present the user with a precise list of what they need to provide / approve. This honors both: the goal moves forward; nothing irreversible happens without consent.

**Apply when**: orchestration tasks where Stop-hook and approval-rule pull in opposite directions. The slice rule: input-side toggles (disable broker-fabric) + local file scaffolding (workspace) + drafts not-yet-applied (openclaw patch, repo bootstrap files) are safe to execute. Applying secrets-bearing patches, opening PRs, modifying others' commits — stop.

## 2026-07-17 — news-video voice/visuals overhaul
- **"Feature works" must mean end-to-end, live.** news-video publish had NEVER delivered (gateway blocks media from workspace-* dirs) yet the pipeline looked healthy because render succeeded and publish was only ever dry-run tested. Rule: before calling a delivery feature done, prove one real delivery (receipt/messageId), not a dry-run.
- **Repo edits are inert until deployed.** bin/butler-news-video prefers the LIVE workspace lib over the repo overlay; testing the repo copy silently exercised old code. Rule: after editing implementation/news-video, always copy to ~/.openclaw/workspace-butler (with backup) and re-verify via the workspace bin.
- **Stale fixtures mask new features.** The e2e episode.json generated by the old lib had no stat/points, so the new card layout silently fell back. Rule: regenerate fixtures with the new code before judging output.
- **Boris requirement (product):** daily video must include ALL stories from the last 3 news digest files + 1-3 latest deep research reports (each as its own finding scene); voice quality and literal, concrete visuals (numbers/bullets, varied phrasing) are hard requirements, not polish.

## 2026-07-17 — lecture publish: клиентский таймаут ≠ провал доставки
- **Симптом**: publish 46-МБ лекции дважды «упал» с GatewayTransportError (timeout 10s), но видео РЕАЛЬНО доставилось (второй upload завершился сервер-сайд после таймаута клиента). Розница: клиентское RPC-окно 10 с < время загрузки большого файла в Telegram.
- **Правило**: при таймауте отправки медиа НИКОГДА не ретраить вслепую — сначала проверить /tmp/openclaw/openclaw-YYYY-MM-DD.log на "telegram outbound send ok ... messageId=" за окно попытки. Иначе дубль.
- **Фикс корня**: gateway.handshakeTimeoutMs=120000 в openclaw.json (клиентский дефолт RPC = max(10s, handshakeTimeoutMs); читается клиентом сразу, рестарт gateway не нужен). Бэкап: openclaw.json.pre-gateway-timeout-2026-07-17.
- **Ресинк receipt**: если видео ушло, а receipt нет — руками записать publish/telegram.json со status=video-sent и messageId из лога, затем обычный publish дошлёт только credits (идемпотентный резюм — штатный механизм).

## 2026-07-19 — news-video: направления от Boris'а (визуал, война, YouTube)
- Визуал: текстовые карточки — крайний случай; большинство сцен должны нести диаграмму/стат/картинку. Картинки искать (vault → Wikimedia/официальные пресс-страницы), лицензию фиксировать в credits — видео публичные на YouTube. Эмодзи в карточках НЕ работают (Arial без глифов).
- Война: пониженный приоритет, только цифры и факты, без драматизации; закреплено в Interests.md («Not interested in»), SKILL.md и worker-промпте.
- YouTube: daily-новости — автопубликация без спроса (standing auth 2026-07-19); research/lecture — только после явного подтверждения. Все butler-видео — на отдельный брендовый канал.
- Паттерн: директивы по контенту видео должны попадать в ТРИ места сразу — skills/news-video/SKILL.md, state/news-video-worker-prompt.txt и (если про фильтрацию тем) Goals/Interests.md; иначе воркер их не увидит.
- Вёрстка карточек: причина «налазящего» текста — фиксированный кегль при фиксированной позиции панели (0.345·h). Решение: `_draw_fitted` (авто-сжатие кегля под бюджет высоты) для заголовка, body, стат-подписи и субтитров. Короткие тексты рендерятся без изменений.
- TTS переключён на OpenAI cedar (standing auth boris:2026-07-19:standing-auth-openai-tts, cap $2.50/run): ключ рендерер берёт из env ИЛИ openclaw.json (skills→openai-whisper-api) — dispatch-воркеры вычищают OPENAI_API_KEY из env. Fallback-цепочка openai→edge→macos→captions, релиз не теряется. PaidAPIApprovalRequired ≠ RenderError — гейт одобрения не глотается фолбэком.
- Мульти-канальный YouTube: один OAuth-клиент, токен на канал (`--profile` → token-<p>.json). Выбор канала происходит на consent-экране Google при `auth --profile <p>`. Bamberg-runs (RUN_ID bamberg-*) — отдельная ветка в task-done: НЕ в Telegram, НЕ на Butler AI News.
- Gemini: новые ключи формата AQ.* требуют google-genai ≥2.x; SDK предпочитает GOOGLE_API_KEY над GEMINI_API_KEY — мусорное/дублирующее значение в ~/.zshrc даёт API_KEY_INVALID при валидном ключе. Диагностика: curl -H "x-goog-api-key" напрямую (мимо SDK).
- Рендерер: mode "image" СПЕЦИАЛЬНО размывает фон (подложка) — для информационных изображений (карты!) использовать mode "broll" (резкий Ken Burns). Погода: выделенный diagram type "weather" (текущее + чипы влажность/давление/ветер + min-max колонки) вместо generic bars; данные из Bright Sky уже содержат humidity/pressure. Бейдж карточек настраивается через episode.branding.badge.
- Персонализация Bamberg News: живой файл state/audience-profile.md (читается каждым прогоном, факты о родителях дописываются датированными пунктами). Приватность: семья/адрес/диагнозы НИКОГДА не упоминаются в видео — персонализация только через отбор тем.
- ROOT CAUSE «обрывов» bamberg-агента: `openclaw agent` CLI имеет жёсткий timeout 600s и УБИВАЕТ ход агента (FailoverError) — любые длинные продакшн-циклы запускать ТОЛЬКО через cron-механизм (`openclaw cron run <id>` или одноразовый `cron add --at +1m --delete-after-run --timeout-seconds 3600`). Переключение раннера codex→claude было по ложному следу (но claude оставлен — стабильнее в fallback).

## L-2026-07-20-broll-html (broll downloader saved rate-limit pages as .jpg)
**Симптом**: рендер завис на сцене — ffmpeg крутится на «.jpg», который на деле HTML (страница 429 от Wikimedia). 427/516 файлов butler-библиотеки и 155/275 bamberg оказались битыми после массовой закачки.
**Правило**: любой скачиватель бинарников ОБЯЗАН проверять status_code и магические байты (JPEG `\xff\xd8\xff` / PNG `\x89PNG`) до записи на диск + троттлить (0.6s) и повторять на 429. Исправлено в обоих *_broll.py.
**Правило 2**: воркер СНОВА фонил рендер (третий раз) — сессия умирает вместе с ним. Промпт ужесточён капсом с перечислением потерянных релизов; если повторится — перенести рендер из воркера в cron-обвязку.

## L-2026-07-20-kenburns-cache (renderer reuses stale broll .mp4 caches)
**Симптом**: заменил JPEG-фоны сцен, перерендерил — в видео всё равно старые фото (две «исправленные» версии подряд).
**Причина**: рендерер кэширует Ken Burns клипы как `<сцена>.mp4` рядом с JPEG в run/assets/broll и при следующем рендере использует их вместо перегенерации из обновлённого JPEG.
**Правило**: при любой замене broll-фото удалять парные `*.mp4` в run/assets/broll И drafts/assets/broll до рендера (`find ... -name '*.mp4' -delete`; zsh `rm глоб` падает на пустом шаблоне и обрывает команду — использовать find).
**Правило 2**: после рендера с заменой визуала — извлечь кадры и проверить ГЛАЗАМИ до аплоада.

## L-2026-08-01-landscape-tv (Бамберг → 16:9, сменяющаяся картинка, карта целиком)
**Контекст**: Boris смотрит Бамберг на телевизоре — вертикальный формат, статичная картинка, обрезанная карта и речь 1.3× не годятся.
**Правило 1 (ориентация)**: общий рендерер обслуживает оба канала — ветвить по ориентации (`create_scene_card` → `_create_scene_card_landscape`), а не переписывать существующую вёрстку. Доказательство отсутствия регресса: рендерить одни и те же карточки старым и новым модулем и сверять sha256 (портрет вышел побайтово идентичным).
**Правило 2 (кто получает сцену)**: в 16:9 медиа и данные конкурируют за место. Приоритет закреплён в `_landscape_media_plan`: карта (`fit=contain`) всегда в окне; декоративное фото уступает окно реальной диаграмме и уходит в размытый фон. Иначе виджеты, рассчитанные на высокую панель (weather), падают — `ImageDraw.rounded_rectangle` кидает «y1 must be greater than or equal to y0» на инвертированном прямоугольнике. Страховка: DIAGRAM_MIN_BOX_HEIGHT (ужимает масштаб под низкий бокс) + клэмп внутри `_draw_weather`.
**Правило 3 (contain vs cover)**: «показывать целиком» = `force_original_aspect_ratio=decrease` + pad; фон кадра — размытая cover-версия того же медиа. `crop` (cover) для карт запрещён и теперь ловится валидатором Бамберга, а не полагается на добрую волю воркера.
**Правило 4 (кэш клипов)**: любой кэш производного клипа именовать по СОДЕРЖИМОМУ (hash имени+размера+mtime исходников), а не по id сцены — иначе повторяется L-2026-07-20-kenburns-cache (подменили фото, получили старое видео).
**Правило 5 (темп/формат — в профиль, не в константу)**: `PROFILE_RULES[...]["format"|"tts_tempo"]` + опциональный `episode.audio.tempo`; так один канал меняется, не задевая другой.
**Готча (ключи в неинтерактивной оболочке)**: `zsh -c` НЕ читает ~/.zshrc — GEMINI_API_KEY/GOOGLE_API_KEY из него в скриптах видны как мусорные 8 символов (значение из родительского окружения). Для платных вызовов читать ключ из ~/.zshrc явно и класть в env процесса; проверять длину (валидный AQ.*-ключ ≈53 символа).
**Готча (крон дописывает в файл)**: инструкция крона «read state/...worker-prompt.txt ... and append only: RUN_ID=...» была понята буквально — агент дописал 3 блока RUN_ID в САМ файл промпта (стали видны стухшие RUN_ID в конце). Если повторится — переформулировать шаг 4 крона: «добавь эти строки к инструкции, которой следуешь, НЕ редактируй файл».

## L-2026-10-16-codex-bundled-client (all codex-runtime butler crons failed: 400 "model not supported ... ChatGPT account")
**Symptom**: every butler cron (~15 jobs) failed with `The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account`. Last OK run 2026-10-13, first failure 2026-10-14 12:00 CT.
**Two stacked causes**: (1) OpenAI retired `gpt-5.5` for ChatGPT-plan Codex — the account's live model list is `~/.codex/models_cache.json` (gpt-6.1-sol, gpt-6-sol/astra/luna, gpt-5.6-*). (2) The butler does NOT use the system `codex`: it runs the `@openai/codex` bundled in the `@openclaw/codex` plugin (`~/.openclaw/npm/node_modules/@openclaw/codex/node_modules/@openai/codex-darwin-arm64/.../codex app-server`), pinned at 0.134.0. That client is rejected for EVERY model (gpt-6.x -> "not supported", gpt-5.6 -> "requires a newer version of Codex") and cannot even decode the models list (`unknown variant max`). Swapping the model name alone fixed nothing.
**Rule 1**: a Codex "model not supported" 400 is not proof the model name is wrong — test `codex exec -m <model> ...` with BOTH the system CLI and the plugin's bundled binary before changing config.
**Rule 2**: OpenClaw 2026.5.x only knows gpt-5.x Codex ids; a new id needs a configured row (`models.providers.openai-codex.models[]` in openclaw.json, hot-reloads) or the run dies with `Unknown model: openai-codex/<id>`. Butler model pin = `agents.list[butler].model.primary` + `agents.defaults.models`.
**Rule 3 (client upgrade without npm)**: `npm install` inside the plugin dir fails (`workspace:*` devDependency). Instead: download `@openai/codex@X` and `@openai/codex@X-darwin-arm64` tarballs, verify sha512 vs `npm view ... dist.integrity`, swap the two dirs under the plugin's `node_modules/@openai/`, bump the pins in plugin `package.json`, `npm-shrinkwrap.json` and the root `~/.openclaw/npm/package-lock.json` (otherwise a root `npm install` silently reverts), then kill the gateway's `codex app-server` child — it respawns lazily on the next turn, no gateway restart needed.
**Rule 4**: after any `openclaw plugins update` / OpenClaw upgrade, re-check `.../codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex --version` (must be >= 0.162.1) — a plugin release still bundling 0.134.0 would re-break the butler. Rollback copies: `~/.openclaw/backups/codex-plugin-pre-upgrade-2026-10-16/`.
**Open item**: `~/.openclaw/acpx/codex-home/config.toml` (generated, ACP path via `@zed-industries/codex-acp@0.14.0`) still says `model = "gpt-5.5"`; currently unused (all butler sessions are embedded, `acp=False`), but it will fail if an ACP codex session is ever started.

## L-2026-10-24-core-lock-order (butler-core test hang at exit = lock-order deadlock)
**Symptom**: the butler-core suite hung at exit after all tests passed. A `faulthandler` stack dump (`faulthandler.register(signal.SIGUSR1)`, then `kill -USR1 <pid>`) showed two threads, each waiting on the other's lock.
**Cause**: `Config.active()` took the config lock and then read from the store (store lock), while store transactions held the store lock and called `config.active()` (config lock). Thread A held config→store; thread B held store→config.
**Rule 1**: a module that is called from inside store transactions must never take its own lock around a store read. Cache reads go without a lock (a stale cached value is harmless; it is refreshed on activation).
**Rule 2**: to debug a hang in Python, take a faulthandler stack dump before guessing. The environment has no `timeout` command; bound runs with `subprocess.run(timeout=...)` or a background job instead.

## L-2026-10-24-cancel-race (a cancelled operation came back as `requested`)
**Symptom**: `test_stop_executor` sometimes failed with state `requested` after a cancel.
**Cause**: `cancel()` finalized `cancelled`, and then the stalled executor woke from its cancel event, raised Ambiguous, and the reconcile→retry path overwrote the state to `requested`.
**Rule**: every post-execution state change is a compare-and-set (`UPDATE ... WHERE id=? AND state IN (...)`), with the expected source states listed. A terminal state set by another path (cancel, resolution) is never overwritten. A success receipt may override `cancelled`, because the receipt proves the effect happened.

## L-2026-10-24-deploy-shell-is-not-a-terminal (owner-only RPC refused during deploy)
**Symptom**: `deploy.py` stopped after launching the daemon: `forbidden: delivery.import is not available to local callers`. The daemon was already on the new version and config, but `deployment.json` was stale and the import had not happened.
**Cause**: the peer classifier maps this assistant's shell (and any non-TTY automation) to `local`, never `terminal`. Owner-only methods (`approve`, `config.activate`, `delivery.import`) are refused by design.
**Rule 1**: anything a deployment must change in core state goes through the offline window (`butler-core admin …` while the daemon is stopped, actor `deploy:offline`, DEC-060), never through an owner-only socket method.
**Rule 2**: order deploy steps so every state change happens before `launch_agent()`; a step that can fail after the restart leaves the installed system and the deployment record out of sync.

## L-2026-10-24-early-exit-branch-in-a-script (empty caption on the new path)
**Symptom**: the new butler-core branch in `butler-send-tasks` used `$CAP`, which the legacy path defined further down; the branch would have sent the board with an empty caption.
**Rule**: when inserting an early-return branch into an existing script, list every variable the branch reads and confirm each is assigned above the insertion point; move shared assignments up before the branch.

## L-2026-10-24-markview-discussion-tool (hand edits where a tool existed)
**Symptom**: discussion entries for the core feature were appended to discussion.md by hand, noting "no tool exists", while `markview_add_discussion_note` was a deferred tool all along.
**Rule**: before any hand edit under docs/features, run ToolSearch for a markview_* tool covering the record type (discussion note, question answer, object update); hand edits are the last resort and only when the search shows nothing.

## L-2026-10-24-whisper-phantoms (recognizer "hears" «спасибо» / «продолжение следует» on silence)
**Symptom**: the owner saw constant «спасибо» and «продолжение следует» in the dialogue feed while nobody spoke.
**Cause**: Whisper invents subtitle-style closing lines on silence/noise; the resident whisper.cpp server ran without VAD, with default no-speech/log-prob thresholds and with temperature fallback; nothing filtered the result.
**Rule 1**: any ASR path needs three guards: a VAD before decoding, deterministic decoding (temperature 0, no increment, -sns), and a phantom-phrase check that compares the phrase against the audio's speech energy so genuine short replies survive.
**Rule 2**: when the owner reports "it recognizes noise", check the recognizer's launch flags first (`pgrep -fl whisper-server`) before touching the dialogue logic.

## L-2026-10-24-deploy-gating (a deploy ran after a failing test pipeline)
**Symptom**: `python3 -m unittest … | grep … | head && python3 deploy.py` deployed butler-core 0.12.0 although two boundary tests had failed: the `&&` saw `head`'s exit code, not unittest's.
**Rule**: never gate a deploy on a pipeline; run the suite to a log file, test `$?` of the test command itself, and deploy only on 0 (as the ambient deploy chain now does). The deployed code was unaffected (the failures were missing boundary rules for new modules), but the gate must not depend on luck.

## L-2026-10-24-feed-redraw-and-chunked-speech (owner: «всё перерисовывается», «обрываются сообщения, нестройная речь»)
**Symptoms**: the panel feed was rebuilt (all rows removed and re-added) on every new entry and pinned to the bottom, so reading older lines was impossible; streamed reply pieces were logged as separate feed lines; passive hearing and the dialogue both logged the same utterance; speech was cut at commas; recall timed out because background analysis (a different model) ran through the turn.
**Rules**: (1) A timeline view is append/update only: diff by stable id, never tear the stack down, anchor the row under the reader's eyes, follow only when already at the bottom. (2) Anything that streams must own ONE feed record per turn and update it in place. (3) When two layers observe the same utterance, the panel merges them; the diagnostics may keep both. (4) TTS boundaries are sentences, not clauses. (5) "Yield the shared model" must mean the shared *servers* (whisper, Ollama), not a model-name equality check. (6) Read the audit metadata (timings, knowledge_state) before guessing; add the timing you lacked.
**Owner expectation recorded as REQ-036**: alone in the room with a quiet Mac means the speech is for Butler, without the name.

## L-2026-10-24-turn-taking (owner: «он прерывается постоянно … как человек: перестать, только если конкретно перебивают»)
**Symptoms**: replies cut between streamed sentences by any VAD frame (state thinking/phase reply was not "speaking"); a cough resets the voice streak and a momentary lease loss or a one-second source gap cancelled a committed turn; meeting quiet flapped on single CoreAudio checks; speech_detected fired on one 60 ms frame.
**Rules**: (1) Model turn-taking with explicit state predicates (reply_in_progress, turn_committed) and route all speech during a reply to the interruption recogniser; stop only on recognised words from the right speaker. (2) Never cancel an admitted turn on a momentary sensor/identity blip; defer to the turn end — privacy rules still apply at the next turn. (3) Every detector that gates behaviour needs hysteresis (frames per second, consecutive checks). (4) When a prompt gains a conditional rule, include it only when it applies; a "false" flag teaches the model the opposite. (5) Read the cancellation reasons in the audit first — they named every cause here.

## L-2026-10-24-lease-in-the-dark (owner: «говорю, ты меня понимаешь — он не реагирует никак»)
**Symptom**: words heard, no reply. Audit: dialogue_allowed False, faces 0, camera mean luminance 21/255; lease needs 3 verified owner seconds and silence reset the count; a routed turn then died in its own `_permitted` lease check.
**Rules**: (1) When "no reaction", read `dialogue_allowed`, `participant_check`, scene faces and the camera luminance before touching the dialogue code — the cause may be physical (dark room). (2) Evidence aggregation (streaks) must distinguish *pause* from *unknown presence*; only the latter resets. (3) Every deferred-cancel rule has to cover ALL cancel sites (service, run loop, and the turn's own permission checks). (4) Swallowed exceptions in capture loops must at least record their type in status.

## L-2026-10-24-attention-and-evidence (owner: «он не понимает меня совершенно… перебивает по телефону»)
**Symptoms**: no model of whom the owner talks to; a prompt flag ("alone + quiet desktop") turned a phone call into an answered question; independent cancel paths made replies "sometimes".
**Rules**: (1) A conversational agent needs an explicit attention state (idle / engaged / busy / paused) fed by sensors, not a prompt sentence; addressee = state + lexical + gaze + session, audited. (2) Before adopting a model (Smart Turn), measure it on the owner's REAL speech; synthetic TTS fixtures gave inverted verdicts. When retention forbids keeping audio, run the model in shadow and keep only numbers. (3) Heavy dependency sets go into their own environment with a resident worker, never into the runtime venv (75 packages incl. torch). (4) Answer the owner's "where is my data" question with a table from the installed system, not from the spec.

## L-2026-10-24-shared-model-queue (owner: «отвечает через 14 секунд», «ты ничего не поменял»)
**Symptom**: the dialogue request waited 9.9 s in Ollama's queue (first_token_ms) behind a background vision batch that began while the owner was still speaking; the "yield" only covered Butler's own busy states.
**Rules**: (1) Interactive and batch inference never share one model instance: give the dialogue its own model name (a copy shares the weights on disk) and raise the loaded-model limit. (2) "Yield to the owner" must start when the owner starts speaking (VAD/ASR phase), not when Butler starts answering. (3) Read `first_token_ms` vs `prompt_ms`: a gap there is queueing, not model speed. (4) The decoder's own confidence numbers (avg_logprob, no_speech_prob) are free; use them before shipping any transcript.

## L-2026-10-24-two-ollamas (owner: «1 — через 20 секунд, 3 — обрывает ответ, 4–6 не работает»)
**Symptoms**: delays of 10–20 s and cut replies after a "successful" deploy.
**Causes found in the audit, not by guessing**: the serving Ollama was the menu-bar app's server (no flash attention / KV q8 / model limit → 15-s reloads on every vision↔dialogue switch) because `brew services restart` migrated the label, dropped the plist variables and lost the port; a copied model name shares the runner (per digest) so "own instance" was a no-op; the panel refresh cancelled private turns without a lease; `company=not alone` turned "unverified" into "someone present".
**Rules**: (1) After any service restart, verify which PID owns the port and its environment (`lsof -iTCP:PORT`, `ps eww`). (2) Never assume a tool's semantics (Ollama copy = new runner); measure with `ollama ps` after a request. (3) Any privacy "hide" on a read path must not have write side effects (cancelling turns). (4) "Unknown" is not "present": only positive evidence of company suppresses direct speech. (5) One deploy per owner test round; batch fixes.

## L-2026-10-25-1 · One session, not one per mode
Private and general dialogue each kept their own "conversation active" clock (`last_direct`, `general_last`). The lease flapped between two turns, so a private answer was followed by a general follow-up that nobody considered a continuation. Rule: any state that means "the owner is talking to Butler" lives in one place (the attention machine) and every path reads it there. When adding a mode or path, grep for parallel copies of session state first.

## L-2026-10-25-2 · A single-frame vision verdict needs corroboration
"talking_to_person" from one camera frame with one face put the owner alone into BUSY and silenced follow-ups. A VLM judging one frame cannot tell "talking to the camera" from "talking to someone off camera". Rule: a vision boolean that suppresses behaviour must be corroborated by an independent sensor (face count, second voice, mic app); otherwise it is evidence for the audit, not a gate.

## L-2026-10-25-3 · Sensing never sits in the reply path
`look_thread.join(3.0)` before planning added up to three seconds to every unnamed reply while the owner was complaining about latency. Rule: slow sensing runs in parallel and may veto (cancel) a turn; it never delays its start. Check every `join`/wait in `_turn` when latency is reported.

## L-2026-10-25-4 · Make silent drops visible
Seven utterances recognized to empty text left no trace in the feed; the owner saw "no reaction" and I saw nothing to debug. Rule: every drop of owner input leaves a mark with metadata (duration, counts), never a bare `return`.

## L-2026-10-25-5 · A security ritual the owner meets every minute is a defect
The 2.5-second lease and the per-utterance voice re-check were correct for the threat model written on day one, but the owner lives alone with the Mac and hit «подтверждение владельца» on every task. Rule: when a protection fires many times a day on the normal case, the threat model is wrong for this user; bring the question to him with concrete options (as DEC-066) instead of tuning thresholds in the dark. Keep identity thresholds; change when verification is required.

## L-2026-10-25-6 · Editing openclaw.json restarts the gateway
Adding `gateway.http.endpoints.chatCompletions.enabled` to `~/.openclaw/openclaw.json` made the gateway reload and restart itself (log: «loading configuration… starting…»; the port was closed for a few seconds). DEC-050 requires asking before any gateway restart; a config edit under the gateway's watcher IS a restart. Rule: treat every write to openclaw.json as a gateway restart: ask the owner first, do it at a quiet moment, verify channels afterwards.

## L-2026-10-25-7 · Never raise the owner's windows
Every deploy relaunched the panel with `open -a Butler.app`, which brought its window to the front; the owner asked why the window keeps appearing. Rule: relaunch background apps with `open -g -j -a` (no foreground, hidden) and only if they were running; the owner opens windows himself.

## L-2026-10-25-8 · A fallback must know what it does not know
When the agent timed out, the local model answered the mail request from a prompt that mentions images and produced a confident irrelevance. Rule: a fallback answers only questions it can answer with what it has; for anything that needed the failed component it says so in one honest sentence.

## L-2026-10-25-9 · One session, one turn in flight
A detached long task and the owner's next question hit the same agent session; the gateway restarted the session and the long task died. Rule: an agent session takes one turn at a time; concurrent speech goes to a side session, and the result of the first turn still comes back. Whatever the transport returns as text can still be an error message: classify before speaking.

## L-2026-10-25-10 · Transport sentinels are not speech
NO_REPLY, «Error: internal error» and a usage footer all arrived as plain text from the agent endpoint and were spoken. Rule: before anything from a text channel reaches the voice, classify it: sentinel → silence, error → honest sentence, footer/markdown → stripped, only then words.

## L-2026-10-25-11 · A parallel veto may only veto what it saw
`_look_and_veto` cancelled a turn on ANY busy state, including a microphone app that appeared while the frame was being judged, and threw away a ready agent answer. Rule: a side check started for one signal (vision) may act only on that signal's verdict; other signals were already checked before it started and have their own path.

## L-2026-10-25-12 · Filler words are part of the conversation
Canned «сейчас посмотрю» felt like a machine to the owner. Rule: anything Butler says while waiting must be about the request itself (what he is doing), generated with the owner's own words, and still guarded: no numbers, no facts, no answer attempts — a wrong "result" is worse than a canned phrase, which stays as fallback.

## L-2026-10-25-13 · A single-line label can resize the whole window
AppKit: an NSTextField with default horizontal compression resistance (750) beats the window's size when its text is long; the feed stamp carried up to 900 characters of event text and the window grew to 7075 pt and was saved that way. Rule: every single-line label in the panel gets `lineBreakMode = .byTruncatingTail` and compression resistance 1, and a restored or saved window frame is clamped to its screen's visible frame.

## L-2026-10-25-14 · Fix the wire format before splitting a protocol between two implementers
The panel agent and I implemented the two ends of `ui-display-select`/`ui-display-action` in parallel; I chose positional argv, the agent chose the existing one-JSON-argument pattern, and the CLI had to be realigned afterwards. Rule: when delegating one side of a command, state the exact argv and JSON forms in the brief (or point at the single existing pattern to copy) instead of leaving the choice to the implementer.

## L-2026-10-25-15 · «Не слышит» has two separate readings in the feed
`heard` + «ЗАНЯТ С ДРУГИМ» means hearing works and the meeting detector is quiet (a Chrome tab held the mic: a Messenger call room). `asr-empty … −60 dBFS` means the input gain is too low (system input volume was 29 %). Rule: on a hearing complaint read `voice.meeting` and `voice.input` from ui-state before touching anything; list Chrome tabs for call rooms; do not assume the service is broken.

## L-2026-10-25-16 · An instruction file has a budget; check it before appending
Twice today I appended sections to the end of Butler's AGENTS.md (MarkView, Дисплей) while OpenClaw injects only the first 12 000 characters of a 48 000-character file — the agent never saw either, and the owner concluded the work was not done. Rule: before adding to any agent bootstrap file, measure it against the injection cap (`openclaw doctor` → «Bootstrap file size», or `len()` vs `bootstrapMaxChars`, default 12 000); put rules into the core or into a playbook the core indexes, and verify afterwards with one real agent turn, not by reading the file.

## L-2026-10-25-17 · Test the agent's own call path, not a stand-in
I verified `butler_display` through a raw MCP stdio session and through the panel, but the agent calls tools through mcporter, which takes a JSON payload only via `--args` (or key=value). My example gave a bare JSON string, so every agent call failed with the generic policy message and Butler told the owner «вывод на дисплей на паузе». Rule: after wiring a tool for an agent, run one call exactly as the agent instructions spell it (same binary, same quoting) before telling the owner it works; a generic refusal text should name the class of failure (call form / validation / policy) so the agent cannot invent a reason.
