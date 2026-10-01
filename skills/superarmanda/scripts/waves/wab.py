#!/usr/bin/env python3
"""wave-autobot dispatcher: run plan waves one per tmux Claude session.

Commands:
  wab.py launch <chain.json> <wave> <prompt-file>   start one wave in tmux
  wab.py watch  <chain.json>                        supervise until the chain ends
  wab.py status <chain.json>                        one-screen status
  wab.py notify <chain.json> <text>                 Telegram message to the owner
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
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
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
# The merge gate (wait for MERGED, fetch the base, ancestor check) is W5. Until it exists a DONE of a
# non-last wave never launches the next one by itself: it is handed to the coordinator like `external`.
MERGE_GATE_IMPLEMENTED = False

NUM_LIMITS = {"ctx_limit": 10_000_000, "idle_minutes": 1440, "handoff_timeout_minutes": 1440,
              "tick_seconds": 3600}


def _plain_name(value):
    return bool(NAME.fullmatch(value)) and set(value) != {"."}


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
    if cfg["model"] is not None and not isinstance(cfg["model"], str):
        raise SystemExit(f"chain.json: model must be a string or null, got {cfg['model']!r}")
    titles = cfg.get("titles")
    if titles is not None and not (isinstance(titles, dict)
                                   and all(isinstance(v, str) for v in titles.values())):
        raise SystemExit("chain.json: titles must be an object of wave id -> string")
    tg = cfg.get("telegram")
    if tg and not isinstance(tg, dict):
        raise SystemExit("chain.json: telegram must be an object")
    if tg and not all(isinstance(tg.get(k), str) and tg[k] for k in ("keyvault", "token_secret", "chat_secret")):
        # an enabled block with a hole would silently turn every notice into a local event
        raise SystemExit("chain.json: telegram needs non-empty string keyvault, token_secret and chat_secret "
                         "(or leave it out / empty to disable)")
    chain = str(cfg.get("chain") or "")
    run_id = str(cfg.get("run_id") or "")
    if not _plain_name(chain):
        raise SystemExit("chain.json: chain is required ([A-Za-z0-9._-]+, not a dot segment)")
    if not _plain_name(run_id):
        raise SystemExit("chain.json: run_id is required ([A-Za-z0-9._-]+, not a dot segment), "
                         "one per approved run")
    if not PREFIX.fullmatch(str(cfg["tmux_prefix"])):
        raise SystemExit("chain.json: tmux_prefix must match [A-Za-z0-9_-]+")
    if cfg.get("merge_gate") not in (None, "", "external"):
        raise SystemExit(f"chain.json: merge_gate must be absent or \"external\", got {cfg['merge_gate']!r}")
    waves = cfg.get("waves")
    if (not isinstance(waves, list) or not waves
            or not all(isinstance(w, str) and WAVE_NAME.fullmatch(w) for w in waves)):
        raise SystemExit("chain.json: waves must be a non-empty list of [A-Za-z0-9_-]+ names")
    if len({w.lower() for w in waves}) != len(waves):  # tmux session names are lower-cased
        raise SystemExit("chain.json: duplicate wave id in waves (compared case-insensitively)")
    here = path.resolve().parent
    # a relative run_dir is relative to chain.json, never to the cwd of whoever runs us
    base = pathlib.Path(os.path.abspath(here / cfg["run_dir"])) if cfg.get("run_dir") else here / "runs"
    if cfg.get("workdir"):  # absolute before admission, state and transcript lookup
        cfg["workdir"] = str(pathlib.Path(os.path.abspath(here / cfg["workdir"])).resolve())
    cfg["chain_file"] = str(path.resolve())
    cfg["run_dir"] = base / chain / run_id
    if create:
        cfg["run_dir"].mkdir(parents=True, exist_ok=True)
    return cfg


_HELD = set()  # lock files held by THIS process (a flock is not re-entrant across descriptors)


class _RunLock:
    """One dispatcher per run: flock on <run_dir>/dispatcher.lock. Taken by `watch` for its
    whole life and by the CLI `launch`; a launch inside a running watch reuses the held lock."""

    def __init__(self, cfg, what):
        self.path = cfg["run_dir"] / "dispatcher.lock"
        self.what = what
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
            raise SystemExit(f"wab: another dispatcher holds {self.path} ({self.what}); "
                             f"запусти волну через watch или останови диспетчер")
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


def save_state(cfg, st):
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


def read(p):
    p = pathlib.Path(p)
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else ""


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


def send_text(name, text, on_typed=None):
    """Paste text as one bracketed paste, then submit it. The buffer is private to this
    dispatcher and session: tmux buffers are global to the server. `on_typed` runs once the
    text is in the window and Enter is still to come (the caller records that step)."""
    buf = f"wab-{os.getpid()}-{name}"
    tmux("load-buffer", "-b", buf, "-", input=text)
    tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane_target(name))
    if on_typed:
        on_typed()
    time.sleep(1.5)
    press_enter(name)


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
    return projects_dir() / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


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


def session_marker(cfg, wave):
    return f"[wab:{cfg['chain']}/{cfg['run_id']}/{wave}]"


def _first_user_text(path):
    """Text of the first main-thread user message in the first 256 KB of a transcript
    (a string, or the text blocks of a content list; tool_result blocks do not count)."""
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
        if not isinstance(d, dict) or d.get("type") != "user" or d.get("isSidechain"):
            continue
        msg = d.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
            if texts:
                return "\n".join(texts)
    return ""


def owned_sessions(st):
    """Every session id that already belongs to some wave: current sessions AND those of all
    earlier attempts (a restarted wave keeps its old transcripts, which carry the same marker)."""
    owned = set()
    for w in st["waves"].values():
        for rec in [w, *(w.get("attempts") or [])]:
            owned.update(rec.get("sessions") or [])
    return owned


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
_REDACT = [
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
    re.compile(r"\+\d[\d ()-]{8,}\d"),                                  # phone numbers (+7 ...)
    re.compile(r"(?<![\w+])(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"),  # RU, no plus
    re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{2}[-\s]\d{2}(?!\d)"),             # 10 digits, separated
    re.compile(r"\b[A-Za-z0-9+/_]{40,}={0,2}"),                          # long opaque blobs
]


_SHA_LABEL = re.compile(r"(?i)(?:\b(?:commit|sha|head|reviewed_head|base|packet_hash)(?:\s+|\s*[=:]\s*)"
                        r"|sha256\s*[:=]\s*)$")


def _keep_match(m):
    """A bare 40-64 hex string is a key; only one right after an explicit SHA/hash label is a commit id."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", m.group(0))
                and _SHA_LABEL.search(m.string[:m.start()]))


def redact(text, limit=TG_LIMIT):
    for rx in _REDACT:
        text = rx.sub(lambda m: (m.group(1) + m.group(2) + "[скрыто]") if rx.groups >= 2 and m.group(2)
                      else m.group(0) if _keep_match(m) else "[скрыто]", text)
    return text if len(text) <= limit else text[:limit - 2].rstrip() + " …"


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
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20):
        pass


def notify(cfg, text):
    tg = cfg.get("telegram")
    lines = [l for l in text.splitlines() if l.strip()]
    first = redact(lines[0], 120) if lines else ""
    if not (isinstance(tg, dict) and all(tg.get(k) for k in ("keyvault", "token_secret", "chat_secret"))):
        event(cfg, f"notify(skipped): {first}")
        return True  # Telegram is not configured: there is nothing to repeat
    try:
        _send_telegram(cfg, redact(text, TG_MESSAGE_LIMIT))
        event(cfg, f"telegram: {first}")
        return True
    except Exception as e:  # notification must never stop supervision
        event(cfg, f"telegram FAILED ({type(e).__name__}): {first[:80]}")
        return False


NOTIFY_RETRY_SECONDS = 300  # a standing notice that failed is repeated no more often than this


def queue_notice(cfg, st, w, key, value, text):
    """A notice about a standing episode (`key`, `value`). It is written to the state BEFORE the
    send and removed only after the transport took it, so a transient Telegram failure is
    retried by later ticks (flush_notices) instead of being lost; supervision never waits for it."""
    box = w.setdefault("outbox", {})
    cur = box.get(key)
    if cur is None or cur.get("value") != value:
        box[key] = {"value": value, "text": text, "next_at": 0}
    save_state(cfg, st)
    flush_notices(cfg, st, w)


def drop_notice(w, key):
    """The episode is over: forget it, and a notice not yet delivered is stale."""
    w.get("notified", {}).pop(key, None)
    w.get("outbox", {}).pop(key, None)


def flush_notices(cfg, st, w):
    """Send the queued notices that are due; one attempt per notice per NOTIFY_RETRY_SECONDS
    (also bounds the `telegram FAILED` events)."""
    box = w.get("outbox")
    if not box:
        return
    now = time.time()
    tried = False
    for key, item in list(box.items()):
        if item.get("next_at", 0) > now:
            continue
        tried = True
        if notify(cfg, item["text"]):
            box.pop(key, None)
        else:
            item["next_at"] = now + NOTIFY_RETRY_SECONDS
    if not box:
        w.pop("outbox", None)
    if tried:
        save_state(cfg, st)


# ---------- launch ----------

ADMISSION_ROOT = re.compile(r"cc-admission-[a-z0-9_]{8}")
ADMISSION_SIGNATURE = {"empty-template", "checkout"}


def admitted_workdir(path):
    """`workdir` must belong to a clone returned by `cc-autonomy prepare`: the clone itself
    or a worktree derived from it (rules/autonomy-allowlist.md). Return the reason it is not."""
    r = sh("git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir",
           check=False)
    if r.returncode != 0:
        return f"not a git checkout: {r.stderr.strip()}"
    common = pathlib.Path(r.stdout.strip()).resolve()
    checkout, root = common.parent, common.parent.parent
    if common.name != ".git" or checkout.name != "checkout" or not ADMISSION_ROOT.fullmatch(root.name):
        return f"git dir {common} is not inside a cc-admission-*/checkout clone"
    try:
        if set(os.listdir(root)) != ADMISSION_SIGNATURE or root.stat().st_uid != os.getuid():
            return f"{root} does not carry the admission signature"
    except OSError as e:
        return f"{root}: {e}"
    return None


def host_helper():
    return pathlib.Path.home() / ".claude" / "bin" / "cc-autonomy.py"


def isolated_workdir(path):
    """Unmanaged host (no admission policy): any git checkout or worktree owned by the current
    user is a valid isolated worktree. Return the reason it is not."""
    r = sh("git", "-C", str(path), "rev-parse", "--show-toplevel", check=False)
    if r.returncode != 0:
        return f"not a git checkout: {r.stderr.strip()}"
    try:
        if pathlib.Path(path).stat().st_uid != os.getuid():
            return f"{path} is not owned by the current user"
    except OSError as e:
        return f"{path}: {e}"
    return None


def prepare_clone(cfg):
    helper = host_helper()
    if not helper.is_file():  # standalone install: no host policy, so no admission to satisfy
        if not cfg.get("workdir"):
            raise SystemExit("admission: no host policy (~/.claude/bin/cc-autonomy.py not found): "
                             "set workdir in chain.json to an isolated git worktree")
        why = isolated_workdir(cfg["workdir"])
        if why:
            raise SystemExit(f"workdir {cfg['workdir']} refused: {why}")
        event(cfg, f"admission: unmanaged host (no host policy), isolated git worktree {cfg['workdir']}")
        return cfg["workdir"]
    if cfg.get("workdir"):
        why = admitted_workdir(cfg["workdir"])
        if why:
            raise SystemExit(f"admission refused for workdir {cfg['workdir']}: {why}")
        event(cfg, f"admission: host policy, cc-admission clone {cfg['workdir']}")
        return cfg["workdir"]
    r = sh("python3", str(helper), "prepare", str(cfg.get("repo") or ""), check=False)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
    if r.returncode != 0 or not out.get("ok"):
        raise SystemExit(f"admission refused: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
    event(cfg, "admission: host policy, cc-autonomy prepare")
    return out["admitted_path"]


MANDATE_HEADER = "Прогон: "


def system_prompt(cfg):
    """Generic protocol plus the mandate of THIS run only (run_dir/mandate.md, approved in
    phase A). A dated mandate must never ride along with an unrelated chain or rerun:
    its first line must name this run_id, otherwise it is ignored. Waves may write into
    run_dir, so the approved bytes are pinned by `mandate_sha256` in chain.json (which lives
    outside run_dir): a mandate whose digest differs is refused, not trusted."""
    text = PROTOCOL.read_text(encoding="utf-8").rstrip() + "\n"
    mandate = cfg["run_dir"] / "mandate.md"
    raw = mandate.read_bytes() if mandate.exists() else b""
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"{mandate} is not valid UTF-8")
    first = body.splitlines()[:1]
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


def launch(cfg, wave, prompt_file):
    """Start one wave. False when the Claude TUI never became ready: nothing is sent then."""
    with _RunLock(cfg, "launch"):
        return _launch(cfg, wave, prompt_file)


HANDED_OVER = ("awaiting_merge", "done")
STOPPED = ("dead", "not_ready")


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


def _launch(cfg, wave, prompt_file):
    require_tmux()
    if wave not in cfg["waves"]:
        raise SystemExit(f"unknown wave {wave}; chain.json waves: {cfg['waves']}")
    st = load_state(cfg)
    name = f"{cfg['tmux_prefix']}{wave.lower()}"
    _check_launch_allowed(cfg, st, wave, name)
    if tmux_alive(name):
        raise SystemExit(f"tmux session {name} already exists")
    wdir = wave_dir(cfg, wave)
    cwd = prepare_clone(cfg)
    prompt_path = pathlib.Path(prompt_file).resolve()
    if not prompt_path.is_file():
        raise SystemExit(f"prompt file {prompt_file} not found")
    prev = st.get("current")
    if prev and prev != wave and st["waves"].get(prev, {}).get("phase") == "awaiting_merge":
        st["waves"][prev]["phase"] = "done"  # merged by the coordinator; saved with the launch intent
    sid = str(uuid.uuid4())  # known up front: the transcript is bound to the wave by session id
    (wdir / "status").write_text("STARTING\n", encoding="utf-8")  # the wave has not started yet
    system_prompt(cfg)
    # the intent is saved BEFORE tmux is touched: a restart finds the wave and its session id
    st["current"] = wave
    st.pop("stopped", None)
    old = st["waves"].get(wave)
    attempts = (old.get("attempts", []) + [{k: v for k, v in old.items() if k != "attempts"}]) if old else []
    st["waves"][wave] = {"tmux": name, "cwd": cwd, "started": time.time(), "restarts": 0,
                         "phase": "launching", "notified": {}, "sessions": [sid],
                         "prompt_file": str(prompt_path)}
    if attempts:
        st["waves"][wave]["attempts"] = attempts  # the earlier try is kept, not overwritten
    save_state(cfg, st)
    start_session(cfg, st, wave)
    return deliver_first_prompt(cfg, st, wave)


def start_session(cfg, st, wave):
    """launching -> starting: create the tmux window (same session id on a repeat)."""
    w = st["waves"][wave]
    wdir = wave_dir(cfg, wave)
    cmd = ["claude", "--permission-mode", "auto", "--append-system-prompt-file", str(system_prompt(cfg)),
           "--name", f"wab-{cfg['chain']}-{wave}", "--session-id", w["sessions"][0]]
    if cfg["model"]:
        cmd += ["--model", cfg["model"]]
    tmux("new-session", "-d", "-s", w["tmux"], "-c", w["cwd"], "-x", "220", "-y", "60",
       "-e", f"WAB_DIR={wdir}", "-e", f"WAB_WAVE={wave}", *cmd)
    w["phase"] = "starting"
    save_state(cfg, st)


def recover_launch(cfg, st, wave):
    """Dispatcher died around `tmux new-session`: a live window means it was started,
    otherwise start it again with the same session id. Then carry on as `starting`."""
    w = st["waves"][wave]
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
    save_state(cfg, st)
    if fresh:
        event(cfg, f"{wave}: tmux session {w['tmux']} is gone (status={status})")
        queue_notice(cfg, st, w, "dead", "1",
                     f"wave-autobot: окно волны {wave} закрылось, статус «{status}». Цепочка стоит.")


def _act(cfg, st, wave, what, fn, *args, **kw):
    """Run one outside tmux action. True when done. Window gone -> phase dead and
    _WindowGone. Window alive but the command failed -> one event and one notice, False;
    the intent phase is already on disk, so the next tick (or a restart) decides."""
    w = st["waves"][wave]
    try:
        fn(*args, **kw)
    except (subprocess.CalledProcessError, OSError) as e:
        if not tmux_alive(w["tmux"]):
            _mark_dead(cfg, st, wave, read(cfg["run_dir"] / wave / "status"))
            raise _WindowGone() from e
        fresh = once_per(w, "tmux_failed", what)
        save_state(cfg, st)
        event(cfg, f"{wave}: tmux action '{what}' failed ({type(e).__name__}); the window is alive")
        if fresh:
            queue_notice(cfg, st, w, "tmux_failed", what,
                         f"wave-autobot: не удалось выполнить «{what}» в окне волны {wave} "
                         f"({type(e).__name__}). Окно живо: {attach_cmd(w['tmux'])}")
        return False
    drop_notice(w, "tmux_failed")
    return True


def _deliver(cfg, st, wave, what, send, text):
    """Two-step send (text, then Enter) without duplicates: once the text is in the window
    that fact is saved (`pending_enter`), and a retry after a failed Enter presses only Enter."""
    w = st["waves"][wave]
    name = w["tmux"]
    if w.get("pending_enter") == what:
        ok = _act(cfg, st, wave, f"{what} (Enter)", press_enter, name)
    else:
        def typed():
            w["pending_enter"] = what
            save_state(cfg, st)
        ok = _act(cfg, st, wave, what, send, name, text, on_typed=typed)
    if ok:
        w.pop("pending_enter", None)
    return ok


def deliver_first_prompt(cfg, st, wave):
    """starting -> sending -> running. The phase is saved before the paste: a dispatcher
    that dies in between leaves `sending`, which is never resent blindly."""
    w = st["waves"][wave]
    name, cwd, wdir = w["tmux"], w["cwd"], wave_dir(cfg, wave)
    if not wait_ready(name):
        blocked = "BLOCKED: окно Claude не стало готовым, задача не отправлена\n"
        (wdir / "status").write_text(blocked, encoding="utf-8")
        w["phase"] = "not_ready"
        save_state(cfg, st)
        event(cfg, f"{wave}: Claude TUI not ready in {name}, prompt NOT sent")
        notify(cfg, f"wave-autobot: окно волны {wave} не стало готовым за 90 с, задачу не отправил. "
                    f"Цепочка стоит. Посмотри: {attach_cmd(name)}")
        return False
    prompt = pathlib.Path(w["prompt_file"]).read_text(encoding="utf-8").strip()
    head = (f"{session_marker(cfg, wave)} [wave-autobot] Волна {wave}. Каталог волны: {wdir} "
            f"(он же $WAB_DIR). Рабочая копия (admitted clone): {cwd}. "
            f"Протокол — в системной инструкции.\n\n")
    (wdir / "first-prompt.md").write_text(head + prompt + "\n", encoding="utf-8")
    w["phase"] = "sending"
    save_state(cfg, st)
    try:
        if not _deliver(cfg, st, wave, "first prompt", send_text, head + prompt):
            return False  # phase `sending` stays on disk; a restart will not resend blindly
    except _WindowGone:
        return False
    w["phase"] = "running"
    once_per(w, "started", "1")  # saved before the notice: at most once
    save_state(cfg, st)
    event(cfg, f"{wave}: launched in tmux {name}, cwd {cwd}")
    notify(cfg, f"wave-autobot: стартовала волна {wave}.\nСмотреть: {attach_cmd(name)}\n(выйти: Ctrl-b d)")
    return True


# ---------- supervision ----------

def once_per(w, key, value):
    """True the first time `value` is seen under `key` (dedup for notifications)."""
    notified = w.setdefault("notified", {})
    if notified.get(key) == value:
        return False
    notified[key] = value
    return True


def resume(cfg, st):
    try:
        advance_pending(cfg, st)
    except _WindowGone:
        pass  # recorded as dead; tick follows


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
            fresh = once_per(w, "no_prompt", "1")
            save_state(cfg, st)
            if fresh:
                queue_notice(cfg, st, w, "no_prompt", "1",
                             f"wave-autobot: волна {wave} запущена, но файла с задачей уже нет, "
                             f"задачу не отправил. Посмотри: {attach_cmd(name)}")
    elif phase == "sending" and w.get("pending_enter") == "first prompt":
        event(cfg, f"{wave}: resumed in phase 'sending': the text is typed, pressing only Enter")
        if _deliver(cfg, st, wave, "first prompt", send_text, ""):
            w["phase"] = "running"
            save_state(cfg, st)
    elif phase == "sending":
        event(cfg, f"{wave}: resumed in phase 'sending': unknown whether the task was delivered, "
                   f"NOT resent")
        fresh = once_per(w, "sending", "1")
        w["phase"] = "running"
        save_state(cfg, st)
        if fresh:
            queue_notice(cfg, st, w, "sending", "1",
                         f"wave-autobot: диспетчер перезапустился при отправке задачи волне {wave}; "
                         f"неизвестно, дошла ли она, повторно не слал. Проверь окно: {attach_cmd(name)}")
    elif phase == "updating":
        recover_update(cfg, st, wave)
    elif phase == "dead" and tmux_alive(name):
        w["phase"] = "running"
        drop_notice(w, "dead")
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
    finish_update(cfg, st, wave)
    if fresh and not arrived and not informed:
        queue_notice(cfg, st, w, "updating", str(w.get("restarts", 0)),
                     f"wave-autobot: диспетчер перезапустился при передаче /update волне {wave}; "
                     f"неизвестно, дошла ли команда, повторно не слал. Проверь окно: {attach_cmd(w['tmux'])}")


def _stop_without_next(cfg, st, w, wave, now):
    """A non-last wave wrote DONE without next-prompt.md: one event, one notice, the chain stops."""
    w.setdefault("finished", now)
    w["phase"] = "done"
    st["current"] = None
    st["stopped"] = f"{wave}: DONE without next-prompt.md"
    fresh_stop = once_per(w, "no_next", "1")
    save_state(cfg, st)
    if fresh_stop:
        event(cfg, f"{wave}: DONE without next-prompt.md; chain stopped")
        queue_notice(cfg, st, w, "no_next", "1",
                     f"wave-autobot: {wave} готова, но нет next-prompt.md — следующую волну не запускаю.")
    return False


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
    status = read(wdir / "status")
    if status:
        w["last_status"] = status
    else:  # an empty read is the wave mid-rewrite: decide on the last real status
        status = w.get("last_status", "")
    now = time.time()
    attach = f"{attach_cmd(name)}  (выйти: Ctrl-b d)"

    if not status.startswith("BLOCKED"):
        drop_notice(w, "blocked")  # the episode is over: the next one notifies again
    if w.get("phase") == "awaiting_merge":
        return False  # handed to the coordinator: the watch ends and frees the run lock
    if w.get("phase") == "not_ready":
        return False
    if w.get("phase") == "launching":
        recover_launch(cfg, st, wave)
        return True

    # DONE first: a wave may finish and close its window between two ticks
    external = cfg.get("merge_gate") == "external"
    waves = cfg["waves"]
    is_last = waves.index(wave) + 1 >= len(waves)
    if status == "DONE" and (external or (not MERGE_GATE_IMPLEMENTED and not is_last)):
        nxt = wdir / "next-prompt.md"
        if not is_last and not nxt.exists():  # the coordinator's launch would fail: stop loudly now
            return _stop_without_next(cfg, st, w, wave, now)
        w["phase"] = "awaiting_merge"  # saved before any notice: at most once
        w["finished"] = now
        save_state(cfg, st)
        if external:
            event(cfg, f"{wave}: DONE, awaiting merge by the coordinator")
        else:
            event(cfg, f"{wave}: DONE, merge gate not implemented yet (W5); handing off to coordinator")
        notify(cfg, f"wave-autobot: волна {wave} сдала PR.\n\n{redact(read(wdir / 'result.md'))}\n\n"
                    f"Мердж и запуск следующей волны — за координатором.")
        if tmux_alive(name):
            tmux("send-keys", "-t", pane_target(name), "-l", "/exit", check=False)
            tmux("send-keys", "-t", pane_target(name), "Enter", check=False)
        return False

    if status == "DONE":
        waves = cfg["waves"]
        idx = waves.index(wave)
        nxt = wdir / "next-prompt.md"
        fresh = once_per(w, "done", "1")  # a restart between this and the next launch must not repeat it
        if fresh:
            w["finished"] = now
        w["phase"] = "done"
        save_state(cfg, st)
        if fresh:
            event(cfg, f"{wave}: DONE")
            queue_notice(cfg, st, w, "done", "1",
                         f"wave-autobot: волна {wave} завершена.\n\n{redact(read(wdir / 'result.md'))}\n\n"
                         f"Целиком: {wdir}/result.md")
            if tmux_alive(name):
                tmux("send-keys", "-t", pane_target(name), "-l", "/exit", check=False)
                tmux("send-keys", "-t", pane_target(name), "Enter", check=False)
        if idx + 1 >= len(waves):
            st["current"] = None
            save_state(cfg, st)
            event(cfg, "chain finished")
            notify(cfg, "wave-autobot: цепочка завершена, все волны готовы.")
            return False
        if not nxt.exists():
            return _stop_without_next(cfg, st, w, wave, now)
        save_state(cfg, st)
        return launch(cfg, waves[idx + 1], nxt)

    if not tmux_alive(name):
        _mark_dead(cfg, st, wave, status)
        return False  # the chain stops; restart the wave by hand, then run watch again

    if w.get("phase") in ("starting", "sending"):  # the first prompt is not through yet
        advance_pending(cfg, st)
        return True

    if status.startswith("BLOCKED"):
        fresh = once_per(w, "blocked", status)
        save_state(cfg, st)
        if fresh:
            event(cfg, f"{wave}: {status[:200]}")
            queue_notice(cfg, st, w, "blocked", status,
                         f"wave-autobot: волна {wave} ждёт тебя.\n\n{redact(status)}\n\nОтветить: {attach}")
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

    txt = pane_text(name)
    awaiting = bool(w.get("await_session"))
    if awaiting:
        sid = find_new_session(cfg, st, wave)
        if sid:
            w["sessions"].append(sid)
            w["await_session"] = awaiting = False
            event(cfg, f"{wave}: new session {sid} bound by marker")
    if awaiting:
        tokens = w.get("tokens", 0)  # old session: not measured, no checkpoint requested
    else:
        tokens = context_tokens(w)
        w["tokens"] = tokens
        w["peak"] = max(w.get("peak", 0), tokens)
        w["ctx_hist"] = (w.get("ctx_hist", []) + [tokens])[-120:]

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
            save_state(cfg, st)
            queue_notice(cfg, st, w, "checkpoint_timeout", str(w.get("checkpoint_at")),
                         f"wave-autobot: {wave} не записала handoff за {cfg['handoff_timeout_minutes']} мин "
                         f"после запроса. Посмотри: {attach}")

    if w.get("phase") == "running":  # a wave out of auto mode asks for every step
        off = auto_mode_off(txt)
        if off:
            w["auto_off_ticks"] = w.get("auto_off_ticks", 0) + 1
            if w["auto_off_ticks"] >= 2 and not w.get("auto_alerted"):
                w["auto_alerted"] = True
                save_state(cfg, st)
                event(cfg, f"{wave}: window is not in auto mode")
                queue_notice(cfg, st, w, "auto_off", "1",
                             f"wave-autobot: волна {wave} вышла из режима auto и будет спрашивать "
                             f"подтверждения. Вернуть: {attach}, Shift+Tab до «auto mode on».")
        elif off is False:
            w["auto_off_ticks"] = 0
            w["auto_alerted"] = False
            drop_notice(w, "auto_off")

    if not any(m in txt for m in PERMISSION_MARKERS):
        drop_notice(w, "permission")
    if any(m in txt for m in PERMISSION_MARKERS):
        if once_per(w, "permission", "visible"):  # one episode while the prompt stays on screen
            save_state(cfg, st)
            event(cfg, f"{wave}: permission prompt on screen")
            queue_notice(cfg, st, w, "permission", "visible",
                         f"wave-autobot: волна {wave} ждёт подтверждения на экране.\n{attach}")

    # idleness is judged without the last two non-empty lines (spinner, timer, footer)
    body = [l for l in txt.splitlines() if l.strip()][:-2]
    digest = hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()
    if digest != w.get("pane_digest"):
        w["pane_digest"], w["pane_changed"] = digest, now
        drop_notice(w, "idle")
    elif now - w.get("pane_changed", now) > cfg["idle_minutes"] * 60:
        if once_per(w, "idle", digest):
            save_state(cfg, st)
            event(cfg, f"{wave}: pane idle {cfg['idle_minutes']}+ min, status={status}")
            queue_notice(cfg, st, w, "idle", digest,
                         f"wave-autobot: волна {wave} молчит {cfg['idle_minutes']}+ мин "
                         f"(статус «{status}»). Возможно, ждёт тебя: {attach}")
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
           "titles", "model")


def _identity(cfg):
    """Everything that decides WHOSE run this is: all chain.json fields except the tunable ones."""
    return {k: v for k, v in cfg.items() if k not in TUNABLE}


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
            event(cfg, f"{wave}: handed to the coordinator; it is the last wave, nothing to launch after the merge")
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
    event(cfg, "watch stopped: no current wave")
    return True


def watch(cfg, path, max_ticks=None):
    with _RunLock(cfg, "watch"):
        return _watch(cfg, path, max_ticks)


def _watch(cfg, path, max_ticks=None):
    require_tmux()
    event(cfg, f"watch started, ctx_limit={cfg['ctx_limit']}")
    drop_stale_btab(cfg, TMUX_SOCKET or os.environ.get("WAB_TMUX_SOCKET") or "")
    resume(cfg, _state_or_event(cfg))
    last = 0.0
    ticks = 0
    pinned = _identity(cfg)
    while True:
        try:
            fresh = load_chain(path)  # thresholds can be tuned live
        except SystemExit as e:
            event(cfg, f"chain.json not re-read, keeping the previous settings: {e}")
        else:
            changed = sorted(k for k in set(pinned) | set(_identity(fresh)) if pinned.get(k) != _identity(fresh).get(k))
            if changed:
                event(cfg, f"chain.json identity changed ({', '.join(changed)}); keeping {pinned['run_dir']}")
                raise SystemExit(f"wab: chain.json identity changed ({', '.join(changed)}) during watch; "
                                 f"stopping; restart watch for the new run")
            cfg = fresh
        st = _state_or_event(cfg)
        if not tick(cfg, st):
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
        if not launch(load_chain(path), argv[3], argv[4]):
            print(f"wab: wave {argv[3]} not started (see events.log)", file=sys.stderr)
            sys.exit(3)
    elif cmd == "watch":
        if watch(load_chain(path), path) is False:
            print("wab: watch ended because the chain stopped (window gone or not started)", file=sys.stderr)
            sys.exit(3)
    elif cmd == "notify":
        notify(load_chain(path), " ".join(argv[3:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
