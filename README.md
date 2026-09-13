# Superarmanda

Это standalone-репозиторий agent skill `superarmanda`: здесь живут определение
skill, локальный Python runtime, схемы, references и регрессионные тесты.
Fleet policy остаётся в `agent-config`: admission выполняет установленная
host-интеграция `~/.claude/bin/cc-autonomy.py` по опубликованному
[allowlist](https://github.com/bronxtc52/agent-config/blob/main/rules/autonomy-allowlist.md).
Skill вызывает эту интеграцию перед автономной разработкой, но не дублирует
policy и не принимает admission-решения самостоятельно.

## Установка

Клонируйте репозиторий в ожидаемый внешний checkout и направьте оба клиента на
канонический каталог skill:

```bash
git clone https://github.com/bronxtc52/skill-superarmanda.git ~/projects/skill-superarmanda
mkdir -p ~/.claude/skills ~/.codex/skills
ln -s ~/projects/skill-superarmanda/skills/superarmanda ~/.claude/skills/superarmanda
ln -s ~/projects/skill-superarmanda/skills/superarmanda ~/.codex/skills/superarmanda
```

Перед созданием ссылок убедитесь, что целевые имена свободны. Установка не
создаёт и не изменяет `~/.claude/bin/cc-autonomy.py`: это host integration из
`agent-config`, который должен быть установлен отдельно. Инструменты standalone
skill (`scripts/state.py`, `scripts/review.py`, `scripts/codex_review.py` и
`scripts/pr_review.py`) работают с локальным состоянием и review-пакетами;
они не заменяют policy fleet, allowlist или approval-gates хоста.

Для существующей fleet-установки используйте sync из `agent-config`, а не
перезаписывайте действующие символьные ссылки этими командами.

## Обновление

```bash
git -C ~/projects/skill-superarmanda pull --ff-only
```

Обе символьные ссылки продолжают указывать на тот же checkout. После обновления
проверьте, что клиент перечитал skills согласно правилам вашего host.

## Проверки

```bash
bash scripts/run-tests.sh
```

Наборы в `tests/` переносят исходные сценарии runtime. CI запускает тот же
runner на Ubuntu и macOS. Происхождение файлов и точное соответствие исходнику
зафиксированы в [MIGRATION.md](MIGRATION.md).
