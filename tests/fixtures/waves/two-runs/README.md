# Два прогона одной волны (`two-runs`)

Снято с живого прогона цепочки `waves-tails`, волна W1 (2026-10-04, `runs/waves-tails/2026-10-04-wt/W1/`),
на которой гейт мерджа судил заблокированный r1, хотя работа шла во втором прогоне (#76).

| Файл фикстуры | Откуда | Что это |
|---|---|---|
| `W1/superarmanda/manifest.json` | `superarmanda/manifest-run1.json` живого прогона | r1, задача `w1-bg-tasks` в статусе `blocked` |
| `W1/superarmanda/manifest-run2.json` | `superarmanda/manifest.json` живого прогона | r2, задача `w1-bg-tasks-r2` в статусе `ready_for_pr_review` |
| `W1/runs.json` | `runs.json` живого прогона | две записи: r1 -> `manifest.json`, r2 -> `manifest-run2.json` |

Файлы имеют вид «как на момент отказа»: координатор потом переставил их руками (`manifest-run1.json` / `manifest.json`),
фикстура возвращает расположение, которое записал `state.py init --from-plan` (r1 в стандартном пути, r2 в новом).

Что заменено:

- абсолютные пути: каталог волны -> `@WAVE_DIR@`, каталог прогона цепочки -> `@RUN_DIR@`, рабочая копия -> `@REPO@`,
  прочие `/home/azureuser/...` -> `@PATH@`. Тест подставляет `@WAVE_DIR@` в `runs.json` и `@REPO@` (настоящий
  пустой git-репозиторий вне каталога волны) в manifest-ы, чтобы работал `state.py where`;
- строки длиннее 160 символов (тексты заметок, формулировки плана и приёмки) обрезаны до 157 + `...`;
- формат полей (`run`, `tasks`, `results`, `head`, `tree_fingerprint`, `acceptances`, ...) не менялся.

Для вердикта `pass` подставлять ничего не нужно: r2 сам по себе проходит `gate.manifest_problems` с собственными
`head` и `tree_fingerprint`; тест берёт эти значения из файла и строит под них факты GitHub и состояние рабочей копии.
Файлы не править руками.
