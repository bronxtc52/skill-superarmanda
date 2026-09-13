# Provenance and moved-file map

Source repository: `https://github.com/bronxtc52/agent-config` at immutable
commit `5272047475e1dace50f31733c72d1f17dae8d264`.

The runtime content below was copied unchanged from that commit. The five Python
test helpers retain their source-relative paths because the standalone repository
keeps the canonical `skills/superarmanda` layout; no assertions or scenarios were
changed.

| Source path | Standalone path |
|---|---|
| `skills/superarmanda/SKILL.md` | `skills/superarmanda/SKILL.md` |
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

Standalone additions are `README.md`, `AGENTS.md`, CI, the test runner, and the
layout acceptance test. `SKILL.md` changes only its formerly repository-relative
links into HTTPS links to the authoritative `agent-config` rules and documents
the installed host integration boundary.
