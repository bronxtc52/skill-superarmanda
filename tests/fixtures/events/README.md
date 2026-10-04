# Фикстуры журнала событий (events.log)

Снято с живых прогонов диспетчера волн (не написано по памяти):

- `waves-finish-events.log` — `events.log` цепочки `waves-finish`, прогон `2026-10-03-wf`
  (`skill-superarmanda-session-artifacts/waves-finish/runs/waves-finish/2026-10-03-wf/events.log`),
  264 строки, 2026-10-03..04.
- `waves-tails-events.log` — одна строка «фоновые хвосты в окне волны» (2026-10-04 14:23:48Z, W2)
  из `skill-superarmanda-session-artifacts/waves-tails/runs/waves-tails/2026-10-04-wt/events.log`.

Что заменено: пути `/home/azureuser/...` -> `/home/USER/…`; префикс токена `sk-ant-` в цитате
волны -> `sk-XXX-`; строки длиннее 300 символов обрезаны (` …` в конце). Префикс времени,
`Wn: `, ключевые слова и форма строк не менялись. Личных данных и реальных секретов в журнале нет.
