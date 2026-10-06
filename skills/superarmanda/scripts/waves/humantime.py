#!/usr/bin/env python3
"""The ONE function that shows a time to a human (#88). Machine logs (events.log, state.json, manifest,
policy-decisions.log) stay UTC ISO-8601; everything a person reads (dashboard, notices, ATTENTION, chain-result.md,
`wab.py status`, idle texts) goes through human() and shows the owner's zone with a signature:
`14:20 Дубай` for Asia/Dubai, `14:20 (+05)` for any other zone, a date in front when the day differs from today.

The zone: chain.json `timezone` (dispatcher only) -> $SUPERARMANDA_TZ -> Asia/Dubai. Entry points call resolve()
and refuse with a reason (unknown zone, no tzdata on the node); inside a loop human() never raises: a failure
gives the UTC time with an explicit note instead of silencing a notice.

  humantime.py [ISO-8601 | epoch ...]   print the time(s) as a human sees it (now by default); rc 2 on a refusal
Only the standard library."""
import datetime
import os
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Dubai"
ENV_TZ = "SUPERARMANDA_TZ"
NAMES = {"Asia/Dubai": "Дубай"}  # zones with a word signature; the others show the numeric offset
FALLBACK_NOTE = "пояс недоступен"


class TzError(ValueError):
    """The owner's zone cannot be used; `reason` is the text shown to the owner."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def resolve(name=None, env=None):
    """(name, ZoneInfo) of the owner's zone: `name` (chain.json) -> $SUPERARMANDA_TZ -> Asia/Dubai.
    Raises TzError with a reason: wrong type, unknown zone, or no tzdata on this node (never a silent UTC)."""
    source = "chain.json timezone"
    if name is None:
        value = (os.environ if env is None else env).get(ENV_TZ)
        name, source = (value, ENV_TZ) if value not in (None, "") else (DEFAULT_TZ, "default")
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise TzError(f"{source} must be an IANA zone name such as {DEFAULT_TZ}, got {name!r}")
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    try:
        ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise TzError(f"{source}: no time zone database (tzdata) on this node: install the system tzdata or "
                      f"`python3 -m pip install tzdata`; the time is not silently shown in UTC") from None
    raise TzError(f"{source}: unknown time zone {name!r} (an IANA name such as {DEFAULT_TZ} or Asia/Tashkent)")


def signature(name, moment):
    """`Дубай` for a zone with a word, otherwise `(+05)` / `(+05:30)` / `(-03)`."""
    if name in NAMES:
        return NAMES[name]
    off = moment.utcoffset() or datetime.timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign, minutes = ("-" if minutes < 0 else "+"), abs(minutes)
    hh, mm = divmod(minutes, 60)
    return f"({sign}{hh:02d}" + (f":{mm:02d}" if mm else "") + ")"


def fmt(ts, name=None, ref=None, date=None, seconds=False, env=None):
    """`ts` (epoch seconds) as the owner's local time with the signature. The date (`06.10 `) is put in front
    when it differs from the day of `ref` (default: now), or always with date=True, never with date=False.
    Raises TzError / ValueError / OverflowError / OSError: callers in loops use human()."""
    name, tz = resolve(name, env)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ValueError(f"not a time: {ts!r}")
    moment = datetime.datetime.fromtimestamp(ts, tz)
    shown = moment.strftime("%H:%M:%S" if seconds else "%H:%M")
    if date is None:
        ref_day = datetime.datetime.fromtimestamp(time.time() if ref is None else ref, tz).date()
        date = moment.date() != ref_day
    prefix = moment.strftime("%d.%m ") if date else ""
    return f"{prefix}{shown} {signature(name, moment)}"


def human(ts, name=None, ref=None, date=None, seconds=False, env=None):
    """fmt() that never raises: `?` for something that is not a time, and for a broken zone the UTC time with
    an explicit note (a failure of formatting must not drop a notice or an ATTENTION)."""
    try:
        return fmt(ts, name, ref, date, seconds, env)
    except Exception:  # noqa: BLE001 - the loop of the dispatcher must go on whatever failed here
        try:
            moment = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            return moment.strftime("%d.%m %H:%M") + f" UTC ({FALLBACK_NOTE})"
        except Exception:  # noqa: BLE001
            return "?"


def _parse(arg):
    try:
        return float(arg)
    except ValueError:
        moment = datetime.datetime.fromisoformat(arg.replace("Z", "+00:00"))
        if moment.tzinfo is None:  # no offset = UTC, like the machine logs, not the zone of the host
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment.timestamp()


def main(argv):
    try:
        resolve()
    except TzError as e:
        print(f"humantime: {e.reason}", file=sys.stderr)
        return 2
    for arg in argv[1:] or [str(time.time())]:
        try:
            print(fmt(_parse(arg), date=True))
        except (ValueError, OverflowError, OSError):
            print(f"humantime: {arg!r} is not a time (ISO-8601 or epoch seconds)", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
