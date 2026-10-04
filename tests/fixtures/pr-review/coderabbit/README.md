# Живые REST-ответы по CodeRabbit (issue #72)

Снято 2026-10-04 командами `gh api` (только чтение):

| Файл | PR | HEAD | Что показывает |
|---|---|---|---|
| `pr70-rest.json` | bronxtc52/skill-superarmanda#70 | `4930cffa099e1164f6c0777196fc741d915396e8` | review CodeRabbit на HEAD с пустым телом (доказательство канала «review») |
| `pr71-rest.json` | bronxtc52/skill-superarmanda#71 | `98b72fc0900086732249dea82c6014c380500893` | review-объекта CodeRabbit нет; сводка с `No actionable comments were generated` и `between 9306e85… and 98b72fc…`; ответы `Review rate limited.`/`Action performed` |
| `pr592-rest.json` | bronxtc52/agent-config#592 | `c1fe47b26b91f046c58b02213b9aa20b85b49b70` | `Review limit reached`, диапазон только внутри блок-цитаты |

Команды:

```
gh api repos/bronxtc52/skill-superarmanda/pulls/70/reviews        # аналогично 71
gh api repos/bronxtc52/skill-superarmanda/pulls/70/comments       # inline, аналогично 71
gh api repos/bronxtc52/skill-superarmanda/issues/70/comments      # аналогично 71
gh api repos/bronxtc52/agent-config/pulls/592/reviews
gh api repos/bronxtc52/agent-config/pulls/592/comments            # пусто
gh api repos/bronxtc52/agent-config/issues/592/comments
```

Форма — та, что принимает `evaluate`: `head`, `draft`, `reviews`, `review_comments`, `issue_comments`.

Убрано: все элементы, кроме ботов `coderabbitai[bot]` и `chatgpt-codex-connector[bot]` (в PR #70 и #71
выпали review и комментарии владельца); поля, которые код не читает (оставлены `id`, `state`,
`commit_id`, `original_commit_id`, `pull_request_review_id`, `html_url`, `body`, `user.login`, `user.type`).

Заменено: в двух ссылках CodeRabbit (`pr70`, `pr71`, сводки) параметр `scope=ghh_…` заменён на
`scope=REDACTED` — это непрозрачный идентификатор области, не нужный коду. Остальной текст тел ботов
не менялся. Секретов и личных данных, кроме публичных логинов, в телах нет (проверено глазами и
поиском по токенам).
