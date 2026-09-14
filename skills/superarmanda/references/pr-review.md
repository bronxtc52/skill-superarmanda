# GitHub PR review gate

`SUPERARMANDA_DIR` задаётся для выбранного host в [profiles.md](profiles.md).

`python3 "$SUPERARMANDA_DIR/scripts/pr_review.py" check --repo OWNER/NAME --pr N --head FULL_SHA --output OUTSIDE_REPO.json [--worktree LOCAL_CHECKOUT]`
performs only `gh api --method GET` requests. It fetches reviews, line comments and issue
comments page by page, then fetches the PR again. A changed head, a missing full `commit_id`,
unknown bot format, pending result, auth/API failure, or incomplete evidence never passes.
The JSON result contains `status`, `current_head`, `draft`, evidence URLs, findings and
limitations. Exit code 0 means the read-only evidence collection succeeded, including
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

Two clean-comment formats are accepted, each a structure, not a wording list. The legacy format
(unchanged by #442) is:

```
Codex Review: Didn't find any major issues. :rocket:

**Reviewed commit:** `f4b817dec1`
```

Its abbreviated SHA is resolved through `gh api repos/OWNER/NAME/commits/SHORT_SHA` and must
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

`--worktree` names the local checkout solely for the output boundary; `--output` must be outside
that checkout before the script makes any GitHub request. When the current directory is outside a
Git checkout, `--worktree LOCAL_CHECKOUT` is required; otherwise it defaults to the current
directory and must resolve to a local Git worktree.
