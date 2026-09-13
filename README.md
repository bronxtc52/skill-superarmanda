# Superarmanda

Standalone repository for the `superarmanda` agent skill. It owns the skill
definition, its local Python runtime, schemas, references, and regression tests.
Fleet-wide policy does not live here: admission remains the responsibility of the
installed `agent-config` host integration at `~/.claude/bin/cc-autonomy.py` and
its published [allowlist](https://github.com/bronxtc52/agent-config/blob/main/rules/autonomy-allowlist.md).
The skill invokes that installed integration before autonomous coding; it does
not duplicate host policy or make an admission decision itself.

## Установка

Клонируйте репозиторий в ожидаемый внешний checkout и направьте оба клиента на
канонический каталог skill:

```bash
git clone https://github.com/bronxtc52/skill-superarmanda.git ~/projects/skill-superarmanda
ln -s ~/projects/skill-superarmanda/skills/superarmanda ~/.claude/skills/superarmanda
ln -s ~/projects/skill-superarmanda/skills/superarmanda ~/.codex/skills/superarmanda
```

Перед созданием ссылок убедитесь, что целевые имена свободны. Установка не
создаёт и не изменяет `~/.claude/bin/cc-autonomy.py`: это host integration из
`agent-config`, который должен быть установлен отдельно. Инструменты standalone
skill (`scripts/state.py`, `scripts/review.py`, `scripts/codex_review.py` и
`scripts/pr_review.py`) работают с локальным состоянием и review-пакетами;
они не заменяют policy fleet, allowlist или approval-gates хоста.

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
