# Provenance and moved-file map

The original Superarmanda runtime was moved from its owner-maintained source
repository, `https://github.com/bronxtc52/agent-config`, at immutable commit
`5272047475e1dace50f31733c72d1f17dae8d264`. This is historical provenance,
not a runtime dependency: no access to that repository is required. The
standalone repository owns subsequent maintenance and imports no third-party
upstream runtime. The helpers retain source-relative paths because the
standalone repository keeps the canonical `skills/superarmanda` layout. This
provenance describes the initial move at `f10e538485a1d43e6a604214d845aa6e5e6fb34b`;
it does not claim current files remain byte-identical.

| Source path | Standalone path |
|---|---|
| `skills/superarmanda/SKILL.md` (modified: published host-integration links) | `skills/superarmanda/SKILL.md` |
| `skills/superarmanda/references/profiles.md` | `skills/superarmanda/references/profiles.md` |
| `skills/superarmanda/references/workflow.md` | `skills/superarmanda/references/workflow.md` |
| `skills/superarmanda/references/review-contract.md` | `skills/superarmanda/references/review-contract.md` |
| `skills/superarmanda/references/pr-review.md` | `skills/superarmanda/references/pr-review.md` |
| `skills/superarmanda/schemas/review-result.schema.json` | `skills/superarmanda/schemas/review-result.schema.json` |
| `skills/superarmanda/scripts/state.py` | `skills/superarmanda/scripts/state.py` |
| `skills/superarmanda/scripts/review.py` | `skills/superarmanda/scripts/review.py` |
| `skills/superarmanda/scripts/codex_review.py` | `skills/superarmanda/scripts/codex_review.py` |
| `skills/superarmanda/scripts/pr_review.py` | `skills/superarmanda/scripts/pr_review.py` |
| `tests/helpers/superarmanda_state_test.py` | `tests/helpers/superarmanda_state_test.py` |
| `tests/helpers/superarmanda_review_test.py` | `tests/helpers/superarmanda_review_test.py` |
| `tests/helpers/superarmanda_repository_test.py` | `tests/helpers/superarmanda_repository_test.py` |
| `tests/helpers/superarmanda_pr_review_test.py` | `tests/helpers/superarmanda_pr_review_test.py` |
| `tests/helpers/superarmanda_codex_review_test.py` | `tests/helpers/superarmanda_codex_review_test.py` |
| `tests/superarmanda-state.test.sh` | `tests/superarmanda-state.test.sh` |
| `tests/superarmanda-review.test.sh` | `tests/superarmanda-review.test.sh` |
| `tests/superarmanda-repository.test.sh` | `tests/superarmanda-repository.test.sh` |
| `tests/superarmanda-pr-review.test.sh` | `tests/superarmanda-pr-review.test.sh` |
| `tests/superarmanda-codex-review.test.sh` | `tests/superarmanda-codex-review.test.sh` |

## Standalone additions

| Added path |
|---|
| `.github/workflows/ci.yml` |
| `.gitignore` |
| `AGENTS.md` |
| `MIGRATION.md` |
| `README.md` |
| `scripts/run-tests.sh` |
| `tests/helpers/standalone_layout_test.py` |
| `tests/standalone-layout.test.sh` |
| `tests/portable-install.test.sh` |
| `tests/helpers/portable_install_test.py` |
| `PORTABILITY.md` |
| `LICENSE` |
| `skills/superarmanda/LICENSE` |
| `skills/superarmanda/scripts/install-skill.py` |
