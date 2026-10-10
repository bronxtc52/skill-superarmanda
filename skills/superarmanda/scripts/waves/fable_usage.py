#!/usr/bin/env python3
"""Local Fable usage of this machine (1.4.0, #108): weighted tokens of the Claude Code journals.

Reads `~/.claude/projects/**/*.jsonl` (the root is `--root`), takes the assistant lines whose `message.model` is
Fable and sums their `message.usage` with the weights input 1, cache_creation 1.25, cache_read 0.1, output 5.
One unit is 1 000 000 weighted tokens. Two windows by the line's `timestamp`: the last 24 hours and the rolling
last 7 days (both end at `--now`). One answer of the assistant is written as several journal lines (one per
content block): it counts once, by `message.id`, with the LARGEST usage of its lines — a
subagent journal writes the partial `output_tokens` of the stream on the early blocks and the final number on the
last one (live sample `tests/fixtures/transcripts/fable-usage-journal.jsonl`); the same answer in two journals (a
resumed session copies the history) counts once too. A Fable line without `message.id` is a form never seen live
(every one of 13 138 sampled lines has it): it is not counted, only reported as `unkeyed`. Sidechain lines
(`isSidechain: true`, the journals of subagents under `<session>/subagents/`) count: a Fable subagent spends the
same subscription limit.

The count is ADVISORY: it sees only the journals of this machine (not other machines, not claude.ai), and the
weights are an estimate, not the provider's accounting. Hard protection is elsewhere (the quota route of the
review to Opus, the switch of a wave window to Opus on the Fable limit).

Output: one JSON object {day_units, week_units, budget, threshold, pct (percent of the budget used in 7 days),
over_threshold, valid, files}. rc 0; no readable journal under the root (missing, empty or unreadable root):
rc 3 with `valid: false` and the `reason` — never a «0 %». Stdlib only, no network.
"""

import argparse
import datetime
import json
import math
import os
import pathlib
import re
import sys

FABLE_MODEL = "claude-fable-5-1"
WEIGHTS = {"input_tokens": 1.0, "cache_creation_input_tokens": 1.25, "cache_read_input_tokens": 0.1,
           "output_tokens": 5.0}
UNIT = 1_000_000
DEFAULT_BUDGET = 80
DEFAULT_THRESHOLD = 0.8
DAY = datetime.timedelta(days=1)
WEEK = datetime.timedelta(days=7)
RC_NOT_COUNTED = 3


def default_root():
    return pathlib.Path.home() / ".claude" / "projects"


def _parse_time(value):
    """An ISO-8601 timestamp (`Z` or an offset; naive = UTC) -> aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = re.sub(r"[Zz]$", "+00:00", value.strip())
    try:
        t = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def _now(value=None):
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    t = _parse_time(value)
    if t is None:
        try:
            return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
        except (TypeError, ValueError):
            raise ValueError(f"not a time: {value!r}") from None
    return t


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else 0


def weighted(usage):
    """Weighted tokens of one `message.usage`."""
    if not isinstance(usage, dict):
        return 0.0
    return sum(_num(usage.get(k)) * w for k, w in WEIGHTS.items())


def _journals(root):
    for dirpath, _dirs, names in os.walk(root, onerror=None, followlinks=False):
        for name in names:
            if name.endswith(".jsonl"):
                yield pathlib.Path(dirpath) / name


def usage(root=None, now=None, budget=DEFAULT_BUDGET, threshold=DEFAULT_THRESHOLD):
    """The count as a dict (see the module doc). Never raises on a journal: an unreadable file or line is
    skipped; `valid` is False when not a single journal under `root` could be read."""
    root = pathlib.Path(root) if root is not None else default_root()
    now = _now(now)
    day_from, week_from = now - DAY, now - WEEK
    out = {"day_units": None, "week_units": None, "budget": budget, "threshold": threshold, "pct": None,
           "over_threshold": None, "valid": False, "files": 0, "root": str(root)}
    if not root.is_dir():
        out["reason"] = f"no journal directory {root}"
        return out
    best = {}  # key of one answer -> (weighted value, time) of its largest line
    unkeyed = 0  # Fable lines without message.id: not counted (no live sample of that form), only reported
    found = readable = 0
    for path in _journals(root):
        found += 1
        try:
            if datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc) < week_from:
                readable += 1  # not written to within the window: nothing in it can count
                continue
            fh = open(path, "rb")
        except OSError:
            continue
        readable += 1
        with fh:
            for raw in fh:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or msg.get("model") != FABLE_MODEL:
                    continue
                t = _parse_time(d.get("timestamp"))
                if t is None or not week_from < t <= now:
                    continue
                key = msg.get("id") if isinstance(msg.get("id"), str) and msg.get("id") else None
                value = weighted(msg.get("usage"))
                if key is None:
                    unkeyed += 1
                elif key not in best or value > best[key][0]:
                    best[key] = (value, t)
    day = week = 0.0
    for value, t in best.values():
        week += value
        if t > day_from:
            day += value
    out["files"], out["unkeyed"] = readable, unkeyed
    if not readable:
        out["reason"] = (f"no journal (*.jsonl) under {root}" if not found
                         else f"no journal under {root} could be read")
        return out
    day_units, week_units = round(day / UNIT, 4), round(week / UNIT, 4)
    pct = round(week_units / budget * 100, 1)
    out.update(day_units=day_units, week_units=week_units, pct=pct,
               over_threshold=week_units >= budget * threshold, valid=True)
    return out


def _positive(text):
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not (math.isfinite(v) and v > 0):
        raise argparse.ArgumentTypeError(f"must be > 0: {text!r}")
    return int(v) if v.is_integer() else v


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weighted Fable tokens of the local Claude Code journals "
                                             "(advisory: this machine only).")
    ap.add_argument("--root", default=None, help="journal root (default ~/.claude/projects)")
    ap.add_argument("--now", default=None, help="end of the windows: ISO-8601 or epoch seconds (tests)")
    ap.add_argument("--budget", type=_positive, default=DEFAULT_BUDGET,
                    help=f"units per 7 days (default {DEFAULT_BUDGET})")
    ap.add_argument("--threshold", type=_positive, default=DEFAULT_THRESHOLD,
                    help=f"share of the budget that warns (default {DEFAULT_THRESHOLD})")
    args = ap.parse_args(argv)
    try:
        result = usage(args.root, now=args.now, budget=args.budget, threshold=args.threshold)
    except ValueError as e:
        ap.error(e.args[0] if e.args else "bad --now")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] else RC_NOT_COUNTED


if __name__ == "__main__":
    sys.exit(main())
