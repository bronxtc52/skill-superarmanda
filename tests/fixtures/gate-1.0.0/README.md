# Замороженный гейт 1.0.0

Копии из коммита `a844fff` (релиз 1.0.0), `git show a844fff:skills/superarmanda/scripts/<файл>`:

| Файл | sha256 |
|---|---|
| `scripts/state.py` | `95f43fda4c79bb0c03c2c284bb31763ca3ab715c5434505a04774a288a18f132` |
| `scripts/pr_review.py` | `a2ce66ace9b74382e2b989b52818e8d3f646a3d10494525dba2d7283f771a482` |
| `scripts/waves/gate.py` | `960c4a7f5b68dadd34d962cd7862c115b560fa972332a5b0846a161d79677eb8` |

Зачем: тест совместимости (`GateCoderabbitUnavailable` в `tests/helpers/superarmanda_waves_test.py`)
сравнивает вердикты `manifest_problems` гейта 1.0.0 и текущего на одних и тех же manifest-ах. CI
клонирует репозиторий с глубиной 1, `git show` там недоступен. `gate.py` грузит соседей
(`state.py`, `pr_review.py`) по пути относительно себя, поэтому раскладка `scripts/` сохранена, а
`pr_review.py` добавлен: без него старый гейт не импортируется. Файлы не править.
