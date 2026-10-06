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

Since 1.0.1-1.0.3 the same run also covers (W4, #68/#72/#57):
  * the idle nudge: W1 is RUNNING and silent past `idle_nudge_minutes` while a background shell of its
    Claude lives (the `ps` stand-in shows it): NO nudge; when the shell is gone the wave gets exactly ONE;
  * an unavailable CodeRabbit: W2 records `coderabbit` = `unavailable` (the verdict of the real
    pr_review.coderabbit_verdict on the live refusal comment, tests/fixtures/pr-review/coderabbit) and the
    gate still passes;
  * the cleanup of a finished chain with panes that are not the run's: an unmarked pane and a pane of
    another run share the wave sessions, the owner's own dashboard of this chain sits next to the owner's
    shell and a dashboard of an older run: only the run's own dashboard pane is closed.

Mutations (same scenario, broken dispatcher, the oracle must notice): no merge gate, no session
binding after /clear, no decision policy, an idle nudge with the process tree ignored (or no nudge at all),
an unavailable CodeRabbit that blocks the gate, a cleanup that does not check whose a pane is.
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
REVIEW_PY = ROOT / "skills" / "superarmanda" / "scripts" / "review.py"
FIXTURES = ROOT / "tests" / "fixtures"
FAKES = Path(__file__).resolve().parent / "wab_e2e_fakes"
sys.path.insert(0, str(WAVES))
sys.path.insert(0, str(ROOT / "skills" / "superarmanda" / "scripts"))

# the host's session may export these (a wave of the live chain does): none of them may leak
for _var in ["SUPERARMANDA_TZ", *(k for k in os.environ if k.startswith(("WAB_", "TMUX")))]:  # SUPERARMANDA_TZ: #88
    os.environ.pop(_var, None)

import wab  # noqa: E402
import pr_review  # noqa: E402

REPO = "acme/waves-demo"
CHAIN = "e2e"
RUN_ID = "2026-10-04-e2e"
CTX_LIMIT = 50_000
TICK = 60
DELAY = 20  # virtual seconds between what the dispatcher typed and the session's reaction
BOT_STATUS = "[class=needs_decision rec=A red=no]"
NUDGE_MINUTES = 5  # chain.json idle_nudge_minutes of the scenario
BG_SECONDS = 9 * 60  # how long W1's background shell lives: longer than the nudge threshold
OLD_RUN = ("e2e/2026-01-01-old", "/elsewhere/runs/e2e/2026-01-01-old")  # (@wab_run, @wab_run_dir) of another run
EXPECTED_TOOLS = ("tmux", "gh", "claude", "ps")
TOOLS_THAT_MUST_NOT_RESOLVE = ("curl", "az", "wget", "ssh")


WAVE_RISK = {"W1": "high", "W2": "medium"}  # the approved plan: one high wave (two reviews) and one medium


def review_cli_doubles():
    """The subscription CLI doubles of the review suite (tests/helpers/superarmanda_review_test.py): the
    review reports of this test are written by the real review.py over them, never by hand. They live in
    their own directory, used only for `review.py run`: the `claude` of the dispatcher's PATH still never runs."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("superarmanda_review_suite_for_e2e",
                                                  Path(__file__).with_name("superarmanda_review_test.py"))
    suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(suite)
    return {"claude": suite.MOCK, "codex": suite.CODEX_MOCK}


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
        self.reviewbin = self.root / "review-bin"
        self.policy = policy
        self.env = {
            "PATH": f"{self.bindir}{os.pathsep}{self.sysbin}", "HOME": str(self.home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
            "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
            "WAB_TMUX_SOCKET": str(self.sock), "FAKE_TMUX_STATE": str(self.tmux_state),
            "FAKE_TMUX_FIXTURES": str(FIXTURES / "screens"), "FAKE_GH_STATE": str(self.gh_state),
            "FAKE_GH_FIXTURES": str(FIXTURES / "github"), "FAKE_TMUX_SOCKET": str(self.sock),
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

    # -- reviews by the real review.py over the CLI doubles (the plan of phase A and the task reviews of a wave)
    def review_py(self, *args, mode="success"):
        env = dict(self.env, PATH=f"{self.reviewbin}{os.pathsep}{self.sysbin}", SA_TEST_MODE=mode,
                   SA_TEST_LOG=str(self.root / "review-cli-log.jsonl"))
        return subprocess.run([sys.executable, str(REVIEW_PY), *map(str, args)], env=env, capture_output=True,
                              text=True, encoding="utf-8")

    def review(self, repo, base, head, out_dir, note):
        """A packet of base..head of `repo` and the reports of both reviewers for it:
        (packet hash for state.py, {profile: report path})."""
        out_dir.mkdir(parents=True, exist_ok=True)
        req, ev, packet = out_dir / "requirements.md", out_dir / "evidence.md", out_dir / "packet.json"
        req.write_text(note, encoding="utf-8")
        ev.write_text("проверки пройдены\n", encoding="utf-8")
        r = self.review_py("packet", "--repo", repo, "--base", base, "--head", head, "--requirements", req,
                           "--test-evidence", ev, "--output", packet)
        must(r.returncode == 0, f"review.py packet: {r.stderr.strip()}")
        reports = {}
        for profile in ("claude-host", "codex-host"):
            reports[profile] = out_dir / f"result-{profile}.json"
            self.review_py("run", "--repo", repo, "--packet", packet, "--profile", profile,
                           "--output", reports[profile], "--timeout", "20")
            must(reports[profile].exists() and json.loads(reports[profile].read_text(encoding="utf-8")).get("gate_ready") is True,
                 f"review.py wrote no gate_ready report for {profile}")
        return "sha256:" + json.loads(packet.read_text(encoding="utf-8"))["packet_hash"], reports

    def approve_plan(self):
        """Phase A: the approved waves.json in the run directory, its pin, and plan-review/ with the packet
        whose diff adds the plan and the reports of both reviewers (`wab.py launch` checks them)."""
        wave = lambda wid, deps: {  # noqa: E731
            "id": wid, "title": f"Волна {wid}", "goal": f"добавить {wid.lower()}.txt", "requirements": "добавить файл",
            "acceptance": ["файл добавлен"], "risk": WAVE_RISK[wid], "checks": [{"name": "tests", "cmd": "true"}],
            "depends_on": deps}
        plan = {"version": 1, "chain": CHAIN, "repo": REPO, "base_branch": "main",
                "waves": [wave("W1", []), wave("W2", ["W1"])]}
        raw = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.plan_sha256 = hashlib.sha256(raw).hexdigest()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "waves.json").write_bytes(raw)
        repo = self.root / "plan-repo"
        repo.mkdir()
        self.run("git", "init", "-q", cwd=repo)
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
        (repo / "README.md").write_text("plan of the chain\n", encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "base", cwd=repo)
        base = self.git("rev-parse", "HEAD", cwd=repo)
        (repo / "waves.json").write_bytes(raw)
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "plan", cwd=repo)
        review_dir = self.run_dir / "plan-review"
        self.review(repo, base, self.git("rev-parse", "HEAD", cwd=repo), review_dir, "ревью плана волн\n")
        (review_dir / "plan.sha256").write_text(self.plan_sha256 + "\n", encoding="utf-8")

    def setup(self):
        for d in (self.home, self.bindir, self.sysbin, self.ctl, self.tmux_state, self.gh_state, self.reviewbin):
            d.mkdir(parents=True, exist_ok=True)
        for name, source in review_cli_doubles().items():
            (self.reviewbin / name).write_text(source, encoding="utf-8")
            (self.reviewbin / name).chmod(0o755)
        # the only system tools the dispatcher and the sessions may use
        for name, path in (("python3", sys.executable), ("git", shutil.which("git")), ("sh", shutil.which("sh")),
                           ("env", shutil.which("env")), ("cat", shutil.which("cat"))):
            must(path, f"{name} is needed by the test")
            (self.sysbin / name).symlink_to(path)
        shebang = f"#!{sys.executable} -S\n"
        for name, src in (("tmux", "tmux.py"), ("gh", "gh.py"), ("claude", "claude.py"), ("ps", "ps.py")):
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
                 "idle_nudge_minutes": NUDGE_MINUTES, "telegram": False}
        if self.policy:
            chain["decision_policy"] = [{"class": "needs_decision", "rec": "A"}]
        self.approve_plan()
        chain["plan_sha256"] = self.plan_sha256
        self.chain_file.write_text(json.dumps(chain, indent=1), encoding="utf-8")
        (self.root / "w1-prompt.md").write_text(
            "Волна W1: добавь файл w1.txt в репозиторий.\n" + "\n".join(f"Пункт {i} задачи." for i in range(1, 7)) + "\n",
            encoding="utf-8")

    # -- the tmux / ps stand-ins' world
    def tmux(self, *argv):
        """A command the OWNER would type on the private socket (split a pane off, set an option on it)."""
        return self.run("tmux", "-S", str(self.sock), *argv)

    def ps_set(self, rows):
        (self.tmux_state / "ps-extra.json").write_text(json.dumps(rows), encoding="utf-8")

    def sessions(self):
        return {f.stem: json.loads(f.read_text(encoding="utf-8"))
                for f in sorted((self.tmux_state / "sessions").glob("*.json"))}

    def panes(self):
        """{pane id: (session name, pane record)} of every pane alive now."""
        return {p["id"]: (n, p) for n, s in self.sessions().items() for p in s["panes"]}

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
        self.foreign = {}    # name -> pane id of a pane that is NOT the run's (see decorate)
        self.bg_until = None  # virtual time at which W1's background shell ended
        self.bg_since = None
        self.worked = False

    # -- scheduling
    def after(self, delay, label, fn):
        self.seq += 1
        self.queue.append((self.clock.offset + delay, self.seq, label, fn))

    def step(self):
        for rec in self.w.jsonl(self.w.tmux_state / "new_sessions.jsonl")[self.new_seen:]:
            self.sessions[rec["name"]] = Session(rec)
            self.new_seen += 1
            self.decorate(rec["name"])
        for rec in self.w.jsonl(self.w.tmux_state / "inbox.jsonl")[self.inbox_seen:]:
            self.inbox_seen += 1
            self.on_message(self.sessions[rec["session"]], rec["text"])
        while True:
            due = sorted(q for q in self.queue if q[0] <= self.clock.offset)
            if not due:
                return
            self.queue.remove(due[0])
            due[0][3]()

    def decorate(self, name):
        """The owner's panes next to the run's (a real owner splits panes off and opens dashboards)."""
        w, tmux = self.w, self.w.tmux
        old = {"@wab_run": OLD_RUN[0], "@wab_run_dir": OLD_RUN[1]}
        mine = {"@wab_run": f"{CHAIN}/{RUN_ID}", "@wab_run_dir": str(w.run_dir)}

        def split(session, **opts):
            pane = tmux("split-window", "-d", "-t", f"={session}:", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
            for k, v in opts.items():
                tmux("set-option", "-p", "-t", pane, k, v)
            return pane

        def mark(pane, marks):
            for k, v in marks.items():
                tmux("set-option", "-p", "-t", pane, k, v)

        if name == "wab-w1":
            self.foreign["unmarked"] = split("wab-w1")  # no @wab_run at all
            dash = tmux("new-session", "-d", "-s", "owner-work", "-P", "-F", "#{pane_id}", "sh").stdout.strip()
            open_cmd = f"python3 wab-open {w.chain_file}"
            tmux("set-option", "-p", "-t", dash, "@wab_open", open_cmd)
            mark(dash, mine)
            self.foreign["own dashboard"] = dash  # the run's: it IS closed at the end
            self.foreign["owner shell"] = split("owner-work")
            old_dash = split("owner-work")
            tmux("set-option", "-p", "-t", old_dash, "@wab_open", open_cmd)
            mark(old_dash, old)
            self.foreign["old dashboard"] = old_dash
        if name == "wab-w2":
            pane = split("wab-w2")
            mark(pane, old)
            self.foreign["other run"] = pane

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

    def state_py(self, *args, s=None):
        env = dict(self.w.env)
        if s is not None:  # what the dispatcher exports into the wave's tmux session (start_session)
            env.update(WAB_DIR=str(s.wave_dir), WAB_WAVE=s.wave, WAB_MAX_RUNS="2", WAB_PLAN_SHA256=self.w.plan_sha256)
        r = subprocess.run([sys.executable, str(STATE_PY), *map(str, args)], env=env, capture_output=True,
                           text=True, encoding="utf-8")
        must(r.returncode == 0, f"state.py {' '.join(map(str, args[:3]))}: {r.stderr.strip()}")

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
        if text.startswith("[wab] Толчок"):  # the idle nudge: the wave remembers its work and goes on
            return self.after(DELAY, "nudge answered", lambda: self.w1_work(s))
        if "Гейт мерджа не пройден" in text and s.wave == "W1":
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
        if self.mode == "lowered_policy" and s.wave == "W1":
            # the mutant wave: not from the approved plan, the level lowered to medium (state.py then asks
            # for one review only and calls the task ready)
            return self.state_py("init", "--manifest", self.manifest_path(s), "--repo", self.w.workdir,
                                 "--base", base, "--head", head, "--risk", "medium", s=s)
        # as the protocol tells a wave: the manifest of the wave from the approved plan (its risk is the level)
        self.state_py("init", "--manifest", self.manifest_path(s), "--repo", self.w.workdir, "--base", base,
                      "--head", head, "--from-plan", f"{self.w.run_dir / 'waves.json'}#{s.wave}",
                      "--expect-sha256", self.w.plan_sha256, s=s)

    def record_results(self, s):
        head = self.w.git("rev-parse", "HEAD")
        m = str(self.manifest_path(s))
        high = WAVE_RISK[s.wave] == "high" and self.mode != "lowered_policy"
        for role in ("coder", "tester"):
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", role, "--status", "pass",
                          "--session-id", f"e2e-{s.wave}-{role}", "--head", head,
                          *(["--model", "fable"] if high else []))
        if not high:  # below high: one review, its report is not dereferenced (the rules of 1.2.0)
            return self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "cross_provider_reviewer",
                                 "--status", "pass", "--session-id", f"e2e-{s.wave}-reviewer", "--head", head,
                                 "--reviewed-head", head, "--packet-hash", hashlib.sha256(b"packet").hexdigest())
        # a high wave (policy 1.2.4): the internal Fable review of this HEAD before the external packet, both
        # task reviews of this HEAD, written by the real review.py and verified by state.py, then the final check
        self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "internal_reviewer", "--status",
                      "pass", "--session-id", f"e2e-{s.wave}-internal", "--head", head, "--model", "fable")
        packet_hash, reports = self.w.review(self.w.workdir, self.w.git("rev-parse", "origin/main"), head,
                                             s.wave_dir / "superarmanda" / "reports", f"задача волны {s.wave}\n")
        for role, profile in (("cross_provider_reviewer", "claude-host"), ("second_reviewer", "codex-host")):
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", role, "--status", "pass",
                          "--session-id", f"e2e-{s.wave}-{role}", "--head", head, "--reviewed-head", head,
                          "--packet-hash", packet_hash, "--artifact", reports[profile])
        if self.mode != "no_final_check":  # the mutant: a high wave that skips the final check never merges
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "final_check", "--status",
                          "pass", "--session-id", f"e2e-{s.wave}-final", "--head", head, "--model", "fable")
        self.did(s, "internal review, two task reviews and the final check recorded")

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
        # RUNNING again, then silence: the wave waits for a background shell (a long `sleep` of its Bash tool)
        # for longer than idle_nudge_minutes. The `ps` stand-in shows the shell under the wave's Claude; it ends
        # at BG_SECONDS, and only then may the dispatcher push the wave (it does not know what the wave waits for).
        self.write_status(s, "RUNNING")
        self.w.ps_set([
            {"pid": 7001, "under": s.name, "etime": "02:00",
             "args": "/bin/bash -c -l source /home/e2e/.claude/shell-snapshots/snapshot-bash.sh && "
                     "eval 'sleep 600' < /dev/null && pwd -P >| /tmp/claude-cwd"},
            {"pid": 7002, "ppid": 7001, "etime": "02:00", "args": "sleep 600"}])
        self.bg_since = self.clock.offset
        self.after(BG_SECONDS, "background shell ends", lambda: self.bg_ended(s))

    def bg_ended(self, s):
        self.w.ps_set([])
        self.bg_until = self.clock.offset
        self.did(s, "background shell ended")

    def w1_work(self, s):
        if self.worked:  # one nudge answers once, whatever else arrives
            return
        self.worked = True
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

    def record_coderabbit_refusal(self, s):
        """CodeRabbit refused the review of this head: its live refusal comment (pr592 of the fixtures; the range
        end is the head of THIS PR), classified by the real pr_review.coderabbit_verdict, recorded as the role."""
        head = self.w.git("rev-parse", "HEAD")
        d = json.loads((FIXTURES / "pr-review" / "coderabbit" / "pr592-rest.json").read_text(encoding="utf-8"))
        old = d["head"]["sha"]
        comments = json.loads(json.dumps(d["issue_comments"]).replace(old, head))
        verdict = pr_review.coderabbit_verdict(head, d["reviews"], d["review_comments"], comments)
        must(verdict["status"] == "unavailable", f"the live refusal is not classified as unavailable: {verdict}")
        self.state_py("task-result", "--manifest", str(self.manifest_path(s)), "--task", "T1", "--role", "coderabbit",
                      "--status", verdict["status"], "--session-id", f"e2e-{s.wave}-coderabbit", "--head", head)
        self.did(s, f"coderabbit {verdict['status']}")

    def w2_done(self, s):
        self.record_results(s)
        self.record_coderabbit_refusal(s)
        s.journal.turn(11_000)
        self.finish(s)


# ------------------------------------------------------------------ one run of the scenario

class Run:
    pass


def run_scenario(mode="normal", limit=90 * 60):
    """mode: normal | gate_off (merge gate always passes, CI red) | gate_off_codex (the same, CI green but
    Codex reviewed an older commit) | no_rebind (find_new_session finds nothing) | no_policy (chain.json
    without decision_policy) | stale_base (the next wave starts on the old working copy) | no_checkpoint
    (the context is never measured) | lowered_policy (the high wave W1 builds its manifest with the level
    lowered to medium and one review) | no_final_check (the high wave W1 records both reviews and no final
    check) | no_children_check (the nudge ignores the process tree) | no_nudge (no idle
    nudge at all) | rabbit_blocks (a CodeRabbit that is `unavailable` blocks the gate) | kill_any (the cleanup
    does not check whose a pane is)."""
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
        if mode == "no_children_check":
            stack.enter_context(mock.patch.object(wab, "wave_children", lambda cfg, w: []))
        if mode == "no_nudge":
            stack.enter_context(mock.patch.object(wab, "_idle_nudge_tick", lambda *a, **kw: None))
        if mode == "rabbit_blocks":
            real = wab.gate.manifest_problems

            def strict(manifest, head, fingerprint, plan=None):
                out = real(manifest, head, fingerprint, plan)
                for name, entry in ((manifest or {}).get("tasks") or {}).items():
                    r = (entry.get("results") or {}).get("coderabbit") or {}
                    if r.get("head") == head and r.get("status") == "unavailable":
                        out.append(f"задача {name}: coderabbit unavailable")
                return out
            stack.enter_context(mock.patch.object(wab.gate, "manifest_problems", strict))
        if mode == "kill_any":
            stack.enter_context(mock.patch.object(wab, "_ours", lambda cfg, session=None, pane=None: "ours"))
        run.env_seen = {"wab_tmux": sorted(k for k in os.environ if k.startswith(("WAB_", "TMUX"))),
                        "home": str(Path.home()), "path": os.environ["PATH"]}
        run.resolved = {t: shutil.which(t) for t in EXPECTED_TOOLS + TOOLS_THAT_MUST_NOT_RESOLVE}
        try:
            run.codes["launch"] = call("launch", str(world.chain_file), "W1", str(world.root / "w1-prompt.md"))
            run.codes["watch"] = call("watch", str(world.chain_file))
            run.codes["done"] = call("done", str(world.chain_file))
            actor.step()  # what was typed after the last sleep (the final /exit) is read too
        except Stalled as stalled:
            # a stalled mutant keeps its run on the exception: the test reads WHY the chain stopped
            events = world.run_dir / "events.log"
            run.events_log = events.read_text(encoding="utf-8") if events.exists() else ""
            stalled.run = run
            raise
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
    ps = w.jsonl(w.tmux_state / "ps_calls.jsonl")
    must(ps, "the process tree was never read (the idle nudge and the tails watch need it)")
    for c in ps:
        must(c["argv"] == list(wab.PS_ARGV[1:]), f"ps called as {c['argv']}")
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


def oracle_wave_risk(run):
    """The gate judged each wave by its risk in the approved plan: the high wave W1 was merged with a manifest
    version 2 of level high carrying BOTH task reviews of the merged head (claude-host and codex-host, verified
    review.py reports of one packet); the medium wave W2 with one review, as in 1.2.0. The launch of the new
    chain passed the check of the two reviews of the plan, and warned once about the missing `model`."""
    w = run.world
    merged = {m["pr"]: m["sha"] for m in w.jsonl(w.gh_state / "merges.jsonl")}
    manifests = {}
    for wave in ("W1", "W2"):
        runs = json.loads((w.run_dir / wave / "runs.json").read_text(encoding="utf-8"))["runs"]
        manifests[wave] = json.loads(Path(runs[-1]["manifest"]).read_text(encoding="utf-8"))
        m = manifests[wave]
        must((m["version"], m["review_policy"], m["plan"]["sha256"])
             == (2, {"version": "1.2.4", "level": WAVE_RISK[wave]}, w.plan_sha256),
             f"{wave}: manifest policy {m.get('version')}/{m.get('review_policy')}")
    results = manifests["W1"]["tasks"]["T1"]["results"]
    reviews = {role: results.get(role) or {} for role in ("cross_provider_reviewer", "second_reviewer")}
    must(sorted(r.get("profile") for r in reviews.values()) == ["claude-host", "codex-host"],
         f"the high wave W1 was merged without the two reviews: {sorted(results)}")
    for role, r in reviews.items():
        must(r["status"] == "pass" and r["head"] == merged.get(1) and r["artifact_sha256"], f"W1 {role}: {r}")
    must(len({r["packet_hash"] for r in reviews.values()}) == 1, "the two reviews of W1 cover different packets")
    must(all(results[role]["model"] == "claude-fable-5-1" for role in ("coder", "tester")), "W1 coder/tester model")
    # 1.2.4: the internal review of the HEAD of the first external packet and the final check of the merged HEAD
    task = manifests["W1"]["tasks"]["T1"]
    internal = task.get("internal_review") or {}
    must(internal.get("status") == "pass" and internal.get("head") == task["external_review"]["head"] == merged.get(1),
         f"W1 internal review: {internal}")
    final = results.get("final_check") or {}
    must(final.get("status") == "pass" and final.get("head") == merged.get(1) and final.get("model") == "claude-fable-5-1"
         and final.get("after_reviews") == {role: reviews[role]["result_id"] for role in reviews},
         f"W1 final check: {final}")
    must("second_reviewer" not in manifests["W2"]["tasks"]["T1"]["results"], "the medium wave W2 needs one review")
    warnings = [l for l in run.events_log.splitlines() if "chain.json has no `model`" in l]
    must(len(warnings) == 1, f"one warning about the missing model, got {len(warnings)}")
    must("plan review" not in run.events_log, "the launch complained about the plan review")


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


def oracle_nudge(run):
    """The idle nudge (#68): W1 was silent for longer than the threshold while its background shell lived
    (no nudge then), and got exactly one push after the shell was gone."""
    a = run.actor
    must(a.bg_since is not None and a.bg_until is not None, "the scenario never ran W1's background shell")
    must(a.bg_until - a.bg_since > NUDGE_MINUTES * 60, "the background shell must outlive the nudge threshold")
    text = wab.idle_nudge_text(NUDGE_MINUTES)
    nudges = [(t, wave) for t, wave, msg in a.received if msg == text]
    must(nudges, "no idle nudge reached the silent wave once its background work was gone")
    must(len(nudges) == 1, f"{len(nudges)} idle nudges in one idle episode")
    t, wave = nudges[0]
    must(wave == "W1", f"the nudge went to {wave}")
    must(t >= a.bg_until, f"a false idle nudge: sent at +{t - a.bg_since:.0f} s while the wave's background shell "
                          f"lived until +{a.bg_until - a.bg_since:.0f} s")
    must(run.events_log.count("W1: idle nudge sent") == 1, "events: idle nudge sent is not exactly once")
    must(not any(msg.startswith("[wab] Толчок") and msg != text for _, _, msg in a.received), "another nudge text")


def oracle_coderabbit(run):
    """An unavailable CodeRabbit (#72): the wave recorded the role, the gate did not count it against the PR."""
    w = run.world
    manifest = json.loads((w.run_dir / "W2" / "superarmanda" / "manifest.json").read_text(encoding="utf-8"))
    rabbit = manifest["tasks"]["T1"]["results"].get("coderabbit")
    must(rabbit and rabbit["status"] == "unavailable", f"W2 did not record coderabbit=unavailable: {rabbit}")
    must(rabbit["head"] == manifest["head"], "the refusal was recorded on another head than the PR's")
    must(w.gh()["prs"]["2"]["state"] == "MERGED", "W2's PR was not merged although only CodeRabbit refused")
    must("coderabbit" not in " ".join(l for l in run.events_log.splitlines() if "W2: merge gate failed" in l),
         "the gate counted the unavailable CodeRabbit against W2")


def oracle_cleanup(run):
    """The end of the chain closes only the run's own panes (#57): the dashboard of THIS run is gone; a pane without
    a mark, a pane of another run, the owner's shell and a dashboard of an older run live, and so do their sessions."""
    w, f = run.world, run.actor.foreign
    must(set(f) == {"unmarked", "own dashboard", "owner shell", "old dashboard", "other run"}, f"foreign panes: {f}")
    alive = w.panes()
    for what in ("unmarked", "owner shell", "old dashboard", "other run"):
        must(f[what] in alive, f"the pane «{what}» ({f[what]}), which is not the run's, was closed")
    must(f["own dashboard"] not in alive, "the run's own dashboard pane is still open after the chain finished")
    sessions = w.sessions()
    must({"wab-w1", "wab-w2", "owner-work"} <= set(sessions), f"sessions left: {sorted(sessions)}")
    for what, session in (("unmarked", "wab-w1"), ("other run", "wab-w2"), ("owner shell", "owner-work"),
                          ("old dashboard", "owner-work")):
        must(alive[f[what]][0] == session, f"{what} moved to {alive[f[what]][0]}")
    must("chain sessions closed: sessions [], panes ['" + f["own dashboard"] + "']" in run.events_log,
         "the dispatcher did not report closing the run's dashboard pane")


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
    must("проверки неуспешны: tests (macos-latest) (failure), tests (ubuntu-latest) (failure)" in " ".join(by_wave["W1"]), "the gate failure did not name the red check")
    order(by_wave["W2"], "[wave-autobot] Волна W2", "WAB-CHECKPOINT", "/clear", "/superarmanda --wave W2 --resume [wab:", "/exit")
    # the dispatcher's own journal: the gate waited for CI and for Codex, failed on red CI, then passed
    for reason in ("проверки не завершены: tests (macos-latest), tests (ubuntu-latest)", "Codex не завершил ревью HEAD"):
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
    must(not [pid for pid, (_, p) in w.panes().items() if p["claude"]], "a wave's Claude pane is still open")
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

    def test_high_wave_is_merged_with_two_reviews_and_the_medium_one_with_one(self):
        oracle_wave_risk(self.chain)

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

    def test_idle_nudge_waits_for_background_work_then_pushes_once(self):
        oracle_nudge(self.chain)

    def test_unavailable_coderabbit_does_not_block_the_gate(self):
        oracle_coderabbit(self.chain)

    def test_cleanup_closes_only_the_runs_own_panes(self):
        oracle_cleanup(self.chain)

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

    def test_a_high_wave_with_a_lowered_policy_and_one_review_never_passes_the_gate(self):
        """state.py calls the task ready (level medium, one review); the gate knows the wave is high by the plan."""
        with self.assertRaises(Stalled):
            run_scenario("lowered_policy", limit=70 * 60)

    def test_a_high_wave_without_the_final_check_never_passes_the_gate(self):
        """1.2.4: the two reviews are there, the final check is not; state.py does not call the task ready and the
        gate of the high wave names the role."""
        with self.assertRaises(Stalled) as held:
            run_scenario("no_final_check", limit=70 * 60)
        failures = [l for l in held.exception.run.events_log.splitlines() if "W1: merge gate failed" in l]
        self.assertTrue(failures, "the gate of W1 never failed")
        # the first refusal of the scenario is the red CI the wave then fixes; from then on the gate names the role
        self.assertIn("final_check", failures[-1])
        self.assertTrue(any("задача T1: final_check" in l for l in failures), failures)
        self.assertNotIn("W2: merge gate", held.exception.run.events_log)

    def test_a_nudge_that_ignores_the_process_tree_is_a_false_nudge(self):
        run = run_scenario("no_children_check")
        with self.assertRaisesRegex(AssertionError, "a false idle nudge"):
            oracle_nudge(run)

    def test_without_the_idle_nudge_the_silent_wave_is_never_pushed(self):
        with self.assertRaises(Stalled):
            run_scenario("no_nudge", limit=60 * 60)

    def test_a_blocking_unavailable_coderabbit_stalls_the_chain(self):
        with self.assertRaises(Stalled):
            run_scenario("rabbit_blocks", limit=70 * 60)

    def test_a_cleanup_that_does_not_check_ownership_kills_foreign_panes(self):
        run = run_scenario("kill_any")
        with self.assertRaisesRegex(AssertionError, "which is not the run's, was closed"):
            oracle_cleanup(run)

    def test_without_the_decision_policy_the_chain_stalls(self):
        with self.assertRaises(Stalled):
            run_scenario("no_policy", limit=25 * 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
