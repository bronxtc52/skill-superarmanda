# Workflow

## Host admission boundary

Сначала прочитай применимые host/project instructions. Host mandate для
admission обязателен. Также проверь локальные сигналы managed integration:
`~/.claude/rules/autonomy-allowlist.md` и
`~/.claude/bin/cc-autonomy.py`. Наличие любого из них означает, что нужно
прочитать локальную policy и выполнить её prepare-flow; отсутствующий второй
обязательный компонент или ошибка prepare означает BLOCKED. Не копируй policy
в skill и не считай частичную установку разрешением. Только когда оба сигнала
отсутствуют и host mandate нет, создай обычный изолированный Git feature
worktree с собственным origin пользователя. Установка skill не меняет remotes,
не выполняет fetch/login и не создаёт сетевые действия; обычный Git workflow
после setup следует применимым project instructions.

## Delivery workflow

1. Reader даёт координатору ссылки на релевантные исходники. Координатор фиксирует
   requirements, acceptance criteria, risk, task boundaries, base/head SHA и checks.
2. Для architectural/high-risk задачи внешний reviewer проверяет план. Coder получает
   только свой brief; один coder пишет в один момент времени.
3. Coder подтверждает красный тест, реализует и запускает checks. Fresh tester получает
   требования и снимок исходников, воспроизводит позитивные, негативные и интеграционные
   проверки. Tester не меняет production-код.
4. После tester отдельный provider проверяет задачу по контракту. На итоговом diff этот
   review повторяется. Пакет содержит requirements, base/head SHA, diff, необходимые файлы
   и результаты тестов; его размер проверяется заранее, усечение запрещено.
   После подтверждённого provider quota event/notice Fable coordinator может
   сделать отдельную Opus-попытку через `codex-host-opus`, сохранив оба artifact;
   никакой runner не переключает профиль автоматически.
5. Findings возвращаются coder. После каждой неудачной corrective attempt, включая failure от
   fresh tester, единственный coordinator вызывает `fix-loop --outcome failed`; coder → tester →
   reviewer повторяется. Это ручная дисциплина coordinator, а не автономное доказательство
   durable enforcement. Третья неудача блокирует задачу. Изменение head инвалидирует результаты
   старого SHA.
6. После всех task reviews создаётся draft PR. GitHub Codex должен завершить review именно
   текущего HEAD. Повторные запросы на тот же head идемпотентны и не опрашиваются бесконечно.
   CodeRabbit необязателен, но существенные полученные findings разбираются.

Недоступный обязательный reviewer/auth/model/quota — `blocked`/`unavailable`, а не pass.
Исключение только для подтверждённого Fable quota: сама Fable-попытка остаётся
unavailable, но отдельная успешная закреплённая Opus-попытка на том же current
SHA может закрыть gate; неуспешная Opus-попытка оставляет PR draft.
Если GitHub review невозможен до ready PR, не меняй draft policy для обхода: запиши зависимость
и запроси решение пользователя. После нового HEAD старый PR review также устарел.

## Local state interface

`SUPERARMANDA_DIR` задаётся для выбранного host в [profiles.md](profiles.md).

`python3 "$SUPERARMANDA_DIR/scripts/state.py" init --manifest <path> --repo <repo> --base <sha> --head <sha>`
creates one manifest atomically. Before any result after a code or working-tree change, run
`resume` with the same `--repo --base` and current `--head`; it removes stale results but keeps
`fix_cycles`. Один coordinator является единственным writer manifest; POSIX lock также защищает
короткие read-modify-write операции. Store a manifest outside the repository or at a gitignored
path, so writing local state cannot itself change the reviewed tree. `status` reports `tree_matches`
without mutating state and never reports stale evidence as `ready_for_pr_review`.

Record a role with `task-result --task <id> --role <role> --status <status> --session-id <id> --head <sha>`.
The only valid roles are `coder`, `tester`, `cross_provider_reviewer`,
`github_codex_review`, and `coderabbit`; valid statuses are `pass`, `findings`,
`incomplete`, `error`, and `unavailable`.
Cross-provider `pass` must contain matching `--reviewed-head <sha>` and
`--packet-hash sha256:<64 lowercase hex>`; a mismatched reviewed head is rejected. GitHub Codex
`pass` instead requires matching `--reviewed-head` and an HTTPS evidence artifact URL, with no
packet hash. Every SHA here is the full hexadecimal commit ID emitted by Git, never an abbreviated
or normalized caller value. State stores artifact metadata but does not dereference its URL or
assert its continued existence; coordinator validates the artifact and adapter report has
`gate_ready: true` before recording a pass.
The script rejects a session ID used by another task or role anywhere in the run and rejects a changed worktree
until `resume`. A task becomes `ready_for_pr_review` only when current coder, tester and
cross-provider reviewer results all pass. GitHub Codex review remains a separate PR gate;
CodeRabbit cannot satisfy either gate. `fix-loop --outcome failed` persists each failed round;
the third makes the task permanently `blocked` in v1. `fix-loop --outcome pass` never substitutes
for required role results. Task IDs are coordinator-approved identifiers: renaming a blocked task
is not a reset. v1 supplies no reset command; any human decision to resume work requires a new,
explicitly documented run rather than editing the manifest.

Every `--repo` is canonicalized to the Git top-level. Relative packet `--context`
paths are resolved from that top-level even when `--repo` names a subdirectory.
An absolute `--context` path must use that physical Git root; when the root is
reached through an alias, supply the repository-relative context path instead.
Fingerprint domain `v3` hashes raw index/worktree entries with explicit delimiters;
`resume` rewrites the current manifest to invalidate legacy `v1`/`v2` evidence
while retaining counters and immutable session ownership. It never rewrites an
issued packet artifact.
For an ordinary repository its v3 byte encoding is unchanged. Initialized
gitlinks are now traversed to depth 32 only after their directory, `.git`
endpoint, metadata routing and per-module filter configuration are validated;
empty or absent uninitialized gitlinks are represented explicitly, while
nonempty uninitialized and unsafe routes fail closed. Consequently, a manifest
that previously recorded an uninitialized gitlink can invalidate on resume.
`fix-loop --outcome pass`
also verifies the current HEAD and fingerprint before it can preserve readiness.
Nonignored empty directories are rejected during fingerprint validation because v3
does not encode them; add a tracked `.gitkeep` when a source directory must exist.
Ignored directories and files remain outside this gate.
Nested repositories must be gitignored or declared as gitlinks; unsupported nested repositories fail closed.

Manifest and its persistent flock file are validated both lexically and
physically; an in-worktree location is allowed only when ignored and untracked.
Packets and result artifacts are strictly outside the worktree and Git metadata.
Manifest/lock symlink endpoints and paths escaping through an in-worktree symlink
are rejected. A benign alias above the repository root (such as macOS `/var`)
preserves the same boundaries and permits ordinary ignored-directory manifests.
Packet/result replacement uses
a private same-directory temporary file, fsync and rename, so an external
hardlink is safely replaced rather than truncating its shared inode.
