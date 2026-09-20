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
CLI, не OS sandbox для процесса CLI. Под sandbox Claude Code (маркер `SANDBOX_RUNTIME`/`CLAUDE_CODE_HOST_HTTP_PROXY_PORT` в окружении) child получает ровно proxy-переменные песочницы (`HTTP(S)_PROXY`/`NO_PROXY`/`ALL_PROXY` + lowercase): и только если все они указывают на loopback (`localhost`/`127.0.0.1`/`::1`) — это доверенный маршрут egress, а не подмена routing: без него сети нет вовсе, а маркер рядом с внешним proxy вырезается как подмена; прочие `*PROXY*` по-прежнему вырезаются. Вход через существующий HOME/CODEX_HOME;
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
