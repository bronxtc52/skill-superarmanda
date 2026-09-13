# Superarmanda

`superarmanda` — standalone skill с локальным Python runtime, схемами и
регрессионными тестами. Его можно форкнуть и использовать из собственного
checkout: ваш `origin` остаётся вашим, а синхронизация с fleet автора —
отдельная политика, не часть установки.

Проект распространяется по [MIT License](LICENSE). Runtime написан и
поддерживается в этом репозитории; сторонний upstream runtime не импортируется.

## Требования

Нужны Python 3.9+ (только standard library), Git и Bash на Linux, macOS или
WSL2. Native Windows не поддерживается: runtime использует POSIX `fcntl` и
символьные ссылки. Локальные state/packet-проверки не требуют аккаунта или
сети.

Полный review требует действующие first-party подписки Claude и Codex с
поддерживаемыми CLI и model metadata. Для обязательного PR review на целевом
репозитории нужен GitHub Codex app; GitHub Actions в форке может потребовать
явного включения владельцем. Если обязательный review недоступен, PR остаётся
draft.

## Форк и безопасная установка

Клонируйте свой fork в любое место, включая путь с пробелами. Не заменяйте свой
`origin` на URL автора:

```bash
git clone https://github.com/YOU/skill-superarmanda.git "$HOME/src/my superarmanda"
cd "$HOME/src/my superarmanda"
python3 skills/superarmanda/scripts/install-skill.py install --client both
```

Installer создаёт только символьные ссылки на этот checkout. Он не делает
fetch, не меняет remote, глобальную Git-конфигурацию, `HOME`, `CODEX_HOME` или
хранилища авторизации. Повторный запуск допустим только если ссылка уже ведёт
в тот же skill; файл, каталог, чужая или dangling ссылка приводят к ошибке без
перезаписи. При `--client both` обе цели проверяются до записи.
Для переносимости installer отклоняет цели, чьи имена образуют вложение после
casefold и Unicode NFD-нормализации, даже на case-sensitive filesystem.
Нельзя устанавливать skill внутрь его собственного source checkout, включая
alias и неоднозначные case/Unicode spelling.
Пути установки с literal компонентом `..` не поддерживаются; укажите путь без
parent traversal.

Для изолированного теста или managed install укажите явный корень вместо
домашней директории пользователя:

```bash
python3 skills/superarmanda/scripts/install-skill.py install \
  --client both --target-home /tmp/superarmanda-test-home
```

Без `--target-home` Claude получает `~/.claude/skills/superarmanda`. Codex
использует `$CODEX_HOME/skills/superarmanda`, если `CODEX_HOME` задан, иначе
`~/.codex/skills/superarmanda`.

## Обновление

Обновляйте свой checkout как обычный fork:

```bash
git pull --ff-only
```

При необходимости добавьте upstream вручную, используя URL, который выбрали
вы. Установка не создаёт и не меняет upstream. После обновления ссылки остаются
на checkout, а клиент перечитывает skill по своим правилам.

## Host policy и модели

Standalone workflow создаёт обычный изолированный feature worktree. Если
локальный host или проект требует admission, либо существует
`~/.claude/rules/autonomy-allowlist.md` **или**
`~/.claude/bin/cc-autonomy.py`, сначала прочитайте локальную policy и выполните
её prepare-flow. Отсутствующий обязательный компонент блокирует автономную
работу; skill не копирует и не обходит host-правила. Наша managed fleet
продолжает использовать свою host-интеграцию.

Координатор, coder и tester выбирают явные модели, доступные на текущем host;
таблица в skill содержит проверенные defaults, а не обязательные глобальные
aliases. Subscription adapters сохраняют ограниченный набор фиксированных
профилей и точную проверку metadata. `codex-host-opus` можно явно выбрать при
первичной настройке, не дожидаясь quota Fable. Автоматического выбора модели
нет: после неудачной Fable-попытки переключение на Opus допустимо лишь при
подтверждённой quota Fable.

## Проверки

```bash
bash scripts/run-tests.sh
```

Проверки включают установку из независимой копии с путём с пробелами, конфликты
целей, сохранение fork origin и создание state/packet из установленного skill
в локальном synthetic Git project без сети.
