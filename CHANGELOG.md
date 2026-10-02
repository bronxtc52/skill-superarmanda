# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — по `version` в
`skills/superarmanda/SKILL.md`.

## 0.8.0 — 2026-10-02

### Добавлено

- `wab.py owner-handover <chain.json> <волна> <run_id>`: владелец подтверждает, что сам смержил PR
  текущей волны вне гейта, и цепочка идёт дальше без ручной правки `state.json` (проверки: прогон,
  замок диспетчера, PR `MERGED` ровно на HEAD волны, merge-коммит в `origin/<base>`). Строка
  `BLOCKED` гейта «PR уже смержен вне гейта» называет эту команду готовой строкой. Негодный
  `next-prompt.md` следующей волны — отказ до передачи; `chain-result.md` пишет «смержен владельцем».
  ([#36](https://github.com/bronxtc52/skill-superarmanda/issues/36), п. 2)

### Исправлено

- Привязка новой сессии волны после `/clear` пропускает служебные строки Claude Code в начале
  транскрипта. ([#35](https://github.com/bronxtc52/skill-superarmanda/pull/35))

## 0.7.0 и ранее

История — в `git log`.
