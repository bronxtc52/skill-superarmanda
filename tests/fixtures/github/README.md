# Живые ответы GitHub для подставного `gh` (tests/helpers/wab_e2e_fakes/gh.py)

Источник: ЖИВЫЕ ответы GitHub на смердженный PR #73 репозитория `bronxtc52/skill-superarmanda`
(head `cb4ab96…`, merge-коммит `6536a45…`), сняты 2026-10-04 только запросами на чтение:

| Файл | Команда снятия |
|---|---|
| `pr-view.json` | `gh pr view 73 --repo <repo> --json number,headRefOid,isDraft,state,baseRefName,headRepositoryOwner,headRepository,mergeCommit` |
| `pr-list.json` | `gh pr list --repo <repo> --head <ветка PR> --base main --state all --limit 5 --json number,headRefOid,isDraft,state,baseRefName,headRepositoryOwner,headRepository` (массив из одного PR) |
| `check-runs.json` | `gh api "repos/<repo>/commits/<head>/check-runs?per_page=100&page=1"` |
| `pull.json` | `gh api repos/<repo>/pulls/73` |
| `reviews.json` | `gh api "repos/<repo>/pulls/73/reviews?per_page=100&page=1"` |
| `review-comments.json` | `gh api "repos/<repo>/pulls/73/comments?per_page=100&page=1"` |
| `issue-comments.json` | `gh api "repos/<repo>/issues/73/comments?per_page=100&page=1"` |
| `review-threads.json` | `gh api graphql -f query=<THREADS_QUERY из gate.py> -F owner=… -F name=… -F number=73` (query, не mutation) |
| `commit.json` | `gh api repos/<repo>/commits/cb4ab96` (так шлюз раскрывает короткий SHA из сводки Codex) |

## Что заменено при сохранении

- `bronxtc52/skill-superarmanda` → `{REPO}`, `skill-superarmanda` → `{NAME}`, логин владельца репозитория
  и автора PR (человек) → `{OWNER}` (в объектах пользователя id/avatar/url — нейтральные, имя — `Owner`);
  e-mail автора коммита → `owner@example.invalid`.
- SHA: head → `{HEAD}` (`{HEAD7}`, `{HEAD10}` — короткие формы), предыдущий коммит PR, на котором писал
  первый Codex-ревью, → `{STALE}` (`{STALE7}`, `{STALE10}`), merge-коммит → `{MERGE}`, база → `{BASE}`.
  Остальные SHA (родители, tree в `commit.json`) оставлены как в снимке.
- Номер PR 73 → `{NUMBER}` (в URL и в поле `number`), node_id → `{NODE}`.
- Тексты, написанные человеком: заголовок и описание PR, тело ответа на замечание, сообщение коммита —
  нейтральные строки; сопроводительные комментарии-команды `@codex review`, `@coderabbitai review` и
  метка `<!-- superarmanda:codex-review head=… -->` оставлены (их читает код, это не свободный текст).
  `patch` файлов в `commit.json` → `{TEXT}`, имена файлов → `path/to/file`.
- Токен `scope=…` в ссылке CodeRabbit → `scope=REDACTED`.
- Остаётся без замены: логины и тип ботов `chatgpt-codex-connector[bot]` / `coderabbitai[bot]` (их читает
  код), тексты ботов, формы и набор полей, даты, числовые id комментариев.

## Что подставляет `gh.py` (только значения, форма и поля — из снимка)

Подстановка `{…}` значениями мира теста; поверх неё — `isDraft`, `state`, `baseRefName`, `mergeCommit`
(`pr view`/`pr list`), `state`/`merged`/`draft`/`head.sha`/`base.ref` (`pulls`). Состояния, которых в живом
снимке нет, получены правкой ТОЛЬКО значений полей статуса живого ответа:

- check-runs: `none` — пустой список, `total_count: 0`; `in_progress` — `status: in_progress`,
  `conclusion: null`, `completed_at: null`; `failure` — `conclusion: failure`. `success` — снимок как есть
  (два живых запуска `tests (ubuntu-latest)` и `tests (macos-latest)`).
- Codex `none`: из ревью, inline-комментариев, комментариев PR и тредов убраны записи Codex и ответ автора
  на его замечание; `head` — снимок как есть (ревью Codex на предыдущем коммите, сводка и комментарий
  «Didn't find any major issues» на head — так выглядит чистый head в живом PR); `stale` — то же, но
  сводка и комментарий Codex сдвинуты на предыдущий коммит (`{HEAD*}` → `{STALE*}`), то есть Codex
  писал не на head.

## Неживые ответы (единственные, не снятые с GitHub)

- `gh pr merge` — это мердж, снять его без побочного эффекта нельзя; подставной `gh` делает настоящий
  squash в локальный bare-репозиторий и ничего не печатает (как настоящий `gh pr merge`).
- `gh pr create` (печатает URL нового PR) и `gh pr ready` — мутации, играет «сессия волны»; снять их
  без записи в GitHub нельзя.
