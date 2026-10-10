# Host profiles

Роль означает обязанность в процессе, а не доказанную идентичность модели.
Каждая native-сессия создаётся свежей, с явной моделью; она не наследует полную
беседу координатора: для Codex указывай `fork_turns="none"`. Проверяй доступность
и фактические CLI metadata на запуске.

| Роль | Claude Code host (проверенный default) | Codex host (проверенный default) |
|---|---|---|
| coordinator / planner | доступная явная native-модель; координатор волны — `model` в `chain.json`, по умолчанию Opus (`claude-opus-5-5`) | доступная явная native-модель (`gpt-6-astra`) |
| coder | по политике ролей (раздел ниже): `coder@sonnet`, при high — Opus | fresh explicit native model (`coder@gpt-5.6-terra`); задачи high здесь не ведутся (#92) |
| tester | по политике ролей (раздел ниже): `tester@sonnet`, effort `high` при high | fresh explicit native model (`tester@gpt-5.6-terra`); задачи high здесь не ведутся (#92) |
| task reviewer | Codex CLI / fixed adapter; при риске high — оба адаптера | Claude CLI / fixed adapter; при риске high — оба адаптера |
| субагентские роли: `architect`, `internal_reviewer`, `triage`, `investigator`, `final_check` | по политике ролей: Opus (`claude-opus-5-5`), свежая сессия | моделей Claude на этом host нет: запись `unavailable`, без подмены (#92) |
| context / drafts | `reader`/`drafter@haiku` | `reader`/`drafter@gpt-5.6-luna` |
| required PR review | GitHub Codex | GitHub Codex |
| optional PR review | CodeRabbit | CodeRabbit |

## Модель роли по риску (1.4.0, #108)

Модель каждой исполняющей роли задаёт политика ролей manifest и риск задачи, а не привычка
координатора. Источник истины — `state.py`: он считает эффективный риск задачи и отдаёт модель
командой `role-model --role <роль>` (для tester — ещё и `effort`). Решение владельца 2026-10-10
(#108, вариант Б): Fable только ревьюит. Встроенные дефолты 1.4.0:

| Роль | low | medium | high |
|---|---|---|---|
| coder | Sonnet (`claude-sonnet-5-5`) | Sonnet | Opus (`claude-opus-5-5`) |
| tester | Sonnet, effort `medium` | Sonnet, effort `medium` | Sonnet, effort `high` |
| `architect`, `investigator`, `triage`, `internal_reviewer`, `final_check` | Opus | Opus | Opus |
| ревью задачи | одно: Astra (`claude-host`) | одно: Astra | два: Astra (`claude-host`) и Fable (`codex-host`) |

Координатор в политику не входит: в режиме волн его модель — `model` в `chain.json`
(по умолчанию Opus, [waves.md](waves.md)); ключ `coordinator` — отказ.

- **Порядок силы моделей:** Sonnet < Opus < Fable. Роль принимает модель политики или сильнее:
  Fable допустима в любой роли, Sonnet у coder high — отказ.
- **Переопределение — только вверх и только при `init`.** `state.py init --role-model
  <роль>[.<риск>]=<модель>` (флаг повторяемый) и env `SUPERARMANDA_ROLE_MODELS` с JSON
  `{"coder.high": "fable", "final_check": "fable"}`; в режиме волн — объект `role_models` в
  `chain.json`, диспетчер передаёт его окну волны этим env. Слияние по ячейкам: дефолты ← env ←
  флаги; внутри одного источника `<роль>` применяется раньше `<роль>.<риск>`. Отказ при `init`
  (manifest не пишется): модель ниже встроенного дефолта, флаг ниже env (политики цепочки), `coordinator`, неизвестные роль, риск
  или модель, повтор флага, битый JSON. Итоговая полная таблица пишется в manifest полем
  `role_models` и дальше не меняется: `resume` её сохраняет, env после `init` не читается, ручная
  правка ниже дефолта — закрытый отказ схемы.
- **Совместимость.** Manifest без `role_models` (1.2.x, 1.3.x) оценивается по дефолтам 1.4.0: это
  только ослабление требования модели, записи на Fable остаются годными. Новые manifest получают
  `review_policy.version: "1.4.0"`; `version` manifest остаётся `2`.
- **Эффективный риск задачи** — больший из риска прогона (`review_policy.level` manifest:
  риск волны при `init --from-plan`, иначе `init --risk`, по умолчанию `low`) и собственного риска
  задачи (`state.py task-risk --task <id> --risk <low|medium|high>`). Риск волны — нижняя граница
  для всех её задач, в том числе для модели: задача medium в волне high идёт на модели политики
  для high и требует двух ревью. Риск задачи можно только поднять.
- **Задачи про безопасность и маскировку обязаны иметь `risk: high`**: маскировка секретов,
  границы данных, авторизация, гейты и проверки, которые что-то запрещают. Сомневаешься — high.
- **Модель в metadata результата.** `task-result --model <значение>` принимает только
  закрытый словарь: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5` и алиасы `fable`,
  `opus`, `sonnet` (совпадение точное, без обрезки пробелов и смены регистра); в manifest пишется
  полный ID. Модель обязательна у субагентских ролей при любом риске, у coder и tester при high и
  там, где переопределение подняло политику выше Sonnet; модель слабее политики для эффективного
  риска — отказ при любом статусе. Готовность задачи high требует у coder и tester модель не
  слабее политики для high (если риск подняли уже после записи — роль переделывается).
- **Модель политики недоступна** (лимит, авторизация, модель не отвечает) — координатор записывает
  `task-result --role <роль> --status unavailable --model <модель политики>` и останавливается:
  `where` отдаёт `BLOCKED`, задача не pass. Подмены моделью слабее нет.
- **Второй ревьюер.** Для задачи high роль `second_reviewer` обязательна наравне с
  `cross_provider_reviewer`: пара отчётов `review.py` — ровно один `claude-host` (Astra) и ровно
  один `codex-host` (Fable) по одному пакету и текущему HEAD. Порядок и команда — в
  [review-contract.md](review-contract.md).
- **Запасной Opus.** `codex-host-opus` заменяет `codex-host` только при подтверждённой квоте
  Fable: запись с `--quota-evidence <отчёт codex-host>`, где отчёт — ошибка `review.py` с
  `error_category: quota` для того же HEAD и пакета. Без него Opus-ревью не засчитывается.
  Сигнал квоты проверен на живой форме события ошибки провайдера в потоке CLI и на живом значении
  `rate_limit` из журнала сессии; живого потока при исчерпанной квоте нет (#91), поэтому ошибка
  возможна только в закрытую сторону — маршрут не откроется. **Запасной Opus-маршрут не проверен
  живым событием квоты** (#91): он закреплён тестами на подставных CLI, в том числе в гейте мерджа
  и в проверке ревью плана `wab.py launch`; первый живой отказ по квоте — повод сверить форму
  события и закрыть #91.
- **Где вести задачи high.** Задача `risk: high` ведётся там, где роли можно запустить
  на моделях политики (Opus, Sonnet), то есть на Claude Code host. На хосте без моделей Claude
  (Codex host, где роли идут на `gpt-5.6-terra`) роль записывается `task-result --status
  unavailable --model <модель политики>`: задача не pass, `where` отдаёт `BLOCKED`. Это ожидаемое
  поведение политики, а не сбой; модель для
  задач high на Codex-хосте — решение владельца (#92), до него подмены нет. Задачи medium и low на
  Codex-хосте идут как в 1.2.0: `--model` для coder и tester необязателен, пока политика их не
  подняла, роли работают на моделях хоста; `role-model` для них отдаёт рекомендацию для
  Claude-хоста (Sonnet) и Codex-хосту её не навязывает.

Manifest `version: 1` (созданный до 1.2.1) оценивается по правилам 1.2.0: одна модель по таблице
host-профилей ниже, одно ревью; `role-model`, `task-risk`, `--model`, `second_reviewer`,
`--quota-evidence`, субагентские роли и `--source internal_reviewer` на нём — отказ.

### Субагентские роли (1.2.3, #86; модель — 1.4.0, #108)

Роли `architect`, `internal_reviewer`, `triage`, `investigator`, `final_check` ведут свежие
субагенты на модели политики: по умолчанию Opus (`claude-opus-5-5`) при любом риске; модель
отдаёт `state.py role-model --role <роль>`. До 1.4.0 они шли только на Fable. Шаблоны брифов — в
[role-briefs.md](role-briefs.md); когда роль применима — [workflow.md](workflow.md), «Субагентские
роли». Результат записывается `task-result --role <роль> --model <модель из role-model>`; без
модели или с моделью слабее политики запись отклоняется при любом статусе и риске. Модель политики
недоступна — `task-result --role <роль> --status unavailable --model <модель политики>`, без подмены
моделью слабее. Роли есть только в manifest `version: 2`.

### Шаблон брифа coder и tester

Модель в бриф не пишется по памяти: её отдаёт `state.py`, и она же уходит в `--model` результата.

```text
Роль: <coder|tester>, задача <id>, свежая сессия.
Модель: значение поля "model" из вывода
  python3 "$SUPERARMANDA_DIR/scripts/state.py" role-model --manifest <path> --task <id> --role <coder|tester>
  (по умолчанию coder high -> claude-opus-5-5, остальное -> claude-sonnet-5-5; claude-fable-5-1
  допустима всегда). Модель слабее не подходит; недоступна -> сообщи координатору, не подменяй.
Риск задачи: <low|medium|high> (поле "risk" того же вывода). Effort: tester — поле "effort"
  того же вывода; остальные — по таблице ниже.
Требования, приёмка, разрешённые файлы, запреты, команды проверок: <...>
Отчёт: <путь>. В отчёте назови фактическую модель сессии.

Для coder задачи с риском high по умолчанию в брифе стоит: Модель: claude-opus-5-5
Запись результата координатором:
  state.py task-result --manifest <path> --task <id> --role <coder|tester> --status <status> \
    --session-id <id> --head <sha> --artifact <отчёт> --model <model из role-model>
```

## Effort ролей

Effort субагента — параметр брифа координатора, а не свойство роли. Координатор указывает его в
брифе явно; без указания действуют значения по умолчанию:

| Роль | Effort по умолчанию | Когда выше |
|---|---|---|
| tester | `medium` | `high` для задачи с риском `high` (план или `waves.json`: `risk: high`); то же отдаёт `role-model --role tester` полем `effort`, от модели не зависит |
| reader | `low` | — (извлекает факты и ссылки `file:line`, не рассуждает) |
| coder | по выбору координатора | — |

Модель роли effort не меняет: её задаёт политика ролей (раздел выше; `reader@haiku` — всегда). Повод (#15): субагенты coder/tester
съедали больше половины суточного лимита, перечитывая репозиторий целиком. Поэтому бриф tester по
умолчанию — дифф и команды проверки, а чтение репозитория идёт через `reader`: выжимка и ссылки
`file:line`, а не файлы целиком. Если host не умеет задавать effort субагента, координатор
записывает это в отчёт и запускает роль с effort по умолчанию host'а.

Defaults above document tested host choices, not aliases required on every
installation. Coordinator, coder and tester select an available explicit
native model and record observed metadata. The subscription review adapters
below are intentionally different: their supported profiles and model IDs are
fixed, and unavailable review leaves the PR draft. A fork changes a reviewer
model only with tests and verified capabilities, never by relabelling metadata.

`codex-host-opus` may be explicitly selected as an initial supported profile
during setup without Fable quota evidence. The runner never selects it, and
the existing separate quota fallback remains limited to a confirmed failed
Fable quota attempt.

`session_id` закреплён за парой task/role на всём run и не может перейти в
другую задачу или роль даже после resume. Это разделяет сессии, но не доказывает, какая модель
фактически отвечала. Для task review используй другой провайдер, чем host coder; при риске
high — оба провайдера (раздел «Модель роли по риску»).

`scripts/review.py` реализует фиксированные подписочные adapters. `codex-host`
остаётся Fable. После подтверждённого provider quota event/notice Fable (либо
уже сохранённого для текущего run quota evidence) coordinator может запустить
отдельную попытку через фиксированный `codex-host-opus`; он сохраняет оба
artifact. Runner не выбирает Opus сам, не принимает модель от caller и не
выводит право fallback из произвольного текста диагностики или ответа. Fable
подтверждается stream metadata Claude CLI. Astra запускается через локальный
`codex app-server --stdio`: отключения применяются к процессу до initialize,
затем проверяется effective config и ChatGPT subscription. Сервер должен вернуть
точные model/provider, read-only sandbox; thread и turn получают `environments: []`.
Любой tool/approval event, model reroute или неверные IDs отклоняют ревью.
`systemError` статуса thread не обрывает чтение до терминального события; notification `error` с
`codexErrorInfo: "usageLimitExceeded"` (или такой же `turn.error` у `failed` turn) — это
`error_category: "quota"`, как и для Fable; прочие коды — `completion`, чужие ID — `protocol`.
Поддерживаются репозитории `--object-format=sha256` (полные 64-hex ID), бинарные изменения
определяются разбором `numstat -z`, а в отчёте `run` есть `state_packet_hash` (`sha256:<hex>`) для
`state.py task-result --packet-hash`.
`thread/settings/updated` допускается только как эхо того же контракта: read-only
sandbox без сети, `on-request`, точные model/provider, без permission profile,
тот же thread.

Codex сливает табличные overrides `mcp_servers={}` / `plugins={}` с
`~/.codex/config.toml`, а не замещает их. Поэтому первый процесс App Server
выполняет только `initialize` и `config/read` и узнаёт имена пользовательских
MCP-серверов и плагинов. Затем адаптер перезапускает процесс, добавив на
каждую запись `-c <table>.<name>.enabled=false`, и требует, чтобы в effective
config каждая запись была строго `enabled = false`. После этого
`mcpServerStatus/list` должен показать, что ни один сервер не запущен и не
отдаёт tools/resources. Имя, которое нельзя адресовать голым сегментом пути
(`[A-Za-z0-9_@-]`, без точек и кавычек), или более 256 записей → `config`.
Глобальный конфиг, `CODEX_HOME` и хранилища авторизации не меняются; ни один
MCP-сервер или плагин, в том числе встроенный, не разрешается включённым.
Известная граница: первый процесс стартует с пользовательскими записями как
есть (так же, как прежний одиночный процесс) и делает только `initialize` и
`config/read`; если версия Codex поднимет MCP уже на `initialize`, это
произойдёт до отключения. `mcpServerStatus/list` вызывается только после
проверки конфига. Пагинация статуса (`nextCursor`) — отказ, а не догрузка.
`thread/settings/updated` также требует `approvalsReviewer` = `user`.
Claude CLI даже под `--safe-mode` загружает встроенные плагины: 2.1.283 —
`agents-md@builtin` и `telemetry@builtin`, 2.1.289 — ещё и
`cc-plugin-plugin-authoring@builtin` (три плагина). Оба Claude-профиля передают только
дочернему процессу `--settings '{"enabledPlugins":{…:false}}'`; пользовательские
настройки не меняются. `init.plugins` по-прежнему обязан быть пустым: новый или
не выключенный плагин оставляет `gate_ready: false`. В отчёте
`capabilities.isolation_checks` содержит только булевы `known_tools`,
`mcp_empty`, `plugins_empty`, `structured_only`, без имён и сырых данных.
Проверен Codex CLI 0.154.0 на mh-central и 0.157.1 на Mac с пользовательскими
MCP и плагинами; неизвестный протокол не считается успехом.
Оба адаптера должны дать `gate_ready: true` для pass; сам статус pass без
подтверждённых capabilities не закрывает gate. Это ограничение инструментов
CLI, не OS sandbox для процесса CLI. Вход через существующий HOME/CODEX_HOME;
не копировать OAuth stores, не подменять login location, не использовать API fallback.
Первичная Fable-попытка при quota остаётся unavailable. При подтверждённом
quota evidence успешная отдельная Opus-попытка для того же current SHA может
закрыть этот review gate; неуспешная Opus-попытка остаётся unavailable и PR
остаётся draft. При auth error, лимите без такого маршрута, таймауте, неверном
JSON, неполном пакете или другом SHA обязательный review unavailable; состояние
сохраняется, PR остаётся draft.

После этого решения coordinator вызывает фиксированный профиль с уже созданным
packet и внешним output, например:

```bash
python3 "$SUPERARMANDA_DIR/scripts/review.py" run --repo /repo \
  --packet /outside/opus-packet.json --profile codex-host-opus \
  --output /outside/opus-result.json
```

Он сохраняет исходный Fable failure artifact вместе с Opus result artifact. Для задачи с
риском high оба пути уходят в manifest: `task-result --role second_reviewer --artifact
/outside/opus-result.json --quota-evidence /outside/fable-quota-result.json`.
Нода без нужного CLI, подписки или подтверждённой capability даёт review
unavailable → BLOCKED, PR остаётся draft. Mock-тесты не подтверждают доступ к
внешнему аккаунту.

## Каталог установленного скилла

Рабочий каталог принадлежит целевому проекту и не обязан содержать этот скилл.
Перед командами установи обычную shell-переменную для выбранного host:

- Codex: `SUPERARMANDA_DIR="$HOME/.codex/skills/superarmanda"`.
- Claude Code: `SUPERARMANDA_DIR="$HOME/.claude/skills/superarmanda"`.

Если скилл загружен из другого места, используй абсолютный каталог его `SKILL.md`.
Все Python-команды запускай как `python3 "$SUPERARMANDA_DIR/scripts/<name>.py"`;
`--repo`/`--worktree` указывают на целевой проект. HOME/CODEX_HOME не меняй.
