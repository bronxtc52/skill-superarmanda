# Формы строк журнала сессии Claude Code (`~/.claude/projects/<cwd>/<session>.jsonl`)

Источник: живые журналы Claude Code 2.1.288 / 2.1.289, записанные на этой машине сессиями волн цепочки
`waves-finish` (каталог `~/.claude/projects/-tmp-cc-admission-*-checkout/`), снято 2026-10-04.
Из журналов взята только СТРУКТУРА строк. Каждое строковое значение, кроме перечисленных ниже
служебных констант, заменено заглушкой; секретов и личных данных в фикстурах нет (проверено обходом
всех строковых значений после санитизации).

Файлы:

- `first-session-head.jsonl` — начало журнала первой сессии волны (`1eb4d9c1…`, строки 0–8): `custom-title`,
  `agent-name`, `mode`, `permission-mode`, `atis-latch`, `file-history-snapshot`, два `attachment`
  (хуки SessionStart) и первое сообщение пользователя: живая форма — вставка
  `\n\n<pasted_content id="…">\n<маркер> …\n</pasted_content id="…">\n`.
- `clear-session-head.jsonl` — начало журнала сессии ПОСЛЕ `/clear` (`3ad76723…`, строки 0–17): шапка и хуки
  `SessionStart:clear`, затем `isMeta`-предупреждение `<local-command-caveat>`, эхо `/clear`
  (`<command-name>/clear</command-name>…`), системная строка `local_command` с пустым
  `<local-command-stdout>`, `last-prompt`, повтор шапки и первое сообщение пользователя. Строка перед
  `last-prompt` включительно — то, что Claude Code пишет при самом `/clear`; остальное появляется, когда
  приходит сообщение. Последняя строка в живом журнале — команда `/update` (формат до 0.15.0); здесь
  имя команды заменено на `/superarmanda`, форма та же (`<command-message>`, `<command-name>`,
  `<command-args>`); текст аргументов — заглушка `{COMMAND_ARGS}`, подставляется тестом.
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
| текст сообщений, вывод хуков, `thinking`, `signature`, `input` инструмента, `lastPrompt` | `{TEXT}` (вывод хуков и `command` — пустая строка) |
| текст первого сообщения | `{MARKER} {TEXT}` внутри живой обёртки `<pasted_content>` |
| поля `serverClassifierContext`, `serverClassifierRequest`, `wireToolInputs`, `toolUseResult` | удалены (диспетчер их не читает) |

Оставлено без замены (служебные константы, не данные): `type`, `subtype`, `role`, `userType`, `entrypoint`,
`version`, `level`, `mode`, `permissionMode`, `model`, `stop_reason`, `service_tier`, имя инструмента,
`origin.kind`, `turnOrigin`, `promptSource`, текст `<local-command-caveat>` и эхо `/clear` (константы
самого Claude Code), числа (в том числе `usage`: тест перезаписывает их, чтобы задать размер контекста).

Подстановка заглушек — `Journal` в `tests/helpers/superarmanda_waves_e2e_test.py`.
