# `state.json` диспетчера 1.4.2 работающей цепочки (`w2-legacy-1.4`)

Используется классом `W2LegacyStateMigration` (`tests/helpers/superarmanda_waves_test.py`, волна W2 цепочки
`waves-remnants`, [#105](https://github.com/bronxtc52/skill-superarmanda/issues/105) п.1,
[#103](https://github.com/bronxtc52/skill-superarmanda/issues/103) п.3): старый `state.json` с текстом волны в полях
загружается без отказа, сохраняет все решения и после первой записи не содержит текста.

## Откуда снято

Живых `state.json` с нужной формой нет: все сохранённые прогоны прошлых цепочек
(`skill-superarmanda-session-artifacts/*/runs/*/state.json` на 2026-10-11) — уже завершённые цепочки, где все волны
`done`. В них нет ни `policy_keys`, ни `policy_key_pending`, ни `policy_refusal`, ни outbox с неотправленным
уведомлением. Живая цепочка `waves-remnants` (1.4.x) этих путей ещё не проходила. Формы старых полей, которые там
есть (`last_status`, `questions[].text`, `notified`), покрывают живые фикстуры `gate-redact-run` и `w7-notices`, и тот
же тест прогоняет и их.

Поэтому эти три файла записал **сам диспетчер 1.4.2** (`wab.py` коммита `23e8d87`, без правок) на его настоящих путях.
Скрипт съёмки — `capture.py` рядом, он запускается только на `23e8d87`. Обвязка — `FixedAnswerPolicy` теста того же
коммита: manifest и `runs.json` выпускает `state.py`, транспорт Telegram «лежит» (`_send_telegram` бросает `OSError`),
tmux подменён так же, как во всём наборе. Руками в сценарии сделано одно, как в
`test_acc07_acc09_repeat_in_the_same_run_and_after_a_watch_restart_goes_to_the_owner`: перезапуск волны. Старая
запись уходит в `attempts`, без `attempts`/`outbox`, как это делает `launch`.

| Файл | Путь диспетчера | Что в нём есть |
|---|---|---|
| `answered.state.json` | ранняя попытка спросила владельца (`BLOCKED` без метки), волна перезапущена; правило с `answer` ответило на развилку `needs_decision` | `policy_keys`, `notified.policy_answer`/`blocked`, `policy_answered_stamp`, `auto_answers: 1`, неотправленное `outbox.policy_answer` с `text`, `attempts[0]` с `questions` и `last_status` |
| `refused.state.json` | то же, затем та же развилка новым эпизодом: машинная проверка отказала (ключ уже отвечен) | всё выше плюс окончательный отказ `policy_refusal`, неотправленное `outbox.blocked` с `text` в поясе `Asia/Dubai`, `questions` |
| `typed.state.json` | ответ правила набран в окно, `watch` умер до Enter | `pending_enter: "policy answer"`, `pending_text_head`, `policy_pending`, `policy_key_pending` (`key`, `status`, `text`) |

## Что заменено

- абсолютные пути временного каталога теста: каталог прогона → `@RUN_DIR@`, рабочая копия волны → `@REPO@`, прочее →
  `@TMP@`. Тест подставляет свои каталоги;
- секретов, личных данных и текста переписки в файлах нет: строки статуса — тестовые строки обвязки
  (`FixedAnswerPolicy.ND`, «вопрос владельцу ранней попытки»), имя цепочки `demo`, `run_id` `2026-10-01`;
- остальное не правилось. Поля и форма — как их записал `save_state` 1.4.2.

Что тест делает руками (описано в docstring класса): пишет файл `status` строкой `last_status` записи и ставит его
`[inode, mtime]` в `policy_answered_stamp` / `policy_refusal.stamp` (это идентичность файла на машине, которая его
записала), а `next_at` неотправленного уведомления — в 0, чтобы срок повтора уже наступил.
