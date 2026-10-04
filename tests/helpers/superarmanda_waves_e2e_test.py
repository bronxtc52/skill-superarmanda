#!/usr/bin/env python3
"""Offline end-to-end test of a two-wave chain through the REAL dispatcher (wab.py, gate.py, state.py).

Nothing paid or networked is touched. PATH of the test is a directory of stand-ins (tmux, gh, a
`claude` that must never run) plus a directory of symlinks to python3/git/sh/env/cat: the real
tmux, gh, claude, curl and az are not reachable. HOME is a temporary directory, no WAB_*/TMUX*
variable of the host survives (the environment is rebuilt), urlopen and socket.connect raise,
Telegram is off, time.sleep/time.time are a virtual clock (a tick of 60 s costs no real time).

  tmux  - tests/helpers/wab_e2e_fakes/tmux.py: stateful, every call needs -S/-L, screens are cut from
          the live snapshots tests/fixtures/screens, every message submitted with Enter lands in an inbox;
  gh    - tests/helpers/wab_e2e_fakes/gh.py: PRs over a REAL local bare repository ("origin"),
          `gh pr merge --squash --match-head-commit` really squashes into its main;
  claude - the `Actor` below: it plays the wave sessions between ticks (it is called from the patched
          sleep), reacting to what the dispatcher typed into the window: writes status/result.md/
          next-prompt.md/handoff.md, commits and pushes, opens the PR, builds the manifest with the real
          state.py and appends to the session journal (~/.claude/projects/<cwd>/<sid>.jsonl) with the
          shape of tests/fixtures/transcripts (cut from live journals).

The dispatcher itself is not patched (its decisions are its own): launch, watch (tick) and done run
through wab.main(), as the CLI does.

Scenario (see Actor): W1 BLOCKED with a machine label -> the dispatcher answers by decision_policy
-> the wave works, DONE -> the merge gate waits (CI running), fails (CI red: the reason reaches the
window), waits (Codex not finished), passes -> `gh pr merge` -> MERGED -> W1's window is closed, W2
starts on a fresh base (contains W1's merge commit) -> W2's context grows past ctx_limit ->
WAB-CHECKPOINT -> handoff.md + HANDOFF_READY -> /clear -> new journal -> `/superarmanda --wave W2
--resume [wab:...]` -> the new session is bound by the marker -> DONE -> gate -> merge ->
chain-result.md.

Mutations (same scenario, broken dispatcher, the oracle must notice): no merge gate, no session
binding after /clear, no decision policy.
"""

import atexit
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WAVES = ROOT / "skills" / "superarmanda" / "scripts" / "waves"
STATE_PY = ROOT / "skills" / "superarmanda" / "scripts" / "state.py"
FIXTURES = ROOT / "tests" / "fixtures"
FAKES = Path(__file__).resolve().parent / "wab_e2e_fakes"
sys.path.insert(0, str(WAVES))

# the host's session may export these (a wave of the live chain does): none of them may leak
for _var in [k for k in os.environ if k.startswith(("WAB_", "TMUX"))]:
    os.environ.pop(_var, None)

import wab  # noqa: E402

REPO = "acme/waves-demo"
CHAIN = "e2e"
RUN_ID = "2026-10-04-e2e"
CTX_LIMIT = 50_000
TICK = 60
DELAY = 20  # virtual seconds between what the dispatcher typed and the session's reaction
BOT_STATUS = "[class=needs_decision rec=A red=no]"
EXPECTED_TOOLS = ("tmux", "gh", "claude")
TOOLS_THAT_MUST_NOT_RESOLVE = ("curl", "az", "wget", "ssh")


class Stalled(BaseException):
    """The chain did not finish within the virtual time the scenario is allowed (BaseException: no
    `except Exception` of the dispatcher may swallow it)."""


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ------------------------------------------------------------------ virtual clock

class Clock:
    """time.time() = real time + the virtual seconds slept so far; sleep() does not sleep, it advances
    the virtual time and lets the Actor play its session (the dispatcher is blocked in sleep, exactly
    when a real session would be working)."""

    def __init__(self, limit):
        self.offset = 0.0
        self.limit = limit
        self.real_time = time.time
        self.actor = None
        self.sleeps = 0

    def time(self):
        return self.real_time() + self.offset

    def sleep(self, seconds):
        self.offset += float(seconds)
        self.sleeps += 1
        if self.offset > self.limit:
            raise Stalled(f"the chain did not finish in {self.limit:.0f} virtual seconds")
        if self.actor is not None:
            self.actor.step()


# ------------------------------------------------------------------ the world on disk

class World:
    def __init__(self, base, policy=True):
        self.root = Path(base).resolve()
        self.home = self.root / "home"
        self.bindir = self.root / "bin"
        self.sysbin = self.root / "sysbin"
        self.ctl = self.root / "ctl"
        self.tmux_state = self.root / "tmux-state"
        self.gh_state = self.root / "gh-state"
        self.origin = self.root / "origin.git"
        self.workdir = self.root / "workdir"
        self.sock = self.root / "tmux.sock"  # never created: the stand-in has no server
        self.chain_file = self.ctl / "chain.json"
        self.run_dir = self.ctl / "runs" / CHAIN / RUN_ID
        self.policy = policy
        self.env = {
            "PATH": f"{self.bindir}{os.pathsep}{self.sysbin}", "HOME": str(self.home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
            "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
            "WAB_TMUX_SOCKET": str(self.sock), "FAKE_TMUX_STATE": str(self.tmux_state),
            "FAKE_TMUX_FIXTURES": str(FIXTURES / "screens"), "FAKE_GH_STATE": str(self.gh_state),
        }

    # -- commands run in the test's own environment (never the host's)
    def run(self, *argv, cwd=None, check=True):
        r = subprocess.run(argv, cwd=cwd, env=self.env, capture_output=True, text=True, encoding="utf-8")
        if check and r.returncode != 0:
            raise AssertionError(f"{' '.join(argv)} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r

    def git(self, *args, cwd=None, check=True):
        return self.run("git", *args, cwd=cwd or self.workdir, check=check).stdout.strip()

    def origin_git(self, *args):
        return self.run("git", "--git-dir", str(self.origin), *args).stdout.strip()

    def setup(self):
        for d in (self.home, self.bindir, self.sysbin, self.ctl, self.tmux_state, self.gh_state):
            d.mkdir(parents=True, exist_ok=True)
        # the only system tools the dispatcher and the sessions may use
        for name, path in (("python3", sys.executable), ("git", shutil.which("git")), ("sh", shutil.which("sh")),
                           ("env", shutil.which("env")), ("cat", shutil.which("cat"))):
            must(path, f"{name} is needed by the test")
            (self.sysbin / name).symlink_to(path)
        shebang = f"#!{sys.executable} -S\n"
        for name, src in (("tmux", "tmux.py"), ("gh", "gh.py"), ("claude", "claude.py")):
            target = self.bindir / name
            target.write_text(shebang + (FAKES / src).read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o755)
        # a bare "origin" with a main branch, and the chain's one shared working copy cloned from it
        self.run("git", "init", "-q", "--bare", str(self.origin), cwd=self.root)
        self.origin_git("symbolic-ref", "HEAD", "refs/heads/main")
        seed = self.root / "seed"
        self.run("git", "clone", "-q", str(self.origin), str(seed), cwd=self.root)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
        (seed / "README.md").write_text("demo repository\n", encoding="utf-8")
        self.git("add", "README.md", cwd=seed)
        self.git("commit", "-q", "-m", "initial", cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        self.run("git", "clone", "-q", str(self.origin), str(self.workdir), cwd=self.root)
        (self.gh_state / "state.json").write_text(
            json.dumps({"repo": REPO, "origin": str(self.origin), "prs": {}}), encoding="utf-8")
        chain = {"chain": CHAIN, "run_id": RUN_ID, "waves": ["W1", "W2"], "repo": REPO, "base_branch": "main",
                 "workdir": str(self.workdir), "merge_gate": "auto", "tick_seconds": TICK,
                 "ctx_limit": CTX_LIMIT, "idle_minutes": 1440, "handoff_timeout_minutes": 1440,
                 "telegram": False}
        if self.policy:
            chain["decision_policy"] = [{"class": "needs_decision", "rec": "A"}]
        self.chain_file.write_text(json.dumps(chain, indent=1), encoding="utf-8")
        (self.root / "w1-prompt.md").write_text(
            "Волна W1: добавь файл w1.txt в репозиторий.\n" + "\n".join(f"Пункт {i} задачи." for i in range(1, 7)) + "\n",
            encoding="utf-8")

    # -- the gh stand-in's world
    def gh(self):
        return json.loads((self.gh_state / "state.json").read_text(encoding="utf-8"))

    def gh_set(self, number, **fields):
        st = self.gh()
        st["prs"][str(number)].update(fields)
        (self.gh_state / "state.json").write_text(json.dumps(st), encoding="utf-8")

    @staticmethod
    def jsonl(path):
        try:
            return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            return []


# ------------------------------------------------------------------ session journals (live shape)

def _fill(text, values, uuids):
    def sub(m):
        if m.group(0).startswith("{UUID:"):
            return uuids.setdefault(m.group(0), str(uuid.uuid4()))
        return json.dumps(values[m.group(0)])[1:-1]
    return re.sub(r"\{UUID:\d+\}|\{[A-Z_]+\}", sub, text)


def fixture_lines(name):
    return (FIXTURES / "transcripts" / name).read_text(encoding="utf-8").splitlines()


class Journal:
    """~/.claude/projects/<slug of the real cwd>/<session id>.jsonl, written from the sanitised live
    fixtures (tests/fixtures/transcripts/README.md)."""

    def __init__(self, path, sid, cwd):
        self.path, self.sid, self.cwd, self.uuids = Path(path), sid, str(cwd), {}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def values(self, **extra):
        v = {"{SESSION_ID}": self.sid, "{CWD}": self.cwd, "{BRANCH}": "main", "{NAME}": f"wab-{CHAIN}",
             "{TEXT}": "text of the session", "{PASTE_ID}": "a1b2",
             "{TIMESTAMP}": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        v.update(extra)
        return v

    def write(self, name, lines=None, usage=None, **extra):
        out = []
        for raw in (fixture_lines(name) if lines is None else lines):
            obj = json.loads(_fill(raw, self.values(**extra), self.uuids))
            if usage and isinstance(obj.get("message"), dict) and obj["message"].get("usage"):
                obj["message"]["usage"].update(usage)
            out.append(json.dumps(obj, ensure_ascii=False))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")

    def first_message(self, marker):
        self.write("first-session-head.jsonl", **{"{MARKER}": marker})

    def after_clear(self):  # what Claude Code writes at /clear, before any message of the wave
        lines = fixture_lines("clear-session-head.jsonl")
        cut = next(i for i, l in enumerate(lines) if "<command-name>/superarmanda" in l)  # the typed command
        while json.loads(lines[cut - 1])["type"] == "file-history-snapshot":  # its snapshot goes with it
            cut -= 1
        self.write("clear-session-head.jsonl", lines=lines[:cut])
        self.rest = lines[cut:]

    def resume_command(self, args):
        self.write("clear-session-head.jsonl", lines=self.rest, **{"{COMMAND_ARGS}": args})

    def turn(self, ctx):
        """One assistant turn whose context size is `ctx` tokens (input + cache creation + cache read)."""
        self.write("assistant-turns.jsonl",
                   usage={"input_tokens": 2, "cache_creation_input_tokens": ctx // 5,
                          "cache_read_input_tokens": ctx - 2 - ctx // 5})


# ------------------------------------------------------------------ the sessions of the waves

class Session:
    def __init__(self, rec):
        cmd = rec["command"]
        self.name = rec["name"]
        self.wave = rec["env"]["WAB_WAVE"]
        self.wave_dir = Path(rec["env"]["WAB_DIR"])
        self.sids = [cmd[cmd.index("--session-id") + 1]]
        self.cwd = rec["cwd"]
        self.command = cmd
        self.journal = None
        self.marker = None
        self.started = False


class Actor:
    """The wave sessions, played. step() is called from the virtual sleep of the dispatcher: it reads
    what was typed into the windows (the tmux stand-in's inbox) and does, a few virtual seconds
    later, what the protocol tells a wave to do."""

    def __init__(self, world, clock, mode):
        self.w, self.clock, self.mode = world, clock, mode
        self.queue, self.seq, self.sessions = [], 0, {}
        self.inbox_seen = self.new_seen = 0
        self.received = []   # (virtual time, wave, text) of every message typed into a window
        self.events = []     # (virtual time, wave, what) the session did
        self.w2_start = None  # the state of the working copy when W2's task arrived
        self.gate_fail_seen = False

    # -- scheduling
    def after(self, delay, label, fn):
        self.seq += 1
        self.queue.append((self.clock.offset + delay, self.seq, label, fn))

    def step(self):
        for rec in self.w.jsonl(self.w.tmux_state / "new_sessions.jsonl")[self.new_seen:]:
            self.sessions[rec["name"]] = Session(rec)
            self.new_seen += 1
        for rec in self.w.jsonl(self.w.tmux_state / "inbox.jsonl")[self.inbox_seen:]:
            self.inbox_seen += 1
            self.on_message(self.sessions[rec["session"]], rec["text"])
        while True:
            due = sorted(q for q in self.queue if q[0] <= self.clock.offset)
            if not due:
                return
            self.queue.remove(due[0])
            due[0][3]()

    def did(self, s, what):
        self.events.append((self.clock.offset, s.wave, what))

    # -- files of the protocol
    def write_status(self, s, text):
        path = s.wave_dir / "status"
        before = path.stat().st_mtime_ns if path.exists() else 0
        path.write_text(text + "\n", encoding="utf-8")
        if path.stat().st_mtime_ns <= before:  # a coarse filesystem clock: the dispatcher compares mtimes
            os.utime(path, ns=(before + 5_000_000, before + 5_000_000))
        self.did(s, f"status {text[:40]}")

    def state_py(self, *args):
        r = subprocess.run([sys.executable, str(STATE_PY), *args], env=self.w.env, capture_output=True,
                           text=True, encoding="utf-8")
        must(r.returncode == 0, f"state.py {' '.join(args[:3])}: {r.stderr.strip()}")

    def manifest_path(self, s):
        return s.wave_dir / "superarmanda" / "manifest.json"

    # -- messages
    def on_message(self, s, text):
        self.received.append((self.clock.offset, s.wave, text))
        if text.strip() in ("/exit", ""):
            return
        if text.strip() == "/clear":
            return self.after(0, "clear", lambda: self.do_clear(s))
        if "[wave-autobot] Волна" in text and not s.started:
            s.started = True
            s.marker = re.search(r"\[wab:[^\]]+\]", text).group(0)
            s.journal = Journal(self.journal_path(s, s.sids[0]), s.sids[0], s.cwd)
            s.journal.first_message(s.marker)
            return self.after(DELAY, "first prompt", lambda: getattr(self, f"start_{s.wave.lower()}")(s))
        if "РЕШЕНИЕ ПО ПОЛИТИКЕ" in text:
            return self.after(DELAY, "policy answer", lambda: self.w1_answered(s))
        if "Гейт мерджа не пройден" in text:
            return self.after(DELAY, "gate failure", lambda: self.w1_fixed(s))
        if text.startswith("WAB-CHECKPOINT"):
            return self.after(DELAY, "checkpoint", lambda: self.w2_checkpoint(s))
        if text.startswith("/superarmanda --wave"):
            return self.on_resume(s, text)

    def journal_path(self, s, sid):
        return self.w.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(s.cwd)) / f"{sid}.jsonl"

    # -- git / PR work of a wave
    def commit_and_pr(self, s, ci, codex):
        branch, name = f"wave/{s.wave.lower()}", f"{s.wave.lower()}.txt"
        self.w.git("switch", "-q", "-c", branch)
        (self.w.workdir / name).write_text(f"work of {s.wave}\n", encoding="utf-8")
        self.w.git("add", name)
        self.w.git("commit", "-q", "-m", f"{s.wave}: add {name}")
        self.w.git("push", "-q", "origin", branch)
        self.w.run("gh", "pr", "create", "--repo", REPO, "--head", branch, "--base", "main",
                   "--title", f"{s.wave}: add {name}", "--body", "demo")
        number = max(int(n) for n in self.w.gh()["prs"])
        self.w.gh_set(number, ci=ci, codex=codex)
        self.did(s, f"commit {self.w.git('rev-parse', 'HEAD')[:12]} and PR #{number}")
        return number

    def init_manifest(self, s):
        head = self.w.git("rev-parse", "HEAD")
        base = self.w.git("rev-parse", "origin/main")
        self.manifest_path(s).parent.mkdir(parents=True, exist_ok=True)
        self.state_py("init", "--manifest", str(self.manifest_path(s)), "--repo", str(self.w.workdir),
                      "--base", base, "--head", head)

    def record_results(self, s):
        head = self.w.git("rev-parse", "HEAD")
        m = str(self.manifest_path(s))
        for role in ("coder", "tester"):
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", role, "--status", "pass",
                          "--session-id", f"e2e-{s.wave}-{role}", "--head", head)
        self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "cross_provider_reviewer",
                      "--status", "pass", "--session-id", f"e2e-{s.wave}-reviewer", "--head", head,
                      "--reviewed-head", head, "--packet-hash", hashlib.sha256(b"packet").hexdigest())

    def finish(self, s, next_prompt=None):
        (s.wave_dir / "result.md").write_text(f"{s.wave} готова: {s.wave.lower()}.txt добавлен.\n", encoding="utf-8")
        if next_prompt:
            (s.wave_dir / "next-prompt.md").write_text(next_prompt, encoding="utf-8")
        self.write_status(s, "DONE")

    # -- W1: BLOCKED -> policy answer -> work -> DONE -> gate waits / fails / passes
    def start_w1(self, s):
        s.journal.turn(6_000)
        self.write_status(s, f"BLOCKED: {BOT_STATUS} Какую схему выбрать? Варианты: A) тонкая правка, B) переделка.")

    def w1_answered(self, s):
        self.write_status(s, "RUNNING")
        self.after(60, "w1 work", lambda: self.w1_work(s))

    def w1_work(self, s):
        # the gate mutants: the PR is red (gate_off) or only Codex wrote on an older commit (gate_off_codex), for good
        bad = self.mode in ("gate_off", "gate_off_codex")
        number = self.commit_and_pr(s, ci={"gate_off": "failure", "gate_off_codex": "success"}.get(self.mode, "in_progress"),
                                    codex="stale" if bad else "none")
        self.init_manifest(s)
        self.record_results(s)
        s.journal.turn(9_000)
        self.finish(s, next_prompt="Волна W2: добавь файл w2.txt в репозиторий.\n")
        if not bad:
            self.after(100, "ci red", lambda: self.w.gh_set(number, ci="failure"))

    def w1_fixed(self, s):  # the gate failed (CI red): the wave "fixes" it, CI goes green, Codex is still reading
        number = max(int(n) for n in self.w.gh()["prs"])
        self.w.gh_set(number, ci="success", codex="stale")  # Codex has only reviewed an older commit so far
        self.write_status(s, "DONE")
        self.after(130, "codex review", lambda: self.w.gh_set(number, codex="head"))

    # -- W2: context grows -> checkpoint -> handoff -> /clear -> resume -> DONE
    def start_w2(self, s):
        w1_merge = (self.w.jsonl(self.w.gh_state / "merges.jsonl") or [{}])[0].get("merge_commit")
        head = self.w.git("rev-parse", "HEAD")
        self.w2_start = {
            "head": head, "origin_main": self.w.origin_git("rev-parse", "main"), "w1_merge": w1_merge,
            "has_merge_commit": bool(w1_merge) and self.w.run(
                "git", "merge-base", "--is-ancestor", w1_merge, head, cwd=self.w.workdir, check=False).returncode == 0,
            "has_w1_file": (self.w.workdir / "w1.txt").exists(),
            "detached": self.w.run("git", "symbolic-ref", "-q", "HEAD", cwd=self.w.workdir, check=False).returncode != 0,
            "clean": self.w.git("status", "--porcelain") == ""}
        self.write_status(s, "RUNNING")
        s.journal.turn(20_000)
        self.after(40, "w2 grows", lambda: s.journal.turn(CTX_LIMIT + 12_000))

    def w2_checkpoint(self, s):
        self.commit_and_pr(s, ci="success", codex="head")
        self.init_manifest(s)
        self.manifest_path(s)
        (s.wave_dir / "handoff.md").write_text(
            f"# Handoff W2\nManifest: {self.manifest_path(s)}\nСледующий шаг: записать результаты и DONE.\n", encoding="utf-8")
        self.write_status(s, "HANDOFF_READY")

    def do_clear(self, s):
        sid = str(uuid.uuid4())
        s.sids.append(sid)
        s.journal = Journal(self.journal_path(s, sid), sid, s.cwd)
        s.journal.after_clear()
        self.did(s, f"/clear: new session {sid[:8]}")

    def on_resume(self, s, text):
        marker = re.search(r"\[wab:[^\]]+\]", text)
        must(marker and marker.group(0) == s.marker, "the resume message must carry the wave's marker")
        s.journal.resume_command(text.split(" ", 1)[1])
        s.journal.turn(8_000)
        self.after(DELAY, "resumed", lambda: self.w2_resumed(s))

    def w2_resumed(self, s):
        self.write_status(s, "RUNNING")
        self.after(80, "w2 done", lambda: self.w2_done(s))

    def w2_done(self, s):
        self.record_results(s)
        s.journal.turn(11_000)
        self.finish(s)


# ------------------------------------------------------------------ one run of the scenario

class Run:
    pass


def run_scenario(mode="normal", limit=90 * 60):
    """mode: normal | gate_off (merge gate always passes, CI red) | gate_off_codex (the same, CI green but
    Codex reviewed an older commit) | no_rebind (find_new_session finds nothing) | no_policy (chain.json
    without decision_policy) | stale_base (the next wave starts on the old working copy) | no_checkpoint
    (the context is never measured)."""
    base = Path(tempfile.mkdtemp(prefix="wabe2e-"))
    atexit.register(shutil.rmtree, base, True)
    world = World(base, policy=mode != "no_policy")
    world.setup()
    clock = Clock(limit)
    actor = clock.actor = Actor(world, clock, mode)
    run = Run()
    run.world, run.actor, run.clock, run.mode, run.codes, run.out = world, actor, clock, mode, {}, io.StringIO()
    run.urlopen_calls = []

    def no_network(*a, **kw):
        run.urlopen_calls.append(a)
        raise AssertionError("network access in an offline test")

    def call(*argv):
        try:
            wab.main(["wab.py", *argv])
            return 0
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, world.env, clear=True))
        stack.enter_context(mock.patch.object(time, "time", clock.time))
        stack.enter_context(mock.patch.object(time, "sleep", clock.sleep))
        stack.enter_context(mock.patch.object(urllib.request, "urlopen", no_network))
        stack.enter_context(mock.patch.object(socket.socket, "connect", no_network))
        stack.enter_context(mock.patch.object(wab, "TMUX_SOCKET", None))
        stack.enter_context(contextlib.redirect_stdout(run.out))
        stack.enter_context(contextlib.redirect_stderr(run.out))
        if mode in ("gate_off", "gate_off_codex"):
            stack.enter_context(mock.patch.object(
                wab.gate, "evaluate",
                lambda facts, head, *a, **kw: wab.gate._verdict("pass", [], head, facts, accepted=[])))
        if mode == "no_rebind":
            stack.enter_context(mock.patch.object(wab, "find_new_session", lambda cfg, st, wave: None))
        if mode == "stale_base":
            stack.enter_context(mock.patch.object(wab, "refresh_workdir", lambda cfg, wave, cwd: None))
        if mode == "no_checkpoint":
            stack.enter_context(mock.patch.object(wab, "context_tokens", lambda w: 0))
        run.env_seen = {"wab_tmux": sorted(k for k in os.environ if k.startswith(("WAB_", "TMUX"))),
                        "home": str(Path.home()), "path": os.environ["PATH"]}
        run.resolved = {t: shutil.which(t) for t in EXPECTED_TOOLS + TOOLS_THAT_MUST_NOT_RESOLVE}
        run.codes["launch"] = call("launch", str(world.chain_file), "W1", str(world.root / "w1-prompt.md"))
        run.codes["watch"] = call("watch", str(world.chain_file))
        run.codes["done"] = call("done", str(world.chain_file))
        actor.step()  # what was typed after the last sleep (the final /exit) is read too
    run.state = json.loads((world.run_dir / "state.json").read_text(encoding="utf-8"))
    run.events_log = (world.run_dir / "events.log").read_text(encoding="utf-8")
    return run


# ------------------------------------------------------------------ the oracle

def oracle_hygiene(run):
    """Nothing but the stand-ins ran: every tmux call carried the private socket, only the expected
    gh verbs ran against the chain's repo, the real claude never started, no network, no real tools."""
    w = run.world
    for name in EXPECTED_TOOLS:
        must(run.resolved[name] == str(w.bindir / name), f"{name} resolves to {run.resolved[name]}, not the stand-in")
    for name in TOOLS_THAT_MUST_NOT_RESOLVE:
        must(run.resolved[name] is None, f"{name} is reachable from the test: {run.resolved[name]}")
    calls = w.jsonl(w.tmux_state / "calls.jsonl")
    must(len(calls) > 50, f"suspiciously few tmux calls: {len(calls)}")
    for c in calls:
        must(c.get("sock") == ["-S", str(w.sock)], f"tmux call without the private socket: {c}")
        must("unsupported" not in c, f"tmux command the stand-in does not know: {c}")
    news = w.jsonl(w.tmux_state / "new_sessions.jsonl")
    must([n["name"] for n in news] == ["wab-w1", "wab-w2"], f"sessions started: {[n['name'] for n in news]}")
    for n in news:
        must(n["command"][0] == "claude" and "--session-id" in n["command"], f"new-session command: {n['command']}")
        must(Path(n["cwd"]).resolve() == w.workdir, f"session cwd {n['cwd']}")
    must(not (w.tmux_state / "claude_calls.jsonl").exists(), "the real claude was started")
    gh = w.jsonl(w.gh_state / "calls.jsonl")
    must(gh, "gh was never called")
    for c in gh:
        must("unexpected" not in c, f"gh verb the dispatcher is not expected to use: {c}")
        argv = c["argv"]
        must(argv[0] in ("pr", "api"), f"gh {argv}")
        if argv[0] == "pr":
            must(argv[1] in ("list", "view", "merge", "create", "ready"), f"gh {argv}")
            must(argv[argv.index("--repo") + 1] == REPO, f"gh {argv}")
    must(not run.urlopen_calls, f"network calls: {run.urlopen_calls}")
    must(run.env_seen["wab_tmux"] == ["WAB_TMUX_SOCKET"], f"host variables leaked: {run.env_seen['wab_tmux']}")
    must(run.env_seen["home"] == str(w.home), f"HOME is {run.env_seen['home']}")
    must(run.env_seen["path"] == f"{w.bindir}{os.pathsep}{w.sysbin}", f"PATH is {run.env_seen['path']}")
    # the screens the detectors read came from the live snapshots: the empty input (plain and with SGR, as
    # clear_input reads it) and a folded paste after the first Enter (the submit check, then a second Enter)
    shown = {(r["kind"], r["ansi"]) for r in w.jsonl(w.tmux_state / "screens.jsonl")}
    must({("empty", False), ("empty", True), ("folded", False)} <= shown, f"screens shown: {sorted(shown)}")


def oracle_gate(run):
    """No merge past the gate: at the moment of every `gh pr merge` the PR was green on its head,
    Codex had reviewed exactly that head, it was not a draft, and the dispatcher pinned the head."""
    merges = run.world.jsonl(run.world.gh_state / "merges.jsonl")
    must(len(merges) == 2, f"expected two merges, got {len(merges)}")
    for m in merges:
        must(m["ci"] == "success", f"PR #{m['pr']} merged while CI was {m['ci']}")
        must(m["codex"] == "head", f"PR #{m['pr']} merged while Codex review was «{m['codex']}»")
        must(not m["draft"], f"PR #{m['pr']} merged as a draft")
        must("--match-head-commit" in m["argv"] and m["argv"][m["argv"].index("--match-head-commit") + 1] == m["sha"],
             f"PR #{m['pr']}: head not pinned")
        must("--squash" in m["argv"], f"PR #{m['pr']}: not a squash merge")


def oracle_rebind(run):
    """After /clear the dispatcher followed the wave into the NEW journal (bound by the marker)."""
    w2 = run.state["waves"]["W2"]
    sids = run.actor.sessions["wab-w2"].sids
    must(len(sids) == 2, f"the session cleared {len(sids) - 1} times")
    must(w2["sessions"] == sids, f"dispatcher sessions {w2['sessions']} != journals {sids}")
    must(w2.get("restarts") == 1, f"restarts {w2.get('restarts')}")
    must(w2.get("tokens", 10 ** 9) < CTX_LIMIT, f"context after the rebind {w2.get('tokens')}: still measuring the old journal")
    must(w2["peak"] >= CTX_LIMIT, f"the dispatcher never saw the context grow (peak {w2['peak']})")


def oracle_policy(run):
    answers = [t for _, wave, t in run.actor.received if "РЕШЕНИЕ ПО ПОЛИТИКЕ" in t]
    must(len(answers) == 1 and answers[0].startswith("[wab] РЕШЕНИЕ ПО ПОЛИТИКЕ (chain.json): вариант A"),
         f"policy answers: {answers}")


def oracle_flow(run):
    """The scenario happened in order, as the dispatcher's own journal and the windows' inbox tell it."""
    w, a = run.world, run.actor
    must(run.codes == {"launch": 0, "watch": 0, "done": 0}, f"exit codes {run.codes}")
    by_wave = {wave: [t for _, x, t in a.received if x == wave] for wave in ("W1", "W2")}

    def order(texts, *needles):
        pos = -1
        for needle in needles:
            hit = next((i for i, t in enumerate(texts) if i > pos and needle in t), None)
            must(hit is not None, f"«{needle}» not found after position {pos} of {len(texts)}; last: "
                 f"{[t[:70] for t in texts[-4:]]}")
            pos = hit
    # the working copy W2 started on: fresh origin main, which carries W1's squash merge
    s = a.w2_start
    must(s and s["w1_merge"], "W2 started before W1 was merged")
    must(s["has_merge_commit"], "W2's working copy does not contain W1's merge commit")
    must(s["has_w1_file"] and s["detached"] and s["clean"], f"W2's working copy at its start: {s}")
    must(s["head"] == s["origin_main"] == w.origin_git("rev-parse", "main~1"), f"W2 base is not the fresh origin main: {s}")
    # what was typed into the windows, in order
    order(by_wave["W1"], "[wave-autobot] Волна W1", "РЕШЕНИЕ ПО ПОЛИТИКЕ", "Гейт мерджа не пройден", "/exit")
    must("проверки неуспешны: ci (failure)" in " ".join(by_wave["W1"]), "the gate failure did not name the red check")
    order(by_wave["W2"], "[wave-autobot] Волна W2", "WAB-CHECKPOINT", "/clear", "/superarmanda --wave W2 --resume [wab:", "/exit")
    # the dispatcher's own journal: the gate waited for CI and for Codex, failed on red CI, then passed
    for reason in ("проверки не завершены: ci", "Codex не завершил ревью HEAD"):
        must(reason in run.events_log, f"the gate never waited: {reason}")
    order(run.events_log.splitlines(),
          "W1: launched in tmux wab-w1", "W1: policy auto-answer: class=needs_decision rec=A",
          "W1: merge gate waits", "W1: merge gate failed", "W1: merge gate passed", "W1: DONE",
          "W2: workdir", "W2: launched in tmux wab-w2", "W2: context", "W2: handoff ready, /clear",
          "W2: new session", "W2: merge gate passed", "W2: DONE", "chain finished")
    # git: two real squash merges on the bare origin
    log = w.origin_git("log", "--format=%s", "main").splitlines()
    must(log[:2] == ["wave/w2 (#2)", "wave/w1 (#1)"], f"origin main: {log}")
    tree = w.origin_git("ls-tree", "--name-only", "main").splitlines()
    must(sorted(tree) == ["README.md", "w1.txt", "w2.txt"], f"origin tree {tree}")
    # PRs, state, report
    prs = w.gh()["prs"]
    must([prs[n]["state"] for n in ("1", "2")] == ["MERGED", "MERGED"], f"PRs {prs}")
    st = run.state
    must(st.get("current") is None, f"current wave {st.get('current')}")
    must([st["waves"][x]["phase"] for x in ("W1", "W2")] == ["done", "done"], "phases")
    must([st["waves"][x]["merged"]["pr"] for x in ("W1", "W2")] == [1, 2], "merged PRs")
    must(st["waves"]["W1"]["auto_answers"] == 1, "W1 auto answers")
    result = (w.run_dir / "chain-result.md").read_text(encoding="utf-8")
    for part in ("# Итог цепочки e2e", "### W1", "### W2", f"[#1](https://github.com/{REPO}/pull/1)",
                 f"[#2](https://github.com/{REPO}/pull/2)", "смержен", "волн: 2, PR: 2"):
        must(part in result, f"chain-result.md lacks «{part}»")
    # the windows are gone, and the notice of the end reached the (stand-in) status line
    must(not list((w.tmux_state / "sessions").glob("*.json")), "a window is still open")
    shown = " ".join(" ".join(d["args"]) for d in w.jsonl(w.tmux_state / "display.jsonl"))
    must("цепочка завершена" in shown, "the end of the chain was not shown")


# ------------------------------------------------------------------ tests

class OfflineChain(unittest.TestCase):
    chain = None

    @classmethod
    def setUpClass(cls):
        t = time.monotonic()
        cls.chain = run_scenario("normal")
        cls.seconds = time.monotonic() - t

    def test_flow_of_two_waves_to_chain_result(self):
        oracle_flow(self.chain)

    def test_only_the_stand_ins_ran(self):
        oracle_hygiene(self.chain)

    def test_no_merge_past_the_gate(self):
        oracle_gate(self.chain)

    def test_session_is_followed_across_clear(self):
        oracle_rebind(self.chain)

    def test_policy_answered_once(self):
        oracle_policy(self.chain)

    def test_session_journals_have_the_live_shape(self):
        w = self.chain.world
        journals = sorted((w.home / ".claude" / "projects").glob("*/*.jsonl"))
        must(len(journals) == 3, f"journals: {[j.name for j in journals]}")
        for j in journals:
            for line in j.read_text(encoding="utf-8").splitlines():
                json.loads(line)
        sids = {s for sess in self.chain.actor.sessions.values() for s in sess.sids}
        must({j.stem for j in journals} == sids, "journal names are session ids")
        cleared = next(j for j in journals if j.stem == self.chain.actor.sessions["wab-w2"].sids[1])
        kinds = [(d.get("type"), bool(d.get("isMeta"))) for d in map(json.loads, cleared.read_text(encoding="utf-8").splitlines())]
        must(("user", True) in kinds and ("system", False) in kinds, "the caveat and the /clear echo are in the new journal")

    def test_runs_fast(self):
        must(self.seconds < 40, f"the scenario took {self.seconds:.0f} s")


class Mutations(unittest.TestCase):
    """The same scenario with the dispatcher broken: the oracle of the normal run must fail."""

    def test_without_the_merge_gate_the_oracle_sees_a_merge_past_it(self):
        run = run_scenario("gate_off")
        with self.assertRaisesRegex(AssertionError, "merged while CI was failure"):
            oracle_gate(run)

    def test_a_codex_review_of_an_older_commit_is_not_a_pass(self):
        run = run_scenario("gate_off_codex")
        with self.assertRaisesRegex(AssertionError, "Codex review was «stale»"):
            oracle_gate(run)

    def test_without_session_binding_after_clear_the_oracle_sees_it(self):
        run = run_scenario("no_rebind")
        with self.assertRaisesRegex(AssertionError, "dispatcher sessions"):
            oracle_rebind(run)

    def test_a_stale_working_copy_for_the_next_wave_is_seen(self):
        run = run_scenario("stale_base")
        with self.assertRaisesRegex(AssertionError, "does not contain W1's merge commit"):
            oracle_flow(run)

    def test_without_context_measuring_the_chain_stalls_at_the_checkpoint(self):
        with self.assertRaises(Stalled):
            run_scenario("no_checkpoint", limit=40 * 60)

    def test_without_the_decision_policy_the_chain_stalls(self):
        with self.assertRaises(Stalled):
            run_scenario("no_policy", limit=25 * 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
