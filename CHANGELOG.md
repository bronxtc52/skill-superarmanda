# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — по `version` в
`skills/superarmanda/SKILL.md`.

## 0.11.0 — 2026-10-03

### Добавлено

- Машинная метка строки `BLOCKED` от волны: `BLOCKED: [class=<класс> rec=<вариант> red=<yes|no>] …`
  (классы `needs_decision`, `blocked_cap`, `plan_mismatch`, `question`; `merge_gate` — только
  диспетчер). Строка без метки или с испорченной меткой обрабатывается как раньше.
  ([#41](https://github.com/bronxtc52/skill-superarmanda/issues/41))
- Раздел «Политика решений» в `mandate.md` (`- auto: class=<класс> [rec=<вариант>]`), пинится тем
  же `mandate_sha256`; строгий разбор: неизвестный ключ/класс, `red`, `merge_gate`, `plan_mismatch` — отказ `launch`
  и `watch`. ([#41](https://github.com/bronxtc52/skill-superarmanda/issues/41))
- Автоответ диспетчера на разрешённую политикой развилку вне красной зоны: текст в окно волны путём
  `say`, строка в `<волна>/policy-decisions.log`, событие и информирующее уведомление
  `policy_answer`; один ответ на эпизод, повтор после неудачной доставки.
  ([#41](https://github.com/bronxtc52/skill-superarmanda/issues/41))
- `max_auto_answers` в `chain.json` (по умолчанию 3, настраивается на лету): после него — обычный
  `BLOCKED` владельцу и событие `policy cap reached`.
  ([#41](https://github.com/bronxtc52/skill-superarmanda/issues/41))

### Исправлено по ревью

- Кап `max_auto_answers` считается по волне целиком (текущая попытка и все `attempts`):
  перезапуск волны его не сбрасывает. ([#49](https://github.com/bronxtc52/skill-superarmanda/pull/49))
- При заданном `mandate_sha256` отсутствующий, пустой или с чужой первой строкой `mandate.md`
  отвергается до вывода «политики нет»: `watch` не стартует. ([#49](https://github.com/bronxtc52/skill-superarmanda/pull/49))
- Перед вводом автоответа `status` перечитывается: строка сменилась — ответ не шлётся.
  ([#49](https://github.com/bronxtc52/skill-superarmanda/pull/49))
- Успешный повтор автоответа снимает обычный сигнал `blocked` этого эпизода и ATTENTION.
  ([#49](https://github.com/bronxtc52/skill-superarmanda/pull/49))
- `plan_mismatch` исключён из классов политики (поправка плана требует «ок» владельца и нового
  пина): правило отвергается разбором, метка допустима и всегда идёт владельцу.
  ([#49](https://github.com/bronxtc52/skill-superarmanda/pull/49))
- Разбор политики пропускает блоки кода по правилу CommonMark (закрывает тот же символ не короче
  открывающего): пример политики внутри мандата не становится правилом.
- Недоставленный до конца автоответ, снятый при смене статуса, засчитывается в `max_auto_answers`
  (он мог уйти до падения `watch`).

## 0.10.0 — 2026-10-03

### Добавлено

- Без Telegram уведомление больше не пропадает в `notify(skipped)`: `tmux display-message` всем
  клиентам сервера окон волн и файл `<run_dir>/ATTENTION` (время UTC, волна, первая строка после
  `redact()`, команда attach); файл удаляется с концом эпизода (не-`BLOCKED` после `BLOCKED`,
  подтверждение координатора, `launch` следующей волны).
  ([#40](https://github.com/bronxtc52/skill-superarmanda/issues/40))
- `wab.py attention <chain>`: печатает ATTENTION, код 1 при открытом сигнале, 0 без файла.
  ([#40](https://github.com/bronxtc52/skill-superarmanda/issues/40))
- `wab.py say <chain> <волна> <файл-текста>`: ответ в окно волны путём диспетчера с проверкой
  отправки по `capture-pane` (строка ввода `❯` пуста, нет `paste again to expand`), до 3 повторных
  Enter, иначе код 3; событие в `events.log`; без замка прогона и записи state.
  ([#42](https://github.com/bronxtc52/skill-superarmanda/issues/42))

### Исправлено

- Доставка диспетчера (`send_text`) не считает текст, оставшийся в превью вставки или в строке
  ввода, отправленным: общая проверка `submit`, повтор Enter; при отказе `pending_enter` остаётся,
  и следующий тик жмёт только Enter с той же проверкой.
  ([#42](https://github.com/bronxtc52/skill-superarmanda/issues/42))
- `display-message` экранирует `#` (`##`): tmux читает текст как формат, и `#(…)` из текста волны
  выполнил бы команду на tmux-сервере.
- Проверка отправки смотрит только активную зону ввода (строка `❯` и подвал): фраза из истории
  экрана больше не считается превью вставки; повтор Enter после перезапуска диспетчера сверяет
  сохранённое начало текста (`pending_text_head`).
- Короткий замок ввода окна волны (`<run_dir>/<волна>/input.lock`) общий у доставок диспетчера и
  `say`: два текста больше не склеиваются в одном поле ввода; занят — доставка откладывается.
  Пока диспетчер должен Enter (`pending_enter`), поле ввода зарезервировано: `say` отказывает.
- `state.json` старой версии (`pending_enter` без начала текста): Enter дожимается как раньше, но
  с уведомлением «доставка не проверена».

## 0.9.0 — 2026-10-02

### Добавлено

- `state.py fix-loop --defer --source <ревьюер> --note "<что отложено и куда>"`: находки уровня
  low/P3 (nit, minor) переносятся в остаток следующей волны/задачи и не сжигают кап — `fix_cycles`,
  `fix_sources` и `decision_required_for` не меняются, запись идёт в новое поле `deferrals`, а не в
  `decisions`. Только для `cross_provider_reviewer`, `github_codex_review`, `coderabbit` (tester —
  нет) и только на результате `findings` текущего head; не в `blocked`/`needs_decision`. Готовность
  задачи и гейт диспетчера (одна функция `state.is_deferred`) засчитывают `findings`, только если
  deferral привязан ровно к этому результату (sha256 его записи с уникальным `result_id`) и его head;
  `where` показывает счётчик `deferred`.
  ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36), п. 3)

### Исправлено

- `wab.py`: `refresh_workdir`, `owner-handover` и `workdir_state` проверяют чистоту дерева через
  `git status --porcelain --untracked-files=all` — неотслеживаемый файл больше не прячется за
  `status.showUntrackedFiles no`. ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36), п. 7)

### Документация

- `references/waves.md`: ограничение `owner-handover` — запускать, когда волна в `BLOCKED`; гонка с
  живым окном — следующий `launch` откажет, коммиты остаются в ветке волны.
  ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36))

## 0.8.0 — 2026-10-02

### Добавлено

- `wab.py owner-handover <chain.json> <волна> <run_id>`: владелец подтверждает, что сам смержил PR
  текущей волны вне гейта, и цепочка идёт дальше без ручной правки `state.json` (проверки: прогон,
  замок диспетчера, PR `MERGED` ровно на HEAD волны, merge-коммит в `origin/<base>`). Строка
  `BLOCKED` гейта «PR уже смержен вне гейта» называет эту команду готовой строкой. Негодный
  `next-prompt.md` следующей волны или грязное дерево волны — отказ до передачи; `chain-result.md` пишет «смержен владельцем».
  ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36), п. 2)

### Исправлено

- Привязка новой сессии волны после `/clear` пропускает служебные строки Claude Code в начале
  транскрипта. ([#35](https://github.com/bronxtc52/skill-superarmanda/pull/35))

## 0.7.0 и ранее

История — в `git log`.
