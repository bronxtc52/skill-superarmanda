# Формы строк журнала сессии Claude Code (`~/.claude/projects/<cwd>/<session>.jsonl`)

Источник: живые журналы Claude Code 2.1.288 / 2.1.289, снято 2026-10-04 (первая сессия и `assistant-turns` — сессии
волн цепочки `waves-finish`; `clear-session-head` — отдельный живой снимок сессии после `/clear`, см. ниже).
Из журналов взята только СТРУКТУРА строк. Каждое строковое значение, кроме перечисленных ниже
служебных констант, заменено заглушкой; секретов и личных данных в фикстурах нет (проверено обходом
всех строковых значений после санитизации).

Файлы:

- `first-session-head.jsonl` — начало журнала первой сессии волны (`1eb4d9c1…`, строки 0–8): `custom-title`,
  `agent-name`, `mode`, `permission-mode`, `atis-latch`, `file-history-snapshot`, два `attachment`
  (хуки SessionStart) и первое сообщение пользователя: живая форма — вставка
  `\n\n<pasted_content id="…">\n<маркер> …\n</pasted_content id="…">\n`.
- `clear-session-head.jsonl` — начало журнала сессии ПОСЛЕ `/clear` (строки 0–24 живого журнала): `mode`,
  `file-history-snapshot`, хуки `SessionStart:clear`, `isMeta`-предупреждение `<local-command-caveat>`, эхо
  `/clear`, системная строка `local_command` с пустым `<local-command-stdout>`, затем то, что Claude Code пишет,
  когда в новой сессии введена команда: `file-history-snapshot`, сама строка `/superarmanda --wave … --resume`
  (`<command-message>`, `<command-name>`, `<command-args>`), `isMeta`-раскрытие скилла и служебные `attachment`
  (`environment`, `model`, `skill_listing`, `instructions`, `session_context`, …), `atis-latch`, `last-prompt`.
  Источник — ЖИВОЙ снимок 2026-10-04, Claude Code 2.1.289: в чистой сессии выполнен `/clear`, затем введена
  команда `/superarmanda --wave W9 --resume [wab:…]`; ответ модели не ждали (прервано). Команда ушла дважды —
  взята одна. Строки до эха `/clear`… `local_command` включительно Claude Code пишет при самом `/clear`;
  строки с командой и далее появляются, когда приходит ввод (тест `Journal.after_clear` режет файл по строке
  команды). Текст аргументов — заглушка `{COMMAND_ARGS}`, метку и каталог подставляет тест.
- `assistant-turns.jsonl` — три строки хода (`1eb4d9c1…`, строки 30–32): `assistant` с блоком
  `thinking`, `assistant` с блоком `tool_use` (оба с полным `message.usage`) и `user` с `tool_result`.
  Диспетчер читает отсюда `type`, `isSidechain`, `message.usage.*_tokens`, `message.content[].type`.

Что заменено:

| Было | Стало |
|---|---|
| `sessionId`, `session_id` | `{SESSION_ID}` |
| `cwd` | `{CWD}` |
| `gitBranch` | `{BRANCH}` |
| `timestamp` | `{TIMESTAMP}` |
| `uuid`, `parentUuid`, `leafUuid`, `messageId`, `promptId`, `toolUseID`, `requestId`, `tool_use_id`, `id` | `{UUID:n}` — один и тот же исходный id даёт один и тот же `n`, связи между строками сохранены |
| `customTitle`, `agentName` | `{NAME}` |
| текст сообщений, вывод хуков, `thinking`, `signature`, `input` инструмента, `lastPrompt`, текст скилла и содержимое `attachment` (пути, e-mail, снимок окружения, список скиллов, git status) | `{TEXT}` (вывод хуков и `command` — пустая строка) |
| текст первого сообщения | `{MARKER} {TEXT}` внутри живой обёртки `<pasted_content>` |
| поля `serverClassifierContext`, `serverClassifierRequest`, `wireToolInputs`, `toolUseResult` | удалены (диспетчер их не читает) |

Оставлено без замены (служебные константы, не данные): `type`, `subtype`, `role`, `userType`, `entrypoint`,
`version`, `level`, `mode`, `permissionMode`, `model`, `stop_reason`, `service_tier`, имя инструмента,
`origin.kind`, `turnOrigin`, `promptSource`, текст `<local-command-caveat>` и эхо `/clear` (константы
самого Claude Code), числа (в том числе `usage`: тест перезаписывает их, чтобы задать размер контекста).

Подстановка заглушек — `Journal` в `tests/helpers/superarmanda_waves_e2e_test.py`.

## `rate-limit-assistant.jsonl` — исчерпанная подписка Claude (1.2.1, #86)

Две живые строки журнала сессии Claude Code, которые CLI записал сам, когда провайдер ответил 429:

1. лимит сессии подписки (Claude Code 2.1.259, 2026-09-06): текст «You've hit your session limit · resets …»,
   поле `quotaLimits` со `status: rejected`;
2. закончились кредиты (Claude Code 2.1.263, 2026-09-09): текст «You're out of usage credits…», поле `errorDetails`
   с ответом API `rate_limit_error`.

Обе строки — `type: assistant` с **верхнеуровневым** `error: "rate_limit"`, `isApiErrorMessage: true`,
`apiErrorStatus: 429` и `message.model: "<synthetic>"`: сообщение сочинил CLI, а не модель. Именно поле `error`
(а не слова в тексте) читает `review.py`, когда решает, что сбой Fable — квота.

Заменено по таблице выше (`uuid`, `parentUuid`, `requestId`, `agentId`, `message.id` → `{UUID:n}`, `sessionId` →
`{SESSION_ID}`, `timestamp` → `{TIMESTAMP}`), `req_…` внутри `errorDetails` → `{REQUEST_ID}`; `cwd`, `gitBranch`, `slug`
удалены. Текст сообщения и `errorDetails` оставлены как есть: это служебные строки CLI и API без личных данных.

Это форма ЖУРНАЛА сессии. Форма события в ПОТОКЕ снята отдельно — следующий раздел.

## `stream-provider-error.jsonl` — живой поток `stream-json` с ошибкой провайдера (1.2.1, #86)

Весь stdout одного живого запуска Claude Code 2.1.289 (2026-10-05, Linux) с теми же флагами, что у `review.py`,
и несуществующей моделью — инференса нет, провайдер отвечает 404:

```
echo 'Reply ok.' | claude --safe-mode -p --model claude-nonexistent-9 --tools "" --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' --settings '{"enabledPlugins":{…:false}}' --no-session-persistence \
  --permission-mode dontAsk --disable-slash-commands --output-format stream-json --verbose
```

Код выхода 1, в stderr одна строка `[claude-code:unrecognized_model] …`. В stdout три события, по одному на строку:

1. `system` / `init` (`plugins: []`, `tools: []`, `mcp_servers: []`);
2. `assistant` с верхнеуровневыми `error: "model_not_found"` и `is_api_error_message: true`, `message.model:
   "<synthetic>"` — сообщение сочинил CLI, а не модель;
3. `result` с `is_error: true`, `api_error_status: 404`, `terminal_reason: "api_error"`.

Заменено: `session_id` → `{SESSION_ID}`, `uuid`, `request_id`, `message.id` → `{UUID:n}`, `timestamp` →
`{TIMESTAMP}`, `cwd` → `{CWD}`, `messaging_socket_path` → `{PATH}`, `duration_ms` и `duration_api_ms` → 0.
Остальное — как в потоке, включая текст сообщения CLI.

Что отсюда читает код (`review.py::claude_stream_facts`): событие `assistant`, верхнеуровневый `error`,
`is_api_error_message: true`, `message.model == "<synthetic>"` и `api_error_status` события `result`. Имена полей
потока отличаются от журнала (`is_api_error_message` против `isApiErrorMessage`, `api_error_status` в `result`
против `apiErrorStatus` в строке журнала) — тест сверяет оба снимка.

Что живое, а что нет:

- форма события ошибки провайдера в потоке — живая (этот снимок, HTTP 404);
- значение `error: "rate_limit"`, статус 429 и тексты сообщений об исчерпанной подписке — живые, из журнала
  сессии (`rate-limit-assistant.jsonl`);
- живого ПОТОКА именно при 429 нет (#91). Двойник CLI в `tests/helpers/superarmanda_review_test.py` берёт три
  живых события этого снимка и меняет в них только то, что говорит журнал: `error`, текст сообщения и
  `api_error_status`. Если настоящий поток при исчерпанной подписке окажется другим, `review.py` не даст
  категорию `quota` и запасной Opus не откроется: ошибка возможна только в закрытую сторону. Появится живой снимок
  при 429 — положить рядом и перевести двойник на него.

## `fable-usage-journal.jsonl` — живой образец для счётчика расхода Fable (1.4.0, #108)

Источник: живые журналы Claude Code на Linux-сервере владельца (`~/.claude/projects/**/*.jsonl`), снято 2026-10-10. 26 строк из двух
журналов, порядок строк внутри каждого сохранён, промежуточные служебные строки (`attachment`, `last-prompt`, …)
пропущены:

- строки 0–13 — главная ветка одной сессии Claude Code 2.1.263 (2026-09-12, `isSidechain: false`): ответ Opus
  (`claude-opus-5`, две строки одного `message.id`), затем пять ответов `claude-fable-5-1` (два — по две строки одного
  `message.id`) и строки `user` между ними;
- строки 14–25 — журнал субагента-Fable сессии волны (Claude Code 2.1.288, 2026-10-03, `isSidechain: true`,
  `<session>/subagents/agent-….jsonl`): три ответа, у двух — несколько строк одного `message.id`.

Что показал образец (и что читает `fable_usage.py`): один ответ модели Claude Code пишет строкой на каждый блок
`content`, все с одним `message.id` и `requestId`. В главной ветке `usage` у всех строк ответа одинаковый, а в журнале
субагента ранние блоки несут ЧАСТИЧНЫЕ `output_tokens` потока (`7` → `451`, `51, 51, 51` → `465`), итог — у последнего.
Счётчик берёт на ответ наибольший `usage` (первая редакция 1.4.0 брала первую строку и недосчитывала выход субагентов). По всей машине
на дату снятия: 13 138 строк Fable, у каждой есть и `message.id`, и `requestId`; 3 830 из них — `isSidechain` (все в
`subagents/`) и считаются в расход — субагент-Fable тратит тот же лимит; 82 ответа лежат в двух журналах главной ветки
сразу (возобновлённая сессия копирует историю) — считаются один раз.

Что заменено:

| Было | Стало |
|---|---|
| `message.id` | `msg_fxNN` — один исходный id даёт один и тот же номер |
| `requestId` | `req_fxNN` (так же) |
| `uuid`, `parentUuid` | `uuid-fx-NN` (так же, связи сохранены) |
| `sessionId` | `session-fx-1` (главная ветка), `session-fx-2` (субагент) |
| `agentId` | `agent-fx-1` |
| `message.content` | список только с `type` каждого блока (`thinking`, `text`, `tool_use`, `tool_result`); строковое содержимое — `{TEXT}` |
| `cwd`, `gitBranch`, `slug`, `session_id`, `attributionSkill`, `attributionAgent`, `advisorModel`, `perTurnEffort`, `serverClassifierRequest`, `wireToolInputs`, `message.diagnostics`, `message.container`, `message.context_management`, `message.stop_details` | удалены (счётчик их не читает) |

Оставлено как есть: `type`, `isSidechain`, `apiBlockIndex`, `timestamp` (настоящие — тесты задают окна через `--now`),
`userType`, `entrypoint`, `version`, `effort`, `message.model`, `message.type`, `message.role`, `message.stop_reason`,
`message.stop_sequence` и весь `message.usage` (числа токенов, `cache_creation`, `iterations`, `service_tier`,
`inference_geo`, `speed`). Проверено обходом всех строковых значений и grep на пути, e-mail, токены и исходные id.

Синтетика в тестах (`tests/helpers/superarmanda_fable_test.py`) — только производные от названных живых строк: ответ
`msg_fx07` без `message.id` (и без `requestId`) — такой формы живьём нет; строка `msg_fx04` с подменённым `usage` — для
арифметики бюджета и строки расхода в `launch`.
