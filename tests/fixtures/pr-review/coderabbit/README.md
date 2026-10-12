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

## Сводка CodeRabbit как шаблон бота (#113 W1 п.2, 1.5.1)

Снято 2026-10-12 тем же способом (только чтение, `gh api … ?per_page=100`), в той же форме и с теми же правилами
удаления, что выше:

| Файл | PR | HEAD | Что показывает |
|---|---|---|---|
| `pr118-rest.json` | bronxtc52/skill-superarmanda#118 | `efbf9d448d7fa487f8f7fa4976e6895e40959017` | сводка CodeRabbit в состоянии «review in progress» (блок `<!-- … review in progress by coderabbit.ai -->`), флажок `<!-- autopilot:start -->` |
| `pr116-rest.json` | bronxtc52/skill-superarmanda#116 | `8556c3116d6b600dc0357e7af59f7d1acc8a5ede` | законченная сводка CodeRabbit (walkthrough, «Recent review info», `No actionable comments…`, autopilot) |
| `pr75-rest.json` | bronxtc52/skill-superarmanda#75 | `54a08e59bd1aa13c73a9c8ea61ee0c78627ac7fa` | настоящая inline-находка CodeRabbit на HEAD (`discussion_r4177677540`) рядом со сводкой: находка остаётся находкой |

Команды (для каждого N из 75, 116, 118):

```
gh api repos/bronxtc52/skill-superarmanda/pulls/N
gh api "repos/bronxtc52/skill-superarmanda/pulls/N/reviews?per_page=100"
gh api "repos/bronxtc52/skill-superarmanda/pulls/N/comments?per_page=100"
gh api "repos/bronxtc52/skill-superarmanda/issues/N/comments?per_page=100"
```

Убрано: всё, кроме элементов ботов `coderabbitai[bot]` и `chatgpt-codex-connector[bot]` (комментарии и ревью владельца
выпали); поля, которые код не читает (оставлены `id`, `state`, `commit_id`, `original_commit_id`,
`pull_request_review_id`, `html_url`, `body`, `user.login`, `user.type`); из PR — только `head.sha` и `draft`.

Заменено: параметр `scope=ghh_…` в ссылках CodeRabbit → `scope=REDACTED` (непрозрачный идентификатор области). Остальной
текст тел ботов не менялся. Секретов и личных данных, кроме публичных логинов, в телах нет (проверено поиском по
токенам и глазами; слова `password`/`SECRET` в `pr116` — цитаты предупреждений Ruff из сводки бота).

Тест `CodeRabbitServiceComment` (`tests/helpers/superarmanda_pr_review_test.py`) вставляет в живую сводку #118 блок
пропуска черновика из живого шаблона «Draft PR not reviewed» (`CODERABBIT_DRAFT_SKIP` того же файла) вместо блока
«review in progress» — живой сводки черновика нового формата в этом репозитории нет.
