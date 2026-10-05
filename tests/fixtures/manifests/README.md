# Manifest-ы завершённых цепочек (`version: 1`)

Сняты с живых прогонов (не написаны по памяти) и показывают, что `state.py` 1.2.1 судит manifest
`version: 1` по правилам 1.2.0: один Astra-ревью, модель роли не требуется.

| Файл фикстуры | Откуда | Что это |
|---|---|---|
| `v1-high-waves-gate-redact-W1.json` | `waves-gate-redact/runs/waves-gate-redact/2026-10-05-gr/W1/superarmanda/manifest.json` | волна с `risk: high`, задача `t1` в статусе `ready_for_pr_review`, один `cross_provider_reviewer` pass |
| `v1-medium-waves-tails-W2.json` | `waves-tails/runs/waves-tails/2026-10-04-wt/W2/superarmanda/manifest.json` | волна с `risk: medium`, задача `w2-coderabbit` в статусе `ready_for_pr_review` |

Пути в столбце «Откуда» — относительно каталога артефактов сессий `skill-superarmanda-session-artifacts`.

Что заменено:

- привязка к репозиторию: `repo` -> `@REPO@`, `base` -> `@BASE@`, `head` (во всех полях: результаты, `position`,
  записи `deferrals`/`acceptances`, `reviewed_head`) -> `@HEAD@`, `tree_fingerprint` -> `@TREE@`. **Привязку подставляет
  тест**: настоящий временный git-репозиторий, его HEAD и отпечаток дерева (берётся из `state.py init`);
- абсолютные пути: каталог артефактов сессий и остаток домашнего каталога -> `@PATH@` (тест подставляет временный каталог;
  файла плана там нет, `where` отдаёт `plan_check: missing`);
- свободный текст волны (`wave.title`, `wave.goal`, `wave.requirements`, каждый пункт `wave.acceptance`) -> `<текст волны убран>`;
  заметки `note` в `deferrals`, `acceptances`, `decisions` -> `<заметка убрана>`;
- `result_sha256` записей `deferrals`/`acceptances`, которые в живом manifest покрывали текущий результат, пересчитан
  после замен выше (замена пути в `artifact` меняет результат, а запись привязана к его хешу). Тест после своей
  подстановки пересчитывает его ещё раз тем же правилом (`state.result_digest`);
- форма остальных полей (`version`, `run`, `plan`, `position`, `tasks`, `results`, `session_roles`, `fix_sources`,
  идентификаторы сессий, ссылки на PR) не менялась.

Manifest `version: 2` и отчёты `review.py` (pass, ошибка квоты, запасной Opus) в фикстурах не лежат: тесты создают их
самим `state.py` и самим `review.py` на подставных CLI из `tests/helpers/superarmanda_review_test.py`.
Файлы не править руками.
