#!/usr/bin/env python3
"""wave-autobot dispatcher: run plan waves one per tmux Claude session.

Commands:
  wab.py launch <chain.json> <wave> <prompt-file>   start one wave in tmux
  wab.py watch  <chain.json>                        supervise until the chain ends
  wab.py status <chain.json>                        one-screen status
  wab.py done   <chain.json> [<wave>]               after the LAST wave's PR is merged: finish the chain
  wab.py cleanup <chain.json>                       close the tmux sessions/panes of a FINISHED chain (chain-result.md)
  wab.py owner-merge <chain.json> <wave> <run_id> <sha>  gated merge by the owner (run by the generated script)
  wab.py owner-handover <chain.json> <wave> <run_id>  the owner merged the wave's PR himself: hand the chain on
  wab.py notify <chain.json> <text>                 the owner's own message (typed by him) to Telegram
  wab.py attention <chain.json>                     print $RUN_DIR/ATTENTION; exit 1 while a signal is open
  wab.py say <chain.json> <wave> <text-file>        type a reply into the wave's window and check it was sent
  wab.py current-tmux <chain.json>                  tmux session name of the current wave

Per wave the session writes $WAB_DIR/status (RUNNING | HANDOFF_READY | BLOCKED: ... | DONE),
handoff.md, result.md, next-prompt.md - see PROTOCOL.md next to this file.
Needs tmux >= 3.2 (checked before launch/watch): new-session takes the command as an argv
list and -e (3.2+). Standard library only; the dashboard (dash.py) is the only user of `rich`.

Every contact with tmux, claude, Telegram and az goes through the module-level functions
`sh`, `tmux_alive`, `pane_text`, `send_text`, `send_command`, `wait_ready` and
`_send_telegram`, so tests can replace them. Paths that depend on $HOME are computed at call
time, not at import.
"""
import bisect
import datetime
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import shlex
import string
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # gate.py lies next to this file, also when it is loaded by path
    sys.path.insert(0, str(HERE))
import gate  # noqa: E402  (stdlib only, like this module)
import humantime  # noqa: E402  (the one function that shows a time to a human, #88)
import fable_usage  # noqa: E402  (stdlib only: the local Fable count of `launch`, #108)

PROTOCOL = HERE / "PROTOCOL.md"
MIN_TMUX = (3, 2)
PERMISSION_MARKERS = ("Do you want to proceed", "Do you want to make this edit",
                      "Do you want to create", "❯ 1. Yes")
READY_MARKERS = ("? for shortcuts", "shift+tab to cycle", "for agents")
TRUST_MARKERS = ("Yes, I trust this folder", "Do you trust the files")
NAME = re.compile(r"[A-Za-z0-9._-]+")
WAVE_NAME = re.compile(r"[A-Za-z0-9_-]+")
PREFIX = re.compile(r"[A-Za-z0-9_-]+")
TAIL_BYTES = 4_000_000       # context is measured from the last 4 MB of a transcript
FIRST_MESSAGE_BYTES = 256 * 1024
HEAD_BYTES = 4096            # transcript identity check: hash of the first 4 KB


def projects_dir():
    return pathlib.Path.home() / ".claude" / "projects"


# ---------- config and state ----------

# Upper bounds: a day of minutes, an hour between ticks, ten million tokens are already absurd.
# merge_gate: absent/"" - a DONE of a non-last wave is handed to the coordinator (like `external`);
# "external" - the same plus the gate verdict in the hand-off; "auto" - the dispatcher runs the gate,
# merges and launches the next wave itself (see «merge gate» below).
MERGE_GATES = (None, "", "external", "auto")
GATE_POLL_SECONDS = 60    # the merge gate / the wait for MERGED is asked no more often than this
ALARM_POLL_SECONDS = 120  # the alarm of a running wave: GitHub is asked no more often than this

NUM_LIMITS = {"ctx_limit": 10_000_000, "idle_minutes": 1440, "handoff_timeout_minutes": 1440,
              "tick_seconds": 3600}

# The defaults of `model` and `ctx_limit` (1.4.0, #108): a chain first launched by 1.4.0+ carries the marker
# `defaults: "1.4"` in its state.json and runs on Opus with 200 000 tokens; a chain started before (state.json
# with waves and without the marker) keeps the defaults it was started with, None (the CLI default) and
# 300 000. Both fields are tunable: the identity of a running chain does not change with them.
DEFAULTS_MARK = "1.4"
NEW_DEFAULTS = {"model": gate.state.OPUS_MODEL, "ctx_limit": 200_000}
LEGACY_DEFAULTS = {"model": None, "ctx_limit": 300_000}
PLAN_REVIEW_FABLE_ROUNDS = 2  # chain.json plan_review_fable_rounds: the Fable rounds of phase A before Astra decides
FABLE_BUDGET_UNITS = fable_usage.DEFAULT_BUDGET  # chain.json fable_budget_units: Fable units per 7 days (advisory)


MANDATE_PIN = re.compile(r"[0-9a-f]{64}")


def _plain_name(value):
    return bool(NAME.fullmatch(value)) and set(value) != {"."}


# chain.json field -> what it must be. Checked before any field is used, so a wrong type is a
# SystemExit with the reason (the live re-read in `watch` keeps the previous settings on it),
# never a TypeError/ValueError later. Optional string fields: absent or null = not set.
OPTIONAL_STRINGS = ("run_dir", "workdir", "repo", "model", "base_branch", "merge_gate")


def _check_types(cfg):
    def bad(key, want):
        raise SystemExit(f"chain.json: {key} must be {want}, got {cfg[key]!r}")
    for key in ("chain", "run_id"):
        if key in cfg and not isinstance(cfg[key], str):
            bad(key, "a string")
    for key in OPTIONAL_STRINGS + ("tmux_prefix",):
        v = cfg.get(key)
        if v is not None and not isinstance(v, str):
            bad(key, "a string" + ("" if key == "tmux_prefix" else " or null"))
        if isinstance(v, str) and "\x00" in v:
            bad(key, "a string without NUL characters")
    tg = cfg.get("telegram")
    if not (tg is None or tg is False or isinstance(tg, dict)):  # "", 0 and [] are typos, not «off»
        bad("telegram", "an object, false or null")
    if "telegram_quote" in cfg and not isinstance(cfg["telegram_quote"], bool):  # "true", 1 and null are typos (#83)
        bad("telegram_quote", "true or false (the first line of a wave's question in a notice, off by default)")
    titles = cfg.get("titles")
    if titles is not None and not (isinstance(titles, dict) and all(isinstance(v, str) for v in titles.values())):
        bad("titles", "an object of wave id -> string")
    if "mandate_sha256" in cfg and not isinstance(cfg["mandate_sha256"], str):
        bad("mandate_sha256", "a string of 64 lowercase hex characters")
    if "plan_sha256" in cfg and not isinstance(cfg["plan_sha256"], str):
        bad("plan_sha256", "a string of 64 lowercase hex characters")
    if "role_models" in cfg and not isinstance(cfg["role_models"], dict):
        bad("role_models", "an object of \"<role>[.<risk>]\" -> model (the role policy of the waves)")
    if "plan_review_fable_rounds" in cfg:
        v = cfg["plan_review_fable_rounds"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100:
            bad("plan_review_fable_rounds", "a whole number 1..100 (the Fable rounds of the plan review, "
                                            f"default {PLAN_REVIEW_FABLE_ROUNDS})")
    if "fable_budget_units" in cfg:  # absent = the default; never written into cfg (#108, T5)
        v = cfg["fable_budget_units"]
        if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                or not 0 < v <= 1_000_000):
            bad("fable_budget_units", "a number > 0 (Fable units of 1M weighted tokens per 7 days, "
                                      f"default {FABLE_BUDGET_UNITS})")


def check_chain_role_models(table):
    """chain.json `role_models`: the same keys and models `state.py init` takes from env
    SUPERARMANDA_ROLE_MODELS (its parser, not a copy), refused here at the entry and not in the wave's
    session: an unknown role, risk or model, a `coordinator` key, or a model below the built-in default."""
    state = gate.state
    try:
        for key, value in table.items():
            role, risks = state.parse_role_model_key(key, "role_models")
            model = state.parse_role_model_value(value, key, "role_models")
            for risk in risks:
                floor = state.DEFAULT_ROLE_MODELS[role][risk]
                if state.weaker(model, floor):
                    raise SystemExit(f"{key}={value} is below the built-in {role}.{risk} ({floor}): "
                                     f"an override may only raise the model")
    except SystemExit as e:
        raise SystemExit(f"chain.json: role_models: {_exc_text(e).removeprefix('state: ')}")


def chain_started_before_defaults(run_dir):
    """True for a run whose state.json has waves and no `defaults` marker: a chain started before 1.4.0.
    No state, no wave yet or an unreadable state: the chain is new (a broken state.json is refused later
    by load_state anyway)."""
    try:
        st = json.loads((pathlib.Path(run_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(st, dict) and "defaults" not in st and isinstance(st.get("waves"), dict) and bool(st["waves"])


def load_chain(path, create=True):
    """Read chain.json. The run directory is <base>/<chain>/<run_id>, where base is
    `run_dir` from chain.json or <directory of chain.json>/runs. run_id is mandatory and is
    always the last path segment: a reused chain name must never pick up an older run's
    mandate.md."""
    path = pathlib.Path(path)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"chain.json: cannot read {path}: {_exc_text(e)}")
    if not isinstance(cfg, dict):
        raise SystemExit("chain.json: must be a JSON object")
    for key in OPTIONAL_STRINGS + ("tmux_prefix",):
        if key in cfg and cfg[key] is None:
            del cfg[key]  # an explicit null is the same as leaving the field out (the default)
    if cfg.get("model") == "":
        del cfg["model"]  # "" is no model either: the default of the chain (Opus for a new one), not the CLI's (#108)
    for key in ("titles", "role_models"):
        if key in cfg and cfg[key] is None:
            del cfg[key]  # no titles / no role policy of the chain, like leaving it out (dash reads cfg.get("titles", {}))
    _check_types(cfg)
    if cfg.get("role_models") == {}:
        del cfg["role_models"]  # an empty policy is no policy: not a part of the identity, no env for the wave
    if "role_models" in cfg:
        check_chain_role_models(cfg["role_models"])
    if cfg.get("telegram_quote") is False:
        del cfg["telegram_quote"]  # «false» is the default: it is no field, and so not a part of the run's identity (#83)
    if "timezone" in cfg and cfg["timezone"] is None:
        del cfg["timezone"]  # null = the default, like the other optional fields
    try:  # the owner's zone: chain.json -> $SUPERARMANDA_TZ -> Asia/Dubai; a refusal at the entry, never a silent UTC
        cfg["timezone"] = humantime.resolve(cfg.get("timezone"))[0]
    except humantime.TzError as e:
        raise SystemExit(f"chain.json: {e.reason}")
    cfg.setdefault("idle_minutes", 12)
    cfg.setdefault("handoff_timeout_minutes", 25)
    cfg.setdefault("tick_seconds", 60)
    cfg.setdefault("tmux_prefix", "wab-")
    for key in NUM_LIMITS:
        if key == "ctx_limit" and key not in cfg:
            continue  # its default depends on the run (DEFAULTS_MARK), set below once run_dir is known
        v = cfg[key]  # an explicit null/string/bool/<=0/nan/inf would crash `watch` later
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or (isinstance(v, float) and not math.isfinite(v))  \
                or v < 0 or (v == 0 and key != "idle_minutes"):  # idle 0 = flag idleness at once
            raise SystemExit(f"chain.json: {key} must be a positive number, got {v!r}")
        if v > NUM_LIMITS[key]:  # time.sleep(1e10) raises OverflowError and kills `watch`
            raise SystemExit(f"chain.json: {key} must be at most {NUM_LIMITS[key]}, got {v!r}")
    if "decision_policy" in cfg:  # not defaulted: a run without it keeps the identity it was started with
        why = check_decision_policy(cfg["decision_policy"])
        if why:
            raise SystemExit(f"chain.json: decision_policy {why}")
    cfg.setdefault("max_auto_answers", MAX_AUTO_ANSWERS)
    v = cfg["max_auto_answers"]
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10_000:
        raise SystemExit(f"chain.json: max_auto_answers must be a whole number 0..10000 "
                         f"(0 = no automatic answers), got {v!r}")
    cfg.setdefault("max_runs", MAX_RUNS)
    v = cfg["max_runs"]
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 1000:
        raise SystemExit(f"chain.json: max_runs must be a whole number 1..1000 "
                         f"(superarmanda runs per wave), got {v!r}")
    cfg.setdefault("idle_nudge_minutes", IDLE_NUDGE_DEFAULT)
    v = cfg["idle_nudge_minutes"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or (isinstance(v, float) and not math.isfinite(v)) or not 0 <= v <= 1440:
        raise SystemExit(f"chain.json: idle_nudge_minutes must be a number 0..1440 (minutes of silence "
                         f"before the nudge, 0 = no nudge), got {v!r}")
    tg = cfg.get("telegram")
    if tg and not isinstance(tg, dict):
        raise SystemExit("chain.json: telegram must be an object")
    if tg and not all(isinstance(tg.get(k), str) and tg[k] for k in ("keyvault", "token_secret", "chat_secret")):
        # an enabled block with a hole would silently turn every notice into a local event
        raise SystemExit("chain.json: telegram needs non-empty string keyvault, token_secret and chat_secret "
                         "(or leave it out / empty to disable)")
    if "mandate_sha256" in cfg and not (isinstance(cfg["mandate_sha256"], str)
                                        and MANDATE_PIN.fullmatch(cfg["mandate_sha256"])):
        # a present pin is never "no pin": "", null, false or 0 must not let a wave start unpinned
        raise SystemExit(f"chain.json: mandate_sha256 must be 64 lowercase hex characters (the sha256 "
                         f"of mandate.md), got {cfg['mandate_sha256']!r}; leave it out for no pin")
    if "plan_sha256" in cfg and not (isinstance(cfg["plan_sha256"], str)
                                     and MANDATE_PIN.fullmatch(cfg["plan_sha256"])):
        raise SystemExit(f"chain.json: plan_sha256 must be 64 lowercase hex characters (the sha256 "
                         f"of the approved waves.json), got {cfg['plan_sha256']!r}; leave it out for no pin")
    chain = cfg.get("chain") or ""
    run_id = cfg.get("run_id") or ""
    if not _plain_name(chain):
        raise SystemExit("chain.json: chain is required ([A-Za-z0-9._-]+, not a dot segment)")
    if not _plain_name(run_id):
        raise SystemExit("chain.json: run_id is required ([A-Za-z0-9._-]+, not a dot segment), "
                         "one per approved run")
    if not PREFIX.fullmatch(str(cfg["tmux_prefix"])):
        raise SystemExit("chain.json: tmux_prefix must match [A-Za-z0-9_-]+")
    if "base_branch" in cfg and not (isinstance(cfg["base_branch"], str) and cfg["base_branch"].strip()
                                     and not cfg["base_branch"].startswith("-")):
        raise SystemExit(f"chain.json: base_branch must be a non-empty branch name, got {cfg['base_branch']!r}")
    if cfg.get("merge_gate") not in MERGE_GATES:
        raise SystemExit(f"chain.json: merge_gate must be absent, \"external\" or \"auto\", "
                         f"got {cfg['merge_gate']!r}")
    if cfg.get("merge_gate") == "auto" and not cfg.get("repo"):
        raise SystemExit("chain.json: merge_gate \"auto\" needs repo (owner/name) to find the PR and merge it")
    waves = cfg.get("waves")
    if (not isinstance(waves, list) or not waves
            or not all(isinstance(w, str) and WAVE_NAME.fullmatch(w) for w in waves)):
        raise SystemExit("chain.json: waves must be a non-empty list of [A-Za-z0-9_-]+ names")
    if len({w.lower() for w in waves}) != len(waves):  # tmux session names are lower-cased
        raise SystemExit("chain.json: duplicate wave id in waves (compared case-insensitively)")
    try:
        here = path.resolve().parent
        # a relative run_dir is relative to chain.json, never to the cwd of whoever runs us
        base = pathlib.Path(os.path.abspath(here / cfg["run_dir"])) if cfg.get("run_dir") else here / "runs"
        if cfg.get("workdir"):  # absolute before admission, state and transcript lookup
            cfg["workdir"] = str(pathlib.Path(os.path.abspath(here / cfg["workdir"])).resolve())
        cfg["chain_file"] = str(path.resolve())
        cfg["run_dir"] = base / chain / run_id
        # the pins (plan_sha256, mandate_sha256) are only worth anything if the waves, which write
        # into run_dir, cannot rewrite chain.json together with the files it pins: compare the
        # physical paths (symlinks of chain.json and of run_dir components), run_dir may not exist yet
        real_run = cfg["run_dir"].resolve(strict=False)
        for where in (path.resolve(strict=False), pathlib.Path(os.path.abspath(path))):
            if where == real_run or real_run in where.parents:
                raise SystemExit(f"chain.json: chain.json must live outside run_dir ({real_run}): a wave "
                                 f"writes there and could replace the pinned files together with the pin")
        if create:
            cfg["run_dir"].mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as e:  # symlink loop, no permission, a file in the way
        raise SystemExit(f"chain.json: run_dir/workdir cannot be used: {_exc_text(e)}")
    defaults = LEGACY_DEFAULTS if chain_started_before_defaults(cfg["run_dir"]) else NEW_DEFAULTS
    for key, value in defaults.items():
        cfg.setdefault(key, value)
    return cfg


_HELD = set()  # lock files held by THIS process (a flock is not re-entrant across descriptors)


class _RunLock:
    """One dispatcher per run: flock on <run_dir>/dispatcher.lock. Taken by `watch` for its
    whole life and by the CLI `launch`; a launch inside a running watch reuses the held lock."""

    def __init__(self, cfg, what, busy="запусти волну через watch или останови диспетчер"):
        self.path = cfg["run_dir"] / "dispatcher.lock"
        self.what = what
        self.busy = busy
        self.fh = None

    def __enter__(self):
        if str(self.path) in _HELD:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a", encoding="utf-8")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise SystemExit(f"wab: another dispatcher holds {self.path} ({self.what}); {self.busy}")
        self.fh = fh
        _HELD.add(str(self.path))
        return self

    def __exit__(self, *exc):
        if self.fh:
            _HELD.discard(str(self.path))
            self.fh.close()  # closing drops the flock
            self.fh = None
        return False


def state_path(cfg):
    return cfg["run_dir"] / "state.json"


def load_state(cfg):
    p = state_path(cfg)
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(st, dict):
            raise ValueError("not a JSON object")
        waves = st.get("waves", {})
        if not isinstance(waves, dict) or not all(isinstance(w, dict) for w in waves.values()):
            raise ValueError("`waves` must map wave names to objects")
        if st.get("current") is not None and not isinstance(st["current"], str):
            raise ValueError("`current` must be a wave name or null")
    except (OSError, ValueError) as e:
        raise SystemExit(f"wab: cannot read state.json: {_exc_text(e)}")
    st.setdefault("waves", {})
    return st


ENDED_PHASES = ("done", "awaiting_merge", "dead")


def count_wave_commits(cwd, start_rev):
    """Commits of a wave: `start_rev..HEAD` in its workdir (only what its HEAD gained since the
    launch, never other branches or fetched refs). None when git cannot tell."""
    if not cwd or not isinstance(start_rev, str) or not re.fullmatch(r"[0-9a-f]{7,64}", start_rev):
        return None
    try:
        r = sh("git", "-C", str(cwd), "rev-list", "--count", f"{start_rev}..HEAD", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def head_rev(cwd):
    try:
        r = sh("git", "-C", str(cwd), "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    rev = r.stdout.strip()
    return rev if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", rev) else None


def save_state(cfg, st):
    waves = st.get("waves")
    if isinstance(waves, dict):  # an episode that ended with this save loses its stale notice here
        for rec in waves.values():
            drop_ended_episodes(rec)
            if isinstance(rec, dict):  # `finished`: the end of the wave's work (dash counts commits up to it)
                if rec.get("phase") in ENDED_PHASES:
                    rec.setdefault("finished", time.time())
                    if "commits" not in rec and isinstance(rec.get("start_rev"), str):
                        # fixed now, known or not: the shared HEAD moves on with the next wave, and a
                        # recount after that would give the finished wave someone else's history;
                        # null is the frozen «unknown» (git failed or timed out), shown «?»
                        rec["commits"] = count_wave_commits(rec.get("cwd"), rec["start_rev"])
                else:
                    rec.pop("finished", None)  # a dead wave is back: it has not ended
                    rec.pop("commits", None)
    target = state_path(cfg)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")  # unique per writer
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(st, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    sync_attention(cfg, st)


def hum(cfg, ts, **kw):
    """THE way a time is shown to a human: the owner's zone of this chain with a signature (humantime.human, which
    never raises: a broken zone gives the UTC time with a note, so a notice is not lost). Machine logs do not use it."""
    return humantime.human(ts, (cfg or {}).get("timezone"), **kw)


def notice_stamp(cfg, ts):
    """The line with the time that a notice (Telegram, display-message) carries."""
    return f"Время: {hum(cfg, ts)}"


def event(cfg, text):
    # the WHOLE text of the event passes safe_text BEFORE it is cut into lines: events.log is read by a
    # human (the watch window tails it, the dashboard shows it), and a line break (a lone CR, U+2028, NEL)
    # inside a secret would otherwise cut it into two pieces that redact() does not recognise (#81)
    text = " ⏎ ".join(l for l in safe_text(text, 10 ** 9).splitlines() if l.strip())
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {text}"
    cfg["run_dir"].mkdir(parents=True, exist_ok=True)
    with open(cfg["run_dir"] / "events.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def wave_dir(cfg, wave):
    d = cfg["run_dir"] / wave
    d.mkdir(parents=True, exist_ok=True)
    return d


def read(p, on_error=""):
    """The stripped text of a small file; "" when it does not exist, `on_error` when it exists but
    cannot be read (a directory, no permission, removed between exists() and read_text())."""
    p = pathlib.Path(p)
    try:
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        if "\n" in text:  # the universal-newline reading may have turned a lone CR into a line break
            raw = p.read_bytes()
            if b"\r" in raw:  # a lone CR stays a CR: `password=sec\rret` is one word for safe_text (#81)
                text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return text.strip()
    except OSError:
        return on_error


# ---------- tmux ----------

def parse_tmux_version(text):
    """`tmux 3.4` -> (3, 4); `3.2a` -> (3, 2); `next-3.5` -> (3, 5); `master` -> newest."""
    text = (text or "").strip()
    if re.search(r"\bmaster\b", text):
        return (99, 0)
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def require_tmux():
    try:
        r = tmux("-V", check=False)
    except OSError as e:
        raise SystemExit(f"tmux is required (>= 3.2) but could not be run: {_exc_text(e)}")
    version = parse_tmux_version(r.stdout) if r.returncode == 0 else None
    if version is None:
        raise SystemExit(f"cannot determine the tmux version (tmux -V gave rc={r.returncode} "
                         f"{r.stdout.strip()!r}); tmux >= 3.2 is required")
    if version < MIN_TMUX:
        raise SystemExit(f"tmux {version[0]}.{version[1]} is too old: tmux >= 3.2 is required "
                         f"(new-session -e, display-popup, source-file -n)")


TMUX_SOCKET = None  # tests point this at a private `-L` socket; None means the default server


def sock_flag(sock):
    """A value with `/` is the path of a socket file (`tmux -S`), otherwise a `-L` name."""
    return (("-S" if "/" in sock else "-L"), sock) if sock else ()


def tmux_argv(*args):
    return ("tmux", *sock_flag(TMUX_SOCKET or os.environ.get("WAB_TMUX_SOCKET")), *args)


def attach_cmd(name):
    """The attach command to show a human: same socket choice as tmux_argv()."""
    return " ".join(shlex.quote(a) for a in tmux_argv()) + f" attach -t {shlex.quote(name)}"


def tmux(*args, **kw):
    return sh(*tmux_argv(*args), **kw)


def session_target(name):
    """Exact session match: a bare `wv-w1` would also hit `wv-w10`."""
    return f"={name}"


def pane_target(name):
    """Exact session, its current window and pane (for send-keys, capture-pane, paste-buffer)."""
    if re.fullmatch(r"%\d+", str(name)):  # a pane id is itself exact (the target of a shared session's own pane)
        return name
    return f"={name}:"


def sh(*args, check=True, **kw):
    return subprocess.run(args, check=check, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def tmux_alive(name):
    return tmux("has-session", "-t", session_target(name), check=False).returncode == 0


def pane_text(name):
    r = tmux("capture-pane", "-p", "-t", pane_target(name), check=False)
    return r.stdout if r.returncode == 0 else ""


def press_enter(name):
    tmux("send-keys", "-t", pane_target(name), "Enter")


PASTE_PREVIEW = "paste again to expand"  # Claude Code folded a long paste: Enter only expanded it
INPUT_MARK = "\u276f"  # «❯ », the start of Claude Code's input line
SUBMIT_RETRIES = 3     # extra Enters after the first one before the delivery counts as failed
SUBMIT_PAUSE = 1.5     # seconds between an Enter and the look at the screen


class NotSubmitted(Exception):
    """The text is in the window, but the input line still holds it after every Enter."""


def _text_head(text, n=20):
    for line in (text or "").splitlines():
        s = " ".join(line.split())
        if s:
            return s[:n]
    return ""


_RULE_CHARS = set("─━═╭╮╰╯┌┐└┘├┤┬┴┼│ ")


def _is_rule(line):
    """A horizontal border line of the input box (box-drawing characters only, with a «─» run)."""
    s = line.strip()
    return bool(s) and "─" in s and set(s) <= _RULE_CHARS


def unsent_reason(screen, text):
    """Why `text` evidently was NOT submitted on this screen, or None. Not submitted: the paste
    preview hint is on the screen, or the input line (the LAST line with «❯», the box sits below
    the history) still holds a folded paste or the start of the text. A screen without an input
    line (a dialog, an empty capture) gives no evidence against the delivery: None."""
    all_lines = screen.splitlines()
    # the input line is a «❯» line right under the input box's top rule. Claude Code also draws
    # SENT prompts in the history with «❯» (no rule above them): right after a submit, while the box
    # is not redrawn yet, such a history line must not read as text still in the input (W4, 2026-10-03)
    marks = [i for i, l in enumerate(all_lines) if INPUT_MARK in l and i > 0 and _is_rule(all_lines[i - 1])]
    # only the active input region counts: the last «❯» line and the footer below it (or, without
    # an input line, the last few lines). History above may quote the hint or the text itself.
    if not marks:
        return None  # no input box on the screen (a dialog, a redraw): no evidence against the delivery
    region = all_lines[marks[-1]:]  # the input line and the footer under it; the paste preview lives there
    if any(PASTE_PREVIEW in l for l in region):
        return f"the paste preview is on the screen («{PASTE_PREVIEW}»)"
    rest = " ".join(all_lines[marks[-1]].split(INPUT_MARK, 1)[1].replace("\u2502", " ").split())
    if "[Pasted text" in rest:
        return "the folded paste is still in the input line"
    head = _text_head(text)
    k = min(len(head), len(rest))
    if head and k >= min(5, len(head)) and rest[:k] == head[:k]:
        return "the text is still in the input line"
    return None


_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_ESC_OTHER = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")
CLEAR_ROUNDS = 4        # rounds of C-e C-u C-k BSpace before clearing counts as failed
CLEAR_PAUSE = 0.7       # seconds between a round and the look at the screen
CLEAR_FOOTER_POLLS = 15  # the folded-paste footer lingers after clearing: polls (1 s) before giving up
NO_INPUT_BOX = "no input box on the screen"


def pane_ansi(name):
    """The screen WITH its SGR (`capture-pane -p -e`): Claude Code draws the placeholder of an empty
    input («Try "..."») dim, and only the escapes tell it from typed text."""
    r = tmux("capture-pane", "-p", "-e", "-t", pane_target(name), check=False)
    return r.stdout if r.returncode == 0 else ""


def send_clear_keys(name):
    """One round that empties Claude Code's input line: end of line, erase to the start, erase to the
    end (the following lines of a multiline text), join with the previous line."""
    tmux("send-keys", "-t", pane_target(name), "C-e", "C-u", "C-k", "BSpace")


def _sgr_dim_after(params, dim):
    """The dim state after one SGR sequence. Only a standalone parameter 2 turns dim on; 0, 22 and an
    empty list turn it off. The arguments of the extended colours (38/48/58 ; 5 ; n  or  ; 2 ; r ; g ; b,
    and the colon forms 38:2::r:g:b) are consumed whole: a «2» inside them is a colour component, not dim.
    Anything not understood after 38/48/58 swallows the rest of the sequence (when in doubt, no dim:
    a false «empty input» is worse than a spare clearing round)."""
    items = params.split(";")
    if items == [""]:
        return False
    i = 0
    while i < len(items):
        p = items[i]
        head = p.split(":")[0]
        if ":" in p:  # a colon group is one parameter with its sub-parameters (38:2::r:g:b, 4:3)
            i += 1
            continue
        if head in ("38", "48", "58"):
            mode = items[i + 1] if i + 1 < len(items) else ""
            i += 3 if mode == "5" else 5 if mode == "2" else len(items)
            continue
        if head == "2":
            dim = True
        elif head in ("0", "22", ""):
            dim = False
        i += 1
    return dim


def _undimmed(line):
    """The text of an ANSI line without the dim segments and without any escape."""
    out, dim, pos = [], False, 0
    for m in _SGR.finditer(line):
        if not dim:
            out.append(line[pos:m.start()])
        pos = m.end()
        dim = _sgr_dim_after(m.group(1), dim)
    if not dim:
        out.append(line[pos:])
    return _ESC_OTHER.sub("", "".join(out))


def input_empty_reason(screen_ansi):
    """None when the input line of the screen (read with SGR, see pane_ansi) is empty: after «❯» and on
    the continuation lines down to the bottom rule there are only blanks/NBSP or dim text (the
    placeholder), and no paste-preview footer. Otherwise the reason it is not."""
    raw = screen_ansi.splitlines()
    plain = [_ESC_OTHER.sub("", _SGR.sub("", l)) for l in raw]
    marks = [i for i, l in enumerate(plain) if INPUT_MARK in l and i > 0 and _is_rule(plain[i - 1])]
    if not marks:
        return NO_INPUT_BOX
    top = marks[-1]
    bottom = next((i for i in range(top + 1, len(plain)) if _is_rule(plain[i])), len(plain))
    rows = [_undimmed(l).replace("\u2502", " ") for l in raw[top:bottom]]
    rows[0] = rows[0].split(INPUT_MARK, 1)[1] if INPUT_MARK in rows[0] else ""
    typed = [" ".join(r.split()) for r in rows]
    if any("[Pasted text" in r for r in typed):
        return "the folded paste is in the input"
    if any(typed):
        return "text in the input line"
    if any(PASTE_PREVIEW in l for l in plain[top:]):
        return f"the paste preview is on the screen («{PASTE_PREVIEW}»)"
    return None


def input_text(screen_ansi):
    """The text in the input line of the screen (read with SGR, see pane_ansi), without the dim
    placeholder, all whitespace removed (a long text wraps over several rows); None when there is no input
    box (a dialog, a menu, an empty capture). Same region as input_empty_reason."""
    raw = screen_ansi.splitlines()
    plain = [_ESC_OTHER.sub("", _SGR.sub("", l)) for l in raw]
    marks = [i for i, l in enumerate(plain) if INPUT_MARK in l and i > 0 and _is_rule(plain[i - 1])]
    if not marks:
        return None
    top = marks[-1]
    bottom = next((i for i in range(top + 1, len(plain)) if _is_rule(plain[i])), len(plain))
    rows = [_undimmed(l).replace("\u2502", " ") for l in raw[top:bottom]]
    rows[0] = rows[0].split(INPUT_MARK, 1)[1] if INPUT_MARK in rows[0] else ""
    return "".join("".join(rows).split())


def clear_input(name):
    """Empty the input line of the window; None on success, else the reason. Success only by the screen
    (input_empty_reason on `-e` snapshots), never by the keys sent. No input box on the screen (a dialog
    is up): not a single key. Already empty: no keys. The footer of a folded paste lingers for seconds
    after the clearing: waited out, without keys. Call it under the caller's _InputLock."""
    why = input_empty_reason(pane_ansi(name))
    rounds = 0
    while why is not None:
        if why == NO_INPUT_BOX:
            return why
        if "paste preview" in why:  # the input is empty, only the footer is still there
            for _ in range(CLEAR_FOOTER_POLLS):
                time.sleep(1)
                why = input_empty_reason(pane_ansi(name))
                if why is None or "paste preview" not in why:
                    break
            if why is None or "paste preview" in why:
                return why
            continue
        if rounds >= CLEAR_ROUNDS:
            return why
        rounds += 1
        send_clear_keys(name)
        time.sleep(CLEAR_PAUSE)
        why = input_empty_reason(pane_ansi(name))
    return None


def submit(name, text):
    """Press Enter and check on the screen that `text` left the input line; up to SUBMIT_RETRIES
    more Enters with a pause, then NotSubmitted with the reason. The one «was it sent» check of the
    dispatcher (send_text, the Enter-only retry of _deliver) and of `say`."""
    press_enter(name)
    for attempt in range(SUBMIT_RETRIES + 1):
        time.sleep(SUBMIT_PAUSE)
        why = unsent_reason(pane_text(name), text)
        if why is None:
            return
        if attempt == SUBMIT_RETRIES:
            raise NotSubmitted(f"{why} after {SUBMIT_RETRIES + 1} Enter presses")
        press_enter(name)


def send_text(name, text, on_typed=None):
    """Paste text as one bracketed paste, then submit it and check that it left the input line
    (submit). The buffer is private to this dispatcher and session: tmux buffers are global to the
    server. `on_typed` runs once the text is in the window and Enter is still to come (the caller
    records that step)."""
    buf = f"wab-{os.getpid()}-{name}"
    tmux("load-buffer", "-b", buf, "-", input=text)
    tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane_target(name))
    if on_typed:
        on_typed()
    time.sleep(1.5)
    submit(name, text)


def send_command(name, cmd, on_typed=None):
    tmux("send-keys", "-t", pane_target(name), "-l", cmd)
    if on_typed:
        on_typed()
    time.sleep(0.7)
    press_enter(name)


def wait_ready(name, timeout=90):
    """Wait for the Claude TUI input box; accept the folder-trust dialog for our own clone."""
    end = time.time() + timeout
    while time.time() < end:
        txt = pane_text(name)
        if any(m in txt for m in TRUST_MARKERS):
            # default option is "No, exit": move to "Yes, I trust this folder" first
            tmux("send-keys", "-t", pane_target(name), "Down")
            time.sleep(0.5)
            tmux("send-keys", "-t", pane_target(name), "Enter")
            time.sleep(3)
            continue
        if any(m in txt for m in READY_MARKERS):
            return True
        time.sleep(2)
    return False


def auto_mode_off(text):
    """Is the session out of auto mode? True / False, or None when the TUI is not drawn
    (no footer on screen: no verdict). `switch to auto mode` anywhere on the screen, or no
    `auto mode on` among the last six non-empty lines, means off."""
    if not any(m in text for m in READY_MARKERS):
        return None
    if "switch to auto mode" in text.lower():
        return True
    tail = [l for l in text.splitlines() if l.strip()][-6:]
    return not any("auto mode on" in l.lower() for l in tail)


# ---------- transcripts ----------

def transcript_dir(cwd):
    """Claude Code names the directory after the REAL cwd (getcwd resolves symlinks): on macOS
    a clone under /tmp lives in -private-tmp-... . Without realpath the dispatcher read no
    transcript at all: context 0k, no WAB-CHECKPOINT, no /clear rebinding."""
    return projects_dir() / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(str(cwd)))


def transcript_path(cwd, session_id):
    return transcript_dir(cwd) / f"{session_id}.jsonl"


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


READ_CHUNK = 1 << 20  # bytes per read() of a transcript


class TranscriptCache:
    """Incremental reader of session transcripts. Per file it keeps (dev, inode, offset,
    the unfinished last line, counters): a new call reads only what was appended, a
    truncated or replaced file starts over. With `tail_bytes` the first read starts that
    far from the end and drops the cut line (a transcript can be hundreds of MB)."""

    def __init__(self, tail_bytes=None):
        self.tail_bytes = tail_bytes
        self.entries = {}
        self.bytes_read = 0

    @staticmethod
    def _blank(key):
        return {"key": key, "offset": None, "partial": b"", "discard_first": False, "head": None,
                "turns": 0, "tools": 0, "out": 0, "read": 0, "ctx": 0, "limit": 0}

    def read(self, path):
        path = str(path)
        try:
            stt = os.stat(path)
        except OSError:
            self.entries.pop(path, None)
            return self._summary(self._blank(None))
        key = (stt.st_dev, stt.st_ino)
        e = self.entries.get(path)
        if e is None or e["key"] != key or stt.st_size < (e["offset"] or 0):
            e = self.entries[path] = self._blank(key)
        try:
            with open(path, "rb") as f:
                if e["head"] is not None:  # same inode and not shorter, but rewritten in place?
                    length, digest = e["head"]
                    if hashlib.sha1(f.read(length)).hexdigest() != digest:
                        e = self.entries[path] = self._blank(key)
                if e["offset"] is None:
                    head = f.read(min(HEAD_BYTES, stt.st_size)) if f.seek(0) == 0 else b""
                    e["head"] = (len(head), hashlib.sha1(head).hexdigest())
                    start = 0
                    if self.tail_bytes and stt.st_size > self.tail_bytes:
                        start = stt.st_size - self.tail_bytes
                        e["discard_first"] = True
                    e["offset"] = start
                f.seek(e["offset"])
                while True:  # in chunks: a transcript can be hundreds of MB
                    data = f.read(READ_CHUNK)
                    if not data:
                        break
                    self.bytes_read += len(data)
                    e["offset"] += len(data)
                    lines = (e["partial"] + data).split(b"\n")
                    e["partial"] = lines.pop()
                    for raw in lines:
                        if e["discard_first"]:
                            e["discard_first"] = False
                            continue
                        self._count(e, raw)
        except OSError:
            pass
        return self._summary(e)

    @staticmethod
    def _summary(e):
        return {k: e[k] for k in ("turns", "tools", "out", "read", "ctx", "limit")}

    @staticmethod
    def _count(e, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
        if u:
            e["out"] += _num(u.get("output_tokens"))
            e["read"] += _num(u.get("cache_read_input_tokens")) + _num(u.get("input_tokens"))
        content = msg.get("content")
        if isinstance(content, list):
            e["tools"] += sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
        if d.get("isSidechain"):
            return
        e["turns"] += 1
        if d.get("error") == "rate_limit" or d.get("apiErrorStatus") == 429:
            e["limit"] += 1  # the provider refused the session itself (#108): by the fields, never by the text
        if u:
            e["ctx"] = (_num(u.get("input_tokens")) + _num(u.get("cache_creation_input_tokens"))
                        + _num(u.get("cache_read_input_tokens")))


CACHE = TranscriptCache(tail_bytes=TAIL_BYTES)


def context_tokens(w):
    """Tokens in the window at the last main-thread assistant turn of the wave's CURRENT
    session. The transcript is found by session_id, never by directory: waves may share one."""
    sessions = w.get("sessions") or []
    if not sessions:
        return 0
    return CACHE.read(transcript_path(w["cwd"], sessions[-1]))["ctx"]


def transcript_activity(w):
    """mtime of every transcript of the wave's CURRENT session: the main one and its subagents'
    (`<session>/subagents/*.jsonl`), as {path: mtime}; {} when there is none. A wave waiting for
    its background coder/tester keeps a still screen while the subagent works: idleness counts
    a changed file as activity (per file, so one future-dated file cannot hide the others)."""
    sessions = w.get("sessions") or []
    if not sessions:
        return {}
    main = transcript_path(w["cwd"], sessions[-1])
    files = [main]
    try:
        files += list((main.parent / sessions[-1] / "subagents").glob("*.jsonl"))
    except OSError:
        pass
    out = {}
    for f in files:
        try:
            out[str(f)] = f.stat().st_mtime
        except OSError:
            continue
    return out


# ---------- process facts: what still runs behind a wave window (#68) ----------
#
# The facts come from `ps` (a full table, parsed here), never from `pgrep -f`: a pattern given to
# pgrep -f also matches the command line of the waiting loop that carries it (the wait never ends).
# Anything that cannot be read or parsed is ProcFactsError: callers treat it as «unknown» and do
# not act on it.

PS_ARGV = ("ps", "-ww", "-A", "-o", "pid=,ppid=,etime=,args=")
PS_TIMEOUT = 10  # seconds
SHELL_NAMES = ("bash", "sh", "zsh", "dash", "fish")  # what Claude Code's Bash tool starts per command
LOOSE_SHELL_MINUTES = 30  # a shell this old whose only descendants are `sleep` waits for nothing
CLEAR_SLACK = 3  # seconds: a shell started this close before /clear was still the old session's


class ProcFactsError(Exception):
    """The process tree of a wave window could not be read (the reason is the message)."""


def _etime_seconds(text):
    """POSIX etime `[[dd-]hh:]mm:ss` -> seconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text)
    if not m:
        raise ValueError(text)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def process_table():
    """{pid: {"ppid", "age" (seconds), "args", "name" (basename of the first word of args)}} of every
    process of the machine, from one `ps`. Any failure is ProcFactsError."""
    try:
        r = sh(*PS_ARGV, check=False, timeout=PS_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise ProcFactsError(f"ps: timeout after {PS_TIMEOUT} s") from e
    except (OSError, subprocess.SubprocessError) as e:
        raise ProcFactsError(_masked_line(f"ps: {type(e).__name__}: {_exc_text(e)}", 150)) from e
    if r.returncode != 0:
        raise ProcFactsError(f"ps: exit {r.returncode}: {_masked_line(r.stderr or '', 100)}")
    table = {}
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 3)
        try:
            if len(parts) < 4:
                raise ValueError(line)
            pid, ppid, age = int(parts[0]), int(parts[1]), _etime_seconds(parts[2])
        except ValueError as e:
            raise ProcFactsError(f"ps: unparsable line {_masked_line(line, 60)!r}") from e
        first = parts[3].split(None, 1)[0]
        table[pid] = {"pid": pid, "ppid": ppid, "age": age, "args": parts[3],
                      "name": first.rsplit("/", 1)[-1]}
    if not table:
        raise ProcFactsError("ps: empty table")
    return table


def claude_pid(w):
    """PID of the process in the wave window's pane (Claude Code itself), by tmux."""
    try:
        r = tmux("display-message", "-p", "-t", pane_target(w["tmux"]), "#{pane_pid}", check=False)
    except (OSError, subprocess.SubprocessError, KeyError) as e:
        raise ProcFactsError(f"tmux display-message: {type(e).__name__}") from e
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out.isdigit():
        raise ProcFactsError(f"tmux display-message: no pane pid ({_masked_line(out, 30) or 'rc ' + str(r.returncode)})")
    return int(out)


def wave_children(cfg, w):
    """The direct children of the wave's Claude, each as its table row plus "tree": every descendant
    row (children first-level excluded). ProcFactsError when the pid or the table cannot be had."""
    pid = claude_pid(w)
    table = process_table()
    if pid not in table:
        raise ProcFactsError(f"pane pid {pid} is not in the process table")
    kids = {}
    for row in table.values():
        kids.setdefault(row["ppid"], []).append(row)
    out = []
    for child in sorted(kids.get(pid, []), key=lambda r: r["pid"]):
        tree, todo, seen = [], list(kids.get(child["pid"], [])), {child["pid"]}
        while todo:
            row = todo.pop()
            if row["pid"] in seen:
                continue
            seen.add(row["pid"])
            tree.append(row)
            todo.extend(kids.get(row["pid"], []))
        out.append({**child, "tree": sorted(tree, key=lambda r: r["pid"])})
    return out


_EVAL_BODY = re.compile(r"eval '(.*)'\s*<\s*/dev/null")


def _short_command(args, limit=80):
    """What a background shell runs, for a one-line event: the part inside `eval '…'` of Claude
    Code's wrapper when there is one, else the whole command line; masked and shortened."""
    m = _EVAL_BODY.search(args)
    return safe_text(m.group(1) if m else args, limit)


def _age_text(secs):
    return f"{secs} с" if secs < 120 else f"{secs // 60} мин" if secs < 7200 else f"{secs // 3600} ч"


def bg_tails(children, w, now):
    """The wave's background shells that nobody waits for: [{"pid","why","age","args"}…]. A shell is a
    tail when it started before the last /clear (`cleared_at`, with a small margin), or when it is older
    than LOOSE_SHELL_MINUTES and everything under it is `sleep` (or nothing)."""
    cleared = w.get("cleared_at")
    cleared = cleared if num(cleared) is not None else None
    tails = []
    for c in children:
        if c["name"] not in SHELL_NAMES:
            continue
        why = None
        if cleared is not None and now - c["age"] < cleared - CLEAR_SLACK:
            why = "запущен до /clear"
        elif c["age"] > LOOSE_SHELL_MINUTES * 60 and all(d["name"] == "sleep" for d in c["tree"]):
            why = f"{c['age'] // 60} мин без работы, только sleep"
        if why:
            tails.append({"pid": c["pid"], "why": why, "age": c["age"], "args": c["args"]})
    return tails


def _procs_unreadable(cfg, w, wave, err):
    """One event per episode of an unreadable process tree (shared by the tails watch and the nudge)."""
    if once_per(w, "procfacts", _exc_text(err)[:150]):
        event(cfg, f"{wave}: дерево процессов недоступно: {_exc_text(err)}")


def _watch_bg_tails(cfg, st, wave, w, children, now):
    """#68 item 2. One event per episode (the set of tail pids); no killing, no notice: the event and
    the dashboard mark (`bg_tails`) only. An empty set ends the episode."""
    tails = bg_tails(children, w, now)
    if not tails:
        w.get("notified", {}).pop("bg_tails", None)
        w.pop("bg_tails", None)
        return
    w["bg_tails"] = [{"pid": t["pid"], "why": t["why"], "age": t["age"]} for t in tails]
    key = ",".join(str(t["pid"]) for t in sorted(tails, key=lambda t: t["pid"]))
    if once_per(w, "bg_tails", key):
        items = "; ".join(f"pid {t['pid']} ({t['why']}, {_age_text(t['age'])}): {_short_command(t['args'])}"
                          for t in tails)
        event(cfg, f"{wave}: фоновые хвосты в окне волны: {items}")


def session_marker(cfg, wave):
    return f"[wab:{cfg['chain']}/{cfg['run_id']}/{wave}]"


# What Claude Code itself writes into a fresh transcript after /clear, before the first real
# message: an isMeta caveat, the echo of /clear and its (empty) stdout. Not a wave message.
_CLEAR_SCAFFOLD = re.compile(
    r"\s*(?:<local-command-caveat>.*</local-command-caveat>"
    r"|<local-command-stdout>.*</local-command-stdout>"
    r"|<command-name>/clear</command-name>\s*<command-message>clear</command-message>"
    r"\s*<command-args>\s*</command-args>)\s*", re.S)


def _first_user_text(path):
    """Text of the first main-thread user message in the first 256 KB of a transcript
    (a string, or the text blocks of a content list; tool_result blocks do not count).
    The scaffolding Claude Code writes on /clear (isMeta lines, the /clear echo and its
    stdout) is skipped: after /clear the marker sits in the resume command that follows it."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(FIRST_MESSAGE_BYTES)
    except OSError:
        return ""
    lines = head.split(b"\n")
    if len(head) >= FIRST_MESSAGE_BYTES:
        lines.pop()  # cut mid-line
    for raw in lines:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
            if texts:
                text = "\n".join(texts)
        if text is not None and not _CLEAR_SCAFFOLD.fullmatch(text):
            return text
    return ""


def owned_sessions(st):
    """Every session id that already belongs to some wave: current sessions AND those of all
    earlier attempts (a restarted wave keeps its old transcripts, which carry the same marker)."""
    owned = set()
    for w in st["waves"].values():
        for rec in [w, *(w.get("attempts") or [])]:
            owned.update(rec.get("sessions") or [])
    return owned


# Frozen-context watchdog (#44). After /clear the session may stay unbound: the dashboard then shows
# an old context for hours while the real journal of the wave (another .jsonl in the same directory)
# keeps growing. When the bound journal and its context stay unchanged for this many ticks AND another
# journal of the directory (owned by no other wave) grew meanwhile, the episode is reported once.
CTX_FROZEN_TICKS = 5  # ticks (tick_seconds each) of an unchanged bound journal before the check fires


def _journal_sizes(cwd, skip):
    """{stem: size} of the top-level journals of the working copy's transcript directory, without the
    stems in `skip`. A file that cannot be read is left out; an unreadable directory gives {}."""
    sizes = {}
    try:
        for f in transcript_dir(cwd).glob("*.jsonl"):
            if f.stem in skip:
                continue
            try:
                sizes[f.stem] = f.stat().st_size
            except OSError:
                continue
    except OSError:
        return {}
    return sizes


def watch_context(cfg, st, wave, w, tokens):
    """Keep w["ctx_watch"] (the bound journal's size and context, how many ticks they stood still, the
    other journals' sizes) and raise w["ctx_frozen"] with ONE event per episode when the bound journal
    is frozen while another journal of the same working copy grows. An episode ends the moment the
    bound journal or the context changes: the mark and the dedup mark go. In memory; the tick's save
    persists it. Never raises: the context measurement must not stop supervision."""
    try:
        if not w.get("sessions"):
            return
        stem = w["sessions"][-1]
        try:
            size = transcript_path(w["cwd"], stem).stat().st_size
        except OSError:
            size = -1
        foreign = set()
        for name, rec in st["waves"].items():
            if name != wave and isinstance(rec, dict):
                for r in [rec, *(rec.get("attempts") or [])]:
                    foreign.update(r.get("sessions") or [])
        for r in w.get("attempts") or []:
            foreign.update(r.get("sessions") or [])  # earlier tries of this wave are not its live journal
        others = _journal_sizes(w["cwd"], foreign | {stem})
        prev = w.get("ctx_watch")
        if not (isinstance(prev, dict) and prev.get("bound") == stem and prev.get("size") == size
                and prev.get("ctx") == tokens):
            # first measurement, or the bound journal / the context moved: a new episode
            w["ctx_watch"] = {"bound": stem, "size": size, "ctx": tokens, "ticks": 0, "others": others,
                              "grew": False}
            w.pop("ctx_frozen", None)
            w.get("notified", {}).pop("ctx_frozen", None)
            return
        old = prev.get("others") if isinstance(prev.get("others"), dict) else {}
        grown = next((k for k, v in others.items() if k not in old or v > (old[k] if isinstance(old[k], int) else 0)),
                     None)
        prev["others"] = others
        prev["ticks"] = (prev["ticks"] if isinstance(prev.get("ticks"), int) else 0) + 1
        if grown is not None and not prev.get("grew"):
            prev["grew"], prev["grown"] = True, grown
        if prev["ticks"] >= CTX_FROZEN_TICKS and prev.get("grew"):
            w["ctx_frozen"] = True
            if once_per(w, "ctx_frozen", f"{stem}:{size}:{tokens}"):
                event(cfg, f"{wave}: контекст не меряется (сессия не привязана?): журнал {stem} неизменен "
                           f"{prev['ticks']} тактов, растёт {prev.get('grown')}")
    except Exception as e:  # noqa: BLE001 - a diagnostic, never a reason to stop the tick
        event(cfg, f"{wave}: проверка замёрзшего контекста не удалась ({type(e).__name__})")


def find_new_session(cfg, st, wave):
    """After /clear the wave continues in a new session file. Find it by the marker in its
    first message, among transcripts of this clone that no wave owns yet."""
    owned = owned_sessions(st)
    d = transcript_dir(st["waves"][wave]["cwd"])
    if not d.exists():
        return None
    marker = session_marker(cfg, wave)
    files = []
    for f in d.glob("*.jsonl"):
        try:
            files.append((f.stat().st_mtime, f))
        except OSError:
            continue
    for _, f in sorted(files, key=lambda t: t[0], reverse=True):
        if f.stem in owned:
            continue
        if marker in _first_user_text(f):
            return f.stem
    return None


# ---------- Telegram ----------

_tg = {}

# Outgoing text is written by the wave session and may quote secrets or personal data.
# Everything leaving through notify() passes redact(); the full text stays in $WAB_DIR.
TG_LIMIT = 600        # quoted wave text (result.md, BLOCKED question)
TG_MESSAGE_LIMIT = 1200  # whole message; quoted text is capped at TG_LIMIT first, so the
                         # framing and the attach command after it always fit
# redact() rules, in order (the list is also in references/waves.md, «redact»):
#  1. STRUCTURED: secrets recognised by their form or their key — PEM private keys, prefixed tokens
#     (sk-, ghp_, xox?-, AKIA, JWT, Telegram bot token), Cookie/Set-Cookie values, `key=value` with a
#     secret-like key, Authorization, bearer/basic, userinfo in URLs, e-mail. Applied everywhere.
#  2. HEURISTIC: phone numbers, hex keys (_HEX_KEY) and long opaque blobs (_OPAQUE). The path of a
#     github.com URL is exempt from them (owner/repo/pull/12, actions/runs/N stay readable), except
#     _HEX_KEY: a 32+ hex run there is masked too, unless it is a 40-hex SHA right after /commit/,
#     /commits/ or /tree/. The query and fragment of the URL are not exempt.
#  3. Explicit exceptions only: a bare 40-64 hex after a SHA label (`commit`, `sha`, `head`, ...),
#     the github SHA above, an _OPAQUE run made only of word-like parts (_readable_dashed), and the
#     owner's merge script path `.cache/wab/<wave>.<sha12>.<id16>.merge` when that file exists
#     (_OWNER_SCRIPT, _owner_script_spans).
# A long run of base64/base64url characters. Without a dash any 40+ run is a blob; with a dash
# it stays readable only when every part between -, /, _ and + looks like a word (see
# _readable_dashed): `kebab-case-words`, paths, dates stay, base64url and UUID-like keys go.
_OPAQUE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}")
# 32+ hex digits in a row, or in dashed groups (UUID), WHEREVER they are: inside a token with a
# prefix or suffix (`key-<hex>`, `<hex>-us21`, `id_<hex>`, `v<hex>`, `cache/<hex>`) only the hex
# part is masked, the rest stays.
_HEX_KEY = re.compile(r"(?=(?:-?[0-9A-Fa-f]){32})[0-9A-Fa-f]+(?:-[0-9A-Fa-f]+)*")
# The words of a key that hides its value: ONE source for the `key=value` rule below and for _key_hides_value (the
# dict keys of a structure that is masked by its leaves).
_SECRET_WORDS = (r"(?:secret|token|password|passwd|pwd|api[_-]?key|private[_-]?key|dsn|cookie|session|"
                 r"credential|connection[_-]?string|accountkey|sharedaccesskey|signature)")
_SECRET_KEY = r"(?:[\w.-]*" + _SECRET_WORDS + r"[\w.-]*|sig)"
_STRUCTURED = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jwt",                                                              # JWT: _JWT below
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),                      # Telegram bot token
    re.compile(r"(?i)\b(set-cookie|cookie)(\s*:\s*)[^\r\n]*"),                 # whole header value
    # the same matches as `(?<![\w-])(["']?<_SECRET_KEY>["']?)...`, in linear time (Codex, #85): that one starts after
    # every `.` of a run and backtracks `[\w.-]*KEY[\w.-]*` over the rest of it. A key ends where its run of [\w.-] ends
    # (`:`/`=`/a quote follow it), so it is the WHOLE run that holds a secret word, and its leftmost start is the start
    # of the run or a quote after a `.` (a start inside the run that matches means the run's start matches too); only
    # the bare `sig` may also start after a `.` of the run (`.sig=`), and there the run-wide branch is not tried.
    re.compile(r"(?i)(?:(?<![\w.-])|(?<=\.)(?=[\"']|sig))"
               r"([\"']?(?:(?:(?<![\w.-])|(?<=[\"']))(?=[\w.-]*" + _SECRET_WORDS + r")[\w.-]+|sig)[\"']?)"
               r"(\s*[:=]\s*)"
               r"(?!\[скрыто\])(?:\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?|[^\s,;&}\]]+)"),             # key as a word, value as a whole
    re.compile(r"(?i)\b((?:proxy-)?authorization)(\s*:\s*)(?:(?:bearer|basic|token|digest)\s+)?\S+"),
    re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?<=://)[^/\s@]+(?=@)"),                           # userinfo in URLs, with or without password
    None,                                                               # e-mail addresses: _EMAIL below
]
class _LinearEmail:
    """The rule `[\\w.+-]+@domain` (e-mail addresses) with the matches of re.sub over it, in linear time. re.sub
    retries the pattern from EVERY character of a long run of `[\\w.+-]` without an `@` and is quadratic (Codex, #85).
    Its leftmost match starts either where the scan goes on (right after the last match) or at the start of a run:
    a start inside a run that matches means the run's earlier character matches too. So: one anchored try where
    the scan goes on, else a search that only starts a run (lookbehind); the match itself is the same pattern's."""
    RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    RUN_START = re.compile(r"(?<![\w.+-])" + RX.pattern)

    def sub(self, repl, text):
        out, pos = [], 0
        while (m := self.RX.match(text, pos) or self.RUN_START.search(text, pos)) is not None:
            out += [text[pos:m.start()], repl(m)]
            pos = m.end()
        return "".join(out + [text[pos:]])


_EMAIL = _LinearEmail()
_STRUCTURED[_STRUCTURED.index(None)] = _EMAIL


class _LinearJwt:
    """The rule `\\beyJ<seg>.<seg>.<seg>` (JWT) with the matches of re.sub over it, in linear time. re.sub tries every
    `eyJ` of a long run of [A-Za-z0-9_-] (`eyJ...-eyJ...-`), and each try runs to the end of the run looking for the
    `.` (quadratic, Astra on #85). A segment ends where its run ends (no `.` inside a run), so a try that fails fails
    for every later `eyJ` of the same run as well: they are skipped up to the run's end."""
    RX = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
    START = re.compile(r"\beyJ")
    RUN = re.compile(r"[A-Za-z0-9_-]*")

    def sub(self, repl, text):
        out, pos, at, failed_to = [], 0, 0, -1
        while (c := self.START.search(text, at)) is not None:
            if c.start() < failed_to:  # a later `eyJ` of a run whose first try failed
                at = failed_to
                continue
            m = self.RX.match(text, c.start())
            if m is None:
                failed_to = self.RUN.match(text, c.start()).end()
                at = c.start() + 1
                continue
            out += [text[pos:m.start()], repl(m)]
            pos = at = m.end()
        return "".join(out + [text[pos:]])


_JWT = _LinearJwt()
_STRUCTURED[_STRUCTURED.index("jwt")] = _JWT
_HEURISTIC = [
    re.compile(r"\+\d[\d ()-]{8,}\d"),                                  # phone numbers (+7 ...)
    re.compile(r"(?<![\w+])(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"),  # RU, no plus
    re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{2}[-\s]\d{2}(?!\d)"),             # 10 digits, separated
    _HEX_KEY,                                                           # UUID, hex keys
    _OPAQUE,                                                            # long opaque blobs
]
# A URL token that may be on github.com: it starts a token (start, whitespace, quote, bracket,
# backtick, comma) and is either `http(s)://<host>/...` or a bare `[www.]github.com/...`. It ends
# at the query/fragment or at a token boundary (whitespace, comma, brackets, quotes, <>). Whether
# the host IS github.com is decided by _github_host on the parsed URL, never by a substring.
_TOKEN_END = "\\s?#,()\\[\\]<>\"'`"
_GITHUB_PATH = re.compile(r"(?i)(?<![^\s\"'`(\[<,])(?:https?://[^/" + _TOKEN_END + r"]+|(?:www\.)?github\.com)"
                          r"/[^" + _TOKEN_END + r"]*")
_GITHUB_HOSTS = ("github.com", "www.github.com")
# The owner's merge script (owner_script_name) in a notice: its sha12 and id16 may look like a phone
# number (`.71234567890a.`) or a blob to the heuristics, and a masked path is a useless command. Only
# a path of that shape whose file EXISTS in ~/.cache/wab (written by write_owner_script) is exempt
# from HEURISTIC: a secret dressed up as such a path in the wave's own text names no file and is
# masked as before. The check survives a restart of the dispatcher (no in-memory registry).
_OWNER_SCRIPT = re.compile(r"\.cache/wab/([A-Za-z0-9_-]+\.[0-9a-f]{12}\.[0-9a-f]{16}\.merge)(?![\w.])")


def _owner_script_spans(text):
    spans = []
    for m in _OWNER_SCRIPT.finditer(text):  # the home folder is looked up only for a path of that shape (#85)
        try:
            if (pathlib.Path.home() / ".cache" / "wab" / m.group(1)).is_file():
                spans.append(m.span())
        except OSError:
            pass
    return spans
_GITHUB_SHA = re.compile(r"/(?:commit|commits|tree)/$")


_SHA_LABEL = re.compile(r"(?i)(?:\b(?:commit|sha|head|reviewed_head|base|packet_hash)(?:\s+|\s*[=:]\s*)"
                        r"|sha256\s*[:=]\s*)$")


# A word-like part of a dashed token: short, one letter case (or Capitalized), digits only, or a
# short lower-case+digits tag (v2, sha256, abcd1234). A segment of 9+ mixing letters and digits,
# or mixed case with digits, is not a word.
_WORDISH = re.compile(r".{0,3}|[a-z]+|[A-Z]+|[A-Z][a-z]+|\d+|[a-z0-9]{1,8}")


def _readable_dashed(m):
    """Known limit of the heuristic: a secret made only of word-like parts (e.g. dictionary words
    joined by dashes) stays readable; dashed hex is caught earlier by _HEX_KEY."""
    t = m.group(0)
    return "-" in t and all(_WORDISH.fullmatch(part) for part in re.split(r"[-/_+=]", t))


_SPACE = re.compile(r"\s")


def _label_window(text, end):
    """Where a label that ends the text at `end` (_SHA_LABEL: a word of 13 characters at most, then spaces, `=`/`:`,
    spaces) can start: the search runs over this window and not over the whole text before `end`, which made redact()
    quadratic in the number of its matches (Codex, #85). `\\b` at the window's start still sees the character before it."""
    i = end
    while i and _SPACE.match(text, i - 1):
        i -= 1
    if i and text[i - 1] in "=:":
        i -= 1
        while i and _SPACE.match(text, i - 1):
            i -= 1
    return max(0, i - 13)


def _labelled_sha(m):
    """A bare 40-64 hex string is a key; only one right after an explicit SHA/hash label is a commit id."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", m.group(0))
                and _SHA_LABEL.search(m.string, _label_window(m.string, m.start()), m.start()))


def _after_commit_path(m):
    """The match follows `/commit/`, `/commits/` or `/tree/`: looked for in the 9 characters before it (`/commits/`),
    not in the whole text before it (quadratic, Codex #85)."""
    return bool(_GITHUB_SHA.search(m.string, max(0, m.start() - 9), m.start()))


def _within(spans, m):
    """The match lies inside one of `spans` (sorted, not overlapping: from finditer): by bisection, not by a walk over
    all of them for every match (quadratic in the number of URLs and matches, Codex #85)."""
    i = bisect.bisect_right(spans, (m.start(), float("inf"))) - 1
    return i >= 0 and m.end() <= spans[i][1]


def _github_host(url):
    """True when the URL token's parsed hostname is github.com (or www.github.com)."""
    if not re.match(r"(?i)https?://", url):
        url = "https://" + url
    try:
        return (urllib.parse.urlsplit(url).hostname or "") in _GITHUB_HOSTS
    except ValueError:
        return False


def _mask_structured(m):
    if m.re.groups >= 2 and m.group(2):  # `key: value` -> the key and separator stay
        return m.group(1) + m.group(2) + "[скрыто]"
    return "[скрыто]"


def redact(text, limit=TG_LIMIT, owner_paths=True):
    """Mask secrets and personal data in text that leaves for Telegram. Rules: see the comment
    above _OPAQUE. A heuristic, not a guarantee; its limits are listed in references/waves.md.
    `owner_paths=False` drops the owner-script exemption (rule 3): text of the wave and other
    outside text is never trusted to name the dispatcher's own file (see quote)."""
    for rx in _STRUCTURED:
        text = rx.sub(_mask_structured, text)
    for rx in _HEURISTIC:
        spans = [m.span() for m in _GITHUB_PATH.finditer(text) if _github_host(m.group(0))]
        own = _owner_script_spans(text) if owner_paths else []

        def keep(m, rx=rx, spans=spans, own=own):
            if _labelled_sha(m) or _within(own, m):
                return True
            in_github = _within(spans, m)
            if rx is _HEX_KEY:
                return bool(in_github and re.fullmatch(r"[0-9a-fA-F]{40}", m.group(0)) and _after_commit_path(m))
            return in_github or (rx is _OPAQUE and _readable_dashed(m))

        text = rx.sub(lambda m: m.group(0) if keep(m) else "[скрыто]", text)
    return _clip(Masked(text), limit)


# The wave's own text (result.md, a status line, a reason from outside) is a QUOTE, never part of the
# dispatcher's trusted wording. quote() cleans control characters (so the wave cannot carry the
# markers below), masks with redact(owner_paths=False) and wraps the text in the two markers; the
# notice keeps them in the outbox. render_notice() is the one place that turns a notice into the text
# that leaves (Telegram, display-message, ATTENTION): it masks the quotes AGAIN without the
# owner-script exemption and shows them marked (a «Цитата волны:» block with `> ` lines, or an inline
# «цитата волны: ...»), and masks the rest, the dispatcher's own wording, as before. The exemption
# exists for the one path the dispatcher itself puts into a message (write_owner_script), and a wave
# could name a secret as the <wave> segment of such a path and quote it.
QUOTE_OPEN, QUOTE_CLOSE = "\x02", "\x03"
_QUOTE_SPAN = re.compile("\x02(.*?)(?:\x03|$)", re.S)
def _chars_of(categories):
    """Characters of the given Unicode categories (planes 0-2 and 14 hold every Cf/Zs/Zl/Zp there is),
    as the inside of a regex class: computed, so the set follows the interpreter's Unicode tables."""
    return "".join(re.escape(c) for c in map(chr, [*range(0x30000), *range(0xE0000, 0xE0200)])
                   if unicodedata.category(c) in categories)


# Default_Ignorable_Code_Point of Unicode (DerivedCoreProperties.txt), as code point ranges: drawn as
# nothing whatever their category. Most are Cf; the rest are combining marks and fillers (CGJ U+034F,
# Hangul fillers U+115F U+1160 U+3164 U+FFA0, Khmer U+17B4 U+17B5, Mongolian FVS U+180B-U+180F,
# variation selectors U+FE00-U+FE0F and U+E0100-U+E01EF) and code points still UNASSIGNED (Cn: U+2065,
# U+FFF0-U+FFF8, most of U+E0000-U+E0FFF), reserved by Unicode to stay invisible, so a terminal or
# Telegram draws them as nothing too, whatever the interpreter's tables say.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))


def _default_ignorable():
    """The default-ignorable characters that _INVISIBLE does not already hold by category (Cf, Cc, Zl,
    Zp): the rest, unassigned ones (Cn) included."""
    return [chr(c) for lo, hi in _DEFAULT_IGNORABLE_RANGES for c in range(lo, hi + 1)
            if unicodedata.category(chr(c)) not in {"Cf", "Cc", "Zl", "Zp"}]


def _as_ranges(chars):
    """Characters as the inside of a regex class, consecutive code points collapsed into `a-b` (the
    unassigned tag block alone is thousands of them)."""
    out, codes = [], sorted(map(ord, chars))
    i = 0
    while i < len(codes):
        j = i
        while j + 1 < len(codes) and codes[j + 1] == codes[j] + 1:
            j += 1
        lo, hi = re.escape(chr(codes[i])), re.escape(chr(codes[j]))
        out.append(lo if i == j else f"{lo}-{hi}")
        i = j + 1
    return "".join(out)


# A character that is invisible or breaks a line glues (`a<ZWSP>sk-...`) or splits (`sk-<ZWSP>...`) a
# secret past redact(): controls (Cc, all but \n \t, \r is handled apart), format characters (Cf: zero
# width, bidi marks, soft hyphen, tags...), the other default-ignorable characters (variation
# selectors such as U+FE0F, CGJ, Hangul fillers, unassigned ones such as U+2065: `sk-<U+FE0F>ant-...`
# is drawn as `sk-ant-...`), line and paragraph separators (Zl, Zp). Any token that holds one is
# masked whole. Unicode spaces (Zs) are separators like a plain space, never part of a token.
_INVISIBLE = ("\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + _chars_of({"Cf", "Zl", "Zp"})
              + _as_ranges(_default_ignorable()))
# A word is a run between spaces, tabs and line breaks. A lone CR is NOT a break for the masking (it is one for
# the line split): `password=sec\rret` is one word, else its tail `ret` would be left over the cut (#81).
# The Unicode spaces (Zs: NBSP, U+2000-U+200A, U+3000...) ARE separators, like a plain space: a secret torn by
# any space leaves its parts visible, plain or not (an existing test pins it, W3Quote); the characters of
# str.split() that are no space to the eye (\x1c-\x1f, NEL, U+2028/9) are in _INVISIBLE: the word is masked whole.
_SPACES = " \\n\\t" + _chars_of({"Zs"})
_CONTROL = re.compile(f"[{_INVISIBLE}]")
# The text split into words and separators in ONE pass (the separators are kept: odd items): a regex that looks for a
# word around a character (`[^ ]*X[^ ]*`) retries from every character of a long word and is quadratic (Codex, #85).
_WORD_SPLIT = re.compile(f"([{_SPACES}]+)")
MASK = "[скрыто]"


class Masked(str):
    """Text that has passed safe_text(): the mark that every cutting or joining helper (_clip, _head, _one_line,
    _masked_line) asks for FIRST (_require_masked). A str subclass: json, state.json and files take it as a str;
    an operation on it (a slice, join) gives a plain str again, so a helper wraps its result itself. Made only
    by safe_text and those helpers (tests/…: the static check). The methods that only trim or cut it (strip,
    lstrip, rstrip, splitlines) give Masked back, so a part masked WITH a policy (owner_paths=True: the
    owner-script path of a notice) keeps being a Masked part; an f-string or `+` with a plain str gives a str,
    and a helper asked with it masks again WITHOUT the policy. Several parts are joined by _join_masked only."""
    __slots__ = ()

    def strip(self, chars=None):
        return Masked(str.strip(self, chars))

    def lstrip(self, chars=None):
        return Masked(str.lstrip(self, chars))

    def rstrip(self, chars=None):
        return Masked(str.rstrip(self, chars))

    def splitlines(self, keepends=False):
        return [Masked(l) for l in str.splitlines(self, keepends)]


def _join_masked(parts, sep="", owner_paths=False):
    """THE one way to join parts into a text that goes on to _clip / _head / _one_line / _masked_line: Masked parts
    stay as they are (so the policy they were masked with is kept: render_notice masks the dispatcher's wording with
    owner_paths=True), any other part (a literal of the dispatcher, a str) is masked first with `owner_paths`.
    An f-string or `"".join` of Masked parts is a plain str, and the helper after it would mask it AGAIN
    without the policy: the owner's merge script path in a notice became `[скрыто]`."""
    return Masked(sep.join(_require_masked(p, owner_paths) for p in parts))


def _require_masked(text, owner_paths=False):
    """THE one place of the rule «mask first, then anything else»: a Masked text goes on as it is, any other
    text (a str, an exception, None) is masked first by safe_text. Chosen over a TypeError: a caller cannot
    forget the mask (no helper ever touches raw outside text), and the mask is idempotent, so a text that is
    masked twice costs only time. A caller that needs the owner-script exemption masks itself (owner_paths=True)."""
    return text if isinstance(text, Masked) else safe_text(text, 10 ** 9, owner_paths)


DEPTH_MARK = "[скрыто: глубина]"


# A dict key that looks secret: the matches of `(?i)(?:<_SECRET_KEY>|(?:proxy-)?authorization)` taken whole (fullmatch),
# in linear time (Astra on #85): there `[\w.-]*WORD[\w.-]*` retried the tail after every secret word of a long key
# (`token`*N + `!`). The same test in two parts: the key is made of [\w.-] only and holds a secret word somewhere
# (every word is made of [\w.-] too), or it is `sig` / `authorization` / `proxy-authorization` itself.
_KEY_ALPHABET = re.compile(r"[\w.-]*")
_SECRET_WORD_RE = re.compile(r"(?i)" + _SECRET_WORDS)
_BARE_SECRET_KEY = re.compile(r"(?i)sig|(?:proxy-)?authorization")


def _secret_key(k):
    return bool(_KEY_ALPHABET.fullmatch(k) and _SECRET_WORD_RE.search(k) or _BARE_SECRET_KEY.fullmatch(k))


def _key_hides_value(key):
    """True when the value under this dict key must be hidden WHOLE (and everything nested in it): the key holds an
    invisible, control or default-ignorable character (it is masked, so what it names is unknown), a lone CR, or it
    looks secret by the rule of `key=value` of redact (_SECRET_KEY, any case). gate.py (stdlib-only, imported by this
    module) asks the same function through gate.key_hides_value."""
    if not isinstance(key, str):
        return False
    k = key.replace("\r\n", "\n")
    return bool(_CONTROL.search(k) or "\r" in k or _secret_key(k.strip().strip("\"'")))


gate.key_hides_value = _key_hides_value  # the one predicate: gate._leaves hides by it (its own default hides every value)


def _masked_leaves(obj, owner_paths, depth=0):
    """A container with every str leaf AND dict key masked (safe_text of each), the rest as it is: str() of a
    list or dict turns an invisible character into a literal `\\u200b` (repr), which no mask can find, so the
    leaves are masked BEFORE the container is written."""
    if isinstance(obj, str):
        return safe_text(obj, 10 ** 9, owner_paths)
    if depth > 20:
        return DEPTH_MARK  # never str()/repr() of what is left: it would write an invisible character as `\\u200b`
    nxt = depth + 1
    if isinstance(obj, dict):
        # a key that hides its value takes the value WHOLE (a structure under it too): key and value are not masked
        # independently, else `{"password<ZWSP>": "Hunter2…"}` shows the value of a masked key
        return {_masked_leaves(k, owner_paths, nxt): (MASK if _key_hides_value(k) else _masked_leaves(v, owner_paths, nxt))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = [_masked_leaves(v, owner_paths, nxt) for v in obj]
        return tuple(items) if isinstance(obj, tuple) else items
    if isinstance(obj, (set, frozenset)):
        return [_masked_leaves(v, owner_paths, nxt) for v in obj]
    if isinstance(obj, BaseException):
        # the args, masked like any container, NEVER str(e): str(KeyError("password<ZWSP>=x")) is the repr of the
        # string and several args give str(tuple): the invisible character becomes a literal `\\u200b` first
        args = list(obj.args)
        if isinstance(obj, OSError):  # what str() adds to the args: the file names
            args += [f for f in (obj.filename, obj.filename2) if f is not None and f not in args]
        parts = [_masked_leaves(a, owner_paths, nxt) for a in args]
        if len(parts) == 1:
            return parts[0]
        return _join_masked([p if isinstance(p, str) else str(p) for p in parts], " ", owner_paths)
    if isinstance(obj, (bytes, bytearray)):
        return safe_text(bytes(obj).decode("utf-8", errors="replace"), 10 ** 9, owner_paths)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return safe_text(str(obj), 10 ** 9, owner_paths)


def _text_of(obj, owner_paths):
    """The str that safe_text masks: a str as it is; a container (or an exception holding one) by its masked leaves."""
    if isinstance(obj, str):
        return obj
    masked = _masked_leaves(obj, owner_paths)
    return masked if isinstance(masked, str) else str(masked)


def _exc_text(e, owner_paths=False):
    """An exception as text for an event, a notice or an error: its args masked as leaves (safe_text), never str(e)."""
    return safe_text(e, 10 ** 9, owner_paths)


def safe_text(text, limit, owner_paths=False):
    """THE one masking of text that is shown to a human (Telegram, display-message, ATTENTION, the
    dashboard, chain-result.md, events.log, status): there is no other way to mask for output, and
    OUTPUT_PATHS lists every path that uses it (#81). The result is Masked. In this order, never another:
    1. a word (a run between ASCII spaces, tabs and line breaks) that holds an invisible, control or
       default-ignorable character is masked WHOLE, before any parsing or cutting: such a character
       glues (`a\\x00sk-...`) or splits (`sk-\\x00...`) a secret past redact(), and no word of a wave holds one;
       a lone CR inside a word is one of them: a word whose glued form (without the CR) is a secret is masked whole;
    2. redact() over the whole text (no cut yet);
    3. the cut to `limit` at a safe border: the mask is never torn (a border inside it cuts before it),
       and the cut is never made BEFORE the masking."""
    parts = _WORD_SPLIT.split(_text_of(text, owner_paths).replace("\r\n", "\n"))
    for i in range(0, len(parts), 2):  # the words; a separator stays what it is (a Zs too)
        word = parts[i]
        if _CONTROL.search(word):
            parts[i] = MASK
        elif "\r" in word:  # masked whole when its glued form holds a secret, else the CR is a line break
            glued = word.replace("\r", "")
            parts[i] = MASK if redact(glued, 10 ** 9, owner_paths) != glued else word.replace("\r", "\n")
    return _clip(Masked(redact("".join(parts), 10 ** 9, owner_paths)), limit)


# The registry of EVERY path that shows text to a human (#81). Each of them masks with safe_text() and
# nothing else (no redact() / safe_text-less cut in a path of output: tests/…/superarmanda_waves_test.py
# checks it statically by ast and goes through THIS registry with every class of invisible character).
# A new path of output is added here in the same change as its test: the test compares the two sets.
OUTPUT_PATHS = {
    "quote": "quote(): the wave's text inside a notice",
    "render_notice": "render_notice(): the text of a notice as it leaves (Telegram, display-message, ATTENTION)",
    "first_line": "_first_line(): the first line of a rendered notice (display-message, ATTENTION, events)",
    "notify_telegram": "notify(): the message to Telegram",
    "notify_display": "notify() without Telegram: display-message in the tmux status line",
    "attention": "attention_text()/note_attention(): the $RUN_DIR/ATTENTION file",
    "event_line": "event(): a line of events.log and of the watch window",
    "short_command": "_short_command(): a background command in an event",
    "note_question": "note_question(): a question to the owner kept for chain-result.md",
    "chain_result": "write_chain_result(): chain-result.md",
    "policy_question": "_policy_question(): the question in policy-decisions.log",
    "say_echo": "_say_first(): the text of `say` in its events",
    "blocked_notice": "blocked_fields() + notice_text(): the BLOCKED notice that needs the owner (the first line of the question, telegram_quote only)",
    "status_cmd": "status_cmd(): `wab.py status`",
    "owner_merge_regate": "owner_merge(): the exit text of the second gate after the threads were closed",
    "resolve_why": "_resolve_why(): the errors (a nested structure) of a thread that owner-merge could not resolve",
    "dash_waves_table": "dash.waves_table(): the table of the waves, an unknown status of a wave as its label",
    "dash_pipeline": "dash.pipeline(): the line of the waves (an unknown status of a wave takes no label there)",
    "status_file": "_write_status(): the dispatcher's own `status` file and its return value (last_status)",
    "gate_failed": "_gate_failed(): status, last_status, the message to the window, events, notice of a failed merge gate",
    "dash_event": "dash.humanize_event(): the events panel of the dashboard",
    "dash_current_panel": "dash.current_panel(): the status and the screen of the current wave",
    "dash_manifest": "dash.manifest_lines(): the superarmanda block of the current wave",
}

# The registry of EVERY path that shows a TIME to a human (#88): `id -> module.function`. Each of them goes through the
# one function hum() -> humantime.human() and formats nothing itself: tests/…/superarmanda_waves_test.py (W6OwnerTime)
# checks it statically by ast, and goes through THIS registry with a zone, a night and a failure of formatting. A new
# path of output is added here in the same change as its driver in that test. Machine logs (events.log, state.json,
# manifest, policy-decisions.log) are UTC on purpose and are not here.
TIME_PATHS = {
    "human_time": "wab.hum",
    "notice_stamp": "wab.notice_stamp",
    "attention": "wab.attention_text",
    "chain_result": "wab.write_chain_result",
    "status_cmd": "wab.status_cmd",
    "idle_notice": "wab.notice_text",
    "dash_header": "dash.header",
    "dash_events": "dash.events_panel",
}


def quote(text, limit=TG_LIMIT):
    """The wave's (or any outside) text for a notice: control characters (and so the quote markers) are
    removed, secrets masked WITHOUT the owner-script exemption, the length capped at `limit`, the whole
    wrapped in the quote markers. See render_notice."""
    return QUOTE_OPEN + _clip(safe_text(text, 10 ** 9, False).strip(), limit).strip() + QUOTE_CLOSE


def _head(text, n):
    """text[:n] that does not end inside the mask `[скрыто]`: a border inside it cuts before it."""
    text = _require_masked(text)
    head = text[:n]
    for k in range(min(len(MASK) - 1, n), 0, -1):
        if head.endswith(MASK[:k]) and text.startswith(MASK[k:], n):
            return Masked(head[:-k])
    return Masked(head)


def _clip(text, limit):
    text = _require_masked(text)
    if len(text) <= limit:
        return text
    if limit < 3:
        return Masked("…"[:limit])
    return Masked(_head(text, limit - 2).rstrip() + " …")


def _render_quote(inner, before):
    """One quote as it is shown: a block when it holds lines or starts a line, else inline."""
    inner = safe_text(inner, 10 ** 9, False)  # again, idempotent
    lines = inner.splitlines() or [""]
    if len(lines) == 1 and before and not before.endswith("\n"):
        return _join_masked(["«цитата волны: ", lines[0], "»"])
    head = ("\n" if before and not before.endswith("\n") else "") + "Цитата волны:"
    # the cap applies to the quote AS SHOWN (the head and the `> ` of every line count): hundreds of
    # short lines must not swell it and push the dispatcher's own words after it out of the message
    shown, used = [], len(head)
    for i, l in enumerate(lines):
        row = f"> {l}"
        if used + 1 + len(row) > TG_LIMIT - 4:
            room = TG_LIMIT - 4 - used - 1
            shown.append(_clip(row, room) if room > 8 and not shown else "> …")
            break
        shown.append(row)
        used += 1 + len(row)
    return _join_masked([head, _join_masked(shown, "\n")], "\n")


def render_notice(text, limit=TG_MESSAGE_LIMIT):
    """The text of a notice as it leaves: the dispatcher's wording is redacted as before (owner-script
    exemption included), every quote is cleaned, redacted without the exemption and marked. A notice
    without markers (an older entry of the outbox) is redacted as a whole, as before."""
    out, pos = [], 0
    for m in _QUOTE_SPAN.finditer(text):
        out.append(safe_text(text[pos:m.start()], 10 ** 9, True))
        out.append(_render_quote(m.group(1), "".join(out)))
        pos = m.end()
    out.append(safe_text(text[pos:], 10 ** 9, True))
    return _clip(_join_masked(out), limit)  # NOT "".join: a plain str would be masked again without owner_paths


def _first_line(text, limit):
    """The first non-empty line of a rendered notice, capped (display-message, ATTENTION, events)."""
    lines = [l for l in render_notice(text, 10 ** 9).splitlines() if l.strip()]  # Masked lines
    if not lines:
        return _join_masked([])
    if lines[0].strip() == "Цитата волны:" and len(lines) > 1:
        # the heading alone says nothing in ATTENTION / display-message: add the quote's first line
        return _clip(_join_masked([lines[0].strip(), " ", lines[1].lstrip("> ").strip()]), limit)
    return _clip(lines[0], limit)


AZ_TIMEOUT = 30  # seconds: a hung `az` must not block the watch loop


def _secret(vault, name):
    return sh("az", "keyvault", "secret", "show", "--vault-name", vault, "--name", name,
              "--query", "value", "-o", "tsv", timeout=AZ_TIMEOUT).stdout.strip()


def _send_telegram(cfg, text):
    """Transport. Vault and secret names come from chain.json `telegram`; nothing about the
    host is built in. The HTTP call is made from Python (urllib), not from a shell pipeline."""
    tg = cfg["telegram"]
    key = (tg["keyvault"], tg["token_secret"], tg["chat_secret"])
    if key not in _tg:
        _tg[key] = (_secret(tg["keyvault"], tg["token_secret"]), _secret(tg["keyvault"], tg["chat_secret"]))
    token, chat = _tg[key]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20):
            pass
    except BaseException:
        # the secret may have been rotated in Key Vault: the next attempt (an outbox retry) reads it
        # again instead of failing with the cached value forever
        _tg.pop(key, None)
        raise


def telegram_configured(cfg):
    tg = cfg.get("telegram")
    return isinstance(tg, dict) and all(tg.get(k) for k in ("keyvault", "token_secret", "chat_secret"))


DISPLAY_LIMIT = 150  # characters of a display-message line


def display_all(text):
    """Show `text` in the status line of every client of the waves' tmux server (the same socket
    as the windows: WAB_TMUX_SOCKET or the default). Raises on a tmux failure; the caller logs it."""
    clients = tmux("list-clients", "-F", "#{client_name}").stdout.split()
    safe = text.replace("#", "##")  # display-message takes a FORMAT: «#(cmd)» would run a shell command
    for client in clients:
        tmux("display-message", "-d", "0", "-c", client, safe)


class Notice(str):
    """The text of an external notice (Telegram, display-message, ATTENTION): made ONLY by notice_text() from a
    template and the fields of the registry (#83), or by manual_notice() for the owner's own `wab.py notify`.
    notify() takes nothing else: a free text of a wave cannot reach the transport by a call that forgot the
    template. The wave's quote, when chain.json allows it (`telegram_quote: true`), is inside in its markers."""
    __slots__ = ()


# ---------- the external notices: one registry of fields, one table of templates, one function (#83) ----------
#
# THE invariant of every text that leaves the machine (Telegram, display-message, ATTENTION): it is the product of a
# template of NOTICE_TEMPLATES filled with fields of NOTICE_FIELDS and nothing else. A value that fails its check is
# «?» (names, numbers, time) or a neutral phrase (the vocabularies below), never the raw value. The free text of a
# wave (status, result.md, a question, a reason, a command, a path) is not in it, unless chain.json has
# `telegram_quote: true`: then the FIRST line of the wave's question is added through quote(). The local outputs
# (dashboard, `wab.py status`, chain-result.md, events.log) show the wave's text through safe_text, as before.
NOTICE_ACTION = "Нужно: "  # the one action line of a notice that needs a human
NOTICE_HEAD_LIMIT = 120
COUNT_LIMIT = 10 ** 9

# the vocabularies: a closed set -> words for a human; an unknown value is a neutral phrase, never the raw key
CLASS_WORDS = {"needs_decision": "нужно решение", "blocked_cap": "кончились попытки исправить",
               "plan_mismatch": "расхождение с планом", "question": "вопрос волны",
               "merge_gate": "гейт мерджа"}
CLASS_OTHER = "другой вопрос"
ANSWER_WORDS = {"invariant": "исправить целиком", "new_run": "ещё одна попытка",
                "accept_limitation": "принять как есть", "cut_surface": "вырезать поверхность находки",
                "owner": "решает владелец"}
VARIANT_OTHER = "другой вариант"
STATUS_WORDS = {"STARTING": "запускается", "RUNNING": "работает", "HANDOFF_READY": "готова передать контекст",
                "BLOCKED": "ждёт ответа", "DONE": "завершена"}
STATUS_OTHER = "другой статус"
SEVERITY_WORDS = {"low": "низкая важность", "medium": "средняя важность", "high": "высокая важность",
                  "P0": "критично (P0)", "P1": "очень важно (P1)", "P2": "важно (P2)", "P3": "не срочно (P3)"}
SEVERITY_OTHER = "замечания"
REASON_WORDS = {
    # why a notice is sent (the dispatcher's own codes, never a text of a wave)
    "plan_pin": "план waves.json изменился после одобрения",
    "window_not_ready": "окно Claude не стало готовым за 90 секунд",
    "no_next_absent": "нет файла next-prompt.md",
    "no_next_unusable": "next-prompt.md непригоден",
    "merge_refused": "мердж отклонён",
    "ready_refused": "перевод PR в ready отклонён",
    "still_draft": "PR остался draft после перевода в ready",
    "no_gate_record": "в записи волны нет номера PR или коммита гейта",
    "wrong_base": "PR смержен не в ту ветку",
    "other_head": "PR смержен с другим коммитом, чем проверял гейт",
    "closed": "PR закрыт без мерджа",
    "policy_refused": "автоответ по политике не отправлен: проверка отказала",
    "gate_pass": "гейт мерджа пройден",
    "gate_threads": "гейт мерджа пройден, но есть незакрытые треды ревью",
    "gate_wait": "гейт мерджа ждёт",
    "gate_fail": "гейт мерджа не пройден",
    "gate_unchecked": "гейт мерджа не проверен",
    "gate_none": "гейт мерджа не включён",
    "result_written": "chain-result.md записан в каталоге прогона",
    "result_not_written": "chain-result.md не записан",
    # the action of the dispatcher in the window (the label of _deliver / _act)
    "first_prompt": "отправка первой задачи",
    "checkpoint_request": "запрос передачи контекста",
    "clear": "очистка контекста командой /clear",
    "resume": "передача продолжения /superarmanda --resume",
    "policy_answer": "автоответ по политике",
    "gate_failure": "сообщение о непройденном гейте",
    "idle_nudge": "толчок молчащей волны",
    "alarm": "будильник волны",
}
REASON_OTHER = "причина записана в журнале событий"
# the labels of _deliver / _act -> the code of REASON_WORDS (a prefix of the label, « (Enter)» cut)
ACTION_REASONS = (("first prompt", "first_prompt"), ("checkpoint request", "checkpoint_request"),
                  ("/clear", "clear"), ("/superarmanda --resume", "resume"), ("policy answer", "policy_answer"),
                  ("gate failure", "gate_failure"), ("idle nudge", "idle_nudge"), ("alarm", "alarm"))


def action_reason(what):
    """The code of REASON_WORDS of a label of a tmux action (`_deliver`/`_act`); `other` when it is not a known one."""
    what = str(what)
    for prefix, code in ACTION_REASONS:
        if what.startswith(prefix):
            return code
    return "other"


def _word(table, other):
    """The formatter of a vocabulary field: a key of the closed table -> its words, anything else -> `other`."""
    def fmt(cfg, value):
        return table.get(value, other) if isinstance(value, str) else other
    return fmt


def _name_field(rx):
    def fmt(cfg, value):
        return value if isinstance(value, str) and rx.fullmatch(value) and set(value) != {"."} else "?"
    return fmt


def _count_field(low=0):
    def fmt(cfg, value):
        ok = isinstance(value, int) and not isinstance(value, bool) and low <= value < COUNT_LIMIT
        return str(value) if ok else "?"
    return fmt


def _time_field(cfg, value):
    ts = num(value)
    return hum(cfg, ts) if ts is not None and 0 < ts < 4_102_444_800 else "?"


def _duration_field(cfg, value):
    return "?" if isinstance(value, bool) else _human_dur(value)


def _status_field(cfg, value):
    """A status of the wave (or its line `BLOCKED: ...`): only the word before the colon, only from the table."""
    word = str(value).split(":", 1)[0].strip() if isinstance(value, str) else ""
    return STATUS_WORDS.get(word, STATUS_OTHER)


def severity_word(value):
    """A severity (a finding's `severity|priority`, dash.finding_counts keeps the raw key) for an external text."""
    return SEVERITY_WORDS.get(value, SEVERITY_OTHER) if isinstance(value, str) else SEVERITY_OTHER


def _variant_field(cfg, value):
    """The variant of an answer: the closed words of ANSWER_WORDS, or a one-letter recommendation (A, B, ...)."""
    if isinstance(value, str):
        if value in ANSWER_WORDS:
            return ANSWER_WORDS[value]
        if re.fullmatch(r"[A-Z]", value):
            return f"вариант {value}"
    return VARIANT_OTHER


def _yes_no_field(cfg, value):
    return {True: "да", False: "нет"}.get(value, "?") if isinstance(value, bool) else "?"


def _question_field(cfg, value):
    """The FIRST line of the wave's question as a quote, and only with `telegram_quote: true`; else nothing."""
    if cfg.get("telegram_quote") is not True or not isinstance(value, str):
        return ""
    lines = [l for l in safe_text(value, 10 ** 9, False).splitlines() if l.strip()]
    return quote(lines[0], TG_LIMIT) if lines else ""


# field -> (what it is, the check/formatter: (cfg, value) -> the text that goes in). The ONLY things a template may use.
NOTICE_FIELDS = {
    "chain": ("имя цепочки как в chain.json", _name_field(NAME)),
    "wave": ("id волны как в chain.json", _name_field(WAVE_NAME)),
    "next_wave": ("id следующей волны как в chain.json", _name_field(WAVE_NAME)),
    "tmux": ("имя tmux-сессии волны (tmux_prefix + id волны)", _name_field(PREFIX)),
    "event": ("ключ уведомления из таблицы эпизодов",
              lambda cfg, v: v if isinstance(v, str) and v in NOTICE_EPISODE_ENDS else "?"),
    "pr": ("номер PR", _count_field(1)),
    "n": ("счётчик или число минут", _count_field()),
    "threads": ("число незакрытых тредов ревью", _count_field()),
    "p01": ("число из них с P0/P1 из прошлых коммитов", _count_field()),
    "attempts": ("число попыток", _count_field()),
    "runs": ("число прогонов superarmanda", _count_field()),
    "waves": ("число волн цепочки", _count_field()),
    "prs": ("число PR цепочки", _count_field()),
    "restarts": ("число перезапусков", _count_field()),
    "questions": ("число вопросов к владельцу", _count_field()),
    "time": ("момент времени, показывается по поясу владельца", _time_field),
    "duration": ("длительность", _duration_field),
    "cls": ("класс вопроса волны, словарь CLASS_WORDS", _word(CLASS_WORDS, CLASS_OTHER)),
    "variant": ("вариант ответа, словарь ANSWER_WORDS или буква", _variant_field),
    "wave_status": ("статус волны, словарь STATUS_WORDS", _status_field),
    "severity": ("важность находки, словарь SEVERITY_WORDS", lambda cfg, v: severity_word(v)),
    "reason": ("причина, словарь REASON_WORDS", _word(REASON_WORDS, REASON_OTHER)),
    "red": ("красная зона: да или нет", _yes_no_field),
    "question": ("первая строка вопроса волны, только с telegram_quote", _question_field),
}


def _tpl(head, notes=(), action=None, fields=()):
    names = ("chain", "wave", "tmux", *fields) if action else ("chain", "wave", *fields)
    return {"head": head, "notes": tuple(notes), "action": action, "fields": names}


_ASK = ("question",)  # a template that shows the wave's question when telegram_quote allows it
NOTICE_TEMPLATES = {
    "started": _tpl("🚀 {chain}: стартовала волна {wave}"),
    "not_ready": _tpl("⛔ {chain}: волна {wave} не запущена", ("Причина: {reason}. Цепочка стоит.",),
                      "устрани причину и запусти волну заново (wab.py launch).", ("reason", *_ASK)),
    "no_prompt": _tpl("❗ {chain}: волна {wave} запущена, но задачи для неё нет",
                      ("Файла с задачей уже нет, задачу не отправил.",),
                      "посмотри окно волны и пришли задачу сам."),
    "sending": _tpl("❗ {chain}: после перезапуска диспетчера неизвестно, дошла ли задача волне {wave}",
                    ("Повторно задачу не слал.",), "проверь окно волны."),
    "updating": _tpl("❗ {chain}: после перезапуска диспетчера неизвестно, дошло ли продолжение волне {wave}",
                     ("Команду /superarmanda --resume повторно не слал.",), "проверь окно волны."),
    "checkpoint_timeout": _tpl("❗ {chain}: волна {wave} не записала handoff вовремя",
                               ("Прошло {n} мин после запроса.",), "посмотри окно волны.", ("n",)),
    "tmux_failed": _tpl("❗ {chain}: волна {wave}: не удалось выполнить действие в окне",
                        ("Что не вышло: {reason}. Окно живо.",), "посмотри окно волны, диспетчер повторит попытку.",
                        ("reason",)),
    "unverified_enter": _tpl("❗ {chain}: волна {wave}: Enter нажат без проверки отправки",
                             ("Действие: {reason}. Состояние старой версии без начала текста.",),
                             "проверь в окне волны, дошёл ли текст.", ("reason",)),
    "dead": _tpl("⛔ {chain}: окно волны {wave} закрылось", ("Статус волны: {wave_status}. Цепочка стоит.",),
                 "узнай, что случилось, и перезапусти волну.", ("wave_status", *_ASK)),
    "blocked": _tpl("⏸ {chain}: волна {wave} ждёт тебя",
                    ("Тип вопроса: {cls}; красная зона: {red}; рекомендация волны: {variant}.",
                     "Автоответ по политике не отправлен: {reason}."),
                    "ответь волне в её окне или текстом командой say.", ("cls", "red", "variant", "reason", *_ASK)),
    "policy_answer": _tpl("🤖 {chain}: волна {wave}: развилка закрыта по политике",
                          ("Тип вопроса: {cls}; выбрано: {variant}. Ответ не нужен.",),
                          None, ("cls", "variant", *_ASK)),
    "permission": _tpl("🔐 {chain}: волна {wave} ждёт подтверждения на экране", (),
                       "подтверди или отклони запрос в окне волны."),
    "idle": _tpl("💤 {chain}: волна {wave} молчит {n}+ мин",
                 ("Молчит с {time}; статус волны: {wave_status}. Возможно, ждёт тебя.",),
                 "посмотри окно волны.", ("n", "time", "wave_status", *_ASK)),
    "auto_off": _tpl("❗ {chain}: волна {wave} вышла из режима auto", ("Теперь она будет спрашивать подтверждения.",),
                     "верни режим: в окне Shift+Tab до «auto mode on»."),
    "handoff": _tpl("📬 {chain}: волна {wave} сдала PR", ("Гейт: {reason}.", "Мердж и следующий шаг — за координатором."),
                    "смержи PR и запусти следующую волну (или заверши цепочку командой done).", ("reason", *_ASK)),
    "merge_owner": _tpl("🔀 {chain}: волна {wave}: гейт пройден, но есть незакрытые треды",
                        ("PR #{pr}, незакрытых тредов: {threads}, из прошлых коммитов с P0/P1: {p01}. Сам не мержу.",
                         "Скрипт заново проверит гейт; draft PR переведёт в ready и остановится — запусти его ещё раз."),
                        "выполни скрипт мерджа (путь к нему — в events.log и в wab.py status).",
                        ("pr", "threads", "p01")),
    "merge_refused": _tpl("❗ {chain}: волна {wave}: гейт пройден, но мердж не выполнен",
                          ("PR #{pr}: {reason}.",), "выполни скрипт мерджа (путь к нему — в events.log и в wab.py status).",
                          ("pr", "reason", *_ASK)),
    "merge_unknown": _tpl("❗ {chain}: волна {wave}: результат мерджа неизвестен",
                          ("PR #{pr}: диспетчер прерывался, сам повторно не мержу.",),
                          "проверь PR и при необходимости выполни скрипт мерджа (путь — в events.log).", ("pr",)),
    "merge_stopped": _tpl("⛔ {chain}: волна {wave}: мердж остановлен", ("Причина: {reason}. Следующую волну не запускаю.",),
                          "проверь PR и реши сам.", ("reason", *_ASK)),
    "done": _tpl("✅ {chain}: волна {wave} завершена", (), None, _ASK),
    "no_next": _tpl("⛔ {chain}: волна {wave} готова, следующую не запускаю", ("Причина: {reason}.",),
                    "реши, что делать со следующей волной.", ("reason", *_ASK)),
    "launch_refused": _tpl("⛔ {chain}: волна {wave} готова, {next_wave} не запущена",
                           ("Цепочка стоит; причина записана в журнале событий.",),
                           "устрани причину и запусти следующую волну командой launch.", ("next_wave", *_ASK)),
    "chain_done": _tpl("🏁 {chain}: цепочка завершена, последняя волна {wave}",
                       ("Волн: {waves}, PR: {prs}, перезапусков: {restarts}, вопросов к владельцу: {questions}, "
                        "время: {duration}.", "Итог: {reason}."),
                       None, ("waves", "prs", "restarts", "questions", "duration", "reason")),
}


def external_attach(name):
    """`tmux attach -t <name>` for a human, or "?" when the name is not of the registry's format. A socket is shown
    only as a plain `-L <name>`: a path of a socket (`-S`) never goes out of the machine."""
    if _name_field(PREFIX)(None, name) == "?":
        return "?"
    sock = TMUX_SOCKET or os.environ.get("WAB_TMUX_SOCKET")
    flags = f" -L {sock}" if sock and NAME.fullmatch(sock) and set(sock) != {"."} else ""
    return f"tmux{flags} attach -t {name}"


def _notice_values(cfg, wave, tpl, fields):
    given = dict(fields, chain=cfg.get("chain"), wave=wave)
    if "tmux" in tpl["fields"] and given.get("tmux") is None:
        given["tmux"] = f"{cfg.get('tmux_prefix') or 'wab-'}{str(wave).lower()}"
    return {n: NOTICE_FIELDS[n][1](cfg, given.get(n)) for n in tpl["fields"]}, given


def notice_text(cfg, wave, key, now=None, **fields):
    """THE one maker of an external text: the template of `key` filled with `fields` of NOTICE_FIELDS (a name outside
    the registry, or outside this template's field list, is a ValueError: a programming error), every value checked
    by its formatter. Lines: the first (emoji, what happened, chain and wave, <= 120), 0-2 explanations, the wave's
    first question line (telegram_quote only), `Время: ...` of the owner's zone, and for a notice that needs a human
    exactly one action line and, LAST, `tmux attach -t <session>`. A note whose fields were not given is left out."""
    tpl = NOTICE_TEMPLATES.get(key)
    if tpl is None:
        raise ValueError(f"notice_text: key {key!r} has no template in NOTICE_TEMPLATES")
    for name in fields:
        if name not in NOTICE_FIELDS:
            raise ValueError(f"notice_text: field {name!r} is not in NOTICE_FIELDS")
        if name not in tpl["fields"]:
            raise ValueError(f"notice_text: field {name!r} is not a field of the template {key!r}")
    values, given = _notice_values(cfg, wave, tpl, fields)
    head = _clip(safe_text(tpl["head"].format(**values), 10 ** 9, True), NOTICE_HEAD_LIMIT)
    lines = [str(head)]
    for note in tpl["notes"]:
        used = {f for _, f, _, _ in string.Formatter().parse(note) if f}
        if all(given.get(n) is not None for n in used - {"chain", "wave"}):
            lines.append(note.format(**values))
    if values.get("question"):
        lines.append(values["question"])
    lines.append(notice_stamp(cfg, time.time() if now is None else now))
    if tpl["action"]:
        lines.append(NOTICE_ACTION + tpl["action"].format(**values))
        lines.append(external_attach(values["tmux"]))
    return Notice("\n".join(lines))


def manual_notice(cfg, text):
    """`wab.py notify <chain.json> <text>`: the owner's OWN text typed in the command line (the one place where a
    notice is not a template; text that does not come from the owner never takes this way). Masked by safe_text."""
    head = _clip(safe_text(f"📣 {cfg.get('chain') or 'wave-autobot'}: {text}", 10 ** 9, False), TG_LIMIT)
    return Notice(f"{head}\n{notice_stamp(cfg, time.time())}")


def notify(cfg, notice, wave=None):
    """The transport of a notice (Telegram, else display-message in tmux). Takes a Notice only: the text is the
    product of a template (notice_text), not a string somebody composed."""
    if not isinstance(notice, Notice):
        raise TypeError("notify() takes a Notice made by notice_text(): an external text is a template's product")
    first = _first_line(notice, 120)
    if not telegram_configured(cfg):
        event(cfg, f"notify(skipped): {first}")
        # no transport: a local signal on the waves' tmux server instead (ATTENTION is written by
        # flush_notices, which knows the episode). One call per notice, so a failure is one event
        # per episode; it never stops the tick.
        stamp = notice_stamp(cfg, time.time())
        short = _clip(_first_line(notice, DISPLAY_LIMIT), DISPLAY_LIMIT - len(stamp) - 3) + f" · {stamp}"
        try:
            display_all(short)
        except (subprocess.CalledProcessError, OSError) as e:
            event(cfg, f"display-message failed ({type(e).__name__}): {_clip(first, 80)}")
        return True  # Telegram is not configured: there is nothing to repeat
    try:
        _send_telegram(cfg, render_notice(notice, TG_MESSAGE_LIMIT))
        event(cfg, f"telegram: {first}")
        return True
    except Exception as e:  # notification must never stop supervision
        event(cfg, f"telegram FAILED ({type(e).__name__}): {_clip(first, 80)}")
        return False


NOTIFY_RETRY_SECONDS = 300  # a standing notice that failed is repeated no more often than this


def _stored_fields(fields):
    """The fields as the outbox keeps them: JSON scalars only (a value of a registry field is one)."""
    return {k: v for k, v in fields.items() if v is None or isinstance(v, (str, int, float, bool))}


def put_notice(cfg, w, wave, key, value, **fields):
    """Put a notice about a standing episode (`key`, `value`) into the wave's outbox, IN MEMORY only.
    What is kept is the template's key and the registry's FIELDS (notice_text builds the text from them when it is
    sent, with the time of the sending); the wave's own question is kept only when chain.json has
    `telegram_quote: true`, otherwise it is not put anywhere. A key outside NOTICE_EPISODE_ENDS /
    NOTICE_TEMPLATES, or a field outside the registry or the template, is a ValueError.
    `key` is a literal with a row in NOTICE_EPISODE_ENDS (the end of its episode): once the
    episode is over the notice is stale and leaves the outbox undelivered (key -> end:
    started/tmux_failed -> window gone; permission/idle/auto_off -> window gone or their screen
    end (SCREEN_EPISODE_ENDS); not_ready, no_prompt,
    checkpoint_timeout, dead, handoff -> their phase is left; sending -> the wave wrote a status;
    updating -> the new session is bound (a Fable switch: its step left `unconfirmed`); blocked -> status not BLOCKED; done/no_next/
    launch_refused -> ack only (`done` is information: it opens no ATTENTION, INFO_NOTICES);
    chain_done -> never).
    The caller saves it with the SAME save_state as the change it reports (the phase, the
    once_per mark), then calls flush_notices: a process killed right after that save still owes
    the notice and the next watch/done sends it, while a save in between would lose it for good.
    The notice is removed only after the transport took it, so a transient Telegram failure is
    retried by later ticks instead of being lost; supervision never waits for it. At least once,
    not exactly once: a crash or a lost transport reply after the actual delivery sends it again."""
    if key not in NOTICE_EPISODE_ENDS:  # a programming error: the notice would never become stale
        raise ValueError(f"put_notice: key {key!r} has no row in NOTICE_EPISODE_ENDS")
    tpl = NOTICE_TEMPLATES.get(key)
    if tpl is None:
        raise ValueError(f"put_notice: key {key!r} has no template in NOTICE_TEMPLATES")
    if "tmux" in tpl["fields"] and "tmux" not in fields and isinstance(w.get("tmux"), str):
        fields["tmux"] = w["tmux"]
    if cfg.get("telegram_quote") is not True:
        fields.pop("question", None)  # no flag: the wave's text is not put into the outbox at all
    stored = _stored_fields(fields)
    text = str(notice_text(cfg, wave, key, **stored))  # also refuses a field outside the registry / the template
    box = w.setdefault("outbox", {})
    cur = box.get(key)
    if cur is None or cur.get("value") != value:
        box[key] = {"value": value, "fields": stored, "text": text, "next_at": 0}


def outbox_notice(cfg, wave, w, key, item):
    """The notice of an outbox entry as it is sent now: its template + its stored fields. An entry of an older
    dispatcher (no `fields`; its text has the wave's quote) is made again by the template of its key from the fields
    of the registry that are still known (chain, wave, session): its free text is dropped."""
    stored = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    tpl = NOTICE_TEMPLATES[key]
    fields = {k: v for k, v in stored.items() if k in tpl["fields"] and k in NOTICE_FIELDS}
    if "tmux" in tpl["fields"] and isinstance(w.get("tmux"), str):
        fields["tmux"] = w["tmux"]
    if cfg.get("telegram_quote") is not True:
        fields.pop("question", None)
    return notice_text(cfg, wave, key, **fields)


def drop_notice(w, key):
    """The episode is over: forget it, and a notice not yet delivered is stale."""
    notified, box, att = w.get("notified"), w.get("outbox"), w.get("attention")
    if isinstance(notified, dict):
        notified.pop(key, None)
    if isinstance(att, dict):
        att.pop(key, None)
        if not att:
            w.pop("attention", None)
    if isinstance(box, dict):
        box.pop(key, None)
        if not box:
            w.pop("outbox", None)


def drop_attention(w, key):
    """Close one open local signal (ATTENTION) of the wave and nothing else: the outbox and the
    «already notified» mark stay (unlike drop_notice), so the notice is neither lost nor repeated."""
    att = w.get("attention")
    if isinstance(att, dict):
        att.pop(key, None)
        if not att:
            w.pop("attention", None)


WINDOW_GONE = ("dead", "done", "awaiting_merge")  # no live window of this wave to look at


def _gone(w):
    return w.get("phase") in WINDOW_GONE


def _switch_unconfirmed(w):
    """A Fable switch (#108) whose resume message may or may not have reached the new session."""
    sw = w.get("fable_switch")
    return w.get("phase") == "switching" and isinstance(sw, dict) and sw.get("step") == "unconfirmed"


def _status(w):
    return str(w.get("last_status") or "")


def _status_stamp(cfg, wave):
    """Identity of the status file's current content write: [inode, mtime_ns], or None."""
    try:
        s = os.stat(wave_dir(cfg, wave) / "status")
    except OSError:
        return None
    return [s.st_ino, s.st_mtime_ns]


OWNER_ANSWERED = "owner-answered"


def write_owner_answered(cfg, wave):
    """`wab.py say` marks the episode it answered: `<run_dir>/<wave>/owner-answered` = {status, stamp,
    at}, written atomically (tmp in the same directory, fsync, os.replace, fsync of the directory).
    The marker is not state.json: `say` takes no run lock and never writes the state. Called under the
    window's input lock, when the owner's text is already in the window."""
    wdir = wave_dir(cfg, wave)
    # one snapshot: stamp -> text -> stamp. A rewrite in between would glue the old text to the new stamp and
    # mute the policy on a NEW episode; then the stamp is None (matches nothing: the error is toward «new episode»)
    stamp = _status_stamp(cfg, wave)
    text = read(wdir / "status", on_error=None)
    if _status_stamp(cfg, wave) != stamp:
        stamp = None
        event(cfg, f"{wave}: say: status rewritten while the marker was taken; the marker answers no episode")
    doc = {"status": text, "stamp": stamp, "at": _utc(time.time())}
    fd, tmp = tempfile.mkstemp(dir=wdir, prefix=".owner-answered.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, wdir / OWNER_ANSWERED)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(wdir, os.O_RDONLY)  # binary fd, "rb"
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def owner_answered(cfg, wave, status):
    """True when the owner answered THIS episode through `say`: the marker holds the same status line AND
    the same stamp of the status file as now (a rewrite of the file, even with the same line, is a new
    episode). A missing, unreadable or corrupt marker is no marker."""
    try:
        doc = json.loads((wave_dir(cfg, wave) / OWNER_ANSWERED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.get("status") != status:
        return False
    stamp = doc.get("stamp")
    return isinstance(stamp, list) and stamp == _status_stamp(cfg, wave)


def _answered(w):
    """The BLOCKED line the dispatcher answered by the decision policy (the episode's mark), or None."""
    notified = w.get("notified")
    return notified.get("policy_answer") if isinstance(notified, dict) else None


# Every notice is about an episode, and it is stale once the episode is over. Key -> the end of
# its episode as a predicate on the wave record (applied by drop_ended_episodes on EVERY save and
# before every send), None = ended only by the coordinator's confirmation (ack_wave_notices).
# Ends that are not in the record but on the screen are in SCREEN_EPISODE_ENDS (listed after `#`).
# A new put_notice key without a row here fails the completeness test.
#   started             window gone / wave finished
#   not_ready           the phase left not_ready (a relaunch); + ack
#   no_prompt           the phase left starting (the task was sent after all)
#   sending             the wave wrote its own status (the task arrived), or window gone
#   updating            the new session is bound by its marker (the resume message arrived), or window gone
#   checkpoint_timeout  the phase left checkpoint (HANDOFF_READY taken, /clear + resume message done)
#   tmux_failed         window gone   # + the next successful action (_act)
#   dead                the phase left dead (the window is back); + ack on a relaunch
#   blocked             the status is no longer BLOCKED, or window gone
#   policy_answer       the status is no longer the answered BLOCKED line, or window gone
#   permission, idle, auto_off   window gone   # + SCREEN_EPISODE_ENDS: the prompt left the screen /
#                                              #   the pane moved / auto mode is back
#   handoff             the phase left awaiting_merge (the coordinator confirmed it); + ack
#   merge_owner, merge_refused, merge_unknown   the phase left merging (the PR is merged, or the wave goes on)
#   merge_stopped       the phase left awaiting_merge (the coordinator took over); + ack
#   done, no_next, launch_refused   None: only the coordinator's confirmation (ack); `done` is told only
#                       (INFO_NOTICES: it opens no ATTENTION), no_next / launch_refused ask for a decision
#   chain_done          None and kept even on ack: the end of the chain must get through
NOTICE_EPISODE_ENDS = {
    "started": _gone,
    "not_ready": lambda w: w.get("phase") != "not_ready",
    "no_prompt": lambda w: w.get("phase") != "starting",
    "sending": lambda w: _gone(w) or _status(w) not in ("", "STARTING"),
    "updating": lambda w: _gone(w) or not (w.get("await_session") or _switch_unconfirmed(w)),
    "checkpoint_timeout": lambda w: w.get("phase") != "checkpoint",
    "tmux_failed": _gone,
    "unverified_enter": _gone,  # a delivery of an older-version state pressed Enter unverified
    "dead": lambda w: w.get("phase") != "dead",
    "blocked": lambda w: _gone(w) or not _status(w).startswith("BLOCKED"),
    "policy_answer": lambda w: _gone(w) or _status(w) != _answered(w),
    "permission": _gone,
    "idle": _gone,
    "auto_off": _gone,
    "handoff": lambda w: w.get("phase") != "awaiting_merge",
    "merge_owner": lambda w: w.get("phase") != "merging",
    "merge_refused": lambda w: w.get("phase") != "merging",
    "merge_unknown": lambda w: w.get("phase") != "merging",
    "merge_stopped": lambda w: w.get("phase") != "awaiting_merge",
    "done": None,
    "no_next": None,
    "launch_refused": None,
    "chain_done": None,
}


def drop_ended_episodes(w):
    """Apply NOTICE_EPISODE_ENDS to one wave record: an ended episode loses its undelivered
    notice and its «already notified» mark (the next episode notifies again). True if the
    outbox changed. Called by save_state, so it lands in the same save as the transition."""
    if not isinstance(w, dict):
        return False
    box = w.get("outbox")
    before = set(box) if isinstance(box, dict) else set()
    for key, ended in NOTICE_EPISODE_ENDS.items():
        if ended is not None and ended(w):
            drop_notice(w, key)
    box = w.get("outbox")
    return before != (set(box) if isinstance(box, dict) else set())


def pane_digest(txt):
    """Idleness is judged without the last two non-empty lines (spinner, timer, footer)."""
    body = [l for l in txt.splitlines() if l.strip()][:-2]
    return hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()


# Episodes whose end is seen on the screen of a live window: key -> predicate on (record, screen),
# screen = {"txt", "digest", "auto_off"}. Applied by end_screen_episodes on EVERY tick with a live
# window, before any branch of the tick returns (BLOCKED, updating, clearing, checkpoint...) and
# before the outbox is sent: a notice stuck in the outbox must not go out once its condition left
# the screen, whatever the wave does meanwhile. The window being gone ends them via NOTICE_EPISODE_ENDS.
SCREEN_EPISODE_ENDS = {
    "permission": lambda w, s: not any(m in s["txt"] for m in PERMISSION_MARKERS),
    "idle": lambda w, s: s["digest"] != w.get("pane_digest"),
    "auto_off": lambda w, s: s["auto_off"] is False,
}


def end_screen_episodes(w, txt, now):
    """The one place where screen episodes end: drop the ended ones (notice and mark), then move
    the screen bookkeeping on (the idle clock restarts on a new digest, auto mode back resets the
    off counter). In memory only: the caller's next save persists it. Returns the screen."""
    screen = {"txt": txt, "digest": pane_digest(txt), "auto_off": auto_mode_off(txt)}
    for key, ended in SCREEN_EPISODE_ENDS.items():
        if ended(w, screen):
            drop_notice(w, key)
    if screen["digest"] != w.get("pane_digest"):
        w["pane_digest"], w["pane_changed"] = screen["digest"], now
    if screen["auto_off"] is False:
        w["auto_off_ticks"] = 0
        w["auto_alerted"] = False
    return screen


def note_transcript_activity(w, now, idle_s=0):
    """A still screen over a working subagent is not silence: a NEW write to any transcript file of
    the session counts as activity at the moment it is seen and ends an idle episode already
    reported, so the next silence is reported again. The first look at a session (a new session
    after /clear, or a record from a version without this bookkeeping) only seeds the baseline from
    the files' own mtimes (a future one clamped); a write later than the moment an idle notice could
    have been raised (`pane_changed` + `idle_s`) makes that notice stale. Runs next to
    end_screen_episodes, before any branch of the tick returns. In memory only; True when the
    record changed (the caller saves at once: some branches return without saving)."""
    files = transcript_activity(w)
    sessions = w.get("sessions") or []
    sid = sessions[-1] if sessions else None
    seen = w.get("activity_files")
    first = not isinstance(seen, dict) or w.get("activity_session") != sid
    before = (seen, w.get("activity_session"), w.get("activity_at"), "idle" in (w.get("notified") or {}))
    if first:
        if files:
            latest = max(files.values())
            w["activity_at"] = max(w.get("activity_at") or 0, min(latest, now))
            # only a real past write proves activity after the old notice; a future mtime does not,
            # and it must not hide a real write to another file either: each file is checked
            after = (w.get("pane_changed") or now) + idle_s
            if any(after < m <= now for m in files.values()):
                drop_notice(w, "idle")
    elif any(seen.get(f) != m for f, m in files.items()):  # a change seen now is activity now
        drop_notice(w, "idle")
        w["activity_at"] = now
    w["activity_files"], w["activity_session"] = files, sid
    return before != (files, sid, w.get("activity_at"), "idle" in (w.get("notified") or {}))


KEEP_ON_ACK = ("chain_done",)  # the end of the chain is never stale: it must get through


def ack_wave_notices(w):
    """The coordinator confirmed this wave (launched the next one, restarted this one, ran `done`):
    every undelivered notice about its past state (handoff, no_next, dead, not_ready, blocked,
    idle, permission, ...) is stale now and leaves the outbox; only «цепочка завершена» stays.
    The one rule for every confirmation point; notices of other waves are not touched.
    Every open local signal of the wave (ATTENTION) is closed as well."""
    w.pop("attention", None)
    box = w.get("outbox")
    if not isinstance(box, dict):
        w.pop("outbox", None)
        return
    for key in list(box):
        if key not in KEEP_ON_ACK:
            box.pop(key, None)
    if not box:
        w.pop("outbox", None)


# Notices that ask nothing of anybody: shown by display-message, but they open no ATTENTION signal
# (`started` lasts as long as the window, `chain_done` forever: `attention` would never go quiet).
# `done` is the same: the dispatcher launches the next wave itself (no ack then), so its episode would
# end only with the coordinator's confirmation and ATTENTION would stay open after a normal DONE (#55).
# The undelivered `done` still waits in the outbox until the coordinator's ack, as before.
INFO_NOTICES = ("started", "chain_done", "policy_answer", "done")  # policy_answer: told, nothing to answer


def note_attention(w, key, notice, now):
    """A notice went out only locally (no Telegram): an open signal of its episode, IN MEMORY; the
    caller's save writes ATTENTION (sync_attention). drop_notice and ack_wave_notices close it. What is kept is the
    first line of the TEMPLATE's text; attention_text() makes the signal again from the notice's key, so a line of
    an older dispatcher (with a wave's quote) is never shown."""
    w.setdefault("attention", {})[key] = {"at": now, "line": _first_line(notice, 300)}


def attention_path(cfg):
    return cfg["run_dir"] / "ATTENTION"


def attention_signal(cfg, wave, key):
    """The signal of ATTENTION: the first line of the notice's template (chain, wave and a key of the table only)."""
    if key in NOTICE_TEMPLATES:
        return _first_line(notice_text(cfg or {}, wave, key, 0), 300)
    return "❗ волне требуется внимание"


def attention_text(st, cfg=None):
    """ATTENTION for the open signals of the state: the latest one, or None when none is open. Every name in it
    (wave, session) is shown only in the format of the registry, else «?»; the wave's question is never in it."""
    latest, total = None, 0
    for wave, w in (st.get("waves") or {}).items():
        att = w.get("attention") if isinstance(w, dict) else None
        if not isinstance(att, dict):
            continue
        for key, item in att.items():
            if isinstance(item, dict):
                total += 1
                if latest is None or (num(item.get("at")) or 0) > (num(latest[3].get("at")) or 0):
                    latest = (wave, w, key, item)
    if latest is None:
        return None
    wave, w, key, item = latest
    shown = NOTICE_FIELDS["wave"][1](cfg, wave)
    text = (f"time: {hum(cfg, num(item.get('at')))}\nwave: {shown}\n"
            f"signal: {attention_signal(cfg, shown, key)}\n"
            f"attach: {external_attach(w.get('tmux'))}\n")
    if total > 1:
        text += f"open signals: {total} (wab.py status, events.log)\n"
    return text


def sync_attention(cfg, st):
    """Make $RUN_DIR/ATTENTION match the open signals of the state: atomically rewritten with the
    latest one, removed when none is left. Called by save_state, so an episode that ends with a save
    (RUNNING after BLOCKED, an ack, the next launch) takes the file with it."""
    path = attention_path(cfg)
    text = attention_text(st, cfg)
    if text is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if read(path, None) == text.strip():
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".attention.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def attention_cmd(cfg):
    """Print ATTENTION; 1 when it holds an open signal, 0 when there is none (no file)."""
    text = read(attention_path(cfg))
    if text:
        print(text)
        return 1
    return 0


def flush_notices(cfg, st, w, force=False):
    """Send the queued notices that are due; one attempt per notice per NOTIFY_RETRY_SECONDS
    (also bounds the `telegram FAILED` events). `force` ignores the interval (the watch is leaving). An ended episode is dropped first."""
    if drop_ended_episodes(w):
        save_state(cfg, st)
    box = w.get("outbox")
    if not box:
        return
    now = time.time()
    tried = False
    local = not telegram_configured(cfg)
    wave = next((k for k, v in st.get("waves", {}).items() if v is w), None)
    for key, item in list(box.items()):
        if key not in NOTICE_TEMPLATES:  # a key of no template cannot be told in words: it leaves unsent
            box.pop(key, None)
            event(cfg, f"{wave}: outbox entry {safe_text(key, 40)} has no template; dropped")
            continue
        if item.get("next_at", 0) > now and not force:
            continue
        tried = True
        notice = outbox_notice(cfg, wave, w, key, item)
        if notify(cfg, notice, wave):
            box.pop(key, None)
            if local and key not in INFO_NOTICES:
                note_attention(w, key, notice, now)
        else:
            item["next_at"] = now + NOTIFY_RETRY_SECONDS
    if not box:
        w.pop("outbox", None)
    if tried:
        save_state(cfg, st)


DRAIN_ATTEMPTS = 3


def pending_notices(st):
    return any(rec.get("outbox") for rec in st.get("waves", {}).values())


def drain_notices(cfg):
    """The watch is about to exit (handoff, chain finished, chain stopped) and nobody will tick
    again: try the undelivered notices a few more times, ignoring the retry interval. What is
    still undelivered stays in the state; the next `watch` (or `done`) sends it before it exits."""
    for attempt in range(DRAIN_ATTEMPTS):
        st = load_state(cfg)
        if not pending_notices(st):
            return
        for rec in st["waves"].values():
            flush_notices(cfg, st, rec, force=True)
        if not pending_notices(st):
            return
        if attempt + 1 < DRAIN_ATTEMPTS:
            time.sleep(min(cfg["tick_seconds"], 10))


# ---------- launch ----------

def host_helper():
    return pathlib.Path.home() / ".claude" / "bin" / "cc-autonomy.py"


def host_policy_signals():
    """Signs that the host has an admission policy (rules/autonomy-allowlist.md of the host):
    its helper or its rule. Any one of them makes the host managed; only none means unmanaged.
    A dangling symlink is still a signal (lexists)."""
    rule = pathlib.Path.home() / ".claude" / "rules" / "autonomy-allowlist.md"
    return [str(p) for p in (host_helper(), rule) if os.path.lexists(p)]


def isolated_workdir(path):
    """Unmanaged host (no admission policy): any git checkout or worktree owned by the current
    user is a valid isolated worktree. Return the reason it is not."""
    r = sh("git", "-C", str(path), "rev-parse", "--show-toplevel", check=False)
    if r.returncode != 0:
        return f"not a git checkout: {r.stderr.strip()}"
    top = r.stdout.strip()
    if not top or not _same_path(path, top):  # a subdirectory of someone else's checkout is not isolated
        return (f"workdir must be the root of an isolated checkout/worktree "
                f"(git top level is {top or '?'})")
    try:
        if pathlib.Path(path).stat().st_uid != os.getuid():
            return f"{path} is not owned by the current user"
    except OSError as e:
        return f"{path}: {_exc_text(e)}"
    return None


def _same_path(a, b):
    try:
        return pathlib.Path(a).resolve() == pathlib.Path(b).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def admit(cfg, st=None):
    """(working copy, reused). Admission belongs to the host: this runtime never judges a clone
    by its own rules.
    Unmanaged host (no policy signal): chain.json `workdir`, any isolated git worktree.
    Managed host (any signal): the helper must be a file, else refused (fail-closed). The clone
    comes only from `cc-autonomy prepare <repo>`; the first wave saves its admitted_path in the
    state and the next waves reuse it. chain.json `workdir` is accepted only when it is that
    saved clone; otherwise (including before the first prepare) it is refused."""
    signals = host_policy_signals()
    if not signals:  # standalone install: no host policy, so no admission to satisfy
        if not cfg.get("workdir"):
            raise SystemExit("admission: no host policy (~/.claude/bin/cc-autonomy.py and "
                             "~/.claude/rules/autonomy-allowlist.md not found): "
                             "set workdir in chain.json to an isolated git worktree")
        why = isolated_workdir(cfg["workdir"])
        if why:
            raise SystemExit(f"workdir {cfg['workdir']} refused: {why}")
        event(cfg, f"admission: unmanaged host (no host policy), isolated git worktree {cfg['workdir']}")
        return cfg["workdir"], True
    helper = host_helper()
    if not helper.is_file():
        raise SystemExit(f"admission refused: host policy present ({', '.join(signals)}) but its helper "
                         f"{helper} is not a runnable file; fix the host install (fail-closed, "
                         f"not treated as an unmanaged host)")
    saved = (st or {}).get("admitted_path")
    if not isinstance(saved, str) or not saved:
        saved = None
    if cfg.get("workdir") and not (saved and _same_path(cfg["workdir"], saved)):
        raise SystemExit(f"admission refused for workdir {cfg['workdir']}: on a managed host workdir comes "
                         f"from cc-autonomy prepare (saved clone: {saved or 'none yet'}); remove workdir "
                         f"from chain.json")
    if saved and os.path.isdir(saved):
        event(cfg, f"admission: host policy, clone of cc-autonomy prepare kept for the chain: {saved}")
        return saved, True
    if saved:
        event(cfg, f"admission: saved clone {saved} is gone; cc-autonomy prepare again")
    r = sh("python3", str(helper), "prepare", str(cfg.get("repo") or ""), check=False)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
    if not isinstance(out, dict):  # valid JSON that is not an object ([], null, 5, "x"): no answer
        out = {}
    path = out.get("admitted_path")
    if r.returncode != 0 or out.get("ok") is not True or not isinstance(path, str) or not path:
        raise SystemExit(f"admission refused: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
    event(cfg, f"admission: host policy, cc-autonomy prepare -> {path}")
    if st is not None:
        st["admitted_path"] = path
        save_state(cfg, st)  # at once: a later refusal must not make the next try prepare a new clone
    return path, False


def prepare_clone(cfg, st=None):
    return admit(cfg, st)[0]  # the working copy only; `launch` tells a reused one by the state


GIT_TIMEOUT = 120


def base_branch_of(cfg, cwd):
    """chain.json `base_branch`, else the branch origin's HEAD points at; None when unknown."""
    if cfg.get("base_branch"):
        return cfg["base_branch"]
    r = sh("git", "-C", str(cwd), "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False,
           timeout=GIT_TIMEOUT)
    name = r.stdout.strip()
    return name[len("origin/"):] if r.returncode == 0 and name.startswith("origin/") else None


def verify_previous_merge(cfg, wave, cwd, branch, base, target):
    """Squash merges (the repositories' standard) leave the wave branch's commits out of
    origin/<base>, so the check is by PR: `gh pr view <branch>` must say MERGED and its merge
    commit must be an ancestor of the fetched base. No gh or no PR known: an event, and go on
    (the full gate is W5)."""
    why = "previous branch unknown"
    if branch and branch != base:
        argv = ["gh", "pr", "view", branch, "--json", "state,mergeCommit"]
        if cfg.get("repo"):
            argv += ["--repo", str(cfg["repo"])]
        try:
            r = sh(*argv, check=False, timeout=GIT_TIMEOUT, cwd=str(cwd))
            info = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            info = None
        if isinstance(info, dict) and info.get("state"):
            oid = (info.get("mergeCommit") or {}).get("oid") or ""
            if info["state"] != "MERGED":
                raise SystemExit(f"wave {wave} refused: previous wave not merged (PR of {branch} is "
                                 f"{info['state']}); merge it, then launch again")
            anc = sh("git", "-C", str(cwd), "merge-base", "--is-ancestor", oid, target,
                     check=False, timeout=GIT_TIMEOUT) if oid else None
            if anc is None or anc.returncode != 0:
                raise SystemExit(f"wave {wave} refused: previous wave not merged into origin/{base} "
                                 f"(merge commit {oid[:12] or '?'} of {branch} is not in it)")
            return
        why = f"no PR information for {branch}"
    event(cfg, f"{wave}: merge of previous wave not verified (W5): {why}")


def refresh_workdir(cfg, wave, cwd):
    """A reused workdir must not start wave N on the branch of wave N-1: clean tree, fetch the
    base, detach on origin/<base>. The previous wave's PR (its branch is the one the tree is on)
    must be MERGED (verify_previous_merge). Refusals are SystemExit before any state is touched."""
    git = lambda *a, **kw: sh("git", "-C", str(cwd), *a, check=False, timeout=GIT_TIMEOUT, **kw)
    base = base_branch_of(cfg, cwd)
    if not base:
        raise SystemExit(f"wave {wave} refused: base_branch is not set in chain.json and origin/HEAD is unknown")
    dirty = git("status", "--porcelain", "--untracked-files=all")
    if dirty.returncode != 0:
        raise SystemExit(f"wave {wave} refused: git status failed in {cwd}: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise SystemExit(f"wave {wave} refused: workdir {cwd} is not clean (git status --porcelain "
                         f"--untracked-files=all is not empty); commit, stash or remove the changes "
                         f"of the previous wave")
    fetch = git("fetch", "origin", base)
    if fetch.returncode != 0:
        raise SystemExit(f"wave {wave} refused: git fetch origin {base} failed: {fetch.stderr.strip()}")
    target = git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{base}^{{commit}}")
    if target.returncode != 0:
        raise SystemExit(f"wave {wave} refused: origin/{base} not found after fetch")
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()  # the previous wave's branch
    verify_previous_merge(cfg, wave, cwd, branch, base, target.stdout.strip())
    sw = git("switch", "--detach", f"origin/{base}")
    if sw.returncode != 0:
        raise SystemExit(f"wave {wave} refused: git switch --detach origin/{base} failed: {sw.stderr.strip()}")
    event(cfg, f"{wave}: workdir {cwd} detached on origin/{base} ({target.stdout.strip()[:12]})")


MANDATE_HEADER = "Прогон: "

# ---------- decision policy (#41) ----------
# A wave that writes BLOCKED puts a machine label first: `BLOCKED: [class=<c> rec=<r> red=<yes|no>] ...`.
# The optional `decision_policy` of chain.json (a list of {"class": c, "rec": r?}; part of the run's
# identity, outside run_dir, so no wave can rewrite it) names the episodes the dispatcher answers itself.
# Nothing in mandate.md is parsed for a policy: a «Политика решений» heading there is plain text.
# A line without a (valid) label, red=yes, merge_gate and anything the policy does not name go to the
# owner as before.
LABEL_CLASSES = ("needs_decision", "blocked_cap", "plan_mismatch", "question", "merge_gate")
# Never in a policy: merge_gate is written by the dispatcher; plan_mismatch needs an amendment of the
# approved waves.json, which takes the owner's «ок» and a new plan_sha256 — an answer cannot give that.
OWNER_ONLY_CLASSES = {
    "merge_gate": "merge_gate is never answered automatically",
    "plan_mismatch": ("plan_mismatch always goes to the owner (владельцу): an amendment of the approved "
                      "waves.json needs his «ок» and a new plan_sha256, an automatic answer cannot give it"),
}
POLICY_CLASSES = tuple(c for c in LABEL_CLASSES if c not in OWNER_ONLY_CLASSES)
REC_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_LABEL = re.compile(r"BLOCKED:[ \t]*\[([^\]\n]*)\][ \t]*(.*)", re.S)
POLICY_ANSWER = ("[wab] РЕШЕНИЕ ПО ПОЛИТИКЕ (chain.json): вариант {rec}. Действуй по своей рекомендации, "
                 "затем запиши RUNNING в status. Находки low/P3 — fix-loop --defer в остаток.")
# A rule with `answer` (#89): the dispatcher's own fixed variant, whatever the wave recommended. The
# closed vocabulary per class; `question` has free variants and takes no `answer`.
POLICY_ANSWERS = {"needs_decision": ("invariant", "cut_surface", "accept_limitation"),
                  "blocked_cap": ("invariant", "new_run", "accept_limitation")}
FORK_STATUS = {"needs_decision": "needs_decision", "blocked_cap": "blocked"}  # class -> manifest task status
FIX_CYCLE_CAP = 3  # state.py: the third unsuccessful fix cycle blocks the task (a literal there, no constant)
READINESS_ROLES = ("coder", "tester", "cross_provider_reviewer", "second_reviewer", "internal_reviewer",
                   "final_check", "github_codex_review")
FIXED_HEAD = "[wab] РЕШЕНИЕ ПО ПОЛИТИКЕ (chain.json): вариант {answer}."
FIXED_THOROUGH = ("чини класс находки целиком, не латай; находки low/P3 — fix-loop --defer в остаток.")
FIXED_RECORD = " Решение запиши: fix-loop --decision invariant."
FIXED_NEW_RUN = " Открой новый прогон (state.py init --from-plan на новом пути), прогон {n} из {max}."
# THE wording, one entry per (class, answer, «a new run is needed»). The invariant: an answer names only what
# the wave can execute in the state of its fork, and one decision. state.py takes `fix-loop --decision` only
# at a needs_decision task — so it is named for that class alone; a blocked task (blocked_cap) does not end
# in its run, so every blocked_cap answer opens a new run, and a limitation is accepted there by
# `fix-loop --accept` (the task of a new run is not needs_decision either).
FIXED_MEANING = {
    ("needs_decision", "invariant", False): " Доделай как следует: ещё один круг, " + FIXED_THOROUGH + FIXED_RECORD,
    ("needs_decision", "invariant", True): " Доделай как следует: " + FIXED_THOROUGH + FIXED_RECORD + FIXED_NEW_RUN,
    ("needs_decision", "cut_surface", False): (" Вырежи поверхность, давшую находку (fix-loop --decision "
                                               "cut_surface), остаток — в result.md."),
    ("needs_decision", "accept_limitation", False): (" Прими ограничение (fix-loop --decision accept_limitation): "
                                                     "строка в «Принятые ограничения» PR и в остаток."),
    ("blocked_cap", "invariant", True): " Доделай как следует: " + FIXED_THOROUGH + FIXED_NEW_RUN,
    ("blocked_cap", "accept_limitation", True): (
        " Прими ограничение: открой новый прогон (state.py init --from-plan на новом пути), прогон {n} из {max}, "
        "и в нём находку не чини, а прими как ограничение (fix-loop --accept, строка в «Принятые ограничения» PR "
        "и в остаток)."),
}
FIXED_MEANING["needs_decision", "cut_surface", True] = FIXED_MEANING["needs_decision", "cut_surface", False] + FIXED_NEW_RUN
FIXED_MEANING["needs_decision", "accept_limitation", True] = (
    FIXED_MEANING["needs_decision", "accept_limitation", False] + FIXED_NEW_RUN)
FIXED_MEANING["blocked_cap", "new_run", True] = FIXED_MEANING["blocked_cap", "invariant", True]
FORK_MARKS_CORRUPT = "отметки автоответов в state.json испорчены (policy_keys / policy_key_pending)"
FORK_CHANGED = "состояние развилки изменилось после ввода ответа: "  # + the reason of the check as it reads now
FIXED_TAIL = " Затем запиши RUNNING в status."
MAX_AUTO_ANSWERS = 3  # chain.json `max_auto_answers` default: automatic answers per wave
MAX_RUNS = 2  # chain.json `max_runs` default: superarmanda runs (`init --from-plan`) per wave


def parse_blocked_label(status):
    """The machine label of a BLOCKED line as {class, rec, red, question}, or None: no label, or any
    deviation from exactly `class=<known> rec=<token> red=<yes|no>` (unknown or repeated key, unknown
    class, a rec that is not a short token). None means «no automatic answer», never a guess."""
    m = _LABEL.match(status or "")
    if not m:
        return None
    fields = {}
    for tok in m.group(1).split():
        key, sep, value = tok.partition("=")
        if not sep or key not in ("class", "rec", "red") or key in fields:
            return None
        fields[key] = value
    if (set(fields) != {"class", "rec", "red"} or fields["class"] not in LABEL_CLASSES
            or not REC_TOKEN.fullmatch(fields["rec"]) or fields["red"] not in ("yes", "no")):
        return None
    return {"class": fields["class"], "rec": fields["rec"], "red": fields["red"] == "yes",
            "question": m.group(2).strip()}


def check_decision_policy(value):
    """chain.json `decision_policy`: a list of {"class": <class>, "rec": <token>?, "answer": <variant>?}
    (`answer`: POLICY_ANSWERS of the class, never with `question`). The reason it is
    refused, or None. Never in it: `red` (the red zone always goes to the owner), merge_gate,
    plan_mismatch (OWNER_ONLY_CLASSES), any other key, an unknown class, a rec that is not a token."""
    if not isinstance(value, list):
        return f"must be a list of {{\"class\": ..., \"rec\": ...}} objects, got {value!r}"
    for i, rule in enumerate(value):
        if not isinstance(rule, dict):
            return f"[{i}] must be an object, got {rule!r}"
        if "red" in rule:
            return f"[{i}]: `red` is not a policy key: the red zone always goes to the owner"
        extra = sorted(set(rule) - {"class", "rec", "answer"})
        if extra:
            return f"[{i}]: unknown key(s) {', '.join(map(repr, extra))} (allowed: class, rec, answer)"
        cls = rule.get("class")
        if isinstance(cls, str) and cls in OWNER_ONLY_CLASSES:
            return f"[{i}]: {OWNER_ONLY_CLASSES[cls]}"
        if cls not in POLICY_CLASSES:
            return f"[{i}]: class must be one of {', '.join(POLICY_CLASSES)}, got {cls!r}"
        rec = rule.get("rec")
        if "rec" in rule and not (isinstance(rec, str) and REC_TOKEN.fullmatch(rec)):
            return f"[{i}]: rec must match [A-Za-z0-9_.-]{{1,40}} (or be left out), got {rec!r}"
        if "answer" in rule:
            answer, allowed = rule["answer"], POLICY_ANSWERS.get(cls)
            if allowed is None:
                return f"[{i}]: answer is not allowed with class {cls} (its variants are free text)"
            if not isinstance(answer, str) or answer not in allowed:
                return (f"[{i}]: answer of class {cls} must be one of {', '.join(allowed)} "
                        f"(or be left out), got {answer!r}")
    return None


def decision_policy(cfg):
    """The rules of chain.json `decision_policy` (checked by load_chain) as [{class, rec}], rec None =
    any recommended variant of that class; a rule with `answer` carries it too, one without has no such
    key (the 1.2.0 shape; read it with .get: None = the wave's own recommendation).
    No field: no automatic answers. The FIRST rule that fits the class and the rec decides."""
    return [dict({"class": r["class"], "rec": r.get("rec")}, **({"answer": r["answer"]} if "answer" in r else {}))
            for r in cfg.get("decision_policy") or []]


def _refuse(reason):
    return {"ok": False, "reason": reason}


def _fork_keys(rec):
    """(answered keys, the key in flight or None) of one wave record of state.json. A mark that is there
    but corrupt (`policy_keys` not a list of lists, `policy_key_pending` not an object with a list `key`)
    raises ValueError: it is never read as «nothing was answered» — fixed_answer refuses (fail closed)."""
    keys, pend = rec.get("policy_keys", []), rec.get("policy_key_pending")
    if (not isinstance(keys, list) or not all(isinstance(k, list) for k in keys)
            or not (pend is None or (isinstance(pend, dict) and isinstance(pend.get("key"), list)))):
        raise ValueError(FORK_MARKS_CORRUPT)
    return keys, pend


def _keep_fork_key(w):
    """A typed fixed answer (#89) is given up and may have been submitted: its fork key leaves the flight
    and counts as answered (`policy_keys`) — never just dropped. A corrupt mark stays as it is:
    fixed_answer refuses on it."""
    try:
        keys, pend = _fork_keys(w)
    except ValueError:
        return
    if pend:
        w["policy_keys"] = keys if pend["key"] in keys else [*keys, pend["key"]]
        w.pop("policy_key_pending")


def _forget_fork_key(w, fixed):
    """Nothing of THIS fixed answer reached the window: its own key (and only its own) leaves the flight."""
    pend = w.get("policy_key_pending")
    if isinstance(pend, dict) and pend.get("key") == fixed["key"]:
        w.pop("policy_key_pending")


def _refusal_ok(refusal):
    """Is it a `policy_refusal` mark as _policy_answer writes it. Anything else under that name is corrupt
    and reads as a refusal for as long as the wave is BLOCKED (fail closed), never as «decide anew»."""
    return (isinstance(refusal, dict) and isinstance(refusal.get("status"), str)
            and isinstance(refusal.get("reason"), str)
            and (refusal.get("stamp") is None or isinstance(refusal.get("stamp"), list)))


def fixed_answer(cfg, wave, w, label, rule, status):
    """THE decision of a rule with `answer` (#89), by the dispatcher's files only — runs.json, the manifest
    of the current run (current_manifest, read by state.py's own reader), state.json — never by the wave's
    text. {"ok": True, text, key, answer, rec} or {"ok": False, reason}: the reason is a fixed wording of
    the dispatcher plus a task/role name of the manifest and numbers. Refused (the owner decides): runs.json
    or the manifest missing/broken/of another run; not exactly one task in the status of the class, or a
    needs_decision without a fix-loop source; a readiness role unavailable/error on the manifest's HEAD (an
    external failure is not fixed by another run); a new run needed above `max_runs`; the same fork (key)
    answered before; a corrupt mark of the answered keys in state.json (this try or an archived one), a
    corrupt `attempts` container included. The Enter-only retry of a typed answer is the whole check again
    (only its own key in flight is not «answered before»): the Enter goes out only when the check passes NOW
    and gives the very decision that was typed (the same key and text) — otherwise a refusal (FORK_CHANGED).
    Never raises."""
    try:
        return _fixed_answer(cfg, wave, w, label, rule, status)
    except Exception:  # noqa: BLE001 - fail closed: no automatic answer, a fixed reason
        return _refuse("состояние развилки не разбирается")


def _fixed_answer(cfg, wave, w, label, rule, status):
    attempts = w.get("attempts", [])  # wave_attempts() skips a corrupt container: here it would hide archived keys
    if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
        return _refuse(FORK_MARKS_CORRUPT)
    try:  # before anything else, the Enter-only retry included: a corrupt mark of ANY record of the wave
        marks = [_fork_keys(rec) for rec in [w, *wave_attempts(w)]]
    except ValueError:
        return _refuse(FORK_MARKS_CORRUPT)
    pend = marks[0][1]
    # the Enter-only retry of THIS episode: the answer is typed, its Enter is owed — and is pressed only when
    # the check below passes now and gives the same decision (a role may have failed, the run or `max_runs`
    # changed since the text was typed)
    retry = bool(pend and pend.get("status") == status and w.get("pending_enter") == "policy answer"
                 and w.get("policy_pending") == status)
    got = _fork_decision(cfg, wave, label, rule, marks, pend if retry else None)
    if retry and not (got["ok"] and got["key"] == pend["key"] and got["text"] == pend.get("text")):
        return _refuse(FORK_CHANGED + ("пересчитанный ответ не совпадает с напечатанным" if got["ok"]
                                       else got["reason"]))
    return got


def _fork_decision(cfg, wave, label, rule, marks, own):
    """The machine check of fixed_answer by the files as they are now. `own`: the key in flight of the
    Enter-only retry — the one mark that does not read as «answered before»."""
    cls, answer = label["class"], rule["answer"]
    path, why = current_manifest(cfg, wave)
    if path is None:
        return _refuse(why)
    try:  # with `answer` runs.json is mandatory: the standard-manifest fallback of current_manifest is not enough
        runs = gate.state.read_runs(wave_dir(cfg, wave) / "runs.json", wave)
    except (SystemExit, Exception):  # noqa: BLE001 - state.py refuses by SystemExit
        runs = None
    if not runs:
        return _refuse("runs.json волны нет, он пуст или не читается")
    index = runs[-1].get("index")
    try:
        data = gate.state.read(path)  # the one schema check of state.py, not a copy
    except (SystemExit, Exception):  # noqa: BLE001
        return _refuse("manifest текущего прогона не читается или не проходит схему state.py")
    run = data.get("run")
    if (not _count(index) or not isinstance(run, dict) or run.get("index") != index
            or not isinstance(data.get("tasks"), dict)):
        return _refuse("номер прогона в manifest не совпадает с runs.json")
    want = FORK_STATUS[cls]
    found = [(n, e) for n, e in data["tasks"].items() if isinstance(e, dict) and e.get("status") == want]
    if len(found) != 1:
        return _refuse(f"задач в статусе {want}: {len(found)} (нужна ровно одна)")
    task, entry = found[0]
    if not REC_TOKEN.fullmatch(task):
        return _refuse("имя задачи развилки не токен")
    source = ""
    if cls == "needs_decision":
        source = entry.get("decision_required_for")
        if not isinstance(source, str) or source not in gate.state.FIX_SOURCES:
            return _refuse(f"у задачи {task} нет источника fix-loop (decision_required_for)")
    results = entry.get("results") if isinstance(entry.get("results"), dict) else {}
    for role in READINESS_ROLES:
        item = results.get(role)
        if (isinstance(item, dict) and item.get("head") == data.get("head")
                and item.get("status") in ("unavailable", "error")):
            return _refuse(f"внешний сбой: роль {role} задачи {task} — {item['status']} на текущем HEAD")
    cycles = entry.get("fix_cycles")
    if isinstance(cycles, bool) or not isinstance(cycles, int):
        return _refuse(f"счётчик fix-loop задачи {task} не число")
    new_run = cls == "blocked_cap" or cycles >= FIX_CYCLE_CAP
    cap = cfg["max_runs"]
    if new_run and index >= cap:
        return _refuse(f"потолок прогонов исчерпан ({index}/{cap}), новый прогон открыть нельзя")
    key = [wave, cfg["run_id"], index, task, cls, source]
    for keys, pend in marks:
        if key in keys or (pend and pend is not own and pend.get("key") == key):
            return _refuse(f"развилка задачи {task} уже получила автоответ в прогоне {index}")
    text = FIXED_HEAD.format(answer=answer) + FIXED_MEANING[cls, answer, new_run].format(n=index + 1, max=cap)
    if label["rec"] != answer:
        text += f" (рекомендация волны: {label['rec']})"
    return {"ok": True, "text": text + FIXED_TAIL, "key": key, "answer": answer, "rec": label["rec"]}


def system_prompt(cfg):
    """Generic protocol plus the mandate of THIS run only (run_dir/mandate.md, approved in
    phase A). A dated mandate must never ride along with an unrelated chain or rerun:
    its first line must name this run_id, otherwise it is ignored. Waves may write into
    run_dir, so the approved bytes are pinned by `mandate_sha256` in chain.json (which lives
    outside run_dir): a mandate whose digest differs is refused, not trusted. A pin is also
    a promise that the mandate exists: with the `mandate_sha256` key present (whatever its value;
    load_chain only lets 64 lowercase hex through), a missing, empty or foreign-headed mandate.md
    refuses the launch instead of falling back to the bare protocol. An unreadable mandate.md
    (a directory, no permission) is refused with or without a pin."""
    text = PROTOCOL.read_text(encoding="utf-8").rstrip() + "\n"
    mandate = cfg["run_dir"] / "mandate.md"
    try:
        raw = mandate.read_bytes() if mandate.exists() else b""
    except OSError as e:
        raise SystemExit(f"{mandate}: cannot read the mandate ({e.strerror or e}); refusing to launch")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"{mandate} is not valid UTF-8")
    first = body.splitlines()[:1]
    pinned = "mandate_sha256" in cfg
    if pinned and not (first and first[0].strip() == MANDATE_HEADER + cfg["run_id"]):
        why = ("is missing" if not mandate.exists() else "is empty" if not body.strip()
               else f"first line is not «{MANDATE_HEADER}{cfg['run_id']}»")
        raise SystemExit(f"{mandate} {why}, but chain.json pins mandate_sha256 {cfg['mandate_sha256']!r}: "
                         f"the approved mandate is gone, refusing to launch without it")
    if first and first[0].strip() != MANDATE_HEADER + cfg["run_id"]:
        event(cfg, f"mandate.md ignored: first line must be «{MANDATE_HEADER}{cfg['run_id']}»")
    elif first:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != cfg.get("mandate_sha256"):
            raise SystemExit(f"mandate.md sha256 {digest} != chain.json mandate_sha256 "
                             f"{cfg.get('mandate_sha256')}: mandate changed after approval")
        text += (f"\n## Мандат прогона `{cfg['chain']}` (из {mandate})\n\n" + body.strip() + "\n")
    out = cfg["run_dir"] / "system-prompt.md"
    out.write_text(text, encoding="utf-8")
    return out


def launch(cfg, wave, prompt_file, by_dispatcher=False, drain=False):
    """Start one wave. False when the Claude TUI never became ready: nothing is sent then.
    `by_dispatcher`: the watch itself goes on to the next wave (no coordinator confirmed anything).
    `drain`: the CLI exits right after a failed launch, so the notices get a few more tries first."""
    with _RunLock(cfg, "launch"):
        ok = _launch(cfg, wave, prompt_file, by_dispatcher)
        if not ok and drain:
            drain_notices(cfg)  # what is still undelivered stays for the next watch/launch/done
        return ok


HANDED_OVER = ("awaiting_merge", "done")
STOPPED = ("dead", "not_ready")


PLAN_MAX_BYTES = 1024 * 1024


def check_plan_pin(cfg):
    """chain.json `plan_sha256` pins the approved <run_dir>/waves.json: waves may write into
    run_dir, so changed, missing or non-regular bytes refuse the launch. No key: no check."""
    if "plan_sha256" in cfg:
        pinned_plan_bytes(cfg)


def pinned_plan_bytes(cfg):
    """The bytes of <run_dir>/waves.json that chain.json `plan_sha256` pins; SystemExit with the reason
    when they are missing, not a regular file, too large or not the pinned ones."""
    plan = cfg["run_dir"] / "waves.json"
    head = "BLOCKED: plan changed since approval: "
    tail = "; the approved plan is not what is on disk, ask the owner (re-pin plan_sha256 in chain.json)"
    nofollow = lambda path, flags: os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)  # binary, "rb"
    try:
        with open(plan, "rb", opener=nofollow) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit(f"{head}{plan} is not a regular file{tail}")
            raw = f.read(PLAN_MAX_BYTES + 1)
        if len(raw) > PLAN_MAX_BYTES:
            raise SystemExit(f"{head}{plan} is larger than {PLAN_MAX_BYTES} bytes{tail}")
    except OSError as e:
        raise SystemExit(f"{head}{plan} cannot be read ({e.strerror or e}){tail}")
    got = hashlib.sha256(raw).hexdigest()
    if got != cfg["plan_sha256"]:
        raise SystemExit(f"BLOCKED: plan changed since approval: waves.json sha256 {got} != "
                         f"chain.json plan_sha256 {cfg['plan_sha256']}")
    return raw


def plan_wave_risk(cfg, wave):
    """What the approved plan says about the wave, for the merge gate (gate.manifest_problems `plan`):
    None - chain.json has no `plan_sha256` (a chain from before the pin: the rules of 1.2.0);
    {"risk": low|medium|high} - the risk of the wave in the pinned <run_dir>/waves.json;
    {"error": reason} - the pinned plan is missing, changed, not a plan or has no such wave: the gate
    refuses (never «not high»). The reasons are fixed texts and quote nothing from the file. Never raises."""
    if "plan_sha256" not in cfg:
        return None
    try:
        raw = pinned_plan_bytes(cfg)
    except SystemExit:
        return {"error": "одобренный waves.json не читается или не совпадает с plan_sha256 chain.json"}
    except Exception:  # noqa: BLE001 - fail closed
        return {"error": "одобренный waves.json не читается"}
    try:
        doc = gate.state.parse_plan(raw)  # the schema of state.py `init --from-plan`, not a copy
    except Exception:  # noqa: BLE001 - PlanError and anything a hostile file may raise: fail closed
        return {"error": "одобренный waves.json не разбирается как план волн"}
    for item in doc["waves"]:
        if item["id"] == wave:
            return {"risk": item["risk"]}
    return {"error": f"волны {wave} нет в одобренном waves.json"}


# ---------- the two reviews of the approved plan (1.2.2, #86 п.3) ----------
#
# Phase A leaves in <run_dir>/plan-review/ (see references/waves.md):
#   plan.sha256                   the sha256 of the approved waves.json (= chain.json plan_sha256);
#   packet.json                   the packet of `review.py packet` whose diff ADDS waves.json with those bytes;
#   result-claude-host.json       the report of `review.py run --profile claude-host` (Astra) for that packet;
#   result-codex-host.json        the report of profile codex-host (Fable) for that packet;
#   result-codex-host-opus.json   only for the fallback: codex-host-opus counts when result-codex-host.json
#                                 is the quota error report of codex-host for the same packet.
# Only these names are read: reports of earlier rounds kept beside them (history/ ...) are never counted,
# and a report copied over one of the names is told apart by its `state_packet_hash`.

PLAN_REVIEW_DIR = "plan-review"
PLAN_REVIEW_PACKET_LIMIT = 8 * 1024 * 1024  # the diff carries the whole plan (at most PLAN_MAX_BYTES)
_review_py = None


def review_py():
    """review.py as a module (the packet format and `state_packet_hash` are its, not a copy)."""
    global _review_py
    if _review_py is None:
        _review_py = gate._load("review")
    return _review_py


def diff_added_file(diff, name):
    """The bytes of `name` when the unified diff of a review packet ADDS it as a new file (one section
    `diff --git a/<name> b/<name>` with `new file mode`, `--- /dev/null` and one hunk of added lines);
    None for anything else: no such section, a change of an existing file, a rename, a binary patch."""
    lines = diff.split("\n")
    starts = [i for i, line in enumerate(lines) if line == f"diff --git a/{name} b/{name}"]
    if len(starts) != 1:
        return None
    new = hunk = bare = False
    body = []
    for line in lines[starts[0] + 1:]:  # the rest of the diff after the header
        if line.startswith("diff --git "):
            break
        if hunk:
            if line.startswith("+"):
                body.append(line.removeprefix("+"))
            elif line == "\\ No newline at end of file":
                bare = True
            elif line:  # a second hunk, a context or a removed line: not a plain addition
                return None
        elif line.startswith("new file mode "):
            new = True
        elif line.startswith("@@ "):
            if not line.startswith("@@ -0,0 +"):
                return None
            hunk = True
        elif line not in ("--- /dev/null", f"+++ b/{name}") and not line.startswith("index "):
            return None
    if not (new and hunk):
        return None
    return ("\n".join(body) + ("" if bare else "\n")).encode("utf-8")


def _plan_report(directory, profile):
    """(report, None) of plan-review/result-<profile>.json, or (None, reason). Read by state.read_report:
    a regular file, not a symlink, bounded, one JSON object."""
    name = f"result-{profile}.json"
    try:
        report, _digest = gate.state.read_report(str(directory / name), name)
    except SystemExit:
        return None, f"{name} is missing or is not a readable review.py report"
    return report, None


def _plan_report_problem(report, profile, envelope):
    """Why a report is not a gate_ready review of THIS packet by `profile` (None: it is). The rules of a
    review report are state.py's (report_models_problem, report_verified), as for a task review."""
    name = f"result-{profile}.json"
    state, packet_hash = gate.state, review_py().state_packet_hash(envelope)
    if report.get("profile") != profile:
        return f"{name} is not a report of profile {profile}"
    if state.report_models_problem(report, profile):
        return f"{name}: its models do not belong to profile {profile}"
    if report.get("state_packet_hash") != packet_hash:
        return f"{name} is a review of another packet (an earlier round of the plan?), not of packet.json"
    response = report.get("response")
    if not (report.get("status") == "pass" and report.get("gate_ready") is True and state.report_verified(report)
            and isinstance(response, dict) and response.get("reviewed_head") == envelope["packet"]["head"]):
        return f"{name} is not a gate_ready pass (status {report.get('status')!r}, gate_ready {report.get('gate_ready')!r})"
    return None


PLAN_HISTORY_DIR = "history"
PLAN_HISTORY_MAX_FILES = 1000  # more files than that in history/ is not a phase A, it is a refusal


def _report_moment(report):
    """The `created_at` of a review.py report (1.4.0) as an aware UTC datetime, or None."""
    value = report.get("created_at") if isinstance(report, dict) else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None and moment.utcoffset() == datetime.timedelta(0) else None


def _history_files(history):
    """The regular .json files of plan-review/history/ (symlinks and anything else are left out), by name."""
    names = sorted(e.name for e in os.scandir(history) if e.name.endswith(".json"))
    if len(names) > PLAN_HISTORY_MAX_FILES:
        raise ValueError(f"more than {PLAN_HISTORY_MAX_FILES} files")
    return [history / n for n in names if stat.S_ISREG(os.lstat(history / n).st_mode)]


def fable_rounds_problem(cfg, directory, envelope, astra):
    """The plan review without result-codex-host.json (1.4.0, #108): (None, (rounds, last report)) when
    plan-review/history/ holds at least `plan_review_fable_rounds` real Fable rounds and the Astra pass of
    the current packet is newer than each valid Fable report there with a created_at (a round or not, any
    status: pass, findings, error/quota; a report without created_at is no round and takes no part in the
    order); (reason, None) otherwise.

    A round is a review.py report of profile codex-host whose models are Fable's (report_models_problem)
    with `primary_model_verified`, status `findings`, a `created_at`, and the packet it reviewed lying in
    history/ as well: a valid review.py packet whose diff ADDS waves.json, whose head the report reviewed,
    and which is not the approved packet itself. review.py writes `gate_ready: false` for every findings
    report, so gate_ready is not asked of a round. Rounds count by distinct packets; error reports
    (a quota among them) are no rounds. Files are regular files, never symlinks (like the reports above)."""
    state, review = gate.state, review_py()
    need = cfg.get("plan_review_fable_rounds", PLAN_REVIEW_FABLE_ROUNDS)
    current = review.state_packet_hash(envelope)
    astra_at = _report_moment(astra)
    if astra_at is None:
        return f"result-{state.ASTRA_PROFILE}.json has no created_at (UTC ISO-8601; review.py 1.4.0+)", None
    history = directory / PLAN_HISTORY_DIR
    if history.is_symlink() or not history.is_dir():
        return f"no {PLAN_HISTORY_DIR}/ directory with the Fable rounds (a symlink is none)", None
    packets, reports = {}, []
    for path in _history_files(history):
        try:
            env, _raw = review.load_packet(path, PLAN_REVIEW_PACKET_LIMIT)
        except Exception:  # noqa: BLE001 - not a packet: maybe a report
            env = None
        if env is not None:
            if diff_added_file(env["packet"]["diff"], "waves.json") is not None:
                packets[review.state_packet_hash(env)] = env
            continue
        try:
            report, _digest = state.read_report(str(path), path.name)
        except SystemExit:
            continue
        if report.get("profile") == state.FABLE_PROFILE:
            reports.append((path, report))
    rounds = {}
    for path, report in reports:
        # (1) the order: the Astra pass is newer than EVERY valid Fable report of history/ with a created_at,
        # whatever its status (pass, findings, error/quota) and its packet in history/ or not (#108)
        moment = _report_moment(report)
        rel = f"{PLAN_HISTORY_DIR}/{path.name}"
        response = report.get("response")
        capabilities = report.get("capabilities")
        is_round = (report.get("status") == "findings"
                    and not state.report_models_problem(report, state.FABLE_PROFILE)
                    and isinstance(capabilities, dict) and capabilities.get("primary_model_verified") is True
                    and isinstance(response, dict) and response.get("status") == "findings"
                    and moment is not None)
        packet_hash = report.get("state_packet_hash")
        if is_round and packet_hash == current:
            return (f"{rel} holds Fable findings on the approved packet itself: they were never addressed by "
                    f"a change of the plan"), None
        if moment is not None and moment >= astra_at:
            return (f"{rel} is not older than the Astra pass of the current packet: the last Fable report "
                    f"must be addressed and the change reviewed by Astra"), None
        # (2) the count: only verified Fable findings on a plan packet lying in history/
        if not is_round:
            continue
        env = packets.get(packet_hash)
        if env is None or response.get("reviewed_head") != env["packet"]["head"]:
            continue  # its packet is not in history/ (or adds no waves.json): no round
        if packet_hash not in rounds or moment > rounds[packet_hash][0]:
            rounds[packet_hash] = (moment, rel)
    if len(rounds) < need:
        return (f"{len(rounds)} Fable round(s) in {PLAN_HISTORY_DIR}/ (findings reports of codex-host on "
                f"distinct plan packets that lie in {PLAN_HISTORY_DIR}/), {need} needed for an Astra-only "
                f"pass"), None
    return None, (len(rounds), max(rounds.values())[1])


def plan_review_problem(cfg):
    """Why the approved plan of this chain does not carry its two reviews (None: it does)."""
    return _plan_review(cfg)[0]


def _plan_review(cfg):
    """(reason, None) when the plan is not reviewed; (None, None) when both reviews of the current packet
    pass; (None, (rounds, last Fable report)) for the Astra-only pass after the Fable rounds."""
    if "plan_sha256" not in cfg:
        return ("chain.json has no plan_sha256: a new chain starts from an approved waves.json pinned by "
                "plan_sha256 and reviewed by both reviewers"), None
    state, review = gate.state, review_py()
    pin, directory = cfg["plan_sha256"], cfg["run_dir"] / PLAN_REVIEW_DIR
    if directory.is_symlink():  # like the files inside (O_NOFOLLOW): the reviews live in the run directory itself
        return f"{PLAN_REVIEW_DIR}/ is a symlink: it must be a real directory inside the run directory", None
    if not directory.is_dir():
        return f"no {PLAN_REVIEW_DIR}/ in the run directory", None
    try:
        fd = os.open(directory / "plan.sha256", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # bytes ("rb"), no text encoding
        with os.fdopen(fd, "rb") as f:
            recorded = f.read(200).decode("ascii", "replace").strip() if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else None
    except OSError:
        recorded = None
    if recorded is None:
        return "plan.sha256 is missing or unreadable", None
    # the bare hash or a line of `sha256sum waves.json`: the first field is the hash
    recorded = (recorded.split() or [""])[0]
    if not MANDATE_PIN.fullmatch(recorded):
        return ("plan.sha256 must start with the sha256 of the approved waves.json as 64 lowercase hex characters "
                "(the bare hash or a line of `sha256sum waves.json`)"), None
    if recorded != pin:
        return "plan.sha256 differs from chain.json plan_sha256: the reviews are of another plan", None
    try:
        envelope, _raw = review.load_packet(directory / "packet.json", PLAN_REVIEW_PACKET_LIMIT)
    except Exception:  # noqa: BLE001 - ValueError of review.py and whatever a hostile file raises
        return "packet.json is missing or is not a review.py packet (its hash must match its payload)", None
    added = diff_added_file(envelope["packet"]["diff"], "waves.json")
    if added is None or hashlib.sha256(added).hexdigest() != pin:
        return ("the diff of packet.json does not add waves.json with sha256 equal to chain.json plan_sha256: "
                "the reviewed packet is not the approved plan"), None
    packet_hash = review.state_packet_hash(envelope)
    astra, why = _plan_report(directory, state.ASTRA_PROFILE)
    why = why or _plan_report_problem(astra, state.ASTRA_PROFILE, envelope)
    if why:
        return why, None
    name = f"result-{state.FABLE_PROFILE}.json"
    present = (directory / name).exists() or (directory / name).is_symlink()
    opus_name = directory / f"result-{state.OPUS_PROFILE}.json"
    opus_present = opus_name.exists() or opus_name.is_symlink()
    if not present and not opus_present:  # 1.4.0 (#108): after the Fable rounds of phase A, Astra decides
        rounds_why, accepted = fable_rounds_problem(cfg, directory, envelope, astra)
        if not rounds_why:
            return None, accepted
        return (f"{name} is missing and the Astra-only pass after the Fable rounds does not hold: "
                f"{rounds_why}"), None
    fable, missing = _plan_report(directory, state.FABLE_PROFILE)
    why = missing or _plan_report_problem(fable, state.FABLE_PROFILE, envelope)
    if not why:
        return None, None
    opus, no_opus = _plan_report(directory, state.OPUS_PROFILE)
    if no_opus:
        return why, None  # no fallback offered: the reason is the Fable report itself
    opus_why = _plan_report_problem(opus, state.OPUS_PROFILE, envelope)
    if opus_why:
        return opus_why, None
    evidence = state.quota_evidence_problem(fable, envelope["packet"]["head"], packet_hash) if fable else "is missing"
    if evidence:
        return (f"result-{state.OPUS_PROFILE}.json counts only with the quota evidence of this packet: "
                f"result-{state.FABLE_PROFILE}.json {evidence}"), None
    return None, None


def check_plan_review(cfg):
    """The launch of a NEW chain (no wave was ever launched in this run) is refused without both reviews of
    the approved plan: an event and SystemExit with the reason. A chain that already runs is not asked."""
    try:
        why, accepted = _plan_review(cfg)
    except Exception as e:  # noqa: BLE001 - fail closed: the launch never starts on an unchecked plan
        why, accepted = f"the check failed ({type(e).__name__})", None
    if not why and accepted:
        rounds, last = accepted
        event(cfg, f"plan review: Fable rounds exhausted ({rounds}), Astra-only pass accepted; "
                   f"last Fable report: {PLAN_REVIEW_DIR}/{last}")
    if why:
        text = (f"launch refused: plan review: {why}. A new chain needs gate_ready reviews of the approved plan "
                f"by claude-host and codex-host in {cfg['run_dir'] / PLAN_REVIEW_DIR} (references/waves.md, phase A)")
        event(cfg, text)
        raise SystemExit(f"wab: {text}")


NO_MODEL_WARNING = ("warning: chain.json has no `model` and the chain started before 1.4.0: its waves keep the "
                    "default model of the Claude CLI; a chain started on 1.4.0+ runs on \"model\": "
                    f"\"{NEW_DEFAULTS['model']}\" by default (launch is not refused)")


def _check_launch_allowed(cfg, st, wave, name):
    """The chain state decides, before any outside action. No bypass flag."""
    waves, records, cur = cfg["waves"], st["waves"], st.get("current")
    if cur and cur in records:
        phase = records[cur].get("phase")
        if phase in HANDED_OVER:
            nxt = waves[waves.index(cur) + 1] if cur in waves and waves.index(cur) + 1 < len(waves) else None
            if wave != nxt:
                raise SystemExit(f"wab: wave {cur} is {phase}; only {nxt or 'nothing (last wave)'} may follow, "
                                 f"not {wave}; launch refused")
        elif phase in STOPPED:
            if wave != cur:
                raise SystemExit(f"wab: wave {cur} is {phase}; only {cur} may be restarted, not {wave}; "
                                 f"launch refused")
            if tmux_alive(name):
                raise SystemExit(f"wab: wave {cur} is {phase} but its tmux session {name} is alive; "
                                 f"launch refused")
        else:
            raise SystemExit(f"wab: wave {cur} is {phase}; launch refused")
    else:  # nothing current: the first wave that is not finished, in order
        expected = next((w for w in waves if records.get(w, {}).get("phase") not in HANDED_OVER), None)
        if wave != expected:
            raise SystemExit(f"wab: launch refused: expected {expected or 'no wave (the chain is finished)'}, "
                             f"not {wave}")
    if wave != cur and records.get(wave, {}).get("phase") in HANDED_OVER:
        raise SystemExit(f"wab: wave {wave} is already {records[wave]['phase']}; launch refused")


# Visible = a letter, digit, punctuation or symbol (categories L*, N*, P*, S*) that is not a filler
# drawn as blank: combining marks, variation selectors, controls, format and separators alone are empty.
_BLANK_FILLERS = frozenset("\u3164\u2800\u115f\u1160\uffa0\ufffc\ufffd\U0001d159")


def _visible(c):
    return unicodedata.category(c)[0] in "LNPS" and c not in _BLANK_FILLERS


def read_prompt(path):
    """The task text of a prompt file: a readable regular file, valid UTF-8, with at least one
    visible character (BOM, zero-width and control characters alone are empty).
    Anything else is refused with the reason, so a launch never sends only the dispatcher's
    header nor fails after the session and the launch intent already exist."""
    path = pathlib.Path(path)
    try:
        target = path.resolve(strict=True)
    except FileNotFoundError:
        why = "is a broken symlink" if path.is_symlink() else "not found"
        raise SystemExit(f"wab: prompt file {path} {why}; launch refused")
    except (OSError, RuntimeError) as e:  # a symlink loop, no permission on a parent
        raise SystemExit(f"wab: prompt file {path}: cannot resolve the path ({_exc_text(e)}); launch refused")
    if not target.is_file():
        raise SystemExit(f"wab: prompt file {path} is not a regular file; launch refused")
    try:
        raw = target.read_bytes()
    except OSError as e:
        raise SystemExit(f"wab: cannot read prompt file {path} ({e.strerror or type(e).__name__}); launch refused")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SystemExit(f"wab: prompt file {path} is not valid UTF-8 (byte {e.start}); launch refused")
    if not any(_visible(c) for c in text):
        raise SystemExit(f"wab: prompt file {path} is empty (only whitespace or invisible characters); "
                         f"launch refused")
    return text.lstrip("\ufeff").strip()


def _launch(cfg, wave, prompt_file, by_dispatcher=False):
    require_tmux()
    if wave not in cfg["waves"]:
        raise SystemExit(f"unknown wave {wave}; chain.json waves: {cfg['waves']}")
    # the checked text, read BEFORE anything is prepared, saved or started and before the workdir
    # is switched: the file may live in the previous wave's checkout and change or vanish with it
    prompt_text = read_prompt(prompt_file)
    st = load_state(cfg)
    check_identity(cfg, st, "launch")  # a missing pin goes into the intent save below
    pushed = close_pending_windows(cfg, st)  # an entry of the coordinator: even a refused launch pushes
    name = f"{cfg['tmux_prefix']}{wave.lower()}"
    _check_launch_allowed(cfg, st, wave, name)
    if tmux_alive(name):
        raise SystemExit(f"tmux session {name} already exists")
    if wave == cfg["waves"][0] and wave not in st["waves"]:
        warn_stale_chains(cfg)  # a hint about finished chains' leftovers; nothing is closed here
    check_plan_pin(cfg)  # before prepare_clone, the workdir and the STARTING record
    new_chain = not st["waves"]  # a NEW chain: no wave of this run was ever launched
    if new_chain:
        check_plan_review(cfg)
    wdir = wave_dir(cfg, wave)
    cur = st.get("current")
    restart = cur == wave and st["waves"].get(cur, {}).get("phase") in STOPPED
    prompt_copy = wdir / "prompt.md"
    keep_copy = restart and restart_prompt_matches(prompt_copy, prompt_text, prompt_file, wave)
    saved = st.get("admitted_path")
    cwd = prepare_clone(cfg, st)
    # a working copy the chain already used (chain.json workdir, or the clone prepare gave the
    # first wave) still sits on the previous wave's branch: refresh it below; a fresh clone does not
    reused = bool(cfg.get("workdir")) or (bool(saved) and cwd == saved)
    previous_window_closed(cfg, st, wave, restart, pushed, force=by_dispatcher)  # before the workdir is touched
    if reused and cfg["waves"].index(wave) > 0 and not restart:
        refresh_workdir(cfg, wave, cwd)
    prev = st.get("current")
    if prev and prev != wave and st["waves"].get(prev, {}).get("phase") == "awaiting_merge":
        st["waves"][prev]["phase"] = "done"  # merged by the coordinator (or the owner); saved with the launch intent
        st["waves"][prev]["merged_by"] = merged_by(st["waves"][prev])
    idx = cfg["waves"].index(wave)
    before = cfg["waves"][idx - 1] if idx > 0 and not restart else None
    if not by_dispatcher and before and isinstance(st["waves"].get(before), dict):
        ack_wave_notices(st["waves"][before])  # the coordinator launched the next wave: confirmed
    elif by_dispatcher and before and isinstance(st["waves"].get(before), dict):
        # the dispatcher's own launch is no confirmation (its other notices stay), but the end of the
        # previous wave is told and asks nothing: its «done» signal (e.g. of an older version) closes (#55)
        drop_attention(st["waves"][before], "done")
    sid = str(uuid.uuid4())  # known up front: the transcript is bound to the wave by session id
    (wdir / "status").write_text("STARTING\n", encoding="utf-8")  # the wave has not started yet
    system_prompt(cfg)
    # after the last refusal, before the intent: delivery and recovery read only this copy;
    # a restart keeps the copy of the stopped try (checked equal above), it is not rewritten
    if not keep_copy:
        fd, tmp = tempfile.mkstemp(dir=wdir, prefix=".prompt.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(prompt_text + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, prompt_copy)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise
    # the intent is saved BEFORE tmux is touched: a restart finds the wave and its session id
    st["current"] = wave
    st.pop("stopped", None)
    old = st["waves"].get(wave)
    attempts, carried = [], None
    if isinstance(old, dict):
        ack_wave_notices(old)  # a relaunch of this wave confirms its old episodes (dead, not_ready...)
        carried = old.get("outbox")  # only what survives the acknowledgement (chain_done)
        earlier = old.get("attempts")
        attempts = [a for a in earlier if isinstance(a, dict)] if isinstance(earlier, list) else []
        attempts.append({k: v for k, v in old.items() if k not in ("attempts", "outbox")})
        archive = archive_attempt_files(wdir, len(attempts))
        if archive:
            attempts[-1]["archive"] = str(archive)
    st["waves"][wave] = {"tmux": name, "cwd": cwd, "started": time.time(), "restarts": 0,
                         "phase": "launching", "notified": {}, "sessions": [sid],
                         "prompt_file": str(prompt_copy), "prompt_source": str(prompt_file)}
    start_rev = head_rev(cwd)  # after the refresh: the dashboard counts the wave's commits from it
    if start_rev:
        st["waves"][wave]["start_rev"] = start_rev
    if attempts:
        st["waves"][wave]["attempts"] = attempts  # the earlier try is kept, not overwritten
    if restart:  # saved with the intent: the first prompt (and its recovery) says «continue»
        st["waves"][wave]["restart"] = len(attempts) + 1
    if carried:
        st["waves"][wave]["outbox"] = carried
    if isinstance(old, dict):  # a relaunch of a wave switched to Opus on the Fable limit stays on Opus (#108)
        for key in ("model_override", "fable_switches"):
            if key in old:
                st["waves"][wave][key] = old[key]
    if new_chain:  # saved with the first launch intent: from now on the run keeps the defaults of 1.4.0
        st["defaults"] = DEFAULTS_MARK
    warn_model = not cfg["model"] and not st.get("model_warned")
    if warn_model:
        st["model_warned"] = True  # once per run: the mark is saved with the launch intent, the event follows it
    save_state(cfg, st)
    if warn_model:  # after every refusal of this launch: a refused and repeated launch does not repeat it
        event(cfg, NO_MODEL_WARNING)
    start_session(cfg, st, wave)
    delivered = deliver_first_prompt(cfg, st, wave)
    # after the window and its first prompt (#108, Codex P2): the count reads every local journal and is slow on a
    # big ~/.claude/projects; advisory, so neither its time nor its failure may hold or undo the launch
    try:
        fable_usage_event(cfg)
    except Exception:  # noqa: BLE001 - fable_usage_event catches its own errors; this guards the event itself
        pass
    return delivered


FABLE_USAGE_WARNING = "Fable ≥80% недельного бюджета: ревью Fable может упереться в лимит"


def fable_usage_event(cfg):
    """The local Fable count (fable_usage.py, #108) as an event of the launch: advisory, never a refusal. The
    journals are this machine's only (other machines and claude.ai are not seen); a count that could not be
    made is said so, never shown as 0 %."""
    budget = cfg.get("fable_budget_units", FABLE_BUDGET_UNITS)
    try:
        r = fable_usage.usage(projects_dir(), budget=budget)
        if not r.get("valid"):
            event(cfg, f"fable-usage: не посчитан ({r.get('reason') or 'нет данных'})")
            return
        event(cfg, f"fable-usage: сутки {round(r['day_units'], 2)} ед., 7 дней {round(r['week_units'], 2)} из {budget} ед. "
                   f"({r['pct']}%; локально, только эта машина)")
        if r["over_threshold"]:
            event(cfg, f"предупреждение: {FABLE_USAGE_WARNING} (сверь claude.ai/settings/usage)")
    except Exception as e:  # noqa: BLE001 - an advisory count never stops a launch
        event(cfg, f"fable-usage: не посчитан ({type(e).__name__}: {_exc_text(e)})")


def restart_prompt_matches(prompt_copy, prompt_text, prompt_file, wave):
    """A restart (dead / not_ready) continues the stopped try: its manifest, handoff.md and branch
    belong to the task saved in prompt.md, so that copy is the task of the new session too. True:
    the copy exists and the passed file carries the same text (compared as read_prompt normalises
    it: BOM and surrounding whitespace aside), the copy is kept. A different text is refused here,
    before status STARTING and the launch intent: a silent switch would hand the old work a new
    task, a silent ignore would hide that the operator meant another one. False: no copy (an
    interrupted first launch), the passed file becomes the copy as on a first launch."""
    if not (prompt_copy.exists() or prompt_copy.is_symlink()):
        return False
    saved = read_prompt(prompt_copy)  # an unreadable or empty copy is refused with its reason
    if saved != prompt_text:
        raise SystemExit(f"wab: restart of {wave}: prompt file {prompt_file} differs from the saved "
                         f"task {prompt_copy}; a restart continues the stopped try (its manifest, "
                         f"handoff.md and branch) with the same task. Pass {prompt_copy} itself, "
                         f"or, for a new task, close the old try first; launch refused")
    return True


ATTEMPT_FILES = ("next-prompt.md", "result.md")  # file results a DONE of the wave is judged by

# The first message of a restarted wave (dead / not_ready) starts with this: the restart is a
# continuation. The manifest, handoff.md and the branch of the failed try are kept (only
# ATTEMPT_FILES are archived), and the unchanged task in prompt.md would lead to `state.py init`,
# which refuses an existing manifest. Only the manifest forbids init: handoff.md alone (a checkpoint
# during the plan check, before init) means the preparation goes on and the first init follows.
# The note is added on delivery from the `restart` field of the wave record, so prompt.md stays
# the checked copy and a recovery sends the same text.
RESTART_NOTE = ("ПЕРЕЗАПУСК волны {wave} (попытка {n}): прошлая сессия волны остановилась. "
                "Это продолжение, а не новый старт. Есть manifest волны (путь — в "
                "$WAB_DIR/handoff.md, обычно $WAB_DIR/superarmanda/manifest.json) — иди путём "
                "«Продолжение» (state.py where; при tree_matches: false — state.py resume; дальше "
                "next_action), state.py init НЕ вызывай, даже если задача ниже говорит «--plan». "
                "Есть только handoff.md, а manifest ещё нет (контрольная точка до init) — прочитай "
                "handoff.md, продолжи подготовку «Старт» с записанного места и после сверки "
                "одобренного плана выполни первый state.py init. Нет ни handoff.md, ни manifest — "
                "начинай со «Старт». next-prompt.md и result.md прошлой попытки убраны в "
                "attempts/: свои пиши заново.")


def archive_attempt_files(wdir, n):
    """A restart reuses the wave's directory: the file results of the earlier try (ATTEMPT_FILES)
    move to <wave>/attempts/<n>/, so a DONE of the new try without its own next-prompt.md stops
    the chain (_stop_without_next) instead of handing the stale one on. A crash after the move
    and before the intent finds the files already there (same n) and reuses the directory; a
    name already taken there gets a fresh directory, nothing is overwritten. None: nothing to keep."""
    present = [f for f in ATTEMPT_FILES if (wdir / f).exists() or (wdir / f).is_symlink()]
    base = wdir / "attempts" / str(n)
    if not present:
        return base if base.is_dir() else None
    target = base
    if any((base / f).exists() or (base / f).is_symlink() for f in present):
        target = wdir / "attempts" / f"{n}-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    for f in present:
        os.replace(wdir / f, target / f)
    return target


def sync_max_runs(cfg, wave):
    """Keep <wave dir>/max-runs equal to chain.json `max_runs` (one integer line, temp+rename).
    The wave's session reads it in `state.py init --from-plan`, so a cap raised while the wave
    runs reaches the session whose env still holds the old value. Written only when the
    value differs or the file is missing; a failure is an event, never a stop."""
    want = f"{cfg['max_runs']}\n"
    try:
        target = wave_dir(cfg, wave) / "max-runs"
        if read(target) == want.strip():
            return
        tmp = target.with_name(f".max-runs.{os.getpid()}.tmp")
        tmp.write_text(want, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as e:
        event(cfg, f"{wave}: max-runs file not written: {_exc_text(e)}")


def wave_model(cfg, w):
    """The model the wave's window runs on: the switch to Opus on the Fable limit (`model_override`, #108)
    wins over chain.json `model`; None = the CLI default (a legacy chain without `model`)."""
    override = w.get("model_override") if isinstance(w, dict) else None
    return override if isinstance(override, str) and override else cfg.get("model")


def session_model(cfg, w):
    """The model the CURRENT window of the wave was really started on: `model_override` -> `session_model`
    (written by start_session; "" = the CLI default) -> chain.json `model` for a wave started before the
    field. A tunable `model` changed in chain.json after the start does not change the live session (#108)."""
    override = w.get("model_override") if isinstance(w, dict) else None
    if isinstance(override, str) and override:
        return override
    if isinstance(w, dict) and isinstance(w.get("session_model"), str):
        return w["session_model"] or None
    return cfg.get("model")


def start_session(cfg, st, wave, sid=None, phase="starting"):
    """launching -> starting: create the tmux window (same session id on a repeat). `sid`/`phase`: the
    Fable switch (#108) starts its new session with its own saved id and stays in its own phase."""
    w = st["waves"][wave]
    wdir = wave_dir(cfg, wave)
    sync_max_runs(cfg, wave)
    cmd = ["claude", "--permission-mode", "auto", "--append-system-prompt-file", str(system_prompt(cfg)),
           "--name", f"wab-{cfg['chain']}-{wave}", "--session-id", sid or w["sessions"][0]]
    model = wave_model(cfg, w)
    if model:
        cmd += ["--model", model]
    w["session_model"] = model or ""  # saved with the phase below: the limit detection reads it (#108)
    pin = ["-e", f"WAB_PLAN_SHA256={cfg['plan_sha256']}"] if "plan_sha256" in cfg else []
    # read by `state.py init` of the wave; always set, empty without a policy (= the built-in one): the global
    # environment of the tmux server could otherwise hand the window someone else's policy (#108)
    policy = cfg.get("role_models")
    pin += ["-e", f"{gate.state.ROLE_MODELS_ENV}="
                  f"{json.dumps(policy, sort_keys=True, separators=(',', ':')) if policy else ''}"]
    made = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", w["tmux"], "-c", w["cwd"], "-x", "220", "-y", "60",
                "-e", f"WAB_DIR={wdir}", "-e", f"WAB_WAVE={wave}", "-e", f"WAB_MAX_RUNS={cfg['max_runs']}",
                "-e", f"SUPERARMANDA_TZ={humantime.resolve(cfg.get('timezone'))[0]}", *pin, *cmd)
    mark_owner(cfg, w["tmux"], (made.stdout or "").strip())
    w["phase"] = phase
    save_state(cfg, st)


def _plan_pin_refused(cfg, st, wave, drop_pending=False):
    """A pinned plan that changed while the launch was half done (the dispatcher died after the
    intent was saved): no session, no prompt. Phase not_ready (the chain stops, a `launch`
    retries through _launch, which checks the pin again), the reason in status, event, notice."""
    try:
        check_plan_pin(cfg)
    except SystemExit as e:
        why = _exc_text(e)
        w = st["waves"][wave]
        # a live session holds a typed or pending stale prompt: Enter in it would start the
        # unapproved plan, and a corrected `launch` is refused while the session exists
        closed, left = "", ""
        if tmux_alive(w["tmux"]):
            tmux("kill-session", "-t", session_target(w["tmux"]), check=False)
            if tmux_alive(w["tmux"]):
                left = f"сессия не закрыта, закрой вручную: tmux kill-session -t {w['tmux']}"
            else:
                closed = "сессия закрыта"
        (wave_dir(cfg, wave) / "status").write_text(why + "\n", encoding="utf-8")
        w["phase"] = "not_ready"
        if drop_pending:
            if left:  # the session lives on: its input holds the stale prompt, clear it (screen-checked)
                _abandon_input(cfg, st, wave, w, "the plan pin refused, the session was not closed")
            else:  # no session: nothing to clear
                w.pop("pending_enter", None)
                w.pop("pending_text_head", None)
        extra = f" ({closed or left})" if closed or left else ""
        if once_per(w, "not_ready", "plan_pin"):
            put_notice(cfg, w, wave, "not_ready", "plan_pin", reason="plan_pin", question=why)
        save_state(cfg, st)
        event(cfg, f"{wave}: {why}; session/prompt NOT started"
                   + (f"; session closed" if closed else f"; {left}" if left else ""))
        flush_notices(cfg, st, w)
        return True
    return False


def _session_env(name, var):
    """The value of `var` in the environment of the tmux session (what `new-session -e` gave it);
    None when unset or unreadable."""
    try:
        r = tmux("show-environment", "-t", session_target(name), var, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out.partition("=")[2] if r.returncode == 0 and out.startswith(var + "=") else None


def _mark_recovered(cfg, name, wave):
    """Marks for a live session found by recovery: only when it is CONFIRMED unmarked (not unknown, not
    foreign, not already ours), its environment is this wave's (WAB_DIR and WAB_WAVE, as start_session
    passed them: a stranger's session of the same name has neither) and it has exactly one pane,
    confirmed unmarked too; that pane is marked by id."""
    who = _ours(cfg, session=name)
    if who == "ours":
        return
    if who == "unmarked" and not (_norm_dir(_session_env(name, "WAB_DIR")) == _norm_dir(wave_dir(cfg, wave))
                                  and _session_env(name, "WAB_WAVE") == wave):
        event(cfg, f"{name}: not marked: session environment is not this wave's; cleanup will only warn")
        return
    panes = _list_panes(name) if who == "unmarked" else None
    if who == "unmarked" and panes is not None and len(panes) == 1 and _ours(cfg, pane=panes[0][0]) == "unmarked":
        mark_owner(cfg, name, panes[0][0])
    else:
        event(cfg, f"{name}: not marked as this run's (state: {who}, "
                   f"panes: {'unknown' if panes is None else len(panes)}); cleanup will only warn")


def recover_launch(cfg, st, wave):
    """Dispatcher died around `tmux new-session`: a live window means it was started,
    otherwise start it again with the same session id. Then carry on as `starting`."""
    w = st["waves"][wave]
    if _plan_pin_refused(cfg, st, wave):
        return
    if tmux_alive(w["tmux"]):
        event(cfg, f"{wave}: resumed in phase 'launching': window exists, not starting another")
        _mark_recovered(cfg, w["tmux"], wave)  # the dispatcher may have died between new-session and the marks
        w["phase"] = "starting"
        save_state(cfg, st)
    else:
        event(cfg, f"{wave}: resumed in phase 'launching': window missing, starting it")
        start_session(cfg, st, wave)
    if w.get("prompt_file") and pathlib.Path(w["prompt_file"]).is_file():
        deliver_first_prompt(cfg, st, wave)


class _WindowGone(Exception):
    """The wave's tmux session vanished while we were acting on it."""


def _mark_dead(cfg, st, wave, status=""):
    """The one `window closed` branch: phase dead, event and a single notice."""
    w = st["waves"][wave]
    fresh = once_per(w, "dead", "1")
    w["phase"] = "dead"
    if fresh:
        put_notice(cfg, w, wave, "dead", "1", wave_status=status, question=status)
    save_state(cfg, st)  # the phase, the mark and the notice together
    if fresh:
        event(cfg, f"{wave}: tmux session {w['tmux']} is gone (status={status})")
    flush_notices(cfg, st, w)


def _act(cfg, st, wave, what, fn, *args, **kw):
    """Run one outside tmux action. True when done. Window gone -> phase dead and
    _WindowGone. Window alive but the command failed -> one event and one notice, False;
    the intent phase is already on disk, so the next tick (or a restart) decides."""
    w = st["waves"][wave]
    try:
        fn(*args, **kw)
    except (subprocess.CalledProcessError, OSError, NotSubmitted) as e:
        if not tmux_alive(w["tmux"]):
            _mark_dead(cfg, st, wave, read(cfg["run_dir"] / wave / "status"))
            raise _WindowGone() from e
        fresh = once_per(w, "tmux_failed", what)
        if fresh:
            put_notice(cfg, w, wave, "tmux_failed", what, reason=action_reason(what))
        save_state(cfg, st)
        event(cfg, f"{wave}: tmux action '{what}' failed ({type(e).__name__}); the window is alive")
        flush_notices(cfg, st, w)
        return False
    drop_notice(w, "tmux_failed")
    return True


INPUT_LOCK_SECONDS = 120  # longest paste + submit with retries; then the writer gives up


class _InputLock:
    """Short per-window input lock (`<run_dir>/<wave>/input.lock`): one writer types into a wave's
    input at a time — the dispatcher's deliveries and `wab.py say` — so two texts never land in the
    same input line before either Enter. Not the run lock: `watch` keeps running while `say` types."""

    def __init__(self, cfg, wave):
        self.path = cfg["run_dir"] / wave / "input.lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.time() + INPUT_LOCK_SECONDS
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    fh.close()
                    raise NotSubmitted(f"input of the wave is busy (another writer holds {self.path})")
                time.sleep(0.5)
        self.fh = fh
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _deliver(cfg, st, wave, what, send, text, precheck=None):
    """Two-step send (text, then Enter) without duplicates: once the text is in the window
    that fact is saved (`pending_enter`), and a retry after a failed Enter presses only Enter.
    A pasted text counts as delivered only when the screen shows it left the input line (submit,
    inside send_text); otherwise `pending_enter` stays and the retry is Enter-only, checked the same way."""
    w = st["waves"][wave]
    name = w["tmux"]
    try:
        lock = _InputLock(cfg, wave).__enter__()
    except NotSubmitted as e:  # `say` is typing right now: nothing sent, the next tick retries
        event(cfg, f"{wave}: {what} postponed: {_exc_text(e)}")
        return False
    try:
        if w.get("pending_enter") == IDLE_NUDGE_WHAT and what != IDLE_NUDGE_WHAT:
            # an undelivered idle nudge is the lowest priority: it is cleared out of the input (by the
            # screen) before any other text is typed, never typed over
            left = _abandon_nudge(cfg, st, wave, w, f"{what} takes over from an undelivered idle nudge",
                                  locked=True)
            if left == "foreign":
                return False  # the owner's text is in the input: nothing is typed over it, nothing cleared
            if left:
                if once_per(w, "input_postponed", f"nudge:{what}"):
                    event(cfg, f"{wave}: {what} postponed: the undelivered idle nudge is not cleared yet")
                return False
        if w.get("pending_clear") and not _settle_clear(cfg, st, wave, w, locked=True):
            # an earlier text may still sit in the input line: nothing is typed over it
            if once_per(w, "input_postponed", str(w["pending_clear"].get("what"))):
                event(cfg, f"{wave}: {what} postponed: the input line is not cleared yet")
            return False
        w.get("notified", {}).pop("input_postponed", None)
        try:
            fresh = precheck() if precheck is not None else True
        except gate.CollectError as e:  # the facts could not be read: neither typed, nor Enter, nor clearing
            if once_per(w, "precheck_error", f"{what}: {_exc_text(e)}"[:150]):
                event(cfg, f"{wave}: {what} postponed: facts not collected under the input lock: {_exc_text(e)}")
            return False
        w.get("notified", {}).pop("precheck_error", None)
        if not fresh:
            # checked under the lock, right before typing or pressing Enter: stale, not sent. A typed
            # text waiting for its Enter (the retry) must not stay in the input line
            if w.get("pending_enter") == what:
                _abandon_input(cfg, st, wave, w, f"{what}: stale before its Enter", locked=True)
            return None
        if w.get("pending_enter") == what:
            if send is send_text:
                # after a restart the caller may not have the text any more: check against the head
                # saved together with `pending_enter`, so recovery validates the same way
                check = text or str(w.get("pending_text_head") or "")
                if not check and once_per(w, "unverified_enter", what):
                    # state of an older version: nothing to compare with — the Enter is pressed as
                    # before, and the owner is told that this delivery is NOT verified
                    put_notice(cfg, w, wave, "unverified_enter", what, reason=action_reason(what))
                    event(cfg, f"{wave}: {what} (Enter): delivery NOT verified (no saved text head)")
                ok = _act(cfg, st, wave, f"{what} (Enter)", submit, name, check)
            else:
                ok = _act(cfg, st, wave, f"{what} (Enter)", press_enter, name)
        else:
            def typed():
                w["pending_enter"] = what
                w["pending_text_head"] = _text_head(text)  # what unsent_reason compares; short
                save_state(cfg, st)
            ok = _act(cfg, st, wave, what, send, name, text, on_typed=typed)
    finally:
        lock.__exit__(None, None, None)
    if ok:
        w.pop("pending_enter", None)
        w.pop("pending_text_head", None)
    return ok


def _abandon_input(cfg, st, wave, w, why, locked=False, target=None):
    """The single point where a typed text that is waiting for its Enter (`pending_enter`) is given up
    WITHOUT delivery. It is not just forgotten: the fact moves to the reserve `pending_clear`, the input
    line is cleared and the SCREEN must show it empty (_settle_clear). Until then nothing new is typed
    into this window (_deliver, `say`). `locked`: the caller already holds the window's input lock."""
    what = w.get("pending_enter")
    if what:
        if what == "policy answer" and "policy_key_pending" in w:
            _keep_fork_key(w)  # a fixed answer (#89) given up, possibly submitted: its fork counts as answered
        w["pending_clear"] = {"what": what, "why": why}
        w.pop("pending_enter", None)
        w.pop("pending_text_head", None)
        save_state(cfg, st)
    return _settle_clear(cfg, st, wave, w, locked=locked, target=target)


def _settle_clear(cfg, st, wave, w, locked=False, target=None):
    """Try to finish the clearing reserved in `pending_clear`; True when nothing is reserved any more.
    Success: the screen shows an empty input (event `input cleared`). Failure: the reserve stays and the
    next tick tries again; one `input NOT cleared` event per episode and reason. A gone window needs
    no clearing (no keys)."""
    reserve = w.get("pending_clear")
    if not isinstance(reserve, dict):
        w.pop("pending_clear", None)
        return True
    what, why = str(reserve.get("what")), str(reserve.get("why"))
    name = w.get("tmux")
    if not name or not tmux_alive(name):
        w.pop("pending_clear", None)
        save_state(cfg, st)
        event(cfg, f"{wave}: input clearing dropped: no window ({what}; {why})")
        return True
    try:
        if locked:
            left = clear_input(target or name)
        else:
            with _InputLock(cfg, wave):
                left = clear_input(target or name)
    except NotSubmitted as e:
        left = _exc_text(e)
    except (subprocess.SubprocessError, OSError) as e:
        left = type(e).__name__
    if left is None:
        w.pop("pending_clear", None)
        w.get("notified", {}).pop("input_clear", None)
        save_state(cfg, st)
        event(cfg, f"{wave}: input cleared: {what} ({why})")
        return True
    if once_per(w, "input_clear", f"{what}: {left}"):
        event(cfg, f"{wave}: input NOT cleared: {what}: {left} ({why})")
    save_state(cfg, st)
    return False


def first_prompt_head(cfg, wave, w):
    """The dispatcher's head of the first message of a wave (marker, wave dir, admitted clone, restart note)."""
    head = (f"{session_marker(cfg, wave)} [wave-autobot] Волна {wave}. Каталог волны: {wave_dir(cfg, wave)} "
            f"(он же $WAB_DIR). Рабочая копия (admitted clone): {w['cwd']}. "
            f"Протокол — в системной инструкции.\n\n")
    n = w.get("restart")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        head += RESTART_NOTE.format(wave=wave, n=n) + "\n\n"
    return head


def deliver_first_prompt(cfg, st, wave):
    """starting -> sending -> running. The phase is saved before the paste: a dispatcher
    that dies in between leaves `sending`, which is never resent blindly."""
    w = st["waves"][wave]
    name, wdir = w["tmux"], wave_dir(cfg, wave)
    if _plan_pin_refused(cfg, st, wave):
        return False
    if not wait_ready(name):
        blocked = "BLOCKED: окно Claude не стало готовым, задача не отправлена\n"
        (wdir / "status").write_text(blocked, encoding="utf-8")
        w["phase"] = "not_ready"
        put_notice(cfg, w, wave, "not_ready", "1", reason="window_not_ready")
        save_state(cfg, st)
        event(cfg, f"{wave}: Claude TUI not ready in {name}, prompt NOT sent")
        flush_notices(cfg, st, w)
        return False
    # the wait above lasts up to 90 s: waves.json may have changed meanwhile
    if _plan_pin_refused(cfg, st, wave):
        return False
    prompt = pathlib.Path(w["prompt_file"]).read_text(encoding="utf-8").strip()
    head = first_prompt_head(cfg, wave, w)
    (wdir / "first-prompt.md").write_text(head + prompt + "\n", encoding="utf-8")
    w["phase"] = "sending"
    save_state(cfg, st)
    try:
        if not _deliver(cfg, st, wave, "first prompt", send_text, head + prompt):
            return False  # phase `sending` stays on disk; a restart will not resend blindly
    except _WindowGone:
        return False
    _started(cfg, st, wave)
    return True


def _started(cfg, st, wave):
    """sending -> running after the task reached the window: the phase, the «started» mark and its
    notice in one save, then the event. The normal path and the Enter-only recovery share it."""
    w = st["waves"][wave]
    w["phase"] = "running"
    if once_per(w, "started", "1"):
        put_notice(cfg, w, wave, "started", "1")
    save_state(cfg, st)
    event(cfg, f"{wave}: launched in tmux {w['tmux']}, cwd {w['cwd']}")
    flush_notices(cfg, st, w)


# ---------- supervision ----------

def once_per(w, key, value):
    """True the first time `value` is seen under `key` (dedup for notifications)."""
    notified = w.setdefault("notified", {})
    if notified.get(key) == value:
        return False
    notified[key] = value
    return True


def resume(cfg, st):
    close_pending_windows(cfg, st)
    try:
        advance_pending(cfg, st)
    except _WindowGone:
        pass  # recorded as dead; tick follows


EXIT_WAIT = 30  # seconds the next launch waits for the previous wave's window after /exit
# Claude Code answers /exit of a session with background tasks (shells, agents) with a menu:
# «❯ 1. Exit and stop tasks / 2. Move to background and exit / 3. Stay». A finished wave owes
# nothing to them: option 1, confirmed by Enter only while it is the highlighted one.
EXIT_DIALOG = "Exit and stop tasks"


_EXIT_OPTIONS = (re.compile(r"^\s*(\u276f\s*)?1\.\s+Exit and stop tasks\s*$"),
                 re.compile(r"^\s*(\u276f\s*)?2\.\s+Move to background and exit\s*$"),
                 re.compile(r"^\s*(\u276f\s*)?3\.\s+Stay\s*$"))


def exit_dialog(text):
    """None: no live /exit menu; True: it is there with option 1 highlighted (Enter takes it);
    False: it is there, but another option is highlighted (Enter would not exit). Recognised only as
    one bounded menu at the very bottom: the «Enter to confirm» hint is the last non-empty line and
    the three non-empty lines right above it are exactly the numbered options, in order. A menu
    quoted in the output, or one with another dialog or the input box below it, is not the live one."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 4 or "Enter to confirm" not in lines[-1]:
        return None
    opts = lines[-4:-1]
    if not all(rx.match(l) for rx, l in zip(_EXIT_OPTIONS, opts)):
        return None
    return INPUT_MARK in opts[0]


def _window_state(cfg, name):
    """Where may /exit of a finished wave go? (kind, target):
    gone (no session) | closed (the session is ours but none of its panes is: the wave's window is closed) |
    foreign (marked by another run) | unknown (panes could not be listed) |
    legacy (no @wab_run at all, wab <= 1.0.2: the session's active pane, as before, target = name) |
    ours (target = the id of OUR pane, found by _ours, never the active pane of a shared session)."""
    if not (isinstance(name, str) and name and tmux_alive(name)):
        return "gone", None
    who = _ours(cfg, session=name)
    if who == "unmarked":
        return "legacy", name
    if who == "foreign":
        return "foreign", None
    if who == "unknown":
        return "unknown", None
    panes = _list_panes(name)
    if panes is None:
        return "unknown", None
    states = [(pane, _ours(cfg, pane=pane)) for pane, _ in panes]
    mine = [pane for pane, st_ in states if st_ == "ours"]
    if mine:
        return "ours", mine[0]
    return ("unknown", None) if any(st_ == "unknown" for _, st_ in states) else ("closed", None)


def close_window(cfg, st, w):
    """Carry out a saved intent to close a finished wave's window (`pending_exit`): /exit while
    the window is alive; the flag is dropped (and saved) once the window is gone. Repeatable, so
    a dispatcher that died between the save and /exit sends it after the restart. /exit is typed
    under the window's input lock and only into an EMPTY input line: a text that is still waiting for
    its Enter (`pending_enter`, `pending_clear`) or shows on the screen is cleared first (as everywhere:
    _abandon_input), otherwise /exit would be appended to it and sent together. Not cleared: no /exit,
    an event with the reason, the intent stays for the next tick."""
    if not isinstance(w, dict) or not w.get("pending_exit"):
        return False
    name = w.get("tmux")
    kind, target = _window_state(cfg, name)
    if kind == "foreign":
        event(cfg, f"{name}: the session is marked by another run; no /exit sent, the intent dropped")
    if kind == "unknown":
        if once_per(w, "exit_blocked", "panes not listed"):
            event(cfg, f"{name}: /exit not sent: panes could not be listed; retried next tick")
        return False
    if kind in ("legacy", "ours"):
        wave = next((k for k, r in (st.get("waves") or {}).items() if r is w), None) or name
        try:
            with _InputLock(cfg, wave):
                menu = exit_dialog(pane_text(target))
                if menu is not None:  # our /exit is being asked about the wave's background tasks
                    if not menu:
                        if once_per(w, "exit_blocked", "exit menu: option 1 not highlighted"):
                            event(cfg, f"{wave}: /exit menu on screen, option 1 not highlighted; "
                                       f"not confirmed, retried next tick")
                        save_state(cfg, st)
                        return False
                    press_enter(target)
                    event(cfg, f"{wave}: /exit menu confirmed: exit and stop the wave's background tasks")
                    return True
                if w.get("pending_enter"):
                    _abandon_input(cfg, st, wave, w, "the window is being closed", locked=True, target=target)
                elif w.get("pending_clear"):
                    _settle_clear(cfg, st, wave, w, locked=True, target=target)
                if w.get("pending_clear"):
                    left = "the earlier input is not cleared"
                else:
                    left = clear_input(target)  # None: empty now; «no input box» (a dialog, a failed capture) is
                    # NOT a go: the Enter after /exit could press a button of the dialog; held, retried
                if left is not None:
                    if once_per(w, "exit_blocked", left):
                        event(cfg, f"{wave}: /exit not sent: {left}; retried next tick")
                    save_state(cfg, st)
                    return False
                w.get("notified", {}).pop("exit_blocked", None)
                if kind == "ours" and _ours(cfg, pane=target) != "ours":  # re-read right before typing
                    event(cfg, f"{wave}: pane {target} is no longer ours; /exit not sent")
                    return False
                tmux("send-keys", "-t", pane_target(target), "-l", "/exit", check=False)
                tmux("send-keys", "-t", pane_target(target), "Enter", check=False)
        except (NotSubmitted, subprocess.SubprocessError, OSError) as e:  # a vanished pane, a busy input
            why = _exc_text(e) if isinstance(e, NotSubmitted) else type(e).__name__
            if once_per(w, "exit_blocked", why):
                event(cfg, f"{wave}: /exit not sent: {why}; retried next tick")
            return False
        return True
    w.pop("pending_exit", None)
    save_state(cfg, st)
    return False


def close_pending_windows(cfg, st):
    """Every entry of the coordinator (watch/resume, each tick, launch, done) pushes the saved
    intents: /exit to every live window with `pending_exit`, the flag dropped where the window is
    gone. Returns the names /exit was sent to, so a caller that waits does not send it twice."""
    pushed = set()
    for rec in list((st.get("waves") or {}).values()):
        if isinstance(rec, dict) and rec.get("pending_exit") and close_window(cfg, st, rec):
            pushed.add(rec.get("tmux"))
    return pushed


def wait_window_closed(cfg, st, w, push=True):
    """Wait up to EXIT_WAIT for the window of a finished wave `w` to go. True when it is gone (the
    flag is dropped in `w`, the caller saves it); False when it still lives: nobody asked it to
    close (no pending_exit) or it did not within EXIT_WAIT. `push`: send /exit first."""
    name = w.get("tmux")
    for attempt in range(EXIT_WAIT + 1):
        kind, target = _window_state(cfg, name)
        if kind in ("gone", "closed", "foreign"):  # no window of ours is left (foreign: close_window says so)
            if kind == "foreign" and w.get("pending_exit"):
                event(cfg, f"{name}: the session is marked by another run; no /exit sent, the intent dropped")
            w.pop("pending_exit", None)
            return True
        if not w.get("pending_exit") or attempt == EXIT_WAIT:
            return False
        if attempt == 0 and push:
            close_window(cfg, st, w)
        elif attempt and kind in ("legacy", "ours") and exit_dialog(pane_text(target)) is not None:
            close_window(cfg, st, w)  # the /exit menu came up after our /exit: confirm it (or log why not)
        time.sleep(1)
    return False


def previous_window_closed(cfg, st, wave, restart, pushed=(), force=False):
    """The wave before `wave` must not still run in its window when the shared workdir is switched
    under it: a pending /exit is pushed (unless it is in `pushed` already) and waited for
    (EXIT_WAIT), else the launch is refused. `force` (the dispatcher's own launch after a confirmed
    DONE): a window of a `done` wave that outlived the wait is closed with kill-session instead of
    stopping the chain for hours — its work is merged, only leftovers (a stuck menu, background
    shells) keep it open."""
    idx = cfg["waves"].index(wave)
    if idx == 0 or restart:
        return
    before = st["waves"].get(cfg["waves"][idx - 1])
    if not isinstance(before, dict) or not isinstance(before.get("tmux"), str):
        return
    name = before["tmux"]
    if wait_window_closed(cfg, st, before, push=name not in pushed):
        return  # the dropped flag is saved with the launch intent
    if force and before.get("phase") == "done":
        tmux("kill-session", "-t", session_target(name), check=False)
        if not tmux_alive(name):
            before.pop("pending_exit", None)
            event(cfg, f"{wave}: previous wave session {name} did not obey /exit within {EXIT_WAIT} s; "
                       f"closed with kill-session (the wave is done)")
            return
    raise SystemExit(f"wab: wave {wave} refused: previous wave session {name} is still running; "
                     f"wait or close it ({attach_cmd(name)})")


def advance_pending(cfg, st):
    """Carry an unfinished transition of the current wave forward from the phase on disk,
    without relaunching. Called at watch start AND by every tick for the phases that tick
    does not drive itself (starting, sending)."""
    wave = st.get("current")
    if not wave or wave not in st["waves"]:
        return
    w = st["waves"][wave]
    name, phase = w["tmux"], w.get("phase")
    if phase == "launching":
        recover_launch(cfg, st, wave)
    elif phase == "starting" and tmux_alive(name):
        event(cfg, f"{wave}: resumed in phase 'starting': waiting for the TUI, then sending the task")
        if w.get("prompt_file") and pathlib.Path(w["prompt_file"]).is_file():
            deliver_first_prompt(cfg, st, wave)
        else:
            event(cfg, f"{wave}: prompt file is gone, task NOT sent")
            if once_per(w, "no_prompt", "1"):
                put_notice(cfg, w, wave, "no_prompt", "1")
            save_state(cfg, st)
            flush_notices(cfg, st, w)
    elif phase == "sending" and w.get("pending_enter") == "first prompt":
        if _plan_pin_refused(cfg, st, wave, drop_pending=True):  # typed text must not start the wave
            return
        event(cfg, f"{wave}: resumed in phase 'sending': the text is typed, pressing only Enter")
        if _deliver(cfg, st, wave, "first prompt", send_text, ""):
            _started(cfg, st, wave)
    elif phase == "sending":
        event(cfg, f"{wave}: resumed in phase 'sending': unknown whether the task was delivered, "
                   f"NOT resent")
        w["phase"] = "running"
        if once_per(w, "sending", "1"):
            put_notice(cfg, w, wave, "sending", "1")
        save_state(cfg, st)
        flush_notices(cfg, st, w)
    elif phase == "updating":
        recover_update(cfg, st, wave)
    elif phase == "dead" and tmux_alive(name):
        w["phase"] = "running"  # the save drops the dead notice (NOTICE_EPISODE_ENDS)
        save_state(cfg, st)
        event(cfg, f"{wave}: window is back, supervision resumed")


def _status_mtime(cfg, wave):
    try:
        return (cfg["run_dir"] / wave / "status").stat().st_mtime_ns
    except OSError:
        return None


def handoff_ready(cfg, w, wave, status):
    """A HANDOFF_READY that the wave wrote AFTER the last transition. The transition leaves
    the old status file in place (it is the wave's), so an unchanged mtime means stale."""
    if status != "HANDOFF_READY":
        w.pop("resuming", None)
        w.pop("resuming_mtime", None)
        return False
    if w.get("resuming"):
        if _status_mtime(cfg, wave) == w.get("resuming_mtime"):
            return False
        w.pop("resuming", None)
        w.pop("resuming_mtime", None)
    return True


RESUME_WHAT = "/superarmanda --resume"  # step label for _deliver / pending_enter
LEGACY_RESUME_WHATS = ("/update",)  # the label written by dispatchers before 0.15.0: recover_update still reads it


def finish_update(cfg, st, wave):
    """Last step of the /clear + resume transition: one restart counted. `status` belongs to
    the wave and is not written; the stale HANDOFF_READY on disk is remembered by its mtime."""
    w = st["waves"][wave]
    w["restarts"] = w.get("restarts", 0) + 1
    w["phase"] = "running"
    w["checkpoint_at"] = None
    w["resuming"] = True
    w["resuming_mtime"] = _status_mtime(cfg, wave)
    save_state(cfg, st)


def resume_message(cfg, wave, wdir):
    """The first message of the new session after /clear: the skill's resume entry. The marker stays
    in the text: the new session is bound by it (find_new_session). The position is not told here:
    the session takes it from `state.py where` by the manifest named in handoff.md."""
    return (f"/superarmanda --wave {wave} --resume {session_marker(cfg, wave)} Каталог волны: {wdir}. "
            f"Позиция — state.py where по manifest из {wdir}/handoff.md.")


def recover_update(cfg, st, wave):
    """The dispatcher died while the resume message was in flight: whether it arrived is unknown, so it
    is NOT resent. The wave goes on with await_session set; the owner is told once."""
    w = st["waves"][wave]
    if w.get("pending_enter") in LEGACY_RESUME_WHATS + (RESUME_WHAT,):
        # typed, only Enter is missing: finish it, nothing is unknown ("/update" — state of an older version)
        if _deliver(cfg, st, wave, w["pending_enter"], send_text, ""):
            w["await_session"] = True
            finish_update(cfg, st, wave)
        return
    fresh = once_per(w, "updating", str(w.get("restarts", 0)))
    informed = bool(w.get("notified", {}).get("tmux_failed"))  # the owner already got the failure notice
    w["await_session"] = True
    # the wave already wrote something new (RUNNING, BLOCKED, DONE...): the resume message did arrive
    arrived = read(cfg["run_dir"] / wave / "status") not in ("HANDOFF_READY", "", "STARTING")
    event(cfg, f"{wave}: resumed in phase 'updating': "
               f"{'resume evidently arrived' if arrived else 'unknown whether resume arrived'}, NOT resent")
    if fresh and not arrived and not informed:  # keyed by the restart count BEFORE finish_update
        put_notice(cfg, w, wave, "updating", str(w.get("restarts", 0)))
    finish_update(cfg, st, wave)  # saves the phase, the mark and the notice together
    flush_notices(cfg, st, w)


# ---------- the Fable limit of a wave window -> the same wave on Opus (1.4.0, #108) ----------
#
# A wave window that runs on Fable and gets the subscription limit (a main-thread assistant line of its
# transcript with error "rate_limit" or apiErrorStatus 429, written by Claude Code itself; the text is not
# read) would stand still for hours. The dispatcher closes that window and starts a NEW session of the same
# wave on Opus with the resume message: the chain goes on. Phase `switching`, its steps in `fable_switch`
# {sid, step}: closing -> starting -> ready -> sending; every step is saved before its outside action, so a
# dispatcher that dies anywhere carries it on without a second window, a second session id or a blind resend.
# It is not a restart of the wave: `restarts`, the attempts and `max_runs` stay untouched (`fable_switches`
# counts it). Once per wave: `model_override` is Opus afterwards, a limit of Opus takes the ordinary idle /
# ATTENTION path. `/model` is never typed into the TUI (it would change the owner's global default).

FABLE_SWITCH_WHAT = "fable switch resume"  # step label for _deliver / pending_enter
FABLE_SWITCH_TO = gate.state.OPUS_MODEL
FABLE_SWITCH_NOTE = "модель переключена на Opus из-за лимита Fable; отметь это в PR"


def fable_limit_hit(cfg, w):
    """True when the wave runs on Fable and the transcript of its CURRENT session holds a provider refusal of
    the main thread (TranscriptCache `limit`). Subagents (sidechain lines) run on their own role models."""
    model = session_model(cfg, w)  # an accepted selector ("fable") is the same model (Codex P2 on #109)
    if gate.state.MODEL_ALIASES.get(model, model) != gate.state.FABLE_MODEL:
        return False
    sessions = w.get("sessions") or []
    if not sessions:
        return False
    return CACHE.read(transcript_path(w["cwd"], sessions[-1]))["limit"] > 0


def fable_switch_message(cfg, wave, w, wdir):
    """The first message of the Opus session; chosen once the old window is closed (nothing writes the wave
    files any more) and saved in `fable_switch.text`, so a resumed dispatcher sends the same text. The limit
    may come before the first WAB-CHECKPOINT, when handoff.md does not exist (Codex P1 on #109):
    - a manifest of the current run exists -> resume with its explicit path (`--manifest`), whatever
      handoff.md says (a checkpoint before `init` leaves a handoff.md without the manifest);
    - handoff.md exists, or runs.json names a run the choice refused -> the ordinary resume message;
    - neither: the wave wrote nothing to resume from -> the wave's first message again (same head, same task)."""
    path, why = current_manifest(cfg, wave)
    if path is not None and path.is_file():
        text = (f"/superarmanda --wave {wave} --resume {session_marker(cfg, wave)} --manifest {path} "
                f"Каталог волны: {wdir}. Manifest волны уже есть: позиция — state.py where --manifest {path}; "
                f"state.py init не вызывай. handoff.md, если есть, прочитай для контекста.")
    elif (wdir / "handoff.md").exists() or path is None:
        text = resume_message(cfg, wave, wdir)
    else:
        first = wdir / "first-prompt.md"
        try:
            text = first.read_text(encoding="utf-8").strip() if first.is_file() else ""
        except (OSError, ValueError):
            text = ""
        if session_marker(cfg, wave) not in text:
            try:
                prompt = pathlib.Path(w["prompt_file"]).read_text(encoding="utf-8").strip()
            except (KeyError, TypeError, OSError, ValueError):
                prompt = None
            text = first_prompt_head(cfg, wave, w) + prompt if prompt else resume_message(cfg, wave, wdir)
    return text + "\n" + FABLE_SWITCH_NOTE


def begin_fable_switch(cfg, st, wave, w):
    """The decision, in ONE save: the override, the counter, the new session id and the phase."""
    w["model_override"] = FABLE_SWITCH_TO
    w["fable_switches"] = (w["fable_switches"] if _count(w.get("fable_switches")) else 0) + 1
    w["fable_switch"] = {"sid": str(uuid.uuid4()), "step": "closing", "from": (w.get("sessions") or [None])[-1]}
    w["phase"] = "switching"
    save_state(cfg, st)
    event(cfg, f"fable limit: {wave} → relaunch on {FABLE_SWITCH_TO}")


def _fable_switch_done(cfg, st, wave, w, how):
    sid = (w.get("fable_switch") or {}).get("sid")
    w["phase"] = "running"
    w.pop("fable_switch", None)
    w["checkpoint_at"] = None  # the new session starts with a fresh context: no checkpoint of the old one
    w.pop("checkpoint_sent", None)
    w.pop("ctx_watch", None)
    w["tokens"] = 0
    save_state(cfg, st)
    event(cfg, f"{wave}: new session {sid} on {wave_model(cfg, w)} after the Fable limit: {how}")
    return True


def _fable_switch_unconfirmed_tick(cfg, st, wave, w, sw):
    """Step `unconfirmed`: the resume message may sit unsent in the input of the new window. Nothing is sent
    blindly; the switch is done once the marker shows up in the new session's journal (the owner pressed
    Enter, or the text did arrive). The window gone -> the ordinary dead window."""
    if session_marker(cfg, wave) in _first_user_text(transcript_path(w["cwd"], sw["sid"])):
        return _fable_switch_done(cfg, st, wave, w, "resume delivered")
    if not tmux_alive(w["tmux"]):
        _mark_dead(cfg, st, wave, read(wave_dir(cfg, wave) / "status"))
        return False
    return True


def _fable_switch_tick(cfg, st, wave, w):
    """Carry the switch on from its saved step (see above). False only when the chain stops (not_ready)."""
    sw = w.get("fable_switch")
    name, wdir = w["tmux"], wave_dir(cfg, wave)
    if not (isinstance(sw, dict) and isinstance(sw.get("sid"), str) and sw.get("step") in
            ("closing", "starting", "ready", "sending", "unconfirmed")):
        event(cfg, f"{wave}: fable switch record is broken ({safe_text(sw, 200)}); supervision resumed as running")
        w["phase"] = "running"
        w.pop("fable_switch", None)
        save_state(cfg, st)
        return True
    if sw["step"] == "unconfirmed":
        return _fable_switch_unconfirmed_tick(cfg, st, wave, w, sw)
    if sw["step"] == "closing":
        if tmux_alive(name):
            tmux("kill-session", "-t", session_target(name), check=False)
        if tmux_alive(name):
            if once_per(w, "fable_switch", "close"):
                event(cfg, f"{wave}: fable switch: session {name} is not closed yet; retried next tick")
            save_state(cfg, st)
            return True
        for key in ("pending_enter", "pending_text_head", "pending_clear", "await_session"):
            w.pop(key, None)  # the old window is gone with whatever was in its input
        if sw["sid"] not in w.setdefault("sessions", []):
            w["sessions"].append(sw["sid"])  # known up front: bound by id, not by its marker
        w["cleared_at"] = time.time()  # background shells older than this were the old session's (#68)
        sw["text"] = fable_switch_message(cfg, wave, w, wdir)  # fixed now: a resumed dispatcher sends the same
        sw["step"] = "starting"
        save_state(cfg, st)
    if sw["step"] == "starting":
        if tmux_alive(name):  # the dispatcher died right after new-session
            event(cfg, f"{wave}: fable switch resumed: the window exists, not starting another")
            _mark_recovered(cfg, name, wave)
        else:
            start_session(cfg, st, wave, sid=sw["sid"], phase="switching")
        sw["step"] = "ready"
        save_state(cfg, st)
    if sw["step"] == "ready":
        if not wait_ready(name):
            blocked = "BLOCKED: окно Claude на Opus не стало готовым после лимита Fable, продолжение не отправлено\n"
            (wdir / "status").write_text(blocked, encoding="utf-8")
            w["phase"] = "not_ready"
            w.pop("fable_switch", None)
            put_notice(cfg, w, wave, "not_ready", "1", reason="window_not_ready")
            save_state(cfg, st)
            event(cfg, f"{wave}: Claude TUI on {wave_model(cfg, w)} not ready in {name}, resume NOT sent")
            flush_notices(cfg, st, w)
            return False
        sw["step"] = "sending"  # from here the resume message is never resent blindly
        save_state(cfg, st)
    elif w.get("pending_enter") != FABLE_SWITCH_WHAT:  # `sending` found on disk, nothing recorded as typed
        if session_marker(cfg, wave) in _first_user_text(transcript_path(w["cwd"], sw["sid"])):
            return _fable_switch_done(cfg, st, wave, w, "the resume message evidently arrived")
        try:
            why = input_empty_reason(pane_ansi(name))
        except (subprocess.SubprocessError, OSError) as e:
            why = type(e).__name__
        if why is not None:  # something may sit in the input: neither typed over nor resent
            # not the end of the switch (Codex P2 on #109): the wave waits for a confirmed delivery or the
            # owner, who is told at once (CodeRabbit on #109); the phase, the step and the notice in ONE save
            sw["step"] = "unconfirmed"
            if once_per(w, "updating", f"fable:{sw['sid']}"):
                put_notice(cfg, w, wave, "updating", f"fable:{sw['sid']}")
            save_state(cfg, st)
            event(cfg, f"{wave}: fable switch: unknown whether the resume message arrived ({why}), NOT resent; "
                       f"waiting for the new session to confirm it")
            flush_notices(cfg, st, w)
            return True
    try:
        text = sw.get("text") if isinstance(sw.get("text"), str) else None
        if text is None:  # a record of an older 1.4.0 build: choose now and keep it
            sw["text"] = text = fable_switch_message(cfg, wave, w, wdir)
            save_state(cfg, st)
        ok = _deliver(cfg, st, wave, FABLE_SWITCH_WHAT, send_text, text)
    except _WindowGone:
        return False
    if not ok:
        save_state(cfg, st)
        return True  # step `sending` is on disk: the next tick decides by pending_enter and the screen
    return _fable_switch_done(cfg, st, wave, w, "resume sent")


def next_prompt_problem(path):
    """Why the next wave could not start from this next-prompt.md (the same read_prompt check as
    its launch), or None when it can."""
    try:
        read_prompt(path)
    except SystemExit as e:
        return _exc_text(e)
    return None


def _stop_without_next(cfg, st, w, wave, now, why=None):
    """A non-last wave wrote DONE without a usable next-prompt.md (missing, or `why`: refused by
    read_prompt): one event, one notice, the chain stops. Terminal like any DONE: the intent to
    close the window goes into the same save, then /exit (a later launch finds it and waits)."""
    w.setdefault("finished", now)
    w["phase"] = "done"
    w["pending_exit"] = True
    st["current"] = None
    st["stopped"] = f"{wave}: DONE with an unusable next-prompt.md" if why else f"{wave}: DONE without next-prompt.md"
    fresh_stop = once_per(w, "no_next", "1")
    if fresh_stop:
        put_notice(cfg, w, wave, "no_next", "1", reason="no_next_unusable" if why else "no_next_absent",
                   question=why or None)
    save_state(cfg, st)
    if fresh_stop:
        event(cfg, f"{wave}: DONE with an unusable next-prompt.md ({why}); chain stopped" if why else
              f"{wave}: DONE without next-prompt.md; chain stopped")
    flush_notices(cfg, st, w)
    close_window(cfg, st, w)
    return False


def _launch_refused(cfg, st, wave, nxt, why):
    """The dispatcher's own launch of `nxt` was refused (SystemExit before the launch intent):
    the chain stops with the reason (st `stopped`), one event and one notice per reason, and the
    watch ends through _stop_event; it does not retry by itself every tick. The coordinator fixes
    the cause and runs `launch` (or watch again, which retries the launch from the DONE wave)."""
    fresh = load_state(cfg)  # the refused launch may have saved (admitted_path, a dropped flag)
    st.clear()
    st.update(fresh)
    w = st["waves"][wave]
    st["stopped"] = f"{wave}: launch of {nxt} refused"
    first = once_per(w, "launch_refused", why)
    if first:
        put_notice(cfg, w, wave, "launch_refused", why, next_wave=nxt, question=why)
    save_state(cfg, st)
    event(cfg, f"{wave}: launch of {nxt} refused: {why}")
    flush_notices(cfg, st, w)


# ---------- merge gate and alarm (W5) ----------
#
# `merge_gate: auto`: a DONE wave is not closed. Phase `gate`: every GATE_POLL_SECONDS the facts
# (GitHub API + the manifest + the wave's working copy) go through gate.evaluate:
#   wait  - one event per reason, nothing else;
#   fail  - status `BLOCKED: merge gate: <reasons>`, the reasons into the wave's window, phase
#           `running` (the usual BLOCKED notice follows; the wave fixes and writes DONE again);
#   pass  - the head the verdict is about is kept (`gate_sha`, `gate_pr`) and the phase is `merging`:
#           no open threads -> `gh pr merge --squash --match-head-commit <sha>` (at most once per sha,
#           the mark is saved BEFORE the call); open threads -> a script for the owner, nothing is merged.
# Phase `merging` waits for the PR to be MERGED with exactly `gate_sha` as its head, then the wave is
# completed like any DONE (window closed, next wave launched or the chain finished). Everything that
# reaches GitHub goes through gh_api, gh_graphql, find_pr, gate_facts, workdir_state, read_manifest
# and `sh`, so tests replace them.

GH_TIMEOUT = 60
MAX_STATUS = 500


def _gh(*args, timeout=GH_TIMEOUT):
    try:
        r = sh("gh", *args, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise gate.CollectError(f"gh {args[0]}: {type(e).__name__}")
    if r.returncode != 0:
        why = _masked_line(r.stderr or r.stdout or "", 200) or f"rc={r.returncode}"
        raise gate.CollectError(f"gh {' '.join(args[:2])}: {why}")
    return r.stdout


def _json(text, what):
    try:
        return json.loads(text)
    except ValueError:
        raise gate.CollectError(f"{what}: not JSON")


def gh_api(path):
    return _json(_gh("api", path), f"gh api {path.split('?')[0]}")


def gh_graphql(query, variables):
    argv = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():  # -F types a number, -f keeps a string
        argv += ["-F" if isinstance(value, int) and not isinstance(value, bool) else "-f", f"{key}={value}"]
    return _json(_gh(*argv), "gh api graphql")


PR_LIST_LIMIT = 1000


def find_pr(cfg, cwd):
    """The PR of the wave's branch (the branch of its working copy) from the chain's repository INTO the
    chain's base branch: the open one, else the newest. None: no branch yet or no PR; CollectError: gh
    could not tell, the base branch is unknown (it is never guessed: several PRs of one branch may go to
    different bases) or two open PRs match (never chosen by number)."""
    repo = cfg.get("repo")
    if not repo:
        raise gate.CollectError("repo is not set in chain.json")
    try:
        r = sh("git", "-C", str(cwd), "symbolic-ref", "--short", "-q", "HEAD", check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise gate.CollectError(f"git: {type(e).__name__}")
    branch = r.stdout.strip()
    if r.returncode != 0 or not branch:
        return None  # detached HEAD: the wave has not made its branch yet
    base = base_branch_of(cfg, cwd)
    if not base:
        raise gate.CollectError("base branch is unknown (set base_branch in chain.json or origin/HEAD)")
    # `--limit` (default 30) is applied BEFORE the head-repository filter below: forks reusing the branch
    # name must not push the wave's own PR out of the page
    items = _json(_gh("pr", "list", "--repo", repo, "--head", branch, "--base", base, "--state", "all",
                      "--limit", str(PR_LIST_LIMIT), "--json",
                      "number,headRefOid,isDraft,state,baseRefName,headRepositoryOwner,headRepository"), "gh pr list")
    items = [i for i in items if isinstance(i, dict) and isinstance(i.get("number"), int)
             and isinstance(i.get("headRefOid"), str) and i.get("baseRefName") == base
             and _head_repo(i) == repo.lower()] \
        if isinstance(items, list) else []
    opened = [i for i in items if i.get("state") == "OPEN"]
    if len(opened) > 1:
        raise gate.CollectError(f"several open PRs of branch {branch} into {base}: "
                                + ", ".join(f"#{i['number']}" for i in opened))
    pool = opened or items
    return max(pool, key=lambda i: i["number"]) if pool else None


def _head_repo(item):
    """owner/name of the repository a PR comes from, lowercased; "" when gh did not say. `gh pr list --head`
    matches the branch name only, so a fork's same-named branch is told apart here (the wave pushes its
    branch into the chain's own repository)."""
    owner = item.get("headRepositoryOwner")
    head = item.get("headRepository")
    login = owner.get("login") if isinstance(owner, dict) else None
    name = head.get("name") if isinstance(head, dict) else None
    return f"{login}/{name}".lower() if isinstance(login, str) and isinstance(name, str) and login and name else ""


def gate_facts(cfg, pr):
    return gate.gather(cfg["repo"], pr["number"], pr["headRefOid"], gh_api, gh_graphql)


def workdir_state(cwd):
    """{clean, head, fingerprint} of the wave's working copy; what cannot be read is False/None."""
    try:
        r = sh("git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=all", check=False, timeout=GIT_TIMEOUT)
        clean = r.returncode == 0 and not r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        clean = False
    try:
        fingerprint = gate.fingerprint(cwd)
    except (SystemExit, Exception):  # state.py refuses with SystemExit; nothing may stop the watch
        fingerprint = None
    return {"clean": clean, "head": head_rev(cwd), "fingerprint": fingerprint}


STATE_PY = pathlib.Path(__file__).resolve().parent.parent / "state.py"
WHERE_TIMEOUT = 10
RUNS_LIMIT = 2 * 1024 * 1024  # bytes of runs.json read by current_manifest


def _current_manifest(cfg, wave):
    """The manifest of the wave's CURRENT run: (path, None), or (None, reason) for a closed refusal.
    No `<wave dir>/runs.json`, or an empty list of runs: the standard `<wave dir>/superarmanda/manifest.json`
    (the file itself may not exist yet: that is "no manifest", not an error here). Otherwise the last
    record of runs.json (as `state.py init` wrote it), which must name an existing regular file whose
    physical path (symlinks resolved) lies inside the physical directory of this wave. A refusal never
    falls back to the standard path: the gate would judge another run than the one that is going on.
    The reasons are short and quote nothing from the files."""
    wdir = cfg["run_dir"] / wave
    standard = wdir / "superarmanda" / "manifest.json"  # the only place that names it
    runs_file = wdir / "runs.json"
    if not os.path.lexists(runs_file):
        return standard, None
    try:  # a FIFO without a writer or a symlink must not hang or redirect the gate
        fd = os.open(runs_file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)  # bytes, decoded below (encoding utf-8)
    except (OSError, ValueError):
        return None, "runs.json не обычный файл или нечитаем"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "runs.json не обычный файл"
        raw = b""
        while len(raw) <= RUNS_LIMIT:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None, "runs.json нечитаем или не JSON"
    finally:
        os.close(fd)
    if len(raw) > RUNS_LIMIT:
        return None, "runs.json слишком большой"
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is a ValueError
        return None, "runs.json нечитаем или не JSON"
    if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("wave"), str)
            or not isinstance(data.get("runs"), list) or not all(isinstance(r, dict) for r in data["runs"])):
        return None, "runs.json неподдерживаемого формата"
    if data["wave"] != wave:
        return None, "runs.json принадлежит другой волне"
    if not data["runs"]:
        return standard, None
    last = data["runs"][-1]
    index = last.get("index") if isinstance(last.get("index"), int) and not isinstance(last.get("index"), bool) else len(data["runs"])
    named = last.get("manifest")
    if not isinstance(named, str) or not named or not os.path.isabs(named):
        return None, f"в записи прогона {index} нет абсолютного пути manifest"
    try:
        found = pathlib.Path(named).resolve(strict=True)
    except FileNotFoundError:
        return None, f"запись прогона {index} указывает на несуществующий файл"
    except (OSError, RuntimeError, ValueError):  # NUL, a lone surrogate (UnicodeEncodeError)
        return None, f"путь manifest записи прогона {index} не разрешается"
    try:
        inside = found.is_relative_to(wdir.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None, "каталог волны не разрешается"
    if not inside:
        return None, f"manifest прогона {index} вне каталога волны"
    try:
        regular = found.is_file()
    except (OSError, ValueError):
        regular = False
    if not regular:
        return None, f"manifest прогона {index} не обычный файл"
    return found, None


_choice_memo = None  # {(run_dir, wave): result} while gate_check runs: ONE choice of the manifest per verdict


def current_manifest(cfg, wave):
    """The invariant of the choice of the run: it never raises. Any failure of reading or parsing
    runs.json or of checking its record (RecursionError of a deeply nested file, an encoding error,
    anything unexpected; not BaseException) is a closed refusal (None, reason), never the standard path.
    Inside one gate_check the choice is made once (_choice_memo): the manifest and the reason of a
    refusal come from the same reading of runs.json, even if it changes between the two questions."""
    key = (str(cfg.get("run_dir")), wave)
    if _choice_memo is not None and key in _choice_memo:
        return _choice_memo[key]
    try:
        result = _current_manifest(cfg, wave)
    except Exception:  # noqa: BLE001 - fail closed; the reason quotes nothing from the files
        result = (None, "runs.json не разбирается или запись прогона не проверяется")
    if _choice_memo is not None:
        _choice_memo[key] = result
    return result


def read_manifest(cfg, wave):
    """The current run's manifest (see current_manifest) as written by state.py; None when there is
    none, it cannot be read or the choice of the run was refused (gate_check names the reason)."""
    path, why = current_manifest(cfg, wave)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - OSError, ValueError, RecursionError of a nested file: no manifest
        return None
    return data if isinstance(data, dict) else None


def manifest_where_of(path, wave_dir, timeout=WHERE_TIMEOUT):
    """`state.py where --manifest <path>` (never parsed here): the printed JSON object, or
    {"error": short reason}. Never raises. `where` judges the last run by the LIVE cap
    `$WAB_DIR/max-runs`, so the child gets WAB_DIR of THIS wave whatever the caller's environment says."""
    try:
        env = dict(os.environ, WAB_DIR=str(pathlib.Path(wave_dir).resolve()))
        proc = subprocess.run([sys.executable, str(STATE_PY), "where", "--manifest", str(path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env)
        if proc.returncode != 0:
            lines = [l.strip() for l in safe_text(proc.stderr or proc.stdout, 10 ** 9).splitlines() if l.strip()]
            raise ValueError(lines[-1] if lines else f"rc {proc.returncode}")
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("where: not an object")
    except subprocess.TimeoutExpired:
        return {"error": f"state.py where: таймаут {timeout} с"}
    except (OSError, ValueError) as e:
        return {"error": _masked_line(e, 150)}
    except RecursionError:
        return {"error": "state.py where: ответ не разбирается"}
    return result


def gate_check(cfg, wave, w):
    """The verdict of the merge gate for the wave's PR. Never raises: whatever goes wrong is a
    `wait` (and never a `pass`). The manifest of the current run is chosen ONCE for the whole verdict."""
    global _choice_memo
    outer, _choice_memo = _choice_memo, {} if _choice_memo is None else _choice_memo
    try:
        return _gate_check(cfg, wave, w)
    finally:
        _choice_memo = outer


def _gate_check(cfg, wave, w):
    base = {"pr": {}, "head": None, "unresolved": [], "draft": False, "number": None}
    try:
        pr = find_pr(cfg, w["cwd"])
        if pr is None:
            return dict(base, verdict="fail", reasons=["PR ветки волны не найден"])
        base["number"] = pr.get("number")  # kept even if the facts below fail: the hand-off names the PR (#34)
        facts = gate_facts(cfg, pr)
        manifest = read_manifest(cfg, wave)
        v = gate.evaluate(facts, pr["headRefOid"], manifest, workdir_state(w["cwd"]),
                          base_branch_of(cfg, w["cwd"]), plan=plan_wave_risk(cfg, wave))
        if manifest is None:  # a refused choice of the run says why, instead of "manifest missing"
            why = current_manifest(cfg, wave)[1]  # the same choice as above: gate_check keeps it (_choice_memo)
            if why:  # fail closed, the reason first; the other reasons (MERGED_OUTSIDE ...) stay
                v = dict(v, verdict="fail", reasons=[f"manifest: {why}"] + [r for r in v["reasons"]
                                                                          if r != gate.MANIFEST_MISSING])
        v["number"] = pr["number"]
        if v["verdict"] == "pass" and gate.critical(gate_facts(cfg, pr)) != gate.critical(facts):
            # a CI rerun or a new finding between the reads: the pass would rest on stale facts
            return dict(v, verdict="wait", reasons=["факты изменились во время сбора"])
        return v
    except gate.CollectError as e:
        return dict(base, verdict="wait", reasons=[f"сбор фактов: {_exc_text(e)}"])
    except Exception as e:  # noqa: BLE001 - fail closed: an unexpected error is a wait, never a pass
        return dict(base, verdict="wait", reasons=[f"гейт: {type(e).__name__}: {_exc_text(e)}"])


def _one_line(text, limit=MAX_STATUS):
    """The text in one line, cut at a WORD border: a border inside a token would leave its prefix (`ghp_abcde`),
    too short for any masking rule. MASKED FIRST (_require_masked), then collapsed: str.split() also cuts at
    \\x1c-\\x1f, NEL, U+2028/9 and the Zs spaces, i.e. inside a secret, if it runs on raw text."""
    text = Masked(" ".join(_require_masked(text).split()))
    if len(text) <= limit:
        return text
    cut = limit - 2
    if not text[cut].isspace():  # the border is inside a word: drop its beginning too
        cut = text.rfind(" ", 0, cut) if " " in text[:cut] else 0
    return Masked(text[:cut].rstrip() + " …")


def _resolve_why(out):
    """Why `resolveReviewThread` left the thread open: the errors of the answer (outside text: masked first,
    then cut), else the plain fact."""
    if isinstance(out, dict) and out.get("errors"):
        return _masked_line(out.get("errors"), 200)
    return "isResolved is not true"


def _masked_line(text, limit):
    """Text from outside as ONE line for an event or a notice: masked as a whole first (safe_text), then
    collapsed to one line, then cut. Never a cut or a collapse before the mask: str.split() also splits at a
    lone CR, U+2028 and the like, i.e. inside a secret."""
    return _clip(Masked(" ".join(_require_masked(text).split())), limit)


def _write_status(wdir, text):
    """The dispatcher's own status line, atomically (the wave may read the file at any moment)."""
    text = _one_line(safe_text(text, 10 ** 9, owner_paths=True))  # the file is read by a human too: masked FIRST
    fd, tmp = tempfile.mkstemp(dir=wdir, prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, wdir / "status")
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    return text


def home_form(path):
    """The path as the owner types it: the home directory becomes `~` (it also survives `redact`,
    which masks a long temporary path)."""
    p = pathlib.Path(path)
    try:
        return "~/" + p.relative_to(pathlib.Path.home()).as_posix()
    except ValueError:
        return str(path)


def owner_script_name(cfg, wave, sha):
    """<wave>.<sha12>.<id16>.merge, id16 = sha256 of the run identity (chain file, chain, run_id, wave,
    sha) as a JSON list: hyphen-joined parts could collide across runs (chain a-b/run c vs a/b-c), and
    one run's script would then overwrite another's. Dots, not dashes: `redact` masks a 40+ run of
    [A-Za-z0-9+/_-] that is not word-like, and a real sha in a dashed name made the owner's path «[скрыто]»."""
    ident = json.dumps([str(cfg["chain_file"]), cfg["chain"], cfg["run_id"], wave, sha])
    return f"{wave}.{sha[:12]}.{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]}.merge"


def write_owner_script(cfg, wave, sha):
    """~/.cache/wab/<owner_script_name>: executable by the owner only, bound to this run and to the
    gated sha (a re-gate on another sha writes another file: the old notice keeps its script, which
    refuses once the head moved); it runs `owner-merge` (which gates the PR again)."""
    text = gate.owner_script(pathlib.Path(__file__).resolve(), cfg["chain_file"], wave, cfg["run_id"], sha)
    folder = pathlib.Path.home() / ".cache" / "wab"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / owner_script_name(cfg, wave, sha)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".merge.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _note_pr(w, number):
    """Keep the PR number of the wave record (chain-result.md and the dashboard read it): in memory,
    saved with the caller's next save. Anything but an int is ignored."""
    if isinstance(number, int) and not isinstance(number, bool):
        w["pr"] = number


def _remember_pr(cfg, w):
    """Best effort at a hand-off without a gate verdict: ask for the PR of the wave's branch once,
    when no number is known yet. A failure is swallowed: the hand-off must not stop (#34).
    Returns True only if it really went to GitHub (a slow step: the caller re-reads DONE after it)."""
    if _wave_pr(w) is not None:
        return False
    try:
        pr = find_pr(cfg, w["cwd"])
    except Exception:  # noqa: BLE001 - the hand-off goes on without a number
        return True
    if isinstance(pr, dict):
        _note_pr(w, pr.get("number"))
    return True


def handoff_gate_line(cfg, wave, w):
    """The verdict for the hand-off notice of `merge_gate: external`: (code of REASON_WORDS, one line for events.log).
    The code is all a notice says (the template's words); the line (the reasons, and when the gate passed, the owner's
    script) is for the machine's own log. Best effort: nothing here may stop the hand-off."""
    try:
        v = gate_check(cfg, wave, w)
        _note_pr(w, v.get("number"))  # whatever the verdict: the hand-off carries the PR number (#34)
        reasons = _masked_line("; ".join(v["reasons"]), 300)
        if v["verdict"] == "wait" and reasons.startswith("сбор фактов"):
            return "gate_unchecked", f"Гейт мерджа не проверен: {reasons}"
        if v["verdict"] != "pass":
            return ("gate_wait" if v["verdict"] == "wait" else "gate_fail",
                    f"Гейт мерджа {'ждёт' if v['verdict'] == 'wait' else 'не пройден'}: {reasons}")
        number, sha = v["number"], v["head"]
        w["pr"] = number
        w["gate_sha"], w["gate_pr"] = sha, number  # saved with the hand-off: `owner-merge` gates against them
        if v["unresolved"]:
            path = write_owner_script(cfg, wave, sha)
            return ("gate_threads", f"Гейт мерджа пройден, но есть незакрытые треды ({len(v['unresolved'])}"
                    f"{gate.threads_note(v['old_p01'])}). "
                    f"Выполни: {home_form(path)}")
        path = write_owner_script(cfg, wave, sha)  # never the raw `gh pr merge`: the script gates again
        return "gate_pass", f"Гейт мерджа пройден. Выполни: {home_form(path)} (заново проверит гейт на этом HEAD; draft PR сперва переведёт в ready и остановится, потом запусти ещё раз, чтобы смержить)"
    except Exception as e:  # noqa: BLE001
        return "gate_unchecked", f"Гейт мерджа не проверен: {type(e).__name__}: {_masked_line(e, 200)}"


def _gate_tick(cfg, st, wave, w, wdir, now):
    if w.get("phase") != "gate":
        w["phase"] = "gate"  # the window stays open: the wave may be asked to fix something
        save_state(cfg, st)
    if now - (w.get("gate_at") or 0) < GATE_POLL_SECONDS:
        return True
    w["gate_at"] = now
    v = gate_check(cfg, wave, w)
    _note_pr(w, v.get("number"))  # kept on wait and fail too (saved below), not only on pass (#34)
    reasons = "; ".join(v["reasons"])
    _note_done_head(cfg, wave, w, v.get("head"))
    if v["verdict"] == "wait":
        if once_per(w, "gate_wait", reasons):
            event(cfg, f"{wave}: merge gate waits: {reasons}")
        save_state(cfg, st)
        return True
    w.get("notified", {}).pop("gate_wait", None)
    # collecting the facts takes a while: the wave may have taken its DONE back meanwhile, and `_tick`
    # would only see it on the next tick, after `gh pr ready`/`gh pr merge` or after a stale BLOCKED
    # overwrote its new status. Re-read it right before acting on either verdict.
    if not _still_done(cfg, st, wave, w, wdir, f"merge gate {v['verdict']}"):
        w["gate_at"] = 0
        save_state(cfg, st)
        return True
    if v["verdict"] == "fail":
        return _gate_failed(cfg, st, wave, w, wdir, reasons)
    return _gate_passed(cfg, st, wave, w, wdir, v)


def _note_done_head(cfg, wave, w, head):
    """The PR head seen while the wave is DONE. A different one later is the wave pushing after DONE:
    one event per change, the new head is remembered (the same one again gives nothing)."""
    if not (isinstance(head, str) and head):
        return
    old = w.get("done_head")
    w["done_head"] = head
    if isinstance(old, str) and old and old != head:
        event(cfg, f"{wave}: волна правит после DONE: {old[:12]}→{head[:12]}")


def _still_done(cfg, st, wave, w, wdir, what):
    """Is the status file still DONE after a slow step? Another line: the wave took DONE back, it goes
    on (phase `running`, its next DONE starts afresh); empty/unreadable: mid-rewrite, the next tick
    decides. Either way the caller does nothing now; the state is saved, one event per occasion."""
    again = read(wdir / "status", on_error=None)
    if again == "DONE":
        return True
    if again:
        w["last_status"], w["phase"] = again, "running"
        w.pop("done_head", None)  # its next DONE starts afresh
    save_state(cfg, st)
    event(cfg, f"{wave}: {what}, but the status is «{safe_text(again or '', 80)}» now: nothing done")
    return False


def merged_by(w):
    """Who merged a handed-over wave: the owner after `owner-handover`, otherwise the coordinator."""
    return "owner" if isinstance(w.get("owner_handover"), dict) else "coordinator"


def owner_handover_cmd(cfg, wave):
    """The ready line for the owner after a merge outside the gate (see owner_handover)."""
    return (f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))} owner-handover "
            f"{shlex.quote(str(cfg['chain_file']))} {wave} {cfg['run_id']}")


def _gate_failed(cfg, st, wave, w, wdir, reasons):
    blocked = f"BLOCKED: merge gate: {reasons}"
    if gate.MERGED_OUTSIDE in reasons:  # the gate can never pass again: the owner's way out, ready to run
        blocked += (f"; если PR смержил владелец — останови watch и выполни: "
                    f"{owner_handover_cmd(cfg, wave)}")
    w["last_status"] = _write_status(wdir, blocked)
    w["phase"] = "running"  # the wave goes on; its next DONE is gated again
    w.pop("done_head", None)  # fixing after a failed gate is lawful: not an after-DONE edit
    if w.get("pending_enter") == "gate failure":
        # a new episode: its text is typed afresh, not just confirmed with Enter — and not over the old text
        _abandon_input(cfg, st, wave, w, "a new merge gate failure")
    # the message to the window is saved WITH the status, like the checkpoint request: a tmux failure
    # (before the paste, or between the paste and Enter) is retried by the next ticks and restarts
    w["gate_fail_msg"] = {"sent": False, "text": (
        f"[wab] Гейт мерджа не пройден: {_one_line(reasons, 1500)}. Исправь причину, при необходимости "
        f"перезапиши manifest и снова запиши DONE в $WAB_DIR/status (или BLOCKED с вопросом).")}
    save_state(cfg, st)
    event(cfg, f"{wave}: merge gate failed: {reasons}")
    _send_gate_failure(cfg, st, wave, w)
    return True


def _send_gate_failure(cfg, st, wave, w):
    """Deliver the saved gate-failure text to the window (once: `sent` is saved after Enter)."""
    msg = w.get("gate_fail_msg")
    if not isinstance(msg, dict) or msg.get("sent"):
        return
    if _deliver(cfg, st, wave, "gate failure", send_text, str(msg.get("text") or "")):
        msg["sent"] = True
    save_state(cfg, st)


def _gate_passed(cfg, st, wave, w, wdir, v):
    repo, number, sha = cfg["repo"], v["number"], v["head"]
    try:
        gate.merge_argv(repo, number, sha)  # validates repo/PR/sha
    except ValueError as e:
        return _gate_failed(cfg, st, wave, w, wdir, f"недопустимые repo/PR/sha: {_exc_text(e)}")
    w["gate_sha"], w["gate_pr"] = sha, number
    unresolved = v["unresolved"]
    if v["reasons"]:  # a pass carries only the accepted limitations (`accepted <sev> <source>: <note>`)
        event(cfg, f"{wave}: merge gate passed with accepted limitations: {safe_text('; '.join(v['reasons']), 600)}")
    if v["draft"] and not unresolved:
        return _gate_ready(cfg, st, wave, w, wdir, number, sha)
    w["phase"] = "merging"
    w["merge_poll_at"] = time.time()
    if unresolved:  # the owner closes the threads and merges; nothing is merged here
        try:
            path = write_owner_script(cfg, wave, sha)
        except (OSError, ValueError) as e:
            return _gate_failed(cfg, st, wave, w, wdir, f"скрипт владельца не записан: {_exc_text(e)}")
        w["last_status"] = _write_status(
            wdir, f"BLOCKED: merge gate passed; {len(unresolved)} unresolved review threads; owner runs {home_form(path)}")
        note_question(w, w["last_status"], time.time())
        put_notice(cfg, w, wave, "merge_owner", sha, pr=number, threads=len(unresolved), p01=v["old_p01"])
        event(cfg, f"{wave}: merge gate passed with {len(unresolved)} open threads; owner script {home_form(path)}")
        save_state(cfg, st)
        event(cfg, f"{wave}: merge gate passed with {len(unresolved)} open threads; owner script {home_form(path)}")
        flush_notices(cfg, st, w)
        return True
    if w.get("merge_called") == sha:  # never twice for one sha
        save_state(cfg, st)
        return True
    w["merge_called"] = sha  # saved BEFORE the call: a crash in it must not merge again
    save_state(cfg, st)
    refused = _run_gh_step(gate.merge_argv(repo, number, sha))
    w["merge_rc"] = 0 if refused is None else 1
    if refused is None:
        save_state(cfg, st)
        event(cfg, f"{wave}: merge gate passed, merge of PR #{number} requested at {sha[:12]}")
        return True
    return _hand_to_owner(cfg, st, wave, w, wdir, number, sha, "merge_refused", f"merge refused: {refused}",
                          question=refused)


def _run_gh_step(argv):
    """One gh call; None on success, else a short reason."""
    try:
        r = sh(*argv, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return type(e).__name__
    if r.returncode != 0:
        return _masked_line(r.stderr or r.stdout or f"rc={r.returncode}", 200)
    return None


def _gate_ready(cfg, st, wave, w, wdir, number, sha):
    """A draft PR that passed the gate is only made ready here; the merge waits for the NEXT gate on a
    non-draft PR (`ready_for_review` may start new checks). Once per sha: a PR GitHub keeps showing
    as draft goes to the owner instead of a loop of `gh pr ready`."""
    if w.get("ready_called") == sha:
        w["phase"] = "merging"
        w["merge_poll_at"] = time.time()
        return _hand_to_owner(cfg, st, wave, w, wdir, number, sha, "still_draft", "PR still draft after ready")
    w["ready_called"] = sha  # saved BEFORE the call: ready is asked once per sha
    save_state(cfg, st)
    refused = _run_gh_step(["gh", "pr", "ready", str(number), "--repo", cfg["repo"]])
    if refused is not None:
        w["phase"] = "merging"
        w["merge_poll_at"] = time.time()
        return _hand_to_owner(cfg, st, wave, w, wdir, number, sha, "ready_refused", f"ready refused: {refused}",
                              question=refused)
    w["phase"] = "gate"  # the new checks must register before the next collection
    w["gate_at"] = time.time()
    save_state(cfg, st)
    event(cfg, f"{wave}: PR #{number} переведён в ready, жду проверки")
    return True


def _hand_to_owner(cfg, st, wave, w, wdir, number, sha, reason, log_text, question=None):
    try:  # the owner gets the script (it gates again, readies a draft and stops, then merges pinned to the sha), not the raw command
        command = home_form(write_owner_script(cfg, wave, sha))
    except (OSError, ValueError) as e:
        command = f"(скрипт владельца не записан: {_masked_line(e, 100)}; проверь PR #{number} вручную)"
    w["last_status"] = _write_status(wdir, f"BLOCKED: merge gate passed; {log_text}; owner runs {command}")
    note_question(w, w["last_status"], time.time())
    put_notice(cfg, w, wave, "merge_refused", sha, pr=number, reason=reason, question=question)
    save_state(cfg, st)
    event(cfg, f"{wave}: merge gate passed, {log_text}; owner runs {command}")
    flush_notices(cfg, st, w)
    return True


def _merge_stopped(cfg, st, wave, w, wdir, why, now, reason="other"):
    """The PR was merged with another head or closed: the next wave is not launched by the
    dispatcher; the coordinator takes over (awaiting_merge) and decides."""
    w["last_status"] = _write_status(wdir, f"BLOCKED: merge gate: {why}")
    note_question(w, w["last_status"], now)
    w["phase"] = "awaiting_merge"
    w["finished"] = now
    w["pending_exit"] = True
    if once_per(w, "merge_stopped", why):
        put_notice(cfg, w, wave, "merge_stopped", why, reason=reason, question=why)
    save_state(cfg, st)
    event(cfg, f"{wave}: {why}; next wave NOT launched")
    flush_notices(cfg, st, w)
    close_window(cfg, st, w)
    return False


def _regate(cfg, st, wave, w, wdir, old, new):
    """The PR is still open but its head moved after the gate passed (a push after a refused merge,
    or while the owner's script waits): the verdict was about another commit, so the wave goes back
    to the gate with everything the pass left behind forgotten."""
    for key in ("gate_sha", "gate_pr", "merge_called", "ready_called", "merge_rc", "merge_poll_at", "gate_at"):
        w.pop(key, None)
    for key in ("merge_unknown", "gate_wait"):
        w.get("notified", {}).pop(key, None)
    w["phase"] = "gate"  # the save drops the merge_owner / merge_refused notices
    w["last_status"] = _write_status(wdir, "DONE")  # the gate runs on DONE; the BLOCKED line was ours
    _note_done_head(cfg, wave, w, new if new != old else None)
    save_state(cfg, st)
    event(cfg, f"{wave}: HEAD сменился после гейта: {str(old)[:12]}→{str(new)[:12]}, гейт заново")
    return True


def owner_merge(cfg, wave, run_id, sha):
    """`wab.py owner-merge <chain.json> <wave> <run_id> <sha>`: what the owner's script runs. It is
    bound to the run and the gated sha the script was written for: another run (chain.json was
    replaced) or another gated sha is refused before anything is read from GitHub. The gate is evaluated
    AGAIN (two collections); only a pass on exactly `gate_sha` goes on: open threads are resolved,
    a draft is made ready, then the squash merge pinned to the sha. The wave's record is not
    touched: the running `watch` sees MERGED. Any refusal is SystemExit (rc 1) before any action."""
    if not (isinstance(run_id, str) and gate.RUN_ID.fullmatch(run_id) and isinstance(sha, str)
            and gate.SHA.fullmatch(sha)):
        raise SystemExit("wab: owner-merge: run_id and sha are required and must be well-formed; nothing done")
    if cfg["run_id"] != run_id:
        raise SystemExit(f"wab: owner-merge: this script is for run {run_id}, chain.json is run {cfg['run_id']}; "
                         f"nothing done")
    st = load_state(cfg)
    if not st.get("identity"):  # no pin saved by a launch: nothing ties this state to this chain.json
        raise SystemExit("wab: owner-merge: state.json has no saved run identity; nothing done "
                         "(owner-merge never writes the pin: run `launch` or `watch` first)")
    check_identity(cfg, st, "owner-merge")  # a changed repo (or any non-tunable field) is refused here
    w = st["waves"].get(wave)
    if isinstance(w, dict) and isinstance(w.get("gate_sha"), str) and w["gate_sha"] != sha:
        raise SystemExit(f"wab: owner-merge: this script is for the gated sha {sha[:12]}, the wave's gate passed "
                         f"on {w['gate_sha'][:12]}; nothing done (use the newer script)")
    if not isinstance(w, dict) or w.get("phase") not in ("merging", "awaiting_merge") \
            or not isinstance(w.get("gate_sha"), str):
        raise SystemExit(f"wab: owner-merge: wave {wave} is not in phase merging (auto) or awaiting_merge "
                         f"(external) with a passed gate; nothing done")
    v = gate_check(cfg, wave, w)
    if v["verdict"] != "pass":
        raise SystemExit(f"wab: owner-merge: the gate is {v['verdict']}: {_masked_line('; '.join(v['reasons']), 600)}; nothing done")
    if v["head"] != w["gate_sha"] or v["number"] != w.get("gate_pr"):
        raise SystemExit(f"wab: owner-merge: PR head {str(v['head'])[:12]} is not the gated {w['gate_sha'][:12]}; "
                         f"nothing done (the dispatcher gates the new head again)")
    repo, number, sha = cfg["repo"], v["number"], v["head"]
    merge = gate.merge_argv(repo, number, sha)  # validates before the first action
    for thread in v["unresolved"]:
        if not gate.NODE_ID.fullmatch(str(thread)):
            raise SystemExit(f"wab: owner-merge: unsafe review thread id {thread!r}; nothing done")
    for thread in v["unresolved"]:
        try:
            out = gh_graphql(gate.RESOLVE_MUTATION, {"id": thread})
        except gate.CollectError as e:
            raise SystemExit(f"wab: owner-merge: thread {thread} not resolved: {_exc_text(e)}")
        resolved = None
        try:
            resolved = out["data"]["resolveReviewThread"]["thread"]["isResolved"]
        except (KeyError, TypeError):
            pass
        if resolved is not True:  # null, no field, false, errors: the thread is NOT closed
            why = _resolve_why(out)
            raise SystemExit(f"wab: owner-merge: thread {thread} not resolved: {why}; nothing merged")
    if v["draft"]:  # ready may start new checks: stop here, the next run gates again on a non-draft PR
        argv = ["gh", "pr", "ready", str(number), "--repo", repo]
        r = sh(*argv, check=False, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"wab: owner-merge: `gh pr ready` failed: "
                             f"{_masked_line(r.stderr or r.stdout or r.returncode, 200)}")
        print(f"wab: owner-merge: PR #{number} переведён в ready; дождись завершения проверок "
              f"и запусти скрипт ещё раз ({len(v['unresolved'])} threads resolved)", flush=True)
        return
    if v["unresolved"]:
        # The threads took time: CI may have restarted or a new P1 / thread appeared on the same head
        # (`--match-head-commit` pins only the sha). Gate once more right before the merge. Without
        # closed threads the first gate was seconds ago, so it is not repeated.
        v2 = gate_check(cfg, wave, w)
        if v2["verdict"] != "pass":
            raise SystemExit(f"wab: owner-merge: гейт изменился после закрытия тредов: {v2['verdict']}: "
                             f"{_masked_line('; '.join(v2['reasons']), 600)}; ничего не смержено")
        if v2["head"] != w["gate_sha"] or v2["number"] != w.get("gate_pr"):
            raise SystemExit(f"wab: owner-merge: гейт изменился после закрытия тредов: PR head "
                             f"{str(v2['head'])[:12]} is not the gated {w['gate_sha'][:12]}; ничего не смержено")
        if v2["unresolved"]:
            raise SystemExit(f"wab: owner-merge: гейт изменился после закрытия тредов: появились новые треды, "
                             f"запусти скрипт ещё раз ({len(v2['unresolved'])} open); ничего не смержено")
    r = sh(*merge, check=False, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"wab: owner-merge: `{' '.join(merge[:3])}` failed: "
                         f"{_masked_line(r.stderr or r.stdout or r.returncode, 200)}")
    print(f"wab: owner-merge: PR #{number} merge requested at {sha[:12]} "
          f"({len(v['unresolved'])} threads resolved)", flush=True)


def owner_handover(cfg, wave, run_id):
    """`wab.py owner-handover <chain.json> <wave> <run_id>`: the owner merged the current wave's PR
    himself (outside the gate, which then fails «PR уже смержен вне гейта» forever) and confirms it.
    Checked before anything is written: the run, the identity, the run lock (watch stopped), the wave
    is current and not handed over / not `merging`, its PR (found as the gate finds it) is MERGED at
    exactly the wave's HEAD, and the merge commit is in the fetched origin/<base>. Then, in one save:
    phase awaiting_merge (the next `launch` is allowed), /exit intent for a live window, the record
    `owner_handover`, stale notices dropped. Any refusal is SystemExit before state.json is touched."""
    what = "owner-handover"
    if not (isinstance(run_id, str) and gate.RUN_ID.fullmatch(run_id)):
        raise SystemExit(f"wab: {what}: run_id is required and must be well-formed; nothing done")
    if cfg["run_id"] != run_id:
        raise SystemExit(f"wab: {what}: the command is for run {run_id}, chain.json is run {cfg['run_id']}; "
                         f"nothing done")
    with _RunLock(cfg, what, busy="останови watch этого прогона и повтори; nothing done"):
        st = load_state(cfg)
        if not st.get("identity"):
            raise SystemExit(f"wab: {what}: state.json has no saved run identity; nothing done "
                             f"(run `launch` or `watch` first)")
        check_identity(cfg, st, what)
        if wave != st.get("current"):
            raise SystemExit(f"wab: {what}: wave {wave} is not the current wave ({st.get('current')}); nothing done")
        w = st["waves"].get(wave)
        phase = w.get("phase") if isinstance(w, dict) else None
        if not isinstance(w, dict) or phase in HANDED_OVER or phase == "merging":
            raise SystemExit(f"wab: {what}: wave {wave} is {phase}; nothing done (awaiting_merge/done: already "
                             f"handed over; merging: the dispatcher sees MERGED itself)")
        cwd = w.get("cwd")
        if not cwd:
            raise SystemExit(f"wab: {what}: wave {wave} has no working copy in state.json; nothing done")
        repo = str(cfg["repo"])
        try:
            pr = find_pr(cfg, cwd)  # the same search as the gate (branch, base, head repository)
            if pr is None:
                raise SystemExit(f"wab: {what}: no PR of the wave's branch found; nothing done")
            info = _json(_gh("pr", "view", str(pr["number"]), "--repo", repo, "--json",
                             "state,mergeCommit,headRefOid,baseRefName"), "gh pr view")
        except gate.CollectError as e:
            raise SystemExit(f"wab: {what}: PR not read: {_exc_text(e)}; nothing done")
        number = pr["number"]
        if not isinstance(info, dict) or info.get("state") != "MERGED":
            got = info.get("state") if isinstance(info, dict) else None
            raise SystemExit(f"wab: {what}: PR #{number} is {got}, not MERGED; nothing done")
        merge = (info.get("mergeCommit") or {}).get("oid") if isinstance(info.get("mergeCommit"), dict) else None
        if not (isinstance(merge, str) and gate.SHA.fullmatch(merge)):
            raise SystemExit(f"wab: {what}: PR #{number} has no merge commit; nothing done")
        pr_head, head = info.get("headRefOid"), head_rev(cwd)
        if not head or pr_head != head:
            raise SystemExit(f"wab: {what}: PR #{number} head {str(pr_head)[:12]} is not the HEAD of the wave "
                             f"{str(head)[:12]} ({cwd}); nothing done (merged is not what the wave handed in)")
        idx = cfg["waves"].index(wave)
        if idx + 1 < len(cfg["waves"]):  # the next wave starts from this file: refuse now, not after the hand-off
            problem = next_prompt_problem(cfg["run_dir"] / wave / "next-prompt.md")
            if problem:
                raise SystemExit(f"wab: {what}: next-prompt.md of {wave} is not usable for "
                                 f"{cfg['waves'][idx + 1]}: {problem}; nothing done")
        base = base_branch_of(cfg, cwd)
        if not base:
            raise SystemExit(f"wab: {what}: the base branch is unknown (base_branch in chain.json); nothing done")
        git = lambda *a: sh("git", "-C", str(cwd), *a, check=False, timeout=GIT_TIMEOUT)
        # the next launch refreshes this working copy and refuses a dirty tree: refuse it here, before
        # the hand-off, not after (the same check as refresh_workdir)
        dirty = git("status", "--porcelain", "--untracked-files=all")
        if dirty.returncode != 0:
            raise SystemExit(f"wab: {what}: git status failed in {cwd}: {_masked_line(dirty.stderr, 200)}; nothing done")
        if dirty.stdout.strip():
            raise SystemExit(f"wab: {what}: the wave's working copy {cwd} is not clean (uncommitted or untracked "
                             f"changes: not what was merged); commit or remove them; nothing done")
        fetch = git("fetch", "origin", base)
        if fetch.returncode != 0:
            raise SystemExit(f"wab: {what}: git fetch origin {base} failed: "
                             f"{_masked_line(fetch.stderr, 200)}; nothing done")
        if git("merge-base", "--is-ancestor", merge, f"refs/remotes/origin/{base}").returncode != 0:
            raise SystemExit(f"wab: {what}: merge commit {merge[:12]} of PR #{number} is not an ancestor of "
                             f"origin/{base}; nothing done")
        now = time.time()
        w["phase"] = "awaiting_merge"
        w["pr"] = number
        name = w.get("tmux")
        if isinstance(name, str) and name and tmux_alive(name):
            w["pending_exit"] = True  # the intent to close the window, in the same save as the phase
        w["owner_handover"] = {"pr": number, "head": head, "merge_commit": merge, "at": now}
        ack_wave_notices(w)  # confirmed by the owner: notices about its past state are stale
        save_state(cfg, st)
        event(cfg, f"{wave}: owner-handover: PR #{number} смержен владельцем (head {head[:12]}, merge {merge[:12]})")
        close_pending_windows(cfg, st)
    waves = cfg["waves"]
    idx = waves.index(wave)
    wab_py = f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))}"
    chain_file = shlex.quote(str(cfg["chain_file"]))
    if idx + 1 < len(waves):
        nxt = cfg["run_dir"] / wave / "next-prompt.md"
        print(f"wab: {what}: волна {wave} передана. Дальше: {wab_py} launch {chain_file} {waves[idx + 1]} "
              f"{shlex.quote(str(nxt))} && {wab_py} watch {chain_file}", flush=True)
    else:
        print(f"wab: {what}: волна {wave} передана, это последняя волна. Дальше: {wab_py} done {chain_file} {wave}",
              flush=True)


def _merging_tick(cfg, st, wave, w, wdir, now):
    if now - (w.get("merge_poll_at") or 0) < GATE_POLL_SECONDS:
        return True
    w["merge_poll_at"] = now
    number, sha = w.get("gate_pr"), w.get("gate_sha")
    if not (isinstance(number, int) and isinstance(sha, str)):
        return _merge_stopped(cfg, st, wave, w, wdir, "в записи волны нет gate_pr/gate_sha", now, "no_gate_record")
    try:
        info = _json(_gh("pr", "view", str(number), "--repo", str(cfg["repo"]), "--json",
                         "state,mergeCommit,headRefOid,baseRefName"), "gh pr view")
        if not isinstance(info, dict):
            raise gate.CollectError("gh pr view: unexpected response")
    except gate.CollectError as e:
        if once_per(w, "merge_view_error", _exc_text(e)[:150]):
            event(cfg, f"{wave}: PR #{number} state not read: {_exc_text(e)}")
        save_state(cfg, st)
        return True
    w.get("notified", {}).pop("merge_view_error", None)
    state = info.get("state")
    got_base = info.get("baseRefName")
    want_base = base_branch_of(cfg, w["cwd"])
    if state in ("MERGED", "OPEN") and not (isinstance(got_base, str) and got_base and want_base):
        # the base is not known (no field / chain.json and origin/HEAD say nothing): a MERGED PR is
        # never taken for the chain's merge; an event once, the next tick asks again
        if state == "MERGED" and once_per(w, "merge_base_unknown", sha):
            event(cfg, f"{wave}: PR #{number} MERGED, base not known (PR: {got_base!r}, chain: {want_base!r}); waiting")
        save_state(cfg, st)
        return True
    if state == "MERGED":
        if got_base != want_base:
            return _merge_stopped(cfg, st, wave, w, wdir,
                                  f"PR #{number} смержен в {got_base}, цепочка ждёт {want_base}", now, "wrong_base")
        head = info.get("headRefOid")
        if head != sha:
            return _merge_stopped(cfg, st, wave, w, wdir,
                                  f"PR #{number} смержен с HEAD {str(head)[:12]}, а гейт проверял {sha[:12]}", now,
                                  "other_head")
        w["merged"] = {"pr": number, "sha": sha, "commit": (info.get("mergeCommit") or {}).get("oid"),
                       "at": now}
        return _complete_wave(cfg, st, wave, w, wdir, now)
    if state == "CLOSED":
        return _merge_stopped(cfg, st, wave, w, wdir, f"PR #{number} закрыт без мерджа", now, "closed")
    if state == "OPEN" and info.get("headRefOid") != sha:
        return _regate(cfg, st, wave, w, wdir, sha, info.get("headRefOid"))
    if state == "OPEN" and got_base != want_base:
        # redirected after the pass: back to the gate (which fails on the base) rather than a stop,
        # since the PR is not merged and the owner may redirect it back
        return _regate(cfg, st, wave, w, wdir, sha, sha)
    if state == "OPEN" and "merge_rc" not in w and w.get("merge_called") == sha:
        # the dispatcher died between the mark and the saved result of `gh pr merge`: never merged
        # twice for one sha, so the owner gets the script (it gates again and merges); once (merge_rc)
        try:
            command = home_form(write_owner_script(cfg, wave, sha))
        except (OSError, ValueError) as e:
            command = f"(скрипт владельца не записан: {_masked_line(e, 100)}; проверь PR #{number} вручную)"
        w["merge_rc"] = None  # marker: handed to the owner
        w["last_status"] = _write_status(
            wdir, f"BLOCKED: merge gate passed; merge result unknown; owner runs {command}")
        note_question(w, w["last_status"], now)
        put_notice(cfg, w, wave, "merge_unknown", sha, pr=number)
        save_state(cfg, st)
        event(cfg, f"{wave}: PR #{number} merge result unknown; handed to the owner: {command}")
        flush_notices(cfg, st, w)
        return True
    if "merge_rc" not in w and once_per(w, "merge_unknown", sha):
        event(cfg, f"{wave}: PR #{number} is not merged and the result of the merge call is unknown; waiting")
    save_state(cfg, st)
    return True


IDLE_NUDGE_WHAT = "idle nudge"
IDLE_NUDGE_DEFAULT = 20  # minutes of silence (chain.json idle_nudge_minutes); 0 switches the nudge off


IDLE_NUDGE_TEMPLATE = ("[wab] Толчок: окно молчит {m}+ мин при status=RUNNING, живых фоновых задач и агентов "
                       "у сессии нет. Если ждала фоновую задачу или агента — они завершились: прочитай результат и "
                       "продолжай. Если ждёшь владельца — запиши BLOCKED в status.")
_NUDGE_IN_INPUT = re.compile("[0-9.]+".join(re.escape("".join(part.split()))
                                            for part in IDLE_NUDGE_TEMPLATE.split("{m}")))


def idle_nudge_text(minutes):
    return IDLE_NUDGE_TEMPLATE.replace("{m}", f"{minutes:g}")


def _drop_nudge(cfg, st, wave, w):
    """Forget a typed idle nudge WITHOUT touching the input line: it now holds another text (the owner's
    draft). No keys, no `pending_clear`, no Enter."""
    w.pop("pending_enter", None)
    w.pop("pending_text_head", None)
    save_state(cfg, st)
    event(cfg, f"{wave}: idle nudge dropped: the input holds another text (left untouched)")


def _nudge_is_foreign(w):
    """True when the input line holds a text that is not our idle nudge (the owner replaced or extended
    it); False when it is ours, empty, or there is no input box to judge by."""
    typed = input_text(pane_ansi(w["tmux"]))
    return bool(typed) and not _NUDGE_IN_INPUT.fullmatch(typed)


def _abandon_nudge(cfg, st, wave, w, why, locked=False):
    """Give up a typed idle nudge: None when nothing of it is left in the input, "foreign" when the field
    holds another text (forgotten, field untouched), "stuck" when it could not be cleared (held)."""
    if _nudge_is_foreign(w):
        _drop_nudge(cfg, st, wave, w)
        return "foreign"
    return None if _abandon_input(cfg, st, wave, w, why, locked=locked) else "stuck"


def _nudge_precheck(cfg, st, wave, w, digest, minutes):
    """What `_deliver` re-reads under the window's input lock, right before typing (or pressing Enter):
    the status file still says RUNNING, the screen shows no dialog and (for a fresh text) an empty input,
    nothing runs behind the window, and (fresh text only) the silence still holds: the screen is the one
    the episode was decided on and no journal was written meanwhile (the wait for the lock is long). The tree unreadable: CollectError, nothing is sent."""
    name, wdir = w["tmux"], wave_dir(cfg, wave)

    def fresh():
        if read(wdir / "status", on_error=None) != "RUNNING":
            return False
        txt = pane_text(name)
        if any(m in txt for m in PERMISSION_MARKERS) or exit_dialog(txt) is not None:
            return False
        if w.get("pending_enter") != IDLE_NUDGE_WHAT:
            if pane_digest(txt) != digest:
                return False
            latest = max(transcript_activity(w).values(), default=0)
            if time.time() - latest < minutes * 60:
                return False
        if w.get("pending_enter") == IDLE_NUDGE_WHAT and _nudge_is_foreign(w):
            _drop_nudge(cfg, st, wave, w)  # before the stale path of _deliver could clear the owner's draft
            return False
        why = input_empty_reason(pane_ansi(name))
        if why == NO_INPUT_BOX:
            return False  # no input box (a menu, a dialog, a blank capture): never an Enter, retry or not
        if why is not None and w.get("pending_enter") != IDLE_NUDGE_WHAT:
            return False  # a fresh text needs an empty input; a retry has its own typed text in it
        try:
            return not wave_children(cfg, w)
        except ProcFactsError as e:
            raise gate.CollectError(f"process tree: {_exc_text(e)}") from e
    return fresh


def _idle_nudge_tick(cfg, st, wave, w, status, now, screen, children):
    """#68 comment 1. A RUNNING wave whose screen and journals (main session and every agent) have been
    silent for `idle_nudge_minutes`, with no child process under its Claude, gets ONE push per idle
    episode (the screen digest). Everything unknown (tree, screen) means no push. The text goes through
    `_deliver` like every other message into the window; a typed text whose Enter failed is retried
    Enter-only, and is cleared when the wave leaves RUNNING (see _tick)."""
    minutes = cfg.get("idle_nudge_minutes", IDLE_NUDGE_DEFAULT)
    digest = screen["digest"]
    retry = w.get("pending_enter") == IDLE_NUDGE_WHAT
    if not retry:
        if not minutes or status != "RUNNING" or w.get("pending_enter") or w.get("pending_clear"):
            return
        if w.get("notified", {}).get("nudge") == digest:
            return  # this idle episode was nudged
        latest = max(transcript_activity(w).values(), default=0)  # read here: a write after the tick began
        if now - max(w.get("pane_changed") or now, w.get("activity_at") or 0, latest) < minutes * 60:
            return
        txt = screen["txt"]
        if any(m in txt for m in PERMISSION_MARKERS) or exit_dialog(txt) is not None:
            return
        if input_empty_reason(pane_ansi(w["tmux"])) is not None:
            return
        if children is None or children:
            return  # the tree is unknown, or something runs behind the window
    elif not minutes:
        _abandon_nudge(cfg, st, wave, w, "the idle nudge is switched off")
        save_state(cfg, st)
        return
    elif _nudge_is_foreign(w):
        _drop_nudge(cfg, st, wave, w)  # not our text any more: no Enter, no clearing
        return
    sent = _deliver(cfg, st, wave, IDLE_NUDGE_WHAT, send_text, idle_nudge_text(minutes or IDLE_NUDGE_DEFAULT),
                    precheck=_nudge_precheck(cfg, st, wave, w, digest, minutes or IDLE_NUDGE_DEFAULT))
    if sent:
        w.setdefault("notified", {})["nudge"] = digest
        save_state(cfg, st)
        event(cfg, f"{wave}: idle nudge sent ({minutes:g} min, no live background work)")
    elif sent is None:
        save_state(cfg, st)  # stale under the lock: nothing sent, the next tick looks again


def _bg_tick(cfg, st, wave, w, status, now, screen, awaiting):
    """The background-work watch of a running wave: the tails of an old session and the idle nudge,
    both on one reading of the process tree."""
    if w.get("phase") != "running" or awaiting or not w.get("sessions"):
        return
    try:
        children = wave_children(cfg, w)
    except ProcFactsError as e:
        children = None
        _procs_unreadable(cfg, w, wave, e)
    else:
        w.get("notified", {}).pop("procfacts", None)
        _watch_bg_tails(cfg, st, wave, w, children, now)
    save_state(cfg, st)
    _idle_nudge_tick(cfg, st, wave, w, status, now, screen, children)


def _alarm_tick(cfg, st, wave, w, now):
    """A RUNNING wave with an open PR whose checks are all completed and on which Codex has finished
    gets ONE message per head with the results (so it need not poll them itself). The intent
    (`alarm_msg`, saved first) outlives a crash: an undelivered message is retried by the next tick or
    a restarted watch (after a paste, with Enter only); `sent` is saved after the delivery."""
    msg = w.get("alarm_msg")
    if isinstance(msg, dict) and not msg.get("sent") and w.get("pending_enter") == "alarm":
        # the text is already in the window: its Enter is pressed only while the PR is still open at the
        # head the text was built for (checked now, not at the next poll); otherwise the input is cleared
        if not _pending_alarm_enter(cfg, st, wave, w):
            return
    if now - (w.get("alarm_at") or 0) < ALARM_POLL_SECONDS:
        return
    w["alarm_at"] = now
    try:
        pr = find_pr(cfg, w["cwd"])
        if not pr or pr.get("state") != "OPEN":
            _drop_stale_alarm(cfg, st, wave, w, "the PR is not open")  # a saved message is outdated
            save_state(cfg, st)
            return
        head = pr["headRefOid"]
        if isinstance(pr.get("number"), int):
            w["pr"] = pr["number"]
        facts = gate_facts(cfg, pr)
    except gate.CollectError as e:
        if once_per(w, "alarm_error", _exc_text(e)[:150]):
            event(cfg, f"{wave}: alarm: PR facts not collected: {_exc_text(e)}")
        return  # the intent is kept, nothing is delivered
    if facts.get("error"):
        if once_per(w, "alarm_error", str(facts["error"])[:150]):
            event(cfg, f"{wave}: alarm: PR facts not collected: {facts['error']}")
        return
    w.get("notified", {}).pop("alarm_error", None)  # a clean collection closes the error episode
    msg = w.get("alarm_msg")
    if isinstance(msg, dict) and msg.get("head") != head and not msg.get("sent"):
        _drop_stale_alarm(cfg, st, wave, w, "the head moved")  # outdated: before it was delivered
        msg = None
    if not gate.alarm_ready(facts, head):
        save_state(cfg, st)
        return
    if isinstance(msg, dict) and msg.get("head") == head:
        if not msg.get("sent"):
            if w.get("pending_enter") != "alarm":
                # nothing is typed yet: the text is rebuilt from this tick's facts (Codex findings or a
                # re-run check may have changed since the failed attempt); a typed one only gets Enter
                msg["text"] = gate.alarm_text(pr["number"], head, facts)
                save_state(cfg, st)
            _send_alarm(cfg, st, wave, w)  # the open PR is still at this head: the retry is safe
        return  # delivered: one message per head
    w["alarm_msg"] = {"head": head, "sent": False, "text": gate.alarm_text(pr["number"], head, facts)}
    if w.get("pending_enter") == "alarm":
        _abandon_input(cfg, st, wave, w, "a new alarm message")  # a new message: typed afresh, not over the old
    save_state(cfg, st)  # the intent first: a crash before the paste does not lose it
    event(cfg, f"{wave}: alarm: PR #{pr['number']} checks completed and Codex finished at {head[:12]}")
    _send_alarm(cfg, st, wave, w)


def _pending_alarm_enter(cfg, st, wave, w):
    """The alarm text is typed and only its Enter is owed: check the PR and its head FIRST (the poll
    throttle does not apply). Same head, PR open: Enter-only retry. PR not open or the head moved: the
    text is outdated, the input is cleared instead of an Enter. PR facts not collected: neither Enter nor
    clearing, the reserve holds. True: the normal path may go on (the owed Enter is settled or dropped)."""
    msg = w.get("alarm_msg")
    try:
        pr = find_pr(cfg, w["cwd"])
    except gate.CollectError as e:
        if once_per(w, "alarm_error", _exc_text(e)[:150]):
            event(cfg, f"{wave}: alarm: PR facts not collected: {_exc_text(e)}")
        return False
    if not pr or pr.get("state") != "OPEN":
        _drop_stale_alarm(cfg, st, wave, w, "the PR is not open")
    elif msg.get("head") != pr.get("headRefOid"):
        _drop_stale_alarm(cfg, st, wave, w, "the head moved")
    else:
        _send_alarm(cfg, st, wave, w)
    return w.get("pending_enter") != "alarm"


def _drop_stale_alarm(cfg, st, wave, w, why):
    """An undelivered alarm whose PR is not open or whose head moved is dropped before any delivery;
    its text, if already typed, is cleared out of the input line."""
    msg = w.get("alarm_msg")
    if isinstance(msg, dict) and not msg.get("sent"):
        w.pop("alarm_msg")
        if w.get("pending_enter") == "alarm":
            _abandon_input(cfg, st, wave, w, f"stale alarm: {why}")


def _send_alarm(cfg, st, wave, w):
    """Deliver the saved alarm text (once: `sent` is saved after Enter)."""
    msg = w.get("alarm_msg")
    if not isinstance(msg, dict) or msg.get("sent"):
        return
    def current():  # under the input lock, right before typing or the Enter: is the alarm still about this PR
        # fresh text too: the tick collected its facts BEFORE the lock, and the wait for the lock (behind
        # `say`) can be long; one extra request under the lock is the price of not sending a stale alarm
        pr = find_pr(cfg, w["cwd"])  # CollectError: _deliver holds everything and retries
        return bool(pr) and pr.get("state") == "OPEN" and pr.get("headRefOid") == msg.get("head")
    sent = _deliver(cfg, st, wave, "alarm", send_text, str(msg.get("text") or ""), precheck=current)
    if sent:
        msg["sent"] = True
    elif sent is None:
        w.pop("alarm_msg", None)  # outdated: the PR closed or the head moved (a typed text was cleared)
    save_state(cfg, st)


def wave_attempts(w):
    """The earlier tries of a wave (`attempts`, archived by a restart), oldest first; a corrupt
    list or a non-object entry is skipped, not a crashed frame."""
    attempts = w.get("attempts")
    return [a for a in attempts if isinstance(a, dict)] if isinstance(attempts, list) else []


def num(x):
    """A finite real number from state.json, else None (a bool, a string, NaN or inf is not one)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        return x if math.isfinite(x) else None
    except OverflowError:  # an int too big for a float (10**400) is not a number either
        return None


def wave_duration(w, now):
    """The wave's working time: the current try (until `finished`, else now) plus each earlier
    try. An earlier try ran until its own `finished`; one without it (dead, stopped) ran until the
    nearest later known start among all the records (earlier tries and the current one) - the
    restart that archived it, whatever the list order. A try whose start or end is unknown, or
    whose end is before its start, adds nothing."""
    recs = [*wave_attempts(w), w]
    starts = [s for s in (num(r.get("started")) for r in recs) if s is not None]
    total = 0.0
    for rec in recs:
        start = num(rec.get("started"))
        if start is None:
            continue
        end = num(rec.get("finished"))
        if end is None:
            if rec is w:
                end = now
            else:
                end = min((s for s in starts if s > start), default=None)
        if end is not None and end > start:
            total += end - start
    return total


def wave_restarts(w):
    """Checkpoint restarts (fresh heads) over all tries of the wave; a corrupt counter (not a
    non-negative int) adds nothing."""
    return sum(n for n in (r.get("restarts") for r in [w, *wave_attempts(w)])
               if isinstance(n, int) and not isinstance(n, bool) and n > 0)


QUESTIONS_CAP = 50  # BLOCKED questions kept per wave


def _policy_question(label):
    """The first line of a BLOCKED question as policy-decisions.log keeps it: masked as a whole first, then cut."""
    lines = safe_text(label["question"], 10 ** 9).splitlines() or [""]
    return _clip(lines[0], 200)


def _say_first(text):
    """The first non-empty line of the coordinator's text for the `say` events: masked as a whole, then cut."""
    lines = [l for l in safe_text(text, 10 ** 9).splitlines() if l.strip()]
    return _clip(lines[0], 120) if lines else ""


def note_question(w, text, now):
    """Record a question to the owner (an owner-facing BLOCKED line) for the chain result: redacted,
    the same text twice in a row once, capped. In memory only: the caller saves it with its change."""
    qs = w.setdefault("questions", [])
    text = safe_text(text, 300)
    if not qs or qs[-1].get("text") != text:
        qs.append({"at": now, "text": text})
        del qs[:-QUESTIONS_CAP]


def _utc(ts):
    ts = num(ts)
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z" if ts is not None else "?"
    except (OverflowError, OSError, ValueError):  # finite but not a representable time
        return "?"


def _human_dur(sec):
    if not (isinstance(sec, (int, float)) and 0 <= sec < 10 ** 9):
        return "?"
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}ч{m:02d}м" if h else f"{m}м{sec % 60:02d}с"


def _wave_pr(w):
    for v in (w.get("pr"), w.get("gate_pr"), (w.get("merged") or {}).get("pr")
              if isinstance(w.get("merged"), dict) else None):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def write_chain_result(cfg, st):
    """Write `<run_dir>/chain-result.md` (atomically) and return (path, short summary for the
    end-of-chain message). Read-only over the state; nothing leaves the machine from here."""
    now = time.time()
    repo = cfg.get("repo")
    rows, questions, prs = [], [], 0
    restarts_heads = restarts_waves = 0
    starts, ends, total = [], [], 0.0
    for wave in cfg["waves"]:
        w = st.get("waves", {}).get(wave)
        if not isinstance(w, dict):
            rows.append(f"### {wave}\n\n- итог: не запускалась\n")
            continue
        recs = [w, *wave_attempts(w)]
        starts += [s for s in (num(r.get("started")) for r in recs) if s is not None]
        fin = num(w.get("finished")) or (now if w.get("phase") == "done" else None)
        ends += [fin] if fin is not None else []
        dur = wave_duration(w, fin or now)
        total += dur
        heads, retries = wave_restarts(w), len(wave_attempts(w))
        restarts_heads += heads
        restarts_waves += retries
        pr = _wave_pr(w)
        prs += pr is not None
        pr_text = "нет" if pr is None else (f"[#{pr}](https://github.com/{repo}/pull/{pr})" if repo else f"#{pr}")
        merged = w.get("merged")
        if isinstance(merged, dict):
            sha = merged.get("commit") or merged.get("sha") or ""
            merge_text = f"смержен ({str(sha)[:12]})" if sha else "смержен"
        elif w.get("merged_by") == "owner":
            sha = (w.get("owner_handover") or {}).get("merge_commit") if isinstance(w.get("owner_handover"), dict) else None
            merge_text = f"смержен владельцем ({str(sha)[:12]})" if sha else "смержен владельцем"
        elif w.get("merged_by") == "coordinator":
            merge_text = "смержен координатором"
        else:
            merge_text = "нет"
        qs = [q for r in [*wave_attempts(w), w] if isinstance(r.get("questions"), list)
              for q in r["questions"] if isinstance(q, dict)]
        qs.sort(key=lambda q: num(q.get("at")) or 0)
        questions += [(wave, q) for q in qs]
        rows.append(
            f"### {wave}\n\n- итог: {w.get('phase') or '?'}\n- PR: {pr_text}\n- мердж: {merge_text}\n"
            f"- перезапуски: свежих голов {heads}, перезапусков волны {retries}\n"
            f"- вопросов владельцу: {len(qs)}\n- Время: {_human_dur(dur)}\n")
    span = f"{hum(cfg, min(starts), date=True)} - {hum(cfg, max(ends), date=True)}" if starts and ends else "?"
    lines = [f"# Итог цепочки {cfg.get('chain')}\n", f"- run: {cfg.get('run_id')}",
             f"- начало / конец: {span}", f"- Время общее: {_human_dur(total)}",
             f"- волн: {len(cfg['waves'])}, PR: {prs}, перезапусков голов: {restarts_heads}, "
             f"перезапусков волн: {restarts_waves}, вопросов: {len(questions)}\n", "## Волны\n", *rows,
             "## Вопросы к владельцу\n"]
    lines += ([f"- {hum(cfg, num(q.get('at')), date=True)} {wave}: {safe_text(q.get('text') or '', 300)}"
               for wave, q in questions] or ["нет"])
    path = cfg["run_dir"] / "chain-result.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".chain-result.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    summary = (f"волн {len(cfg['waves'])}, PR {prs}, перезапусков {restarts_heads}+{restarts_waves}, "
               f"вопросов {len(questions)}, время {_human_dur(total)}")
    counts = {"waves": len(cfg["waves"]), "prs": prs, "restarts": restarts_heads + restarts_waves,
              "questions": len(questions), "duration": total}
    return path, summary, counts


def _put_chain_done(cfg, st, w, wave):
    """The end-of-chain notice: the counts of the summary and whether chain-result.md was written. A failed write
    costs the file, never the message or the watch."""
    try:
        _path, _summary, counts = write_chain_result(cfg, st)
        reason = "result_written"
    except Exception as e:  # noqa: BLE001 - the end of the chain is always reported and saved
        event(cfg, f"chain-result.md not written: {_masked_line(e, 100)}")
        counts, reason = {}, "result_not_written"
    put_notice(cfg, w, wave, "chain_done", "1", waves=counts.get("waves"), prs=counts.get("prs"),
               restarts=counts.get("restarts"), questions=counts.get("questions"),
               duration=counts.get("duration"), reason=reason)
    if reason == "result_written":
        event(cfg, f"chain-result.md: {home_form(_path.parent)}")


def _complete_wave(cfg, st, wave, w, wdir, now):
    """The wave's PR is in (merged, or there is nothing to merge): done, window closed, then the
    chain ends (last wave) or the next wave is launched by the dispatcher. True: the watch goes on."""
    waves = cfg["waves"]
    idx = waves.index(wave)
    nxt = wdir / "next-prompt.md"
    # a DONE before the alarm / without a gate has no number yet (#34); asked BEFORE the «done» mark:
    # a wave that took DONE back during the slow lookup is not completed, and its next DONE is fresh
    asked = _remember_pr(cfg, w)
    if asked and w.get("phase") != "done" and not _still_done(cfg, st, wave, w, wdir, "PR lookup"):
        return True
    fresh = once_per(w, "done", "1")  # a restart between this and the next launch must not repeat it
    if fresh:
        w["finished"] = now
        w["pending_exit"] = True  # the intent to close the window, in the same save as the mark
        put_notice(cfg, w, wave, "done", "1", question=read(wdir / "result.md"))
    w["phase"] = "done"
    last = idx + 1 >= len(waves)
    if last:  # the end of the chain goes into the same save as the mark
        st["current"] = None
        _put_chain_done(cfg, st, w, wave)
    save_state(cfg, st)
    if fresh:
        event(cfg, f"{wave}: DONE")
    close_window(cfg, st, w)
    if last:
        event(cfg, "chain finished")
        flush_notices(cfg, st, w)
        close_finished_chain(cfg, st)  # the chain's own windows and dashboard panes: none is left behind
        return False
    if not nxt.exists():
        return _stop_without_next(cfg, st, w, wave, now)
    why = next_prompt_problem(nxt)
    if why:
        return _stop_without_next(cfg, st, w, wave, now, why)
    flush_notices(cfg, st, w)  # «волна завершена» goes out before the next wave starts
    try:
        started = launch(cfg, waves[idx + 1], nxt, by_dispatcher=True)
    except SystemExit as e:  # refused before its intent (previous window alive, admission...)
        _launch_refused(cfg, st, wave, waves[idx + 1], _exc_text(e))
        return False
    fresh_st = load_state(cfg)  # launch saved a newer snapshot (new current, new record):
    st.clear()                  # the caller's dict must not be written back over it
    st.update(fresh_st)
    return started


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _answer_in_flight(r):
    """A policy answer that may have reached the window without its count: typed and awaiting its
    Enter (`pending_enter`), or begun (`policy_pending`) when the window or watch died."""
    return r.get("pending_enter") == "policy answer" or bool(r.get("policy_pending"))


def auto_answers_used(w):
    """Automatic answers of the whole wave: this try and every archived attempt (a restart moves the
    record into `attempts`, the cap must survive it), +1 for each record with an answer in flight
    (charged, never free); a corrupt counter adds nothing."""
    recs = [w, *wave_attempts(w)]
    return (sum(r["auto_answers"] for r in recs if _count(r.get("auto_answers")))
            + sum(1 for r in recs if _answer_in_flight(r)))


def _policy_answer(cfg, st, wave, w, status, now, attach):
    """Answer a BLOCKED episode by the decision_policy of chain.json. True when it is answered (now
    or earlier in this episode): the owner is then informed (`policy_answer`), not asked. False sends
    the episode down the usual BLOCKED path: no or bad label, red=yes, merge_gate, a class/rec the
    policy does not allow, no policy, the cap reached, or a failed delivery (the next
    tick tries again; `pending_enter` makes the retry Enter-only, so the answer is never typed twice).
    None: the status file changed between the tick's read and the delivery: nothing is sent and the
    tick ends; the next tick starts the new episode.
    A rule with `answer` (#89) answers with the dispatcher's fixed variant after fixed_answer()'s machine
    check; its refusal is the usual BLOCKED path (False) with one event per episode and the reason in the
    owner's notice, the cap untouched. THE refusal is final for its episode — the same status line AND the
    same stamp of the status file, kept in `policy_refusal` of state.json (it outlives the watch): the owner
    was asked and may be answering in the window, so later ticks neither re-check nor answer, even when the
    reason is gone; a rewritten or another line is a new episode — rewritten means the file READS as this
    line under another stamp (an empty or unreadable file mid-rewrite, decided on last_status, is still the
    refused episode). A corrupt `policy_refusal` (not an object, or an object with corrupt fields) reads as
    a refusal (fail closed). `_tick` drops the mark when the episode ends. Under a rule with `answer` the
    `blocked` mark of the owner's notice belongs to the EPISODE too, not to the text of the line: a new
    episode of the very line answered or refused before, and the first record of a refusal, reset it, so
    every outcome without an auto-answer (a machine refusal, the cap) raises BLOCKED to the owner again —
    one notice per episode; a rule without `answer` keeps the 1.2.0 dedup by text. An answered fork key is kept in
    `policy_keys`, one in flight in `policy_key_pending`. THE key in flight lives only inside its episode
    and is never lost: when its typed answer is given up (_abandon_input — it may have been submitted) it
    MOVES to `policy_keys` and counts as answered; it is dropped unanswered only by its own decision and
    only when nothing reached the window. The path of a rule without `answer` in this function neither
    reads nor writes `policy_key_pending` / `policy_keys`."""
    stamp = _status_stamp(cfg, wave)
    anew = False  # the very line of the last decision under another stamp: a new episode of the same text
    if _answered(w) == status:
        if w.get("policy_answered_stamp") in (None, stamp):
            return True
        # the same text, but the file was rewritten since the answer (e.g. RUNNING in between, unseen by
        # the poll): a NEW episode, not the answered one — otherwise the wave would wait silently
        w.get("notified", {}).pop("policy_answer", None)
        w.pop("policy_answered_stamp", None)
        anew = True
    refusal = w.get("policy_refusal")
    if refusal is not None:
        if not _refusal_ok(refusal) or (refusal["status"] == status and (
                refusal.get("stamp") == stamp or read(wave_dir(cfg, wave) / "status", on_error=None) != status)):
            # one decision per episode: this one was refused and stays with the owner. Another stamp alone
            # is not a new episode: the file must read as this very line again (not empty or unreadable)
            return False
        w.pop("policy_refusal")  # the same line written anew: another episode, decided anew
        anew = True
    label = parse_blocked_label(status)
    if label is None or label["red"] or label["class"] == "merge_gate":
        return False
    rules = decision_policy(cfg)
    rule = next((r for r in rules if r["class"] == label["class"] and r["rec"] in (None, label["rec"])), None)
    if rule is None:
        return False
    fixed = None  # a rule with `answer` (#89); without it every line below is the 1.2.0 path
    if anew and rule.get("answer") is not None:
        # under a rule with `answer` the `blocked` mark belongs to the EPISODE (the line AND the stamp), not to
        # the text: the earlier episode of this line left it set (answered or refused), and an outcome of the
        # new one without an auto-answer (a machine refusal, the cap) must still raise BLOCKED to the owner
        drop_notice(w, "blocked")
    if owner_answered(cfg, wave, status):  # the owner answered this very episode with `say`
        if w.get("pending_enter") == "policy answer" and w.get("policy_pending") == status:
            # our answer is typed and waits for its Enter, the owner's came after: clear it (it may
            # have gone out already: charged to the cap)
            w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
            w.pop("policy_pending", None)
            _abandon_input(cfg, st, wave, w, "policy answer: the owner answered this episode (say)")
            save_state(cfg, st)
        if once_per(w, "owner_answered", f"{status}|{_status_stamp(cfg, wave)}"):
            event(cfg, f"{wave}: policy answer not sent: the owner answered this episode (say)")
            save_state(cfg, st)
        return None
    used = auto_answers_used(w)
    if w.get("pending_enter") == "policy answer" and w.get("policy_pending") == status:
        used -= 1  # the Enter-only retry of THIS answer: it is the one in flight, already charged
    cap = cfg.get("max_auto_answers", MAX_AUTO_ANSWERS)
    if used >= cap and w.get("pending_enter") == "policy answer" and w.get("policy_pending") == status:
        # the cap was lowered under an answer that is typed and waits for its Enter: it may already be
        # submitted (charged, like on a status change); never leave its text in the input line
        w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
        w.pop("policy_pending", None)
        _abandon_input(cfg, st, wave, w, "policy answer: the cap was lowered")
        used = auto_answers_used(w)
    if used >= cap:
        if not w.get("policy_cap_reached"):
            w["policy_cap_reached"] = True
            save_state(cfg, st)
            event(cfg, f"{wave}: policy cap reached ({used}/{cap}): class={label['class']} "
                       f"rec={label['rec']} goes to the owner")
        return False
    if rule.get("answer") is not None:
        fixed = fixed_answer(cfg, wave, w, label, rule, status)
        if not fixed["ok"]:  # a machine refusal: the usual BLOCKED path with the reason, the cap untouched
            w["policy_refusal"] = {"status": status, "stamp": stamp, "reason": fixed["reason"]}
            # the refusal is this episode's decision and always reaches the owner WITH its reason, whatever
            # `blocked` mark the text of the line already carries (once per episode: later ticks return above)
            drop_notice(w, "blocked")
            if w.get("pending_enter") == "policy answer" and w.get("policy_pending") == status:
                # a typed answer waits for its Enter, and the check no longer gives it (a corrupt mark, or the
                # fork changed since the typing: FORK_CHANGED) — the Enter is not pressed, and its text never
                # stays in the input line of an episode the owner now answers (it may have gone out: charged)
                w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
                w.pop("policy_pending", None)
                _abandon_input(cfg, st, wave, w, "policy answer: refused by the machine check")
            save_state(cfg, st)
            event(cfg, f"{wave}: policy answer not sent: {safe_text(fixed['reason'], 200)}")
            return False
    seen = {}

    def still():  # the status read AND its file stamp, taken under the input lock right before typing
        seen["stamp"] = _status_stamp(cfg, wave)
        if owner_answered(cfg, wave, status):  # `say` held the lock and answered this episode meanwhile
            seen["owner"] = True
            return False
        return read(wave_dir(cfg, wave) / "status", on_error=None) == status
    if not still():  # the wave (or the owner in its window) moved on since the tick read it
        event(cfg, f"{wave}: policy answer not sent: the status changed before delivery")
        return None
    retry = w.get("pending_enter") == "policy answer"  # typed earlier: only its Enter is owed
    w["policy_pending"] = status
    if fixed:  # the intent, saved by the same save as policy_pending (before typing): the key is in flight
        old = _fork_keys(w)[1]
        if old and old.get("key") != fixed["key"]:  # an earlier answer given up, possibly submitted
            w["policy_keys"] = [*_fork_keys(w)[0], old.get("key")]
        w["policy_key_pending"] = {"key": fixed["key"], "status": status, "text": fixed["text"]}
    sent = _deliver(cfg, st, wave, "policy answer", send_text,
                    fixed["text"] if fixed else POLICY_ANSWER.format(rec=label["rec"]),
                    precheck=still)  # re-read again UNDER the input lock: `say` may have answered meanwhile
    if sent is None:
        w.pop("policy_pending", None)
        if fixed and not retry:
            _forget_fork_key(w, fixed)  # nothing was typed: this fork key is not in flight
        if retry:  # the typed answer was given up, possibly already submitted: charged to the cap
            w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
        save_state(cfg, st)
        if seen.get("owner"):
            event(cfg, f"{wave}: policy answer not sent: the owner answered this episode (say)")
        else:
            event(cfg, f"{wave}: policy answer not sent: the status changed while waiting for the input")
        return None
    if not sent:
        if w.get("pending_enter") != "policy answer":
            w.pop("policy_pending", None)  # nothing reached the window: the next tick starts afresh
            if fixed:
                _forget_fork_key(w, fixed)
        save_state(cfg, st)
        return False
    w.pop("policy_pending", None)
    if fixed:  # the fork is answered: its key outlives the watch and the wave's restart (`attempts`)
        w.pop("policy_key_pending", None)
        w["policy_keys"] = [*_fork_keys(w)[0], fixed["key"]]
    tokens = f"class={label['class']} " + (f"answer={fixed['answer']} " if fixed else "") + f"rec={label['rec']}"
    w.setdefault("notified", {})["policy_answer"] = status
    # the stamp of the status the answer was for, taken BEFORE typing: a fast wave may already have
    # rewritten the same line by now, and that rewrite must read as a new episode
    w["policy_answered_stamp"] = seen.get("stamp", stamp)
    w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
    drop_notice(w, "blocked")  # a usual BLOCKED signal of a failed first try asks nothing any more
    w.setdefault("notified", {})["blocked"] = status  # ...and is not raised again in this episode
    question = _policy_question(label)
    try:
        with open(wave_dir(cfg, wave) / "policy-decisions.log", "a", encoding="utf-8") as f:
            f.write(f"{_utc(now)} {tokens} {question}\n")
    except OSError as e:
        event(cfg, f"{wave}: policy-decisions.log not written ({e.strerror or e})")
    put_notice(cfg, w, wave, "policy_answer", status, cls=label["class"],
               variant=fixed["answer"] if fixed else label["rec"], question=status)
    save_state(cfg, st)  # the mark, the counter and the notice together
    event(cfg, f"{wave}: policy auto-answer: {tokens}")
    return True


_OPTIONS = re.compile(r"(?i)\b(?:варианты|variants)[ \t]*:[ \t]*")
BLOCKED_PART_LIMIT = 220  # quoted question / options of the BLOCKED notice, each: the notice must keep its tail


def blocked_fields(status, refusal=None):
    """The fields of the `blocked` notice from a BLOCKED line: {cls, red, variant, reason, question}; None = «not
    known», the template leaves that note out. A labelled line (`[class=… rec=… red=…], see parse_blocked_label) gives
    the class, the red zone and the wave's recommendation (the parser's validated tokens: the notice shows them
    only as the words of the registry's vocabularies); the question is the FIRST line of the wave's question (the
    options after `variants:` are cut off), shown only with chain.json `telegram_quote: true`. No label, a broken
    label and the dispatcher's own `BLOCKED: merge gate:` keep the whole line as the question. `refusal`: the
    dispatcher's automatic answer was not sent (its reason stays in events.log)."""
    label = parse_blocked_label(status)
    reason = "policy_refused" if refusal else None
    if label is None or label["class"] == "merge_gate":
        return {"cls": None, "red": None, "variant": None, "reason": reason, "question": status}
    # the WHOLE question takes the quote's own path (safe_text, the W3 invariant) before any split:
    # a separator inside a secret's value (`password=variants:…`) would cut the value away from its key,
    # an invisible character inside the key (`password<U+200B>=…`) hides the key from redact(), and a
    # line separator (U+2028, NEL) would cut a token in two for splitlines(); a token that holds an
    # invisible character is masked whole first.
    question = safe_text(label["question"], 10 ** 9, False)
    m = _OPTIONS.search(question)
    if m:
        question = question[:m.start()]
    question = ((question.strip().splitlines() or [""])[0]).rstrip(" ;,")
    return {"cls": label["class"], "red": label["red"], "variant": label["rec"], "reason": reason,
            "question": question or None}


def tick(cfg, st):
    try:
        alive = _tick(cfg, st)
    except _WindowGone:
        alive = False  # already recorded as dead
    # after the tick: an episode that ended in it has dropped its notice, which is stale now
    for rec in st.get("waves", {}).values():
        flush_notices(cfg, st, rec)
    return alive


def _tick(cfg, st):
    close_pending_windows(cfg, st)
    wave = st.get("current")
    if not wave:
        return False
    if wave not in st["waves"]:
        event(cfg, f"state.json: current wave {wave} has no record")
        return False
    w = st["waves"][wave]
    w.setdefault("notified", {})
    w.setdefault("sessions", [])
    name, wdir = w["tmux"], wave_dir(cfg, wave)
    status = read(wdir / "status", on_error=None)
    if status is None:  # unreadable: decide on the last known status, one event per episode
        if once_per(w, "status_unreadable", "1"):
            event(cfg, f"{wave}: status unreadable ({wdir / 'status'}), keeping «{w.get('last_status', '')}»")
        status = ""
    else:
        w.get("notified", {}).pop("status_unreadable", None)  # readable again: the episode is over
    if status:
        w["last_status"] = status
    else:  # an empty read is the wave mid-rewrite: decide on the last real status
        status = w.get("last_status", "")
    now = time.time()
    attach = f"{attach_cmd(name)}  (выйти: Ctrl-b d)"
    refusal = w.get("policy_refusal")
    if refusal is not None and (refusal["status"] != status if _refusal_ok(refusal)
                                else not status.startswith("BLOCKED")):
        w.pop("policy_refusal")  # the refused episode is over (whatever comes next, DONE included): its mark goes with it
        save_state(cfg, st)

    if w.get("phase") == "awaiting_merge":
        return False  # handed to the coordinator: the watch ends and frees the run lock
    if w.get("phase") == "not_ready":
        return False
    if w.get("phase") == "launching":
        recover_launch(cfg, st, wave)
        return True
    if w.get("phase") == "switching":  # the Fable limit (#108): the window is closed and started again by us
        return _fable_switch_tick(cfg, st, wave, w)

    if w.get("phase") == "gate" and status != "DONE":
        w["phase"] = "running"  # the wave took its DONE back while the gate waited
        w.pop("done_head", None)
    if w.get("phase") == "merging":  # the gate passed: wait for MERGED (whatever the status file says now)
        return _merging_tick(cfg, st, wave, w, wdir, now)

    # DONE first: a wave may finish and close its window between two ticks
    gate_mode = cfg.get("merge_gate")
    external = gate_mode == "external"
    waves = cfg["waves"]
    is_last = waves.index(wave) + 1 >= len(waves)
    if gate_mode == "auto" and w.get("phase") == "done":
        # the PR is merged and `done` was saved, but the completion was cut short (a crash, a refused
        # launch): carry it on whatever the status file says (it may still hold our BLOCKED line)
        return _complete_wave(cfg, st, wave, w, wdir, now)
    if status == "DONE" and gate_mode == "auto" and w.get("phase") != "done":
        return _gate_tick(cfg, st, wave, w, wdir, now)  # phase `done`: completion was cut short, see below
    if status == "DONE" and gate_mode != "auto" and (external or not is_last):
        nxt = wdir / "next-prompt.md"
        if not is_last and not nxt.exists():  # the coordinator's launch would fail: stop loudly now
            return _stop_without_next(cfg, st, w, wave, now)
        why = None if is_last else next_prompt_problem(nxt)
        if why:  # present but refused by the same check as the launch: not handed over either
            return _stop_without_next(cfg, st, w, wave, now, why)
        gate_code, gate_text = handoff_gate_line(cfg, wave, w) if external else ("gate_none", "")
        if external and not _still_done(cfg, st, wave, w, wdir, "hand-off gate collected"):
            return True  # the gate reading is slow: a DONE taken back meanwhile is not handed over
        # the phase and its notice in ONE save: a crash after it still owes the notice (at least once),
        # and the phase stops a second hand-off, so a restart does not queue it twice
        if not external:
            # no gate reading here: the number is asked once, best effort (#34); DONE is re-read after it
            if _remember_pr(cfg, w) and not _still_done(cfg, st, wave, w, wdir, "hand-off PR lookup"):
                return True
        w["phase"] = "awaiting_merge"
        w["finished"] = now
        w["pending_exit"] = True  # the intent to close the window, in the same save as the phase
        put_notice(cfg, w, wave, "handoff", "1", reason=gate_code, question=read(wdir / "result.md"))
        if gate_text:  # the verdict and the owner's command: for a human at the machine, not for an outside text
            event(cfg, f"{wave}: hand-off gate: {gate_text}")
        save_state(cfg, st)
        if external:
            event(cfg, f"{wave}: DONE, awaiting merge by the coordinator")
        else:
            event(cfg, f"{wave}: DONE, no merge_gate: handing off to coordinator")
        flush_notices(cfg, st, w)
        close_window(cfg, st, w)
        return False

    if status == "DONE":  # the last wave without merge_gate (the chain ends as before), or a completion
        # of `auto` that was cut short (phase `done`: restart, a refused launch): it is carried on
        return _complete_wave(cfg, st, wave, w, wdir, now)

    if not tmux_alive(name):
        _mark_dead(cfg, st, wave, status)
        return False  # the chain stops; restart the wave by hand, then run watch again

    # screen episodes end on screen before any branch below returns or sends the outbox
    screen = end_screen_episodes(w, pane_text(name), now)
    if note_transcript_activity(w, now, cfg["idle_minutes"] * 60):
        save_state(cfg, st)  # a policy-answered BLOCKED returns without saving: keep the ended episode
    txt = screen["txt"]

    if w.get("phase") in ("starting", "sending"):  # the first prompt is not through yet
        save_state(cfg, st)  # a screen episode that ended is not sent by another record's flush
        advance_pending(cfg, st)
        return True

    if w.get("pending_clear"):
        _settle_clear(cfg, st, wave, w)  # an earlier clearing that the screen did not confirm: again

    if isinstance(w.get("gate_fail_msg"), dict):
        if status.startswith("BLOCKED: merge gate:"):
            _send_gate_failure(cfg, st, wave, w)  # a retry of a delivery that failed or was cut short
        else:  # the wave has moved on: an outdated failure is not sent
            w.pop("gate_fail_msg")
            if w.get("pending_enter") == "gate failure":
                _abandon_input(cfg, st, wave, w, "the wave moved on from the gate failure")

    if isinstance(w.get("alarm_msg"), dict) and not w["alarm_msg"].get("sent") and status != "RUNNING":
        w.pop("alarm_msg")  # the wave left RUNNING: an undelivered alarm is outdated
        if w.get("pending_enter") == "alarm":
            _abandon_input(cfg, st, wave, w, "the wave left RUNNING")
        save_state(cfg, st)

    if w.get("pending_enter") == IDLE_NUDGE_WHAT and (status != "RUNNING" or w.get("phase") != "running"):
        _abandon_nudge(cfg, st, wave, w, "the wave left RUNNING")  # an idle nudge is about a silent RUNNING wave
        save_state(cfg, st)

    if w.get("pending_enter") == "policy answer" and w.get("policy_pending") != status:
        # the wave left that BLOCKED line: the half-sent answer is outdated. It may already have been
        # submitted (watch died before its final save), so it is charged to the cap: never one free
        w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
        w.pop("policy_pending", None)
        _abandon_input(cfg, st, wave, w, "the wave left the BLOCKED line of the answer")
        save_state(cfg, st)

    answered = _policy_answer(cfg, st, wave, w, status, now, attach) if status.startswith("BLOCKED") else False
    if answered is None or answered:
        flush_notices(cfg, st, w)
        return True
    if status.startswith("BLOCKED"):
        fresh = once_per(w, "blocked", status)
        if fresh:
            if not status.startswith("BLOCKED: merge gate:"):  # the wave fixes a gate failure itself
                note_question(w, status, now)
            refusal = w.get("policy_refusal") if _refusal_ok(w.get("policy_refusal")) else {}
            asked = blocked_fields(status, refusal.get("reason") if refusal.get("status") == status else None)
            put_notice(cfg, w, wave, "blocked", status, cls=asked["cls"], red=asked["red"],
                       variant=asked["variant"], reason=asked["reason"], question=asked["question"])
        save_state(cfg, st)
        if fresh:
            event(cfg, f"{wave}: {safe_text(status, 200)}")
        flush_notices(cfg, st, w)
        return True

    if w.get("phase") == "updating":
        recover_update(cfg, st, wave)
        return True

    ready = handoff_ready(cfg, w, wave, status)
    if (ready and w.get("phase") == "checkpoint") or w.get("phase") == "clearing":
        event(cfg, f"{wave}: handoff ready, /clear + /superarmanda --resume (restart #{w.get('restarts', 0) + 1})")
        w["phase"] = "clearing"  # saved before each outside action; a repeated /clear is safe
        save_state(cfg, st)
        if not _deliver(cfg, st, wave, "/clear", send_command, "/clear"):
            return True  # phase `clearing` is on disk: retried next tick
        w["phase"] = "updating"  # from here the resume message is never resent blindly
        w["cleared_at"] = time.time()  # background shells older than this are the old session's (#68)
        w["await_session"] = True  # the next session is found by its marker, not by guesswork
        save_state(cfg, st)
        time.sleep(6)
        update = resume_message(cfg, wave, wdir)
        if not _deliver(cfg, st, wave, RESUME_WHAT, send_text, update):
            return True  # phase `updating` is on disk: recover_update decides, nothing is resent
        finish_update(cfg, st, wave)
        return True

    awaiting = bool(w.get("await_session"))
    if awaiting:
        sid = find_new_session(cfg, st, wave)
        if sid:
            w["sessions"].append(sid)
            w["await_session"] = awaiting = False
            event(cfg, f"{wave}: new session {sid} bound by marker")
    if awaiting:
        tokens = w.get("tokens", 0)  # old session: not measured, no checkpoint requested
        watch_context(cfg, st, wave, w, tokens)  # the unbound session is exactly what #44 looks for
    else:
        tokens = context_tokens(w)
        w["tokens"] = tokens
        w["peak"] = max(w.get("peak", 0), tokens)
        w["ctx_hist"] = (w.get("ctx_hist", []) + [tokens])[-120:]
        watch_context(cfg, st, wave, w, tokens)
        if w.get("phase") in ("running", "checkpoint") and status != "HANDOFF_READY" and fable_limit_hit(cfg, w):
            begin_fable_switch(cfg, st, wave, w)
            return _fable_switch_tick(cfg, st, wave, w)

    if w.get("phase") == "running" and not awaiting and tokens >= cfg["ctx_limit"]:
        event(cfg, f"{wave}: context {tokens} >= {cfg['ctx_limit']}, checkpoint requested")
        w["phase"] = "checkpoint"  # persisted first: a restarted watch must see the request
        w["checkpoint_at"] = now
        w["checkpoint_sent"] = False
        save_state(cfg, st)
    if w.get("phase") == "checkpoint" and not w.get("checkpoint_sent", True):
        # also re-sent after a dispatcher restart between the save above and delivery
        request = (f"WAB-CHECKPOINT: контекст {tokens // 1000}k токенов. По протоколу: доведи "
                   f"шаг, закоммить и запушь WIP, перепиши {wdir}/handoff.md, запиши "
                   f"HANDOFF_READY в {wdir}/status и остановись.")
        if _deliver(cfg, st, wave, "checkpoint request", send_text, request):
            w["checkpoint_sent"] = True
        save_state(cfg, st)
    elif w.get("phase") == "checkpoint" and now - (w.get("checkpoint_at") or now) > cfg["handoff_timeout_minutes"] * 60:
        if once_per(w, "checkpoint_timeout", str(w.get("checkpoint_at"))):
            put_notice(cfg, w, wave, "checkpoint_timeout", str(w.get("checkpoint_at")),
                       n=int(cfg["handoff_timeout_minutes"]))
            save_state(cfg, st)
            flush_notices(cfg, st, w)

    if w.get("phase") == "running":  # a wave out of auto mode asks for every step
        off = screen["auto_off"]
        if off:  # its end (auto mode back) is in end_screen_episodes
            w["auto_off_ticks"] = w.get("auto_off_ticks", 0) + 1
            if w["auto_off_ticks"] >= 2 and not w.get("auto_alerted"):
                w["auto_alerted"] = True
                put_notice(cfg, w, wave, "auto_off", "1")
                save_state(cfg, st)
                event(cfg, f"{wave}: window is not in auto mode")
                flush_notices(cfg, st, w)

    if any(m in txt for m in PERMISSION_MARKERS):
        if once_per(w, "permission", "visible"):  # one episode while the prompt stays on screen
            put_notice(cfg, w, wave, "permission", "visible")
            save_state(cfg, st)
            event(cfg, f"{wave}: permission prompt on screen")
            flush_notices(cfg, st, w)

    digest = screen["digest"]  # a new digest already restarted the clock (end_screen_episodes)
    if note_transcript_activity(w, now, cfg["idle_minutes"] * 60):  # a session bound in this tick
        save_state(cfg, st)
    active = max(w.get("pane_changed", now), w.get("activity_at") or 0)
    if now - active > cfg["idle_minutes"] * 60:
        if once_per(w, "idle", digest):
            put_notice(cfg, w, wave, "idle", digest, n=int(cfg["idle_minutes"]), time=active, wave_status=status,
                       question=status)
            save_state(cfg, st)
            event(cfg, f"{wave}: pane idle {cfg['idle_minutes']}+ min, status={status}")
            flush_notices(cfg, st, w)
    _bg_tick(cfg, st, wave, w, status, now, screen, awaiting)
    if w.get("phase") == "running" and status == "RUNNING" and cfg.get("repo"):
        _alarm_tick(cfg, st, wave, w, now)  # one message per head when checks and Codex are done
    save_state(cfg, st)
    return True


# ---------- Ctrl+\ popup ----------
#
# One global root binding of Ctrl+\ serves every dashboard of a tmux server. It reads the session
# option @wab_open ("<wab-open> <chain.json>") of the session the key is pressed in: set (a
# dashboard runs there) - open the wave popup; unset - close a popup client or pass the key on.
# Each dashboard (dash.py) sets the option on its own session and unsets it on exit; a killed
# session takes the option with it, so there is no registry to prune.

SAFE_PATH = re.compile(r"/[A-Za-z0-9_./+-]*")

# byte-for-byte what `tmux list-keys -T root` shows for it; other chains install the same text
KEYS_BINDING = (
    'bind-key -n "C-\\\\" if-shell -F "#{@wab_open}" '
    '{ run-shell -b "tmux display-popup -c \'#{client_name}\' -E -w 95% -h 90% -T \' волна \' '
    '\'#{@wab_open}\'" } '
    '{ if-shell -F "#{m:*ignore-size*,#{client_flags}}" { detach-client } { send-keys C-\\\\ } }\n')


def keys_conf():
    """tmux config of the global Ctrl+\\ binding (Shift+Tab and prefix+W are not touched)."""
    return KEYS_BINDING


def wab_open_value(path):
    """The @wab_open value `<wab-open> <chain.json>`. Both paths end up inside quotes of a tmux
    format and a shell command: only a conservative character set (no spaces, quotes) is allowed."""
    opener, chain = str(HERE / "wab-open"), str(pathlib.Path(path).resolve())
    for p in (opener, chain):
        if not SAFE_PATH.fullmatch(p):
            raise ValueError(f"unsafe path {p!r}: absolute, [A-Za-z0-9_./+-] only")
    return f"{opener} {chain}"


def stale_btab(list_keys_output):
    """Is the root binding of Shift+Tab a leftover of an old wab version (it let the key
    through to the wave window and switched Claude out of auto mode)? Only those are ours."""
    return any("BTab" in line and ("wab-open" in line or "ignore-size" in line)
               for line in (list_keys_output or "").splitlines())


def tmux_on(sock, *args, **kw):
    """A tmux call on exactly the server `sock` names ("" = the default server, said explicitly):
    neither TMUX_SOCKET nor $WAB_TMUX_SOCKET may redirect it."""
    return sh("tmux", *sock_flag(sock), *args, **kw)


def _root_cstar(list_keys_output):
    """The root-table lines that bind Ctrl+\\ (key shown as C-\\\\)."""
    out = []
    for line in (list_keys_output or "").splitlines():
        tok = line.split()
        if "root" in tok and tok.index("root") + 1 < len(tok) and tok[tok.index("root") + 1] == "C-\\\\":
            out.append(line)
    return out


def _ensure_binding(cfg, sock):
    """True when a binding of the @wab_open scheme is in place (found or installed).
    Install the global Ctrl+\\ binding unless one of the @wab_open scheme is already there.
    A binding that is neither ours nor an old wab one is somebody else's: left alone, with an event."""
    r = tmux_on(sock, "list-keys", "-T", "root", check=False)
    if r.returncode != 0:
        event(cfg, f"Ctrl+\\ binding skipped: list-keys failed: {r.stderr.strip() or r.stdout.strip()}")
        return False
    cur = _root_cstar(r.stdout)
    if any("#{@wab_open}" in line for line in cur):
        return True
    if cur and not any("wab-open" in line for line in cur):
        event(cfg, "Ctrl+\\ binding skipped: a foreign binding is in place")
        return False
    conf = cfg["run_dir"] / "keys.tmux"
    conf.write_text(keys_conf(), encoding="utf-8")
    r = tmux_on(sock, "source-file", str(conf), check=False)
    if r.returncode != 0:
        event(cfg, f"toggle key bind failed: {r.stderr.strip() or r.stdout.strip()}")
        return False
    return True


# `set-option -t =name` fails ("no such session"): a session option target is a pane, pane_target() = `=name:`.
# The dashboard marks ITS PANE (`set-option -p`): two dashboards in windows of one session must not
# overwrite or unset each other's value. The binding's format `#{@wab_open}` looks pane -> window ->
# session, so chains that still set the session option stay compatible.
def register_dash(cfg, path, session, sock="", pane=None):
    """Called by dash.py inside tmux: mark its own pane (else session) with @wab_open and make sure
    the global binding exists. `sock` names the server the dashboard runs in."""
    try:
        value = wab_open_value(path)
    except ValueError as e:
        event(cfg, f"Ctrl+\\ binding refused: {_exc_text(e)}")
        print(f"wab: Ctrl+\\ binding refused: {_exc_text(e)}", file=sys.stderr)
        return False
    marks = (("@wab_open", value), ("@wab_run", run_tag(cfg)), ("@wab_run_dir", str(cfg["run_dir"])))
    for opt, val in marks:  # @wab_run / @wab_run_dir: whose pane it is (close_chain_sessions, stale_chains)
        if pane:  # a pane id (%N) is itself exact
            r = tmux_on(sock, "set-option", "-p", "-t", pane, opt, val, check=False)
        else:
            r = tmux_on(sock, "set-option", "-t", pane_target(session), opt, val, check=False)
        if r.returncode != 0:
            event(cfg, f"{opt} not set: {r.stderr.strip() or r.stdout.strip()}")
            return False
    if not _ensure_binding(cfg, sock):
        return False
    event(cfg, "Ctrl+\\ toggles the wave popup")
    return True


def unregister_dash(session, sock="", pane=None):
    """Unset @wab_open of the dashboard's pane (else session) on exit, SIGTERM/SIGHUP.
    A killed pane or session needs no cleanup."""
    for opt in ("@wab_open", "@wab_run", "@wab_run_dir"):
        if pane:
            tmux_on(sock, "set-option", "-p", "-u", "-t", pane, opt, check=False)
        else:
            tmux_on(sock, "set-option", "-u", "-t", pane_target(session), opt, check=False)


def run_tag(cfg):
    """Whose tmux things these are: `<chain>/<run_id>` (both plain names), stored in @wab_run."""
    return f"{cfg['chain']}/{cfg['run_id']}"


def mark_owner(cfg, name, pane=None):
    """@wab_run and @wab_run_dir on the wave's session and on its pane, so that the end of the chain (and
    `cleanup`) closes only what this run started. The pane is given by its id (`new-session -P`, or the
    one confirmed lone pane of a recovered session), never as «the active pane of =name:». A failure is
    an event, the launch goes on."""
    try:
        for opt, val in (("@wab_run", run_tag(cfg)), ("@wab_run_dir", str(cfg["run_dir"]))):
            targets = [((), name)]
            if isinstance(pane, str) and re.fullmatch(r"%\d+", pane):
                targets.append((("-p",), pane))
            for flag, where in targets:
                r = tmux("set-option", *flag, "-t", pane_target(where), opt, val, check=False)
                if r.returncode != 0:
                    event(cfg, f"{name}: {opt} not set: {r.stderr.strip() or r.stdout.strip()}")
        if not (isinstance(pane, str) and re.fullmatch(r"%\d+", pane)):
            event(cfg, f"{name}: pane id unknown, only the session is marked")
    except (OSError, subprocess.SubprocessError) as e:
        event(cfg, f"{name}: owner marks not set: {type(e).__name__}")


def tmux_opt(opt, session=None, pane=None):
    """The value of a user option set exactly on that pane (else session), never an inherited one;
    None when it is not set there or tmux fails. Read right before the action that depends on it."""
    try:
        if pane:
            r = tmux("show-options", "-p", "-v", "-t", pane, opt, check=False)
        else:
            r = tmux("show-options", "-v", "-t", pane_target(session), opt, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _list_panes(session=None):
    """[(pane id, session name)] of one session, or of the whole server; None when tmux fails
    (told apart from an empty, successful listing)."""
    try:
        if session:
            r = tmux("list-panes", "-s", "-t", pane_target(session), "-F", "#{pane_id}\t#{session_name}", check=False)
        else:
            r = tmux("list-panes", "-a", "-F", "#{pane_id}\t#{session_name}", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [tuple(l.split("\t", 1)) for l in r.stdout.splitlines() if "\t" in l]


def _wave_session_names(cfg, st):
    names = []
    for rec in (st.get("waves") or {}).values():
        if not isinstance(rec, dict):
            continue
        for r in (rec, *wave_attempts(rec)):
            n = r.get("tmux") if isinstance(r, dict) else None
            if isinstance(n, str) and n.startswith(cfg["tmux_prefix"]) and n not in names:
                names.append(n)
    return names


def _norm_dir(path):
    if not path:
        return None
    try:
        return str(pathlib.Path(path).resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _marks(session=None, pane=None):
    """{"run": value or None, "dir": value or None} of the marks set exactly on that pane (else session),
    None when they could not be read: «no such option» (confirmed by a successful listing) differs
    of course from a tmux failure."""
    try:
        if pane:
            r = tmux("show-options", "-p", "-t", pane, check=False)
        else:
            r = tmux("show-options", "-t", pane_target(session), check=False)
        if r.returncode != 0:
            return None
        names = {l.split(None, 1)[0] for l in r.stdout.splitlines() if l.strip()}
        out = {}
        for key, opt in (("run", "@wab_run"), ("dir", "@wab_run_dir")):
            out[key] = None
            if opt in names:
                v = tmux_opt(opt, session=session, pane=pane)
                if v is None:
                    return None
                out[key] = v
        return out
    except (OSError, subprocess.SubprocessError):
        return None


def _ours(cfg, session=None, pane=None):
    """Whose is this session / pane (options at ITS OWN level, read now, never inherited)?
    "ours": @wab_run == run_tag AND @wab_run_dir names this run's directory (two chain.json may repeat
    chain/run_id and a session name; the directory tells them apart). "unmarked": no @wab_run, CONFIRMED
    by a successful listing. "foreign": anything else (another run, or the tag without a matching
    directory). "unknown": the marks could not be read: callers do nothing (no kill, no input)."""
    marks = _marks(session=session, pane=pane)
    if marks is None:
        return "unknown"
    if marks["run"] is None:
        return "unmarked"
    if marks["run"] == run_tag(cfg) and marks["dir"] and _norm_dir(marks["dir"]) == _norm_dir(cfg["run_dir"]):
        return "ours"
    return "foreign"


def _server_gone():
    """True when tmux says there is no server any more (its last session ended)."""
    try:
        r = tmux("list-sessions", check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    text = r.stderr.lower()
    return r.returncode != 0 and ("no server running" in text or "error connecting" in text)


def _kill_pane(cfg, pane):
    """kill-pane of a pane that is ours RIGHT NOW (re-read). True only when it is confirmed gone:
    rc 0 and the pane is not in a successful listing (or the server ended with it). Else an event."""
    if _ours(cfg, pane=pane) != "ours":
        event(cfg, f"pane {pane}: no longer ours, not closed")
        return False
    r = tmux("kill-pane", "-t", pane, check=False)
    if r.returncode != 0:
        event(cfg, f"pane {pane}: kill-pane failed: {r.stderr.strip() or r.stdout.strip() or r.returncode}; not closed")
        return False
    left = _list_panes()
    if left is None:
        if _server_gone():
            return True
        event(cfg, f"pane {pane}: could not confirm it is gone (listing failed)")
        return False
    if pane in {q for q, _ in left}:
        event(cfg, f"pane {pane}: still there after kill-pane")
        return False
    return True


def _kill_pane_sent(cfg, pane):
    """kill-pane of a pane of a wave session that is ours right now; True when tmux accepted it (rc 0).
    Whether it is really gone is confirmed by the caller from one listing of the session."""
    if _ours(cfg, pane=pane) != "ours":
        event(cfg, f"pane {pane}: no longer ours, not closed")
        return False
    r = tmux("kill-pane", "-t", pane, check=False)
    if r.returncode != 0:
        event(cfg, f"pane {pane}: kill-pane failed: {r.stderr.strip() or r.stdout.strip() or r.returncode}; not closed")
        return False
    return True


def _session_gone(name):
    """After a kill: True when tmux CONFIRMS the session is not there («can't find session», «no server
    running», or no socket at all: «error connecting … (No such file or directory)», the forms of tmux 3.4),
    False when it is, None when has-session failed any other way (a timeout, a refused connection, OSError):
    then nothing is known, and «gone» must not be claimed (tmux_alive reads any failure as «not alive»)."""
    try:
        r = tmux("has-session", "-t", session_target(name), check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return False
    err = (r.stderr or "").lower()
    if "can't find session" in err or "no server running" in err or (
            "error connecting" in err and "no such file or directory" in err):
        return True
    return None


def close_chain_sessions(cfg, st):
    """The chain is finished (the caller checked chain-result.md): close the tmux sessions of its waves
    and the dashboard panes, only the ones THIS run marked (`_ours`: @wab_run and @wab_run_dir, read at
    their own level right before the kill). A session without the mark is only reported; one marked by
    another run (the name was reused) is left alone. Sessions are never killed as such: their own panes
    are, and tmux ends the session with its last pane. Only confirmed closures are returned:
    {sessions, panes, warnings}. Never raises on a tmux failure."""
    tag = run_tag(cfg)
    res = {"sessions": [], "panes": [], "warnings": []}
    gone = set()

    def warn(text):
        res["warnings"].append(text)
        event(cfg, text)

    try:
        for name in _wave_session_names(cfg, st):
            if not tmux_alive(name):
                continue
            who = _ours(cfg, session=name)
            if who == "unmarked":
                warn(f"{name}: no @wab_run mark, not closed (started by an older wab?); "
                     f"close it yourself: tmux kill-session -t {name}")
                continue
            if who == "unknown":
                event(cfg, f"{name}: marks could not be read; nothing closed, retried next time")
                continue
            if who != "ours":
                event(cfg, f"{name}: not ours (@wab_run={tmux_opt('@wab_run', session=name)}, "
                           f"@wab_run_dir={tmux_opt('@wab_run_dir', session=name)}; this run {tag}, "
                           f"{cfg['run_dir']}); left alone")
                continue
            panes = _list_panes(name)
            if panes is None:
                event(cfg, f"{name}: panes could not be listed; nothing closed in this session")
                continue
            ours = [pane for pane, _ in panes if _ours(cfg, pane=pane) == "ours"]
            if not ours:
                warn(f"{name}: marked as ours but its panes have no @wab_run mark, not closed; "
                     f"close it yourself: tmux kill-session -t {name}")
                continue
            attempted = [pane for pane in ours if _kill_pane_sent(cfg, pane)]
            gone_now = _session_gone(name)
            if gone_now is None:
                event(cfg, f"{name}: could not confirm what was closed (has-session failed)")
                continue
            if gone_now:
                res["sessions"].append(name)
                gone.update(pane for pane, _ in panes)
                continue
            left = _list_panes(name)
            if left is None:
                event(cfg, f"{name}: could not confirm what was closed (listing failed)")
                continue
            still = {q for q, _ in left}
            closed = [pane for pane in attempted if pane not in still]
            res["panes"].extend(closed)
            gone.update(closed)
            if closed and len(closed) < len(panes):
                event(cfg, f"{name}: session stays: panes of other runs")
        chain_file = str(pathlib.Path(cfg.get("chain_file") or "").resolve()) if cfg.get("chain_file") else None
        listing = _list_panes()
        if listing is None:
            if not _server_gone():  # a gone server has no dashboards either: nothing to say
                event(cfg, "dashboard panes could not be listed; none closed")
            listing = []
        for pane, sess in listing:
            if pane in gone:
                continue
            opened = tmux_opt("@wab_open", pane=pane)
            if not (opened and chain_file and opened.rsplit(None, 1)[-1] == chain_file):
                continue
            who = _ours(cfg, pane=pane)
            if who == "unmarked":
                warn(f"pane {pane} (session {sess}) shows this chain's dashboard but has no @wab_run mark, "
                     f"not closed; close it yourself: tmux kill-pane -t {pane}")
            elif who == "ours" and _kill_pane(cfg, pane):
                res["panes"].append(pane)
        if res["sessions"] or res["panes"]:
            event(cfg, f"chain sessions closed: sessions {res['sessions']}, panes {res['panes']}")
    except (OSError, subprocess.SubprocessError) as e:
        event(cfg, f"closing the chain's tmux sessions failed: {type(e).__name__}: {_exc_text(e)}")
    return res


def close_finished_chain(cfg, st):
    """close_chain_sessions when chain-result.md exists (the one sign of a finished chain);
    None when it does not. Whatever fails is an event: the end of a chain must not break on it."""
    if not (cfg["run_dir"] / "chain-result.md").exists():
        return None
    try:
        return close_chain_sessions(cfg, st)
    except Exception as e:  # noqa: BLE001
        event(cfg, f"closing the chain's tmux sessions failed: {type(e).__name__}: {_exc_text(e)}")
        return None


def _dispatcher_alive(run_dir):
    """Does somebody hold <run_dir>/dispatcher.lock? Opened for reading only (never created)."""
    try:
        fh = open(pathlib.Path(run_dir) / "dispatcher.lock", "r", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        fh.close()  # closing drops a lock we took


def stale_chains(cfg):
    """Finished chains of other runs (marked @wab_run != ours, chain-result.md, no dispatcher) that
    still have tmux sessions or panes on this server: [{tag, run_dir, targets, chain_file}]."""
    mine, my_dir = run_tag(cfg), _norm_dir(cfg["run_dir"])
    found = {}

    def add(tag, run_dir, target, chain_file=None):
        run_dir = _norm_dir(run_dir) if run_dir else None
        if not tag or not run_dir or (tag == mine and run_dir == my_dir):
            return  # ours means the tag AND the directory, as in _ours
        rec = found.setdefault((tag, run_dir), {"tag": tag, "run_dir": run_dir, "targets": [], "chain_file": None})
        if target not in rec["targets"]:
            rec["targets"].append(target)
        rec["chain_file"] = rec["chain_file"] or chain_file

    try:
        r = tmux("list-sessions", "-F", "#{session_name}", check=False)
        sessions = r.stdout.split() if r.returncode == 0 else []
        marked = set()
        for name in sessions:
            tag = tmux_opt("@wab_run", session=name)
            if tag:
                marked.add((name, tag, _norm_dir(tmux_opt("@wab_run_dir", session=name) or "")))
                add(tag, tmux_opt("@wab_run_dir", session=name), name)
        for p, sess in _list_panes() or []:
            tag = tmux_opt("@wab_run", pane=p)
            pdir = tmux_opt("@wab_run_dir", pane=p)
            if not tag or (sess, tag, _norm_dir(pdir or "")) in marked:
                continue
            opened = tmux_opt("@wab_open", pane=p)
            add(tag, pdir, f"{sess}:{p}", opened.rsplit(None, 1)[-1] if opened else None)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for rec in found.values():
        try:
            if (pathlib.Path(rec["run_dir"]) / "chain-result.md").exists() and not _dispatcher_alive(rec["run_dir"]):
                out.append(rec)
        except OSError:
            continue
    return out


def warn_stale_chains(cfg):
    """At the launch of the first wave: tell, never close. Silent on any failure."""
    try:
        for rec in stale_chains(cfg):
            wab_py = f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))}"
            chain_file = rec.get("chain_file") or f"<chain.json of {rec['tag']}>"
            text = (f"wab: finished chain {rec['tag']} still has tmux sessions/panes {rec['targets']}; "
                    f"close them: {wab_py} cleanup {chain_file}")
            event(cfg, text)
            print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001 - a hint, never a reason to refuse a launch
        pass


def cleanup_cmd(cfg):
    """`wab.py cleanup <chain.json>`: close the tmux sessions and panes of a FINISHED chain (chain-result.md
    exists) that nobody runs any more. Refused while its dispatcher lives or before the chain is finished.
    Repeatable: the second call closes nothing and says nothing in events.log."""
    with _RunLock(cfg, "cleanup", busy="dispatcher of this chain is alive; wait for it or stop it first"):
        st = load_state(cfg)
        check_identity(cfg, st, "cleanup")  # refused when chain.json is not this run's; nothing is saved
        if not (cfg["run_dir"] / "chain-result.md").exists():
            raise SystemExit(f"wab: cleanup refused: chain not finished (no {cfg['run_dir'] / 'chain-result.md'})")
        res = close_chain_sessions(cfg, st)
    if res["sessions"] or res["panes"]:
        print(f"wab: cleanup: closed sessions {res['sessions']}, panes {res['panes']}")
    else:
        print("wab: cleanup: nothing to close")
    return res


def drop_stale_btab(cfg, sock=""):
    """Remove a Shift+Tab binding left by an old wab version; never touches Ctrl+\\."""
    keys = tmux_on(sock, "list-keys", "-T", "root", "BTab", check=False)
    if keys.returncode == 0 and stale_btab(keys.stdout):  # a foreign Shift+Tab binding stays
        tmux_on(sock, "unbind-key", "-n", "BTab", check=False)
        event(cfg, "removed a stale wab binding of Shift+Tab")


def _state_or_event(cfg):
    try:
        return load_state(cfg)
    except SystemExit as e:
        event(cfg, _exc_text(e))
        raise


TUNABLE = ("ctx_limit", "idle_minutes", "handoff_timeout_minutes", "tick_seconds", "telegram",
           "titles", "model", "plan_sha256", "max_auto_answers", "max_runs", "idle_nudge_minutes",
           "timezone",  # timezone only changes how a time is shown: never part of whose run this is (#88)
           "fable_budget_units")  # an advisory threshold of launch (#108): tuning it changes no run


def _identity(cfg):
    """Everything that decides WHOSE run this is: all chain.json fields except the tunable ones."""
    return {k: v for k, v in cfg.items() if k not in TUNABLE}


def _pinned_identity(cfg):
    """The identity as state.json keeps it: JSON values only; where chain.json lies is not part
    of it (run_dir is), so the same file reached by another path is the same run."""
    ident = {k: v for k, v in _identity(cfg).items() if k != "chain_file"}
    return json.loads(json.dumps(ident, default=str, sort_keys=True))


def check_identity(cfg, st, what):
    """Every entry of the coordinator (launch, watch, done) compares chain.json with the identity
    the run was started with (state.json `identity`, written by the first launch): a change of
    waves, repo, workdir or any other non-tunable field between two calls is refused (event +
    SystemExit) before anything is touched. A state from before the pin gets it now: True means
    the caller still has to save it."""
    want = _pinned_identity(cfg)
    have = st.get("identity")
    if have is None:
        st["identity"] = want
        return True
    if not isinstance(have, dict):
        changed = ["identity (corrupt in state.json)"]
    else:
        changed = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
    if changed:
        event(cfg, f"{what} refused: chain.json identity differs from the run's ({', '.join(changed)}); "
                   f"state.json left as it is")
        raise SystemExit(f"wab: {what} refused: chain.json identity changed since the run started "
                         f"({', '.join(changed)}); restore chain.json or start a new run (new run_id)")
    return False


def _stop_event(cfg, st, path):
    wave = st.get("current")
    if wave and st["waves"].get(wave, {}).get("phase") == "awaiting_merge":
        waves = cfg["waves"]
        idx = waves.index(wave) if wave in waves else len(waves)
        chain_file = cfg.get("chain_file", path)
        if idx + 1 < len(waves):
            nxt = waves[idx + 1]
            wab_py = f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))}"
            event(cfg, f"{wave}: handed to the coordinator; after merge run: {wab_py} launch "
                       f"{shlex.quote(str(chain_file))} {nxt} "
                       f"{shlex.quote(str(cfg['run_dir'] / wave / 'next-prompt.md'))} "
                       f"&& {wab_py} watch {shlex.quote(str(chain_file))}")
        else:
            wab_py = f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))}"
            event(cfg, f"{wave}: handed to the coordinator; it is the last wave, nothing to launch; "
                       f"after the merge of its PR run: {wab_py} done {shlex.quote(str(chain_file))} {wave}")
        event(cfg, "watch stopped: handed to the coordinator")
        return True
    elif wave and st["waves"].get(wave, {}).get("phase") == "not_ready":
        event(cfg, f"next wave {wave} not started: TUI not ready; chain stopped")
        return False
    elif wave and st["waves"].get(wave, {}).get("phase") == "dead":
        event(cfg, f"{wave}: window is gone; chain stopped")
        return False
    if wave and st["waves"].get(wave, {}).get("phase") not in ("done", None):
        event(cfg, f"{wave}: stopped in phase {st['waves'][wave].get('phase')}; chain stopped")
        return False
    if st.get("stopped"):
        event(cfg, f"watch stopped: {st['stopped']}; chain stopped")
        return False
    if not wave and not _last_window_closed(cfg, st, path):
        return False
    event(cfg, "watch stopped: no current wave")
    return True


def _last_window_closed(cfg, st, path):
    """The chain is finished (no current wave): the last wave's window, asked to close
    (`pending_exit`), is waited for like in `done` (EXIT_WAIT). False when it is still open after
    /exit: the watch then exits 3, and the coordinator's `done` pushes it again."""
    waves = cfg.get("waves") or []
    w = st.get("waves", {}).get(waves[-1]) if waves else None
    if not isinstance(w, dict) or not w.get("pending_exit"):
        return True
    if wait_window_closed(cfg, st, w, push=False):  # /exit went out in this tick (or at resume)
        save_state(cfg, st)
        return True
    wab_py = f"python3 {shlex.quote(str(pathlib.Path(__file__).resolve()))}"
    event(cfg, f"{waves[-1]}: window {w.get('tmux')} still open after /exit; the chain is finished; "
               f"run: {wab_py} done {shlex.quote(str(cfg.get('chain_file', path)))} "
               f"or close it ({attach_cmd(w.get('tmux') or '')})")
    return False


def watch(cfg, path, max_ticks=None):
    with _RunLock(cfg, "watch"):
        return _watch(cfg, path, max_ticks)


def _watch(cfg, path, max_ticks=None):
    require_tmux()
    event(cfg, f"watch started, ctx_limit={cfg['ctx_limit']}")
    drop_stale_btab(cfg, TMUX_SOCKET or os.environ.get("WAB_TMUX_SOCKET") or "")
    st = _state_or_event(cfg)
    if check_identity(cfg, st, "watch") and state_path(cfg).exists():
        save_state(cfg, st)  # a state from before the pin: migrated, not refused
    resume(cfg, st)
    last = 0.0
    ticks = 0
    pinned = _identity(cfg)
    while True:
        try:
            fresh = load_chain(path)  # thresholds can be tuned live
        except SystemExit as e:
            event(cfg, f"chain.json not re-read, keeping the previous settings: {_exc_text(e)}")
        except Exception as e:  # noqa: BLE001 - load_chain refuses with SystemExit; anything else is a
            # bug, and a live edit must still never kill the supervision
            event(cfg, f"chain.json not re-read, keeping the previous settings: {type(e).__name__}: {_exc_text(e)}")
        else:
            changed = sorted(k for k in set(pinned) | set(_identity(fresh)) if pinned.get(k) != _identity(fresh).get(k))
            if changed:
                event(cfg, f"chain.json identity changed ({', '.join(changed)}); keeping {pinned['run_dir']}")
                raise SystemExit(f"wab: chain.json identity changed ({', '.join(changed)}) during watch; "
                                 f"stopping; restart watch for the new run")
            cfg = fresh
        st = _state_or_event(cfg)
        if st.get("current") in cfg["waves"]:
            sync_max_runs(cfg, st["current"])
        if not tick(cfg, st):
            drain_notices(cfg)  # handoff / chain-finished notices must not be lost with the exit
            return _stop_event(cfg, load_state(cfg), path)
        ticks += 1
        st = load_state(cfg)
        w = st["waves"].get(st.get("current") or "", {})
        if time.time() - last > 600 and w:
            event(cfg, f"{st['current']}: phase={w.get('phase')} ctx={w.get('tokens', 0) // 1000}k "
                       f"restarts={w.get('restarts')} status={read(wave_dir(cfg, st['current']) / 'status')}")
            last = time.time()
        if max_ticks is not None and ticks >= max_ticks:
            return
        time.sleep(cfg["tick_seconds"])


def done_cmd(cfg, wave=None):
    """The coordinator merged the PR of the LAST wave: close it and finish the chain. Idempotent;
    a repeat also pushes a window that is still open (pending_exit) and drops the flag once it is gone.
    False when the window is still open after /exit (the CLI exits 3 then); True otherwise."""
    with _RunLock(cfg, "done"):
        st = load_state(cfg)
        if check_identity(cfg, st, "done") and state_path(cfg).exists():
            save_state(cfg, st)  # a state from before the pin: migrated, not refused
        pushed = close_pending_windows(cfg, st)  # an entry of the coordinator
        waves = cfg["waves"]
        last = waves[-1]
        wave = wave or st.get("current") or last
        if wave not in waves:
            raise SystemExit(f"wab: unknown wave {wave}; chain.json waves: {waves}")
        if wave != last:
            raise SystemExit(f"wab: done is for the last wave {last}, not {wave}; for {wave} the merge is "
                             f"followed by `launch` of {waves[waves.index(wave) + 1]}")
        w = st["waves"].get(wave)
        if not w:
            raise SystemExit(f"wab: wave {wave} has no record; nothing to finish")
        if w.get("phase") not in ("awaiting_merge", "done"):
            raise SystemExit(f"wab: wave {wave} is {w.get('phase')}; done only after its DONE hand-off "
                             f"(awaiting_merge)")
        if w.get("phase") == "awaiting_merge":
            w["phase"] = "done"
            w["merged_by"] = merged_by(w)
            w.setdefault("finished", time.time())
            ack_wave_notices(w)  # confirmed by this call; chain_done below must still get through
            if st.get("current") == wave:
                st["current"] = None
            _put_chain_done(cfg, st, w, wave)
            save_state(cfg, st)  # the phase and the notice together: a repeated `done` still owes it
            event(cfg, "chain finished")
            flush_notices(cfg, st, w)
        else:
            ack_wave_notices(w)  # a repeat call confirms too: only chain_done may still be owed
            save_state(cfg, st)
            flush_notices(cfg, st, w, force=True)  # a repeat call sends what was not delivered
        window_closed = True
        if w.get("pending_exit"):  # e.g. a crash between the awaiting_merge save and /exit
            if wait_window_closed(cfg, st, w, push=w.get("tmux") not in pushed):
                save_state(cfg, st)
            else:
                window_closed = False
        closed = close_finished_chain(cfg, st)  # the chain's own sessions and dashboard panes
        if not window_closed:
            name = w.get("tmux")
            if _window_state(cfg, name)[0] in ("gone", "closed"):
                w.pop("pending_exit", None)  # no window of this chain is left: closed by this call
                save_state(cfg, st)
                return True
            event(cfg, f"{wave}: window {w.get('tmux')} still open after /exit; run done again "
                       f"or close it ({attach_cmd(w.get('tmux') or '')})")
            return False
        return True


def say_cmd(cfg, wave, text_file):
    """Type the coordinator's reply into the wave's window the way the dispatcher does (send_text:
    one bracketed paste, Enter, the submit check with Enter retries) and log the outcome. True when
    the text left the input line.

    Safe next to a running `watch`: it takes no run lock and writes no state, only the short
    per-window input lock shared with the dispatcher's deliveries (_InputLock). It puts input into
    the window and appends one line to events.log; the dispatcher's own deliveries are tracked in the
    state (`pending_enter`) and are not touched, and a wave reading its input does not depend on who
    typed it."""
    text = read_prompt(text_file)
    if wave not in cfg["waves"]:
        raise SystemExit(f"wab: unknown wave {wave}; chain.json waves: {cfg['waves']}")
    rec = load_state(cfg)["waves"].get(wave)
    if not (isinstance(rec, dict) and rec.get("tmux")):
        # never guess the session name: with a shared tmux_prefix it may belong to another chain
        raise SystemExit(f"wab: say: wave {wave} has no launched session in state.json of this run; nothing sent")
    name = rec["tmux"]
    first = _say_first(text)
    if not tmux_alive(name):
        event(cfg, f"{wave}: say FAILED (no window {name}): {first}")
        return False
    try:
        with _InputLock(cfg, wave):  # never interleaves with a dispatcher delivery into the same input
            # the input stays reserved while the dispatcher still owes an Enter: its text is in the
            # input line, and a paste now would merge with it (read under the lock, after any delivery)
            pending = load_state(cfg)["waves"].get(wave, {})
            if isinstance(pending, dict) and pending.get("pending_enter"):
                raise NotSubmitted(f"the dispatcher still owes an Enter for «{pending['pending_enter']}» "
                                   f"in this input; retry after it is delivered")
            if isinstance(pending, dict) and pending.get("pending_clear"):
                raise NotSubmitted(f"the dispatcher has not yet cleared the input of «{pending['pending_clear'].get('what')}» "
                                   f"(its text may still be there); retry after it is cleared")
            def typed():  # the owner's text is in the input: the dispatcher must not type over it, even if
                try:      # the Enter fails later
                    write_owner_answered(cfg, wave)
                except OSError as e:
                    event(cfg, f"{wave}: say: owner-answered marker not written ({e.strerror or e})")
            send_text(name, text, on_typed=typed)
    except (subprocess.CalledProcessError, OSError, NotSubmitted) as e:
        why = _exc_text(e) if isinstance(e, NotSubmitted) else type(e).__name__
        event(cfg, f"{wave}: say FAILED ({why}): {first}")
        print(f"wab: say: the text did not leave the input line of {name}: {why}; "
              f"look: {attach_cmd(name)}", file=sys.stderr)
        return False
    event(cfg, f"{wave}: say: {first}")
    return True


def manifest_status_line(cfg, wave):
    """One line about the superarmanda manifest of the wave's current run for `status`; None when the
    wave has no manifest yet. Never raises: a refusal or a failing `where` is a line with the reason."""
    try:
        path, why = current_manifest(cfg, wave)
        if path is None:
            return f"  manifest: {why}"
        if not path.exists():
            return None
        where = manifest_where_of(path, cfg["run_dir"] / wave)
        if where.get("error"):
            return f"  manifest: {where['error']}"
        verdicts = where.get("verdicts") if isinstance(where.get("verdicts"), dict) else {}
        vt = " ".join(f"{r}={v}" for r, v in verdicts.items()) or "—"
        run = where.get("run")
        run = f"{run}{'!' if where.get('last_run') is True else ''}" if isinstance(run, str) and run else "—"
        return (f"  superarmanda: шаг {where.get('step')} ({where.get('role')}) "
                f"задача {where.get('task')} [{where.get('task_status')}] прогон {run} вердикты: {vt}")
    except Exception as e:  # noqa: BLE001 - `status` must answer whatever one manifest looks like
        return f"  manifest: {type(e).__name__}"


def status_cmd(cfg):
    st = load_state(cfg)
    print("current:", safe_text(st.get("current"), 200))
    for wave, w in st["waves"].items():  # the texts of the state and of the status file pass safe_text, the numbers stay numbers
        tokens = w.get("tokens", 0)
        ctx = tokens // 1000 if isinstance(tokens, int) and not isinstance(tokens, bool) else "?"
        print(f"{safe_text(wave, 200)}: tmux={safe_text(w.get('tmux'), 200)} phase={safe_text(w.get('phase'), 200)} ctx={ctx}k "
              f"restarts={safe_text(w.get('restarts', 0), 50)} sessions={len(w.get('sessions') or [])} "
              + (f"model={safe_text(w['model_override'], 200)} (лимит Fable) " if w.get("model_override") else "")
              + (f"step={safe_text(w['fable_switch'].get('step'), 50)} " if w.get("phase") == "switching"
                 and isinstance(w.get("fable_switch"), dict) else "") +
              f"started={hum(cfg, num(w.get('started')))} "
              f"status={safe_text(read(cfg['run_dir'] / wave / 'status'), 10 ** 6)}")
        line = manifest_status_line(cfg, wave)
        if line:
            print(safe_text(line, 10 ** 6))


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    cmd, path = argv[1], argv[2]
    if cmd == "current-tmux":
        cfg = load_chain(path, create=False)
        st = load_state(cfg)
        wave = st.get("current")
        print(st["waves"][wave]["tmux"] if wave and wave in st["waves"] else "")
    elif cmd == "status":
        status_cmd(load_chain(path, create=False))
    elif cmd == "launch" and len(argv) == 5:
        if not launch(load_chain(path), argv[3], argv[4], drain=True):
            print(f"wab: wave {argv[3]} not started (see events.log)", file=sys.stderr)
            sys.exit(3)
    elif cmd == "watch":
        if watch(load_chain(path), path) is False:
            print("wab: watch ended because the chain stopped (window gone or not started) or the last "
                  "wave's window is still open after /exit (then run done); see events.log", file=sys.stderr)
            sys.exit(3)
    elif cmd == "done" and len(argv) in (3, 4):
        if done_cmd(load_chain(path), argv[3] if len(argv) == 4 else None) is False:
            print("wab: done: the chain is finished but the last wave's window is still open after /exit; "
                  "run done again (see events.log)", file=sys.stderr)
            sys.exit(3)
    elif cmd == "cleanup" and len(argv) == 3:
        cleanup_cmd(load_chain(path, create=False))
    elif cmd == "owner-merge" and len(argv) == 6:
        owner_merge(load_chain(path, create=False), argv[3], argv[4], argv[5])
    elif cmd == "owner-merge":
        sys.exit("wab: owner-merge <chain.json> <wave> <run_id> <sha>: run_id and sha are required "
                 "(an old script without them is refused; use the script the dispatcher wrote last)")
    elif cmd == "owner-handover" and len(argv) == 5:
        owner_handover(load_chain(path, create=False), argv[3], argv[4])
    elif cmd == "notify":
        cfg = load_chain(path)
        notify(cfg, manual_notice(cfg, " ".join(argv[3:])))
    elif cmd == "attention":
        sys.exit(attention_cmd(load_chain(path, create=False)))
    elif cmd == "say" and len(argv) == 5:
        if not say_cmd(load_chain(path, create=False), argv[3], argv[4]):
            sys.exit(3)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
