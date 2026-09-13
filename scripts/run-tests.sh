#!/usr/bin/env bash
# Run every standalone Superarmanda test suite locally and in CI.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="${TESTS_DIR:-$REPO/tests}"
shopt -s nullglob
suites=("$TESTS_DIR"/*.test.sh)

if [ ${#suites[@]} -eq 0 ]; then
  printf 'no test suites found in %s\n' "$TESTS_DIR" >&2
  exit 2
fi

failed=()
for suite in "${suites[@]}"; do
  name="$(basename "$suite" .test.sh)"
  printf '▶ %s\n' "$name"
  if bash "$suite"; then
    printf '  ✓ %s\n' "$name"
  else
    failed+=("$name")
    printf '  ✗ %s\n' "$name" >&2
  fi
done

if [ ${#failed[@]} -gt 0 ]; then
  printf 'failed suites: %s\n' "${failed[*]}" >&2
  exit 1
fi
