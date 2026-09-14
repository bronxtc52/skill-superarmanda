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
Проверен Codex CLI 0.154.0 на mh-central; неизвестный протокол не считается успехом.
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

## Пользовательский конфиг Codex для `claude-host`

Codex CLI 0.154.0 сливает табличные `-c` overrides с пользовательским конфигом:
`mcp_servers={}`, `plugins={}` и `hooks={}` не гарантируют пустые таблицы в
`config/read`. Адаптер проверяет полученное эффективное состояние до
`account/read`, создания thread и передачи пакета:

- `hooks.state` допускается как реестр доверия, а не исполняемый хук. Все
  остальные значения `hooks` должны быть пустыми списками; `features.hooks`
  по-прежнему должен быть явно `false`.
- В `mcp_servers` допустимы только записи-таблицы с явным `enabled = false`.
  `true`, отсутствующий `enabled`, неверный тип или смесь выключенных и
  невыключенных серверов отклоняются. `orchestrator.mcp.enabled` также должен
  оставаться явно `false`; одного этого флага недостаточно для допуска MCP.
- Непустая таблица `plugins`, в том числе с отдельными `enabled = true`,
  допустима только при одновременных `features.plugins = false` и
  `features.remote_plugin = false`. Отсутствующий либо включённый глобальный
  флаг означает отказ.

Остальные проверки сохраняются, включая read-only sandbox, `environments: []`,
отказ на tool/approval events и проверку model/provider identity. Адаптер не
редактирует пользовательский конфиг, не отключает отдельные MCP автоматически
и не меняет `HOME`/`CODEX_HOME`. Поэтому конфиг с активными MCP по-прежнему
даёт `error_category: config`; такой отказ не считается успешным ревью.

На Mac с Codex CLI 0.154.0 проверен реальный `initialize` → `config/read`:
таблицы MCP и плагинов сохраняются после пустых overrides, а оба флага
плагинов возвращаются как `false`. В отдельном тестовом процессе с точечными
`mcp_servers.<name>.enabled=false` конфиг с сохранёнными пользовательскими
плагинами принят; без этих тестовых параметров невыключенные MCP отклонены.
Пользовательский конфиг не изменялся. Положительные и отрицательные состояния
конфига, включая `hooks.state`, дополнительно покрыты синтетическими App Server
contract-тестами.
Эта проверка конфигурации не подтверждает успешное подписочное ревью Astra
на Mac; приведённая выше проверка inference на mh-central — отдельное evidence.

## Каталог установленного скилла

Рабочий каталог принадлежит целевому проекту и не обязан содержать этот скилл.
Перед командами установи обычную shell-переменную для выбранного host:

- Codex: `SUPERARMANDA_DIR="$HOME/.codex/skills/superarmanda"`.
- Claude Code: `SUPERARMANDA_DIR="$HOME/.claude/skills/superarmanda"`.

Если скилл загружен из другого места, используй абсолютный каталог его `SKILL.md`.
Все Python-команды запускай как `python3 "$SUPERARMANDA_DIR/scripts/<name>.py"`;
`--repo`/`--worktree` указывают на целевой проект. HOME/CODEX_HOME не меняй.
