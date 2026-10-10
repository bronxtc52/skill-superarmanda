# GitHub PR review gate

`SUPERARMANDA_DIR` задаётся для выбранного host в [profiles.md](profiles.md).

`python3 "$SUPERARMANDA_DIR/scripts/pr_review.py" check --repo OWNER/NAME --pr N --head FULL_SHA --output OUTSIDE_REPO.json [--worktree LOCAL_CHECKOUT]`
sends only GraphQL `query` operations through `gh api graphql` (read-only; never a `mutation`, no REST).
It reads the PR header (`headRefOid`, `isDraft`, `state`), then **every page** of reviews, review
threads (and, per thread, every page of its comments), issue comments and the check runs of the
HEAD commit (suites, then each suite's runs), and finally reads the PR header again. Pagination is
fail-closed: a missing `pageInfo`, non-list `nodes`, `hasNextPage` without `endCursor`, a repeated
cursor, more than `MAX_PAGES` (50) pages of one connection, a null `pullRequest`, an `errors` key,
no `data`, or any `gh` failure ends the run with exit code 2 and the fixed message
`pr_review: gh api graphql failed` (raw diagnostics are never shown). GraphQL was chosen because
under a secondary REST rate limit every REST GET returned 403 while GraphQL kept working (#20).
Authors are normalized to the REST shape the rules were written for: a `Bot` gets the `[bot]`
suffix and type `Bot`; any other type (a `User` named `chatgpt-codex-connector` included) is never
trusted; a missing author is untrusted. The evidence is read twice; a changed head or a changed
snapshot is `incomplete`. A changed head, a missing full `commit_id`,
unknown bot format, pending result, auth/API failure, or incomplete evidence never passes.
The JSON result contains `status`, `current_head`, `draft`, evidence URLs, findings and
limitations, plus the informational `check_runs` (`name`, `status`, `conclusion`, `app`) and
`checks` (`total`, `pending` names, `failed` pairs; only `COMPLETED` + `SUCCESS` counts as
passed). They never change `status`, which is about the review only; unreadable check runs
fail the whole run instead of producing an empty list. Exit code 0 means the read-only evidence collection succeeded, including
`findings` or `incomplete`; it is **not** a passed review gate. The coordinator must
inspect JSON `status`, verify current-HEAD completion and explicitly dispose of
every finding before recording approval. Never use shell exit status as approval.
It does not use the local cross-provider `packet_hash`: GitHub evidence is bound
to the PR's full commit SHA.

The mandatory identity is exactly `chatgpt-codex-connector[bot]` with GitHub actor type `Bot`; CodeRabbit
(`coderabbitai[bot]`) is optional. An `APPROVED` Codex review for the exact current full SHA is
clean only when it has no body or attached review comments. A `COMMENTED` review with findings,
and all substantive trusted-bot review/comment threads, are emitted as findings for coordinator
disposition. They are never silently erased. CodeRabbit unavailability does not block, but any
actual CodeRabbit finding remains a finding.

### CodeRabbit: результат по факту ревью HEAD (1.0.2, #72)

`check` кладёт в результат поле `coderabbit` = `{"status", "reason", "evidence_url"}`; оно есть всегда. Если HEAD PR или evidence изменились между снимками, `coderabbit` тоже `pending` (как и `status` = `incomplete`).
(при HEAD ≠ ожидаемому — `pending`). Основной `status` и список `findings` оно не меняет. Учитываются
только элементы с login `coderabbitai[bot]` и типом `Bot`.

- `findings` — inline-комментарий CodeRabbit на HEAD (не шаблон-заглушка), либо его review на HEAD с
  непустым телом, с прикреплёнными комментариями или в состоянии `CHANGES_REQUESTED`/`DISMISSED`.
  Находки перекрывают любое доказательство.
- `pass` — нет находок на HEAD и есть доказательство ревью HEAD по любому из двух каналов:
  1. review CodeRabbit с `commit_id` = HEAD в состоянии `COMMENTED`/`APPROVED`/`CHANGES_REQUESTED`
     (в том числе с пустым телом);
  2. сводный issue comment CodeRabbit (тела review этот канал не дают), где среди строк вне блок-цитат (первая непробельная
     литера `>` — цитата) есть `No actionable comments were generated` или `Actionable comments posted: N`
     И диапазон `between <полный SHA> and <полный SHA>` (40 или 64 hex), конец которого равен HEAD
     без учёта регистра. Маркер и диапазон — в одном и том же комментарии. Сокращённые SHA не принимаются.
- `unavailable` — доказательства и находок нет, но в теле доверенного комментария/review есть явный отказ
  (`review limit reached`, `rate limited`, `no credits available`, `reviews are disabled`, без учёта
  регистра) и в том же теле есть диапазон `between X and Y`, конец которого равен HEAD (диапазон может
  быть в цитате). Отказ без диапазона или с диапазоном на другой HEAD не считается: это `pending`, а
  `unavailable` по таймауту даёт правило координатора ниже. `reason` называет фразу, `evidence_url` — комментарий.
- `pending` — всё остальное (`no CodeRabbit review bound to current HEAD yet`; для известного шаблона
  пропуска черновика — `CodeRabbit skipped the draft PR`). Скрипт не ждёт.

Правило координатора. Пока `coderabbit.status == pending`, повторять `check`. Если CodeRabbit не дал
доказательства за 30 минут после завершения Codex на HEAD — записать
`task-result --role coderabbit --status unavailable` с причиной timeout. Запись роли `coderabbit` по полю:
`pass` → `pass`, `findings` → `findings`, `unavailable` → `unavailable`. `unavailable` гейт мерджа не
блокирует; `findings` разбираются (исправление, `fix-loop --defer` или `--accept`).
Фикстуры — `tests/fixtures/pr-review/coderabbit/` (PR #70, #71, #592).

Two clean-comment formats are accepted, each a structure, not a wording list. The legacy format
(unchanged by #442) is:

```
Codex Review: Didn't find any major issues. :rocket:

**Reviewed commit:** `f4b817dec1`
```

Its abbreviated SHA is resolved through GraphQL `repository.object(expression: SHORT_SHA)` (a
Commit `oid`; null or non-Commit means unresolved, not pass; a gh/GraphQL failure or `errors` while resolving is a refusal, rc 2) and must
equal the requested full SHA; prefix matching is not used. The same exact summary is accepted in
a current `APPROVED` or `COMMENTED` Codex review body only after the review `commit_id` and
resolved body SHA both match the requested full SHA, with no current attached inline comment.
CRLF is normalized. The rocket summary may have the exact legacy footer
`<details><summary>About Codex</summary>Automated review.</details>`.

The second (observed-connector) format begins `Codex Review: Didn't find any major issues.`
optionally followed by one short closing phrase, then the fixed `**Reviewed commit:** \`SHA\``
line and the exact GitHub connector footer beginning `<details> <summary>ℹ️ About Codex in
GitHub</summary>`, including its observed whitespace before `</details>`. The closing phrase is
accepted by *property*, not by a fixed wording list (Codex's own phrasing varies run to run —
`:rocket:`, `You're on a roll.`, `Chef's kiss!`, … have all been observed live): it is optional;
when present it is one line, 1–48 characters. Its **first character** must be non-whitespace
and none of `<`, `>`, `[`, `]`, `` ` ``, `#`; its **remaining characters** (spaces allowed) must
each be neither a newline nor one of that same `<>[]\`#` set — the forbidden characters are
rejected throughout the phrase, not only at one end of it. A bare leading `#123 fixed`,
`<script src=x`, or `[see notes` does not qualify as a phrase (fails on the first character),
and neither does one with a forbidden character in the middle, such as `ok <b>x</b>`,
`ok [x](y)`, `` ok `sha` ``, or `ok #1` (fails on a later character). A phrase that is
empty-after-a-trailing-space, spans multiple lines, carries
markdown/HTML markup, or exceeds 48 characters does not match, and the comment falls through to
`findings` for coordinator disposition — same as a weakened connector footer (e.g. a missing
space in `<details> <summary>`). **The phrase itself is never proof of a clean review** — proof
is always the pair (`gh api`-resolved commit SHA equals the requested full HEAD) and (zero inline
comments/findings at that HEAD), exactly as for the legacy format. A Codex comment containing
`<!-- codex-pull-request-review-summary -->` records completion only and never proves a clean
review. Other formats are incomplete.

The 18 live Codex clean-answer variants (all phrases observed, `tests/fixtures/pr-review/codex-clean-variants.json`)
are recognized by a test, together with mutations of each (extra line, markup or over-long phrase,
altered footer, other SHA, a `User` with the same login) that never pass (#5). Live GraphQL answers
for PR #71 (`pr71-graphql.json`) replay the whole `check` against a fake `gh`.

Each trusted current-HEAD inline comment is emitted independently of its parent review with its
`commit_id` and `original_commit_id`; its reason states that it applies to the current HEAD. A stale,
missing or unrelated parent cannot hide it. CodeRabbit boilerplate is ignored only for supported
whole-message templates (including the known draft-skip template); markers and status words inside
other text never suppress a finding.

After the user has authorized a PR review, the coordinator may make one manual request with
`gh pr comment ... --body-file` whose exact two lines are
`<!-- superarmanda:codex-review head=FULL_SHA -->` and `@codex review`. Before posting, inspect
comments for that exact marker for the same SHA, so retries are idempotent. Do not wait without a
bound; run `check` later or resume from saved state. Keep the PR draft until the required review
is ready. Manual Codex review on draft PR #426 has been proven; there is still no ready-PR/API
fallback or automatic draft transition.

## Финальная сверка перед снятием draft (1.2.3, #86)

Для задачи `high` перед снятием draft свежий субагент (`final_check`, модель из `state.py role-model`; бриф — в
[role-briefs.md](role-briefs.md)) сверяет итоговый diff с ТЗ: каждый пункт приёмки закрыт и доказан
тестом или командой. Пробелы возвращаются в работу обычным кругом; результат записывается
`task-result --role final_check --model <модель из role-model>` и попадает в тело PR. С 1.2.4 `pass` сверки на
финальном HEAD как последнее слово по задаче (после последнего ревью задачи, закрытия последнего круга
исправлений и любой другой записи ролей готовности и `fix-loop` задачи — `task_epoch`) входит в готовность задачи high и в гейт мерджа
high-волны (`workflow.md`, «Обязательные роли задачи high»); draft PR и Codex-ревью можно делать до
сверки, новый HEAD по их находкам сбрасывает её через `resume`.

## Шаблон тела PR

Тело PR собирает координатор по manifest (`state.py where`: `risk`, `artifacts`,
`accepted_limitations`). Секции обязательны, пустая — словом «Нет».

```markdown
## Что и зачем

<изменение и причина; Closes/Refs #N>

## Проверки (HEAD <sha>)

- `<команда>` — <итог>
- Риск задачи: <low|medium|high>. Модель coder и tester: <claude-opus-5-5 | claude-sonnet-5-5 | claude-fable-5-1> (из manifest).
- Переключение волны на Opus по лимиту Fable: <нет | да — с какой сессии>.
- Ревью задачи: Astra (`claude-host`) — <pass | findings: решение>; для риска high также Fable (`codex-host`) — <pass | findings: решение>.
- Запасное Opus-ревью (`codex-host-opus`): <нет | да — вместо Fable; подтверждение квоты Fable: отчёт `codex-host` с `error_category: quota` на этом HEAD и пакете, `<путь из quota_evidence>`>.
- Финальная сверка с ТЗ (`final_check`, модель из manifest): <pass | пробелы: что возвращено в работу | не применялась или пропущена: причина>.

## Принятые ограничения

<Нет. | по строке на каждую запись `fix-loop --accept` с severity medium/high: источник, что принято и почему>

## Остаток (вне приёмки)

<Нет. | отложенные находки `fix-loop --defer` и куда они перенесены>
```

Строка о запасном Opus-ревью пишется всегда: читатель PR должен видеть, что второе ревью делала
не Fable, и чем подтверждена её квота.

`--worktree` names the local checkout solely for the output boundary; `--output` must be outside
that checkout before the script makes any GitHub request. When the current directory is outside a
Git checkout, `--worktree LOCAL_CHECKOUT` is required; otherwise it defaults to the current
directory and must resolve to a local Git worktree.
