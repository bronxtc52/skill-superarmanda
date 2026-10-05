# Живые снимки дерева процессов окна волны

Сняты 2026-10-04 координатором волны W1 цепочки `waves-tails` на сервере разработки (Linux, procps),
командой диспетчера `ps -ww -A -o pid=,ppid=,etime=,args=`. Формат строки: `pid ppid etime args`.
Из снимка оставлены только PID 1 и поддерево процесса `claude`; прочие процессы машины убраны.

- `ps-claude-bg-loops.txt` — процесс `claude` живой волны (PID 2034867) с тремя фоновыми
  Bash-задачами, запущенными через `run_in_background`: `until grep -q … never.output; do sleep 5; done`
  (ждёт файл, который не появится), `until ! pgrep -f "run-tests.sh|superarmanda-review.test.sh"; do
  sleep 5; done` (находит собственную командную строку, ожидание вечное — проявление 3 из #68) и
  `sleep 600` (молчащая долгая команда). Снимок через ~30 с после запуска.
- `ps-claude-idle.txt` — свежий `claude` в приватном tmux (`-L wabfix-<pid>`) без единого запроса:
  дочерних процессов нет.

Заменено: домашний каталог → `@HOME@` (1.2.1: раньше стояла заглушка-путь, теперь в фикстурах нет ни одного
домашнего пути и имени хоста — это проверяет `tests/helpers/standalone_layout_test.py`), путь scratchpad сессии →
`/tmp/claude-1000/<project>/<session>/scratchpad`, путь системной инструкции →
`<run_dir>/system-prompt.md`, `--session-id` → нулевой UUID. Хвосты `args` (префикс shell-snapshot
Claude Code, `eval '…'`, `pwd -P >| /tmp/claude-XXXX-cwd`) оставлены как есть: их читает код.
