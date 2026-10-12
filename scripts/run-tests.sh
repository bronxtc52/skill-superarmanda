#!/usr/bin/env bash
# Run every standalone Superarmanda test suite locally and in CI.
#
# Наборы идут параллельно: по умолчанию столько сразу, сколько CPU (nproc, на macOS sysctl hw.ncpu);
# RUN_TESTS_JOBS=<n> (1–999999) задаёт число явно, RUN_TESTS_JOBS=1 — последовательно, как раньше (вывод сразу в терминал).
# В параллельном режиме вывод каждого набора (stdout и stderr вместе) копится в файл и печатается целым блоком
# в исходном порядке наборов, как только набор и все до него закончились. Долгие наборы (SLOW_FIRST) стартуют
# первыми. Код возврата: 0 — все зелёные, 1 — упал хоть один, 2 — нет наборов или неверный RUN_TESTS_JOBS.
# Наборы идут с PYTHONDONTWRITEBYTECODE=1; порядок наборов — байтовый (glob под LC_ALL=C), локаль наборов не меняется.
# Совместим с bash 3.2 (macOS /bin/bash): без `wait -n`, ассоциативных массивов и mapfile.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="${TESTS_DIR:-$REPO/tests}"
SLOW_FIRST="superarmanda-state superarmanda-waves-e2e superarmanda-waves superarmanda-review"
# наборы не пишут __pycache__: параллельный набор иначе кладёт байткод в фикстуры, которые проверяет другой
export PYTHONDONTWRITEBYTECODE=1
shopt -s nullglob
# порядок наборов — байтовый на любой машине: glob сортирует по LC_COLLATE, а LC_ALL его перекрывает; локаль
# самих наборов после glob возвращается как была
lc_all_set="${LC_ALL+set}" lc_all="${LC_ALL-}"
LC_ALL=C
suites=("$TESTS_DIR"/*.test.sh)
if [ "$lc_all_set" = set ]; then LC_ALL="$lc_all"; else unset LC_ALL; fi

if [ ${#suites[@]} -eq 0 ]; then
  printf 'no test suites found in %s\n' "$TESTS_DIR" >&2
  exit 2
fi

cpus() {
  local n
  n="$(nproc 2>/dev/null)" || n=""
  [ -n "$n" ] || n="$(sysctl -n hw.ncpu 2>/dev/null)" || n=""
  [ -n "$n" ] || n="$(getconf _NPROCESSORS_ONLN 2>/dev/null)" || n=""
  case "$n" in ''|*[!0-9]*|0) n=1 ;; esac
  printf '%s\n' "$n"
}

if [ "${RUN_TESTS_JOBS+set}" = set ]; then
  jobs="$RUN_TESTS_JOBS"
  case "$jobs" in
    ''|*[!0-9]*|0*) printf 'RUN_TESTS_JOBS must be a positive integer, got %s\n' "'$jobs'" >&2; exit 2 ;;
  esac
  # больше 6 цифр — отказ: за пределом int64 `[ -lt ]` пишет «integer expression expected», и цикл не кончается
  if [ ${#jobs} -gt 6 ]; then
    printf 'RUN_TESTS_JOBS must be at most 999999, got %s\n' "'$jobs'" >&2
    exit 2
  fi
else
  jobs="$(cpus)"
fi

names=()
for suite in "${suites[@]}"; do
  names+=("$(basename "$suite" .test.sh)")
done
count=${#suites[@]}

if [ "$jobs" -eq 1 ]; then
  failed=()
  for i in "${!suites[@]}"; do
    name="${names[$i]}"
    printf '▶ %s\n' "$name"
    if /bin/bash "${suites[$i]}"; then
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
  exit 0
fi

printf 'run-tests: %s parallel\n' "$jobs" >&2

work="$(mktemp -d "${TMPDIR:-/tmp}/run-tests.XXXXXX")" || exit 2
pids=()     # индекс набора -> PID (пусто, пока не запущен)
rcs=()      # индекс набора -> код возврата (пусто, пока не собран wait)
cleanup() {
  local p
  for p in ${pids[@]+"${pids[@]}"}; do
    [ -n "$p" ] && kill "$p" 2>/dev/null
  done
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# порядок запуска: сначала долгие (в порядке SLOW_FIRST), затем остальные в исходном порядке
order=()
for slow in $SLOW_FIRST; do
  for i in "${!names[@]}"; do
    [ "${names[$i]}" = "$slow" ] && order+=("$i")
  done
done
for i in "${!names[@]}"; do
  case " $SLOW_FIRST " in *" ${names[$i]} "*) ;; *) order+=("$i") ;; esac
done

start() {  # $1 — индекс набора; набор пишет свой вывод в файл, по окончании — файл-отметку
  local i="$1"
  ( /bin/bash "${suites[$i]}" >"$work/$i.out" 2>&1; rc=$?; : >"$work/$i.done"; exit "$rc" ) &
  pids[$i]=$!
}

failed=()
printed=0   # сколько наборов уже напечатано (по исходному порядку)
next=0      # позиция в order следующего к запуску
running=0
while [ "$printed" -lt "$count" ]; do
  while [ "$running" -lt "$jobs" ] && [ "$next" -lt "$count" ]; do
    start "${order[$next]}"
    next=$((next + 1))
    running=$((running + 1))
  done
  # собрать закончившиеся: есть отметка .done (или процесса уже нет — убит сигналом) — wait по PID отдаёт код
  # возврата набора
  reaped=0
  for i in "${!names[@]}"; do
    if [ -n "${pids[$i]:-}" ] && [ -z "${rcs[$i]:-}" ] \
        && { [ -e "$work/$i.done" ] || ! kill -0 "${pids[$i]}" 2>/dev/null; }; then
      wait "${pids[$i]}"
      rcs[$i]=$?
      pids[$i]=""
      running=$((running - 1))
      reaped=1
    fi
  done
  # напечатать готовый префикс в исходном порядке
  while [ "$printed" -lt "$count" ] && [ -n "${rcs[$printed]:-}" ]; do
    name="${names[$printed]}"
    printf '▶ %s\n' "$name"
    cat "$work/$printed.out"
    if [ "${rcs[$printed]}" -eq 0 ]; then
      printf '  ✓ %s\n' "$name"
    else
      failed+=("$name")
      printf '  ✗ %s\n' "$name" >&2
    fi
    printed=$((printed + 1))
  done
  [ "$reaped" -eq 1 ] || sleep 0.2
done

if [ ${#failed[@]} -gt 0 ]; then
  printf 'failed suites: %s\n' "${failed[*]}" >&2
  exit 1
fi
exit 0
