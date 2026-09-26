# Host profiles

Роль означает обязанность в процессе, а не доказанную идентичность модели.
Каждая native-сессия создаётся свежей, с явной моделью; она не наследует полную
беседу координатора: для Codex указывай `fork_turns="none"`. Проверяй доступность
и фактические CLI metadata на запуске.

| Роль | Claude Code host (проверенный default) | Codex host (проверенный default) |
|---|---|---|
| coordinator / planner | доступная явная native-модель (Fable) | доступная явная native-модель (`gpt-6-astra`) |
| coder | fresh explicit native model (`coder@sonnet`) | fresh explicit native model (`coder@gpt-5.6-terra`) |
| tester | fresh explicit native model (`tester@sonnet`) | fresh explicit native model (`tester@gpt-5.6-terra`) |
| task reviewer | Codex CLI / fixed adapter | Claude CLI / fixed adapter |
| context / drafts | `reader`/`drafter@haiku` | `reader`/`drafter@gpt-5.6-luna` |
| required PR review | GitHub Codex | GitHub Codex |
| optional PR review | CodeRabbit | CodeRabbit |

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
фактически отвечала. Для task review используй другой провайдер, чем host coder.

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

Он сохраняет исходный Fable failure artifact вместе с Opus result artifact.
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
