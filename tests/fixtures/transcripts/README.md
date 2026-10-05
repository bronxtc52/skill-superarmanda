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

Граница: это форма ЖУРНАЛА. Живого снимка stdout `claude -p --output-format stream-json` в момент квоты нет.
Двойник CLI в `tests/helpers/superarmanda_review_test.py` строит событие потока из `message` и `error` этой
фикстуры по схеме вывода установленного Claude Code 2.1.289 (событие `assistant`: `message`, `parent_tool_use_id`,
`error`, `uuid`, `session_id`). Появится живой снимок потока — заменить им.
