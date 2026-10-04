#!/usr/bin/env python3
"""wave-autobot dispatcher: run plan waves one per tmux Claude session.

Commands:
  wab.py launch <chain.json> <wave> <prompt-file>   start one wave in tmux
  wab.py watch  <chain.json>                        supervise until the chain ends
  wab.py status <chain.json>                        one-screen status
  wab.py done   <chain.json> [<wave>]               after the LAST wave's PR is merged: finish the chain
  wab.py owner-merge <chain.json> <wave> <run_id> <sha>  gated merge by the owner (run by the generated script)
  wab.py owner-handover <chain.json> <wave> <run_id>  the owner merged the wave's PR himself: hand the chain on
  wab.py notify <chain.json> <text>                 Telegram message to the owner
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
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import shlex
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
    titles = cfg.get("titles")
    if titles is not None and not (isinstance(titles, dict) and all(isinstance(v, str) for v in titles.values())):
        bad("titles", "an object of wave id -> string")
    if "mandate_sha256" in cfg and not isinstance(cfg["mandate_sha256"], str):
        bad("mandate_sha256", "a string of 64 lowercase hex characters")
    if "plan_sha256" in cfg and not isinstance(cfg["plan_sha256"], str):
        bad("plan_sha256", "a string of 64 lowercase hex characters")


def load_chain(path, create=True):
    """Read chain.json. The run directory is <base>/<chain>/<run_id>, where base is
    `run_dir` from chain.json or <directory of chain.json>/runs. run_id is mandatory and is
    always the last path segment: a reused chain name must never pick up an older run's
    mandate.md."""
    path = pathlib.Path(path)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"chain.json: cannot read {path}: {e}")
    if not isinstance(cfg, dict):
        raise SystemExit("chain.json: must be a JSON object")
    for key in OPTIONAL_STRINGS + ("tmux_prefix",):
        if key in cfg and cfg[key] is None:
            del cfg[key]  # an explicit null is the same as leaving the field out (the default)
    if "titles" in cfg and cfg["titles"] is None:
        del cfg["titles"]  # no titles, like leaving it out (dash reads cfg.get("titles", {}))
    _check_types(cfg)
    cfg.setdefault("ctx_limit", 300_000)
    cfg.setdefault("idle_minutes", 12)
    cfg.setdefault("handoff_timeout_minutes", 25)
    cfg.setdefault("tick_seconds", 60)
    cfg.setdefault("model", None)
    cfg.setdefault("tmux_prefix", "wab-")
    for key in NUM_LIMITS:
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
        raise SystemExit(f"chain.json: run_dir/workdir cannot be used: {e}")
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
        raise SystemExit(f"wab: cannot read state.json: {e}")
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


def event(cfg, text):
    text = " ⏎ ".join(l for l in text.splitlines() if l.strip())
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
        return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else ""
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
        raise SystemExit(f"tmux is required (>= 3.2) but could not be run: {e}")
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
                "turns": 0, "tools": 0, "out": 0, "read": 0, "ctx": 0}

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
        return {k: e[k] for k in ("turns", "tools", "out", "read", "ctx")}

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
    stdout) is skipped: after /clear the marker sits in the /update that follows it."""
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
_STRUCTURED = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),                      # Telegram bot token
    re.compile(r"(?i)\b(set-cookie|cookie)(\s*:\s*)[^\r\n]*"),                 # whole header value
    re.compile(r"(?i)(?<![\w-])([\"']?(?:[\w.-]*(?:secret|token|password|passwd|pwd|api[_-]?key|"
               r"private[_-]?key|dsn|cookie|session|credential|connection[_-]?string|accountkey|"
               r"sharedaccesskey|signature)[\w.-]*|sig)[\"']?)(\s*[:=]\s*)"
               r"(?!\[скрыто\])(?:\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?|[^\s,;&}\]]+)"),             # key as a word, value as a whole
    re.compile(r"(?i)\b((?:proxy-)?authorization)(\s*:\s*)(?:(?:bearer|basic|token|digest)\s+)?\S+"),
    re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?<=://)[^/\s@]+(?=@)"),                           # userinfo in URLs, with or without password
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                      # e-mail addresses
]
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
    folder = pathlib.Path.home() / ".cache" / "wab"
    spans = []
    for m in _OWNER_SCRIPT.finditer(text):
        try:
            if (folder / m.group(1)).is_file():
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


def _labelled_sha(m):
    """A bare 40-64 hex string is a key; only one right after an explicit SHA/hash label is a commit id."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", m.group(0))
                and _SHA_LABEL.search(m.string[:m.start()]))


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
            if _labelled_sha(m) or any(a <= m.start() and m.end() <= b for a, b in own):
                return True
            in_github = any(a <= m.start() and m.end() <= b for a, b in spans)
            if rx is _HEX_KEY:
                return bool(in_github and re.fullmatch(r"[0-9a-fA-F]{40}", m.group(0))
                            and _GITHUB_SHA.search(m.string[:m.start()]))
            return in_github or (rx is _OPAQUE and _readable_dashed(m))

        text = rx.sub(lambda m: m.group(0) if keep(m) else "[скрыто]", text)
    return text if len(text) <= limit else text[:limit - 2].rstrip() + " …"


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
_SPACES = " \\n\\t\\r" + _chars_of({"Zs"})
_CONTROL = re.compile(f"[{_INVISIBLE}]")
_CTRL_WORD = re.compile(f"[^{_SPACES}]*[{_INVISIBLE}][^{_SPACES}]*")


def _clean_redact(text, limit, owner_paths):
    """redact() of text with control characters (quote markers included) neutralised WITHOUT a
    choice heuristic: a token (a run between spaces, tabs and line breaks) that holds a control
    character is masked whole, since a control character glues (`a\\x00sk-...`) or splits
    (`sk-\\x00...`) a secret and no word of the wave holds one; the rest goes through redact()."""
    text = _CTRL_WORD.sub("[скрыто]", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _clip(redact(text, 10 ** 9, owner_paths), limit)


def quote(text, limit=TG_LIMIT):
    """The wave's (or any outside) text for a notice: control characters (and so the quote markers) are
    removed, secrets masked WITHOUT the owner-script exemption, the length capped at `limit`, the whole
    wrapped in the quote markers. See render_notice."""
    return QUOTE_OPEN + _clean_redact(str(text).strip(), limit, False).strip() + QUOTE_CLOSE


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 2].rstrip() + " …"


def _render_quote(inner, before):
    """One quote as it is shown: a block when it holds lines or starts a line, else inline."""
    inner = _clean_redact(inner, 10 ** 9, False)  # again, idempotent
    lines = inner.splitlines() or [""]
    if len(lines) == 1 and before and not before.endswith("\n"):
        return f"«цитата волны: {lines[0]}»"
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
    return head + "\n" + "\n".join(shown)


def render_notice(text, limit=TG_MESSAGE_LIMIT):
    """The text of a notice as it leaves: the dispatcher's wording is redacted as before (owner-script
    exemption included), every quote is cleaned, redacted without the exemption and marked. A notice
    without markers (an older entry of the outbox) is redacted as a whole, as before."""
    out, pos = [], 0
    for m in _QUOTE_SPAN.finditer(text):
        out.append(_clean_redact(text[pos:m.start()], 10 ** 9, True))
        out.append(_render_quote(m.group(1), "".join(out)))
        pos = m.end()
    out.append(_clean_redact(text[pos:], 10 ** 9, True))
    return _clip("".join(out), limit)


def _first_line(text, limit):
    """The first non-empty line of a rendered notice, capped (display-message, ATTENTION, events)."""
    lines = [l for l in render_notice(text, 10 ** 9).splitlines() if l.strip()]
    if not lines:
        return ""
    if lines[0].strip() == "Цитата волны:" and len(lines) > 1:
        # the heading alone says nothing in ATTENTION / display-message: add the quote's first line
        return _clip(f"{lines[0].strip()} {lines[1].lstrip('> ').strip()}", limit)
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


def notify(cfg, text, wave=None):
    first = _first_line(text, 120)
    if not telegram_configured(cfg):
        event(cfg, f"notify(skipped): {first}")
        # no transport: a local signal on the waves' tmux server instead (ATTENTION is written by
        # flush_notices, which knows the episode). One call per notice, so a failure is one event
        # per episode; it never stops the tick.
        short = _clip(f"wave-autobot{' ' + wave if wave else ''}: {_first_line(text, DISPLAY_LIMIT)}", DISPLAY_LIMIT)
        try:
            display_all(short)
        except (subprocess.CalledProcessError, OSError) as e:
            event(cfg, f"display-message failed ({type(e).__name__}): {first[:80]}")
        return True  # Telegram is not configured: there is nothing to repeat
    try:
        _send_telegram(cfg, render_notice(text, TG_MESSAGE_LIMIT))
        event(cfg, f"telegram: {first}")
        return True
    except Exception as e:  # notification must never stop supervision
        event(cfg, f"telegram FAILED ({type(e).__name__}): {first[:80]}")
        return False


NOTIFY_RETRY_SECONDS = 300  # a standing notice that failed is repeated no more often than this


def put_notice(w, key, value, text):
    """Put a notice about a standing episode (`key`, `value`) into the wave's outbox, IN MEMORY only.
    `key` is a literal with a row in NOTICE_EPISODE_ENDS (the end of its episode): once the
    episode is over the notice is stale and leaves the outbox undelivered (key -> end:
    started/tmux_failed -> window gone; permission/idle/auto_off -> window gone or their screen
    end (SCREEN_EPISODE_ENDS); not_ready, no_prompt,
    checkpoint_timeout, dead, handoff -> their phase is left; sending -> the wave wrote a status;
    updating -> the new session is bound; blocked -> status not BLOCKED; done/no_next/
    launch_refused -> ack only (`done` is information: it opens no ATTENTION, INFO_NOTICES);
    the wave's own text in the notice is wrapped by quote() (see render_notice);
    chain_done -> never).
    The caller saves it with the SAME save_state as the change it reports (the phase, the
    once_per mark), then calls flush_notices: a process killed right after that save still owes
    the notice and the next watch/done sends it, while a save in between would lose it for good.
    The notice is removed only after the transport took it, so a transient Telegram failure is
    retried by later ticks instead of being lost; supervision never waits for it. At least once,
    not exactly once: a crash or a lost transport reply after the actual delivery sends it again."""
    if key not in NOTICE_EPISODE_ENDS:  # a programming error: the notice would never become stale
        raise ValueError(f"put_notice: key {key!r} has no row in NOTICE_EPISODE_ENDS")
    box = w.setdefault("outbox", {})
    cur = box.get(key)
    if cur is None or cur.get("value") != value:
        box[key] = {"value": value, "text": text, "next_at": 0}


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
#   updating            the new session is bound by its marker (/update arrived), or window gone
#   checkpoint_timeout  the phase left checkpoint (HANDOFF_READY taken, /clear + /update done)
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
    "updating": lambda w: _gone(w) or not w.get("await_session"),
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


def note_attention(w, key, text, now):
    """A notice went out only locally (no Telegram): an open signal of its episode, IN MEMORY; the
    caller's save writes ATTENTION (sync_attention). drop_notice and ack_wave_notices close it."""
    w.setdefault("attention", {})[key] = {"at": now, "line": _first_line(text, 300)}


def attention_path(cfg):
    return cfg["run_dir"] / "ATTENTION"


def attention_text(st):
    """ATTENTION for the open signals of the state: the latest one, or None when none is open."""
    latest, total = None, 0
    for wave, w in (st.get("waves") or {}).items():
        att = w.get("attention") if isinstance(w, dict) else None
        if not isinstance(att, dict):
            continue
        for item in att.values():
            if isinstance(item, dict):
                total += 1
                if latest is None or (num(item.get("at")) or 0) > (num(latest[2].get("at")) or 0):
                    latest = (wave, w, item)
    if latest is None:
        return None
    wave, w, item = latest
    text = (f"time: {_utc(item.get('at'))}\nwave: {wave}\nsignal: {item.get('line', '')}\n"
            f"attach: {attach_cmd(str(w.get('tmux') or ''))}\n")
    if total > 1:
        text += f"open signals: {total} (wab.py status, events.log)\n"
    return text


def sync_attention(cfg, st):
    """Make $RUN_DIR/ATTENTION match the open signals of the state: atomically rewritten with the
    latest one, removed when none is left. Called by save_state, so an episode that ends with a save
    (RUNNING after BLOCKED, an ack, the next launch) takes the file with it."""
    path = attention_path(cfg)
    text = attention_text(st)
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
        if item.get("next_at", 0) > now and not force:
            continue
        tried = True
        if notify(cfg, item["text"], wave):
            box.pop(key, None)
            if local and key not in INFO_NOTICES:
                note_attention(w, key, item["text"], now)
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
        return f"{path}: {e}"
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
    """chain.json `decision_policy`: a list of {"class": <class>, "rec": <token>?}. The reason it is
    refused, or None. Never in it: `red` (the red zone always goes to the owner), merge_gate,
    plan_mismatch (OWNER_ONLY_CLASSES), any other key, an unknown class, a rec that is not a token."""
    if not isinstance(value, list):
        return f"must be a list of {{\"class\": ..., \"rec\": ...}} objects, got {value!r}"
    for i, rule in enumerate(value):
        if not isinstance(rule, dict):
            return f"[{i}] must be an object, got {rule!r}"
        if "red" in rule:
            return f"[{i}]: `red` is not a policy key: the red zone always goes to the owner"
        extra = sorted(set(rule) - {"class", "rec"})
        if extra:
            return f"[{i}]: unknown key(s) {', '.join(map(repr, extra))} (allowed: class, rec)"
        cls = rule.get("class")
        if isinstance(cls, str) and cls in OWNER_ONLY_CLASSES:
            return f"[{i}]: {OWNER_ONLY_CLASSES[cls]}"
        if cls not in POLICY_CLASSES:
            return f"[{i}]: class must be one of {', '.join(POLICY_CLASSES)}, got {cls!r}"
        rec = rule.get("rec")
        if "rec" in rule and not (isinstance(rec, str) and REC_TOKEN.fullmatch(rec)):
            return f"[{i}]: rec must match [A-Za-z0-9_.-]{{1,40}} (or be left out), got {rec!r}"
    return None


def decision_policy(cfg):
    """The rules of chain.json `decision_policy` (checked by load_chain) as [{class, rec}], rec None =
    any recommended variant of that class. No field: no automatic answers."""
    return [{"class": r["class"], "rec": r.get("rec")} for r in cfg.get("decision_policy") or []]


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
    if "plan_sha256" not in cfg:
        return
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
        raise SystemExit(f"wab: prompt file {path}: cannot resolve the path ({e}); launch refused")
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
    check_plan_pin(cfg)  # before prepare_clone, the workdir and the STARTING record
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
    save_state(cfg, st)
    start_session(cfg, st, wave)
    return deliver_first_prompt(cfg, st, wave)


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
        event(cfg, f"{wave}: max-runs file not written: {e}")


def start_session(cfg, st, wave):
    """launching -> starting: create the tmux window (same session id on a repeat)."""
    w = st["waves"][wave]
    wdir = wave_dir(cfg, wave)
    sync_max_runs(cfg, wave)
    cmd = ["claude", "--permission-mode", "auto", "--append-system-prompt-file", str(system_prompt(cfg)),
           "--name", f"wab-{cfg['chain']}-{wave}", "--session-id", w["sessions"][0]]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    pin = ["-e", f"WAB_PLAN_SHA256={cfg['plan_sha256']}"] if "plan_sha256" in cfg else []
    tmux("new-session", "-d", "-s", w["tmux"], "-c", w["cwd"], "-x", "220", "-y", "60",
       "-e", f"WAB_DIR={wdir}", "-e", f"WAB_WAVE={wave}", "-e", f"WAB_MAX_RUNS={cfg['max_runs']}", *pin, *cmd)
    w["phase"] = "starting"
    save_state(cfg, st)


def _plan_pin_refused(cfg, st, wave, drop_pending=False):
    """A pinned plan that changed while the launch was half done (the dispatcher died after the
    intent was saved): no session, no prompt. Phase not_ready (the chain stops, a `launch`
    retries through _launch, which checks the pin again), the reason in status, event, notice."""
    try:
        check_plan_pin(cfg)
    except SystemExit as e:
        why = str(e)
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
            put_notice(w, "not_ready", "plan_pin",
                       f"wave-autobot: волна {wave} не запущена: {quote(why)}{extra}. Цепочка стоит.")
        save_state(cfg, st)
        event(cfg, f"{wave}: {why}; session/prompt NOT started"
                   + (f"; session closed" if closed else f"; {left}" if left else ""))
        flush_notices(cfg, st, w)
        return True
    return False


def recover_launch(cfg, st, wave):
    """Dispatcher died around `tmux new-session`: a live window means it was started,
    otherwise start it again with the same session id. Then carry on as `starting`."""
    w = st["waves"][wave]
    if _plan_pin_refused(cfg, st, wave):
        return
    if tmux_alive(w["tmux"]):
        event(cfg, f"{wave}: resumed in phase 'launching': window exists, not starting another")
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
        put_notice(w, "dead", "1", f"wave-autobot: окно волны {wave} закрылось, статус {quote(status)}. Цепочка стоит.")
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
            put_notice(w, "tmux_failed", what,
                       f"wave-autobot: не удалось выполнить «{what}» в окне волны {wave} "
                       f"({type(e).__name__}). Окно живо: {attach_cmd(w['tmux'])}")
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
        event(cfg, f"{wave}: {what} postponed: {e}")
        return False
    try:
        if w.get("pending_clear") and not _settle_clear(cfg, st, wave, w, locked=True):
            # an earlier text may still sit in the input line: nothing is typed over it
            if once_per(w, "input_postponed", str(w["pending_clear"].get("what"))):
                event(cfg, f"{wave}: {what} postponed: the input line is not cleared yet")
            return False
        w.get("notified", {}).pop("input_postponed", None)
        try:
            fresh = precheck() if precheck is not None else True
        except gate.CollectError as e:  # the facts could not be read: neither typed, nor Enter, nor clearing
            if once_per(w, "precheck_error", f"{what}: {e}"[:150]):
                event(cfg, f"{wave}: {what} postponed: facts not collected under the input lock: {e}")
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
                    put_notice(w, "unverified_enter", what,
                               f"wave-autobot: {wave}: «{what}» дожат Enter без проверки отправки "
                               f"(state старой версии без начала текста). Проверь окно: {attach_cmd(name)}")
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


def _abandon_input(cfg, st, wave, w, why, locked=False):
    """The single point where a typed text that is waiting for its Enter (`pending_enter`) is given up
    WITHOUT delivery. It is not just forgotten: the fact moves to the reserve `pending_clear`, the input
    line is cleared and the SCREEN must show it empty (_settle_clear). Until then nothing new is typed
    into this window (_deliver, `say`). `locked`: the caller already holds the window's input lock."""
    what = w.get("pending_enter")
    if what:
        w["pending_clear"] = {"what": what, "why": why}
        w.pop("pending_enter", None)
        w.pop("pending_text_head", None)
        save_state(cfg, st)
    return _settle_clear(cfg, st, wave, w, locked=locked)


def _settle_clear(cfg, st, wave, w, locked=False):
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
            left = clear_input(name)
        else:
            with _InputLock(cfg, wave):
                left = clear_input(name)
    except NotSubmitted as e:
        left = str(e)
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


def deliver_first_prompt(cfg, st, wave):
    """starting -> sending -> running. The phase is saved before the paste: a dispatcher
    that dies in between leaves `sending`, which is never resent blindly."""
    w = st["waves"][wave]
    name, cwd, wdir = w["tmux"], w["cwd"], wave_dir(cfg, wave)
    if _plan_pin_refused(cfg, st, wave):
        return False
    if not wait_ready(name):
        blocked = "BLOCKED: окно Claude не стало готовым, задача не отправлена\n"
        (wdir / "status").write_text(blocked, encoding="utf-8")
        w["phase"] = "not_ready"
        put_notice(w, "not_ready", "1",
                   f"wave-autobot: окно волны {wave} не стало готовым за 90 с, задачу не отправил. "
                   f"Цепочка стоит. Посмотри: {attach_cmd(name)}")
        save_state(cfg, st)
        event(cfg, f"{wave}: Claude TUI not ready in {name}, prompt NOT sent")
        flush_notices(cfg, st, w)
        return False
    # the wait above lasts up to 90 s: waves.json may have changed meanwhile
    if _plan_pin_refused(cfg, st, wave):
        return False
    prompt = pathlib.Path(w["prompt_file"]).read_text(encoding="utf-8").strip()
    head = (f"{session_marker(cfg, wave)} [wave-autobot] Волна {wave}. Каталог волны: {wdir} "
            f"(он же $WAB_DIR). Рабочая копия (admitted clone): {cwd}. "
            f"Протокол — в системной инструкции.\n\n")
    n = w.get("restart")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        head += RESTART_NOTE.format(wave=wave, n=n) + "\n\n"
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
        put_notice(w, "started", "1",
                   f"wave-autobot: стартовала волна {wave}.\nСмотреть: {attach_cmd(w['tmux'])}\n(выйти: Ctrl-b d)")
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
    if isinstance(name, str) and name and tmux_alive(name):
        wave = next((k for k, r in (st.get("waves") or {}).items() if r is w), None) or name
        try:
            with _InputLock(cfg, wave):
                menu = exit_dialog(pane_text(name))
                if menu is not None:  # our /exit is being asked about the wave's background tasks
                    if not menu:
                        if once_per(w, "exit_blocked", "exit menu: option 1 not highlighted"):
                            event(cfg, f"{wave}: /exit menu on screen, option 1 not highlighted; "
                                       f"not confirmed, retried next tick")
                        save_state(cfg, st)
                        return False
                    press_enter(name)
                    event(cfg, f"{wave}: /exit menu confirmed: exit and stop the wave's background tasks")
                    return True
                if w.get("pending_enter"):
                    _abandon_input(cfg, st, wave, w, "the window is being closed", locked=True)
                elif w.get("pending_clear"):
                    _settle_clear(cfg, st, wave, w, locked=True)
                if w.get("pending_clear"):
                    left = "the earlier input is not cleared"
                else:
                    left = clear_input(name)  # None: empty now; «no input box» (a dialog, a failed capture) is
                    # NOT a go: the Enter after /exit could press a button of the dialog; held, retried
                if left is not None:
                    if once_per(w, "exit_blocked", left):
                        event(cfg, f"{wave}: /exit not sent: {left}; retried next tick")
                    save_state(cfg, st)
                    return False
                w.get("notified", {}).pop("exit_blocked", None)
                tmux("send-keys", "-t", pane_target(name), "-l", "/exit", check=False)
                tmux("send-keys", "-t", pane_target(name), "Enter", check=False)
        except (NotSubmitted, subprocess.SubprocessError, OSError) as e:  # a vanished pane, a busy input
            why = str(e) if isinstance(e, NotSubmitted) else type(e).__name__
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
        if not (isinstance(name, str) and name and tmux_alive(name)):
            w.pop("pending_exit", None)
            return True
        if not w.get("pending_exit") or attempt == EXIT_WAIT:
            return False
        if attempt == 0 and push:
            close_window(cfg, st, w)
        elif attempt and exit_dialog(pane_text(name)) is not None:
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
                put_notice(w, "no_prompt", "1",
                           f"wave-autobot: волна {wave} запущена, но файла с задачей уже нет, "
                           f"задачу не отправил. Посмотри: {attach_cmd(name)}")
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
            put_notice(w, "sending", "1",
                       f"wave-autobot: диспетчер перезапустился при отправке задачи волне {wave}; "
                       f"неизвестно, дошла ли она, повторно не слал. Проверь окно: {attach_cmd(name)}")
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


def finish_update(cfg, st, wave):
    """Last step of the /clear + /update transition: one restart counted. `status` belongs to
    the wave and is not written; the stale HANDOFF_READY on disk is remembered by its mtime."""
    w = st["waves"][wave]
    w["restarts"] = w.get("restarts", 0) + 1
    w["phase"] = "running"
    w["checkpoint_at"] = None
    w["resuming"] = True
    w["resuming_mtime"] = _status_mtime(cfg, wave)
    save_state(cfg, st)


def recover_update(cfg, st, wave):
    """The dispatcher died while /update was in flight: whether it arrived is unknown, so it
    is NOT resent. The wave goes on with await_session set; the owner is told once."""
    w = st["waves"][wave]
    if w.get("pending_enter") == "/update":  # typed, only Enter is missing: finish it, nothing is unknown
        if _deliver(cfg, st, wave, "/update", send_text, ""):
            w["await_session"] = True
            finish_update(cfg, st, wave)
        return
    fresh = once_per(w, "updating", str(w.get("restarts", 0)))
    informed = bool(w.get("notified", {}).get("tmux_failed"))  # the owner already got the failure notice
    w["await_session"] = True
    # the wave already wrote something new (RUNNING, BLOCKED, DONE...): /update did arrive
    arrived = read(cfg["run_dir"] / wave / "status") not in ("HANDOFF_READY", "", "STARTING")
    event(cfg, f"{wave}: resumed in phase 'updating': "
               f"{'/update evidently arrived' if arrived else 'unknown whether /update arrived'}, NOT resent")
    if fresh and not arrived and not informed:  # keyed by the restart count BEFORE finish_update
        put_notice(w, "updating", str(w.get("restarts", 0)),
                   f"wave-autobot: диспетчер перезапустился при передаче /update волне {wave}; "
                   f"неизвестно, дошла ли команда, повторно не слал. Проверь окно: {attach_cmd(w['tmux'])}")
    finish_update(cfg, st, wave)  # saves the phase, the mark and the notice together
    flush_notices(cfg, st, w)


def next_prompt_problem(path):
    """Why the next wave could not start from this next-prompt.md (the same read_prompt check as
    its launch), or None when it can."""
    try:
        read_prompt(path)
    except SystemExit as e:
        return str(e)
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
        put_notice(w, "no_next", "1",
                   f"wave-autobot: {wave} готова, но next-prompt.md непригоден ({quote(why)}) — "
                   f"следующую волну не запускаю." if why else
                   f"wave-autobot: {wave} готова, но нет next-prompt.md — следующую волну не запускаю.")
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
        put_notice(w, "launch_refused", why,
                   f"wave-autobot: {wave} готова, но следующую волну {nxt} не запустил: {quote(why)}. "
                   f"Цепочка стоит; устрани причину и запусти {nxt} (wab.py launch).")
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
        why = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:200] or f"rc={r.returncode}"
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


def read_manifest(cfg, wave):
    """<wave dir>/superarmanda/manifest.json as written by state.py; None when it cannot be read."""
    try:
        data = json.loads((cfg["run_dir"] / wave / "superarmanda" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def gate_check(cfg, wave, w):
    """The verdict of the merge gate for the wave's PR. Never raises: whatever goes wrong is a
    `wait` (and never a `pass`)."""
    base = {"pr": {}, "head": None, "unresolved": [], "draft": False, "number": None}
    try:
        pr = find_pr(cfg, w["cwd"])
        if pr is None:
            return dict(base, verdict="fail", reasons=["PR ветки волны не найден"])
        base["number"] = pr.get("number")  # kept even if the facts below fail: the hand-off names the PR (#34)
        facts = gate_facts(cfg, pr)
        v = gate.evaluate(facts, pr["headRefOid"], read_manifest(cfg, wave), workdir_state(w["cwd"]),
                          base_branch_of(cfg, w["cwd"]))
        v["number"] = pr["number"]
        if v["verdict"] == "pass" and gate.critical(gate_facts(cfg, pr)) != gate.critical(facts):
            # a CI rerun or a new finding between the reads: the pass would rest on stale facts
            return dict(v, verdict="wait", reasons=["факты изменились во время сбора"])
        return v
    except gate.CollectError as e:
        return dict(base, verdict="wait", reasons=[f"сбор фактов: {e}"])
    except Exception as e:  # noqa: BLE001 - fail closed: an unexpected error is a wait, never a pass
        return dict(base, verdict="wait", reasons=[f"гейт: {type(e).__name__}: {e}"])


def _one_line(text, limit=MAX_STATUS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 2].rstrip() + " …"


def _write_status(wdir, text):
    """The dispatcher's own status line, atomically (the wave may read the file at any moment)."""
    fd, tmp = tempfile.mkstemp(dir=wdir, prefix=".status.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_one_line(text) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, wdir / "status")
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    return _one_line(text)


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
    """One line for the hand-off notice of `merge_gate: external`: the verdict and, when the gate
    passed, the command (or the owner's script). Best effort: nothing here may stop the hand-off."""
    try:
        v = gate_check(cfg, wave, w)
        _note_pr(w, v.get("number"))  # whatever the verdict: the hand-off carries the PR number (#34)
        reasons = _one_line("; ".join(v["reasons"]), 300)
        if v["verdict"] == "wait" and reasons.startswith("сбор фактов"):
            return f"Гейт мерджа не проверен: {quote(reasons)}"
        if v["verdict"] != "pass":
            return f"Гейт мерджа {'ждёт' if v['verdict'] == 'wait' else 'не пройден'}: {quote(reasons)}"
        number, sha = v["number"], v["head"]
        w["pr"] = number
        w["gate_sha"], w["gate_pr"] = sha, number  # saved with the hand-off: `owner-merge` gates against them
        if v["unresolved"]:
            path = write_owner_script(cfg, wave, sha)
            return (f"Гейт мерджа пройден, но есть незакрытые треды ({len(v['unresolved'])}"
                    f"{gate.threads_note(v['old_p01'])}). "
                    f"Выполни: {home_form(path)}")
        path = write_owner_script(cfg, wave, sha)  # never the raw `gh pr merge`: the script gates again
        return f"Гейт мерджа пройден. Выполни: {home_form(path)} (заново проверит гейт на этом HEAD; draft PR сперва переведёт в ready и остановится, потом запусти ещё раз, чтобы смержить)"
    except Exception as e:  # noqa: BLE001
        return f"Гейт мерджа не проверен: {type(e).__name__}: {quote(_one_line(e, 200))}"


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
    event(cfg, f"{wave}: {what}, but the status is «{_one_line(again or '', 80)}» now: nothing done")
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
        return _gate_failed(cfg, st, wave, w, wdir, f"недопустимые repo/PR/sha: {e}")
    w["gate_sha"], w["gate_pr"] = sha, number
    unresolved = v["unresolved"]
    if v["reasons"]:  # a pass carries only the accepted limitations (`accepted <sev> <source>: <note>`)
        event(cfg, f"{wave}: merge gate passed with accepted limitations: {_one_line('; '.join(v['reasons']), 600)}")
    if v["draft"] and not unresolved:
        return _gate_ready(cfg, st, wave, w, wdir, number, sha)
    w["phase"] = "merging"
    w["merge_poll_at"] = time.time()
    if unresolved:  # the owner closes the threads and merges; nothing is merged here
        try:
            path = write_owner_script(cfg, wave, sha)
        except (OSError, ValueError) as e:
            return _gate_failed(cfg, st, wave, w, wdir, f"скрипт владельца не записан: {e}")
        w["last_status"] = _write_status(
            wdir, f"BLOCKED: merge gate passed; {len(unresolved)} unresolved review threads; owner runs {home_form(path)}")
        note_question(w, w["last_status"], time.time())
        put_notice(w, "merge_owner", sha,
                   f"wave-autobot: волна {wave}: гейт мерджа пройден (PR #{number}, {sha[:12]}), но есть "
                   f"незакрытые треды ревью: {len(unresolved)}{gate.threads_note(v['old_p01'])}. Сам не мержу.\nВыполни: {home_form(path)}\n"
                   f"Скрипт заново проверит гейт на этом HEAD и закроет треды; если PR draft, переведёт его в ready и остановится: дождись проверок и запусти скрипт ещё раз, он смержит.")
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
    return _hand_to_owner(cfg, st, wave, w, wdir, number, sha, f"гейт пройден, мердж отклонён: {quote(refused)}",
                          f"merge refused: {refused}")


def _run_gh_step(argv):
    """One gh call; None on success, else a short reason."""
    try:
        r = sh(*argv, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return type(e).__name__
    if r.returncode != 0:
        return _one_line(r.stderr or r.stdout or f"rc={r.returncode}", 200)
    return None


def _gate_ready(cfg, st, wave, w, wdir, number, sha):
    """A draft PR that passed the gate is only made ready here; the merge waits for the NEXT gate on a
    non-draft PR (`ready_for_review` may start new checks). Once per sha: a PR GitHub keeps showing
    as draft goes to the owner instead of a loop of `gh pr ready`."""
    if w.get("ready_called") == sha:
        w["phase"] = "merging"
        w["merge_poll_at"] = time.time()
        return _hand_to_owner(cfg, st, wave, w, wdir, number, sha,
                              f"PR #{number} после gh pr ready всё ещё draft", "PR still draft after ready")
    w["ready_called"] = sha  # saved BEFORE the call: ready is asked once per sha
    save_state(cfg, st)
    refused = _run_gh_step(["gh", "pr", "ready", str(number), "--repo", cfg["repo"]])
    if refused is not None:
        w["phase"] = "merging"
        w["merge_poll_at"] = time.time()
        return _hand_to_owner(cfg, st, wave, w, wdir, number, sha,
                              f"гейт пройден, перевод в ready отклонён: {quote(refused)}", f"ready refused: {refused}")
    w["phase"] = "gate"  # the new checks must register before the next collection
    w["gate_at"] = time.time()
    save_state(cfg, st)
    event(cfg, f"{wave}: PR #{number} переведён в ready, жду проверки")
    return True


def _hand_to_owner(cfg, st, wave, w, wdir, number, sha, text, log_text):
    try:  # the owner gets the script (it gates again, readies a draft and stops, then merges pinned to the sha), not the raw command
        command = home_form(write_owner_script(cfg, wave, sha))
    except (OSError, ValueError) as e:
        command = f"(скрипт владельца не записан: {_one_line(e, 100)}; проверь PR #{number} вручную)"
    w["last_status"] = _write_status(wdir, f"BLOCKED: merge gate passed; {log_text}")
    note_question(w, w["last_status"], time.time())
    put_notice(w, "merge_refused", sha, f"wave-autobot: волна {wave}: {text}. Выполни: {command}")
    save_state(cfg, st)
    event(cfg, f"{wave}: merge gate passed, {log_text}")
    flush_notices(cfg, st, w)
    return True


def _merge_stopped(cfg, st, wave, w, wdir, why, now):
    """The PR was merged with another head or closed: the next wave is not launched by the
    dispatcher; the coordinator takes over (awaiting_merge) and decides."""
    w["last_status"] = _write_status(wdir, f"BLOCKED: merge gate: {why}")
    note_question(w, w["last_status"], now)
    w["phase"] = "awaiting_merge"
    w["finished"] = now
    w["pending_exit"] = True
    if once_per(w, "merge_stopped", why):
        put_notice(w, "merge_stopped", why,
                   f"wave-autobot: волна {wave}: {quote(why)}. Следующую волну не запускаю: проверь PR и реши сам.")
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
        raise SystemExit(f"wab: owner-merge: the gate is {v['verdict']}: {'; '.join(v['reasons'])}; nothing done")
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
            raise SystemExit(f"wab: owner-merge: thread {thread} not resolved: {e}")
        resolved = None
        try:
            resolved = out["data"]["resolveReviewThread"]["thread"]["isResolved"]
        except (KeyError, TypeError):
            pass
        if resolved is not True:  # null, no field, false, errors: the thread is NOT closed
            why = str(out.get("errors"))[:200] if isinstance(out, dict) and out.get("errors") else "isResolved is not true"
            raise SystemExit(f"wab: owner-merge: thread {thread} not resolved: {why}; nothing merged")
    if v["draft"]:  # ready may start new checks: stop here, the next run gates again on a non-draft PR
        argv = ["gh", "pr", "ready", str(number), "--repo", repo]
        r = sh(*argv, check=False, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"wab: owner-merge: `gh pr ready` failed: "
                             f"{_one_line(r.stderr or r.stdout or r.returncode, 200)}")
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
                             f"{'; '.join(v2['reasons'])}; ничего не смержено")
        if v2["head"] != w["gate_sha"] or v2["number"] != w.get("gate_pr"):
            raise SystemExit(f"wab: owner-merge: гейт изменился после закрытия тредов: PR head "
                             f"{str(v2['head'])[:12]} is not the gated {w['gate_sha'][:12]}; ничего не смержено")
        if v2["unresolved"]:
            raise SystemExit(f"wab: owner-merge: гейт изменился после закрытия тредов: появились новые треды, "
                             f"запусти скрипт ещё раз ({len(v2['unresolved'])} open); ничего не смержено")
    r = sh(*merge, check=False, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"wab: owner-merge: `{' '.join(merge[:3])}` failed: "
                         f"{_one_line(r.stderr or r.stdout or r.returncode, 200)}")
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
            raise SystemExit(f"wab: {what}: PR not read: {e}; nothing done")
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
            raise SystemExit(f"wab: {what}: git status failed in {cwd}: {_one_line(dirty.stderr, 200)}; nothing done")
        if dirty.stdout.strip():
            raise SystemExit(f"wab: {what}: the wave's working copy {cwd} is not clean (uncommitted or untracked "
                             f"changes: not what was merged); commit or remove them; nothing done")
        fetch = git("fetch", "origin", base)
        if fetch.returncode != 0:
            raise SystemExit(f"wab: {what}: git fetch origin {base} failed: "
                             f"{_one_line(fetch.stderr, 200)}; nothing done")
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
        return _merge_stopped(cfg, st, wave, w, wdir, "в записи волны нет gate_pr/gate_sha", now)
    try:
        info = _json(_gh("pr", "view", str(number), "--repo", str(cfg["repo"]), "--json",
                         "state,mergeCommit,headRefOid,baseRefName"), "gh pr view")
        if not isinstance(info, dict):
            raise gate.CollectError("gh pr view: unexpected response")
    except gate.CollectError as e:
        if once_per(w, "merge_view_error", str(e)[:150]):
            event(cfg, f"{wave}: PR #{number} state not read: {e}")
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
                                  f"PR #{number} смержен в {got_base}, цепочка ждёт {want_base}", now)
        head = info.get("headRefOid")
        if head != sha:
            return _merge_stopped(cfg, st, wave, w, wdir,
                                  f"PR #{number} смержен с HEAD {str(head)[:12]}, а гейт проверял {sha[:12]}", now)
        w["merged"] = {"pr": number, "sha": sha, "commit": (info.get("mergeCommit") or {}).get("oid"),
                       "at": now}
        return _complete_wave(cfg, st, wave, w, wdir, now)
    if state == "CLOSED":
        return _merge_stopped(cfg, st, wave, w, wdir, f"PR #{number} закрыт без мерджа", now)
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
            command = f"(скрипт владельца не записан: {_one_line(e, 100)}; проверь PR #{number} вручную)"
        w["merge_rc"] = None  # marker: handed to the owner
        w["last_status"] = _write_status(
            wdir, f"BLOCKED: merge gate passed; merge result unknown; owner runs {command}")
        note_question(w, w["last_status"], now)
        put_notice(w, "merge_unknown", sha,
                   f"wave-autobot: волна {wave}: гейт пройден, но результат вызова мерджа PR #{number} "
                   f"неизвестен (диспетчер прерывался). Сам повторно не мержу. Выполни: {command}")
        save_state(cfg, st)
        event(cfg, f"{wave}: PR #{number} merge result unknown; handed to the owner: {command}")
        flush_notices(cfg, st, w)
        return True
    if "merge_rc" not in w and once_per(w, "merge_unknown", sha):
        event(cfg, f"{wave}: PR #{number} is not merged and the result of the merge call is unknown; waiting")
    save_state(cfg, st)
    return True


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
        if once_per(w, "alarm_error", str(e)[:150]):
            event(cfg, f"{wave}: alarm: PR facts not collected: {e}")
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
        if once_per(w, "alarm_error", str(e)[:150]):
            event(cfg, f"{wave}: alarm: PR facts not collected: {e}")
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


def note_question(w, text, now):
    """Record a question to the owner (an owner-facing BLOCKED line) for the chain result: redacted,
    the same text twice in a row once, capped. In memory only: the caller saves it with its change."""
    qs = w.setdefault("questions", [])
    text = redact(str(text), 300)
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
    span = f"{_utc(min(starts))} - {_utc(max(ends))}" if starts and ends else "?"
    lines = [f"# Итог цепочки {cfg.get('chain')}\n", f"- run: {cfg.get('run_id')}",
             f"- начало / конец (UTC): {span}", f"- Время общее: {_human_dur(total)}",
             f"- волн: {len(cfg['waves'])}, PR: {prs}, перезапусков голов: {restarts_heads}, "
             f"перезапусков волн: {restarts_waves}, вопросов: {len(questions)}\n", "## Волны\n", *rows,
             "## Вопросы к владельцу\n"]
    lines += ([f"- {_utc(q.get('at'))} {wave}: {redact(str(q.get('text') or ''), 300)}"
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
    return path, summary


def chain_done_text(cfg, st):
    """The end-of-chain message: the short summary and the path of chain-result.md. A failed write
    costs the file, never the message or the watch."""
    try:
        path, summary = write_chain_result(cfg, st)
    except Exception as e:  # noqa: BLE001 - the end of the chain is always reported and saved
        return f"wave-autobot: цепочка завершена, все волны готовы (chain-result.md не записан: {_one_line(e, 100)})."
    return (f"wave-autobot: цепочка завершена, все волны готовы.\n{summary}\n"
            f"Итог: chain-result.md в каталоге прогона {home_form(path.parent)}")


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
        put_notice(w, "done", "1",
                   f"wave-autobot: волна {wave} завершена.\n\n{quote(read(wdir / 'result.md'))}\n\n"
                   f"Целиком: {wdir}/result.md")
    w["phase"] = "done"
    last = idx + 1 >= len(waves)
    if last:  # the end of the chain goes into the same save as the mark
        st["current"] = None
        put_notice(w, "chain_done", "1", chain_done_text(cfg, st))
    save_state(cfg, st)
    if fresh:
        event(cfg, f"{wave}: DONE")
    close_window(cfg, st, w)
    if last:
        event(cfg, "chain finished")
        flush_notices(cfg, st, w)
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
        _launch_refused(cfg, st, wave, waves[idx + 1], str(e))
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
    tick ends; the next tick starts the new episode."""
    stamp = _status_stamp(cfg, wave)
    if _answered(w) == status:
        if w.get("policy_answered_stamp") in (None, stamp):
            return True
        # the same text, but the file was rewritten since the answer (e.g. RUNNING in between, unseen by
        # the poll): a NEW episode, not the answered one — otherwise the wave would wait silently
        w.get("notified", {}).pop("policy_answer", None)
        w.pop("policy_answered_stamp", None)
    label = parse_blocked_label(status)
    if label is None or label["red"] or label["class"] == "merge_gate":
        return False
    rules = decision_policy(cfg)
    if not any(r["class"] == label["class"] and r["rec"] in (None, label["rec"]) for r in rules):
        return False
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
    sent = _deliver(cfg, st, wave, "policy answer", send_text, POLICY_ANSWER.format(rec=label["rec"]),
                    precheck=still)  # re-read again UNDER the input lock: `say` may have answered meanwhile
    if sent is None:
        w.pop("policy_pending", None)
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
        save_state(cfg, st)
        return False
    w.pop("policy_pending", None)
    w.setdefault("notified", {})["policy_answer"] = status
    # the stamp of the status the answer was for, taken BEFORE typing: a fast wave may already have
    # rewritten the same line by now, and that rewrite must read as a new episode
    w["policy_answered_stamp"] = seen.get("stamp", stamp)
    w["auto_answers"] = (w["auto_answers"] if _count(w.get("auto_answers")) else 0) + 1
    drop_notice(w, "blocked")  # a usual BLOCKED signal of a failed first try asks nothing any more
    w.setdefault("notified", {})["blocked"] = status  # ...and is not raised again in this episode
    question = redact((label["question"].splitlines() or [""])[0], 200)
    try:
        with open(wave_dir(cfg, wave) / "policy-decisions.log", "a", encoding="utf-8") as f:
            f.write(f"{_utc(now)} class={label['class']} rec={label['rec']} {question}\n")
    except OSError as e:
        event(cfg, f"{wave}: policy-decisions.log not written ({e.strerror or e})")
    put_notice(w, "policy_answer", status,
               f"wave-autobot: волна {wave}: развилка закрыта по политике chain.json "
               f"(class={label['class']}, вариант {label['rec']}), ответ не нужен.\n\n"
               f"{quote(status)}\n\nСмотреть: {attach}")
    save_state(cfg, st)  # the mark, the counter and the notice together
    event(cfg, f"{wave}: policy auto-answer: class={label['class']} rec={label['rec']}")
    return True


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

    if w.get("phase") == "awaiting_merge":
        return False  # handed to the coordinator: the watch ends and frees the run lock
    if w.get("phase") == "not_ready":
        return False
    if w.get("phase") == "launching":
        recover_launch(cfg, st, wave)
        return True

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
        gate_line = f"{handoff_gate_line(cfg, wave, w)}\n\n" if external else ""
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
        put_notice(w, "handoff", "1",
                   f"wave-autobot: волна {wave} сдала PR.\n\n{quote(read(wdir / 'result.md'))}\n\n{gate_line}"
                   + ("Мердж и запуск следующей волны — за координатором." if not is_last else
                      f"Мердж последнего PR и завершение цепочки (wab.py done) — за координатором."))
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
            put_notice(w, "blocked", status,
                       f"wave-autobot: волна {wave} ждёт тебя.\n\n{quote(status)}\n\nОтветить: {attach}")
        save_state(cfg, st)
        if fresh:
            event(cfg, f"{wave}: {status[:200]}")
        flush_notices(cfg, st, w)
        return True

    if w.get("phase") == "updating":
        recover_update(cfg, st, wave)
        return True

    ready = handoff_ready(cfg, w, wave, status)
    if (ready and w.get("phase") == "checkpoint") or w.get("phase") == "clearing":
        event(cfg, f"{wave}: handoff ready, /clear + /update (restart #{w.get('restarts', 0) + 1})")
        w["phase"] = "clearing"  # saved before each outside action; a repeated /clear is safe
        save_state(cfg, st)
        if not _deliver(cfg, st, wave, "/clear", send_command, "/clear"):
            return True  # phase `clearing` is on disk: retried next tick
        w["phase"] = "updating"  # from here /update is never resent blindly
        w["await_session"] = True  # the next session is found by its marker, not by guesswork
        save_state(cfg, st)
        time.sleep(6)
        update = (f"/update {session_marker(cfg, wave)} Продолжаем волну {wave} wave-autobot. "
                  f"Каталог волны: {wdir}. Прочитай {wdir}/handoff.md и продолжи с шага «Следующий шаг».")
        if not _deliver(cfg, st, wave, "/update", send_text, update):
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
            put_notice(w, "checkpoint_timeout", str(w.get("checkpoint_at")),
                       f"wave-autobot: {wave} не записала handoff за {cfg['handoff_timeout_minutes']} мин "
                       f"после запроса. Посмотри: {attach}")
            save_state(cfg, st)
            flush_notices(cfg, st, w)

    if w.get("phase") == "running":  # a wave out of auto mode asks for every step
        off = screen["auto_off"]
        if off:  # its end (auto mode back) is in end_screen_episodes
            w["auto_off_ticks"] = w.get("auto_off_ticks", 0) + 1
            if w["auto_off_ticks"] >= 2 and not w.get("auto_alerted"):
                w["auto_alerted"] = True
                put_notice(w, "auto_off", "1",
                           f"wave-autobot: волна {wave} вышла из режима auto и будет спрашивать "
                           f"подтверждения. Вернуть: {attach}, Shift+Tab до «auto mode on».")
                save_state(cfg, st)
                event(cfg, f"{wave}: window is not in auto mode")
                flush_notices(cfg, st, w)

    if any(m in txt for m in PERMISSION_MARKERS):
        if once_per(w, "permission", "visible"):  # one episode while the prompt stays on screen
            put_notice(w, "permission", "visible",
                       f"wave-autobot: волна {wave} ждёт подтверждения на экране.\n{attach}")
            save_state(cfg, st)
            event(cfg, f"{wave}: permission prompt on screen")
            flush_notices(cfg, st, w)

    digest = screen["digest"]  # a new digest already restarted the clock (end_screen_episodes)
    if note_transcript_activity(w, now, cfg["idle_minutes"] * 60):  # a session bound in this tick
        save_state(cfg, st)
    active = max(w.get("pane_changed", now), w.get("activity_at") or 0)
    if now - active > cfg["idle_minutes"] * 60:
        if once_per(w, "idle", digest):
            put_notice(w, "idle", digest,
                       f"wave-autobot: волна {wave} молчит {cfg['idle_minutes']}+ мин "
                       f"(статус {quote(status)}). Возможно, ждёт тебя: {attach}")
            save_state(cfg, st)
            event(cfg, f"{wave}: pane idle {cfg['idle_minutes']}+ min, status={status}")
            flush_notices(cfg, st, w)
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
        event(cfg, f"Ctrl+\\ binding refused: {e}")
        print(f"wab: Ctrl+\\ binding refused: {e}", file=sys.stderr)
        return False
    if pane:  # a pane id (%N) is itself exact
        r = tmux_on(sock, "set-option", "-p", "-t", pane, "@wab_open", value, check=False)
    else:
        r = tmux_on(sock, "set-option", "-t", pane_target(session), "@wab_open", value, check=False)
    if r.returncode != 0:
        event(cfg, f"@wab_open not set: {r.stderr.strip() or r.stdout.strip()}")
        return False
    if not _ensure_binding(cfg, sock):
        return False
    event(cfg, "Ctrl+\\ toggles the wave popup")
    return True


def unregister_dash(session, sock="", pane=None):
    """Unset @wab_open of the dashboard's pane (else session) on exit, SIGTERM/SIGHUP.
    A killed pane or session needs no cleanup."""
    if pane:
        tmux_on(sock, "set-option", "-p", "-u", "-t", pane, "@wab_open", check=False)
    else:
        tmux_on(sock, "set-option", "-u", "-t", pane_target(session), "@wab_open", check=False)


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
        event(cfg, str(e))
        raise


TUNABLE = ("ctx_limit", "idle_minutes", "handoff_timeout_minutes", "tick_seconds", "telegram",
           "titles", "model", "plan_sha256", "max_auto_answers", "max_runs")


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
            event(cfg, f"chain.json not re-read, keeping the previous settings: {e}")
        except Exception as e:  # noqa: BLE001 - load_chain refuses with SystemExit; anything else is a
            # bug, and a live edit must still never kill the supervision
            event(cfg, f"chain.json not re-read, keeping the previous settings: {type(e).__name__}: {e}")
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
            put_notice(w, "chain_done", "1", chain_done_text(cfg, st))
            save_state(cfg, st)  # the phase and the notice together: a repeated `done` still owes it
            event(cfg, "chain finished")
            flush_notices(cfg, st, w)
        else:
            ack_wave_notices(w)  # a repeat call confirms too: only chain_done may still be owed
            save_state(cfg, st)
            flush_notices(cfg, st, w, force=True)  # a repeat call sends what was not delivered
        if w.get("pending_exit"):  # e.g. a crash between the awaiting_merge save and /exit
            if wait_window_closed(cfg, st, w, push=w.get("tmux") not in pushed):
                save_state(cfg, st)
            else:
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
    lines = [l for l in text.splitlines() if l.strip()]
    first = redact(lines[0] if lines else "", 120)
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
        why = str(e) if isinstance(e, NotSubmitted) else type(e).__name__
        event(cfg, f"{wave}: say FAILED ({why}): {first}")
        print(f"wab: say: the text did not leave the input line of {name}: {why}; "
              f"look: {attach_cmd(name)}", file=sys.stderr)
        return False
    event(cfg, f"{wave}: say: {first}")
    return True


def status_cmd(cfg):
    st = load_state(cfg)
    print("current:", st.get("current"))
    for wave, w in st["waves"].items():
        print(f"{wave}: tmux={w.get('tmux')} phase={w.get('phase')} ctx={w.get('tokens', 0) // 1000}k "
              f"restarts={w.get('restarts', 0)} sessions={len(w.get('sessions') or [])} "
              f"status={read(cfg['run_dir'] / wave / 'status')}")


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
    elif cmd == "owner-merge" and len(argv) == 6:
        owner_merge(load_chain(path, create=False), argv[3], argv[4], argv[5])
    elif cmd == "owner-merge":
        sys.exit("wab: owner-merge <chain.json> <wave> <run_id> <sha>: run_id and sha are required "
                 "(an old script without them is refused; use the script the dispatcher wrote last)")
    elif cmd == "owner-handover" and len(argv) == 5:
        owner_handover(load_chain(path, create=False), argv[3], argv[4])
    elif cmd == "notify":
        notify(load_chain(path), " ".join(argv[3:]))
    elif cmd == "attention":
        sys.exit(attention_cmd(load_chain(path, create=False)))
    elif cmd == "say" and len(argv) == 5:
        if not say_cmd(load_chain(path, create=False), argv[3], argv[4]):
            sys.exit(3)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
