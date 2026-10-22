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
  * the cleanup of a finished chain with panes that are not the run's: an unmarked pane and a pane of
    another run share the wave sessions, the owner's own dashboard of this chain sits next to the owner's
    shell and a dashboard of an older run: only the run's own dashboard pane is closed.

Режим `remnants` (W5 цепочки waves-remnants, класс RemnantsChain): кап автоответов 1; сразу после автоответа W2 на
развилку капа диспетчер останавливается и перезапускается на state.json диспетчера 1.4.1, снятом в той же точке
(tests/fixtures/waves/e2e-1.4.1-state); W2 дважды пишет ту же строку BLOCKED, владелец отвечает, цепочка доходит до
chain-result.md. Инвариант — у oracle_no_wave_text.

Режим `remnants150` (W4 цепочки waves-tails2, релиз 1.6.0, #113, класс Remnants150Chain): тот же сценарий и та же точка
перезапуска, но state.json и хранилище текстов `.texts/` сняты диспетчером 1.5.0 (tests/fixtures/waves/e2e-1.5.0-state):
текущий диспетчер читает отметки и outbox 1.5.0, не отвечает повторно и доставляет уведомления outbox.

Режим `threads` (W1 цепочки waves-tails2, #117, класс ThreadsChain): гейт W1 проходит с открытым тредом (скрипт
владельца); пока скрипт не запущен, владельцу приходят напоминания с интервалом chain.json `merge_remind_minutes`, а
`wab.py status` и дашборд после `merge_attention_minutes` показывают ATTENTION; тред закрыт на том же HEAD — следующий
такт мерджит сам. Гейт W2 тоже проходит с открытым тредом, затем волна пушит новый коммит и закрывает тред: мерджа
старого HEAD нет, новый HEAD мерджится только своим гейтом (CI на нём сначала идёт).

Mutations (same scenario, broken dispatcher, the oracle must notice): no merge gate, no session
binding after /clear, no decision policy, an idle nudge with the process tree ignored (or no nudge at all),
a cleanup that does not check whose a pane is.
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

REPO = "acme/waves-demo"
CHAIN = "e2e"
RUN_ID = "2026-10-04-e2e"
CTX_LIMIT = 50_000
TICK = 60
FLOAT_EPS = 1e-6  # допуск сравнения виртуальных времён: они суммы float, точного равенства нет
# Порог test_runs_fast сценария threads — по замеру, не вслепую (ревью W1, круг 4). Linux локально: 33 с на 692da11, 6b5e534
# и круге 4 одинаково (регрессии нет, ~105 тиков по ~0,3 с реального времени). macOS-раннер CI на 6b5e534 дал 81 с, причём
# весь его прогон был в 1,2–1,65 раза медленнее прогона на 692da11 (343 теста 907 с против 768 с, e2e 524 с против 352 с).
# Порог ловит реальное ожидание в сценарии (сон по-настоящему, ожидание лока до INPUT_LOCK_SECONDS = 120 с), а не скорость
# раннера: Linux — 60 с (запас ~1,8 к замеру), macOS — 150 с (запас ~1,85 к 81 с; одно реальное ожидание лока поверх
# сценария, 120 с + 33–81 с, его всё равно превышает).
THREADS_FAST_SECONDS = 150 if sys.platform == "darwin" else 60
# Порог test_runs_fast сценариев offline, remnants и remnants150 — тоже по замеру. Linux — прежние 40 с. macOS-раннер CI
# (run 38314774689) дал remnants150 44 с: run-tests.sh гоняет наборы параллельно, и медленный раннер делит CPU между
# ними; macOS — 90 с (запас ~2 к замеру 44 с при параллельном прогоне).
CHAIN_FAST_SECONDS = 90 if sys.platform == "darwin" else 40
DELAY = 20  # virtual seconds between what the dispatcher typed and the session's reaction
BOT_STATUS = "[class=needs_decision rec=A red=no]"
CAP_STATUS = "[class=blocked_cap rec=new_run red=no]"
MODEL = "claude-opus-5-5"  # the default `model` of a NEW chain (1.4.0, #108): chain.json has none, every wave starts on it
# the role policy of 1.4.0 for a high task: coder and the subagent roles on Opus, tester on Sonnet (Fable only reviews)
ROLE_MODEL = {"coder": "opus", "tester": "sonnet", "internal_reviewer": "opus", "final_check": "opus"}
# unique words in the wave questions: no external text may carry them (telegram_quote is off, #83)
QUESTION_MARKERS = {"W1": "ВОПРОС-КЕДР-7731", "W2": "ВОПРОС-ЯСЕНЬ-4418", "W2-owner": "ВОПРОС-ЛИПА-5902"}
NUDGE_MINUTES = 5  # chain.json idle_nudge_minutes of the scenario
# режим remnants: W2 после автоответа на развилку капа пишет ту же строку ещё CAP_REPEATS раз; первая повторная запись —
# через CAP_REPEAT_AFTER виртуальных секунд: позже срока повтора неотправленного уведомления (NOTIFY_RETRY_SECONDS),
# чтобы уведомление из outbox state.json 1.4.1 успело уйти, пока его эпизод жив
CAP_REPEATS = 2
CAP_REPEAT_AFTER = 7 * 60
LEGACY_141 = FIXTURES / "waves" / "e2e-1.4.1-state"
LEGACY_150 = FIXTURES / "waves" / "e2e-1.5.0-state"  # state.json и `.texts/` диспетчера 1.5.0 той же точки (режим remnants150)
# режим threads (#117): интервал напоминания и порог ATTENTION цепочки (tunable-поля chain.json), когда закрыть тред
REMIND_MINUTES = 20
ATTENTION_MINUTES = 10
THREAD_CLOSED_AFTER = 45 * 60  # виртуальных секунд после прохода гейта W1 с открытым тредом
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


class Restart(BaseException):
    """Режим remnants: диспетчер остановлен в точке перезапуска (как убитый `watch`): BaseException, чтобы ни один
    `except Exception` диспетчера его не поглотил."""


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
        self.hook = None  # режим remnants: точка перезапуска диспетчера (проверяется перед ходом волн)
        self.sleeps = 0

    def time(self):
        return self.real_time() + self.offset

    def sleep(self, seconds):
        self.offset += float(seconds)
        self.sleeps += 1
        if self.offset > self.limit:
            raise Stalled(f"the chain did not finish in {self.limit:.0f} virtual seconds")
        if self.hook is not None:
            self.hook()
        if self.actor is not None:
            self.actor.step()


# ------------------------------------------------------------------ the world on disk

class World:
    def __init__(self, base, policy=True, quote=False, max_auto_answers=None, threads=False):
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
        self.quote = quote
        self.max_auto_answers = max_auto_answers
        self.threads = threads
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
                 "idle_nudge_minutes": NUDGE_MINUTES, "telegram": False}  # no `model`: the default
        if self.quote:  # the mutant: the wave's question is allowed into the notices
            chain["telegram_quote"] = True
        if self.max_auto_answers is not None:  # режим remnants: кап автоответов волны — один
            chain["max_auto_answers"] = self.max_auto_answers
        if self.threads:  # режим threads: интервал напоминания и порог ATTENTION — tunable-поля chain.json
            chain["merge_remind_minutes"] = REMIND_MINUTES
            chain["merge_attention_minutes"] = ATTENTION_MINUTES
        if self.policy:
            chain["decision_policy"] = [{"class": "needs_decision", "rec": "A"},
                                        {"class": "blocked_cap", "rec": "new_run", "answer": "invariant"}]
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
        self.manifest_name = "manifest.json"


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
        self.statuses = []   # every status line a wave wrote
        self.cap_line = None  # строка BLOCKED развилки капа W2 (режим remnants пишет её повторно)
        self.cap_repeats = []  # (виртуальное время) каждой повторной записи той же строки
        self.threads = {}  # режим threads: wave -> что и когда было (гейт с тредом, снимки status/dash, закрытие, HEAD)

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
        self.statuses.append(text)
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

    def manifest_path(self, s):  # the CURRENT run's manifest of the wave (a new run has its own file)
        return s.wave_dir / "superarmanda" / s.manifest_name

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
            if s.wave == "W2" and self.mode.startswith("remnants"):
                return self.after(CAP_REPEAT_AFTER, "cap line again", lambda: self.w2_cap_repeat(s))
            return self.after(DELAY, "policy answer", lambda: (self.w1_answered if s.wave == "W1" else self.w2_new_run)(s))
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

    def init_manifest(self, s, name=None):
        if name:
            s.manifest_name = name
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
                          *(["--model", ROLE_MODEL[role]] if high else []))
        if not high:  # below high: one review, its report is not dereferenced (the rules of 1.2.0)
            return self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "cross_provider_reviewer",
                                 "--status", "pass", "--session-id", f"e2e-{s.wave}-reviewer", "--head", head,
                                 "--reviewed-head", head, "--packet-hash", hashlib.sha256(b"packet").hexdigest())
        # a high wave (policy 1.4.0): the internal review (Opus) of this HEAD before the external packet, both
        # task reviews of this HEAD, written by the real review.py and verified by state.py, then the final check
        self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "internal_reviewer", "--status",
                      "pass", "--session-id", f"e2e-{s.wave}-internal", "--head", head,
                      "--model", ROLE_MODEL["internal_reviewer"])
        packet_hash, reports = self.w.review(self.w.workdir, self.w.git("rev-parse", "origin/main"), head,
                                             s.wave_dir / "superarmanda" / "reports", f"задача волны {s.wave}\n")
        for role, profile in (("cross_provider_reviewer", "claude-host"), ("second_reviewer", "codex-host")):
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", role, "--status", "pass",
                          "--session-id", f"e2e-{s.wave}-{role}", "--head", head, "--reviewed-head", head,
                          "--packet-hash", packet_hash, "--artifact", reports[profile])
        if self.mode != "no_final_check":  # the mutant: a high wave that skips the final check never merges
            self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "final_check", "--status",
                          "pass", "--session-id", f"e2e-{s.wave}-final", "--head", head,
                          "--model", ROLE_MODEL["final_check"])
        self.did(s, "internal review, two task reviews and the final check recorded")

    def finish(self, s, next_prompt=None):
        (s.wave_dir / "result.md").write_text(f"{s.wave} готова: {s.wave.lower()}.txt добавлен.\n", encoding="utf-8")
        if next_prompt:
            (s.wave_dir / "next-prompt.md").write_text(next_prompt, encoding="utf-8")
        self.write_status(s, "DONE")

    # -- W1: BLOCKED -> policy answer -> work -> DONE -> gate waits / fails / passes
    def start_w1(self, s):
        s.journal.turn(6_000)
        self.write_status(s, f"BLOCKED: {BOT_STATUS} Какую схему выбрать? {QUESTION_MARKERS['W1']}. Варианты: A) тонкая правка, B) переделка.")

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
        if self.mode == "threads":  # Codex reviews the head and leaves one open thread on it
            self.after(130, "codex review", lambda: (self.w.gh_set(number, codex="head", thread="open"),
                                                     self.wait_owner_script(s, self.w1_owner_wait)))
        else:
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
        self.after(80, "w2 asks", lambda: self.w2_asks(s))

    def w2_asks(self, s):
        """A fork no rule of the policy covers (class `question`): the owner is asked (the notice that needs a human,
        ATTENTION) and answers in the window a few minutes later; the wave goes on."""
        s.journal.turn(9_000)
        self.write_status(s, f"BLOCKED: [class=question rec=A red=no] Нужен выбор. {QUESTION_MARKERS['W2-owner']}. "
                             "Варианты: A) да, B) нет")
        self.after(240, "owner answers", lambda: self.w2_owner_answered(s))

    def w2_owner_answered(self, s):
        s.journal.turn(10_000)
        self.write_status(s, "RUNNING")
        self.after(60, "w2 blocks", lambda: self.w2_blocks(s))

    def w2_blocks(self, s):
        """Three unsuccessful fix cycles on the manifest of the first run (state.py's own fix-loop): the task is
        `blocked`, the wave asks the cap fork. The dispatcher answers it by the fixed answer `invariant`; the wave
        then opens run 2 (a new manifest of the plan, runs.json counts it) and goes on there."""
        s.journal.turn(11_000)
        head, m = self.w.git("rev-parse", "HEAD"), str(self.manifest_path(s))
        self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "coder", "--status", "pass",
                      "--session-id", "e2e-W2-coder-1", "--head", head, s=s)
        self.state_py("task-result", "--manifest", m, "--task", "T1", "--role", "tester", "--status", "findings",
                      "--session-id", "e2e-W2-tester-1", "--head", head, s=s)
        fix = lambda *a: self.state_py("fix-loop", "--manifest", m, "--task", "T1", "--source", "tester", *a, s=s)  # noqa: E731
        fix("--outcome", "failed")
        fix("--outcome", "failed")
        fix("--decision", "invariant", "--note", "ещё круг")
        fix("--outcome", "failed")
        self.cap_line = (f"BLOCKED: {CAP_STATUS} Три круга исправления T1. {QUESTION_MARKERS['W2']}. "
                         "Варианты: new_run | accept_limitation")
        self.write_status(s, self.cap_line)

    def w2_cap_repeat(self, s):
        """Режим remnants: волна не открыла новый прогон и снова записала ТУ ЖЕ строку развилки капа, без RUNNING между
        записями (кап автоответов волны — один, он израсходован). Каждая запись — новый эпизод: владелец получает
        уведомление. После CAP_REPEATS записей владелец отвечает в окне, и волна идёт в прогон 2."""
        s.journal.turn(11_500)
        self.write_status(s, self.cap_line)
        self.cap_repeats.append(self.clock.offset)
        if len(self.cap_repeats) < CAP_REPEATS:
            return self.after(3 * TICK, "cap line again", lambda: self.w2_cap_repeat(s))
        self.after(4 * TICK, "owner answers the cap fork", lambda: self.w2_new_run(s))

    def w2_new_run(self, s):
        s.journal.turn(12_000)
        self.init_manifest(s, "manifest-2.json")
        self.write_status(s, "RUNNING")
        self.after(40, "w2 done", lambda: self.w2_done(s))

    def w2_done(self, s):
        self.record_results(s)
        s.journal.turn(11_000)
        if self.mode == "threads":  # the gate of W2 also passes with an open thread
            self.w.gh_set(max(int(n) for n in self.w.gh()["prs"]), thread="open")
            self.wait_owner_script(s, self.w2_pushes)
        self.finish(s)

    # -- режим threads (#117)
    def wait_owner_script(self, s, then):
        """Each tick: has the gate passed with an open thread (the BLOCKED line of the owner's script)?"""
        text = (s.wave_dir / "status").read_text(encoding="utf-8") if (s.wave_dir / "status").exists() else ""
        if text.startswith("BLOCKED: merge gate passed; 1 unresolved review threads; owner runs "):
            self.threads[s.wave] = {"gate_at": self.clock.offset, "head": self.w.git("rev-parse", "HEAD")}
            self.did(s, "the owner's script is waiting")
            return then(s)
        self.after(TICK, "owner script?", lambda: self.wait_owner_script(s, then))

    def snapshot(self, s, label):
        """What the owner sees at the machine now: `wab.py status` and the dashboard's panel of the current wave."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.status_cmd(wab.load_chain(self.w.chain_file, create=False))
        try:
            import dash
            from rich.console import Console
        except ImportError:  # no rich (a supported standalone setup): the oracle skips only the dashboard checks
            panel = None
        else:
            con = Console(width=220, record=True, file=io.StringIO())
            cfg = wab.load_chain(self.w.chain_file, create=False)
            con.print(dash.current_panel(cfg, wab.load_state(cfg)))
            panel = con.export_text()
        self.threads[s.wave][label] = {"at": self.clock.offset, "status": out.getvalue(), "dash": panel}

    def w1_owner_wait(self, s):
        """Nobody runs the owner's script: the dispatcher reminds, the dashboard and `status` raise ATTENTION; then the
        thread is closed on GitHub (the owner or the wave did it) — the head stays the same."""
        number = max(int(n) for n in self.w.gh()["prs"])
        self.after((ATTENTION_MINUTES - 5) * 60, "snapshot early", lambda: self.snapshot(s, "early"))
        self.after((ATTENTION_MINUTES + 5) * 60, "snapshot late", lambda: self.snapshot(s, "late"))

        def close():
            self.w.gh_set(number, thread="resolved")
            self.threads[s.wave]["closed_at"] = self.clock.offset
            self.did(s, "the thread is closed on the same head")
        self.after(THREAD_CLOSED_AFTER, "thread closed", close)

    def w2_pushes(self, s):
        """The wave pushes a fix after the gate passed with an open thread and closes the thread: the old head must
        never be merged, the new one only by its own gate (its CI is running at first)."""
        number = max(int(n) for n in self.w.gh()["prs"])
        (self.w.workdir / "w2.txt").write_text("work of W2, fixed after the thread\n", encoding="utf-8")
        self.w.git("commit", "-q", "-am", "W2: fix after the review thread")
        self.w.git("push", "-q", "origin", "wave/w2")
        self.w.gh_set(number, thread="resolved", ci="in_progress")
        self.state_py("resume", "--manifest", self.manifest_path(s), "--repo", self.w.workdir,
                      "--base", self.w.git("rev-parse", "origin/main"), "--head", self.w.git("rev-parse", "HEAD"), s=s)
        self.record_results(s)
        self.threads[s.wave]["new_head"] = self.w.git("rev-parse", "HEAD")
        self.threads[s.wave]["pushed_at"] = self.clock.offset
        self.did(s, "a new commit pushed, the thread closed")
        self.after(3 * TICK, "ci green", lambda: self.w.gh_set(number, ci="success"))


# ------------------------------------------------------------------ one run of the scenario

class Run:
    pass


def put_legacy_state(run):
    """Режим remnants: state.json прогона заменяется снятым state.json диспетчера 1.4.1 той же точки сценария
    (tests/fixtures/waves/e2e-1.4.1-state, см. README). Руками, как названо в README: плейсхолдеры получают значения
    ЭТОГО прогона (корень мира, его HOME, срез имени каталога журналов, id сессий, sha коммитов по их темам), моменты @T:±с@ —
    «сейчас» виртуальных часов плюс сдвиг, [inode, mtime] файла status (@STAMP:W@) — штамп файла status этого прогона;
    хранилище текстов `.texts/` волн удаляется: у 1.4.1 его не было, текст есть только в самом state.json.
    Режим remnants150: то же из tests/fixtures/waves/e2e-1.5.0-state, а хранилище `.texts/` волн заменяется снятым
    диспетчером 1.5.0 (файлы названы eid своего текста и копируются как есть, 0600 в каталоге 0700)."""
    w, a = run.world, run.actor
    raw = (run.fixture / "state.json").read_text(encoding="utf-8")
    raw = raw.replace("@SLUG@", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(w.workdir)))
    raw = raw.replace("@HOME@", str(w.home)).replace("@ROOT@", str(w.root))
    for s in a.sessions.values():
        for i, sid in enumerate(s.sids):
            raw = raw.replace(f"@SID:{s.wave}:{i}@", sid)
    revs = {}
    for args in (("-C", str(w.workdir)), ("--git-dir", str(w.origin))):
        for line in w.run("git", *args, "log", "--all", "--format=%H %s").stdout.splitlines():
            sha, subject = line.split(" ", 1)
            revs[subject] = sha
    raw = re.sub(r"@REV(\d*):([^@]+)@", lambda m: revs[m.group(2)][:int(m.group(1) or 40)], raw)
    now = time.time()

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            m = re.fullmatch(r"@T:([-+][0-9.]+)@", node)
            if m:
                return now + float(m.group(1))
            m = re.fullmatch(r"@STAMP:(W\d)@", node)
            if m:
                st = os.stat(w.run_dir / m.group(1) / "status")
                return [st.st_ino, st.st_mtime_ns]
        return node
    doc = walk(json.loads(raw))
    if run.mode == "remnants150_no_marks":  # мутант фикстуры: отметок отвеченного эпизода 1.5.0 у W2 нет
        for key in ("policy_episode", "policy_answered_stamp", "policy_keys"):
            doc["waves"]["W2"].pop(key, None)
    left = re.findall(r"@(?:ROOT|HOME|SLUG|SID|REV|T|STAMP)[^@]*@", json.dumps(doc, ensure_ascii=False))
    must(not left, f"placeholders of the {run.fixture.name} fixture left: {left}")
    for wave in ("W1", "W2"):
        store = w.run_dir / wave / wab.TEXTS_DIR
        shutil.rmtree(store, ignore_errors=True)
        texts = run.fixture / "texts" / wave
        if texts.is_dir():  # 1.5.0: the store of the wave as the old dispatcher left it
            store.mkdir(mode=0o700)
            for f in sorted(texts.iterdir()):
                (store / f.name).write_bytes(f.read_bytes())
                os.chmod(store / f.name, 0o600)
    wab.state_path({"run_dir": w.run_dir}).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8")
    return doc


def run_scenario(mode="normal", limit=90 * 60):
    """mode: normal | gate_off (merge gate always passes, CI red) | gate_off_codex (the same, CI green but
    Codex reviewed an older commit) | no_rebind (find_new_session finds nothing) | no_policy (chain.json
    without decision_policy) | stale_base (the next wave starts on the old working copy) | no_checkpoint
    (the context is never measured) | lowered_policy (the high wave W1 builds its manifest with the level
    lowered to medium and one review) | no_final_check (the high wave W1 records both reviews and no final
    check) | no_children_check (the nudge ignores the process tree) | no_nudge (no idle
    nudge at all) | kill_any (the cleanup
    does not check whose a pane is) | remnants (restart on the 1.4.1 state.json) | remnants150 (restart on the
    state.json and `.texts/` of 1.5.0) | remnants150_no_marks (the same without the marks of W2's answered episode)."""
    base = Path(tempfile.mkdtemp(prefix="wabe2e-"))
    atexit.register(shutil.rmtree, base, True)
    legacy = mode.startswith("remnants")  # remnants | remnants_no_upgrade | remnants_no_episode | remnants150*
    world = World(base, policy=mode != "no_policy", quote=mode == "telegram_quote",
                  max_auto_answers=1 if legacy else None, threads=mode == "threads")
    world.setup()
    clock = Clock(limit)
    actor = clock.actor = Actor(world, clock, mode)
    run = Run()
    run.world, run.actor, run.clock, run.mode, run.codes, run.out = world, actor, clock, mode, {}, io.StringIO()
    run.urlopen_calls = []
    run.attention = []  # every text of the ATTENTION file as the dispatcher wrote it
    run.telegram = []  # what Telegram would carry: render_notice() of EVERY notice that reaches notify()
    run.notices = []  # (виртуальное время, волна, текст) того же
    run.restart_at = None  # режим remnants: виртуальный момент перезапуска диспетчера на state.json прошлой версии
    run.fixture = LEGACY_150 if mode.startswith("remnants150") else LEGACY_141  # чей state.json подкладывается
    run.legacy = None  # state.json 1.4.1 с подставленными значениями этого прогона
    run.first_write = None  # state.json после первой записи перезапущенного диспетчера
    run.outbox_sent = []  # (время, волна, ключ) записей outbox, отправленных после перезапуска

    def restart_point():  # W2 получила автоответ на развилку капа, ответ дошёл (без pending_enter): перезапуск
        if run.restart_at is not None:
            return
        try:
            rec = json.loads((world.run_dir / "state.json").read_text(encoding="utf-8"))["waves"].get("W2") or {}
        except (OSError, ValueError, KeyError):
            return
        if rec.get("auto_answers") == 1 and "pending_enter" not in rec:
            raise Restart()
    if legacy:
        clock.hook = restart_point

    def no_network(*a, **kw):
        run.urlopen_calls.append(a)
        raise AssertionError("network access in an offline test")

    def call(*argv):
        try:
            wab.main(["wab.py", *argv])
            return 0
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1

    real_notify = wab.notify

    def notify_seen(cfg, notice, wave=None):  # the one place every notice passes: record the Telegram text, then send
        run.telegram.append(wab.render_notice(notice, wab.TG_MESSAGE_LIMIT))
        run.notices.append((clock.offset, wave, run.telegram[-1]))
        return real_notify(cfg, notice, wave)

    real_save, real_outbox_notice = wab.save_state, wab.outbox_notice

    def save_seen(cfg, st):  # the first state.json the restarted dispatcher writes
        real_save(cfg, st)
        if run.restart_at is not None and run.first_write is None:
            run.first_write = json.loads(wab.state_path(cfg).read_text(encoding="utf-8"))

    def outbox_seen(cfg, wave, w, key, item):  # an outbox entry is being sent
        if run.restart_at is not None:
            run.outbox_sent.append((clock.offset, wave, key))
        return real_outbox_notice(cfg, wave, w, key, item)

    real_attention = wab.attention_text

    def attention_seen(st, cfg=None):  # the one function that makes the text of ATTENTION
        text = real_attention(st, cfg)
        if text is not None:
            run.attention.append(text)
        return text

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(wab, "attention_text", attention_seen))
        stack.enter_context(mock.patch.object(wab, "notify", notify_seen))
        if legacy:
            stack.enter_context(mock.patch.object(wab, "save_state", save_seen))
            stack.enter_context(mock.patch.object(wab, "outbox_notice", outbox_seen))
        if mode == "remnants_no_upgrade":  # мутант: state.json 1.4.1 читается без миграции текста волны
            stack.enter_context(mock.patch.object(wab, "upgrade_record", lambda w, legacy: None))
        if mode == "remnants_no_episode":  # мутант: штампа записи status нет, повтор строки — не новый эпизод (1.4.3)
            stack.enter_context(mock.patch.object(wab, "_status_stamp", lambda cfg, wave: None))
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
        if mode == "kill_any":
            stack.enter_context(mock.patch.object(wab, "_ours", lambda cfg, session=None, pane=None: "ours"))
        run.env_seen = {"wab_tmux": sorted(k for k in os.environ if k.startswith(("WAB_", "TMUX"))),
                        "home": str(Path.home()), "path": os.environ["PATH"]}
        run.resolved = {t: shutil.which(t) for t in EXPECTED_TOOLS + TOOLS_THAT_MUST_NOT_RESOLVE}
        try:
            run.codes["launch"] = call("launch", str(world.chain_file), "W1", str(world.root / "w1-prompt.md"))
            if legacy:  # the first watch is stopped at the restart point, state.json becomes the one of 1.4.1
                try:
                    call("watch", str(world.chain_file))
                    raise AssertionError("the chain never reached the restart point (W2 auto-answered on the cap fork)")
                except Restart:
                    pass
                run.restart_at = clock.offset
                run.received_at_restart = len(actor.received)
                run.legacy = put_legacy_state(run)
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
    run.display = [" ".join(d["args"]) for d in world.jsonl(world.tmux_state / "display.jsonl")]
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
    chain passed the check of the two reviews of the plan and, as a chain of 1.4.0 without `model`, did not warn."""
    w = run.world
    merged = {m["pr"]: m["sha"] for m in w.jsonl(w.gh_state / "merges.jsonl")}
    manifests = {}
    for wave in ("W1", "W2"):
        runs = json.loads((w.run_dir / wave / "runs.json").read_text(encoding="utf-8"))["runs"]
        manifests[wave] = json.loads(Path(runs[-1]["manifest"]).read_text(encoding="utf-8"))
        m = manifests[wave]
        must((m["version"], m["review_policy"], m["plan"]["sha256"])
             == (2, {"version": "1.4.0", "level": WAVE_RISK[wave]}, w.plan_sha256),
             f"{wave}: manifest policy {m.get('version')}/{m.get('review_policy')}")
    results = manifests["W1"]["tasks"]["T1"]["results"]
    reviews = {role: results.get(role) or {} for role in ("cross_provider_reviewer", "second_reviewer")}
    must(sorted(r.get("profile") for r in reviews.values()) == ["claude-host", "codex-host"],
         f"the high wave W1 was merged without the two reviews: {sorted(results)}")
    for role, r in reviews.items():
        must(r["status"] == "pass" and r["head"] == merged.get(1) and r["artifact_sha256"], f"W1 {role}: {r}")
    must(len({r["packet_hash"] for r in reviews.values()}) == 1, "the two reviews of W1 cover different packets")
    must((results["coder"]["model"], results["tester"]["model"]) == ("claude-opus-5-5", "claude-sonnet-5-5"),
         "W1 coder/tester model: the role policy of 1.4.0 (coder Opus, tester Sonnet)")
    # 1.2.4: the internal review of the HEAD of the first external packet and the final check of the merged HEAD
    task = manifests["W1"]["tasks"]["T1"]
    internal = task.get("internal_review") or {}
    must(internal.get("status") == "pass" and internal.get("head") == task["external_review"]["head"] == merged.get(1),
         f"W1 internal review: {internal}")
    final = results.get("final_check") or {}
    must(final.get("status") == "pass" and final.get("head") == merged.get(1) and final.get("model") == "claude-opus-5-5"
         and final.get("after_reviews") == {role: reviews[role]["result_id"] for role in reviews},
         f"W1 final check: {final}")
    must("second_reviewer" not in manifests["W2"]["tasks"]["T1"]["results"], "the medium wave W2 needs one review")
    must("chain.json has no `model`" not in run.events_log, "a new chain runs on Opus by default: no warning")
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
    answers = [(wave, t) for _, wave, t in run.actor.received if "РЕШЕНИЕ ПО ПОЛИТИКЕ" in t]
    must([w for w, _ in answers] == ["W1", "W2"], f"policy answers went to {[w for w, _ in answers]}")
    must(answers[0][1].startswith("[wab] РЕШЕНИЕ ПО ПОЛИТИКЕ (chain.json): вариант A"), f"policy answers: {answers}")


def oracle_blocked_cap(run):
    """The cap fork (#89): W2 got exactly ONE fixed answer `invariant`, after the dispatcher's machine check of
    runs.json / the manifest / max_runs; the decision is logged with the answer, the recommendation and the class
    as separate tokens; the wave went on in run 2 (runs.json counts two) and was merged."""
    a, w = run.actor, run.world
    got = [t for _, wave, t in a.received if wave == "W2" and "РЕШЕНИЕ ПО ПОЛИТИКЕ" in t]
    must(len(got) == 1 and got[0].startswith("[wab] РЕШЕНИЕ ПО ПОЛИТИКЕ (chain.json): вариант invariant."),
         f"W2 policy answers: {got}")
    must("(рекомендация волны: new_run)" in got[0], f"the answer does not name the wave's recommendation: {got[0]}")
    log = (w.run_dir / "W2" / "policy-decisions.log").read_text(encoding="utf-8").splitlines()
    must(len(log) == 1 and all(re.search(rf"(?<![\w=]){tok}(?![\w])", log[0])
                               for tok in ("answer=invariant", "rec=new_run", "class=blocked_cap")),
         f"policy-decisions.log of W2: {log}")
    must(run.state["waves"]["W2"]["auto_answers"] == 1, "W2 auto answers")
    must("W2: policy auto-answer: class=blocked_cap answer=invariant rec=new_run" in run.events_log, "no event of the answer")
    runs = json.loads((w.run_dir / "W2" / "runs.json").read_text(encoding="utf-8"))["runs"]
    must([r["index"] for r in runs] == [1, 2], f"W2 runs: {runs}")


def oracle_model(run):
    """The model of the chain — the default Opus of a new chain without `model` (1.4.0) — reaches the command of
    EVERY wave session (`claude ... --model <model>`), and the zone
    the session is told is the default one (Dubai: SUPERARMANDA_TZ was cleared, #88)."""
    news = run.world.jsonl(run.world.tmux_state / "new_sessions.jsonl")
    must([n["name"] for n in news] == ["wab-w1", "wab-w2"], f"sessions: {[n['name'] for n in news]}")
    for n in news:
        cmd = n["command"]
        must("--model" in cmd and cmd[cmd.index("--model") + 1] == MODEL, f"{n['name']}: command {cmd}")
        must(n["env"].get("SUPERARMANDA_TZ") == "Asia/Dubai", f"{n['name']}: zone {n['env'].get('SUPERARMANDA_TZ')}")


def external_texts(run):
    """EVERY text that leaves the dispatcher, with its surface: what Telegram would carry (render_notice of each
    notice at notify()), the status-line messages that reached tmux, every state of the ATTENTION file."""
    return ([("telegram", t) for t in run.telegram] + [("display", t) for t in run.display]
            + [("attention", t) for t in run.attention])


def oracle_external(run):
    """THE invariant of 1.2.7 + 1.2.6 over everything that leaves the machine: a line of time signed by the owner's
    zone (Dubai by default), no machine tokens (class=, rec=, red=), no word of the wave's question (telegram_quote
    is off). One sweep over all captured texts, whatever the surface or the event."""
    texts = external_texts(run)
    must(texts, "no external text was captured")
    surfaces = {k for k, _ in texts}
    must(surfaces == {"telegram", "display", "attention"}, f"surfaces captured: {sorted(surfaces)}")
    must(any("Нужно:" in t for k, t in texts if k == "telegram"), "no notice that needs the owner (blocked) was captured")
    must(any("по политике" in t for k, t in texts if k == "telegram"),
         "no notice of a policy answer was captured")
    must(any("завершена" in t for k, t in texts if k == "telegram"), "no notice of a finished (merged) wave was captured")
    markers = tuple(QUESTION_MARKERS.values())
    for kind, text in texts:
        must(re.search(r"(?m)^(?:Время|time): .*Дубай", text) or (kind == "display" and re.search(r"Время: .*Дубай", text)),
             f"{kind}: no line of time signed «Дубай»: {text[:160]!r}")
        for token in ("class=", "rec=", "red="):
            must(token not in text, f"{kind}: the machine token «{token}» in {text[:160]!r}")
        for marker in markers:
            must(marker not in text, f"{kind}: the wave's question leaked ({marker}) in {text[:160]!r}")
    # the markers are real: the waves wrote them into their BLOCKED lines (so the sweep above can fail)
    written = " ".join(run.actor.statuses)
    for marker in markers:
        must(marker in written, f"the scenario never wrote {marker}")


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


def check_reminder_times(at, gate_at, closed_at):
    """The reminders of W1 on the virtual clock: none before the interval, then once per interval, none after the
    merge. `at` — the virtual times of the reminders, `gate_at`/`closed_at` — of the gate pass and the closed thread."""
    # Виртуальное время — сумма float-слагаемых: интервал ровно в REMIND_MINUTES выходит 1199.9999999999998, и
    # сравнение без допуска ложно падает (CI ubuntu). Интервалы — разностями абсолютных времён, а не смещений от
    # gate_at (лишнее вычитание — лишняя ошибка округления), и с допуском FLOAT_EPS: он на порядки меньше любого
    # шага часов (TICK, полсекунды опроса лока), поэтому настоящий недобор интервала оракул по-прежнему видит.
    times = [t - gate_at for t in at]
    must(len(at) == THREAD_CLOSED_AFTER // (REMIND_MINUTES * 60), f"W1 reminders at {times}")
    must(times[0] >= REMIND_MINUTES * 60 - TICK - FLOAT_EPS, f"an early reminder: {times}")
    must(all(b - a_ >= REMIND_MINUTES * 60 - FLOAT_EPS for a_, b in zip(at, at[1:])),
         f"reminders closer than the interval: {times}")
    must(all(t <= closed_at + 3 * TICK + FLOAT_EPS for t in at), f"a reminder after the merge: {times}")


def oracle_threads(run):
    """#117 end to end: W1's gate passed with an open thread; reminders came once per `merge_remind_minutes` while the
    owner's script waited; `wab.py status` and the dashboard showed ATTENTION past `merge_attention_minutes` (not before);
    the thread closed on the same head -> the dispatcher merged that head itself, one merge event. W2's head moved after
    its gate: the old head was never merged, the new one only after its own gate."""
    a, w = run.actor, run.world
    t1, t2 = a.threads.get("W1"), a.threads.get("W2")
    must(t1 and t2 and "closed_at" in t1 and "new_head" in t2, f"the scenario of the threads did not run: {a.threads}")
    merges = {m["pr"]: m for m in w.jsonl(w.gh_state / "merges.jsonl")}
    must(sorted(merges) == [1, 2] and len(w.jsonl(w.gh_state / "merges.jsonl")) == 2, f"merges: {merges}")
    # W1: the gated head merged by the dispatcher, after the thread was closed, one merge event
    must(merges[1]["sha"] == t1["head"], "W1: not the gated head was merged")
    log = run.events_log
    must(log.count("W1: merge gate passed, merge of PR #1 requested") == 1, "W1: not exactly one merge event")
    must("W1: merge gate passed with 1 open threads; owner script ~/.cache/wab/" in log, "W1: no owner script")
    order = [l for l in log.splitlines() if l[21:].startswith("W1: ")]
    i_owner = next(i for i, l in enumerate(order) if "merge gate passed with 1 open threads" in l)
    i_closed = next(i for i, l in enumerate(order) if "треды закрыты" in l)
    i_merge = next(i for i, l in enumerate(order) if "merge of PR #1 requested" in l)
    must(i_owner < i_closed < i_merge, f"W1 events out of order: {order[i_owner:i_merge + 1]}")
    # the reminders: the fixed template, none before the interval, then once per interval, none after the merge
    remind = [(t, text) for t, wave, text in run.notices
              if wave == "W1" and text.splitlines()[0].endswith("ждёт тебя: запусти скрипт мерджа")]
    check_reminder_times([t for t, _ in remind], t1["gate_at"], t1["closed_at"])
    for _, text in remind:
        must("~/.cache" not in text and ".merge" not in text, "a path in the reminder")
    # ATTENTION on the long merge: not yet at the early snapshot, then in `wab.py status` and on the dashboard
    early, late = t1["early"], t1["late"]
    must("ATTENTION" not in early["status"], "ATTENTION before the threshold")
    must("ATTENTION: W1: мердж ждёт" in late["status"], f"no ATTENTION in wab.py status: {late['status']}")
    if early["dash"] is not None and late["dash"] is not None:  # None: rich is not installed, no dashboard to check
        must("ATTENTION" not in early["dash"], "ATTENTION on the dashboard before the threshold")
        must("ATTENTION" in late["dash"], "no ATTENTION on the dashboard")
    # W2: the head moved after the gate: the old head never merged, the new one only after its own gate
    must(merges[2]["sha"] == t2["new_head"] != t2["head"], "W2: the old head was merged")
    w2 = [l for l in log.splitlines() if l[21:].startswith("W2: ")]
    hits = [next((i for i, l in enumerate(w2) if needle in l), None)
            for needle in ("merge gate passed with 1 open threads", "HEAD сменился после гейта", "merge gate waits",
                           f"merge of PR #2 requested at {t2['new_head'][:12]}")]
    must(None not in hits and hits == sorted(hits), f"W2: no new gate between the moved head and the merge: {hits}")
    must(f"requested at {t2['head'][:12]}" not in log, "W2: a merge of the old head was requested")


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


# ------------------------------------------------------------------ remnants: state.json без текста, эпизоды, 1.4.1
#
# ИНВАРИАНТ (сквозная приёмка цепочки waves-remnants, W2 + W3 + рестарт): на любом пути офлайн-цепочки, включая рестарт
# диспетчера на state.json прошлой версии, state.json не содержит текста волны, каждый эпизод развилки без автоответа
# даёт ровно одно уведомление владельцу, а одна развилка — не больше одного автоответа.

_UNIT = {}


def unit_suite():
    """Набор unit-тестов waves (tests/helpers/superarmanda_waves_test.py): проверка «нет текста волны» берётся оттуда
    (json_strings / text_holders класса W2StateHoldsNoWaveText), а не пишется заново."""
    if "mod" not in _UNIT:
        import importlib.util
        spec = importlib.util.spec_from_file_location("superarmanda_waves_suite_for_e2e",
                                                      Path(__file__).with_name("superarmanda_waves_test.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _UNIT["mod"] = mod
    return _UNIT["mod"]


def wave_texts(run):
    """Тексты волн сценария: каждая строка status, которую писала волна (кроме слов STATUS_KINDS), и уникальные маркеры
    вопросов. Ни одного из них не должно быть в state.json — ни в значении, ни в ключе."""
    return sorted({t for t in run.actor.statuses if len(t) >= 5 and t not in wab.STATUS_KINDS}
                  | set(QUESTION_MARKERS.values()))


def oracle_no_wave_text(run, doc=None, where="the final state.json"):
    """(а) Инвариант 1.4.3 на state.json этого прогона: ни текста волны, ни готового текста уведомления (время в поясе
    владельца «Дубай») и набранного ответа («РЕШЕНИЕ ПО ПОЛИТИКЕ») — те же иглы, что у W2LegacyStateMigration."""
    holders = unit_suite().text_holders
    doc = run.state if doc is None else doc
    must(isinstance(doc, dict) and doc.get("waves"), f"{where}: no state.json to check")
    for text in [*wave_texts(run), "Дубай", "РЕШЕНИЕ ПО ПОЛИТИКЕ"]:
        found = holders(doc, text)
        must(not found, f"{where}: the wave's text {text[:60]!r} is in state.json at {found}")


def blocked_notices(run, wave, since=0.0, until=float("inf")):
    """Уведомления владельцу «волна ждёт тебя» (шаблон blocked) волны `wave` в окне виртуального времени."""
    head = f"⏸ {CHAIN}: волна {wave} ждёт тебя"
    return [t for at, x, t in run.notices if x == wave and since <= at < until and t.startswith(head)]


def oracle_cap_repeat(run):
    """(б) Эпизоды (1.4.4, #100): кап автоответов W2 (один) израсходован автоответом на развилку blocked_cap; волна
    дважды пишет ту же строку BLOCKED без RUNNING между записями. Каждая запись — новый эпизод без автоответа и ровно
    одно уведомление владельцу; отвеченный эпизод (до первой повторной записи) владельца не спрашивает."""
    a = run.actor
    must(len(a.cap_repeats) == CAP_REPEATS, f"the scenario wrote the cap line again {len(a.cap_repeats)} times")
    must(a.statuses.count(a.cap_line) == CAP_REPEATS + 1, "the repeats are not the very same line")
    answered = next(t for t, wave, msg in a.received if wave == "W2" and "РЕШЕНИЕ ПО ПОЛИТИКЕ" in msg)
    must(blocked_notices(run, "W2", answered, a.cap_repeats[0]) == [],
         "the owner was asked about the cap fork the policy had answered")
    bounds = [*a.cap_repeats, float("inf")]
    got = [len(blocked_notices(run, "W2", bounds[i], bounds[i + 1])) for i in range(CAP_REPEATS)]
    must(got == [1] * CAP_REPEATS, f"notices to the owner after each repeat of the BLOCKED line at the cap: {got} "
                                   f"(expected one per episode: {[1] * CAP_REPEATS})")
    must(run.events_log.count("W2: policy cap reached (1/1)") == 1, "no event of the cap reached")


def oracle_legacy_restart(run):
    """(в) Рестарт на state.json 1.4.1 (фикстура снята настоящим диспетчером 1.4.1 в той же точке сценария: W2 получила
    автоответ на развилку капа, уведомления не отправлены — Telegram лежал): (1) state.json фикстуры несёт текст волны,
    после первой записи перезапущенного диспетчера его нет; (2) повторного автоответа нет — ни в окне, ни в
    policy-decisions.log, ни в счётчике; (3) каждое неотправленное уведомление outbox фикстуры отправлено, пока жив его
    эпизод (до первой повторной записи строки), ровно один раз; (4) текст вопроса, мигрированный из state.json
    (хранилища `.texts/` у 1.4.1 нет), дошёл до chain-result.md.
    Рестарт на state.json 1.5.0 (режим remnants150): (1) текста волны нет уже в фикстуре — он в её хранилище `.texts/`,
    и вопрос владельцу из `questions[].eid` там есть; (2)–(4) — те же: отметки и outbox 1.5.0 прочитаны текущим
    диспетчером, вопрос дошёл до chain-result.md из хранилища 1.5.0."""
    a, w = run.actor, run.world
    old = "1.5.0" if run.fixture == LEGACY_150 else "1.4.1"
    must(run.restart_at is not None and run.legacy, f"the dispatcher was never restarted on the {old} state.json")
    legacy = json.dumps(run.legacy, ensure_ascii=False)
    if old == "1.4.1":
        for marker in (QUESTION_MARKERS["W2"], QUESTION_MARKERS["W2-owner"], "Дубай"):
            must(marker in legacy, f"the 1.4.1 fixture has no {marker!r}: the check below would be empty")
    else:
        oracle_no_wave_text(run, run.legacy, "the state.json of the 1.5.0 fixture")
        store = run.fixture / "texts" / "W2"
        asked = [(store / q["eid"].split(":", 1)[1]) for q in run.legacy["waves"]["W2"].get("questions") or []]
        must(any(f.exists() and QUESTION_MARKERS["W2-owner"] in f.read_text(encoding="utf-8") for f in asked),
             f"the 1.5.0 fixture keeps no question of W2 in its store: {asked}")
    oracle_no_wave_text(run, run.first_write, "state.json after the first write of the restarted dispatcher")
    oracle_no_wave_text(run)
    # (2) one fork, one automatic answer
    answers = [(t, msg) for t, wave, msg in a.received if wave == "W2" and "РЕШЕНИЕ ПО ПОЛИТИКЕ" in msg]
    must(len(answers) == 1 and answers[0][0] < run.restart_at, f"W2 policy answers: {[(t, m[:50]) for t, m in answers]}")
    log = (w.run_dir / "W2" / "policy-decisions.log").read_text(encoding="utf-8").splitlines()
    must(len(log) == 1, f"policy-decisions.log of W2 after the restart: {log}")
    must(run.state["waves"]["W2"]["auto_answers"] == 1, f"W2 auto answers {run.state['waves']['W2'].get('auto_answers')}")
    must(run.events_log.count("W2: policy auto-answer") == 1, "events: W2 auto-answered more than once")
    # (3) the unsent notices of the fixture: delivered once each, while their episode lived
    owed = sorted((wave, key) for wave, rec in run.legacy["waves"].items() for key in (rec.get("outbox") or {}))
    must(owed == [("W1", "done"), ("W2", "policy_answer"), ("W2", "started")], f"outbox of the {old} fixture: {owed}")
    for wave, key in owed:
        sent = [t for t, x, k in run.outbox_sent if (x, k) == (wave, key)]
        must(len(sent) == 1, f"the unsent {wave}/{key} of the {old} state.json was sent {len(sent)} times after the restart")
        must(sent[0] < a.cap_repeats[0], f"{wave}/{key} was sent after its episode could end")
    told = [t for at, x, t in run.notices if at >= run.restart_at]
    for wave, head in (("W1", "волна W1 завершена"), ("W2", "стартовала волна W2"),
                       ("W2", "волна W2: развилка закрыта по политике")):
        must(sum(head in t.splitlines()[0] for t in told) == 1, f"the notice «{head}» of the {old} outbox was lost")
    for wave in ("W1", "W2"):
        must(not run.state["waves"][wave].get("outbox"), f"{wave}: notices left undelivered: {run.state['waves'][wave]}")
    # (4) the question of the old record reached chain-result.md from the store (1.4.1: migrated into it; 1.5.0: its own)
    result = (w.run_dir / "chain-result.md").read_text(encoding="utf-8")
    must(QUESTION_MARKERS["W2-owner"] in result, f"the question of W2 kept only in the {old} state is lost")


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

    def test_cap_fork_is_answered_by_the_fixed_answer_invariant(self):
        oracle_blocked_cap(self.chain)

    def test_chain_model_reaches_every_wave_session(self):
        oracle_model(self.chain)

    def test_every_external_text_has_dubai_time_and_no_machine_tokens_or_questions(self):
        oracle_external(self.chain)

    def test_idle_nudge_waits_for_background_work_then_pushes_once(self):
        oracle_nudge(self.chain)

    def test_cleanup_closes_only_the_runs_own_panes(self):
        oracle_cleanup(self.chain)

    def test_runs_fast(self):
        must(self.seconds < CHAIN_FAST_SECONDS, f"the scenario took {self.seconds:.0f} s")

    def test_state_json_holds_no_wave_text(self):
        oracle_no_wave_text(self.chain)


class RemnantsChain(unittest.TestCase):
    """Сквозная приёмка цепочки waves-remnants (W5): тот же сценарий с капом автоответов 1; диспетчер остановлен сразу
    после автоответа W2 на развилку капа и перезапущен на state.json диспетчера 1.4.1, снятом в той же точке
    (tests/fixtures/waves/e2e-1.4.1-state); затем W2 дважды пишет ту же строку BLOCKED, владелец отвечает, цепочка
    доходит до chain-result.md. Инвариант — над oracle_no_wave_text."""
    chain = None

    @classmethod
    def setUpClass(cls):
        t = time.monotonic()
        cls.chain = run_scenario("remnants")
        cls.seconds = time.monotonic() - t

    def test_a_state_json_holds_no_wave_text_after_the_chain(self):
        oracle_no_wave_text(self.chain)

    def test_b_each_repeat_of_the_blocked_line_at_the_cap_notifies_the_owner_once(self):
        oracle_cap_repeat(self.chain)

    def test_c_restart_on_the_1_4_1_state_json_no_second_answer_no_lost_notice(self):
        oracle_legacy_restart(self.chain)

    def test_the_chain_still_runs_to_chain_result(self):
        oracle_flow(self.chain)
        oracle_gate(self.chain)
        oracle_blocked_cap(self.chain)

    def test_every_external_text_is_still_clean(self):
        oracle_external(self.chain)

    def test_runs_fast(self):
        must(self.seconds < CHAIN_FAST_SECONDS, f"the scenario took {self.seconds:.0f} s")


class Remnants150Chain(unittest.TestCase):
    """Сквозная приёмка релиза 1.6.0 (W4 цепочки waves-tails2, #113): тот же сценарий, что у RemnantsChain, но диспетчер
    перезапущен на state.json и хранилище `.texts/`, снятых диспетчером 1.5.0 в той же точке
    (tests/fixtures/waves/e2e-1.5.0-state): цепочка доходит до chain-result.md без повторного автоответа, outbox 1.5.0
    доставлен по разу, эпизоды повторов строки капа — по уведомлению."""
    chain = None

    @classmethod
    def setUpClass(cls):
        t = time.monotonic()
        cls.chain = run_scenario("remnants150")
        cls.seconds = time.monotonic() - t

    def test_a_state_json_holds_no_wave_text_after_the_chain(self):
        oracle_no_wave_text(self.chain)

    def test_b_each_repeat_of_the_blocked_line_at_the_cap_notifies_the_owner_once(self):
        oracle_cap_repeat(self.chain)

    def test_c_restart_on_the_1_5_0_state_json_no_second_answer_no_lost_notice(self):
        oracle_legacy_restart(self.chain)

    def test_the_chain_still_runs_to_chain_result(self):
        oracle_flow(self.chain)
        oracle_gate(self.chain)
        oracle_blocked_cap(self.chain)

    def test_every_external_text_is_still_clean(self):
        oracle_external(self.chain)

    def test_runs_fast(self):
        must(self.seconds < CHAIN_FAST_SECONDS, f"the scenario took {self.seconds:.0f} s")


class ThreadsChain(unittest.TestCase):
    """Сквозная приёмка W1 цепочки waves-tails2 (#117): гейт с открытым тредом, напоминание с интервалом, ATTENTION на
    затянувшемся merging, автомердж того же HEAD после закрытия треда, новый HEAD — только своим гейтом."""
    chain = None

    @classmethod
    def setUpClass(cls):
        t = time.monotonic()
        cls.chain = run_scenario("threads", limit=4 * 3600)
        cls.seconds = time.monotonic() - t

    def test_threads_closed_on_the_same_head_merge_and_a_moved_head_is_gated_again(self):
        oracle_threads(self.chain)

    def test_the_chain_still_runs_to_chain_result(self):
        oracle_flow(self.chain)
        oracle_gate(self.chain)

    def test_every_external_text_is_still_clean(self):
        oracle_external(self.chain)

    def test_state_json_holds_no_wave_text(self):
        oracle_no_wave_text(self.chain)

    def test_runs_fast(self):
        must(self.seconds < THREADS_FAST_SECONDS, f"the scenario took {self.seconds:.0f} s")


class ReminderOracle(unittest.TestCase):
    """The oracle of the reminders on substituted times, without a chain. The virtual times are float sums: a CI run on
    ubuntu got reminders exactly one interval apart, and the subtraction of `gate_at` made the gap 1199.9999999999998."""

    GATE = 2460.0000000000005  # with it, (gate + 2340) - gate - ((gate + 1140) - gate) == 1199.9999999999995

    def check(self, *after_gate):
        check_reminder_times([self.GATE + t for t in after_gate], self.GATE, self.GATE + THREAD_CLOSED_AFTER)

    def test_reminders_exactly_one_interval_apart_pass_despite_the_float_error(self):
        check_reminder_times([3600.0000000000005, 4800.0], self.GATE, self.GATE + THREAD_CLOSED_AFTER)

    def test_reminders_closer_than_the_interval_fail(self):
        with self.assertRaisesRegex(AssertionError, "reminders closer than the interval"):
            self.check(REMIND_MINUTES * 60, 2 * REMIND_MINUTES * 60 - 1)
        with self.assertRaisesRegex(AssertionError, "reminders closer than the interval"):
            self.check(REMIND_MINUTES * 60, 2 * REMIND_MINUTES * 60 - 0.001)

    def test_an_early_reminder_and_one_after_the_merge_fail(self):
        with self.assertRaisesRegex(AssertionError, "an early reminder"):
            self.check(REMIND_MINUTES * 60 - TICK - 1, 2 * REMIND_MINUTES * 60)
        with self.assertRaisesRegex(AssertionError, "a reminder after the merge"):
            self.check(REMIND_MINUTES * 60, THREAD_CLOSED_AFTER + 3 * TICK + 1)


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

    def test_a_cleanup_that_does_not_check_ownership_kills_foreign_panes(self):
        run = run_scenario("kill_any")
        with self.assertRaisesRegex(AssertionError, "which is not the run's, was closed"):
            oracle_cleanup(run)

    def test_a_quoted_question_in_a_notice_is_seen_by_the_external_sweep(self):
        run = run_scenario("telegram_quote")
        with self.assertRaisesRegex(AssertionError, "machine token|the wave's question leaked"):
            oracle_external(run)

    def test_a_1_4_1_state_json_read_without_migration_is_seen(self):
        """Без upgrade_record текст волны из state.json 1.4.1 остаётся в state.json после первой записи."""
        run = run_scenario("remnants_no_upgrade")
        with self.assertRaisesRegex(AssertionError, "after the first write of the restarted dispatcher: the wave's text"):
            oracle_legacy_restart(run)

    def test_a_repeat_of_the_line_without_a_new_episode_is_seen(self):
        """Без штампа записи status эпизод — только текст строки: повтор той же строки на капе молчит, а отвеченный эпизод
        после рестарта читается как новый и спрашивает владельца (oracle видит первое из двух)."""
        run = run_scenario("remnants_no_episode")
        with self.assertRaisesRegex(AssertionError, "notices to the owner after each repeat|the owner was asked about the cap"):
            oracle_cap_repeat(run)

    def test_a_1_5_0_state_json_without_the_marks_of_the_answered_episode_is_seen(self):
        """Чувствительность Remnants150Chain к фикстуре: те же state.json и `.texts/` 1.5.0 без отметок отвеченного эпизода
        W2 (policy_episode, policy_answered_stamp, policy_keys) — оракул эпизодов видит неверные уведомления на повторах."""
        run = run_scenario("remnants150_no_marks")
        with self.assertRaisesRegex(AssertionError, "notices to the owner after each repeat|the owner was asked about the cap"):
            oracle_cap_repeat(run)

    def test_without_the_decision_policy_the_chain_stalls(self):
        with self.assertRaises(Stalled):
            run_scenario("no_policy", limit=25 * 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
