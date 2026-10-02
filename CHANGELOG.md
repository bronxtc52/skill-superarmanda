# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — по `version` в
`skills/superarmanda/SKILL.md`.

## 0.9.0 — 2026-10-02

### Добавлено

- `state.py fix-loop --defer --source <ревьюер> --note "<что отложено и куда>"`: находки уровня
  low/P3 (nit, minor) переносятся в остаток следующей волны/задачи и не сжигают кап — `fix_cycles`,
  `fix_sources` и `decision_required_for` не меняются, запись идёт в новое поле `deferrals`, а не в
  `decisions`. Только для `cross_provider_reviewer`, `github_codex_review`, `coderabbit` (tester —
  нет) и только на результате `findings` текущего head; не в `blocked`/`needs_decision`. Готовность
  задачи и гейт диспетчера засчитывают `findings` с deferral той же роли не раньше результата;
  `where` показывает счётчик `deferred`.
  ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36), п. 3)

### Исправлено

- `wab.py`: `refresh_workdir` и `owner-handover` проверяют чистоту дерева через
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
