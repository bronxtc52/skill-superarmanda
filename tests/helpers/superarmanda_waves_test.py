#!/usr/bin/env python3
"""Acceptance tests A1-A13 for the wave dispatcher in scripts/waves/.

Nothing here talks to a user's tmux server: every tmux/claude/Telegram interaction goes
through module functions that are patched, TMUX is removed from the environment and
TMUX_TMPDIR points into a throw-away directory. The only real tmux use is A8, on a private
`-L wabtest-<pid>` socket that is killed afterwards.

The module enforces this itself (TMUX_GUARD below, active before any test runs):
  * TMUX_TMPDIR is a fresh temporary directory, TMUX/TMUX_PANE/WAB_TMUX_SOCKET are unset;
  * subprocess.Popen (so run/call/check_output too), os.system and os.popen raise AssertionError
    for a tmux command without `-L`/`-S`;
  * a `tmux` shim first in PATH refuses the same for scripts (wab-open) and then execs the
    real tmux.

MANUAL CHECKS (a human or an agent poking at the dispatcher) must follow the same rules, or
they hit the REAL tmux server: the global Ctrl+backslash binding gets rebound to a test session
and live sessions are lost (incident W4, 2026-10-01):
  * `export TMUX_TMPDIR=$(mktemp -d)`; `unset TMUX TMUX_PANE WAB_TMUX_SOCKET`;
  * every tmux call carries `-L <name>` or `-S <path>`; never run dash.py or wab.py on the
    default server.
"""

import ast
import atexit
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import random
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

# macOS: the default temporary directory (/var/folders/<xx>/<opaque blob>/T) holds a segment that redact() masks as an
# opaque token (_OPAQUE), so the test chain's path inside the dispatcher's own hint (`launch <chain.json> ...`) would be
# masked and the tests that read the hint fail. The tests live in /tmp there, as on Linux; the limit itself (a chain
# under such a path is masked in the hints) is an accepted limitation of 1.2.0 (PR #85).
if sys.platform == "darwin":
    tempfile.tempdir = "/tmp"
    os.environ["TMPDIR"] = "/tmp"


# ---------- TMUX_GUARD: no test may touch a tmux server it did not create ----------
def _install_tmux_guard():
    tmpdir = tempfile.mkdtemp(prefix="wabtmux-")
    atexit.register(shutil.rmtree, tmpdir, True)
    os.environ["TMUX_TMPDIR"] = tmpdir
    # a wave session exports WAB_DIR/WAB_MAX_RUNS/...: nothing of the live run may leak into tests
    for var in ["TMUX", "TMUX_PANE", *(k for k in os.environ if k.startswith("WAB_"))]:
        os.environ.pop(var, None)
    real = shutil.which("tmux")
    if real:  # a shim that refuses a socketless tmux and then execs the real binary
        shim_dir = Path(tmpdir) / "tmux-shim"
        shim_dir.mkdir()
        shim = shim_dir / "tmux"
        shim.write_text(
            '#!/bin/sh\n'
            'ok=0\n'
            'for a in "$@"; do case "$a" in -L|-S) ok=1;; esac; done\n'
            '[ "$#" = 1 ] && [ "$1" = "-V" ] && ok=1\n'
            'if [ "$ok" != 1 ]; then echo "tmux without a private socket in tests: tmux $*" >&2; exit 97; fi\n'
            f'exec "{real}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def private(argv):
        return any(a in ("-L", "-S") for a in argv)

    def check(args, shell=False):
        if isinstance(args, (str, bytes, os.PathLike)):
            text = os.fsdecode(args)
            if shell:
                if re.search(r"\btmux\b", text) and not re.search(r"(^|\s)-[LS](\s|$)", text):
                    raise AssertionError("tmux without a private socket in tests: " + text)
                return
            argv = [text]
        else:
            argv = [os.fsdecode(a) for a in args]
        if argv and os.path.basename(argv[0]) == "tmux" and argv[1:] != ["-V"] and not private(argv):
            raise AssertionError("tmux without a private socket in tests: " + " ".join(argv))

    orig_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        check(args, kw.get("shell") or (len(a) > 7 and a[7]))
        orig_init(self, args, *a, **kw)
    subprocess.Popen.__init__ = popen_init
    orig_system, orig_popen = os.system, os.popen
    os.system = lambda cmd: (check(cmd, True), orig_system(cmd))[1]
    os.popen = lambda cmd, *a, **kw: (check(cmd, True), orig_popen(cmd, *a, **kw))[1]
    return tmpdir


TMUX_GUARD_DIR = _install_tmux_guard()

ROOT = Path(__file__).resolve().parents[2]
WAVES = ROOT / "skills" / "superarmanda" / "scripts" / "waves"
sys.path.insert(0, str(WAVES))

import wab  # noqa: E402

REAL_SH = wab.sh
REAL_FIND_PR = wab.find_pr
OWN_HEAD = {"headRepositoryOwner": {"login": "o"}, "headRepository": {"name": "r"}}  # PR from the chain repo "o/r"
CHAIN = "demo"
RUN_ID = "2026-10-01"


def load_orig(name):
    """A private, unpatched copy of wab.py (the shared module is patched in setUp)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, WAVES / "wab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sanitize(cwd):
    # like Claude Code: the transcript directory is named after the REAL cwd (symlinks resolved)
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(str(cwd)))


def asst(inp=0, create=0, read=0, out=0, tools=0, side=False):
    msg = {
        "usage": {
            "input_tokens": inp,
            "cache_creation_input_tokens": create,
            "cache_read_input_tokens": read,
            "output_tokens": out,
        },
        "content": [{"type": "tool_use", "name": "Bash"} for _ in range(tools)],
    }
    d = {"type": "assistant", "message": msg}
    if side:
        d["isSidechain"] = True
    return json.dumps(d)


SCREENS = Path(__file__).resolve().parent.parent / "fixtures" / "screens"


def live(name):
    """A real Claude Code 2.1.288 screen captured in tmux (tests/fixtures/screens/README.md)."""
    return (SCREENS / name).read_text(encoding="utf-8")


EMPTY_ANSI = live("18-cleared.ansi")  # the input box with its dim placeholder: nothing typed


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {"HOME": str(self.home), "TMUX_TMPDIR": str(self.tmp / "sock")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        self.tmux_calls = []
        self.sent = []  # (kind, name, text)
        self.alive = True
        self.kill_closes = True  # a mocked `tmux kill-session` really closes the session
        self.pane = ""
        self.ansi = EMPTY_ANSI  # what `capture-pane -p -e` shows (the input-emptiness check reads it)
        self.clear_works = True  # a mocked clearing round (C-e C-u C-k BSpace) really empties the input
        self.clear_keys = []
        self.ready = True
        self.tg = []
        self.cwd = str(self.tmp / "clone")
        Path(self.cwd).mkdir()

        self.gh_calls = []
        self.gh_real = False  # gh_shim() puts a fake `gh` into PATH: then the real subprocess call runs it
        self.gh_handler = None  # args -> CompletedProcess; without it every `gh` fails, never the network

        def fake_sh(*args, **kw):
            if args and args[0] == "tmux":
                self.tmux_calls.append(args)
                if args[1:2] == ("kill-session",) and self.kill_closes:
                    self.alive = False
                if args[1:2] == ("send-keys",) and "C-u" in args:
                    self.clear_keys.append(args)
                    self.sent.append(("clear", args[-1], ""))
                    if self.clear_works:
                        self.ansi = EMPTY_ANSI
                return subprocess.CompletedProcess(args, 0, "", "")
            if args and args[0] == "gh" and not self.gh_real:
                self.gh_calls.append(args)
                if self.gh_handler:
                    return self.gh_handler(args)
                return subprocess.CompletedProcess(args, 1, "", "gh is not available in tests")
            return REAL_SH(*args, **kw)

        def patch(name, **kw):
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)

        patch("sh", side_effect=fake_sh)
        patch("tmux_alive", side_effect=lambda name: self.alive)
        patch("pane_text", side_effect=lambda name: self.pane)
        patch("pane_ansi", side_effect=lambda name: self.ansi)
        patch("send_text", side_effect=lambda n, t, **kw: self.sent.append(("text", n, t)))
        patch("send_command", side_effect=lambda n, t, **kw: self.sent.append(("cmd", n, t)))
        self.enters = []
        patch("press_enter", side_effect=lambda n: self.enters.append(n))
        patch("wait_ready", side_effect=lambda name, timeout=90: self.ready)
        patch("require_tmux")
        # nothing may reach GitHub: no PR is known unless a test sets one (see MergeGateBase)
        patch("find_pr", return_value=None)
        # the process tree of the window (#68) is not read from the test machine: no children by default;
        # ProcessFacts / BgBase put the real function back over a faked `ps`
        patch("wave_children", return_value=[])
        patch("_send_telegram", side_effect=lambda cfg, text: self.tg.append(text))
        sleeper = mock.patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    # ----- fixtures -----
    def chain(self, **over):
        doc = {
            "chain": CHAIN,
            "run_id": RUN_ID,
            "repo": "o/r",
            "waves": ["W1", "W2"],
            "tmux_prefix": "wv-",
            "tick_seconds": 1,
            "ctx_limit": 300000,
            "telegram": {"keyvault": "kv-test", "token_secret": "tok", "chat_secret": "chat"},
        }
        doc.update(over)
        doc = {k: v for k, v in doc.items() if v is not None}
        cfgdir = self.tmp / "cfg"
        cfgdir.mkdir(exist_ok=True)
        path = cfgdir / "chain.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return wab.load_chain(path), path

    def put_state(self, cfg, st):
        wab.state_path(cfg).write_text(json.dumps(st), encoding="utf-8")

    def get_state(self, cfg):
        return json.loads(wab.state_path(cfg).read_text(encoding="utf-8"))

    def wave_rec(self, name="W1", sessions=None, **kw):
        rec = {
            "tmux": f"wv-{name.lower()}",
            "cwd": self.cwd,
            "started": time.time() - 60,
            "restarts": 0,
            "phase": "running",
            "notified": {},
            "sessions": sessions if sessions is not None else [],
        }
        rec.update(kw)
        return rec

    def set_status(self, cfg, wave, text):
        (wab.wave_dir(cfg, wave) / "status").write_text(text + "\n", encoding="utf-8")

    def transcript(self, sid, lines, cwd=None, marker=None):
        d = self.home / ".claude" / "projects" / sanitize(cwd or self.cwd)
        d.mkdir(parents=True, exist_ok=True)
        if marker:
            lines = [json.dumps({"type": "user", "message": {"content": f"{marker} hello"}})] + list(lines)
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def messages(self):
        return [t for t in self.tg]

    # ----- merge_gate "auto": a wave whose PR is already MERGED at the sha the gate passed -----
    def merged_view(self, args):
        if args[:3] == ("gh", "pr", "view"):
            doc = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": "c" * 40, "baseRefName": "main"}
            return subprocess.CompletedProcess(args, 0, json.dumps(doc), "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected gh call in tests")

    def auto_chain(self, **over):
        """chain.json with merge_gate "auto" and a `gh` that reports every PR MERGED."""
        cfg, path = self.chain(**{"merge_gate": "auto", "base_branch": "main", **over})
        self.gh_handler = self.merged_view
        return cfg, path

    def auto_rec(self, name="W1", **kw):
        """A wave record in phase `merging`: the gate passed at sha c*40 for PR #7."""
        return self.wave_rec(name, phase="merging", gate_pr=7, gate_sha="c" * 40, **kw)


# ---------------------------------------------------------------- A1
class TranscriptParsing(Base):
    def test_context_is_last_main_assistant_usage_ignoring_sidechain_and_garbage(self):
        cfg, _ = self.chain()
        self.transcript("s1", [
            asst(inp=10, create=20, read=30),
            asst(inp=999999, side=True),
            "this is not json",
            json.dumps({"type": "user", "message": {"content": "x"}}),
            asst(inp=1, create=2, read=3),
            json.dumps({"type": "assistant", "message": {"content": []}}),  # no usage
            "{broken",
        ])
        w = self.wave_rec(sessions=["s1"])
        self.assertEqual(wab.context_tokens(w), 6)

    def test_no_session_or_file_means_zero(self):
        self.assertEqual(wab.context_tokens(self.wave_rec(sessions=[])), 0)
        self.assertEqual(wab.context_tokens(self.wave_rec(sessions=["absent"])), 0)

    def test_symlinked_cwd_finds_transcript_under_the_real_path(self):
        # macOS: /tmp -> /private/tmp. Claude Code names the transcript directory after the
        # REAL cwd, while the admitted clone is recorded as /tmp/cc-admission-*/checkout.
        # Live regression mh-creators run01 W0 (2026-10-03): the dashboard showed 0k/300k
        # at 157k, and WAB-CHECKPOINT would never have fired.
        real = self.tmp / "private" / "checkout"
        real.mkdir(parents=True)
        link = self.tmp / "alias"
        link.symlink_to(real.parent, target_is_directory=True)
        cwd = str(link / "checkout")
        self.transcript("s1", [asst(inp=100, create=20, read=37)], cwd=os.path.realpath(cwd))
        w = self.wave_rec(sessions=["s1"], cwd=cwd)
        self.assertEqual(wab.context_tokens(w), 157)
        self.assertEqual(wab.transcript_dir(cwd), wab.transcript_dir(os.path.realpath(cwd)))


# ---------------------------------------------------------------- A2
class TranscriptCacheTests(Base):
    def test_second_read_only_takes_appended_bytes(self):
        p = self.transcript("s1", [asst(inp=5)])
        cache = wab.TranscriptCache()
        first = cache.read(p)
        self.assertEqual(first["ctx"], 5)
        self.assertEqual(cache.bytes_read, p.stat().st_size)
        again = cache.read(p)
        self.assertEqual(again["ctx"], 5)
        self.assertEqual(cache.bytes_read, p.stat().st_size)  # nothing re-read
        before = p.stat().st_size
        extra = asst(inp=7, tools=2, out=4) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(extra)
        third = cache.read(p)
        self.assertEqual(third["ctx"], 7)
        self.assertEqual(third["tools"], 2)
        self.assertEqual(third["turns"], 2)
        self.assertEqual(cache.bytes_read, before + len(extra.encode()))

    def test_chunked_read_equals_whole_read_when_a_line_crosses_a_chunk(self):
        lines = [asst(inp=i + 1, out=i, tools=i % 3) for i in range(8)]
        p = self.tmp / "big.jsonl"
        p.write_text("\n".join(lines) + "\n" + lines[0][:15], encoding="utf-8")
        whole = wab.TranscriptCache().read(p)
        for chunk in (1, 7, 50):
            with self.subTest(chunk=chunk), mock.patch.object(wab, "READ_CHUNK", chunk):
                cache = wab.TranscriptCache()
                self.assertEqual(cache.read(p), whole)
                self.assertEqual(cache.bytes_read, p.stat().st_size)
                with open(p, "a", encoding="utf-8") as f:  # the cut line is completed later
                    f.write(lines[0][15:] + "\n")
                self.assertEqual(cache.read(p)["turns"], whole["turns"] + 1)
                p.write_text("\n".join(lines) + "\n" + lines[0][:15], encoding="utf-8")

    def test_incomplete_last_line_is_kept_and_not_parsed_twice(self):
        p = self.tmp / "t.jsonl"
        line = asst(inp=3, out=9)
        p.write_text(line[:20], encoding="utf-8")
        cache = wab.TranscriptCache()
        self.assertEqual(cache.read(p)["turns"], 0)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line[20:] + "\n")
        got = cache.read(p)
        self.assertEqual((got["turns"], got["out"], got["ctx"]), (1, 9, 3))
        self.assertEqual(cache.read(p)["turns"], 1)

    def test_truncation_and_replacement_reset_the_entry(self):
        p = self.tmp / "t.jsonl"
        p.write_text(asst(inp=1, out=5) + "\n" + asst(inp=2, out=5) + "\n", encoding="utf-8")
        cache = wab.TranscriptCache()
        self.assertEqual(cache.read(p)["turns"], 2)
        p.write_text(asst(inp=9, out=1) + "\n", encoding="utf-8")  # shorter: truncated
        got = cache.read(p)
        self.assertEqual((got["turns"], got["out"], got["ctx"]), (1, 1, 9))
        # replaced by a different file of the same or larger size (new inode)
        other = self.tmp / "other.jsonl"
        other.write_text(asst(inp=4, out=2) + "\n" + asst(inp=8, out=2) + "\n" + asst(inp=6, out=2) + "\n",
                         encoding="utf-8")
        os.replace(other, p)
        got = cache.read(p)
        self.assertEqual((got["turns"], got["ctx"]), (3, 6))

    def test_in_place_rewrite_with_same_inode_and_larger_size_resets(self):
        p = self.tmp / "t.jsonl"
        p.write_text(asst(inp=1, out=5) + "\n", encoding="utf-8")
        cache = wab.TranscriptCache()
        self.assertEqual(cache.read(p)["ctx"], 1)
        ino = p.stat().st_ino
        with open(p, "r+", encoding="utf-8") as f:  # same inode, different head, longer
            f.write(asst(inp=2, out=60000) + "\n" + asst(inp=3, out=7) + "\n")
        self.assertEqual(p.stat().st_ino, ino)
        got = cache.read(p)
        self.assertEqual((got["turns"], got["ctx"]), (2, 3))

    def test_vanished_file_is_zero_not_an_exception(self):
        got = wab.TranscriptCache().read(self.tmp / "gone.jsonl")
        self.assertEqual((got["turns"], got["ctx"]), (0, 0))

    def test_tail_mode_starts_near_the_end_on_a_line_boundary(self):
        p = self.tmp / "big.jsonl"
        old = [asst(inp=1, out=1) for _ in range(50)]
        new = [asst(inp=2, out=1) for _ in range(3)]
        p.write_text("\n".join(old + new) + "\n", encoding="utf-8")
        size = p.stat().st_size
        cache = wab.TranscriptCache(tail_bytes=size // 4)
        got = cache.read(p)
        self.assertEqual(got["ctx"], 2)
        self.assertLess(cache.bytes_read, size)
        self.assertLess(got["turns"], 53)
        self.assertGreaterEqual(got["turns"], 3)  # whole lines only, the cut line is dropped

    def test_two_waves_sharing_a_workdir_keep_separate_counters(self):
        self.transcript("a1", [asst(inp=100)])
        self.transcript("b1", [asst(inp=7)])
        self.assertEqual(wab.context_tokens(self.wave_rec("W1", sessions=["a1"])), 100)
        self.assertEqual(wab.context_tokens(self.wave_rec("W2", sessions=["b1"])), 7)


class SessionBinding(Base):
    def marker(self, cfg, wave):
        return f"[wab:{CHAIN}/{RUN_ID}/{wave}]"

    def test_new_session_found_by_marker_among_unowned_files(self):
        cfg, _ = self.chain()
        self.transcript("old1", [asst(inp=1)], marker=self.marker(cfg, "W1"))
        self.transcript("other", [asst(inp=1)], marker=self.marker(cfg, "W2"))
        self.transcript("new1", [asst(inp=1)], marker=self.marker(cfg, "W1"))
        self.transcript("noise", [asst(inp=1)])
        st = {"waves": {"W1": self.wave_rec("W1", sessions=["old1"]),
                        "W2": self.wave_rec("W2", sessions=["other"])}}
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "new1")
        st["waves"]["W1"]["sessions"].append("new1")
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))

    def test_marker_beyond_the_first_256k_is_not_a_first_message(self):
        cfg, _ = self.chain()
        pad = json.dumps({"type": "user", "message": {"content": "x" * 300_000}})
        self.transcript("late", [pad, json.dumps({"type": "user", "message": {"content": self.marker(cfg, "W1")}})])
        st = {"waves": {"W1": self.wave_rec("W1", sessions=[])}}
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))

    def test_marker_must_sit_in_the_first_user_message(self):
        cfg, _ = self.chain()
        m = self.marker(cfg, "W1")
        user = lambda c: json.dumps({"type": "user", "message": {"content": c}})  # noqa: E731
        cases = {
            "assistant_only": [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": m}]}})],
            "tool_result_only": [user([{"type": "tool_result", "tool_use_id": "x", "content": m}])],
            "second_user_message": [user("plain first message"), user(f"{m} second")],
            "sidechain_user": [json.dumps({"type": "user", "isSidechain": True, "message": {"content": m}}),
                               user("plain")],
        }
        for name, lines in cases.items():
            with self.subTest(case=name):
                self.transcript(f"neg-{name}", lines)
        st = {"waves": {"W1": self.wave_rec("W1", sessions=[])}}
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))
        # positives: plain string, text block, text block after a tool_result block
        self.transcript("pos-block", [user([{"type": "text", "text": f"hi {m}"}])])
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "pos-block")
        st["waves"]["W1"]["sessions"] = ["pos-block"]
        self.transcript("pos-mixed", [json.dumps({"type": "summary"}),
                                      user([{"type": "tool_result", "content": "x"}, {"type": "text", "text": m}])])
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "pos-mixed")

    def test_marker_after_the_scaffolding_claude_writes_on_clear(self):
        # The real shape of a transcript after /clear (Claude Code 2.1.287, run 2026-10-02-wab):
        # an isMeta caveat and the /clear echo precede the /update that carries the marker.
        cfg, _ = self.chain()
        m = self.marker(cfg, "W1")
        user = lambda c, **kw: json.dumps({"type": "user", **kw, "message": {"content": c}})  # noqa: E731
        caveat = user("<local-command-caveat>The command below was run directly in Claude Code, not sent "
                      "to you as a request.</local-command-caveat>", isMeta=True)
        clear = user("<command-name>/clear</command-name>\n            <command-message>clear</command-message>"
                     "\n            <command-args></command-args>")
        stdout = user("<local-command-stdout></local-command-stdout>")
        update = user(f"<command-message>update</command-message>\n<command-name>/update</command-name>\n"
                      f"<command-args>{m} Продолжаем волну W1.</command-args>")
        head = [json.dumps({"type": "custom-title"}), json.dumps({"type": "attachment", "isSidechain": False})]
        st = {"waves": {"W1": self.wave_rec("W1", sessions=["old"])}}
        # negative first: scaffolding is skipped, but the first real message still decides
        self.transcript("neg-plain-first", head + [caveat, clear, stdout, user("plain"), update])
        self.transcript("neg-marker-only-in-meta", head + [user(m, isMeta=True), clear, user("plain")])
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))
        self.transcript("after-clear", head + [caveat, clear, stdout, update])
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "after-clear")

    def test_marker_in_a_superarmanda_resume_slash_command_binds_the_session(self):
        # Same shape as the live /update line, with the skill command: args `--wave W1 --resume [wab:...] ...`.
        cfg, _ = self.chain()
        m = self.marker(cfg, "W1")
        user = lambda c, **kw: json.dumps({"type": "user", **kw, "message": {"content": c}})  # noqa: E731
        caveat = user("<local-command-caveat>x</local-command-caveat>", isMeta=True)
        clear = user("<command-name>/clear</command-name>\n            <command-message>clear</command-message>"
                     "\n            <command-args></command-args>")
        stdout = user("<local-command-stdout></local-command-stdout>")
        resume = user("<command-message>superarmanda</command-message>\n<command-name>/superarmanda</command-name>\n"
                      f"<command-args>--wave W1 --resume {m} Каталог волны: /x.</command-args>")
        st = {"waves": {"W1": self.wave_rec("W1", sessions=["old"])}}
        self.transcript("resumed", [caveat, clear, stdout, resume])
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "resumed")

    def test_resume_message_shape(self):
        cfg, _ = self.chain()
        text = wab.resume_message(cfg, "W1", "/runs/W1")
        self.assertTrue(text.startswith("/superarmanda --wave W1 --resume [wab:"), text)
        self.assertIn(wab.session_marker(cfg, "W1"), text)
        self.assertIn("/runs/W1", text)
        self.assertNotIn("wave-autobot", text)
        self.assertNotIn("/update", text)

    def test_sessions_of_earlier_attempts_are_never_adopted(self):
        cfg, _ = self.chain()
        m = self.marker(cfg, "W1")
        self.transcript("old1", [asst(inp=1)], marker=m)
        self.transcript("old2", [asst(inp=1)], marker=m)
        self.transcript("new0", [asst(inp=1)], marker=m)
        w = self.wave_rec("W1", sessions=["new0"], await_session=True)
        w["attempts"] = [{"phase": "dead", "sessions": ["old1", "old2"]}]
        st = {"waves": {"W1": w}}
        self.assertEqual(wab.owned_sessions(st), {"old1", "old2", "new0"})
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))  # the new log is late: nothing to adopt
        self.transcript("new1", [asst(inp=1)], marker=m)
        self.assertEqual(wab.find_new_session(cfg, st, "W1"), "new1")

    def test_marker_of_a_different_run_does_not_match(self):
        cfg, _ = self.chain()
        self.transcript("n", [asst(inp=1)], marker=f"[wab:{CHAIN}/other-run/W1]")
        st = {"waves": {"W1": self.wave_rec("W1", sessions=[])}}
        self.assertIsNone(wab.find_new_session(cfg, st, "W1"))

    def test_while_awaiting_new_session_nothing_is_measured_or_requested(self):
        cfg, _ = self.chain()
        self.transcript("s1", [asst(inp=400000)], marker=self.marker(cfg, "W1"))
        w = self.wave_rec("W1", sessions=["s1"], await_session=True)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "RESUMING")
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        st = self.get_state(cfg)
        self.assertEqual(st["waves"]["W1"]["phase"], "running")
        self.assertEqual(st["waves"]["W1"].get("tokens", 0), 0)
        self.assertEqual([s for s in self.sent if "CHECKPOINT" in s[2]], [])
        # the new session appears: it is adopted and measured
        self.transcript("s2", [asst(inp=5000)], marker=self.marker(cfg, "W1"))
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["sessions"], ["s1", "s2"])
        self.assertFalse(w["await_session"])
        self.assertEqual(w["tokens"], 5000)
        self.assertEqual(w["phase"], "running")

    def test_high_context_requests_a_checkpoint_once(self):
        cfg, _ = self.chain()
        self.transcript("s1", [asst(inp=400000)])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", sessions=["s1"])}})
        self.set_status(cfg, "W1", "RUNNING")
        wab.tick(cfg, wab.load_state(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        asks = [s for s in self.sent if "WAB-CHECKPOINT" in s[2]]
        self.assertEqual(len(asks), 1)
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual((w["phase"], w["checkpoint_sent"]), ("checkpoint", True))

    def test_clear_cycle_marks_await_and_update_carries_the_marker(self):
        cfg, _ = self.chain()
        w = self.wave_rec("W1", sessions=["s1"], phase="checkpoint", checkpoint_sent=True,
                          checkpoint_at=time.time())
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        wab.tick(cfg, wab.load_state(cfg))
        self.assertIn(("cmd", "wv-w1", "/clear"), self.sent)
        update = [s for s in self.sent if s[2].startswith("/superarmanda --wave W1 --resume")]
        self.assertEqual(len(update), 1)
        self.assertIn(f"[wab:{CHAIN}/{RUN_ID}/W1]", update[0][2])
        self.assertNotIn("wave-autobot", update[0][2])
        self.assertNotIn("/update", update[0][2])
        self.assertEqual([s for s in self.sent if s[2].startswith("/update")], [])
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertTrue(w["await_session"])
        self.assertEqual((w["phase"], w["restarts"]), ("running", 1))


# ---------------------------------------------------------------- A3 + A6 + A11
class Supervision(Base):
    def test_blocked_twice_notifies_once(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: need a decision")
        for _ in range(3):
            self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(len(self.tg), 1)
        self.assertIn("need a decision", self.tg[0])
        self.assertIn("need a decision", json.dumps(self.get_state(cfg)["waves"]["W1"]["notified"]))
        # a different question is a new notification
        self.set_status(cfg, "W1", "BLOCKED: another one")
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), 2)

    def test_done_with_dead_session_launches_the_next_wave(self):
        cfg, _ = self.auto_chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("PR #1\n", encoding="utf-8")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "launch", return_value=True) as launch:
            self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        launch.assert_called_once()
        self.assertEqual(launch.call_args[0][1], "W2")
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "done")

    def test_done_is_not_notified_twice_when_watch_restarts_before_the_next_launch(self):
        cfg, _ = self.auto_chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "launch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wab.tick(cfg, wab.load_state(cfg))
        first = len(self.tg)
        with mock.patch.object(wab, "launch", return_value=True):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), first)

    def test_done_with_external_merge_gate_waits_for_the_coordinator(self):
        cfg, _ = self.chain(merge_gate="external")
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "launch") as launch:
            self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))  # handed over: the watch ends
            self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        launch.assert_not_called()
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        self.assertEqual(len(self.tg), 1)

    def test_last_wave_done_finishes_the_chain(self):
        cfg, _ = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertIsNone(self.get_state(cfg)["current"])

    def test_done_without_next_prompt_stops_the_chain_loudly(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("W1: DONE without next-prompt.md; chain stopped"), 1)
        self.assertEqual(len([t for t in self.tg if "next-prompt.md" in t]), 1)
        with mock.patch.object(wab, "time") as t, mock.patch.object(wab, "drop_stale_btab"), \
                contextlib.redirect_stderr(io.StringIO()):
            t.time.return_value = 0.0
            with self.assertRaises(SystemExit) as ctx:
                wab.main(["wab.py", "watch", str(path)])
        self.assertEqual(ctx.exception.code, 3)

    def test_dead_window_stops_the_chain_with_one_notice(self):
        cfg, _ = self.chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(len(self.tg), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "dead")

    def test_tui_not_ready_blocks_and_sends_nothing(self):
        cfg, _ = self.chain()
        self.alive = False
        self.ready = False
        prompt = self.tmp / "p.md"
        prompt.write_text("do it\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertFalse(wab.launch(cfg, "W1", prompt))
        self.assertEqual(self.sent, [])
        status = (wab.wave_dir(cfg, "W1") / "status").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("BLOCKED"))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "not_ready")
        self.assertEqual(len(self.tg), 1)

    def test_launch_passes_a_fresh_session_id_and_marks_the_first_prompt(self):
        cfg, _ = self.chain()
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("do it\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W1", prompt))
        new = [c for c in self.tmux_calls if c[1] == "new-session"]
        self.assertEqual(len(new), 1)
        argv = list(new[0])
        sid = argv[argv.index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual((w["sessions"], w["phase"]), ([sid], "running"))
        texts = [s for s in self.sent if s[0] == "text"]
        self.assertEqual(len(texts), 1)
        self.assertIn(f"[wab:{CHAIN}/{RUN_ID}/W1]", texts[0][2])
        self.assertIn("do it", texts[0][2])

    def test_launch_refuses_when_the_session_already_exists(self):
        cfg, _ = self.chain()
        with self.assertRaises(SystemExit):
            wab.launch(cfg, "W1", self.tmp / "p.md")


# ---------------------------------------------------------------- A4
class Redaction(Base):
    def test_escaped_quotes_and_backslashes_do_not_leak_a_secret_tail(self):
        cases = [
            '{"password":"alpha\\"secret-tail"}', "token='a\\'b-tail' x", 'api_key="p\\\\q\\"r-tail" y',
            'password=a\\"b-tail', '{"secret": "x\\\\", "n": 1}',
        ]
        for text in cases:
            with self.subTest(text=text):
                out = wab.redact(text)
                for part in ("alpha", "secret-tail", "b-tail", "r-tail", "p\\"):
                    self.assertNotIn(part, out)
                self.assertLessEqual(len(wab.redact(text * 200, 600)), 600)
                self.assertLessEqual(len(wab.redact(text * 200, 1200)), 1200)

    def test_bare_long_hex_is_masked_but_labelled_shas_survive(self):
        key = "ab12" * 16  # 64 hex chars, no label: a key
        self.assertEqual(wab.redact(key), "[скрыто]")
        self.assertEqual(wab.redact("key " + key), "key [скрыто]")
        self.assertEqual(wab.redact("40 " + "c" * 40), "40 [скрыто]")
        sha = "3c71c34b" + "0" * 32
        for text in (f"commit {sha}", f"SHA={sha}", f"head: {sha}", f"HEAD {sha}", f"reviewed_head={sha}",
                     f"base {sha}", f"packet_hash: {sha}", f"sha256:{key}", f"Commit {sha}"):
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text), text)
        self.assertEqual(wab.redact("short 3c71c34 and 3c71c34b0a12"), "short 3c71c34 and 3c71c34b0a12")

    SECRETS = [
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEabcdef\n-----END RSA PRIVATE KEY-----",
        "sk-abcdefghijklmnop1234",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1",
        "xoxb-1234567890-abcdef",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sflKxwRJSMeKKF2QT4fwpM",
        "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-Ts",
        "password=hunter2hunter2",
        'token: "quoted secret value"',
        "Authorization: Bearer abcdefghijklmnop",
        "https://user:pa55w0rd@example.com/x",
        "ivan.petrov@example.com",
        "+7 (916) 123-45-67",
        "QWxhZGRpbjpvcGVuIHNlc2FtZVF1aWNrQnJvd25Gb3hKdW1wcw",
    ]
    ORIGINALS = [
        "MIIEabcdef", "sk-abcdefghijklmnop1234", "a1B2c3D4e5F6g7H8i9J0k1L2", "A1b2C3d4E5f6G7h8I9j0K1",
        "1234567890-abcdef", "ABCDEFGHIJKLMNOP", "sflKxwRJSMeKKF2QT4fwpM", "AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-Ts",
        "hunter2hunter2", "quoted secret value", "abcdefghijklmnop", "pa55w0rd", "ivan.petrov",
        "916", "QWxhZGRpbjpvcGVuIHNlc2FtZVF1aWNrQnJvd25Gb3hKdW1wcw",
    ]

    def test_every_class_is_scrubbed(self):
        for secret, original in zip(self.SECRETS, self.ORIGINALS):
            with self.subTest(secret=secret[:20]):
                out = wab.redact(f"before {secret} after", 1200)
                self.assertNotIn(original, out)
                self.assertIn("[скрыто]", out)

    EXTRA = [
        ('{"api_key": "AbCdEf123456"}', "AbCdEf123456"),
        ('{"token":"Zz9Yy8Xx7Ww6"}', "Zz9Yy8Xx7Ww6"),
        ("{'secret': 'hush-hush-42'}", "hush-hush-42"),
        ("client_secret=Qq1Ww2Ee3Rr", "Qq1Ww2Ee3Rr"),
        ("access_token=Aa1Ss2Dd3Ff", "Aa1Ss2Dd3Ff"),
        ('"refresh_token": "Rr4Tt5Yy6Uu"', "Rr4Tt5Yy6Uu"),
        ("private_key: Pk7Pk8Pk9Pk0", "Pk7Pk8Pk9Pk0"),
        ("x-api-key: Xk1Xk2Xk3Xk4", "Xk1Xk2Xk3Xk4"),
        ("Cookie: sid=abc123; theme=dark; uid=77", "abc123"),
        ("Set-Cookie: session=Ss9Ss8; Path=/; HttpOnly", "Ss9Ss8"),
        ("https://x.blob.core.windows.net/c?sv=2020&sig=Zm9vYmFy%2Bq1&se=2030", "Zm9vYmFy%2Bq1"),
        ("звони 8 916 123 45 67", "123 45 67"),
        ("тел 8(916)1234567", "1234567"),
        ("phone 916-123-45-67 ok", "123-45-67"),
        ("тел +79161234567", "79161234567"),
        ("session_id=Se55Se55Se55", "Se55Se55Se55"),
        ("credential=Cr3dCr3d", "Cr3dCr3d"),
        ("https://SECRETX9@host/p", "SECRETX9"),
        ("https://:SECRETX9@host/p", "SECRETX9"),
    ]

    def test_no_stray_bracket_after_a_cookie(self):
        self.assertEqual(wab.redact("Cookie: session=X1"), "Cookie: [скрыто]")
        self.assertEqual(wab.redact("Set-Cookie: a=b; Path=/"), "Set-Cookie: [скрыто]")

    def test_extra_classes_are_scrubbed(self):
        for text, original in self.EXTRA:
            with self.subTest(text=text):
                self.assertNotIn(original, wab.redact(text, 1200))

    def test_ordinary_text_is_kept(self):
        keep = ["commit 3f2a9c1 and sha 3f2a9c1d4e5b6a7988776655443322110099aabb", "see PR #12 and #345",
                "date 2026-10-01 at 12:34:56", "wave W4 ctx 300000 tokens", "tokenizer notes; session closed",
                "version 3.4.1 build 20261001"]
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text, 1200), text)

    def test_limits(self):
        long = "слово " * 400
        self.assertLessEqual(len(wab.redact(long)), 600)
        self.assertLessEqual(len(wab.redact(long, 1200)), 1200)
        for body in (("x" * 39 + " ") * 200, "я" * 5000, "ab " * 2000, "слово " * 400):
            for limit in (120, 600, 1200):
                with self.subTest(body=body[:6], limit=limit):
                    out = wab.redact(body, limit)
                    self.assertLessEqual(len(out), limit)
                    self.assertTrue(out.endswith("…"))
        self.assertEqual(wab.redact("short"), "short")

    def test_notify_redacts_and_caps_the_whole_message(self):
        cfg, _ = self.chain()
        wab.notify(cfg, "leak password=hunter2hunter2 " + "я" * 3000)
        self.assertEqual(len(self.tg), 1)
        self.assertNotIn("hunter2hunter2", self.tg[0])
        self.assertLessEqual(len(self.tg[0]), 1200)

    def test_quoted_wave_text_is_capped_at_600(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("ж" * 5000, encoding="utf-8")
        with mock.patch.object(wab, "launch"):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue(self.tg)
        self.assertLessEqual(self.tg[0].count("ж"), 600)

    def test_telegram_without_config_is_skipped_with_an_event(self):
        for value in (None, False, {}):
            with self.subTest(telegram=value):
                cfg, _ = self.chain(telegram=value)
                if value is None:
                    cfg.pop("telegram", None)
                wab.notify(cfg, "hello world")
                log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
                self.assertIn("notify(skipped)", log)
        self.assertEqual(self.tg, [])

    def test_transport_reads_secrets_from_the_configured_vault_only(self):
        cfg, _ = self.chain()
        seen = []

        seen_kw = []

        def fake(*args, **kw):
            seen.append(args)
            seen_kw.append(kw)
            return subprocess.CompletedProcess(args, 0, "value\n", "")

        orig = load_orig("wab_orig")
        with mock.patch.object(orig, "sh", side_effect=fake), \
                mock.patch.object(orig.urllib.request, "urlopen") as urlopen:
            orig._send_telegram(cfg, "hi")
        flat = [" ".join(a) for a in seen]
        self.assertTrue(all("kv-test" in f for f in flat), flat)
        self.assertTrue(any("tok" in f for f in flat) and any("chat" in f for f in flat))
        urlopen.assert_called_once()
        self.assertTrue(all(0 < kw.get("timeout", 0) <= 60 for kw in seen_kw), seen_kw)
        source = (WAVES / "wab.py").read_text(encoding="utf-8")
        self.assertNotIn("kv-bronxtc", source)
        self.assertNotIn("curl", source)


# ---------------------------------------------------------------- A5
class Admission(Base):
    def make_root(self, extra=None, name="cc-admission-abcd1234"):
        root = self.tmp / name
        (root / "empty-template").mkdir(parents=True)
        (root / "checkout").mkdir()
        subprocess.run(["git", "init", "-q", str(root / "checkout")], check=True)
        if extra:
            (root / extra).write_text("x", encoding="utf-8")
        return root

    def test_relative_workdir_is_absolute_everywhere(self):
        root = self.make_root()
        cfg, path = self.chain(workdir="../cc-admission-abcd1234/checkout")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        old = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, old)
        cfg = wab.load_chain(path)
        self.assertTrue(os.path.isabs(cfg["workdir"]))
        self.assertEqual(cfg["workdir"], str((root / "checkout").resolve()))
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        self.assertTrue(wab.launch(cfg, "W1", prompt))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["cwd"], cfg["workdir"])
        self.transcript(w["sessions"][0], [asst(inp=1234)], cwd=w["cwd"])
        self.assertEqual(wab.context_tokens(w), 1234)

    def host_policy(self):
        helper = self.home / ".claude" / "bin" / "cc-autonomy.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("# host policy present\n", encoding="utf-8")

    def test_unmanaged_host_takes_any_isolated_worktree_and_says_so(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        cfg, _ = self.chain(workdir=str(plain))
        self.assertEqual(wab.prepare_clone(cfg), str(plain.resolve()))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("unmanaged host", log)

    def test_unmanaged_host_needs_a_workdir_and_a_git_checkout(self):
        cfg, _ = self.chain()
        with self.assertRaises(SystemExit) as ctx:
            wab.prepare_clone(cfg)
        self.assertIn("workdir", str(ctx.exception))
        self.assertIn("isolated git worktree", str(ctx.exception))
        cfg, _ = self.chain(workdir=str(self.tmp / "clone"))  # a directory, not a git checkout
        with self.assertRaises(SystemExit):
            wab.prepare_clone(cfg)

    def test_host_policy_takes_only_the_clone_of_prepare(self):
        self.host_policy()
        root = self.make_root()  # the old signature means nothing to this runtime any more
        cfg, _ = self.chain(workdir=str(root / "checkout"))
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg)
        self.assertIn("cc-autonomy prepare", str(cm.exception))
        cfg, _ = self.chain()
        out = json.dumps({"ok": True, "admitted_path": "/x/y"})
        with mock.patch.object(wab, "sh", return_value=subprocess.CompletedProcess([], 0, out, "")) as m:
            self.assertEqual(wab.prepare_clone(cfg), "/x/y")
        self.assertIn("prepare", m.call_args[0])
        self.assertIn("host policy", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_launch_refuses_an_unadmitted_workdir(self):
        self.host_policy()
        plain = self.tmp / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        cfg, _ = self.chain(workdir=str(plain))
        self.alive = False
        with self.assertRaises(SystemExit):
            wab.launch(cfg, "W1", self.tmp / "p.md")
        self.assertFalse([c for c in self.tmux_calls if c[1] == "new-session"])


# ---------------------------------------------------------------- A7, A8
class CorruptState(Base):
    def test_status_and_current_tmux_report_a_clean_error(self):
        cfg, path = self.chain()
        wab.state_path(cfg).write_text("{broken", encoding="utf-8")
        for cmd in ("status", "current-tmux"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(SystemExit) as ctx:
                    wab.main(["wab.py", cmd, str(path)])
                self.assertIn("wab: cannot read state.json", str(ctx.exception))

    def test_malformed_wave_records_are_a_clean_error(self):
        cfg, path = self.chain()
        for doc in ({"waves": {"W1": "bad"}}, {"waves": []}, {"waves": {"W1": [1]}},
                    {"current": 5, "waves": {}}):
            wab.state_path(cfg).write_text(json.dumps(doc), encoding="utf-8")
            for cmd in ("status", "current-tmux"):
                with self.subTest(doc=doc, cmd=cmd):
                    with self.assertRaises(SystemExit) as ctx:
                        wab.main(["wab.py", cmd, str(path)])
                    self.assertIn("wab: cannot read state.json", str(ctx.exception))

    def test_watch_logs_an_event_and_stops(self):
        cfg, path = self.chain()
        wab.state_path(cfg).write_text("{broken", encoding="utf-8")
        with mock.patch.object(wab, "drop_stale_btab"):
            with self.assertRaises(SystemExit) as ctx:
                wab.watch(cfg, path, max_ticks=1)
        self.assertIn("cannot read state.json", str(ctx.exception))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("cannot read state.json", log)


class CheckpointTransition(Base):
    def setUp(self):
        super().setUp()
        self.cfg, self.path = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(),
                          sessions=["s1"])
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(self.cfg, "W1", "HANDOFF_READY")
        self.rec_cmd = wab.send_command.side_effect
        self.rec_text = wab.send_text.side_effect

    def boom_cmd(self, *a, **kw):
        raise RuntimeError("dispatcher died")

    def heal(self):
        wab.send_command.side_effect = self.rec_cmd
        wab.send_text.side_effect = self.rec_text

    def kinds(self):
        clears = [s for s in self.sent if s[:3] == ("cmd", "wv-w1", "/clear")]
        updates = [s for s in self.sent if s[0] == "text" and s[2].startswith("/superarmanda --wave W1 --resume")]
        return len(clears), len(updates)

    def w(self):
        return self.get_state(self.cfg)["waves"]["W1"]

    def test_crash_while_clearing_repeats_clear_once_and_finishes(self):
        wab.send_command.side_effect = self.boom_cmd
        with self.assertRaises(RuntimeError):
            wab.tick(self.cfg, wab.load_state(self.cfg))
        self.assertEqual((self.w()["phase"], self.w()["restarts"]), ("clearing", 0))
        self.assertEqual(self.kinds(), (0, 0))
        self.heal()
        wab.tick(self.cfg, wab.load_state(self.cfg))
        self.assertEqual(self.kinds(), (1, 1))
        w = self.w()
        self.assertEqual((w["phase"], w["restarts"], w["await_session"]), ("running", 1, True))
        wab.tick(self.cfg, wab.load_state(self.cfg))
        self.assertEqual((self.kinds(), self.w()["restarts"]), ((1, 1), 1))

    def test_crash_while_updating_is_not_resent_and_notifies_once(self):
        def boom_text(name, text, **kw):
            raise RuntimeError("dispatcher died")
        wab.send_text.side_effect = boom_text
        with self.assertRaises(RuntimeError):
            wab.tick(self.cfg, wab.load_state(self.cfg))
        w = self.w()
        self.assertEqual((w["phase"], w["restarts"], w["await_session"]), ("updating", 0, True))
        self.assertEqual(self.kinds(), (1, 0))
        self.heal()
        for _ in range(2):
            with mock.patch.object(wab, "drop_stale_btab"):
                wab.watch(self.cfg, self.path, max_ticks=1)
        w = self.w()
        self.assertEqual((w["phase"], w["restarts"], w["await_session"]), ("running", 1, True))
        self.assertEqual(self.kinds(), (1, 0))  # nothing resent
        self.assertEqual(len(self.tg), 1)

    def test_updating_recovered_by_tick_alone(self):
        w = self.w()
        w.update(phase="updating", await_session=True)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}})
        wab.tick(self.cfg, wab.load_state(self.cfg))
        wab.tick(self.cfg, wab.load_state(self.cfg))
        self.assertEqual((self.kinds(), self.w()["restarts"], self.w()["phase"]), ((0, 0), 1, "running"))
        self.assertEqual(len(self.tg), 1)

    def test_clearing_is_recovered_by_resume_too(self):
        w = self.w()
        w.update(phase="clearing")
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}})
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(self.cfg, self.path, max_ticks=1)
        self.assertEqual((self.kinds(), self.w()["restarts"]), ((1, 1), 1))


class WaveOwnsStatus(Base):
    def setUp(self):
        super().setUp()
        self.cfg, self.path = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(),
                          sessions=["s1"])
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(self.cfg, "W1", "HANDOFF_READY")
        (wab.wave_dir(self.cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        rec = wab.send_text.side_effect

        def delivered_then_died(name, text, **kw):
            rec(name, text)
            raise RuntimeError("died after delivery")
        wab.send_text.side_effect = delivered_then_died
        with self.assertRaises(RuntimeError):
            wab.tick(self.cfg, wab.load_state(self.cfg))
        wab.send_text.side_effect = rec
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["phase"], "updating")

    def status(self):
        return (wab.wave_dir(self.cfg, "W1") / "status").read_text(encoding="utf-8").strip()

    def restart(self, **kw):
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "launch", return_value=True) as ln:
            wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        return ln

    def test_done_after_a_crashed_transition_is_done(self):
        cfg, _ = self.auto_chain()  # same chain.json, now with the gate: the PR is merged already
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(self.cfg, "W1", "DONE")
        ln = self.restart()
        self.assertEqual(self.status(), "DONE")
        ln.assert_called_once()
        self.assertEqual(ln.call_args[0][1], "W2")

    def test_done_with_external_gate_waits_for_merge(self):
        cfg, path = self.chain(merge_gate="external")
        self.set_status(self.cfg, "W1", "DONE")
        ln = self.restart()  # reloads chain.json: merge_gate external
        self.assertEqual(self.status(), "DONE")
        ln.assert_not_called()
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["phase"], "awaiting_merge")

    def test_blocked_after_a_crashed_transition_notifies_once(self):
        self.set_status(self.cfg, "W1", "BLOCKED: need you")
        self.restart()
        self.assertEqual(self.status(), "BLOCKED: need you")
        self.assertEqual(len(self.tg), 1)
        self.assertIn("need you", self.tg[0])

    def test_dispatcher_never_writes_status_during_the_transition(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual((wab.wave_dir(cfg, "W1") / "status").read_text(encoding="utf-8").strip(),
                         "HANDOFF_READY")

    def test_stale_handoff_ready_does_not_start_a_second_transition(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        self.sent.clear()  # setUp already ran one crashed transition
        clears = lambda: len([x for x in self.sent if x[2] == "/clear"])  # noqa: E731
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(clears(), 1)
        self.transcript("s2", [asst(inp=400000)], marker=f"[wab:{CHAIN}/{RUN_ID}/W1]")
        wab.tick(cfg, wab.load_state(cfg))   # adopts s2, context is high: checkpoint requested
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "checkpoint")
        wab.tick(cfg, wab.load_state(cfg))   # the status file is still the OLD handoff: ignored
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(clears(), 1)
        sp = wab.wave_dir(cfg, "W1") / "status"
        future = time.time() + 10
        sp.write_text("HANDOFF_READY\n", encoding="utf-8")  # the wave writes a fresh handoff
        os.utime(sp, (future, future))
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(clears(), 2)


class LaunchIntent(Base):
    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("task\n", encoding="utf-8")
        return p

    def new_sessions(self):
        return [c for c in self.tmux_calls if c[1] == "new-session"]

    def wrap_sh(self, before=False):
        orig = wab.sh.side_effect

        def crashing(*args, **kw):
            if args and args[0] == "tmux" and args[1] == "new-session":
                if before:
                    raise KeyboardInterrupt
                orig(*args, **kw)
                raise KeyboardInterrupt
            return orig(*args, **kw)
        wab.sh.side_effect = crashing
        return orig

    def test_crash_right_after_new_session_does_not_start_a_second_one(self):
        cfg, path = self.chain()
        self.alive = False
        orig = self.wrap_sh()
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(KeyboardInterrupt):
                wab.launch(cfg, "W1", self.prompt())
        wab.sh.side_effect = orig
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "launching")
        self.assertEqual(self.get_state(cfg)["current"], "W1")
        self.alive = True  # the tmux session exists now
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(len(self.new_sessions()), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")
        self.assertEqual(len([s for s in self.sent if s[0] == "text"]), 1)

    def test_crash_before_new_session_starts_exactly_one_with_the_same_id(self):
        cfg, path = self.chain()
        self.alive = False
        orig = self.wrap_sh(before=True)
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(KeyboardInterrupt):
                wab.launch(cfg, "W1", self.prompt())
        wab.sh.side_effect = orig
        self.assertEqual(self.new_sessions(), [])
        inner = wab.sh.side_effect

        def window_appears(*args, **kw):
            r = inner(*args, **kw)
            if args and args[0] == "tmux" and args[1] == "new-session":
                self.alive = True
            return r
        wab.sh.side_effect = window_appears
        sid = self.get_state(cfg)["waves"]["W1"]["sessions"][0]
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "launching")
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=1)
        started = self.new_sessions()
        self.assertEqual(len(started), 1)
        self.assertEqual(list(started[0])[list(started[0]).index("--session-id") + 1], sid)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

    def test_previous_wave_is_saved_done_before_the_next_launch(self):
        cfg, _ = self.auto_chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        seen = {}

        def fake_launch(c, wave, prompt, **kw):
            seen["phase"] = json.loads(wab.state_path(c).read_text(encoding="utf-8"))["waves"]["W1"]["phase"]
            return True
        with mock.patch.object(wab, "launch", side_effect=fake_launch):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(seen["phase"], "done")


class DispatcherLock(Base):
    def hold_elsewhere(self, cfg):
        lock = cfg["run_dir"] / "dispatcher.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
             "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)", str(lock)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)  # cleanups run last-in first-out: kill, then wait
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return lock

    def test_second_watch_of_the_same_run_is_refused(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        lock = self.hold_elsewhere(cfg)
        with mock.patch.object(wab, "drop_stale_btab") as bind:
            with self.assertRaises(SystemExit) as ctx:
                wab.watch(cfg, path, max_ticks=1)
        self.assertIn("another dispatcher holds", str(ctx.exception))
        self.assertIn(str(lock), str(ctx.exception))
        bind.assert_not_called()

    def test_cli_launch_is_refused_while_a_dispatcher_holds_the_lock(self):
        cfg, path = self.chain()
        self.hold_elsewhere(cfg)
        self.alive = False
        with self.assertRaises(SystemExit) as ctx:
            wab.launch(cfg, "W1", self.tmp / "p.md")
        self.assertIn("another dispatcher holds", str(ctx.exception))
        self.assertEqual(self.tmux_calls, [])

    def test_lock_is_released_after_watch_and_launch(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=1)
            wab.watch(cfg, path, max_ticks=1)  # would be refused if the lock leaked
        import fcntl
        with open(cfg["run_dir"] / "dispatcher.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_launch_inside_watch_does_not_retake_the_lock(self):
        cfg, path = self.chain()
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="done")}})

        def tick_that_launches(c, st):
            wab.launch(c, "W2", prompt)
            return False
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                mock.patch.object(wab, "tick", side_effect=tick_that_launches):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), 1)

    def test_save_state_uses_a_unique_temp_file_and_leaves_none(self):
        cfg, _ = self.chain()
        real = tempfile_mkstemp = __import__("tempfile").mkstemp
        with mock.patch("tempfile.mkstemp", wraps=real) as mk:
            wab.save_state(cfg, {"waves": {}})
            wab.save_state(cfg, {"waves": {}, "current": None})
        self.assertEqual(mk.call_count, 2)
        self.assertEqual([p.name for p in cfg["run_dir"].iterdir() if p.name.endswith(".tmp")], [])
        (cfg["run_dir"] / "state.tmp").mkdir()  # the old shared name must not matter
        wab.save_state(cfg, {"waves": {}})
        self.assertEqual(self.get_state(cfg), {"waves": {}})


class WatchIdentity(Base):
    def setUp(self):
        super().setUp()
        self.cfg, self.path = self.chain()
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(self.cfg, "W1", "RUNNING")

    def rewrite(self, **over):
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc.update(over)
        self.path.write_text(json.dumps(doc), encoding="utf-8")

    def run_watch(self, on_first_tick, ticks=3):
        seen = []

        def fake_tick(cfg, st):
            seen.append((str(cfg["run_dir"]), cfg["ctx_limit"]))
            if len(seen) == 1:
                on_first_tick()
            return True
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "tick", side_effect=fake_tick):
            try:
                wab.watch(self.cfg, self.path, max_ticks=ticks)
                exit_ = None
            except SystemExit as e:
                exit_ = str(e)
        return seen, exit_

    def test_changed_run_id_stops_the_watch_before_any_tick_on_the_new_run(self):
        seen, exit_ = self.run_watch(lambda: self.rewrite(run_id="2099-01-01"))
        self.assertEqual(len(seen), 1)
        self.assertIn("identity changed", exit_)
        log = (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("chain.json identity changed", log)
        self.assertIn("keeping", log)
        self.assertFalse((self.path.parent / "runs" / CHAIN / "2099-01-01" / "state.json").exists())
        import fcntl  # the lock is released on the way out
        with open(self.cfg["run_dir"] / "dispatcher.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_other_identity_fields_also_stop_it(self):
        for field, value in (("tmux_prefix", "zz-"), ("repo", "x/y"), ("waves", ["W1", "W9"]),
                             ("merge_gate", "external"), ("workdir", "/tmp/other")):
            with self.subTest(field=field):
                self.cfg, self.path = self.chain()
                seen, exit_ = self.run_watch(lambda: self.rewrite(**{field: value}))
                self.assertEqual(len(seen), 1)
                self.assertIn("identity changed", exit_)

    def test_tunable_fields_apply_live(self):
        seen, exit_ = self.run_watch(lambda: self.rewrite(ctx_limit=111, idle_minutes=1, titles={"W1": "x"},
                                                           model="m", tick_seconds=5), ticks=2)
        self.assertIsNone(exit_)
        self.assertEqual([c for _, c in seen], [300000, 111])

    def test_broken_chain_json_keeps_the_old_config(self):
        seen, exit_ = self.run_watch(lambda: self.path.write_text("{broken", encoding="utf-8"), ticks=3)
        self.assertIsNone(exit_)
        self.assertEqual(len(seen), 3)
        self.assertIn("cannot read", (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))


class CoordinatorHandoff(Base):
    def lock_is_free(self, cfg):
        import fcntl
        with open(cfg["run_dir"] / "dispatcher.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_external_gate_ends_the_watch_and_the_next_wave_continues_by_cli(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        nxt = wab.wave_dir(cfg, "W1") / "next-prompt.md"
        nxt.write_text("second wave\n", encoding="utf-8")
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=50)  # returns on its own: no endless loop
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("handed to the coordinator", log)
        self.assertIn(f"wab.py launch {path.resolve()} W2 {nxt}", log)
        self.assertIn(f"wab.py watch {path.resolve()}", log)
        self.lock_is_free(cfg)
        # the coordinator merged: CLI launch of the next wave
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W2", nxt))
        st = self.get_state(cfg)
        self.assertEqual((st["current"], st["waves"]["W1"]["phase"], st["waves"]["W2"]["phase"]),
                         ("W2", "done", "running"))
        self.lock_is_free(cfg)
        news = len([c for c in self.tmux_calls if c[1] == "new-session"])
        self.alive = True
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "launch") as again:
            wab.watch(cfg, path, max_ticks=1)
        again.assert_not_called()
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), news)
        self.assertEqual(self.get_state(cfg)["waves"]["W2"]["phase"], "running")

    def test_resume_in_awaiting_merge_exits_with_the_same_hint(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.set_status(cfg, "W1", "DONE")
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=50)
        self.assertIn("handed to the coordinator", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        self.lock_is_free(cfg)

    def test_handoff_command_is_runnable(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        wab._stop_event(cfg, wab.load_state(cfg), path)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        m = re.search(r"run: python3 (\S+) launch ", log)
        self.assertIsNotNone(m, log)
        self.assertTrue(Path(shlex.split(m.group(1))[0]).is_absolute())
        self.assertTrue(Path(shlex.split(m.group(1))[0]).exists())
        self.assertIn("&& python3 ", log)
        self.assertNotRegex(log, r"run: wab\.py|&& wab\.py")

    def test_last_wave_ends_the_watch_in_both_modes(self):
        for gate in (None, "external"):
            with self.subTest(gate=gate):
                cfg, path = self.chain(waves=["W1"], merge_gate=gate)
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                with mock.patch.object(wab, "drop_stale_btab"):
                    wab.watch(cfg, path, max_ticks=50)
                self.lock_is_free(cfg)


class LaunchGuard(Base):
    def setUp(self):
        super().setUp()
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("go\n", encoding="utf-8")
        self.alive = False

    def attempt(self, cfg, wave):
        self.tmux_calls.clear()
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            return wab.launch(cfg, wave, self.prompt)

    def refused(self, cfg, wave, text="launch refused"):
        with self.assertRaises(SystemExit) as ctx:
            self.attempt(cfg, wave)
        self.assertIn(text, str(ctx.exception))
        self.assertEqual(self.tmux_calls, [])
        return str(ctx.exception)

    def state(self, cfg, current, **phases):
        self.put_state(cfg, {"current": current,
                             "waves": {w: self.wave_rec(w, phase=p) for w, p in phases.items()}})

    def test_busy_current_wave_refuses_everything(self):
        cfg, _ = self.chain(waves=["W1", "W2", "W3"])
        for phase in ("running", "starting", "launching", "sending", "checkpoint", "clearing", "updating"):
            with self.subTest(phase=phase):
                self.state(cfg, "W1", W1=phase)
                msg = self.refused(cfg, "W2")
                self.assertIn(f"wave W1 is {phase}", msg)
                self.refused(cfg, "W1")

    def test_order_is_enforced(self):
        cfg, _ = self.chain(waves=["W1", "W2", "W3"])
        self.state(cfg, "W1", W1="done")
        self.refused(cfg, "W3")
        self.refused(cfg, "W1")  # a finished wave is not launched again
        self.put_state(cfg, {"current": None, "waves": {}})
        self.refused(cfg, "W2")
        self.assertTrue(self.attempt(cfg, "W1"))

    def test_finished_chain_refuses_any_launch(self):
        cfg, _ = self.chain()
        self.state(cfg, None, W1="done", W2="done")
        self.refused(cfg, "W1")
        self.refused(cfg, "W2")

    def test_next_wave_after_done_or_awaiting_merge_is_allowed(self):
        for phase in ("done", "awaiting_merge"):
            with self.subTest(phase=phase):
                cfg, _ = self.chain(waves=["W1", "W2", "W3"])
                self.state(cfg, "W1", W1=phase)
                self.refused(cfg, "W3")
                self.assertTrue(self.attempt(cfg, "W2"))
                self.assertEqual(self.get_state(cfg)["current"], "W2")

    def test_dead_or_not_ready_wave_restarts_itself_only_when_its_window_is_gone(self):
        for phase in ("dead", "not_ready"):
            with self.subTest(phase=phase):
                cfg, _ = self.chain(waves=["W1", "W2", "W3"])
                self.state(cfg, "W1", W1=phase)
                self.refused(cfg, "W2")
                self.alive = True
                self.refused(cfg, "W1")
                self.alive = False
                self.assertTrue(self.attempt(cfg, "W1"))
                w = self.get_state(cfg)["waves"]["W1"]
                self.assertEqual(len(w["attempts"]), 1)
                self.assertEqual(w["attempts"][0]["phase"], phase)

    def test_failed_next_launch_is_reported_with_its_reason(self):
        cfg, path = self.auto_chain()
        self.alive = False
        self.ready = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            wab.watch(cfg, path, max_ticks=3)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("next wave W2 not started: TUI not ready", log)
        self.assertNotIn("no current wave", log)


class Episodes(Base):
    def setUp(self):
        super().setUp()
        self.cfg, _ = self.chain()
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})

    def tick(self):
        self.assertTrue(wab.tick(self.cfg, wab.load_state(self.cfg)))

    def test_blocked_episodes(self):
        self.set_status(self.cfg, "W1", "BLOCKED: same question")
        self.tick()
        self.tick()
        self.assertEqual(len(self.tg), 1)
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.assertNotIn("blocked", self.get_state(self.cfg)["waves"]["W1"]["notified"])
        self.set_status(self.cfg, "W1", "BLOCKED: same question")
        self.tick()
        self.tick()
        self.assertEqual(len(self.tg), 2)

    def test_permission_episodes(self):
        self.set_status(self.cfg, "W1", "RUNNING")
        prompt = "Do you want to proceed?\n❯ 1. Yes"
        self.pane = prompt
        self.tick()
        self.tick()
        self.assertEqual(len(self.tg), 1)
        self.pane = "working..."
        self.tick()
        self.pane = prompt
        self.tick()
        self.tick()
        self.assertEqual(len(self.tg), 2)

    def test_idle_episodes(self):
        self.cfg, _ = self.chain(idle_minutes=0)
        self.set_status(self.cfg, "W1", "RUNNING")
        t = [1000.0]

        def screen(body, footer):
            return f"{body}\nline2\nline3\n{footer} one\n{footer} two\n"
        with mock.patch("time.time", side_effect=lambda: t[0]):
            self.pane = screen("same screen", "f0")
            self.tick()
            t[0] += 5
            self.tick()
            self.tick()
            self.assertEqual(len(self.tg), 1)
            self.pane = screen("screen moved", "f0")
            self.tick()
            t[0] += 5
            self.pane = screen("same screen", "f0")  # a body seen before: a new episode
            self.tick()
            t[0] += 5
            self.tick()
        self.assertEqual(len(self.tg), 2)

    def test_a_ticking_footer_does_not_hide_idleness(self):
        self.cfg, _ = self.chain(idle_minutes=0)
        self.set_status(self.cfg, "W1", "RUNNING")
        t = [1000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for i in range(4):
                self.pane = f"work\nmore work\nthe end\n* Thinking... ({i}s)\n  esc to interrupt {i}\n"
                self.tick()
                t[0] += 5
        self.assertEqual(len(self.tg), 1)
        self.assertIn("молчит", self.tg[0])

    def test_working_subagent_is_not_idleness(self):
        # Live false alarms (mh-creators run01, 2026-10-03, three in one day): the wave waited for
        # its background coder/tester subagent, the screen stood still for 12+ min, the owner got
        # «волна молчит, возможно ждёт тебя». The subagent transcript was being written all along.
        self.cfg, _ = self.chain(idle_minutes=1)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(sessions=["s1"])}})
        self.set_status(self.cfg, "W1", "RUNNING")
        self.transcript("s1", [asst(inp=5)])
        sub = self.home / ".claude" / "projects" / sanitize(self.cwd) / "s1" / "subagents"
        sub.mkdir(parents=True)
        agent = sub / "agent-a1.jsonl"
        agent.write_text(asst(inp=1) + "\n", encoding="utf-8")
        t = [5_000_000.0]
        main = wab.transcript_path(self.cwd, "s1")
        with mock.patch("time.time", side_effect=lambda: t[0]):
            os.utime(main, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            for _ in range(3):  # 3 x 5 min of a still screen while the subagent writes
                t[0] += 300
                os.utime(agent, (t[0] - 20, t[0] - 20))
                self.tick()
            self.assertEqual(self.tg, [])
            t[0] += 300  # the subagent stopped writing too: real silence is still reported
            self.tick()
        self.assertEqual(len(self.tg), 1)
        self.assertIn("молчит", self.tg[0])

    def _subagent_wave(self):
        self.cfg, _ = self.chain(idle_minutes=1)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(sessions=["s1"])}})
        self.set_status(self.cfg, "W1", "RUNNING")
        self.transcript("s1", [asst(inp=5)])
        sub = self.home / ".claude" / "projects" / sanitize(self.cwd) / "s1" / "subagents"
        sub.mkdir(parents=True)
        agent = sub / "agent-a1.jsonl"
        agent.write_text(asst(inp=1) + "\n", encoding="utf-8")
        return wab.transcript_path(self.cwd, "s1"), agent

    def test_subagent_write_after_an_idle_notice_ends_the_episode(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for f in (main, agent):
                os.utime(f, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            t[0] += 300
            self.tick()
            self.assertEqual(len(self.tg), 1)  # real silence: reported
            os.utime(agent, (t[0] - 5, t[0] - 5))  # the subagent works again, the screen is still
            t[0] += 30
            self.tick()
            self.assertNotIn("idle", self.get_state(self.cfg)["waves"]["W1"].get("notified", {}))
            t[0] += 300  # and stops: the new silence is reported again, the old mark does not mute it
            self.tick()
        self.assertEqual(len(self.tg), 2)

    def test_future_dated_transcript_does_not_mute_silence(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            os.utime(main, (t[0] - 3600, t[0] - 3600))
            os.utime(agent, (t[0] + 36000, t[0] + 36000))  # a skewed clock: 10 h ahead, never rewritten
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            t[0] += 300
            self.tick()
        self.assertEqual(len(self.tg), 1)
        self.assertIn("молчит", self.tg[0])

    def test_a_future_dated_file_does_not_hide_writes_to_another(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            os.utime(main, (t[0] + 36000, t[0] + 36000))  # skewed and never rewritten
            os.utime(agent, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            for _ in range(3):  # the subagent writes; the max over files stays the future one
                t[0] += 50
                os.utime(agent, (t[0] - 5, t[0] - 5))
                self.tick()
            self.assertEqual(self.tg, [])
            t[0] += 300
            self.tick()
        self.assertEqual(len(self.tg), 1)

    def test_subagent_write_ends_the_idle_episode_on_an_early_return_path(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for f in (main, agent):
                os.utime(f, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            t[0] += 300
            self.tick()
            self.assertIn("idle", self.get_state(self.cfg)["waves"]["W1"]["notified"])
            self.set_status(self.cfg, "W1", "BLOCKED: a question")  # the tick returns early now
            os.utime(agent, (t[0] - 5, t[0] - 5))
            t[0] += 30
            self.tick()
        self.assertNotIn("idle", self.get_state(self.cfg)["waves"]["W1"].get("notified", {}))

    def test_ended_idle_episode_is_saved_even_when_the_tick_returns_without_saving(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for f in (main, agent):
                os.utime(f, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            t[0] += 300
            self.tick()
            self.assertIn("idle", self.get_state(self.cfg)["waves"]["W1"]["notified"])
            os.utime(agent, (t[0] - 5, t[0] - 5))
            t[0] += 30
            # a policy-answered BLOCKED: the branch returns True without save_state
            self.set_status(self.cfg, "W1", "BLOCKED: [class=question rec=A red=no] q")
            with mock.patch.object(wab, "_policy_answer", return_value=True):
                self.tick()
        self.assertNotIn("idle", self.get_state(self.cfg)["waves"]["W1"].get("notified", {}))

    def test_a_write_with_a_corrected_older_mtime_still_counts_as_activity(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            os.utime(main, (t[0] - 3600, t[0] - 3600))
            os.utime(agent, (t[0] + 36000, t[0] + 36000))  # skewed: clamped to now on the first look
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            t[0] += 50
            self.tick()
            t[0] += 50
            os.utime(agent, (t[0] - 200, t[0] - 200))  # the clock is fixed: a write with an older mtime
            self.tick()
            self.assertEqual(self.tg, [])  # a change seen now is activity now
            t[0] += 300
            self.tick()
        self.assertEqual(len(self.tg), 1)

    def test_upgrade_from_a_record_without_bookkeeping_drops_a_stale_idle_notice(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        self.pane = "static screen\nline2\nline3\n"
        st = self.get_state(self.cfg)
        w = st["waves"]["W1"]
        w["pane_digest"] = wab.pane_digest(self.pane)
        w["pane_changed"] = t[0] - 1000  # 0.13.0 raised «молчит» at pane_changed + 60 s
        wab.once_per(w, "idle", w["pane_digest"])
        wab.put_notice(w, "idle", w["pane_digest"], "wave-autobot: волна W1 молчит 1+ мин")
        self.put_state(self.cfg, st)
        os.utime(main, (t[0] - 3600, t[0] - 3600))
        os.utime(agent, (t[0] - 10, t[0] - 10))  # the subagent wrote after that notice
        with mock.patch("time.time", side_effect=lambda: t[0]), \
                mock.patch.object(wab, "flush_notices"):
            self.tick()
        w = self.get_state(self.cfg)["waves"]["W1"]
        self.assertNotIn("idle", w.get("notified", {}))
        self.assertNotIn("idle", w.get("outbox", {}))

    def test_a_new_session_seeds_its_baseline_instead_of_counting_as_a_write(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for f in (main, agent):
                os.utime(f, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            st = self.get_state(self.cfg)
            st["waves"]["W1"]["sessions"] = ["s1", "s2"]  # bound after /clear
            self.put_state(self.cfg, st)
            p2 = self.transcript("s2", [asst(inp=2)])
            os.utime(p2, (t[0] - 3600, t[0] - 3600))  # the new session is old and quiet
            t[0] += 90
            self.tick()
        self.assertEqual(len(self.tg), 1)
        self.assertIn("молчит", self.tg[0])

    def test_upgrade_with_a_future_dated_file_keeps_a_valid_idle_notice(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        self.pane = "static screen\nline2\nline3\n"
        st = self.get_state(self.cfg)
        w = st["waves"]["W1"]
        w["pane_digest"] = wab.pane_digest(self.pane)
        w["pane_changed"] = t[0] - 1000
        wab.once_per(w, "idle", w["pane_digest"])
        self.put_state(self.cfg, st)
        os.utime(main, (t[0] - 3600, t[0] - 3600))
        os.utime(agent, (t[0] + 36000, t[0] + 36000))  # skewed, no write happened
        with mock.patch("time.time", side_effect=lambda: t[0]):
            self.tick()
        self.assertIn("idle", self.get_state(self.cfg)["waves"]["W1"].get("notified", {}))

    def test_a_session_bound_in_the_same_tick_is_counted_before_the_idle_check(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        with mock.patch("time.time", side_effect=lambda: t[0]):
            for f in (main, agent):
                os.utime(f, (t[0] - 3600, t[0] - 3600))
            self.pane = "static screen\nline2\nline3\n"
            self.tick()
            st = self.get_state(self.cfg)
            st["waves"]["W1"]["await_session"] = True  # after /clear: the new transcript is not bound yet
            self.put_state(self.cfg, st)
            p2 = self.transcript("s2", [asst(inp=2)])
            t[0] += 90
            os.utime(p2, (t[0] - 2, t[0] - 2))  # the new session has just written

            def bind(cfg, st_, wave):
                return "s2"
            with mock.patch.object(wab, "find_new_session", side_effect=bind):
                self.tick()
        self.assertEqual(self.tg, [])
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["sessions"], ["s1", "s2"])

    def test_upgrade_counts_a_real_write_next_to_a_future_dated_file(self):
        main, agent = self._subagent_wave()
        t = [5_000_000.0]
        self.pane = "static screen\nline2\nline3\n"
        st = self.get_state(self.cfg)
        w = st["waves"]["W1"]
        w["pane_digest"] = wab.pane_digest(self.pane)
        w["pane_changed"] = t[0] - 1000
        wab.once_per(w, "idle", w["pane_digest"])
        self.put_state(self.cfg, st)
        os.utime(main, (t[0] + 36000, t[0] + 36000))  # skewed
        os.utime(agent, (t[0] - 10, t[0] - 10))  # a real write after the old notice
        with mock.patch("time.time", side_effect=lambda: t[0]), \
                mock.patch.object(wab, "flush_notices"):
            self.tick()
        self.assertNotIn("idle", self.get_state(self.cfg)["waves"]["W1"].get("notified", {}))

    def test_empty_status_read_changes_nothing(self):
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        for text in ("", "  \n"):
            (wab.wave_dir(self.cfg, "W1") / "status").write_text(text, encoding="utf-8")
            self.tick()
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertEqual(len(self.tg), 1)

    def test_permission_episode_is_keyed_by_the_marker_not_the_screen(self):
        self.set_status(self.cfg, "W1", "RUNNING")
        for i in range(4):
            self.pane = f"Do you want to proceed?\n❯ 1. Yes\ncountdown {i}"
            self.tick()
        self.assertEqual(len(self.tg), 1)
        self.pane = "working"
        self.tick()
        self.pane = "Do you want to proceed?\n❯ 1. Yes\ncountdown 9"
        self.tick()
        self.assertEqual(len(self.tg), 2)


class ExactTargets(Base):
    def setUp(self):
        super().setUp()
        exe = shutil.which("tmux")
        if not exe:
            self.skipTest("tmux is not installed")
        out = subprocess.run([exe, "-V"], capture_output=True, text=True, encoding="utf-8").stdout
        ver = wab.parse_tmux_version(out)
        if ver is None or ver < (3, 2):
            self.skipTest(f"tmux {out.strip()!r} is older than 3.2")
        self.exe = exe
        self.sock = f"wabtest-{os.getpid()}"
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.addCleanup(lambda: subprocess.run([exe, "-L", self.sock, "kill-server"],
                                               capture_output=True, env=self.env))
        self.w = load_orig("wab_targets")  # unpatched copy: real tmux functions
        self.w.TMUX_SOCKET = self.sock

    def tm(self, *args):
        return subprocess.run([self.exe, "-L", self.sock, "-f", "/dev/null", *args], capture_output=True,
                              text=True, encoding="utf-8", env=self.env)

    def test_a_prefix_name_never_matches_another_session(self):
        self.assertEqual(self.tm("new-session", "-d", "-s", "x-w10", "-x", "80", "-y", "24").returncode, 0)
        self.assertTrue(self.w.tmux_alive("x-w10"))
        self.assertFalse(self.w.tmux_alive("x-w1"))
        self.assertFalse(self.w.tmux_alive("x-w"))
        self.assertEqual(self.w.pane_text("x-w1"), "")
        with self.assertRaises(subprocess.CalledProcessError):
            self.w.send_command("x-w1", "echo LEAK")
        time.sleep(0.3)
        self.assertNotIn("LEAK", self.w.pane_text("x-w10"))
        for _ in range(100):  # a slow runner: wait until the shell has printed something
            if self.w.pane_text("x-w10").strip():
                break
            time.sleep(0.1)
        self.w.send_command("x-w10", "echo MARK123")
        for _ in range(100):
            if "MARK123" in self.w.pane_text("x-w10").replace("echo MARK123", ""):
                break
            time.sleep(0.1)
        else:
            self.fail("send_command did not reach the exact session")

    def test_no_bare_session_targets_remain(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        for m in re.finditer(r'"-t",\s*([^,)\n]+)', src):
            self.assertRegex(m.group(1).strip(), r"^(session_target\(|pane_target\(|pane$)", m.group(0))

    def test_cli_launch_failure_exits_nonzero(self):
        cfg, path = self.chain()
        self.alive = False
        self.ready = False
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(SystemExit) as ctx:
                wab.main(["wab.py", "launch", str(path), "W1", str(prompt)])
        self.assertEqual(ctx.exception.code, 3)

    def test_watch_that_ends_in_failure_exits_nonzero(self):
        cfg, path = self.chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        with mock.patch.object(wab, "drop_stale_btab"):
            with self.assertRaises(SystemExit) as ctx:
                wab.main(["wab.py", "watch", str(path)])
        self.assertEqual(ctx.exception.code, 3)


class WindowVanishesMidAction(Base):
    def failing(self, kill_window):
        def boom(*args, **kw):
            if kill_window:
                self.alive = False
            raise subprocess.CalledProcessError(1, ["tmux"], stderr="can't find session")
        return boom

    def w(self, cfg):
        return self.get_state(cfg)["waves"]["W1"]

    def test_checkpoint_request_when_the_window_is_gone(self):
        cfg, _ = self.chain()
        self.transcript("s1", [asst(inp=400000)])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(sessions=["s1"])}})
        self.set_status(cfg, "W1", "RUNNING")
        wab.send_text.side_effect = self.failing(True)
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.w(cfg)["phase"], "dead")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(len(self.tg), 1)
        self.assertIn("закрылось", self.tg[0])

    def test_clear_when_the_window_is_gone_keeps_one_notice(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        wab.send_command.side_effect = self.failing(True)
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.w(cfg)["phase"], "dead")
        self.assertEqual(len(self.tg), 1)

    def test_clear_failing_in_a_live_window_notifies_once_and_retries(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        rec = wab.send_command.side_effect
        wab.send_command.side_effect = self.failing(False)
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual((self.w(cfg)["phase"], len(self.tg)), ("clearing", 1))
        wab.send_command.side_effect = rec
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual((self.w(cfg)["phase"], self.w(cfg)["restarts"]), ("running", 1))

    def test_first_prompt_when_the_window_is_gone(self):
        cfg, _ = self.chain()
        self.alive = False
        wab.send_text.side_effect = self.failing(True)
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertFalse(wab.launch(cfg, "W1", prompt))
        self.assertEqual(self.w(cfg)["phase"], "dead")
        self.assertEqual(len(self.tg), 1)


class TwoStepSend(Base):
    def typed_then_enter_fails(self, kind):
        def fn(name, text, on_typed=None):
            if on_typed:
                on_typed()  # the text is in the window; Enter is what failed
            raise subprocess.CalledProcessError(1, ["tmux", "send-keys"], stderr="boom")
        getattr(wab, kind).side_effect = fn

    def w(self, cfg):
        return self.get_state(cfg)["waves"]["W1"]

    def test_clear_enter_failure_resends_only_enter(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        rec = wab.send_command.side_effect
        self.typed_then_enter_fails("send_command")
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.w(cfg)["pending_enter"], "/clear")
        wab.send_command.side_effect = rec
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual([x for x in self.sent if x[:3] == ("cmd", "wv-w1", "/clear")], [])  # not typed twice
        self.assertEqual((self.w(cfg)["phase"], self.w(cfg)["restarts"]), ("running", 1))
        self.assertNotIn("pending_enter", self.w(cfg))

    def test_checkpoint_request_enter_failure_resends_only_enter(self):
        cfg, _ = self.chain()
        self.transcript("s1", [asst(inp=400000)])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(sessions=["s1"])}})
        self.set_status(cfg, "W1", "RUNNING")
        rec = wab.send_text.side_effect
        self.typed_then_enter_fails("send_text")
        wab.tick(cfg, wab.load_state(cfg))
        self.assertFalse(self.w(cfg)["checkpoint_sent"])
        wab.send_text.side_effect = rec
        wab.tick(cfg, wab.load_state(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual([x for x in self.sent if "WAB-CHECKPOINT" in x[2]], [])
        self.assertTrue(self.w(cfg)["checkpoint_sent"])

    def test_update_failure_in_a_live_window_notifies_once(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")

        def fail(name, text, on_typed=None):
            raise subprocess.CalledProcessError(1, ["tmux"], stderr="boom")  # not even typed
        wab.send_text.side_effect = fail
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.w(cfg)["phase"], "updating")
        wab.tick(cfg, wab.load_state(cfg))  # recover_update: the owner decides, but no second notice
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), 1)

    def test_recover_update_finishes_enter_for_old_and_new_pending_labels(self):
        for label in ("/update", wab.RESUME_WHAT):
            with self.subTest(label=label):
                cfg, _ = self.chain()
                w = self.wave_rec(phase="updating", sessions=["s1"], pending_enter=label,
                                  pending_text_head="x")
                self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
                self.set_status(cfg, "W1", "HANDOFF_READY")
                n = len(self.sent)
                wab.tick(cfg, wab.load_state(cfg))
                self.assertEqual(self.sent[n:], [], self.sent[n:])  # nothing typed again
                self.assertEqual(self.enters[-1:], ["wv-w1"])
                self.assertEqual((self.w(cfg)["phase"], self.w(cfg)["restarts"]), ("running", 1))

    def test_update_enter_failure_is_finished_with_enter_only(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time(), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "HANDOFF_READY")
        rec = wab.send_text.side_effect
        self.typed_then_enter_fails("send_text")
        wab.tick(cfg, wab.load_state(cfg))
        wab.send_text.side_effect = rec
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual([x for x in self.sent if x[2].startswith("/superarmanda --wave")], [])
        self.assertEqual((self.w(cfg)["phase"], self.w(cfg)["restarts"]), ("running", 1))

    def test_watch_exits_nonzero_when_the_next_wave_does_not_reach_running(self):
        cfg, path = self.auto_chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        calls = {"wv-w2": 0}

        def alive(name):
            if name in calls:
                calls[name] += 1
                return calls[name] > 1  # absent for the pre-launch check, alive afterwards
            return False
        wab.tmux_alive.side_effect = alive

        def fail(name, text, on_typed=None):
            raise subprocess.CalledProcessError(1, ["tmux"], stderr="boom")
        wab.send_text.side_effect = fail
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(SystemExit) as ctx:
                wab.main(["wab.py", "watch", str(path)])
        self.assertEqual(ctx.exception.code, 3)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("W2: stopped in phase sending", log)
        self.assertNotIn("no current wave", log)


class TickAdvancesPendingTransitions(Base):
    def w(self, cfg):
        return self.get_state(cfg)["waves"]["W1"]

    def test_failed_enters_in_sending_are_retried_by_tick_without_a_restart(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="sending", pending_enter="first prompt", pending_text_head="задача волны")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "STARTING")
        boom = subprocess.CalledProcessError(1, ["tmux"], stderr="boom")
        wab.press_enter.side_effect = boom
        for _ in range(3):
            self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.w(cfg)["phase"], "sending")
        self.assertEqual(len(self.tg), 1)  # one notice for the whole failure episode
        self.assertEqual(self.enters, [])
        wab.press_enter.side_effect = lambda n: self.enters.append(n)
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual(self.w(cfg)["phase"], "running")
        self.assertNotIn("pending_enter", self.w(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        # round 30: the Enter-only recovery finishes the start like the normal path («стартовала» once)
        self.assertEqual((self.enters, len(self.tg)), (["wv-w1"], 2))
        self.assertIn("стартовала волна W1", self.tg[-1])

    def test_sending_without_pending_enter_is_not_resent_and_goes_on(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="sending")}})
        self.set_status(cfg, "W1", "STARTING")
        wab.tick(cfg, wab.load_state(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(([s for s in self.sent if s[0] == "text"], self.enters), ([], []))
        self.assertEqual((self.w(cfg)["phase"], len(self.tg)), ("running", 1))

    def test_starting_is_completed_by_tick(self):
        cfg, _ = self.chain()
        prompt = self.tmp / "p.md"
        prompt.write_text("the task\n", encoding="utf-8")
        w = self.wave_rec(phase="starting", prompt_file=str(prompt), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "STARTING")
        self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(len([s for s in self.sent if s[0] == "text" and "the task" in s[2]]), 1)
        self.assertEqual(self.w(cfg)["phase"], "running")


class AtMostOnce(Base):
    def crash_then_tick(self, cfg, **extra):
        with mock.patch.object(wab, "notify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wab.tick(cfg, wab.load_state(cfg))
        # a notice not confirmed by the transport is retried once (at least once), never twice
        with mock.patch.object(wab, "notify", return_value=True) as retry, \
                mock.patch.object(wab, "launch", return_value=True):
            try:
                wab.tick(cfg, wab.load_state(cfg))
            except Exception:
                pass
        self.assertLessEqual(len(retry.call_args_list), 1)
        with mock.patch.object(wab, "notify", return_value=True) as again, \
                mock.patch.object(wab, "launch", return_value=True):
            try:
                wab.tick(cfg, wab.load_state(cfg))
            except Exception:
                pass
        return again

    def test_awaiting_merge(self):
        cfg, _ = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        again = self.crash_then_tick(cfg)
        again.assert_not_called()

    def test_done(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        again = self.crash_then_tick(cfg)
        again.assert_not_called()

    def test_blocked_dead_and_permission(self):
        for status, alive, pane in (("BLOCKED: q", True, ""), ("RUNNING", False, ""),
                                    ("RUNNING", True, "Do you want to proceed")):
            with self.subTest(status=status, alive=alive):
                self.alive, self.pane = alive, pane
                cfg, _ = self.chain()
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", status)
                again = self.crash_then_tick(cfg)
                again.assert_not_called()

    def test_launch_start_notice(self):
        cfg, _ = self.chain()
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                mock.patch.object(wab, "notify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wab.launch(cfg, "W1", prompt)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")
        self.assertTrue(self.get_state(cfg)["waves"]["W1"]["notified"].get("started"))
        # «стартовала» goes through the outbox: not confirmed by the transport -> sent once more
        # (at least once), and never again after that
        st = self.get_state(cfg)
        st["waves"]["W1"]["outbox"]["started"]["next_at"] = 0
        self.put_state(cfg, st)
        self.alive = True
        with mock.patch.object(wab, "notify", return_value=True) as again, \
                mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, cfg_path(cfg), max_ticks=1)
            wab.watch(cfg, cfg_path(cfg), max_ticks=1)
        self.assertEqual(len([c for c in again.call_args_list if "стартовала" in c[0][1]]), 1)


def cfg_path(cfg):
    return cfg["chain_file"]


class Resume(Base):
    def run_watch(self, cfg, path, ticks=1):
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "launch") as launch:
            wab.watch(cfg, path, max_ticks=ticks)
        return launch

    def test_running_wave_is_continued_without_launch_or_notice(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        launch = self.run_watch(cfg, path, 2)
        launch.assert_not_called()
        self.assertEqual(self.tg, [])
        self.assertEqual(self.sent, [])

    def test_checkpoint_is_resent_only_when_not_delivered(self):
        cfg, path = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=False, checkpoint_at=time.time())
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "RUNNING")
        self.run_watch(cfg, path)
        self.assertEqual(len([s for s in self.sent if "WAB-CHECKPOINT" in s[2]]), 1)
        self.run_watch(cfg, path)  # restarted again: already delivered
        self.assertEqual(len([s for s in self.sent if "WAB-CHECKPOINT" in s[2]]), 1)
        self.assertTrue(self.get_state(cfg)["waves"]["W1"]["checkpoint_sent"])

    def test_checkpoint_already_sent_is_not_repeated(self):
        cfg, path = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time())
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "RUNNING")
        self.run_watch(cfg, path)
        self.assertEqual(self.sent, [])

    def test_awaiting_merge_stays_quiet(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.set_status(cfg, "W1", "DONE")
        launch = self.run_watch(cfg, path, 2)
        launch.assert_not_called()
        self.assertEqual(self.tg, [])

    def test_starting_waits_for_ready_and_sends_the_task(self):
        cfg, path = self.chain()
        prompt = self.tmp / "p.md"
        prompt.write_text("the task\n", encoding="utf-8")
        w = self.wave_rec(phase="starting", prompt_file=str(prompt), sessions=["s1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        launch = self.run_watch(cfg, path)
        launch.assert_not_called()
        texts = [s for s in self.sent if s[0] == "text"]
        self.assertEqual(len(texts), 1)
        self.assertIn("the task", texts[0][2])
        self.assertIn(f"[wab:{CHAIN}/{RUN_ID}/W1]", texts[0][2])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

    def test_sending_is_not_resent_and_notifies_once(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="sending")}})
        self.set_status(cfg, "W1", "STARTING")
        self.run_watch(cfg, path)
        self.run_watch(cfg, path)
        self.assertEqual([s for s in self.sent if s[0] == "text"], [])
        self.assertEqual(len(self.tg), 1)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("sending", log)

    def test_state_is_saved_before_returning_on_every_branch(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        wab.tick(cfg, wab.load_state(cfg))
        self.assertIn("blocked", self.get_state(cfg)["waves"]["W1"]["notified"])


class AutoGuard(Base):
    OFF = "work\n\n  ⏵ plan mode on (shift+tab to cycle)\n"
    ON = "work\n\n  ⏵⏵ auto mode on (shift+tab to cycle)\n"

    def test_pure_function(self):
        self.assertTrue(wab.auto_mode_off(self.OFF))
        self.assertFalse(wab.auto_mode_off(self.ON))
        self.assertTrue(wab.auto_mode_off("Press to Switch To Auto Mode\nshift+tab to cycle"))
        self.assertIsNone(wab.auto_mode_off("loading…"))  # TUI not ready: no verdict
        old = "auto mode on\n" + "\n".join(f"line {i}" for i in range(10)) + "\nshift+tab to cycle\n"
        self.assertTrue(wab.auto_mode_off(old))  # only the last six non-empty lines count

    def test_two_off_ticks_notify_once_and_return_starts_a_new_episode(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        self.pane = self.OFF
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(self.tg, [])
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), 1)
        wab.tick(cfg, wab.load_state(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), 1)
        self.pane = self.ON
        wab.tick(cfg, wab.load_state(cfg))
        self.pane = self.OFF
        wab.tick(cfg, wab.load_state(cfg))
        wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.tg), 2)

    def test_a_single_off_tick_is_forgiven(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        for pane in (self.OFF, self.ON, self.OFF, self.ON):
            self.pane = pane
            wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(self.tg, [])

    def test_not_watched_outside_the_running_phase(self):
        cfg, _ = self.chain()
        w = self.wave_rec(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time())
        self.put_state(cfg, {"current": "W1", "waves": {"W1": w}})
        self.set_status(cfg, "W1", "RUNNING")
        self.pane = self.OFF
        for _ in range(3):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertEqual(self.tg, [])


class KeysAndRegistry(Base):
    def test_wab_open_uses_the_socket_it_is_given_for_every_tmux_call(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W2", "waves": {"W2": self.wave_rec("W2")}})
        bindir = self.tmp / "fakebin"
        bindir.mkdir()
        log = self.tmp / "tmux.log"
        fake = bindir / "tmux"
        fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexit 0\n', encoding="utf-8")
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "WAB_TMUX_SOCKET"}
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["WAB_TMUX_SOCKET"] = "sockq"
        subprocess.run([str(WAVES / "wab-open"), str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", env=env)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("-L sockq "), line)

    def test_hung_az_makes_the_notice_fail_fast_and_not_raise(self):
        cfg, _ = self.chain()
        orig = load_orig("wab_orig_to")
        orig.event = lambda c, t: self.events.append(t)
        self.events = []

        def hang(*args, **kw):
            raise subprocess.TimeoutExpired(args, kw["timeout"])
        with mock.patch.object(orig, "sh", side_effect=hang):
            orig.notify(cfg, "hello")  # must not raise
        self.assertTrue(any("FAILED (TimeoutExpired)" in e for e in self.events), self.events)

    def test_dash_own_session_reads_the_server_and_pane_from_tmux_env(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        sockdir = f"{os.environ['TMUX_TMPDIR']}/tmux-{os.getuid()}"
        env = {"TMUX": f"{sockdir}/sockZ,123,0", "TMUX_PANE": "%3"}
        with mock.patch.dict(os.environ, env), mock.patch.object(
                dash.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "my-work\n", "")) as run:
            self.assertEqual(dash.own_session(), ("my-work", "sockZ", "%3"))
        self.assertIn("%3", run.call_args[0][0])
        with mock.patch.dict(os.environ, {"TMUX": f"{sockdir}/default,1,0", "TMUX_PANE": "%1"}), \
                mock.patch.object(dash.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "s\n", "")):
            self.assertEqual(dash.own_session(), ("s", "", "%1"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            self.assertEqual(dash.own_session(), (None, "", None))

    def test_socket_path_means_dash_S_and_a_name_means_dash_L(self):
        with mock.patch.object(wab, "TMUX_SOCKET", "/tmp/w/waves.sock"):
            self.assertEqual(wab.tmux_argv("ls"), ("tmux", "-S", "/tmp/w/waves.sock", "ls"))
            self.assertEqual(wab.attach_cmd("wv-w1"), "tmux -S /tmp/w/waves.sock attach -t wv-w1")
        with mock.patch.object(wab, "TMUX_SOCKET", None), mock.patch.dict(os.environ, {"WAB_TMUX_SOCKET": "/a/b"}):
            self.assertEqual(wab.tmux_argv("ls"), ("tmux", "-S", "/a/b", "ls"))
        with mock.patch.object(wab, "TMUX_SOCKET", "name"):
            self.assertEqual(wab.tmux_argv("ls"), ("tmux", "-L", "name", "ls"))

    def test_dash_keeps_the_full_socket_path_unless_it_is_tmuxs_own_dir(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        done = subprocess.CompletedProcess([], 0, "w\n", "")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/waves.sock,5,0", "TMUX_PANE": "%1"}), \
                mock.patch.object(dash.subprocess, "run", return_value=done):
            self.assertEqual(dash.own_session(), ("w", "/tmp/waves.sock", "%1"))

    def test_own_session_name_only_for_the_socket_dir_this_process_looks_at(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        uid = os.getuid()
        done = subprocess.CompletedProcess([], 0, "w\n", "")

        def own(tmux_env, tmpdir):
            env = {"TMUX": tmux_env, "TMUX_PANE": "%1"}
            with mock.patch.dict(os.environ, env), mock.patch.object(dash.subprocess, "run", return_value=done):
                if tmpdir is None:
                    os.environ.pop("TMUX_TMPDIR", None)
                else:
                    os.environ["TMUX_TMPDIR"] = tmpdir
                return dash.own_session()
        # same directory `-L` reads here: the short name
        self.assertEqual(own(f"/x/y/tmux-{uid}/r18a,1,0", "/x/y"), ("w", "r18a", "%1"))
        self.assertEqual(own(f"/x/y/tmux-{uid}/default,1,0", "/x/y"), ("w", "", "%1"))
        self.assertEqual(own(f"/tmp/tmux-{uid}/r18a,1,0", None), ("w", "r18a", "%1"))
        # a tmux-<uid> directory elsewhere (another TMUX_TMPDIR): `-L` would name ANOTHER server
        self.assertEqual(own(f"/tmp/claude-1000/xyz/tmux-{uid}/r18a,1,0", "/other"),
                         ("w", f"/tmp/claude-1000/xyz/tmux-{uid}/r18a", "%1"))
        self.assertEqual(own(f"/tmp/claude-1000/xyz/tmux-{uid}/r18a,1,0", None),
                         ("w", f"/tmp/claude-1000/xyz/tmux-{uid}/r18a", "%1"))
        self.assertEqual(own(f"/x/y/tmux-{uid}/default,1,0", "/z"), ("w", f"/x/y/tmux-{uid}/default", "%1"))

    def test_wab_open_uses_dash_S_for_a_socket_path(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W2", "waves": {"W2": self.wave_rec("W2")}})
        bindir = self.tmp / "fakebin"
        bindir.mkdir()
        log = self.tmp / "tmux.log"
        fake = bindir / "tmux"
        fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexit 0\n', encoding="utf-8")
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "WAB_TMUX_SOCKET": "/tmp/w/s.sock"}
        subprocess.run([str(WAVES / "wab-open"), str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", env=env)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("-S /tmp/w/s.sock "), line)

    def test_stale_btab_decision(self):
        old = ('bind-key -T root BTab if-shell -F "#{m:*ignore-size*,#{client_flags}}" '
               '{ send-keys BTab } { display-popup "wab-open /x/chain.json" }')
        self.assertTrue(wab.stale_btab(old))
        self.assertTrue(wab.stale_btab("bind-key -T root BTab run-shell /a/wab-open"))
        self.assertFalse(wab.stale_btab("bind-key -T root BTab send-keys BTab"))
        self.assertFalse(wab.stale_btab("bind-key -T root BTab select-pane -t :.-"))
        self.assertFalse(wab.stale_btab(""))
        self.assertFalse(wab.stale_btab("bind-key -T root C-x run-shell wab-open"))

    def run_bind(self, list_keys_output):
        cfg, path = self.chain()
        calls = self.tmux_calls
        inner = wab.sh.side_effect

        def fake(*args, **kw):
            r = inner(*args, **kw)
            if args[:2] == ("tmux", "list-keys"):
                return subprocess.CompletedProcess(args, 0, list_keys_output, "")
            return r
        wab.sh.side_effect = fake
        wab.drop_stale_btab(cfg)
        return [c for c in calls if c[1] == "unbind-key"]

    def test_drop_stale_btab_removes_only_the_stale_wab_btab_binding(self):
        stale = 'bind-key -T root BTab if-shell -F "#{m:*ignore-size*,#{client_flags}}" { send-keys BTab }'
        self.assertEqual(len(self.run_bind(stale)), 1)

    def test_drop_stale_btab_leaves_a_foreign_btab_binding_alone(self):
        self.assertEqual(self.run_bind("bind-key -T root BTab select-pane -t :.-"), [])
        self.assertEqual(self.run_bind(""), [])

    # ----- Ctrl+\ through the session option @wab_open (no registry) -----
    REF_BINDING = (
        'bind-key -n "C-\\\\" if-shell -F "#{@wab_open}" { run-shell -b "tmux display-popup -c '
        "'#{client_name}' -E -w 95% -h 90% -T ' волна ' '#{@wab_open}'\" } { if-shell -F "
        '"#{m:*ignore-size*,#{client_flags}}" { detach-client } { send-keys C-\\\\ } }\n')

    def test_keys_conf_is_the_reference_binding_alone(self):
        conf = wab.keys_conf()
        self.assertEqual(conf, self.REF_BINDING)
        self.assertEqual(len(re.findall(r"(?m)^bind-key\b", conf)), 1)
        self.assertNotIn("BTab", conf)
        self.assertNotIn("unbind", conf)
        self.assertNotRegex(conf, r"bind(-key)?\s[^\n]*\bW\b")
        self.assertFalse(hasattr(wab, "registry_path"))
        self.assertFalse(hasattr(wab, "bind_popup"))

    def tmux3(self):
        exe = shutil.which("tmux")
        if not exe:
            self.skipTest("tmux is not installed")
        ver = wab.parse_tmux_version(subprocess.run([exe, "-V"], capture_output=True, text=True,
                                                    encoding="utf-8").stdout)
        if ver is None or ver < (3, 2):
            self.skipTest("tmux older than 3.2")
        return exe

    def test_our_binding_lists_exactly_like_the_reference_on_a_private_server(self):
        exe = self.tmux3()
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        sock = f"wabkeys-{os.getpid()}"
        ours = self.tmp / "ours.tmux"
        ours.write_text(wab.keys_conf(), encoding="utf-8")
        ref = self.tmp / "ref.tmux"  # as a foreign chain wrote it: the same binding in `list-keys` spelling
        ref.write_text(self.REF_BINDING.replace("bind-key -n", "bind-key -T root"), encoding="utf-8")

        def run(*a):
            return subprocess.run([exe, "-L", sock, "-f", "/dev/null", *a], capture_output=True, text=True,
                                  encoding="utf-8", env=env)

        def cstar():
            out = run("list-keys", "-T", "root").stdout
            return [ln for ln in out.splitlines() if ln.split()[3:4] == ["C-\\\\"]]
        try:
            self.assertEqual(run("new-session", "-d", "-s", "keep", "sleep 60", ";", "source-file",
                                 str(ref)).returncode, 0)
            first = cstar()
            self.assertEqual(len(first), 1, first)
            r = run("source-file", str(ours))
            self.assertEqual((r.returncode, r.stderr.strip()), (0, ""), r.stderr)
            self.assertEqual(cstar(), first)  # re-sourcing ours over theirs changes nothing
        finally:
            run("kill-server")
        sock2 = sock + "b"
        try:
            subprocess.run([exe, "-L", sock2, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 60", ";",
                            "source-file", str(ours)],
                           capture_output=True, env=env)
            out = subprocess.run([exe, "-L", sock2, "list-keys", "-T", "root"], capture_output=True, text=True,
                                 encoding="utf-8", env=env).stdout
            self.assertEqual([ln for ln in out.splitlines() if ln.split()[3:4] == ["C-\\\\"]], first)
        finally:
            subprocess.run([exe, "-L", sock2, "kill-server"], capture_output=True, env=env)

    def test_register_dash_sets_the_option_on_its_own_server_only(self):
        cfg, path = self.chain()
        cases = [("X", ["-L", "X"]), ("/p/x.sock", ["-S", "/p/x.sock"]), ("", [])]
        for env_sock in (None, "envsock"):
            for sock, flag in cases:
                with self.subTest(sock=sock, env=env_sock):
                    self.tmux_calls.clear()
                    os.environ.pop("WAB_TMUX_SOCKET", None)
                    env = {} if env_sock is None else {"WAB_TMUX_SOCKET": env_sock}
                    with mock.patch.object(wab, "TMUX_SOCKET", None), mock.patch.dict(os.environ, env):
                        self.assertTrue(wab.register_dash(cfg, path, "my-work", sock, "%7"))
                    n = len(flag)
                    setc = [c for c in self.tmux_calls if c[1 + n] == "set-option"]
                    self.assertEqual(len(setc), 3)  # @wab_open, then the owner marks (#57)
                    self.assertEqual(list(setc[0][2 + n:6 + n]), ["-p", "-t", "%7", "@wab_open"])
                    self.assertEqual(setc[0][6 + n], f"{WAVES / 'wab-open'} {path.resolve()}")
                    self.assertEqual(list(setc[1][2 + n:7 + n]), ["-p", "-t", "%7", "@wab_run", f"{CHAIN}/{RUN_ID}"])
                    self.assertEqual(list(setc[2][2 + n:6 + n]), ["-p", "-t", "%7", "@wab_run_dir"])
                    self.assertIn("source-file", {c[1 + n] for c in self.tmux_calls})
                    for call in self.tmux_calls:
                        self.assertEqual(list(call[1:1 + n]), flag, call)
                        self.assertNotIn(call[1 + n], ("-L", "-S"), call)

    def test_unregister_unsets_the_option_on_the_same_server(self):
        wab.unregister_dash("my-work", "/p/x.sock", "%7")
        for i, opt in enumerate(("@wab_open", "@wab_run", "@wab_run_dir")):
            self.assertEqual(self.tmux_calls[-3 + i],
                             ("tmux", "-S", "/p/x.sock", "set-option", "-p", "-u", "-t", "%7", opt))
        wab.unregister_dash("my-work", "/p/x.sock")  # no pane known: the session option, as before
        for i, opt in enumerate(("@wab_open", "@wab_run", "@wab_run_dir")):
            self.assertEqual(self.tmux_calls[-3 + i],
                             ("tmux", "-S", "/p/x.sock", "set-option", "-u", "-t", "=my-work:", opt))

    def test_foreign_binding_refusal_does_not_claim_the_key_toggles_the_popup(self):
        cfg, path = self.chain()
        self.listing('bind-key -T root C-\\\\ send-keys foo')
        self.assertFalse(wab.register_dash(cfg, path, "s", "", "%1"))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("foreign binding", log)
        self.assertNotIn("toggles the wave popup", log)
        self.listing('bind-key -T root C-\\\\ if-shell -F "#{@wab_open}" { x }')
        self.assertTrue(wab.register_dash(cfg, path, "s", "", "%1"))
        self.assertIn("toggles the wave popup", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_unsafe_path_refusal_also_goes_to_stderr_in_one_line(self):
        import contextlib
        import io
        cfg, path = self.chain()
        d = self.tmp / "my dir"
        d.mkdir()
        bad = d / "chain.json"
        bad.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(wab.register_dash(cfg, bad, "s", "", "%1"))
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("unsafe path", lines[0])

    def test_two_dashboards_in_one_session_keep_their_own_pane_option(self):
        exe = self.tmux3()
        cfg, path = self.chain()
        sock = self.tmp / "p.sock"
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        run = lambda *a: subprocess.run([exe, "-S", str(sock), "-f", "/dev/null", *a],  # noqa: E731
                                        capture_output=True, text=True, encoding="utf-8", env=env)
        try:
            self.assertEqual(run("new-session", "-d", "-s", "s", "sleep 60").returncode, 0)
            p1 = run("display", "-p", "-t", "=s:", "#{pane_id}").stdout.strip()
            p2 = run("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=s:", "sleep 60").stdout.strip()
            cfg2, path2 = self.chain(waves=["W1"])
            path2 = path2.with_name("chain2.json")
            path2.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch.object(wab, "sh", REAL_SH):
                self.assertTrue(wab.register_dash(cfg, path, "s", str(sock), p1))
                self.assertTrue(wab.register_dash(cfg, path2, "s", str(sock), p2))
                val = lambda pane: run("display", "-p", "-t", pane, "#{@wab_open}").stdout.strip()  # noqa: E731
                self.assertTrue(val(p1).endswith(str(path.resolve())), val(p1))
                self.assertTrue(val(p2).endswith(str(path2.resolve())), val(p2))
                # the binding's if-shell -F format sees the pane option of the active pane
                run("select-pane", "-t", p2)
                run("if-shell", "-F", "-t", p2, "#{@wab_open}", "set -g @r yes", "set -g @r no")
                self.assertEqual(run("show-options", "-gv", "@r").stdout.strip(), "yes")
                wab.unregister_dash("s", str(sock), p1)
                self.assertEqual(val(p1), "")
                self.assertTrue(val(p2).endswith(str(path2.resolve())), val(p2))
        finally:
            run("kill-server")

    def test_unsafe_chain_path_is_refused_without_touching_tmux(self):
        cfg, path = self.chain()
        for name in ("my dir", "it's", 'a"b', "a;b", "a#b", "a{b", "a$(x)"):
            with self.subTest(name=name):
                d = self.tmp / name
                d.mkdir()
                bad = d / "chain.json"
                bad.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                self.tmux_calls.clear()
                self.assertFalse(wab.register_dash(cfg, bad, "s", ""))
                self.assertEqual(self.tmux_calls, [])
        self.assertIn("refused", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def listing(self, text):
        inner = wab.sh.side_effect

        def fake(*args, **kw):
            r = inner(*args, **kw)
            if args[:2] == ("tmux", "list-keys"):
                return subprocess.CompletedProcess(args, 0, text, "")
            return r
        wab.sh.side_effect = fake

    def sourced(self):
        return [c for c in self.tmux_calls if c[1] == "source-file"]

    def test_binding_is_installed_unless_one_of_the_scheme_or_a_foreign_one_is_there(self):
        cfg, path = self.chain()
        ours = 'bind-key -T root C-\\\\ if-shell -F "#{@wab_open}" { x }'
        old = 'bind-key -T root C-\\\\ if-shell -F "#{==:#{session_name},d}" { display-popup "/a/wab-open /c" }'
        foreign = 'bind-key -T root C-\\\\ send-keys foo'
        other = 'bind-key -T root C-x run-shell wab-open'
        for text, installs in ((ours, False), (old, True), (foreign, False), ("", True), (other, True)):
            with self.subTest(text=text):
                self.tmux_calls.clear()
                self.listing(text)
                wab.register_dash(cfg, path, "s", "")
                self.assertEqual(len(self.sourced()), 1 if installs else 0)
        self.assertIn("foreign binding", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        self.assertEqual((cfg["run_dir"] / "keys.tmux").read_text(encoding="utf-8"), wab.keys_conf())

    def test_watch_never_touches_ctrl_backslash(self):
        import inspect
        src = inspect.getsource(wab._watch) + inspect.getsource(wab.drop_stale_btab)
        for word in ("keys_conf", "source-file", "register_dash", "_ensure_binding", "C-\\\\"):
            self.assertNotIn(word, src)
        cfg, _ = self.chain()
        self.listing('bind-key -T root BTab run-shell /a/wab-open')
        self.tmux_calls.clear()
        wab.drop_stale_btab(cfg)
        self.assertEqual([c[1] for c in self.tmux_calls], ["list-keys", "unbind-key"])

    def test_real_private_server_option_roundtrip_on_a_socket_path(self):
        exe = self.tmux3()
        cfg, path = self.chain()
        sock = self.tmp / "waves.sock"
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        run = lambda *a: subprocess.run([exe, "-S", str(sock), "-f", "/dev/null", *a],  # noqa: E731
                                        capture_output=True, text=True, encoding="utf-8", env=env)
        try:
            self.assertEqual(run("new-session", "-d", "-s", "dash", "sleep 60").returncode, 0)
            with mock.patch.object(wab, "sh", REAL_SH):
                self.assertTrue(wab.register_dash(cfg, path, "dash", str(sock)))
                got = run("show-options", "-v", "-t", "=dash:", "@wab_open").stdout.strip()
                self.assertEqual(got, f"{WAVES / 'wab-open'} {path.resolve()}")
                keys = run("list-keys", "-T", "root").stdout
                self.assertEqual(len([ln for ln in keys.splitlines() if ln.split()[3:4] == ["C-\\\\"]]), 1)
                wab.unregister_dash("dash", str(sock))
                self.assertEqual(run("show-options", "-v", "-t", "=dash:", "@wab_open").stdout.strip(), "")
        finally:
            run("kill-server")

    def test_dash_cleans_up_on_sigterm_and_sighup_and_ignores_sigquit(self):
        src = (WAVES / "dash.py").read_text(encoding="utf-8")
        self.assertIn("signal.SIGTERM", src)
        self.assertIn("signal.SIGHUP", src)
        self.assertIn("wab.unregister_dash(", src)
        self.assertIn("signal.signal(signal.SIGQUIT, signal.SIG_IGN)", src)

    def test_dash_survives_sigquit(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        import signal as sg
        old = {s: sg.getsignal(s) for s in (sg.SIGQUIT, sg.SIGTERM, sg.SIGHUP)}
        for s, h in old.items():
            self.addCleanup(sg.signal, s, h)

        class Boom(Exception):
            pass

        class FakeLive:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                os.kill(os.getpid(), sg.SIGQUIT)  # would terminate the process with the default handler
                raise Boom

            def __exit__(self, *a):
                return False
        cfg, path = self.chain()
        out = mock.Mock()
        out.isatty.return_value = True
        with mock.patch.object(dash, "Live", FakeLive), mock.patch.object(dash, "safe_render", return_value=""), \
                mock.patch.object(dash.wab, "load_chain", return_value=cfg), \
                mock.patch.object(dash, "own_session", return_value=(None, "", None)), \
                mock.patch.object(sys, "argv", ["dash.py", str(path)]), mock.patch.object(sys, "stdout", out):
            with self.assertRaises(Boom):
                dash.main()

    def test_wab_open_takes_the_server_from_TMUX_when_no_socket_is_set(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W2", "waves": {"W2": self.wave_rec("W2")}})
        bindir = self.tmp / "fakebin"
        bindir.mkdir()
        log = self.tmp / "tmux.log"
        fake = bindir / "tmux"
        fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexit 0\n', encoding="utf-8")
        fake.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "WAB_TMUX_SOCKET"}
        env.update(PATH=f"{bindir}:{env['PATH']}", TMUX="/tmp/w/s.sock,123,0")
        subprocess.run([str(WAVES / "wab-open"), str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", env=env)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("-S /tmp/w/s.sock "), line)

    def test_wab_open_contract(self):
        script = WAVES / "wab-open"
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding="utf-8")
        self.assertIn("-u attach", text)
        self.assertIn('has-session -t "=$name"', text)
        self.assertIn('attach -f ignore-size -t "=$name"', text)
        self.assertIn("current-tmux", text)
        self.assertNotIn("python3 -c", text)
        self.assertNotIn("$1'", text)
        mode = subprocess.run(["git", "ls-files", "-s", str(script)], cwd=ROOT, capture_output=True,
                              text=True).stdout.split()[:1]
        if mode:
            self.assertEqual(mode[0], "100755")

    def test_wab_open_survives_a_hostile_chain_path(self):
        cfg, path = self.chain()
        hostile = path.parent / "it's \"x\"; $(touch pwned).json"
        hostile.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self.put_state(cfg, {"current": None, "waves": {}})
        proc = subprocess.run([str(WAVES / "wab-open"), str(hostile)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Нет активной волны", proc.stdout)
        self.assertFalse((path.parent / "pwned").exists())
        self.assertFalse(Path("pwned").exists())

    def test_current_tmux_command(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W2", "waves": {"W2": self.wave_rec("W2")}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wab.main(["wab.py", "current-tmux", str(path)])
        self.assertEqual(buf.getvalue().strip(), "wv-w2")


# ---------------------------------------------------------------- A9
class TmuxVersion(Base):
    def test_parse(self):
        p = wab.parse_tmux_version
        self.assertEqual(p("tmux 3.4"), (3, 4))
        self.assertEqual(p("tmux 3.2a\n"), (3, 2))
        self.assertEqual(p("tmux next-3.5"), (3, 5))
        self.assertEqual(p("tmux 2.9a"), (2, 9))
        self.assertEqual(p("tmux 10.1"), (10, 1))
        self.assertGreaterEqual(p("tmux master"), (3, 5))
        self.assertIsNone(p("garbage"))
        self.assertIsNone(p(""))

    def test_require_refuses_old_or_unknown_and_accepts_new(self):
        orig = load_orig("wab_orig2")

        def with_output(text, rc=0):
            return mock.patch.object(orig, "sh", return_value=subprocess.CompletedProcess([], rc, text, ""))
        with with_output("tmux 3.0\n"):
            with self.assertRaises(SystemExit) as ctx:
                orig.require_tmux()
        self.assertIn("3.2", str(ctx.exception))
        with with_output("tmux 2.9a\n"), self.assertRaises(SystemExit):
            orig.require_tmux()
        with with_output("???\n"), self.assertRaises(SystemExit):
            orig.require_tmux()
        with with_output("", rc=1), self.assertRaises(SystemExit):
            orig.require_tmux()
        with mock.patch.object(orig, "sh", side_effect=FileNotFoundError("tmux")):
            with self.assertRaises(SystemExit):
                orig.require_tmux()
        for ok in ("tmux 3.2\n", "tmux 3.4\n", "tmux next-3.5\n", "tmux master\n"):
            with with_output(ok):
                orig.require_tmux()

    def test_launch_and_watch_check_the_version_first(self):
        cfg, path = self.chain()
        wab.require_tmux.side_effect = SystemExit("too old")
        prompt = self.tmp / "p.md"
        prompt.write_text("x", encoding="utf-8")
        with self.assertRaises(SystemExit):
            wab.launch(cfg, "W1", prompt)
        with self.assertRaises(SystemExit):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.tmux_calls, [])


# ---------------------------------------------------------------- A12 + chain config
class Mandate(Base):
    def test_without_mandate_file_there_is_no_mandate_section(self):
        cfg, _ = self.chain()
        text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertNotIn("## Мандат прогона", text)
        self.assertIn("handoff", text)

    def test_wrong_digest_is_refused_and_right_one_is_included(self):
        body = f"Прогон: {RUN_ID}\nМердж разрешён при зелёном CI.\n".encode("utf-8")
        cfg, _ = self.chain(mandate_sha256="0" * 64)
        (cfg["run_dir"] / "mandate.md").write_bytes(body)
        with self.assertRaises(SystemExit):
            wab.system_prompt(cfg)
        cfg["mandate_sha256"] = hashlib.sha256(body).hexdigest()
        text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertIn("## Мандат прогона", text)
        self.assertIn("Мердж разрешён", text)

    def test_mandate_of_another_run_is_ignored(self):
        cfg, _ = self.chain()
        (cfg["run_dir"] / "mandate.md").write_bytes("Прогон: other\nZZ-UNIQUE-BODY\n".encode("utf-8"))
        text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertNotIn("## Мандат прогона", text)
        self.assertNotIn("ZZ-UNIQUE-BODY", text)

    def test_pinned_mandate_cannot_be_dodged_by_removing_or_spoiling_the_file(self):
        # A mandate_sha256 in chain.json pins phase A: deleting, emptying or re-heading
        # mandate.md must refuse the launch, not quietly fall back to the bare protocol.
        body = f"Прогон: {RUN_ID}\nМердж разрешён при зелёном CI.\n".encode("utf-8")
        for name, content in (("missing", None), ("empty", b""), ("blank", b"\n\n"),
                              ("foreign header", "Прогон: other\nМердж разрешён\n".encode("utf-8"))):
            with self.subTest(name):
                cfg, _ = self.chain(mandate_sha256=hashlib.sha256(body).hexdigest())
                mandate = cfg["run_dir"] / "mandate.md"
                if mandate.exists():
                    mandate.unlink()
                if content is not None:
                    mandate.write_bytes(content)
                with self.assertRaises(SystemExit) as cm:
                    wab.system_prompt(cfg)
                self.assertIn("mandate_sha256", str(cm.exception))
                self.assertFalse((cfg["run_dir"] / "system-prompt.md").exists())

    def test_mandate_is_decoded_as_utf8_whatever_the_locale(self):
        body = f"Прогон: {RUN_ID}\nкириллица\n".encode("utf-8")
        cfg, _ = self.chain(mandate_sha256=hashlib.sha256(body).hexdigest())
        (cfg["run_dir"] / "mandate.md").write_bytes(body)
        with mock.patch("locale.getpreferredencoding", return_value="ANSI_X3.4-1968"):
            text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertIn("кириллица", text)


class ChainConfig(Base):
    def test_unknown_merge_gate_fails_closed(self):
        for bad in ("externl", "EXTERNAL", "Auto", 1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    self.chain(merge_gate=bad)
                self.assertIn("merge_gate", str(ctx.exception))
        for ok in (None, "", "external", "auto"):
            with self.subTest(ok=ok):
                self.chain(merge_gate=ok)

    def _bad_doc(self, **over):
        cfgdir = self.tmp / "badcfg"
        cfgdir.mkdir(exist_ok=True)
        path = cfgdir / "chain.json"
        path.write_text(json.dumps({"chain": CHAIN, "run_id": RUN_ID, "waves": ["W1"], **over}), encoding="utf-8")
        return path

    def test_numeric_settings_are_validated_before_launch(self):
        for field in ("ctx_limit", "idle_minutes", "handoff_timeout_minutes", "tick_seconds"):
            for bad in ("12", True, False, None, -1, 0 if field != "idle_minutes" else -0.5, float("nan"), float("inf"), [1], {}):
                with self.subTest(field=field, bad=bad):
                    path = self._bad_doc(**{field: bad})
                    with self.assertRaises(SystemExit) as ctx:
                        wab.load_chain(path, create=False)
                    self.assertIn(field, str(ctx.exception))
            for ok in (1, 0.5, 600) + ((0,) if field == "idle_minutes" else ()):
                with self.subTest(field=field, ok=ok):
                    cfg, _ = self.chain(**{field: ok})
                    self.assertEqual(cfg[field], ok)

    def test_numeric_settings_have_upper_bounds(self):
        for field, big in (("tick_seconds", 3601), ("tick_seconds", 1e10), ("tick_seconds", 1e308),
                           ("idle_minutes", 1441), ("handoff_timeout_minutes", 1e9),
                           ("ctx_limit", 10_000_001)):
            with self.subTest(field=field, big=big):
                with self.assertRaises(SystemExit) as ctx:
                    wab.load_chain(self._bad_doc(**{field: big}), create=False)
                self.assertIn(field, str(ctx.exception))
        cfg, _ = self.chain(tick_seconds=3600, idle_minutes=1440, ctx_limit=10_000_000)
        self.assertEqual(cfg["tick_seconds"], 3600)

    def test_other_tunables_have_the_right_type(self):
        for field, bad in (("model", 5), ("model", ["x"]), ("titles", "x"), ("titles", [1]),
                           ("titles", {"W1": 3}), ("telegram", "x"), ("telegram", [1])):
            with self.subTest(field=field, bad=bad):
                path = self._bad_doc(**{field: bad})
                with self.assertRaises(SystemExit) as ctx:
                    wab.load_chain(path, create=False)
                self.assertIn(field, str(ctx.exception))

    def test_invalid_live_edit_keeps_old_settings_and_watch_survives(self):
        cfg, path = self.chain()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["tick_seconds"] = None
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.alive = True
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        with mock.patch.object(wab.time, "sleep"):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(cfg["tick_seconds"], 1)
        self.assertIn("keeping the previous settings", "\n".join(
            p.read_text(encoding="utf-8") for p in cfg["run_dir"].glob("*.log")))

    def test_attach_hint_names_the_private_socket(self):
        with mock.patch.object(wab, "TMUX_SOCKET", None), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAB_TMUX_SOCKET", None)
            self.assertEqual(wab.attach_cmd("wv-w1"), "tmux attach -t wv-w1")
        with mock.patch.object(wab, "TMUX_SOCKET", "my sock"):
            self.assertEqual(wab.attach_cmd("wv-w1"), "tmux -L 'my sock' attach -t wv-w1")
        with mock.patch.object(wab, "TMUX_SOCKET", None), mock.patch.dict(os.environ, {"WAB_TMUX_SOCKET": "s1"}):
            self.assertEqual(wab.attach_cmd("wv-w1"), "tmux -L s1 attach -t wv-w1")

    def test_notices_carry_the_socket_in_the_attach_command(self):
        cfg, _ = self.chain()
        self.alive = True
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: need you")
        with mock.patch.object(wab, "TMUX_SOCKET", "sockz"):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue(self.tg)
        self.assertIn("-L sockz attach", self.tg[-1])

    def test_run_directory_layout(self):
        cfg, path = self.chain()
        self.assertEqual(cfg["run_dir"], path.parent.resolve() / "runs" / CHAIN / RUN_ID)
        base = self.tmp / "elsewhere"
        cfg, _ = self.chain(run_dir=str(base))
        self.assertEqual(cfg["run_dir"], base / CHAIN / RUN_ID)
        self.assertNotIn(str(WAVES), str(cfg["run_dir"]))

    def test_relative_run_dir_is_resolved_against_the_chain_file(self):
        cfg, path = self.chain(run_dir="runs")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        old = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, old)
        again = wab.load_chain(path)
        self.assertTrue(again["run_dir"].is_absolute())
        self.assertEqual(again["run_dir"], path.parent.resolve() / "runs" / CHAIN / RUN_ID)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_wave_dir_and_system_prompt_are_absolute(self):
        cfg, path = self.chain(run_dir="runs")
        self.assertTrue(wab.wave_dir(cfg, "W1").is_absolute())
        self.assertTrue(wab.system_prompt(cfg).is_absolute())

    def test_duplicate_wave_ids_are_rejected(self):
        for waves in (["W1", "W1", "W2"], ["W1", "w1"]):
            with self.subTest(waves=waves):
                with self.assertRaises(SystemExit) as ctx:
                    self.chain(waves=waves)
                self.assertIn("duplicate", str(ctx.exception))
        self.chain(waves=["W1", "W2"])

    def test_unsafe_identifiers_are_rejected(self):
        for field, value in (("run_id", None), ("run_id", ""), ("run_id", ".."), ("run_id", "a/b"),
                             ("run_id", "."), ("chain", "a/b"), ("chain", ".."), ("chain", ""),
                             ("tmux_prefix", "a b"), ("tmux_prefix", "a;b"), ("tmux_prefix", "")):
            with self.subTest(field=field, value=value):
                doc = {field: value}
                with self.assertRaises(SystemExit):
                    self.chain(**doc)

    def test_status_reading_does_not_create_directories(self):
        cfg, path = self.chain()
        shutil.rmtree(cfg["run_dir"])
        wab.load_chain(path, create=False)
        self.assertFalse(cfg["run_dir"].exists())


class W4Round20(Base):
    def _doc(self, **over):
        d = self.tmp / "r20"
        d.mkdir(exist_ok=True)
        p = d / "chain.json"
        p.write_text(json.dumps({"chain": CHAIN, "run_id": RUN_ID, "waves": ["W1"], **over}), encoding="utf-8")
        return p

    def test_huge_int_settings_are_a_clean_refusal(self):
        for field in ("ctx_limit", "idle_minutes", "handoff_timeout_minutes", "tick_seconds"):
            with self.subTest(field=field):
                path = self._doc()
                path.write_text('{"chain": "%s", "run_id": "%s", "waves": ["W1"], "%s": %s}'
                                % (CHAIN, RUN_ID, field, "9" * 400), encoding="utf-8")
                with self.assertRaises(SystemExit) as ctx:
                    wab.load_chain(path, create=False)
                self.assertIn(field, str(ctx.exception))

    def test_huge_int_on_a_live_edit_keeps_the_previous_settings(self):
        cfg, path = self.chain()
        text = path.read_text(encoding="utf-8").replace('"tick_seconds": 1,', '"tick_seconds": %s,' % ("9" * 400))
        path.write_text(text, encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        with mock.patch.object(wab.time, "sleep"):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(cfg["tick_seconds"], 1)
        self.assertIn("keeping the previous settings", "\n".join(
            p.read_text(encoding="utf-8") for p in cfg["run_dir"].glob("*.log")))

    def test_wab_open_with_an_empty_socket_is_the_default_server(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W2", "waves": {"W2": self.wave_rec("W2")}})
        bindir = self.tmp / "fakebin2"
        bindir.mkdir()
        log = self.tmp / "tmux2.log"
        fake = bindir / "tmux"
        fake.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexit 0\n', encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ, WAB_TMUX_SOCKET="", PATH=f"{bindir}:{os.environ['PATH']}")
        subprocess.run([str(WAVES / "wab-open"), str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", env=env)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertNotRegex(line, r"(^|\s)-[LS]\s")

    def test_without_merge_gate_done_hands_off_to_the_coordinator(self):
        cfg, path = self.chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        nxt = wab.wave_dir(cfg, "W1") / "next-prompt.md"
        nxt.write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "launch") as launch, mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=5)
        launch.assert_not_called()
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("no merge_gate: handing off to coordinator", log)
        self.assertIn(f"wab.py launch {path.resolve()} W2 {nxt}", log)

    def test_last_wave_without_merge_gate_finishes_as_before(self):
        cfg, _ = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertIsNone(self.get_state(cfg)["current"])
        self.assertIn("chain finished", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_external_done_without_next_prompt_stops_loudly(self):
        for gate in ("external", None):
            with self.subTest(gate=gate):
                self.tg.clear()
                cfg, path = self.chain(merge_gate=gate)
                (cfg["run_dir"] / "events.log").unlink(missing_ok=True)  # shared run dir between subtests
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
                self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
                log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
                self.assertEqual(log.count("W1: DONE without next-prompt.md; chain stopped"), 1)
                self.assertNotIn("handed to the coordinator", log)
                self.assertEqual(len([t for t in self.tg if "next-prompt.md" in t]), 1)
                with mock.patch.object(wab, "time") as t, mock.patch.object(wab, "drop_stale_btab"), \
                        contextlib.redirect_stderr(io.StringIO()):
                    t.time.return_value = 0.0
                    with self.assertRaises(SystemExit) as ctx:
                        wab.main(["wab.py", "watch", str(path)])
                self.assertEqual(ctx.exception.code, 3)

    def test_enabled_telegram_needs_all_three_fields_as_strings(self):
        full = {"keyvault": "kv", "token_secret": "t", "chat_secret": "c"}
        for bad in ({"keyvault": "kv"}, {**full, "chat_secret": ""}, {**full, "token_secret": 5},
                    {**full, "keyvault": None}, {"x": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    wab.load_chain(self._doc(telegram=bad), create=False)
                self.assertIn("telegram", str(ctx.exception))
        wab.load_chain(self._doc(telegram=full), create=False)

    def test_partial_telegram_on_a_live_edit_keeps_the_previous_settings(self):
        cfg, path = self.chain()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["telegram"] = {"keyvault": "kv"}
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        with mock.patch.object(wab.time, "sleep"):
            wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(cfg["telegram"]["token_secret"], "tok")
        self.assertIn("keeping the previous settings", "\n".join(
            p.read_text(encoding="utf-8") for p in cfg["run_dir"].glob("*.log")))


class W4Round23Notices(Base):
    """A notice about a standing status is marked delivered only after the transport took it."""

    def setUp(self):
        super().setUp()
        self.down = True
        self.attempts = 0

        def flaky(cfg, text):
            self.attempts += 1
            if self.down:
                raise OSError("telegram down")
            self.tg.append(text)
        p = mock.patch.object(wab, "_send_telegram", side_effect=flaky)
        p.start()
        self.addCleanup(p.stop)
        self.cfg, _ = self.chain()
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})

    def tick(self):
        wab.tick(self.cfg, wab.load_state(self.cfg))

    def rec(self):
        return self.get_state(self.cfg)["waves"]["W1"]

    def due(self):
        """The retry interval has passed."""
        st = self.get_state(self.cfg)
        for item in st["waves"]["W1"].get("outbox", {}).values():
            item["next_at"] = 0
        self.put_state(self.cfg, st)

    def failed_events(self):
        log = (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        return log.count("telegram FAILED")

    def test_notify_reports_whether_it_is_done(self):
        self.assertFalse(wab.notify(self.cfg, "x"))
        self.down = False
        self.assertTrue(wab.notify(self.cfg, "x"))
        cfg, _ = self.chain(telegram=False)
        self.assertTrue(wab.notify(cfg, "x"))  # not configured: nothing to repeat

    def test_blocked_failed_send_is_retried_then_stops(self):
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertEqual(self.attempts, 1)
        self.assertIn("blocked", self.rec().get("outbox", {}))
        self.down = False
        self.tick()  # interval has not passed
        self.assertEqual(self.attempts, 1)
        self.due()
        self.tick()
        self.assertEqual(len(self.tg), 1)
        self.assertFalse(self.rec().get("outbox"))
        self.due()
        self.tick()
        self.tick()
        self.assertEqual(len(self.tg), 1)

    def test_permanent_failure_is_throttled_in_sends_and_events(self):
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        for _ in range(6):
            self.tick()
        self.assertEqual(self.attempts, 1)
        self.assertEqual(self.failed_events(), 1)
        self.due()
        self.tick()
        self.tick()
        self.assertEqual(self.attempts, 2)
        self.assertEqual(self.failed_events(), 2)
        self.assertGreater(self.rec()["outbox"]["blocked"]["next_at"], time.time())

    def test_supervision_goes_on_while_the_notice_fails(self):
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertEqual(self.rec()["phase"], "running")
        self.assertEqual(self.rec()["last_status"], "BLOCKED: q")

    def test_episode_over_before_delivery_drops_the_stale_notice(self):
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.set_status(self.cfg, "W1", "RUNNING")
        self.down = False
        self.due()
        self.tick()
        self.assertEqual(self.tg, [])
        self.assertFalse(self.rec().get("outbox"))

    def test_other_episodes_use_the_same_retry(self):
        cases = {
            "dead": dict(alive=False, status="RUNNING", pane=""),
            "permission": dict(alive=True, status="RUNNING", pane="Do you want to proceed?\n"),
            "auto_off": dict(alive=True, status="RUNNING", pane=AutoGuard.OFF),
        }
        for key, c in cases.items():
            with self.subTest(key=key):
                self.down = True
                self.attempts = 0
                self.tg.clear()
                self.alive, self.pane = c["alive"], c["pane"]
                self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(self.cfg, "W1", c["status"])
                for _ in range(3):
                    self.tick()
                self.assertIn(key, self.rec().get("outbox", {}))
                self.down = False
                self.due()
                self.tick()
                self.assertEqual(len(self.tg), 1, self.tg)
                self.due()
                self.tick()
                self.tick()
                self.assertEqual(len(self.tg), 1)

    def test_stopped_chain_without_next_prompt_retries_too(self):
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(self.cfg, "W1", "DONE")
        (wab.wave_dir(self.cfg, "W1") / "result.md").write_text("r\n", encoding="utf-8")
        self.tick()  # chain stops: current is None afterwards
        self.assertIn("no_next", self.rec().get("outbox", {}))
        self.down = False
        self.due()
        self.tick()
        self.assertTrue(any("next-prompt" in t for t in self.tg), self.tg)


class W4Round24(Base):
    """Round 24: no stale snapshot over a newer state, handoff notices through the outbox, `done`,
    a fresh checkout for the next wave, redaction of dash-separated tokens."""

    def setUp(self):
        super().setUp()
        self.down = False
        self.attempts = 0

        def flaky(cfg, text):
            self.attempts += 1
            if self.down:
                raise OSError("telegram down")
            self.tg.append(text)
        p = mock.patch.object(wab, "_send_telegram", side_effect=flaky)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)

    def prompt(self):
        f = self.tmp / "p.md"
        f.write_text("go\n", encoding="utf-8")
        return f

    # ----- 1: a notice flush must not roll the state back after launch -----
    def test_flush_after_launch_keeps_the_new_current_and_record(self):
        cfg, _ = self.auto_chain()
        self.alive = False
        old = {"value": "x", "text": "old notice", "next_at": 0}
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec(outbox={"no_next": old})}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.down = True
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            wab.tick(cfg, wab.load_state(cfg))
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W2")
        self.assertIn("W2", st["waves"])
        self.assertEqual(st["waves"]["W2"]["phase"], "running")
        self.assertTrue(st["waves"]["W2"]["sessions"])
        self.assertIn("no_next", st["waves"]["W1"].get("outbox", {}))  # the stale notice is still kept

    # ----- 2: handoff and chain-finished notices survive a Telegram failure -----
    def test_handoff_notice_is_retried_by_the_next_watch_exactly_once(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.down = True
        wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(self.tg, [])
        self.assertIn("handoff", self.get_state(cfg)["waves"]["W1"].get("outbox", {}))
        self.down = False
        wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(len([t for t in self.tg if "сдала PR" in t]), 1, self.tg)
        wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(len([t for t in self.tg if "сдала PR" in t]), 1)
        self.assertFalse(self.get_state(cfg)["waves"]["W1"].get("outbox"))

    def test_watch_drains_the_handoff_notice_before_it_exits(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        calls = []

        def once_down(c, text):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("down")
            self.tg.append(text)
        with mock.patch.object(wab, "_send_telegram", side_effect=once_down):
            wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(len(self.tg), 1)

    def test_chain_finished_notice_goes_through_the_outbox(self):
        cfg, path = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.down = True
        wab.tick(cfg, wab.load_state(cfg))
        self.assertIn("chain_done", self.get_state(cfg)["waves"]["W1"].get("outbox", {}))
        self.down = False
        wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1, self.tg)
        wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)

    # ----- 3: the last wave has a way to finish -----
    def last_wave_awaiting(self, **over):
        cfg, path = self.chain(waves=["W1"], merge_gate="external", **over)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.set_status(cfg, "W1", "DONE")
        return cfg, path

    def test_done_command_finishes_the_chain_after_the_last_merge(self):
        cfg, path = self.last_wave_awaiting()
        wab.main(["wab.py", "done", str(path), "W1"])
        st = self.get_state(cfg)
        self.assertEqual(st["waves"]["W1"]["phase"], "done")
        self.assertIsNone(st["current"])
        self.assertIn("chain finished", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)
        wab.main(["wab.py", "done", str(path)])  # again, wave omitted: nothing new
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))

    def test_done_defaults_to_the_current_wave_and_refuses_the_rest(self):
        cfg, path = self.last_wave_awaiting()
        wab.main(["wab.py", "done", str(path)])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "done")
        cfg, path = self.chain(merge_gate="external")  # W1, W2: W1 is not the last wave
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        with self.assertRaises(SystemExit) as ctx:
            wab.main(["wab.py", "done", str(path), "W1"])
        self.assertIn("launch", str(ctx.exception))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="running")}})
        with self.assertRaises(SystemExit):
            wab.main(["wab.py", "done", str(path), "W1"])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")
        with self.assertRaises(SystemExit):
            wab.main(["wab.py", "done", str(path), "W9"])

    def test_handoff_of_the_last_wave_names_the_done_command(self):
        cfg, path = self.last_wave_awaiting()
        wab._stop_event(cfg, wab.load_state(cfg), path)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        wab_py = shlex.quote(str(Path(wab.__file__).resolve()))
        self.assertIn(f"python3 {wab_py} done {path.resolve()} W1", log)

    # ----- 4: the next wave starts on the fresh base, not on the previous branch -----
    def git(self, cwd, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.y",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.y")
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def repos(self):
        origin, clone = self.tmp / "origin.git", self.tmp / "wd"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
        self.git(clone, "checkout", "-q", "-b", "main")
        (clone / "a.txt").write_text("a\n", encoding="utf-8")
        self.git(clone, "add", "a.txt")
        self.git(clone, "commit", "-q", "-m", "base")
        self.git(clone, "push", "-q", "origin", "main")
        return origin, clone

    def next_wave_cfg(self, clone, **over):
        cfg, _ = self.chain(workdir=str(clone), base_branch="main", **over)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="done")}})
        self.alive = False
        return cfg

    def test_dirty_workdir_refuses_the_next_wave(self):
        _, clone = self.repos()
        cfg = self.next_wave_cfg(clone)
        (clone / "junk.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            wab.launch(cfg, "W2", self.prompt())
        self.assertIn("not clean", str(ctx.exception))
        self.assertEqual(self.get_state(cfg)["current"], "W1")

    def test_untracked_file_refuses_even_with_show_untracked_files_off(self):  # #36 defect 7
        _, clone = self.repos()
        cfg = self.next_wave_cfg(clone)
        self.git(clone, "config", "status.showUntrackedFiles", "no")
        (clone / "junk.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            wab.launch(cfg, "W2", self.prompt())
        self.assertIn("not clean", str(ctx.exception))
        self.assertEqual(self.get_state(cfg)["current"], "W1")

    def test_clean_workdir_goes_detached_to_origin_base(self):
        origin, clone = self.repos()
        self.git(clone, "checkout", "-q", "-b", "wave-1")
        (clone / "b.txt").write_text("b\n", encoding="utf-8")
        self.git(clone, "add", "b.txt")
        self.git(clone, "commit", "-q", "-m", "wave 1")
        self.git(clone, "push", "-q", "origin", "HEAD:main")  # merged by the coordinator
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
        (other / "c.txt").write_text("c\n", encoding="utf-8")
        self.git(other, "add", "c.txt")
        self.git(other, "commit", "-q", "-m", "later")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        want = self.git(other, "rev-parse", "HEAD")
        cfg = self.next_wave_cfg(clone)
        self.assertTrue(wab.launch(cfg, "W2", self.prompt()))
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), want)
        self.assertEqual(subprocess.run(["git", "-C", str(clone), "symbolic-ref", "-q", "HEAD"],
                                        capture_output=True).returncode, 1)  # detached

    def gh_shim(self, body):
        """A fake `gh` first in PATH: no real GitHub call. `body` is the shell text it runs."""
        d = self.tmp / "ghshim"
        d.mkdir(exist_ok=True)
        f = d / "gh"
        f.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        f.chmod(0o755)
        p = mock.patch.dict(os.environ, {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"})
        p.start()
        self.addCleanup(p.stop)
        self.gh_real = True

    def wave1_branch(self, clone):
        self.git(clone, "checkout", "-q", "-b", "wave-1")
        (clone / "b.txt").write_text("b\n", encoding="utf-8")
        self.git(clone, "add", "b.txt")
        self.git(clone, "commit", "-q", "-m", "wave 1")

    def squash_into_origin(self, origin):
        """The coordinator's `gh pr merge --squash`: a NEW commit on base, wave-1 is no ancestor."""
        other = self.tmp / "sq"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
        (other / "b.txt").write_text("b\n", encoding="utf-8")
        self.git(other, "add", "b.txt")
        self.git(other, "commit", "-q", "-m", "wave 1 (squash)")
        self.git(other, "push", "-q", "origin", "HEAD:main")
        return self.git(other, "rev-parse", "HEAD")

    def test_squash_merged_previous_wave_lets_the_next_one_start(self):
        origin, clone = self.repos()
        self.wave1_branch(clone)
        sq = self.squash_into_origin(origin)
        self.gh_shim(f'echo \'{{"state":"MERGED","mergeCommit":{{"oid":"{sq}"}}}}\'')
        cfg = self.next_wave_cfg(clone)
        self.assertTrue(wab.launch(cfg, "W2", self.prompt()))
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), sq)

    def test_previous_pr_not_merged_refuses_the_next_wave(self):
        _, clone = self.repos()
        self.wave1_branch(clone)
        head = self.git(clone, "rev-parse", "HEAD")
        self.gh_shim('echo \'{"state":"OPEN","mergeCommit":null}\'')
        cfg = self.next_wave_cfg(clone)
        with self.assertRaises(SystemExit) as ctx:
            wab.launch(cfg, "W2", self.prompt())
        self.assertIn("previous wave not merged", str(ctx.exception))
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), head)  # nothing was switched
        self.assertEqual(self.get_state(cfg)["current"], "W1")

    def test_merge_commit_missing_from_origin_base_refuses(self):
        _, clone = self.repos()
        self.wave1_branch(clone)
        self.gh_shim('echo \'{"state":"MERGED","mergeCommit":{"oid":"' + "e" * 40 + '"}}\'')
        cfg = self.next_wave_cfg(clone)
        with self.assertRaises(SystemExit) as ctx:
            wab.launch(cfg, "W2", self.prompt())
        self.assertIn("previous wave not merged", str(ctx.exception))

    def test_unverifiable_previous_wave_continues_with_an_event(self):
        _, clone = self.repos()
        self.wave1_branch(clone)
        self.gh_shim("echo gh: no network >&2; exit 1")
        cfg = self.next_wave_cfg(clone)
        self.assertTrue(wab.launch(cfg, "W2", self.prompt()))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("merge of previous wave not verified (W5)", log)

    def test_first_wave_does_not_touch_the_checkout(self):
        _, clone = self.repos()
        (clone / "junk.txt").write_text("x\n", encoding="utf-8")
        cfg, _ = self.chain(workdir=str(clone), base_branch="main")
        self.alive = False
        self.assertTrue(wab.launch(cfg, "W1", self.prompt()))
        self.assertTrue((clone / "junk.txt").exists())

    def test_base_branch_must_be_a_string(self):
        for bad in (5, ["main"], ""):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.chain(workdir=str(self.tmp), base_branch=bad)

    # ----- 5: the handoff command in the docs is the one the runtime prints -----
    def test_docs_handoff_command_is_the_absolute_python_call(self):
        text = (ROOT / "skills" / "superarmanda" / "references" / "waves.md").read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if "launch <chain.json> <следующая-волна>" in l]
        self.assertTrue(lines)
        for l in lines:
            self.assertTrue(l.startswith("python3 <"), l)
            self.assertNotRegex(l, r"&& wab\.py")
        self.assertRegex(text, r"wab\.py done")
        self.assertIn("base_branch", text)

    # ----- 6: dash-separated opaque tokens -----
    def test_base64url_token_with_dashes_is_masked(self):
        tok = "abcD3-fgh8_ijkL-mnoP4-qrs9_tuvW-xyzA5-bcd2"
        self.assertGreaterEqual(len(tok), 40)
        for text in (tok, f"token is {tok} ok", f"x {tok}"):
            with self.subTest(text=text):
                out = wab.redact(text)
                self.assertNotIn("abcD3", out)
                self.assertNotIn("xyzA5", out)
                self.assertIn("[скрыто]", out)
        for limit in (600, 1200):
            self.assertLessEqual(len(wab.redact(tok * 300, limit)), limit)

    def test_ordinary_dashed_words_and_paths_are_not_masked(self):
        keep = ["feat/waves-dispatcher", "skills/superarmanda/scripts/waves/wab.py",
                "superarmanda-waves-dispatcher-and-dashboard",
                "skills/superarmanda/references/review-contract.md",
                "docs/specs/2026-09-12-remove-external-review-and-more",
                "tests/helpers/superarmanda_waves_test.py",
                "commit " + "3c71c34b" + "0" * 32]
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text), text)


class W4Round26(Base):
    """Round 26: the mandate pin is 64 lowercase hex or nothing, an unreadable mandate.md is a clear
    refusal, a confirmed handoff leaves the outbox, a corrupt `attempts` does not break the dashboard."""

    def setUp(self):
        super().setUp()
        self.down = False

        def flaky(cfg, text):
            if self.down:
                raise OSError("telegram down")
            self.tg.append(text)
        p = mock.patch.object(wab, "_send_telegram", side_effect=flaky)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)

    def raw_chain(self, **over):
        doc = {"chain": CHAIN, "run_id": RUN_ID, "repo": "o/r", "waves": ["W1", "W2"],
               "tmux_prefix": "wv-", "tick_seconds": 1}
        doc.update(over)
        cfgdir = self.tmp / "cfg"
        cfgdir.mkdir(exist_ok=True)
        path = cfgdir / "chain.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    # ----- 1: a present mandate_sha256 is 64 lowercase hex, never a falsy "no pin" -----
    def test_present_pin_must_be_64_lowercase_hex(self):
        for bad in ("", None, False, 0, 1, True, [], {}, "A" * 64, "0" * 63, "0" * 65, "g" * 64,
                    " " + "0" * 63, hashlib.sha256(b"x").hexdigest().upper()):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as cm:
                    wab.load_chain(self.raw_chain(mandate_sha256=bad))
                self.assertIn("mandate_sha256", str(cm.exception))
        wab.load_chain(self.raw_chain(mandate_sha256=hashlib.sha256(b"x").hexdigest()))
        cfg = wab.load_chain(self.raw_chain())  # no key: no pin, the old behaviour
        self.assertNotIn("mandate_sha256", cfg)

    def test_pin_is_checked_by_presence_not_truthiness(self):
        # load_chain is the gate; system_prompt must still refuse a falsy pin that slipped past it
        for bad in ("", None, False, 0):
            with self.subTest(bad=bad):
                cfg, _ = self.chain()
                cfg["mandate_sha256"] = bad
                with self.assertRaises(SystemExit) as cm:
                    wab.system_prompt(cfg)
                self.assertIn("mandate_sha256", str(cm.exception))
                self.assertFalse((cfg["run_dir"] / "system-prompt.md").exists())

    # ----- 3: an unreadable mandate.md is a refusal with a reason, not a traceback -----
    def unreadable(self, cfg, how):
        mandate = cfg["run_dir"] / "mandate.md"
        if how == "directory":
            mandate.mkdir()
        else:
            mandate.write_bytes(f"Прогон: {RUN_ID}\nx\n".encode("utf-8"))
            mandate.chmod(0)
            self.addCleanup(mandate.chmod, 0o600)

    def test_unreadable_mandate_is_a_clear_refusal(self):
        for how in ("directory", "chmod 0"):
            if how == "chmod 0" and os.geteuid() == 0:
                continue  # root reads it anyway
            for pin in (None, hashlib.sha256(b"x").hexdigest()):
                with self.subTest(how=how, pin=bool(pin)):
                    shutil.rmtree(self.tmp / "cfg", ignore_errors=True)
                    cfg, _ = self.chain(mandate_sha256=pin)
                    self.unreadable(cfg, how)
                    with self.assertRaises(SystemExit) as cm:
                        wab.system_prompt(cfg)
                    self.assertIn("mandate.md", str(cm.exception))
                    self.assertIn("cannot read", str(cm.exception))
                    self.assertFalse((cfg["run_dir"] / "system-prompt.md").exists())

    # ----- 2: a confirmed handoff is not delivered later as a stale notice -----
    def handoff_undelivered(self, **over):
        cfg, path = self.chain(merge_gate="external", **over)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.down = True
        wab.watch(cfg, path, max_ticks=3)
        self.assertIn("handoff", self.get_state(cfg)["waves"]["W1"].get("outbox", {}))
        return cfg, path

    def test_launch_of_the_next_wave_drops_the_undelivered_handoff(self):
        cfg, path = self.handoff_undelivered()
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W2", wab.wave_dir(cfg, "W1") / "next-prompt.md"))
        st = self.get_state(cfg)
        self.assertEqual(st["waves"]["W1"]["phase"], "done")
        self.assertNotIn("handoff", st["waves"]["W1"].get("outbox") or {})
        self.down = False
        wab.drain_notices(cfg)
        self.assertEqual([t for t in self.tg if "сдала PR" in t], [])

    def test_done_drops_the_undelivered_handoff_but_keeps_chain_done(self):
        cfg, path = self.handoff_undelivered(waves=["W1"])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        self.alive = False  # the window obeys /exit (a window still open makes done exit 3, round 35)
        wab.main(["wab.py", "done", str(path), "W1"])  # Telegram still down
        box = self.get_state(cfg)["waves"]["W1"].get("outbox") or {}
        self.assertNotIn("handoff", box)
        self.assertIn("chain_done", box)  # the end of the chain must still get through
        self.down = False
        wab.main(["wab.py", "done", str(path)])
        self.assertEqual([t for t in self.tg if "сдала PR" in t], [])
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1, self.tg)
        self.assertFalse(self.get_state(cfg)["waves"]["W1"].get("outbox"))

    # ----- 4: a corrupt `attempts` in state.json does not crash the dashboard -----
    def test_wave_stats_skip_corrupt_attempts(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.transcript("cur1", [asst(inp=1, out=5, tools=2)])
        self.transcript("old1", [asst(inp=1, out=40, tools=3)])
        good = self.wave_rec("W1", sessions=["old1"])
        for bad, want in (("junk", 5), ({"sessions": ["old1"]}, 5), (7, 5),
                          ([None, 3, "x", good], 45), ([[good]], 5)):
            with self.subTest(attempts=bad):
                w = self.wave_rec("W1", sessions=["cur1"], attempts=bad)
                dash.CACHE = wab.TranscriptCache()
                self.assertEqual(dash.wave_stats(w)["out"], want)


class W4Round27(Base):
    """Round 27: every notice about a wave goes through the outbox (not_ready and «стартовала» too),
    and a wave confirmed by the coordinator (launch of the next wave, restart of this one, `done`)
    loses all its undelivered notices about the past state except «цепочка завершена»."""

    def setUp(self):
        super().setUp()
        self.down = False
        self.fail_left = 0

        def flaky(cfg, text):
            if self.down or self.fail_left > 0:
                self.fail_left = max(0, self.fail_left - 1)
                raise OSError("telegram down")
            self.tg.append(text)
        p = mock.patch.object(wab, "_send_telegram", side_effect=flaky)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def item(text):
        return {"value": "1", "text": text, "next_at": 0}

    def launch(self, cfg, wave, prompt):
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            return wab.launch(cfg, wave, prompt)

    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("do it\n", encoding="utf-8")
        return p

    # ----- A: an undelivered «следующая волна не запущена» is stale once the next wave is launched -----
    def test_launch_after_a_stop_without_next_drops_the_stale_no_next(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.down = True
        self.assertFalse(wab.watch(cfg, path, max_ticks=3))
        st = self.get_state(cfg)
        self.assertIsNone(st["current"])
        self.assertIn("no_next", st["waves"]["W1"].get("outbox", {}))
        # the operator writes next-prompt.md and launches W2 by hand
        nxt = wab.wave_dir(cfg, "W1") / "next-prompt.md"
        nxt.write_text("go\n", encoding="utf-8")
        self.alive = False
        self.down = False
        self.assertTrue(self.launch(cfg, "W2", nxt))
        st = self.get_state(cfg)
        self.assertNotIn("stopped", st)
        self.assertFalse(st["waves"]["W1"].get("outbox"))
        self.alive = True
        self.set_status(cfg, "W2", "RUNNING")
        wab.watch(cfg, path, max_ticks=1)
        wab.drain_notices(cfg)
        self.assertEqual([t for t in self.tg if "не запускаю" in t], [], self.tg)
        self.assertEqual(len([t for t in self.tg if "стартовала волна W2" in t]), 1, self.tg)

    # ----- B: not_ready goes through the outbox and is delivered exactly once after the failure -----
    def test_not_ready_survives_a_telegram_failure(self):
        cfg, _ = self.chain()
        self.alive = False
        self.ready = False
        self.down = True
        self.assertFalse(self.launch(cfg, "W1", self.prompt()))
        box = self.get_state(cfg)["waves"]["W1"].get("outbox", {})
        self.assertIn("not_ready", box)
        self.assertEqual(self.tg, [])
        self.down = False
        wab.drain_notices(cfg)
        wab.drain_notices(cfg)
        self.assertEqual(len([t for t in self.tg if "не стало готовым" in t]), 1, self.tg)
        self.assertFalse(self.get_state(cfg)["waves"]["W1"].get("outbox"))

    def test_cli_launch_drains_before_it_exits_nonzero(self):
        cfg, path = self.chain()
        self.alive = False
        self.ready = False
        self.fail_left = 1  # the first attempt fails, the drain gets it through
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(SystemExit) as cm:
                wab.main(["wab.py", "launch", str(path), "W1", str(self.prompt())])
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(len([t for t in self.tg if "не стало готовым" in t]), 1, self.tg)
        self.assertFalse(self.get_state(cfg)["waves"]["W1"].get("outbox"))

    def test_start_notice_survives_a_telegram_failure(self):
        cfg, path = self.chain()
        self.alive = False
        self.down = True
        self.assertTrue(self.launch(cfg, "W1", self.prompt()))
        self.assertIn("started", self.get_state(cfg)["waves"]["W1"].get("outbox", {}))
        self.down = False
        self.alive = True
        self.set_status(cfg, "W1", "RUNNING")
        st = self.get_state(cfg)
        st["waves"]["W1"]["outbox"]["started"]["next_at"] = 0
        self.put_state(cfg, st)
        wab.watch(cfg, path, max_ticks=2)
        self.assertEqual(len([t for t in self.tg if "стартовала волна W1" in t]), 1, self.tg)

    def test_only_flush_and_the_manual_command_call_notify(self):
        import ast
        tree = ast.parse((WAVES / "wab.py").read_text(encoding="utf-8"))
        callers = set()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "notify":
                        callers.add(fn.name)
        self.assertEqual(callers, {"flush_notices", "main"})

    # ----- 2: a restart of the same wave acknowledges its old notices -----
    def test_restart_drops_the_old_dead_and_not_ready_but_keeps_chain_done(self):
        cfg, _ = self.chain()
        box = {k: self.item(f"old {k}") for k in ("dead", "not_ready", "idle", "permission", "chain_done")}
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead", outbox=box)}})
        self.alive = False
        self.down = True
        self.assertTrue(self.launch(cfg, "W1", self.prompt()))
        st = self.get_state(cfg)
        rec = st["waves"]["W1"]
        self.assertEqual(set(rec.get("outbox", {})) - {"started"}, {"chain_done"})
        for old in rec["attempts"]:
            self.assertNotIn("outbox", old)  # an attempt is history, not a second outbox
        self.down = False
        wab.drain_notices(cfg)
        self.assertEqual(sorted(t for t in self.tg if t.startswith("demo: old")), ["demo: old chain_done"])

    # ----- 2: only the confirmed wave is acknowledged; another wave keeps its notices -----
    def test_unconfirmed_waves_keep_their_notices(self):
        cfg, _ = self.chain(waves=["W1", "W2", "W3"], merge_gate="external")
        # a notice that only a confirmation ends («волна завершена»): idle of a finished wave is
        # stale by itself since round 29 (NOTICE_EPISODE_ENDS), so it would not test the ack
        w1 = self.wave_rec("W1", phase="done", outbox={"done": self.item("W1 done")})
        w2 = self.wave_rec("W2", phase="awaiting_merge",
                           outbox={"handoff": self.item("W2 handoff"), "permission": self.item("W2 perm"),
                                   "chain_done": self.item("W2 chain_done")})
        self.put_state(cfg, {"current": "W2", "waves": {"W1": w1, "W2": w2}})
        self.alive = False
        self.down = True
        self.assertTrue(self.launch(cfg, "W3", self.prompt()))
        st = self.get_state(cfg)
        self.assertEqual(set(st["waves"]["W1"].get("outbox", {})), {"done"})
        self.assertEqual(set(st["waves"]["W2"].get("outbox", {})), {"chain_done"})

    def test_done_command_acknowledges_every_episode_but_chain_done(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        box = {k: self.item(f"old {k}") for k in ("handoff", "idle", "permission", "checkpoint_timeout")}
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge", outbox=box)}})
        self.down = True
        wab.main(["wab.py", "done", str(path), "W1"])
        self.assertEqual(set(self.get_state(cfg)["waves"]["W1"].get("outbox", {})), {"chain_done"})
        self.down = False
        wab.main(["wab.py", "done", str(path)])
        self.assertEqual([t for t in self.tg if t.startswith("old")], [])
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1, self.tg)

    def test_every_confirmation_point_uses_one_function(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        self.assertNotIn('drop_notice(st["waves"][prev], "handoff")', src)
        self.assertNotIn('drop_notice(w, "handoff")', src)
        self.assertGreaterEqual(src.count("ack_wave_notices("), 4)  # def + launch prev + restart + done

    # ----- 3: a corrupt `attempts` does not break a restart -----
    def test_restart_with_corrupt_attempts(self):
        good = {"phase": "dead", "sessions": ["s0"]}
        for bad, kept in (("junk", 0), (7, 0), ({"x": 1}, 0), ([None, "x", 3, good], 1)):
            with self.subTest(attempts=bad):
                cfg, _ = self.chain()
                self.put_state(cfg, {"current": "W1",
                                     "waves": {"W1": self.wave_rec(phase="dead", attempts=bad)}})
                self.alive = False
                self.assertTrue(self.launch(cfg, "W1", self.prompt()))
                attempts = self.get_state(cfg)["waves"]["W1"]["attempts"]
                self.assertIsInstance(attempts, list)
                self.assertTrue(all(isinstance(a, dict) for a in attempts))
                self.assertEqual(len(attempts), kept + 1)
                (wab.wave_dir(cfg, "W1") / "status").unlink()


class _Crash(BaseException):
    """The dispatcher process dies right here (not an Exception: nothing in wab may swallow it)."""


class W4Round28(Base):
    """Round 28: a phase change (or an «already notified» mark) and its notice in the outbox are
    saved by ONE save_state. A process killed right after that save still owes the notice, and the
    next `watch` / `done` delivers it exactly once."""

    def setUp(self):
        super().setUp()
        self.crash_when = None
        real = load_orig("wab_r28_orig").save_state

        def crashing(cfg, st):
            real(cfg, st)
            if self.crash_when and self.crash_when(st):
                self.crash_when = None
                raise _Crash()
        p = mock.patch.object(wab, "save_state", side_effect=crashing)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "prepare_clone", return_value=self.cwd)
        p.start()
        self.addCleanup(p.stop)

    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("do it\n", encoding="utf-8")
        return p

    def count(self, needle):
        return len([t for t in self.tg if needle in t])

    def crash(self, fn, *a, **kw):
        with self.assertRaises(_Crash):
            fn(*a, **kw)
        self.assertIsNone(self.crash_when, "the crash point was never saved")
        self.assertEqual(self.tg, [])

    def test_dead(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        self.alive = False
        self.crash_when = lambda st: st["waves"]["W1"].get("phase") == "dead"
        self.crash(wab.watch, cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.count("окно волны W1 закрылось"), 1, self.tg)

    def test_started(self):
        cfg, path = self.chain()
        self.alive = False
        self.crash_when = lambda st: st["waves"]["W1"].get("notified", {}).get("started") == "1"
        self.crash(wab.launch, cfg, "W1", self.prompt())
        self.alive = True
        self.set_status(cfg, "W1", "RUNNING")
        wab.watch(cfg, path, max_ticks=2)
        self.assertEqual(self.count("стартовала волна W1"), 1, self.tg)

    def test_not_ready(self):
        cfg, path = self.chain()
        self.alive = False
        self.ready = False
        self.crash_when = lambda st: st["waves"]["W1"].get("phase") == "not_ready"
        self.crash(wab.launch, cfg, "W1", self.prompt())
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.count("не стало готовым"), 1, self.tg)

    def test_handoff(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.crash_when = lambda st: st["waves"]["W1"].get("phase") == "awaiting_merge"
        self.crash(wab.watch, cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.count("сдала PR"), 1, self.tg)

    def test_no_next(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.crash_when = lambda st: st["waves"]["W1"].get("notified", {}).get("no_next") == "1"
        self.crash(wab.watch, cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.count("не запускаю"), 1, self.tg)

    def test_tick_done_before_the_next_launch(self):
        cfg, path = self.auto_chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.crash_when = lambda st: st["waves"]["W1"].get("notified", {}).get("done") == "1"
        self.crash(wab.watch, cfg, path, max_ticks=1)
        self.alive = False  # the old window closed; W2 gets a new one
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.get_state(cfg)["current"], "W2")
        self.assertEqual(self.count("волна W1 завершена"), 1, self.tg)

    def test_tick_chain_done(self):
        cfg, path = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        self.crash_when = lambda st: st.get("current") is None
        self.crash(wab.watch, cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.assertEqual(self.count("волна W1 завершена"), 1, self.tg)
        self.assertEqual(self.count("цепочка завершена"), 1, self.tg)

    def test_done_command(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.crash_when = lambda st: st["waves"]["W1"].get("phase") == "done"
        self.crash(wab.main, ["wab.py", "done", str(path), "W1"])
        wab.main(["wab.py", "done", str(path)])
        wab.main(["wab.py", "done", str(path)])
        self.assertEqual(self.count("цепочка завершена"), 1, self.tg)

    def test_blocked(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: need a decision")
        self.crash_when = lambda st: "blocked" in st["waves"]["W1"].get("notified", {})
        self.crash(wab.watch, cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=2)
        self.assertEqual(self.count("need a decision"), 1, self.tg)

    def test_no_save_between_a_mark_and_its_notice(self):
        """The rule in code: no save_state call sits between once_per/a phase change and the
        put_notice that belongs to it (put_notice itself never saves)."""
        import ast
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "put_notice")
        self.assertFalse([n for n in ast.walk(fn) if isinstance(n, ast.Call)
                          and getattr(n.func, "id", None) in ("save_state", "flush_notices")])
        self.assertNotIn("def queue_notice", src)
        self.assertNotIn("saved before any notice: at most once", src)


class DashboardBindingHint(Base):
    """Round 28 (P2): the Ctrl+\\ hint is shown only when this dashboard's binding really works."""

    def setUp(self):
        super().setUp()
        try:
            import dash  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        import dash
        self.dash = dash

    def first_frame(self, registered):
        from rich.console import Console
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        frames = []

        def live(renderable, **kw):
            buf = io.StringIO()
            Console(file=buf, width=200, force_terminal=False).print(renderable)
            frames.append(buf.getvalue())
            raise KeyboardInterrupt
        with mock.patch.object(sys, "argv", ["dash.py", str(path)]), \
                mock.patch.object(sys.stdout, "isatty", return_value=True), \
                mock.patch.object(self.dash.signal, "signal"), \
                mock.patch.object(self.dash, "own_session", return_value=("s", "privsock", "%1")), \
                mock.patch.object(wab, "register_dash", return_value=registered), \
                mock.patch.object(wab, "unregister_dash"), \
                mock.patch.object(self.dash, "Live", side_effect=live), \
                mock.patch.object(self.dash, "BINDING", None, create=True):
            with self.assertRaises(KeyboardInterrupt):
                self.dash.main()
        return frames[0]

    def test_refused_binding_is_not_advertised(self):
        text = self.first_frame(False)
        self.assertNotIn("открыть/закрыть волну", text)
        self.assertIn("W1", text)

    def test_working_binding_is_advertised(self):
        self.assertIn("открыть/закрыть волну", self.first_frame(True))


class Utf8(Base):
    def test_subprocess_and_files_are_explicitly_utf8(self):
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(a, 0, "", "")
        with mock.patch.object(wab.subprocess, "run", side_effect=fake_run):
            REAL_SH("echo")
        self.assertEqual(seen.get("encoding"), "utf-8")
        self.assertEqual(seen.get("errors"), "replace")
        cfg, _ = self.chain()
        wab.event(cfg, "кириллица ✓")
        raw = (cfg["run_dir"] / "events.log").read_bytes()
        self.assertIn("кириллица ✓".encode("utf-8"), raw)

    def test_the_whole_module_has_no_bare_text_io(self):
        for name in ("wab.py", "dash.py"):
            src = (WAVES / name).read_text(encoding="utf-8")
            for line in src.splitlines():
                if re.search(r"\.(read_text|write_text)\(", line):
                    self.assertIn("encoding", line, f"{name}: {line.strip()}")
            for line in src.splitlines():
                if re.search(r"(?<![A-Za-z_])open\(", line) and '"rb"' not in line:
                    self.assertIn("encoding", line, f"{name}: {line.strip()}")


# ---------------------------------------------------------------- A13
class Dashboard(Base):
    def setUp(self):
        super().setUp()
        try:
            import dash  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        import dash
        self.dash = dash
        self.assertEqual(Path(dash.__file__).resolve().parent, WAVES.resolve())

    def render_text(self, cfg):
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(self.dash.safe_render(cfg))
        return buf.getvalue()

    def test_corrupt_state_is_a_frame_not_an_exception(self):
        cfg, _ = self.chain()
        wab.state_path(cfg).write_text("{not json", encoding="utf-8")
        text = self.render_text(cfg)
        self.assertIn("кадр не отрисован", text)

    def test_missing_keys_do_not_raise(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": {"tmux": "wv-w1"}}})
        self.assertIn("кадр не отрисован", self.render_text(cfg))

    def test_healthy_frame_shows_the_hint_and_the_wave(self):
        cfg, _ = self.chain(titles={"W1": "first"})
        self.transcript("s1", [asst(inp=1000, out=50, tools=2)])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(sessions=["s1"])}})
        self.set_status(cfg, "W1", "RUNNING")
        with mock.patch.object(self.dash, "BINDING", True):  # the hint is shown only for a working binding
            text = self.render_text(cfg)
        self.assertIn("Ctrl+\\", text)
        self.assertIn("W1", text)

    def test_attach_hint_in_dashboard_names_the_private_socket(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        with mock.patch.object(wab, "TMUX_SOCKET", "sockd"):
            text = self.render_text(cfg)
        self.assertIn("-L sockd attach -t", text)

    def test_stats_per_session_with_subagents_and_incremental_reads(self):
        self.transcript("a1", [asst(inp=1, out=10, tools=1), asst(inp=2, out=5, tools=2)])
        self.transcript("b1", [asst(inp=1, out=99, tools=7)])
        sub = self.home / ".claude" / "projects" / sanitize(self.cwd) / "a1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(asst(inp=1, out=3, tools=4, side=True) + "\n", encoding="utf-8")
        wa = self.wave_rec("W1", sessions=["a1"])
        wb = self.wave_rec("W2", sessions=["b1"])
        self.dash.CACHE = wab.TranscriptCache()
        a = self.dash.wave_stats(wa)
        b = self.dash.wave_stats(wb)
        self.assertEqual((a["turns"], a["tools"], a["out"], a["agents"]), (2, 7, 18, 1))
        self.assertEqual((b["turns"], b["tools"], b["out"], b["agents"]), (1, 7, 99, 0))
        done = self.dash.CACHE.bytes_read
        self.dash.wave_stats(wa)
        self.assertEqual(self.dash.CACHE.bytes_read, done)

    def test_stats_sum_the_sessions_of_earlier_attempts_once(self):
        # A restarted wave keeps its earlier tries in `attempts` (each a flat copy of the old
        # record, with its own cwd and sessions): their work is the wave's work too.
        other = str(self.tmp / "old-clone")
        self.transcript("old1", [asst(inp=1, out=40, tools=3)], cwd=other)
        self.transcript("old2", [asst(inp=1, out=2, tools=1)])
        self.transcript("cur1", [asst(inp=1, out=5, tools=2)])
        w = self.wave_rec("W1", sessions=["cur1"])
        w["attempts"] = [dict(self.wave_rec("W1", sessions=["old1"]), cwd=other),
                         self.wave_rec("W1", sessions=["old2", "cur1"])]
        self.dash.CACHE = wab.TranscriptCache()
        s = self.dash.wave_stats(w)
        self.assertEqual((s["turns"], s["tools"], s["out"]), (3, 6, 47))

    def test_without_a_tty_it_refuses_to_draw(self):
        cfg, path = self.chain()
        proc = subprocess.run([sys.executable, str(WAVES / "dash.py"), str(path)],
                              capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
                              timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tty", proc.stderr)

    def test_rich_is_imported_by_dash_only(self):
        self.assertNotRegex((WAVES / "wab.py").read_text(encoding="utf-8"), r"(?m)^\s*(import|from)\s+rich")
        self.assertIn("from rich", (WAVES / "dash.py").read_text(encoding="utf-8"))


class W4Round29EpisodeEnds(Base):
    """Round 29: a notice about an episode leaves the outbox when its episode is over, wherever
    that happens. For every key: Telegram down -> the notice waits in the outbox -> the episode
    ends -> Telegram is back -> the stale notice is NOT sent; and the control: while the episode
    lasts, the notice is delivered exactly once."""

    def setUp(self):
        super().setUp()
        self.down = True

        def flaky(cfg, text):
            if self.down:
                raise OSError("telegram down")
            self.tg.append(text)
        p = mock.patch.object(wab, "_send_telegram", side_effect=flaky)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)
        self.cfg, self.path = self.chain()

    # ----- helpers -----
    def state(self, **rec):
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(**rec)}})

    def tick(self):
        return wab.tick(self.cfg, wab.load_state(self.cfg))

    def box(self):
        return self.get_state(self.cfg)["waves"]["W1"].get("outbox") or {}

    def queued(self, key):
        self.assertIn(key, self.box())
        self.assertEqual(self.tg, [])

    def restore(self):
        self.down = False
        wab.drain_notices(self.cfg)
        wab.drain_notices(self.cfg)

    def count(self, part):
        return len([t for t in self.tg if part in t])

    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("do it\n", encoding="utf-8")
        return p

    def launch(self, wave="W1", prompt=None):
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            return wab.launch(self.cfg, wave, prompt or self.prompt())

    # ----- the table covers every key -----
    def test_every_put_notice_key_has_an_episode_end(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'put_notice\(\s*\w+\s*,\s*"([a-z_]+)"', src))
        self.assertGreaterEqual(len(keys), 16, keys)
        self.assertEqual(keys, set(wab.NOTICE_EPISODE_ENDS), "a new notice key needs an episode end")
        for key in wab.KEEP_ON_ACK:
            self.assertIsNone(wab.NOTICE_EPISODE_ENDS[key])

    def test_a_put_with_a_dynamic_key_is_not_allowed(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        calls = re.findall(r"(?<!def )put_notice\(([^,]+),\s*([^,]+),", src)
        for _, key in calls:
            self.assertRegex(key.strip(), r'^"[a-z_]+"$', "put_notice key must be a literal")

    # ----- checkpoint_timeout: ends when HANDOFF_READY is taken (clear + update) -----
    def checkpoint_timeout(self):
        self.state(phase="checkpoint", checkpoint_sent=True, checkpoint_at=time.time() - 3600)
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.queued("checkpoint_timeout")

    def test_checkpoint_timeout_is_stale_after_the_handoff_went_through(self):
        self.checkpoint_timeout()
        self.set_status(self.cfg, "W1", "HANDOFF_READY")
        self.tick()
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["phase"], "running")
        self.restore()
        self.assertEqual(self.count("не записала handoff"), 0, self.tg)

    def test_checkpoint_timeout_is_delivered_once_while_the_handoff_is_missing(self):
        self.checkpoint_timeout()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("не записала handoff"), 1, self.tg)

    # ----- updating: ends when the new session is bound by its marker (/update arrived) -----
    def updating(self):
        self.state(phase="updating", sessions=["s1"])
        self.set_status(self.cfg, "W1", "HANDOFF_READY")
        self.tick()
        self.queued("updating")

    def test_updating_is_stale_once_the_new_session_is_bound(self):
        self.updating()
        self.transcript("s2", [asst(inp=1)], marker=wab.session_marker(self.cfg, "W1"))
        self.tick()
        self.assertFalse(self.get_state(self.cfg)["waves"]["W1"].get("await_session"))
        self.restore()
        self.assertEqual(self.count("при передаче /superarmanda --resume"), 0, self.tg)

    def test_updating_is_delivered_once_while_unknown(self):
        self.updating()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("при передаче /superarmanda --resume"), 1, self.tg)

    # ----- sending: ends when the wave writes its own status (the task arrived) -----
    def sending(self):
        self.state(phase="sending", sessions=["s1"], prompt_file=str(self.prompt()))
        self.set_status(self.cfg, "W1", "STARTING")
        self.tick()
        self.queued("sending")

    def test_sending_is_stale_once_the_wave_writes_its_status(self):
        self.sending()
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.restore()
        self.assertEqual(self.count("при отправке задачи"), 0, self.tg)

    def test_sending_is_delivered_once_while_unknown(self):
        self.sending()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("при отправке задачи"), 1, self.tg)

    # ----- no_prompt: ends when the task is sent after all -----
    def no_prompt(self):
        missing = self.tmp / "gone.md"
        self.state(phase="starting", sessions=["s1"], prompt_file=str(missing))
        self.set_status(self.cfg, "W1", "STARTING")
        self.tick()
        self.queued("no_prompt")
        return missing

    def test_no_prompt_is_stale_once_the_task_is_sent(self):
        missing = self.no_prompt()
        missing.write_text("task\n", encoding="utf-8")
        self.tick()
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["phase"], "running")
        self.restore()
        self.assertEqual(self.count("файла с задачей уже нет"), 0, self.tg)

    def test_no_prompt_is_delivered_once_while_the_file_is_missing(self):
        self.no_prompt()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("файла с задачей уже нет"), 1, self.tg)

    # ----- not_ready: ends with a relaunch of the wave -----
    def not_ready(self):
        self.alive = False
        self.ready = False
        self.assertFalse(self.launch())
        self.queued("not_ready")

    def test_not_ready_is_stale_after_a_relaunch(self):
        self.not_ready()
        self.ready = True
        self.assertTrue(self.launch())
        self.restore()
        self.assertEqual(self.count("не стало готовым"), 0, self.tg)

    def test_not_ready_is_delivered_once_while_the_wave_stands(self):
        self.not_ready()
        self.restore()
        self.restore()
        self.assertEqual(self.count("не стало готовым"), 1, self.tg)

    # ----- started: ends when the window closes (or the wave finishes) -----
    def started(self):
        self.alive = False
        self.assertTrue(self.launch())
        self.queued("started")
        self.alive = True
        self.set_status(self.cfg, "W1", "RUNNING")

    def test_started_is_stale_once_the_window_closed(self):
        self.started()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("стартовала волна W1"), 0, self.tg)
        self.assertEqual(self.count("окно волны W1 закрылось"), 1, self.tg)

    def test_started_is_delivered_once_while_the_wave_runs(self):
        self.started()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("стартовала волна W1"), 1, self.tg)

    # ----- dead: ends when the window is back -----
    def dead(self):
        self.state(sessions=["s1"])
        self.set_status(self.cfg, "W1", "RUNNING")
        self.alive = False
        self.tick()
        self.queued("dead")

    def test_dead_is_stale_once_the_window_is_back(self):
        self.dead()
        self.alive = True
        wab.resume(self.cfg, wab.load_state(self.cfg))
        self.restore()
        self.assertEqual(self.count("закрылось"), 0, self.tg)

    def test_dead_is_delivered_once_while_the_window_is_gone(self):
        self.dead()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("закрылось"), 1, self.tg)

    # ----- tmux_failed: ends on the next successful action or when the window is gone -----
    def tmux_failed(self):
        def boom(name, text, **kw):
            raise subprocess.CalledProcessError(1, "tmux")
        self.state(phase="checkpoint", checkpoint_sent=False, checkpoint_at=time.time(), sessions=["s1"])
        self.set_status(self.cfg, "W1", "RUNNING")
        with mock.patch.object(wab, "send_text", side_effect=boom):
            self.tick()
        self.queued("tmux_failed")

    def test_tmux_failed_is_stale_once_the_window_closed(self):
        self.tmux_failed()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("не удалось выполнить"), 0, self.tg)
        self.assertEqual(self.count("закрылось"), 1, self.tg)

    def test_tmux_failed_is_stale_after_a_successful_retry(self):
        self.tmux_failed()
        self.tick()
        self.assertTrue(self.get_state(self.cfg)["waves"]["W1"]["checkpoint_sent"])
        self.restore()
        self.assertEqual(self.count("не удалось выполнить"), 0, self.tg)

    def test_tmux_failed_is_delivered_once_while_it_fails(self):
        self.tmux_failed()
        self.restore()
        self.restore()
        self.assertEqual(self.count("не удалось выполнить"), 1, self.tg)

    # ----- blocked / permission / idle / auto_off: end on screen, and when the window is gone -----
    def blocked(self):
        self.state(sessions=["s1"])
        self.set_status(self.cfg, "W1", "BLOCKED: question")
        self.tick()
        self.queued("blocked")

    def test_blocked_is_stale_once_answered(self):
        self.blocked()
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт тебя"), 0, self.tg)

    def test_blocked_is_stale_once_the_window_closed(self):
        self.blocked()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт тебя"), 0, self.tg)

    def test_blocked_is_delivered_once_while_it_waits(self):
        self.blocked()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт тебя"), 1, self.tg)

    def permission(self):
        self.state(sessions=["s1"])
        self.set_status(self.cfg, "W1", "RUNNING")
        self.pane = "Do you want to proceed?\n❯ 1. Yes"
        self.tick()
        self.queued("permission")

    def test_permission_is_stale_once_the_window_closed(self):
        self.permission()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт подтверждения"), 0, self.tg)

    def test_permission_is_stale_once_the_prompt_left_the_screen(self):
        self.permission()
        self.pane = "working"
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт подтверждения"), 0, self.tg)

    def test_permission_is_delivered_once_while_on_screen(self):
        self.permission()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт подтверждения"), 1, self.tg)

    def idle(self):
        self.cfg, self.path = self.chain(idle_minutes=0)
        self.state(sessions=["s1"], pane_digest="x", pane_changed=time.time() - 60)
        self.set_status(self.cfg, "W1", "RUNNING")
        self.pane = "a\nb\nc\nd"
        self.tick()  # a new digest: the clock restarts
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["pane_changed"] = time.time() - 60
        wab.save_state(self.cfg, st)
        self.tick()
        self.queued("idle")

    def test_idle_is_stale_once_the_window_closed(self):
        self.idle()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("молчит"), 0, self.tg)

    def test_idle_is_delivered_once_while_silent(self):
        self.idle()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("молчит"), 1, self.tg)

    def auto_off(self):
        self.state(sessions=["s1"])
        self.set_status(self.cfg, "W1", "RUNNING")
        self.pane = AutoGuard.OFF
        self.tick()
        self.tick()
        self.queued("auto_off")

    def test_auto_off_is_stale_once_the_window_closed(self):
        self.auto_off()
        self.alive = False
        self.tick()
        self.restore()
        self.assertEqual(self.count("вышла из режима auto"), 0, self.tg)

    def test_auto_off_is_delivered_once_while_off(self):
        self.auto_off()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("вышла из режима auto"), 1, self.tg)

    # ----- round 37: a screen episode ends on screen even while the wave is BLOCKED -----
    # The BLOCKED branch of the tick returns before the normal screen checks; a screen notice
    # stuck in the outbox (Telegram down) must not go out once its prompt / silence / plan mode
    # left the screen, while the BLOCKED notice itself goes out exactly once.
    def blocked_after(self, pane):
        self.pane = pane
        self.set_status(self.cfg, "W1", "BLOCKED: question")
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт тебя"), 1, self.tg)

    def test_permission_is_stale_once_the_prompt_left_while_blocked(self):
        self.permission()
        self.blocked_after("working")
        self.assertEqual(self.count("ждёт подтверждения"), 0, self.tg)

    def test_idle_is_stale_once_the_pane_moved_while_blocked(self):
        self.idle()
        self.blocked_after("e\nf\ng\nh\ni")
        self.assertEqual(self.count("молчит"), 0, self.tg)

    def test_auto_off_is_stale_once_auto_mode_is_back_while_blocked(self):
        self.auto_off()
        self.blocked_after(AutoGuard.ON)
        self.assertEqual(self.count("вышла из режима auto"), 0, self.tg)

    def test_permission_still_on_screen_while_blocked_is_delivered_once(self):
        self.permission()
        self.blocked_after(self.pane)
        self.tick()
        self.restore()
        self.assertEqual(self.count("ждёт подтверждения"), 1, self.tg)
        self.assertEqual(self.count("ждёт тебя"), 1, self.tg)

    def test_every_screen_episode_has_a_screen_end(self):
        self.assertEqual(set(wab.SCREEN_EPISODE_ENDS), {"permission", "idle", "auto_off"})
        self.assertLessEqual(set(wab.SCREEN_EPISODE_ENDS), set(wab.NOTICE_EPISODE_ENDS))

    # ----- handoff: ends when the wave leaves awaiting_merge (the coordinator confirmed it) -----
    def test_handoff_is_stale_once_the_wave_left_awaiting_merge(self):
        w = self.wave_rec(phase="done", outbox={"handoff": {"value": "1", "text": "old handoff", "next_at": 0}})
        self.put_state(self.cfg, {"current": None, "waves": {"W1": w}})
        self.restore()
        self.assertEqual(self.count("old handoff"), 0, self.tg)

    def test_handoff_is_delivered_once_while_awaiting_merge(self):
        self.cfg, self.path = self.chain(merge_gate="external")
        self.state(sessions=["s1"])
        self.set_status(self.cfg, "W1", "DONE")
        (wab.wave_dir(self.cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.assertFalse(self.tick())
        self.queued("handoff")
        self.restore()
        self.restore()
        self.assertEqual(self.count("сдала PR"), 1, self.tg)

    # ----- done / no_next / chain_done: only the coordinator's confirmation ends them -----
    def test_done_and_no_next_wait_for_the_confirmation_and_go_out_once(self):
        box = {k: {"value": "1", "text": f"old {k}", "next_at": 0} for k in ("done", "no_next", "chain_done")}
        self.put_state(self.cfg, {"current": None, "waves": {"W1": self.wave_rec(phase="done", outbox=box)}})
        self.restore()
        self.restore()
        self.assertEqual(sorted(self.tg), ["demo: old chain_done", "demo: old done", "demo: old no_next"])


class W4Round37DashTerminalPhase(Base):
    """Round 37: a terminal phase (the window is gone or the wave is handed over) wins over a
    stale status in the file: no «ждёт тебя» and no attach command for a closed window."""

    def setUp(self):
        super().setUp()
        try:
            import dash  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        import dash
        self.dash = dash

    def render_text(self, cfg):
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(self.dash.safe_render(cfg))
        return buf.getvalue()

    def test_dead_wins_over_a_stale_blocked_status(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead", last_status="BLOCKED: q")}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        self.alive = False
        self.assertEqual(self.dash.wave_state(cfg, wab.load_state(cfg), "W1")[0], "dead")
        text = self.render_text(cfg)
        self.assertIn("окно закрыто", text)
        self.assertNotIn("ждёт тебя", text)
        self.assertNotIn("attach -t", text)

    def test_a_gone_window_wins_over_blocked_before_the_watch_marks_it(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        self.alive = False
        self.assertEqual(self.dash.wave_state(cfg, wab.load_state(cfg), "W1")[0], "dead")

    def test_handed_over_and_done_win_over_a_stale_blocked_status(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": None, "waves": {
            "W1": self.wave_rec("W1", phase="awaiting_merge"), "W2": self.wave_rec("W2", phase="done")}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        self.set_status(cfg, "W2", "BLOCKED: q")
        st = wab.load_state(cfg)
        self.assertEqual(self.dash.wave_state(cfg, st, "W1")[0], "awaiting_merge")
        self.assertEqual(self.dash.wave_state(cfg, st, "W2")[0], "DONE")

    def test_every_window_gone_phase_has_a_dashboard_state(self):
        self.assertEqual(set(self.dash.TERMINAL_PHASES), set(wab.WINDOW_GONE))
        for key in self.dash.TERMINAL_PHASES.values():
            self.assertIn(key, self.dash.STYLE)

    def test_handed_over_current_wave_shows_no_attach(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        text = self.render_text(cfg)
        self.assertIn("ждёт мерджа", text)
        self.assertNotIn("attach -t", text)

    def test_blocked_with_a_live_window_still_waits_for_the_owner(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: q")
        self.assertEqual(self.dash.wave_state(cfg, wab.load_state(cfg), "W1")[0], "BLOCKED")
        text = self.render_text(cfg)
        self.assertIn("ждёт тебя", text)
        self.assertIn("attach -t", text)


class W4Round29PromptFile(Base):
    """Round 29 (Codex P2): a prompt file that cannot be read, is not UTF-8 or holds only
    whitespace is refused BEFORE the tmux session and the launch intent: nothing changes."""

    def refused(self, raw=None, mode=None):
        cfg, _ = self.chain()
        p = self.tmp / "p.md"
        p.write_bytes(raw if raw is not None else b"do it\n")
        if mode is not None:
            p.chmod(mode)
            self.addCleanup(p.chmod, 0o644)
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd) as prep:
            with self.assertRaises(SystemExit) as cm:
                wab.launch(cfg, "W1", p)
        self.assertFalse(prep.called, "nothing is prepared for a prompt that is refused")
        self.assertIn("prompt", str(cm.exception))
        self.assertEqual([c for c in self.tmux_calls if "new-session" in c], [])
        self.assertEqual(self.sent, [])
        self.assertFalse(wab.state_path(cfg).exists() and "W1" in self.get_state(cfg).get("waves", {}))
        self.assertFalse((wab.wave_dir(cfg, "W1") / "status").exists())
        return str(cm.exception)

    def test_empty_prompt_is_refused(self):
        self.assertIn("empty", self.refused(b""))

    def test_whitespace_prompt_is_refused(self):
        self.assertIn("empty", self.refused(b"  \n\t\n \xc2\xa0\n"))

    def test_invalid_utf8_prompt_is_refused(self):
        self.assertIn("UTF-8", self.refused(b"\xff\xfe task \x80\n"))

    def test_unreadable_prompt_is_refused(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads a chmod 0 file")
        self.assertIn("cannot read", self.refused(b"task\n", mode=0))

    def test_a_good_prompt_still_launches(self):
        cfg, _ = self.chain()
        p = self.tmp / "p.md"
        p.write_text("  задача\n", encoding="utf-8")
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W1", p))
        self.assertIn("задача", self.sent[-1][2])


class W4Round30ChainTypes(Base):
    """Round 30: every chain.json field is type-checked by load_chain (SystemExit with the reason,
    never TypeError/ValueError later), and a live edit with a wrong type keeps `watch` alive with
    the previous settings."""

    BAD = {
        "chain": [5, ["demo"], {"a": 1}, True],
        "run_id": [20261001, ["r"], {"a": 1}, True],
        "run_dir": [5, ["x"], {"a": 1}, True, "a\x00b"],
        "workdir": [5, ["x"], {"a": 1}, True, "a\x00b"],
        "repo": [5, ["o/r"], {"a": 1}, True],
        "tmux_prefix": [5, ["wv-"], True],
        "telegram": [True, "kv", [1], [], 0, ""],
        "titles": [["x"], "x", {"W1": 5}],
        "model": [5, ["m"], True],
        "base_branch": [5, ["main"], True],
        "merge_gate": [5, ["external"], True],
        "waves": ["W1", {"W1": 1}, [1]],
        "mandate_sha256": [5, ["a" * 64]],
        "ctx_limit": ["x", [1], {"a": 1}, True],
        "idle_minutes": ["x", [1]],
        "handoff_timeout_minutes": ["x", {}],
        "tick_seconds": ["1", [1]],
    }

    def test_every_field_with_a_wrong_type_is_a_clear_refusal(self):
        for field, values in self.BAD.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    try:
                        self.chain(**{field: value})
                    except SystemExit as e:
                        self.assertIn("chain.json", str(e))
                        self.assertIn(field, str(e))
                    except Exception as e:  # noqa: BLE001
                        self.fail(f"{field}={value!r}: {type(e).__name__}: {e}")
                    else:
                        self.fail(f"{field}={value!r} was accepted")

    def test_correct_types_still_load(self):
        cfg, _ = self.chain(run_dir="rd", workdir=self.cwd, repo="o/r", model="m", base_branch="main",
                            merge_gate="external", titles={"W1": "x"}, telegram=False,
                            mandate_sha256="a" * 64, idle_minutes=0)
        self.assertTrue(str(cfg["run_dir"]).endswith(f"rd/{CHAIN}/{RUN_ID}"))
        for tg in (False, {}):
            with self.subTest(telegram=tg):
                self.chain(telegram=tg)

    def run_watch(self, cfg, path, on_first_tick, ticks=3):
        seen = []

        def fake_tick(c, st):
            seen.append((str(c["run_dir"]), c["ctx_limit"]))
            if len(seen) == 1:
                on_first_tick()
            return True
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "tick", side_effect=fake_tick):
            try:
                wab.watch(cfg, path, max_ticks=ticks)
                exit_ = None
            except SystemExit as e:
                exit_ = str(e)
        return seen, exit_

    def test_live_edit_with_a_wrong_type_keeps_watch_alive_and_the_old_settings(self):
        for field, values in self.BAD.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    cfg, path = self.chain()
                    self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                    log = cfg["run_dir"] / "events.log"
                    if log.exists():
                        log.unlink()

                    def edit():
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        doc["ctx_limit"] = 111  # a good change in the same edit is not applied either
                        doc[field] = value
                        path.write_text(json.dumps(doc), encoding="utf-8")
                    seen, exit_ = self.run_watch(cfg, path, edit)
                    self.assertIsNone(exit_)
                    self.assertEqual(len(seen), 3)
                    self.assertEqual({c for _, c in seen}, {300000})
                    self.assertEqual({r for r, _ in seen}, {str(cfg["run_dir"])})
                    self.assertIn("chain.json not re-read", log.read_text(encoding="utf-8"))

    def test_watch_survives_an_unexpected_error_while_reloading(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        real = wab.load_chain
        calls = []

        def boom(p, create=True):
            calls.append(p)
            if len(calls) == 1:
                raise TypeError("unexpected")
            return real(p, create)
        with mock.patch.object(wab, "load_chain", side_effect=boom):
            seen, exit_ = self.run_watch(cfg, path, lambda: None)
        self.assertIsNone(exit_)
        self.assertEqual(len(seen), 3)
        self.assertIn("TypeError", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))


class W4Round30PromptCopy(Base):
    """Round 30: the checked prompt text is kept as an unchanged copy in the wave directory before
    the workdir is switched and before the launch intent is saved; delivery and recovery read only
    the copy."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(wab, "prepare_clone", return_value=self.cwd)
        p.start()
        self.addCleanup(p.stop)
        self.cfg, _ = self.chain(workdir=self.cwd)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="done")}})
        self.prompt = Path(self.cwd) / "next-prompt.md"  # lives in the previous wave's checkout
        self.prompt.write_text("исходная задача W2\n", encoding="utf-8")
        self.alive = False

    def switch(self, how):
        def refresh(cfg, wave, cwd):
            if how == "change":
                self.prompt.write_text("ЧУЖОЙ текст из базовой ветки\n", encoding="utf-8")
            else:
                self.prompt.unlink()
        return mock.patch.object(wab, "refresh_workdir", side_effect=refresh)

    def assert_original_sent(self):
        self.assertIn("исходная задача W2", self.sent[-1][2])
        self.assertNotIn("ЧУЖОЙ", self.sent[-1][2])
        rec = self.get_state(self.cfg)["waves"]["W2"]
        copy = wab.wave_dir(self.cfg, "W2") / "prompt.md"
        self.assertEqual(rec["prompt_file"], str(copy))
        self.assertIn("исходная задача W2", copy.read_text(encoding="utf-8"))

    def test_prompt_changed_by_the_switch_is_delivered_as_checked(self):
        with self.switch("change"):
            self.assertTrue(wab.launch(self.cfg, "W2", self.prompt))
        self.assert_original_sent()

    def test_prompt_removed_by_the_switch_is_delivered_as_checked(self):
        with self.switch("remove"):
            self.assertTrue(wab.launch(self.cfg, "W2", self.prompt))
        self.assert_original_sent()

    def test_recovery_after_a_crash_between_intent_and_send_uses_the_same_text(self):
        with self.switch("change"), mock.patch.object(wab, "start_session", side_effect=_Crash):
            with self.assertRaises(_Crash):
                wab.launch(self.cfg, "W2", self.prompt)
        self.assertEqual(self.get_state(self.cfg)["waves"]["W2"]["phase"], "launching")
        self.prompt.write_text("ещё один ЧУЖОЙ текст\n", encoding="utf-8")
        self.alive = True
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.resume(self.cfg, wab.load_state(self.cfg))
        self.assert_original_sent()

    def test_a_refused_launch_leaves_no_copy(self):
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="running")}})
        with self.assertRaises(SystemExit):
            wab.launch(self.cfg, "W2", self.prompt)
        self.assertFalse((self.cfg["run_dir"] / "W2" / "prompt.md").exists())


class W4Round30PromptEdges(Base):
    """Round 30 (tester P3): symlink loop, directory, dangling symlink and invisible-only text are
    clean refusals before anything is prepared."""

    def refused(self, path):
        cfg, _ = self.chain()
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd) as prep:
            try:
                wab.launch(cfg, "W1", path)
            except SystemExit as e:
                msg = str(e)
            except Exception as e:  # noqa: BLE001
                self.fail(f"{type(e).__name__}: {e}")
            else:
                self.fail("launched")
        self.assertFalse(prep.called)
        self.assertEqual(self.sent, [])
        self.assertIn("prompt", msg)
        return msg

    def test_symlink_loop(self):
        p = self.tmp / "loop.md"
        p.symlink_to(p)
        self.assertIn("cannot resolve", self.refused(p))

    def test_directory_is_not_a_file(self):
        d = self.tmp / "dir.md"
        d.mkdir()
        self.assertIn("not a regular file", self.refused(d))

    def test_dangling_symlink(self):
        p = self.tmp / "dangling.md"
        p.symlink_to(self.tmp / "nowhere.md")
        self.assertIn("broken symlink", self.refused(p))

    def test_missing_file(self):
        self.assertIn("not found", self.refused(self.tmp / "absent.md"))

    def test_invisible_only_text_is_empty(self):
        for raw in (b"\xef\xbb\xbf", "​​\n".encode(), b"\x00\x00\n",
                    "﻿ ​⁠­\x00　\n".encode()):
            with self.subTest(raw=raw):
                p = self.tmp / "p.md"
                p.write_bytes(raw)
                self.assertIn("empty", self.refused(p))

    def test_bom_before_real_text_launches_without_the_bom(self):
        cfg, _ = self.chain()
        p = self.tmp / "p.md"
        p.write_bytes("﻿задача\n".encode())
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W1", p))
        self.assertIn("задача", self.sent[-1][2])
        self.assertNotIn("﻿", self.sent[-1][2])


def _outbox_violations(src):
    """Writers of the outbox outside the notice functions, and put_notice used as a value."""
    import ast
    allowed = {"put_notice", "drop_notice", "drop_ended_episodes", "ack_wave_notices", "flush_notices",
               "pending_notices", "_launch"}
    tree = ast.parse(src)
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "outbox":
            fn = node
            while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parent.get(fn)
            if fn is None or fn.name not in allowed:
                bad.append(f"'outbox' in {fn.name if fn else '<module>'} line {node.lineno}")
        if isinstance(node, ast.Name) and node.id == "put_notice":
            up = parent.get(node)
            if not (isinstance(up, ast.Call) and up.func is node):
                bad.append(f"put_notice used as a value at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr == "put_notice":
            up = parent.get(node)
            if not (isinstance(up, ast.Call) and up.func is node):
                bad.append(f"put_notice used as a value at line {node.lineno}")
    return bad


class W4Round30NoticeGuard(Base):
    """Round 30 (tester P3): the completeness test of NOTICE_EPISODE_ENDS cannot be bypassed: the
    outbox is touched only by the notice functions, put_notice is never aliased, and a key without
    an episode end is refused at run time."""

    def test_put_notice_refuses_a_key_without_an_episode_end(self):
        with self.assertRaises(ValueError):
            wab.put_notice({}, "bogus_key", "1", "text")

    def test_outbox_is_touched_only_by_the_notice_functions(self):
        self.assertEqual(_outbox_violations((WAVES / "wab.py").read_text(encoding="utf-8")), [])

    def test_the_guard_sees_aliases_and_direct_writes(self):
        src = ("def put_notice(w, k, v, t):\n    pass\n"
               "def f(w):\n    send = put_notice\n    w['outbox'] = {}\n"
               "def g(w):\n    w.setdefault('outbox', {})['x'] = 1\n"
               "def h(m, w):\n    import functools\n    functools.partial(m.put_notice, w)\n")
        self.assertEqual(len(_outbox_violations(src)), 4, _outbox_violations(src))


class W4Round30Redact(Base):
    """Round 30 (Codex P1): a dashed token of only hex (UUID, hex keys) and a long letters+digits
    segment are masked; kebab words and paths stay readable."""

    def test_dashed_hex_and_long_mixed_segments_are_masked(self):
        for tok in ("01234567-89ab-cdef-0123-456789abcdefabcd",
                    "123e4567-e89b-12d3-a456-426614174000",
                    "A1B2C3D4-E5F6-A7B8-C9D0-E1F2A3B4C5D6",
                    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
                    "build-x7k2m9q4w8e1r5t3y6u0-and-more-stuff-here"):
            for text in (tok, f"key {tok} end", f"id={tok}"):
                with self.subTest(text=text):
                    out = wab.redact(text)
                    self.assertNotIn(tok, out)
                    self.assertIn("[скрыто]", out)

    def test_paths_kebab_words_and_labelled_shas_survive(self):
        keep = ["wave-autobot/runs/superarmanda-waves/2026-10-01/W4/sa/coder-report.md",
                "superarmanda-waves", "feat/waves-dispatcher", "cc-admission-abcd1234/checkout",
                "2026-10-01-2026-10-02", "deadbeef-cafe", "sha 3f2a9c1d4e5b6a7988776655443322110099aabb",
                "superarmanda-waves-dispatcher-and-dashboard"]
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text), text)


class W4Round30PendingEnterStarted(Base):
    """Round 30 (Codex P2): recovery that only presses Enter for the first prompt finishes the start
    exactly like the normal path: the «started» mark, notice and event in one save."""

    def test_crash_after_the_text_and_before_enter_still_reports_the_start_once(self):
        cfg, path = self.chain()
        p = self.tmp / "p.md"
        p.write_text("do it\n", encoding="utf-8")

        def typed_then_crash(name, text, on_typed=None, **kw):
            self.sent.append(("text", name, text))
            on_typed()
            raise _Crash()
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                mock.patch.object(wab, "send_text", side_effect=typed_then_crash):
            with self.assertRaises(_Crash):
                wab.launch(cfg, "W1", p)
        rec = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual((rec["phase"], rec.get("pending_enter")), ("sending", "first prompt"))
        self.assertEqual(self.tg, [])
        self.alive = True
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.resume(cfg, wab.load_state(cfg))
            wab.resume(cfg, wab.load_state(cfg))
        self.assertEqual(len(self.enters), 1)
        rec = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(rec["phase"], "running")
        self.assertEqual(rec["notified"].get("started"), "1")
        self.assertEqual(len([t for t in self.tg if "стартовала волна W1" in t]), 1, self.tg)
        self.assertIn("W1: launched in tmux", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))


class W4Round31Redact(Base):
    """Round 31: github.com URL paths stay readable (A1); a 32+ hex run inside any token is masked
    whatever surrounds it (A2). Exceptions are explicit: a labelled SHA and a 40-hex SHA right
    after /commit/, /commits/ or /tree/ in a github.com URL."""

    SHA = "3f2a9c1d4e5b6a7988776655443322110099aabb"

    def test_github_urls_stay_readable(self):
        for url in ("https://github.com/bronxtc52/skill-superarmanda/pull/12",
                    "https://github.com/bronxtc52/skill-superarmanda/actions/runs/18234567890",
                    "https://github.com/bronxtc52/skill-superarmanda/actions/runs/81234567890",
                    "https://github.com/bronxtc52/skill-superarmanda/pull/12/files",
                    f"https://github.com/bronxtc52/skill-superarmanda/commit/{self.SHA}",
                    f"https://github.com/bronxtc52/skill-superarmanda/tree/{self.SHA}",
                    f"https://github.com/bronxtc52/skill-superarmanda/pull/12/commits/{self.SHA}",
                    "github.com/bronxtc52/wave-autobot-superarmanda-dispatcher/issues/345"):
            for text in (url, f"PR: {url} готов", f"({url})"):
                with self.subTest(text=text):
                    self.assertEqual(wab.redact(text, 1200), text)

    def test_secrets_in_github_query_fragment_and_tokens_in_path_are_masked(self):
        hexkey = "a1b2c3d4" * 4  # synthetic values, built so secret scanners do not flag the test
        tok = "Zz9Yy8" + "Xx7Ww6"
        cases = [
            (f"https://github.com/o/r/pull/1?token={tok}", tok),
            (f"https://github.com/o/r/pull/1#key={hexkey}", hexkey),
            (f"https://github.com/o/r/blob/{hexkey}/x.md", hexkey),
            ("https://github.com/o/r/blob/main/ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2", "a1B2c3D4e5F6g7H8i9J0k1L2"),
            ("https://ghp_a1B2c3D4e5F6g7H8i9J0@github.com/o/r", "a1B2c3D4e5F6g7H8i9J0"),
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                out = wab.redact(text, 1200)
                self.assertNotIn(secret, out)
                self.assertIn("[скрыто]", out)

    def test_hex_inside_a_token_is_masked_whatever_the_boundaries(self):
        hx = "0123456789abcdef0123456789abcdef"
        for text in (f"{hx}-us21", f"key-{hx}", f"id_{hx}", f"cache/{hx}", f"v{hx}", f"{hx}x",
                     f"apikey {hx}-us21 end", f"https://example.com/cache/{hx}/blob",
                     f"https://example.com/commit/{self.SHA}", f"x{self.SHA}"):
            with self.subTest(text=text):
                out = wab.redact(text, 1200)
                self.assertNotIn(hx, out)
                self.assertNotIn(self.SHA, out)
                self.assertIn("[скрыто]", out)
        self.assertEqual(wab.redact(f"{hx}-us21"), "[скрыто]-us21")

    def test_explicit_exceptions_still_hold(self):
        for text in (f"commit {self.SHA}", f"sha {self.SHA}", "deadbeef-cafe", "short 3c71c34b0a12",
                     "wave-autobot/runs/superarmanda-waves/2026-10-01/W4/sa/coder-report.md"):
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text), text)


class W4Round31TelegramCache(Base):
    """Round 31 (Codex P2): a failed send drops the cached Telegram credentials, so a secret rotated
    in Key Vault is picked up by the next retry instead of failing forever."""

    def test_rotated_token_is_reread_after_a_failed_send(self):
        import urllib.error
        cfg, _ = self.chain()
        orig = load_orig("wab_tg_cache")
        tokens = iter(["OLD:tok", "chat1", "NEW:tok", "chat1"])
        reads = []

        def fake_sh(*args, **kw):
            reads.append(args)
            return subprocess.CompletedProcess(args, 0, next(tokens) + "\n", "")

        sent = []

        def fake_urlopen(req, timeout=None):
            if "OLD:tok" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
            sent.append(req.full_url)
            return contextlib.nullcontext()

        with mock.patch.object(orig, "sh", side_effect=fake_sh), \
                mock.patch.object(orig.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertFalse(orig.notify(cfg, "first"))
            self.assertEqual(orig._tg, {}, "a failed send must drop the cached credentials")
            self.assertTrue(orig.notify(cfg, "second"))
            self.assertTrue(orig.notify(cfg, "third"))  # cached again after a success: no new read
        self.assertEqual(len(reads), 4)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("NEW:tok" in u for u in sent))


class W4Round31StatusRead(Base):
    """Round 31 (Codex P2): an unreadable status file (a directory, chmod 0, gone between exists()
    and read_text()) keeps the last known status; one event per episode; watch keeps running."""

    def run_ticks(self, cfg, n=2):
        for _ in range(n):
            wab.tick(cfg, wab.load_state(cfg))  # must not raise
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        return [l for l in log.splitlines() if "status unreadable" in l]

    def setup_wave(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(last_status="BLOCKED: q?")}})
        return cfg

    def test_status_is_a_directory(self):
        cfg = self.setup_wave()
        (wab.wave_dir(cfg, "W1") / "status").mkdir()
        self.assertEqual(len(self.run_ticks(cfg)), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["last_status"], "BLOCKED: q?")
        self.assertTrue(any("q?" in t for t in self.tg), "the last known status still drives the tick")

    def test_status_is_unreadable(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads a chmod 0 file")
        cfg = self.setup_wave()
        p = wab.wave_dir(cfg, "W1") / "status"
        p.write_text("DONE\n", encoding="utf-8")
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        self.assertEqual(len(self.run_ticks(cfg)), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

    def test_status_vanishes_between_exists_and_read(self):
        cfg = self.setup_wave()
        self.set_status(cfg, "W1", "DONE")
        real = Path.read_text

        def flaky(path, *a, **kw):
            if path.name == "status":
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real(path, *a, **kw)

        with mock.patch.object(Path, "read_text", flaky):
            self.assertEqual(len(self.run_ticks(cfg, 3)), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")
        # readable again: the episode is over, a later failure is a new event
        self.set_status(cfg, "W1", "BLOCKED: q?")
        wab.tick(cfg, wab.load_state(cfg))
        (wab.wave_dir(cfg, "W1") / "status").unlink()
        (wab.wave_dir(cfg, "W1") / "status").mkdir()
        self.assertEqual(len(self.run_ticks(cfg)), 2)


class W4Round31ChainNulls(Base):
    """Round 31: an explicit JSON null in an optional string field is the same as leaving it out."""

    def write(self, **fields):
        doc = {"chain": CHAIN, "run_id": RUN_ID, "repo": "o/r", "waves": ["W1", "W2"]}
        doc.update(fields)
        cfgdir = self.tmp / "cfg"
        cfgdir.mkdir(exist_ok=True)
        path = cfgdir / "chain.json"
        path.write_text(json.dumps(doc), encoding="utf-8")  # null kept: the helper would drop None
        return path

    def test_base_branch_null_is_auto(self):
        path = self.write(base_branch=None)
        self.assertIn('"base_branch": null', path.read_text(encoding="utf-8"))
        cfg = wab.load_chain(path)
        self.assertNotIn("base_branch", cfg)
        with mock.patch.object(wab, "sh", return_value=subprocess.CompletedProcess(
                [], 0, "origin/main\n", "")):
            self.assertEqual(wab.base_branch_of(cfg, self.cwd), "main")

    def test_tmux_prefix_null_is_the_default(self):
        cfg = wab.load_chain(self.write(tmux_prefix=None))
        self.assertEqual(cfg["tmux_prefix"], "wab-")

    def test_other_optional_nulls_load(self):
        cfg = wab.load_chain(self.write(model=None, merge_gate=None, workdir=None, run_dir=None))
        self.assertIsNone(cfg.get("model"))
        self.assertIsNone(cfg.get("merge_gate"))


class W4Round31CommitsWindow(Base):
    """Round 31: the dashboard counts a finished wave's commits only up to its end, so a later
    wave in the same workdir does not grow W1's counter; the end is stored on every terminal phase."""

    def git(self, *args, when=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
        subprocess.run(["git", "-C", self.cwd, *args], check=True, capture_output=True, env=env)

    def test_two_sequential_waves_in_one_workdir(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        t0 = int(time.time()) - 10_000
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "w1", when=t0 + 10)
        self.git("commit", "-q", "--allow-empty", "-m", "w1b", when=t0 + 20)
        self.git("commit", "-q", "--allow-empty", "-m", "w2", when=t0 + 200)
        self.assertEqual(dash.commits_since(self.cwd, t0, t0 + 100), 2)
        self.assertEqual(dash.commits_since(self.cwd, t0 + 150), 1)
        cfg, _ = self.chain()
        st = {"current": "W2", "waves": {
            "W1": self.wave_rec("W1", phase="done", started=t0, finished=t0 + 100),
            "W2": self.wave_rec("W2", phase="running", started=t0 + 150)}}
        seen = {}
        with mock.patch.object(dash, "commits_since", side_effect=lambda cwd, s, until=None:
                               seen.setdefault(s, until) or 0), \
                mock.patch.object(dash, "wave_stats", return_value={k: 0 for k in
                                                                     ("turns", "tools", "agents", "out", "read")}):
            dash.waves_table(cfg, st)
        self.assertEqual(seen.get(t0), t0 + 100)
        self.assertIsNone(seen.get(t0 + 150))

    def test_every_terminal_phase_records_its_end(self):
        cfg, _ = self.chain()
        st = {"current": "W1", "waves": {"W1": self.wave_rec()}}
        self.put_state(cfg, st)
        self.alive = False
        self.set_status(cfg, "W1", "RUNNING")
        wab.tick(cfg, wab.load_state(cfg))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "dead")
        self.assertIsInstance(w.get("finished"), (int, float))
        # the window is back: the wave runs again, the end is not an end any more
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "running"
        wab.save_state(cfg, st)
        self.assertNotIn("finished", self.get_state(cfg)["waves"]["W1"])


class W4Round31PromptVisible(Base):
    """Round 31 (P3): a prompt of only combining marks, variation selectors or filler characters
    (U+3164, U+2800, U+115F, U+1160, U+FFA0) is empty."""

    def test_visually_empty_prompts_are_refused(self):
        for text in ("́", "️", "ㅤ", "⠀", "ᅟᅠﾠ", " ́️ㅤ⠀\n"):
            with self.subTest(text=ascii(text)):
                p = self.tmp / "p.md"
                p.write_text(text, encoding="utf-8")
                with self.assertRaises(SystemExit) as cm:
                    wab.read_prompt(p)
                self.assertIn("empty", str(cm.exception))

    def test_real_text_is_accepted(self):
        for text in ("a", "задача", "#", "1", "✓", "é"):
            with self.subTest(text=ascii(text)):
                p = self.tmp / "p.md"
                p.write_text(text, encoding="utf-8")
                self.assertTrue(wab.read_prompt(p))


class W4Round32Admission(Base):
    """Round 32 (P1): admission belongs to the host. No local copy of its signature rules: on a
    managed host (any policy signal: the helper or rules/autonomy-allowlist.md) the clone comes
    only from `cc-autonomy prepare`, saved in the state and reused by the next waves; a signal
    without a working helper is a refusal, never «unmanaged»."""

    def git_repo(self, name="admitted"):
        p = self.tmp / name
        p.mkdir()
        subprocess.run(["git", "init", "-q", str(p)], check=True)
        return p

    def helper(self, admitted=None, rc=0, ok=True):
        """A fake host helper in the temporary HOME: counts its calls, prints the JSON of prepare."""
        h = self.home / ".claude" / "bin" / "cc-autonomy.py"
        h.parent.mkdir(parents=True, exist_ok=True)
        calls = self.tmp / "prepare-calls"
        out = json.dumps({"ok": ok, "admitted_path": str(admitted) if admitted else None})
        h.write_text("import sys\n"
                     f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                     f"print({out!r})\n"
                     f"sys.exit({rc})\n", encoding="utf-8")
        return calls

    def rules(self):
        r = self.home / ".claude" / "rules" / "autonomy-allowlist.md"
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_text("# host policy\n", encoding="utf-8")

    def ncalls(self, calls):
        return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0

    def test_no_local_copy_of_the_signature_rules(self):
        for name in ("admitted_workdir", "ADMISSION_ROOT", "ADMISSION_SIGNATURE"):
            self.assertFalse(hasattr(wab, name), name)
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        self.assertNotIn("cc-admission-", src)
        self.assertNotIn("empty-template", src)

    def test_rules_signal_without_helper_is_a_refusal_not_unmanaged(self):
        self.rules()
        cfg, _ = self.chain(workdir=str(self.git_repo("plain")))
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg)
        self.assertIn("host policy", str(cm.exception))
        self.assertIn("autonomy-allowlist.md", str(cm.exception))

    def test_helper_that_is_not_a_file_is_a_refusal(self):
        h = self.home / ".claude" / "bin" / "cc-autonomy.py"
        h.mkdir(parents=True)  # present, but not a runnable file
        cfg, _ = self.chain(workdir=str(self.git_repo("plain")))
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg)
        self.assertIn("host policy", str(cm.exception))

    def test_dangling_helper_symlink_is_a_signal_and_a_refusal(self):
        h = self.home / ".claude" / "bin" / "cc-autonomy.py"
        h.parent.mkdir(parents=True)
        h.symlink_to(self.tmp / "gone.py")
        cfg, _ = self.chain(workdir=str(self.git_repo("plain")))
        with self.assertRaises(SystemExit):
            wab.prepare_clone(cfg)

    def test_failing_helper_is_a_refusal(self):
        self.rules()
        self.helper(admitted=self.git_repo(), rc=3, ok=False)
        cfg, _ = self.chain()
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg)
        self.assertIn("admission refused", str(cm.exception))

    def test_managed_host_refuses_workdir_from_chain_json_even_with_the_old_signature(self):
        root = self.tmp / "cc-admission-abcd1234"
        (root / "empty-template").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root / "checkout")], check=True)
        calls = self.helper(admitted=self.git_repo())
        cfg, _ = self.chain(workdir=str(root / "checkout"))
        self.alive = False
        p = self.tmp / "p.md"
        p.write_text("go", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            wab.launch(cfg, "W1", p)
        self.assertIn("cc-autonomy prepare", str(cm.exception))
        self.assertFalse([c for c in self.tmux_calls if c[1] == "new-session"])
        self.assertEqual(self.ncalls(calls), 0)

    def test_managed_chain_keeps_the_prepared_clone_for_the_next_waves(self):
        clone = self.git_repo()
        calls = self.helper(admitted=clone)
        self.rules()
        cfg, _ = self.chain()
        self.alive = False
        p = self.tmp / "p.md"
        p.write_text("go", encoding="utf-8")
        self.assertTrue(wab.launch(cfg, "W1", p))
        st = self.get_state(cfg)
        self.assertEqual(st["admitted_path"], str(clone))
        self.assertEqual(st["waves"]["W1"]["cwd"], str(clone))
        self.assertEqual(self.ncalls(calls), 1)
        self.assertIn("prepare o/r", calls.read_text(encoding="utf-8"))
        st["waves"]["W1"]["phase"] = "awaiting_merge"
        self.put_state(cfg, st)
        refreshed = []
        with mock.patch.object(wab, "refresh_workdir", side_effect=lambda c, w, cwd: refreshed.append((w, cwd))):
            self.assertTrue(wab.launch(cfg, "W2", p))
        self.assertEqual(self.ncalls(calls), 1)  # not prepared again: the same clone
        self.assertEqual(refreshed, [("W2", str(clone))])
        self.assertEqual(self.get_state(cfg)["waves"]["W2"]["cwd"], str(clone))

    def test_managed_workdir_equal_to_the_saved_clone_is_accepted_other_refused(self):
        clone = self.git_repo()
        calls = self.helper(admitted=clone)
        cfg, _ = self.chain(workdir=str(clone))
        st = {"waves": {}, "admitted_path": str(clone)}
        self.assertEqual(wab.prepare_clone(cfg, st), str(clone))
        cfg, _ = self.chain(workdir=str(self.git_repo("other")))
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg, st)
        self.assertIn("cc-autonomy prepare", str(cm.exception))
        self.assertEqual(self.ncalls(calls), 0)

    def test_unmanaged_only_without_any_signal(self):
        plain = self.git_repo("plain")
        cfg, _ = self.chain(workdir=str(plain))
        self.assertEqual(wab.prepare_clone(cfg), str(plain.resolve()))
        self.assertIn("unmanaged host", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))


class W4Round32NextPrompt(Base):
    """Round 32 (P2): a non-last wave's DONE is handed over only with a next-prompt.md that the
    coordinator's launch will accept (read_prompt); an unusable one stops like a missing one."""

    def test_unusable_next_prompt_is_not_handed_over(self):
        for gate in ("external", None):
            for content in (b"", b"  \n\t", b"\xff\xfe bad", "\u200b\u3164".encode()):
                with self.subTest(gate=gate, content=content):
                    self.tg.clear()
                    cfg, _ = self.chain(merge_gate=gate)
                    (cfg["run_dir"] / "events.log").unlink(missing_ok=True)
                    self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                    self.set_status(cfg, "W1", "DONE")
                    (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_bytes(content)
                    with mock.patch.object(wab, "launch") as launch:
                        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
                    launch.assert_not_called()
                    st = self.get_state(cfg)
                    self.assertEqual(st["waves"]["W1"]["phase"], "done")
                    self.assertIsNone(st["current"])
                    self.assertIn("next-prompt.md", st["stopped"])
                    log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
                    self.assertIn("unusable next-prompt.md", log)
                    self.assertNotIn("awaiting merge", log)
                    self.assertEqual(len([t for t in self.tg if "next-prompt.md" in t]), 1)

    def test_valid_next_prompt_is_still_handed_over(self):
        cfg, _ = self.chain(merge_gate="external")
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")


class W4Round32GithubHost(Base):
    """Round 32: the github.com exemption is decided by the parsed host of the URL, not by a
    `github.com/` substring anywhere."""

    SECRET = "Qw7" + "Er8Ty9Ui0Op1As2Df3Gh4Jk5Lz6Xc7Vb8Nm9Qa1Ws2"  # synthetic, 45 chars

    def test_github_lookalikes_are_not_exempt(self):
        s = self.SECRET
        for text in (f"https://example.org/github.com/o/r/{s}",
                     f"https://example.org/x?u=github.com/o/r/{s}",
                     f"https://example.org/x?next=https://github.com/o/r/{s}",
                     f"https://github.com.evil.example/o/r/{s}",
                     f"github.com.evil.example/o/r/{s}",
                     f"https://github.com@evil.example/o/r/{s}",
                     f"https://evil.example/github.com/{s}",
                     f"see example.org/github.com/o/{s}",
                     f"xgithub.com/o/r/{s}"):
            with self.subTest(text=text):
                out = wab.redact(text, 1200)
                self.assertNotIn(s, out)
                self.assertIn("[скрыто]", out)

    def test_real_github_urls_stay_readable(self):
        for url in ("https://github.com/o/r/pull/12",
                    "https://www.github.com/o/r/actions/runs/18234567890",
                    "http://github.com/o/wave-autobot-superarmanda-dispatcher/issues/345",
                    "github.com/o/wave-autobot-superarmanda-dispatcher/issues/345",
                    "HTTPS://GitHub.com/o/r/pull/12/files"):
            for text in (url, f"PR: {url} ok", f"({url})", f"`{url}`", f"<{url}>"):
                with self.subTest(text=text):
                    self.assertEqual(wab.redact(text, 1200), text)


class W4Round32DashTitlesNull(Base):
    """Round 32: `titles: null` is the same as leaving it out; the dashboard frame renders."""

    def test_titles_null_loads_and_renders(self):
        doc = {"chain": CHAIN, "run_id": RUN_ID, "repo": "o/r", "waves": ["W1", "W2"], "titles": None}
        cfgdir = self.tmp / "cfg"
        cfgdir.mkdir(exist_ok=True)
        path = cfgdir / "chain.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        cfg = wab.load_chain(path)
        self.assertEqual(cfg.get("titles") or {}, {})
        try:
            import dash
            from rich.console import Console
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(dash.safe_render(cfg))
        text = buf.getvalue()
        self.assertNotIn("кадр не отрисован", text)
        self.assertIn("W1", text)
        cfg["titles"] = None  # a cfg built by hand: dash itself must not crash on it
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(dash.safe_render(cfg))
        self.assertNotIn("кадр не отрисован", buf.getvalue())


class W4Round32TesterP3(Base):
    """Round 32, tester-r31 P3: the github exemption ends with the URL token; an unreadable status
    keeps the last known one on the dashboard; U+FFFC, U+FFFD and U+1D159 are not visible."""

    BLOB = "aB3_xY7-Qz9" + "Kp2Lm5Nv8Rt1Ws4Hd6Jf0Gc3Vb7Ne9Mu2Xi5Yo8Pq1Zr4St7U"  # synthetic, 60 chars

    def test_github_exemption_stops_at_the_url_token(self):
        b = self.BLOB
        self.assertEqual(len(b), 60)
        for text in (f"https://github.com/o/r/pull/3,{b}",
                     f"(https://github.com/o/r/pull/3){b}",
                     f"https://github.com/o/r/pull/3\"{b}",
                     f"<https://github.com/o/r/pull/3>{b}",
                     f"https://x.io/go?u=github.com/{b}"):
            with self.subTest(text=text):
                out = wab.redact(text, 1200)
                self.assertNotIn(b, out)
                self.assertIn("[скрыто]", out)
        self.assertEqual(wab.redact("https://github.com/o/r/pull/3, ok", 1200), "https://github.com/o/r/pull/3, ok")

    def test_dashboard_keeps_the_last_status_when_status_is_unreadable(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        cfg, _ = self.chain()
        st = {"current": "W1", "waves": {"W1": self.wave_rec(last_status="RUNNING")}}
        sp = wab.wave_dir(cfg, "W1") / "status"
        sp.unlink(missing_ok=True)
        sp.mkdir()  # exists, cannot be read as a file
        self.assertEqual(dash.wave_state(cfg, st, "W1")[0], "RUNNING")

    def test_object_replacement_and_null_notehead_are_not_visible(self):
        for text in ("￼", "�", "\U0001d159", " ￼�\U0001d159\n"):
            with self.subTest(text=ascii(text)):
                p = self.tmp / "p.md"
                p.write_text(text, encoding="utf-8")
                with self.assertRaises(SystemExit) as cm:
                    wab.read_prompt(p)
                self.assertIn("empty", str(cm.exception))


class W4Round33(Base):
    """Round 33: a non-object from prepare is a clean refusal; the previous wave's window is
    closed before the shared workdir is switched (and the intent to close it survives a crash);
    an unmanaged workdir is the root of a checkout; the dashboard counts a wave's commits from
    its own start revision, not every ref of the repository."""

    def git(self, cwd, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.y",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.y")
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()

    def repo(self, name="wd"):
        p = self.tmp / name
        p.mkdir()
        self.git(p, "init", "-q")
        self.git(p, "commit", "-q", "--allow-empty", "-m", "base")
        return p

    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("task\n", encoding="utf-8")
        return p

    # ----- 1: prepare printed valid JSON that is not an object -----
    def test_non_object_json_from_prepare_is_a_clean_refusal(self):
        r = self.home / ".claude" / "rules" / "autonomy-allowlist.md"
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_text("# host policy\n", encoding="utf-8")
        h = self.home / ".claude" / "bin" / "cc-autonomy.py"
        h.parent.mkdir(parents=True, exist_ok=True)
        cfg, _ = self.chain()
        for raw in ("[]", "null", "5", '"x"', "[1, 2]", "true"):
            with self.subTest(raw=raw):
                h.write_text(f"print({raw!r})\n", encoding="utf-8")
                with self.assertRaises(SystemExit) as cm:
                    wab.prepare_clone(cfg)
                self.assertIn("admission refused", str(cm.exception))

    # ----- 2: the previous wave's window is closed before the workdir is switched -----
    def test_crash_between_awaiting_merge_and_exit_sends_exit_after_restart(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")

        def crash(*args, **kw):
            if "send-keys" in args:
                raise _Crash()
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(wab, "tmux", side_effect=crash):
            with self.assertRaises(_Crash):
                wab.tick(cfg, wab.load_state(cfg))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "awaiting_merge")
        self.assertTrue(w.get("pending_exit"))
        exits = lambda: [c for c in self.tmux_calls if "send-keys" in c and "/exit" in c]
        self.assertFalse(exits())
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=5)  # the restarted dispatcher
        self.assertTrue(exits(), self.tmux_calls)
        self.assertTrue(self.get_state(cfg)["waves"]["W1"].get("pending_exit"))  # window still open
        self.alive = False
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=5)
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    def test_crash_between_done_and_exit_sends_exit_after_restart(self):
        cfg, _ = self.chain(waves=["W1", "W2", "W3"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")

        def crash(*args, **kw):
            if "send-keys" in args:
                raise _Crash()
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(wab, "tmux", side_effect=crash):
            with self.assertRaises(_Crash):
                wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue(self.get_state(cfg)["waves"]["W1"].get("pending_exit"))
        with mock.patch.object(wab, "launch", return_value=True):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue([c for c in self.tmux_calls if "send-keys" in c and "/exit" in c])

    def test_launch_refuses_while_the_previous_wave_window_is_alive(self):
        wd = self.repo()
        cfg, _ = self.chain(workdir=str(wd), base_branch="main")
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec(phase="awaiting_merge", cwd=str(wd), pending_exit=True)}})
        head = self.git(wd, "rev-parse", "HEAD")
        refreshed = []
        with mock.patch.object(wab, "tmux_alive", side_effect=lambda n: n == "wv-w1"), \
                mock.patch.object(wab, "refresh_workdir", side_effect=lambda *a: refreshed.append(a)):
            with self.assertRaises(SystemExit) as cm:
                wab.launch(cfg, "W2", self.prompt())
        self.assertIn("previous wave session wv-w1 is still running", str(cm.exception))
        self.assertEqual(refreshed, [])
        self.assertFalse([c for c in self.tmux_calls if "new-session" in c])
        self.assertEqual(self.git(wd, "rev-parse", "HEAD"), head)
        st = self.get_state(cfg)
        self.assertEqual((st["current"], st["waves"]["W1"]["phase"]), ("W1", "awaiting_merge"))
        self.assertNotIn("W2", st["waves"])
        # the window is gone: the same launch goes through and the flag is dropped
        self.alive = False
        with mock.patch.object(wab, "refresh_workdir", side_effect=lambda *a: refreshed.append(a)):
            self.assertTrue(wab.launch(cfg, "W2", self.prompt()))
        self.assertEqual(len(refreshed), 1)
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    # ----- 3: an unmanaged workdir is the root of a checkout -----
    def test_unmanaged_workdir_must_be_the_root_of_a_checkout(self):
        wd = self.repo()
        sub = wd / "sub"
        sub.mkdir()
        cfg, _ = self.chain(workdir=str(sub))
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_clone(cfg)
        self.assertIn("workdir must be the root of an isolated checkout/worktree", str(cm.exception))
        cfg, _ = self.chain(workdir=str(wd))
        self.assertEqual(wab.prepare_clone(cfg), str(wd.resolve()))
        wt = self.tmp / "wt"
        self.git(wd, "worktree", "add", "-q", "--detach", str(wt))
        cfg, _ = self.chain(workdir=str(wt))
        self.assertEqual(wab.prepare_clone(cfg), str(wt.resolve()))
        link = self.tmp / "link"
        link.symlink_to(wd)
        self.assertIsNone(wab.isolated_workdir(str(link)))
        self.assertIsNotNone(wab.isolated_workdir(str(sub)))

    # ----- 4: commits of a wave from its own start revision -----
    def test_commits_count_from_the_wave_start_revision_only(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        wd = self.repo()
        start = self.git(wd, "rev-parse", "HEAD")
        cfg, _ = self.chain(workdir=str(wd), merge_gate="external")
        self.alive = False
        self.assertTrue(wab.launch(cfg, "W1", self.prompt()))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w.get("start_rev"), start)
        self.git(wd, "commit", "-q", "--allow-empty", "-m", "w1")
        # another branch and a fetched remote ref with commits inside the wave's window
        self.git(wd, "branch", "other")
        self.git(wd, "switch", "-q", "other")
        self.git(wd, "commit", "-q", "--allow-empty", "-m", "o1")
        self.git(wd, "commit", "-q", "--allow-empty", "-m", "o2")
        self.git(wd, "update-ref", "refs/remotes/origin/feature", "HEAD")
        self.git(wd, "switch", "-q", "-")
        self.assertEqual(dash.wave_commits(self.get_state(cfg)["waves"]["W1"]), 1)
        # the wave hands off: the count is fixed in the record, a later HEAD does not change it
        self.alive = True
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        wab.tick(cfg, wab.load_state(cfg))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual((w["phase"], w.get("commits")), ("awaiting_merge", 1))
        self.git(wd, "switch", "-q", "--detach", "other")
        self.assertEqual(dash.wave_commits(w), 1)
        # an old record without start_rev keeps the time-window count
        legacy = {k: v for k, v in w.items() if k not in ("start_rev", "commits")}
        with mock.patch.object(dash, "commits_since", return_value=7) as cs:
            self.assertEqual(dash.wave_commits(legacy), 7)
        cs.assert_called_once()


class W4Round34(Base):
    """Round 34: a terminal wave with a live window always carries pending_exit and every entry of
    the coordinator (launch, done, watch/resume) pushes it; a dispatcher launch refusal is an event
    and a notice, not a dead watch; strict `ok` of prepare; the dashboard without rich, its status
    fallback and corrupt commit counters."""

    def setUp(self):
        super().setUp()
        self.windows = {"wv-w1"}
        self.exit_closes = True  # the Claude TUI obeys /exit at once
        wab.tmux_alive.side_effect = lambda n: n in self.windows

        def fake_tmux(*args, **kw):
            self.tmux_calls.append(args)
            if "new-session" in args:
                self.windows.add(args[args.index("-s") + 1])
            if "send-keys" in args and "/exit" in args and self.exit_closes:
                tgt = args[args.index("-t") + 1]
                self.windows -= {n for n in set(self.windows) if wab.pane_target(n) == tgt}
            return subprocess.CompletedProcess(args, 0, "", "")
        p = mock.patch.object(wab, "tmux", side_effect=fake_tmux)
        p.start()
        self.addCleanup(p.stop)

    git = W4Round33.git
    repo = W4Round33.repo

    def exits(self, name="wv-w1"):
        return [c for c in self.tmux_calls if "send-keys" in c and "/exit" in c
                and wab.pane_target(name) in c]

    def policy_host(self, helper_src):
        r = self.home / ".claude" / "rules" / "autonomy-allowlist.md"
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_text("# host policy\n", encoding="utf-8")
        h = self.home / ".claude" / "bin" / "cc-autonomy.py"
        h.parent.mkdir(parents=True, exist_ok=True)
        h.write_text(helper_src, encoding="utf-8")

    # ----- 1: DONE without a usable next-prompt.md keeps the intent to close the window -----
    def test_stop_without_next_closes_the_window_and_a_fixed_launch_goes_through(self):
        wd = self.repo()
        cfg, _ = self.chain(workdir=str(wd), base_branch="main", merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(cwd=str(wd))}})
        self.set_status(cfg, "W1", "DONE")
        self.exit_closes = False  # busy: the first /exit does not close the window yet
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        st = self.get_state(cfg)
        self.assertEqual((st["waves"]["W1"]["phase"], st["current"]), ("done", None))
        self.assertTrue(st["waves"]["W1"].get("pending_exit"))
        self.assertTrue(self.exits())
        self.assertIn("wv-w1", self.windows)
        # the coordinator fixes next-prompt.md and launches W2 while W1's window still lives
        nxt = wab.wave_dir(cfg, "W1") / "next-prompt.md"
        nxt.write_text("go\n", encoding="utf-8")
        self.exit_closes = True
        with mock.patch.object(wab, "refresh_workdir"):
            self.assertTrue(wab.launch(cfg, "W2", nxt))
        self.assertNotIn("wv-w1", self.windows)
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W2")
        self.assertNotIn("pending_exit", st["waves"]["W1"])

    # ----- 2: done finishes the close left by a crash -----
    def test_done_closes_the_window_left_by_a_crash_after_awaiting_merge(self):
        cfg, _ = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")

        def crash(*args, **kw):
            if "send-keys" in args:
                raise _Crash()
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(wab, "tmux", side_effect=crash):
            with self.assertRaises(_Crash):
                wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue(self.get_state(cfg)["waves"]["W1"].get("pending_exit"))
        self.assertFalse(self.exits())
        self.exit_closes = False  # the window does not go within the wait
        wab.done_cmd(cfg)
        self.assertTrue(self.exits())
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "done")
        self.assertTrue(w.get("pending_exit"))  # the window is still there: the intent stays
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("still open", log)
        # a repeated done pushes again; the window goes and the flag with it
        n = len(self.exits())
        self.exit_closes = True
        wab.done_cmd(cfg)
        self.assertGreater(len(self.exits()), n)
        self.assertNotIn("wv-w1", self.windows)
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    def test_done_without_watch_after_a_crash_closes_at_once(self):
        cfg, _ = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec(phase="awaiting_merge", pending_exit=True)}})
        wab.done_cmd(cfg)
        self.assertTrue(self.exits())
        self.assertNotIn("wv-w1", self.windows)
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    # ----- invariant: every terminal transition ends with the window closed -----
    def test_every_terminal_transition_closes_the_window(self):
        cases = [  # (name, merge_gate, "auto" = the PR is merged already, waves, next-prompt.md, then)
            ("awaiting_merge, next wave", "external", False, ["W1", "W2"], b"go\n", None),
            ("awaiting_merge, last wave + done", "external", False, ["W1"], None, "done"),
            ("external, no next-prompt", "external", False, ["W1", "W2"], None, None),
            ("external, unusable next-prompt", "external", False, ["W1", "W2"], b" \n", None),
            ("no gate, next wave handed over", None, False, ["W1", "W2"], b"go\n", None),
            ("no gate, no next-prompt", None, False, ["W1", "W2"], None, None),
            ("no gate, last wave + done", None, False, ["W1"], None, "done"),
            ("no gate, unusable next-prompt", None, False, ["W1", "W2"], b"\xff", None),
            ("auto, merged, last wave", "auto", True, ["W1"], None, None),
            ("auto, merged, no next-prompt", "auto", True, ["W1", "W2"], None, None),
            ("auto, merged, unusable next-prompt", "auto", True, ["W1", "W2"], b"\xff", None),
            ("auto, merged, next launched by the dispatcher", "auto", True, ["W1", "W2"], b"go\n", "launch"),
        ]
        for name, gate, merged, waves, nxt, then in cases:
            with self.subTest(case=name):
                self.windows = {"wv-w1"}
                self.tmux_calls.clear()
                cfg, path = self.chain(merge_gate=gate, waves=waves, base_branch="main")
                self.gh_handler = self.merged_view if merged else None
                self.put_state(cfg, {"current": "W1", "waves": {
                    "W1": self.auto_rec() if merged else self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                p = wab.wave_dir(cfg, "W1") / "next-prompt.md"
                p.unlink(missing_ok=True)
                if nxt is not None:
                    p.write_bytes(nxt)
                with mock.patch.object(wab, "launch", return_value=True):
                    wab.tick(cfg, wab.load_state(cfg))
                    if then == "done":
                        wab.done_cmd(cfg)
                    wab.resume(cfg, wab.load_state(cfg))  # any later entry of the coordinator
                self.assertNotIn("wv-w1", self.windows, name)
                self.assertTrue(self.exits(), name)
                self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"], name)

    def test_not_ready_keeps_its_window_for_the_owner(self):
        """Deliberate exclusion: a window that never became ready is the owner's to look at."""
        cfg, _ = self.chain()
        self.windows = set()
        self.ready = False
        prompt = self.tmp / "p.md"
        prompt.write_text("task\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertFalse(wab.launch(cfg, "W1", prompt))
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        self.assertNotIn("pending_exit", w)
        self.assertIn("wv-w1", self.windows)
        self.assertFalse(self.exits())

    # ----- 5: prepare's ok must be exactly true -----
    def test_admission_requires_ok_to_be_true(self):
        cfg, _ = self.chain()
        for ok in ("[1]", "1", '"yes"', "{\"a\": 1}"):
            with self.subTest(ok=ok):
                self.policy_host(f"print('{{\"ok\": {ok}, \"admitted_path\": \"/tmp\"}}')\n")
                with self.assertRaises(SystemExit) as cm:
                    wab.prepare_clone(cfg)
                self.assertIn("admission refused", str(cm.exception))
        self.policy_host(f"print('{{\"ok\": true, \"admitted_path\": \"{self.cwd}\"}}')\n")
        self.assertEqual(wab.prepare_clone(cfg), self.cwd)

    # ----- 6: a refused launch inside the dispatcher's tick -----
    def test_dispatcher_launch_refusal_is_an_event_and_a_notice(self):
        wd = self.repo()
        cfg, path = self.auto_chain(workdir=str(wd), base_branch="main")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec(cwd=str(wd))}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.exit_closes = False  # the previous wave's window will not close: launch refuses
        with mock.patch.object(wab, "drop_stale_btab"):
            self.assertIs(wab.watch(cfg, path, max_ticks=3), False)
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W1")
        self.assertNotIn("W2", st["waves"])
        self.assertIn("launch of W2 refused", st.get("stopped", ""))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("W1: launch of W2 refused", log)
        self.assertIn("previous wave session wv-w1 is still running", log)
        self.assertIn("watch stopped: W1: launch of W2 refused", log)
        refused = [t for t in self.tg if "не запустил" in t]
        self.assertEqual(len(refused), 1, self.tg)
        self.assertIn("W2", refused[0])
        self.assertIn("launch_refused", wab.NOTICE_EPISODE_ENDS)
        # a second watch on the same refusal does not notify again
        with mock.patch.object(wab, "drop_stale_btab"):
            self.assertIs(wab.watch(cfg, path, max_ticks=3), False)
        self.assertEqual(len([t for t in self.tg if "не запустил" in t]), 1)
        # the window goes: the coordinator's launch goes through and clears the stop
        self.exit_closes = True
        with mock.patch.object(wab, "refresh_workdir"):
            self.assertTrue(wab.launch(cfg, "W2", wab.wave_dir(cfg, "W1") / "next-prompt.md"))
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W2")
        self.assertNotIn("stopped", st)
        self.assertNotIn("launch_refused", st["waves"]["W1"].get("outbox") or {})

    def test_dispatcher_force_closes_a_done_wave_window_that_ignores_exit(self):
        # Live stop (mh-creators run01, 2026-10-03 18:56Z): W1 was merged, its window answered /exit
        # with the «Exit and stop tasks» menu, the launch of W2 was refused and the chain stood 9 h.
        wd = self.repo()
        cfg, path = self.auto_chain(workdir=str(wd), base_branch="main")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec(cwd=str(wd))}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.exit_closes = False  # the window ignores /exit
        inner = wab.tmux.side_effect

        def tmux_with_kill(*args, **kw):
            if "kill-session" in args:
                self.windows.discard("wv-w1")
            return inner(*args, **kw)
        wab.tmux.side_effect = tmux_with_kill
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "refresh_workdir"):
            wab.watch(cfg, path, max_ticks=3)
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W2")
        self.assertNotIn("stopped", st)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("closed with kill-session (the wave is done)", log)
        self.assertNotIn("launch of W2 refused", log)

    # ----- 3: dash.py without rich -----
    def test_dash_without_rich_is_one_clear_line_and_rc_2(self):
        shim = self.tmp / "norich"
        (shim / "rich").mkdir(parents=True)
        (shim / "rich" / "__init__.py").write_text(
            "raise ModuleNotFoundError(\"No module named 'rich'\", name='rich')\n", encoding="utf-8")
        cfg, path = self.chain()
        env = dict(os.environ, PYTHONPATH=str(shim))
        r = subprocess.run([sys.executable, str(WAVES / "dash.py"), str(path)], capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(),
                         "dash.py needs the Python package rich: python3 -m pip install --user rich")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_dash_imported_without_rich_raises_import_error(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dash_norich", WAVES / "dash.py")
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"rich": None}):
            with self.assertRaises(ImportError):
                spec.loader.exec_module(mod)


class W4Round34Dashboard(Base):
    """Round 34: the current-wave panel shows the same status as the table (last_status fallback);
    a corrupt commit counter is shown as «?»."""

    def setUp(self):
        super().setUp()
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.dash = dash

    def panel_text(self, cfg):
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(
            self.dash.current_panel(cfg, wab.load_state(cfg)))
        return buf.getvalue()

    def test_panel_status_falls_back_to_last_status(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec(last_status="BLOCKED: нужен ключ")}})
        self.set_status(cfg, "W1", "")
        self.assertIn("статус: BLOCKED: нужен ключ", self.panel_text(cfg))
        st_file = wab.wave_dir(cfg, "W1") / "status"
        st_file.unlink()
        st_file.mkdir()  # unreadable
        self.assertIn("статус: BLOCKED: нужен ключ", self.panel_text(cfg))
        st_file.rmdir()
        self.set_status(cfg, "W1", "RUNNING")
        self.assertIn("статус: RUNNING", self.panel_text(cfg))
        w = wab.load_state(cfg)["waves"]["W1"]
        self.assertEqual(self.dash.wave_status(cfg, "W1", w), "RUNNING")

    def test_corrupt_commit_counter_is_a_question_mark(self):
        for bad in (-1, "3", 2.5, True, None, [1]):
            with self.subTest(bad=bad):
                self.assertIsNone(self.dash.wave_commits({"commits": bad, "cwd": self.cwd,
                                                          "started": 0, "start_rev": "x"}))
        self.assertEqual(self.dash.wave_commits({"commits": 0}), 0)
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(commits=-4)}})
        self.set_status(cfg, "W1", "RUNNING")
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(
            self.dash.waves_table(cfg, wab.load_state(cfg)))
        row = [l for l in buf.getvalue().splitlines() if l.strip().startswith("W1")]
        self.assertTrue(row and row[0].rstrip().endswith("?"), buf.getvalue())
        self.assertNotIn("-4", row[0])


class W4Round35(Base):
    """Round 35: a restarted wave archives the file results of its earlier try (a stale
    next-prompt.md never goes to the next wave); the identity of the chain is pinned in
    state.json across launch/watch/done; `done` with a window still open exits non-zero;
    the dashboard sums the commits of earlier attempts."""

    def setUp(self):
        super().setUp()
        self.windows = {"wv-w1"}
        self.exit_closes = True
        wab.tmux_alive.side_effect = lambda n: n in self.windows

        def fake_tmux(*args, **kw):
            self.tmux_calls.append(args)
            if "new-session" in args:
                self.windows.add(args[args.index("-s") + 1])
            if "send-keys" in args and "/exit" in args and self.exit_closes:
                tgt = args[args.index("-t") + 1]
                self.windows -= {n for n in set(self.windows) if wab.pane_target(n) == tgt}
            return subprocess.CompletedProcess(args, 0, "", "")
        p = mock.patch.object(wab, "tmux", side_effect=fake_tmux)
        p.start()
        self.addCleanup(p.stop)
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("go\n", encoding="utf-8")

    def exits(self, name="wv-w1"):
        return [c for c in self.tmux_calls if "send-keys" in c and "/exit" in c
                and wab.pane_target(name) in c]

    def launch(self, cfg, wave):
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            return wab.launch(cfg, wave, self.prompt)

    def log(self, cfg):
        p = cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # ----- 1: a restart archives next-prompt.md and result.md of the earlier try -----
    def test_restart_does_not_hand_a_stale_next_prompt_to_the_next_wave(self):
        for gate in (None, "external"):
            with self.subTest(gate=gate):
                cfg, _ = self.chain(merge_gate=gate)
                wdir = wab.wave_dir(cfg, "W1")
                for f in ("next-prompt.md", "result.md"):
                    (wdir / f).unlink(missing_ok=True)
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead")}})
                (wdir / "next-prompt.md").write_text("stale prompt of the failed try\n", encoding="utf-8")
                (wdir / "result.md").write_text("stale result\n", encoding="utf-8")
                self.windows = set()  # the failed try's window is gone
                self.assertTrue(self.launch(cfg, "W1"))
                self.assertFalse((wdir / "next-prompt.md").exists())
                self.assertFalse((wdir / "result.md").exists())
                st = self.get_state(cfg)
                arch = Path(st["waves"]["W1"]["attempts"][-1]["archive"])
                self.assertEqual((arch / "next-prompt.md").read_text(encoding="utf-8"),
                                 "stale prompt of the failed try\n")
                self.assertEqual((arch / "result.md").read_text(encoding="utf-8"), "stale result\n")
                # the new try says DONE without writing its own next-prompt.md
                self.set_status(cfg, "W1", "DONE")
                with mock.patch.object(wab, "launch") as nxt:
                    wab.tick(cfg, wab.load_state(cfg))
                nxt.assert_not_called()
                st = self.get_state(cfg)
                self.assertEqual(st.get("stopped"), "W1: DONE without next-prompt.md")
                self.assertNotEqual(st["waves"]["W1"]["phase"], "awaiting_merge")

    def test_second_restart_keeps_both_archives(self):
        cfg, _ = self.chain()
        wdir = wab.wave_dir(cfg, "W1")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead")}})
        archives = []
        for n in (1, 2):
            (wdir / "next-prompt.md").write_text(f"try {n}\n", encoding="utf-8")
            self.windows = set()
            self.assertTrue(self.launch(cfg, "W1"))
            st = self.get_state(cfg)
            archives.append(Path(st["waves"]["W1"]["attempts"][-1]["archive"]))
            st["waves"]["W1"]["phase"] = "dead"
            self.put_state(cfg, st)
        self.assertNotEqual(archives[0], archives[1])
        self.assertEqual([(a / "next-prompt.md").read_text(encoding="utf-8") for a in archives],
                         ["try 1\n", "try 2\n"])

    # ----- 2: the identity of the chain is pinned in state.json -----
    def awaiting(self, waves=("W1", "W2"), **over):
        cfg, path = self.chain(waves=list(waves), merge_gate="external", **over)
        self.put_state(cfg, {"current": None, "waves": {}})
        self.windows = set()
        self.assertTrue(self.launch(cfg, "W1"))
        st = self.get_state(cfg)
        self.assertIn("identity", st)
        st["waves"]["W1"]["phase"] = "awaiting_merge"
        self.put_state(cfg, st)
        self.windows = set()  # the handed-over wave's window has closed
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        return cfg, path

    def rewrite(self, path, **over):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.update(over)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return wab.load_chain(path)

    def refused_unchanged(self, cfg, fn):
        before = wab.state_path(cfg).read_bytes()
        calls = len(self.tmux_calls)
        with self.assertRaises(SystemExit) as cm:
            fn()
        self.assertIn("identity", str(cm.exception))
        self.assertNotIn("Traceback", str(cm.exception))
        self.assertEqual(wab.state_path(cfg).read_bytes(), before)
        self.assertEqual(self.tmux_calls[calls:], [])
        self.assertIn("identity", self.log(cfg))

    def test_changed_identity_between_handoff_and_launch_is_refused(self):
        for field, value in (("waves", ["W1", "W9"]), ("repo", "x/y"), ("workdir", "/tmp/other-wd")):
            with self.subTest(field=field):
                cfg, path = self.awaiting()
                fresh = self.rewrite(path, **{field: value})
                nxt = fresh["waves"][1]
                self.refused_unchanged(cfg, lambda: self.launch(fresh, nxt))

    def test_changed_identity_between_handoff_and_watch_is_refused(self):
        for field, value in (("waves", ["W1", "W9"]), ("repo", "x/y"), ("workdir", "/tmp/other-wd")):
            with self.subTest(field=field):
                cfg, path = self.awaiting()
                fresh = self.rewrite(path, **{field: value})
                with mock.patch.object(wab, "drop_stale_btab"):
                    self.refused_unchanged(cfg, lambda: wab.watch(fresh, path, max_ticks=1))

    def test_changed_identity_between_handoff_and_done_is_refused(self):
        for field, value in (("waves", ["W9"]), ("repo", "x/y"), ("workdir", "/tmp/other-wd")):
            with self.subTest(field=field):
                cfg, path = self.awaiting(waves=("W1",))
                fresh = self.rewrite(path, **{field: value})
                self.refused_unchanged(cfg, lambda: wab.done_cmd(fresh))

    def test_unchanged_identity_passes_and_tunables_may_change(self):
        cfg, path = self.awaiting()
        fresh = self.rewrite(path, ctx_limit=111, titles={"W1": "x"})
        self.assertTrue(self.launch(fresh, "W2"))
        self.assertEqual(self.get_state(cfg)["current"], "W2")
        cfg, path = self.awaiting(waves=("W1",))
        wab.done_cmd(wab.load_chain(path))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "done")

    def test_old_state_without_identity_is_migrated_not_refused(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.windows = set()
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=1)
        self.assertIn("identity", self.get_state(cfg))
        fresh = self.rewrite(path, repo="x/y")
        self.refused_unchanged(cfg, lambda: wab.done_cmd(fresh))
        st = self.get_state(cfg)
        del st["identity"]
        self.put_state(cfg, st)
        wab.done_cmd(wab.load_chain(path))
        st = self.get_state(cfg)
        self.assertEqual(st["waves"]["W1"]["phase"], "done")
        self.assertIn("identity", st)

    # ----- 3: done with a window still open exits non-zero -----
    def test_cli_done_with_a_window_still_open_exits_non_zero(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec(phase="awaiting_merge", pending_exit=True)}})
        self.exit_closes = False
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            wab.main(["wab.py", "done", str(path)])
        self.assertNotIn(cm.exception.code, (0, None))
        self.assertIn("still open", err.getvalue())
        self.assertTrue(self.get_state(cfg)["waves"]["W1"].get("pending_exit"))
        self.exit_closes = True
        wab.main(["wab.py", "done", str(path)])  # no SystemExit: rc 0
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    # ----- 4: the dashboard sums the commits of earlier attempts -----
    def test_dashboard_commits_include_earlier_attempts(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        w = self.wave_rec(commits=2, attempts=[{"commits": 3, "cwd": self.cwd, "started": 0},
                                               "junk", {"commits": 1, "cwd": self.cwd, "started": 0}])
        self.assertEqual(dash.wave_commits(w), 6)
        # an attempt whose counter was never fixed (never ended, e.g. not_ready) adds nothing
        w = self.wave_rec(commits=2, attempts=[{"phase": "not_ready", "start_rev": "a" * 40,
                                                "cwd": self.cwd, "started": 0}])
        self.assertEqual(dash.wave_commits(w), 2)
        # a corrupt counter of an attempt makes the total unknown, like the wave's own one
        for bad in (-1, "3", 2.5, True, None):
            with self.subTest(bad=bad):
                w = self.wave_rec(commits=2, attempts=[{"commits": bad, "cwd": self.cwd, "started": 0}])
                self.assertIsNone(dash.wave_commits(w))


class W4Round38DashGitTimeout(Base):
    """Round 38: the dashboard's time-window `git log` (records without start_rev) runs with a
    timeout; a hung or missing git leaves the commit counter unknown («?»), not a hung or dead
    frame and not a false 0."""

    def dash(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        return dash

    def legacy(self):
        return {k: v for k, v in self.wave_rec(phase="running").items() if k != "start_rev"}

    def test_git_log_gets_a_timeout(self):
        dash = self.dash()
        done = subprocess.CompletedProcess([], 0, stdout="a\nb\n", stderr="")
        with mock.patch.object(dash.subprocess, "run", return_value=done) as run:
            self.assertEqual(dash.commits_since(self.cwd, time.time() - 60), 2)
        self.assertIsInstance(run.call_args.kwargs.get("timeout"), (int, float))
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_failures_are_unknown_not_zero(self):
        dash = self.dash()
        for exc in (subprocess.TimeoutExpired(["git"], 10), FileNotFoundError("git"),
                    PermissionError("cwd"), subprocess.SubprocessError("x")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(dash.subprocess, "run", side_effect=exc):
                self.assertIsNone(dash.commits_since(self.cwd, time.time() - 60))
                self.assertIsNone(dash.wave_commits(self.legacy()))
                self.assertEqual(dash.fmt_commits(dash.wave_commits(self.legacy())), "?")

    def test_git_error_exit_is_unknown(self):
        dash = self.dash()
        bad = subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: not a git repository")
        with mock.patch.object(dash.subprocess, "run", return_value=bad):
            self.assertIsNone(dash.commits_since(self.cwd, time.time() - 60))

    def test_frame_survives_a_hung_git(self):
        dash = self.dash()
        cfg, _ = self.chain()
        st = {"current": "W1", "waves": {"W1": self.legacy()}}
        with mock.patch.object(dash.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["git"], 10)), \
                mock.patch.object(dash, "wave_stats", return_value={k: 0 for k in
                                                                     ("turns", "tools", "agents", "out", "read")}):
            table = dash.waves_table(cfg, st)
        self.assertEqual(str(table.columns[-1]._cells[0]), "?")


class W4Round39(Base):
    """Round 39: the watch that finishes the chain waits for the last window like `done` (still
    open -> exit 3 with a hint to run done); an unknown commit count of an ended wave is frozen as
    unknown (null), not recounted after the shared checkout moved; the dashboard shows an unknown
    `start_rev..HEAD` count as «?», not 0."""

    def dash(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        return dash

    # ----- 1: the last window after /exit -----
    def last_done(self, cfg, **rec):
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(**rec)}})
        self.set_status(cfg, "W1", "DONE")

    def test_watch_with_last_window_still_open_fails_like_done(self):
        cfg, path = self.chain(waves=["W1"])
        self.last_done(cfg)
        self.alive = True  # Claude ignores /exit
        self.assertFalse(wab.watch(cfg, path, max_ticks=3))
        st = self.get_state(cfg)
        self.assertIsNone(st["current"])
        self.assertTrue(st["waves"]["W1"].get("pending_exit"))
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("chain finished", log)
        self.assertIn("still open", log)
        self.assertIn("wab.py done", log)
        self.assertNotIn("watch stopped: no current wave", log)
        with self.assertRaises(SystemExit) as cm:  # the CLI: code 3, as `done` in the same case
            wab.main(["wab.py", "watch", str(path)])
        self.assertEqual(cm.exception.code, 3)
        self.alive = False  # the coordinator's done closes it out
        wab.main(["wab.py", "done", str(path)])
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    def test_watch_with_last_window_closed_succeeds(self):
        cfg, path = self.chain(waves=["W1"])
        self.last_done(cfg)
        self.alive = False
        self.assertTrue(wab.watch(cfg, path, max_ticks=3))
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("chain finished", log)
        self.assertNotIn("still open", log)

    def test_watch_waits_for_the_window_that_obeys_exit(self):
        cfg, path = self.chain(waves=["W1"])
        self.last_done(cfg)
        polls = []

        def alive(name):  # the window goes a few seconds after /exit
            polls.append(name)
            return len(polls) < 4
        with mock.patch.object(wab, "tmux_alive", side_effect=alive):
            self.assertTrue(wab.watch(cfg, path, max_ticks=3))
        self.assertNotIn("pending_exit", self.get_state(cfg)["waves"]["W1"])

    # ----- 2: an unknown count is frozen as unknown -----
    def test_unknown_count_at_end_is_frozen(self):
        cfg, _ = self.chain()
        st = {"current": "W1", "waves": {"W1": self.wave_rec(phase="done", start_rev="a" * 40)}}
        with mock.patch.object(wab, "count_wave_commits", return_value=None):
            wab.save_state(cfg, st)
        self.assertIn("commits", self.get_state(cfg)["waves"]["W1"])
        self.assertIsNone(self.get_state(cfg)["waves"]["W1"]["commits"])
        st = wab.load_state(cfg)  # the next wave switched the shared checkout: git answers now
        with mock.patch.object(wab, "count_wave_commits", return_value=7) as count:
            wab.save_state(cfg, st)
        count.assert_not_called()
        self.assertIsNone(self.get_state(cfg)["waves"]["W1"]["commits"])
        st = wab.load_state(cfg)  # a dead wave back alive drops the marker too
        st["waves"]["W1"]["phase"] = "running"
        wab.save_state(cfg, st)
        self.assertNotIn("commits", self.get_state(cfg)["waves"]["W1"])

    def test_dash_shows_the_frozen_unknown_as_question_mark(self):
        dash = self.dash()
        w = self.wave_rec(phase="done", start_rev="a" * 40, commits=None)
        with mock.patch.object(wab, "count_wave_commits", return_value=7) as count:
            self.assertEqual(dash.fmt_commits(dash.wave_commits(w)), "?")
        count.assert_not_called()
        w = self.wave_rec(start_rev="a" * 40, attempts=[{"commits": None}])
        with mock.patch.object(wab, "count_wave_commits", return_value=2):
            self.assertEqual(dash.fmt_commits(dash.wave_commits(w)), "?")

    # ----- 3: start_rev..HEAD that git cannot count -----
    def test_dash_unknown_start_rev_count_is_question_mark(self):
        dash = self.dash()
        w = self.wave_rec(start_rev="a" * 40)
        with mock.patch.object(wab, "count_wave_commits", return_value=None):
            self.assertIsNone(dash.wave_commits(w))
            self.assertEqual(dash.fmt_commits(dash.wave_commits(w)), "?")
        with mock.patch.object(wab, "count_wave_commits", return_value=0):
            self.assertEqual(dash.wave_commits(w), 0)


class W4Round40RestartContinues(Base):
    """Round 40 (Codex P2): a restart of a stopped wave is a continuation. The manifest, handoff.md
    and the branch of the failed try stay where they are; the first message of the new session says
    so before the unchanged task (which leads to `state.py init`, refused on an existing manifest).
    prompt.md stays the checked copy; a crash recovery on the restart sends the same message."""

    def setUp(self):
        super().setUp()
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("--wave W1 --plan /run/waves.json: start the wave\n", encoding="utf-8")

    def launch(self, cfg, wave="W1"):
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            return wab.launch(cfg, wave, self.prompt)

    def first_texts(self):
        return [s[2] for s in self.sent if s[0] == "text" and s[2]]

    def dead_with_work(self, cfg):
        wdir = wab.wave_dir(cfg, "W1")
        man = wdir / "superarmanda" / "manifest.json"
        man.parent.mkdir(parents=True, exist_ok=True)
        man.write_text('{"wave": "W1"}\n', encoding="utf-8")
        (wdir / "handoff.md").write_text(f"manifest: {man}\n", encoding="utf-8")
        (wdir / "next-prompt.md").write_text("stale\n", encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead")}})
        self.alive = False  # the failed try's window is gone
        return wdir, man

    def test_restart_first_message_says_continue_and_keeps_manifest_and_handoff(self):
        cfg, _ = self.chain()
        wdir, man = self.dead_with_work(cfg)
        self.assertTrue(self.launch(cfg))
        texts = self.first_texts()
        self.assertEqual(len(texts), 1)
        text = texts[0]
        note = text.index("ПЕРЕЗАПУСК волны W1")
        self.assertLess(note, text.index("--wave W1 --plan"))  # before the unchanged task
        self.assertIn("«Продолжение»", text)
        self.assertIn("state.py where", text)
        self.assertIn("tree_matches: false", text)
        self.assertIn("state.py init НЕ вызывай", text)
        self.assertIn("(попытка 2)", text)
        # the work of the failed try stays in place; only the file results are archived
        self.assertEqual(man.read_text(encoding="utf-8"), '{"wave": "W1"}\n')
        self.assertEqual((wdir / "handoff.md").read_text(encoding="utf-8"), f"manifest: {man}\n")
        self.assertFalse((wdir / "next-prompt.md").exists())
        # prompt.md is the unchanged checked copy; first-prompt.md is what was sent
        self.assertEqual((wdir / "prompt.md").read_text(encoding="utf-8"),
                         "--wave W1 --plan /run/waves.json: start the wave\n")
        self.assertEqual((wdir / "first-prompt.md").read_text(encoding="utf-8"), text + "\n")
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["restart"], 2)

    def test_not_ready_restart_says_continue_too(self):
        cfg, _ = self.chain()
        self.dead_with_work(cfg)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="not_ready")}})
        self.assertTrue(self.launch(cfg))
        self.assertIn("ПЕРЕЗАПУСК волны W1", self.first_texts()[0])

    def test_second_restart_counts_the_attempt(self):
        cfg, _ = self.chain()
        self.dead_with_work(cfg)
        self.assertTrue(self.launch(cfg))
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "dead"
        self.put_state(cfg, st)
        self.sent.clear()
        self.assertTrue(self.launch(cfg))
        self.assertIn("(попытка 3)", self.first_texts()[0])

    def test_ordinary_start_has_no_restart_note(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": None, "waves": {}})
        self.alive = False
        self.assertTrue(self.launch(cfg))
        text = self.first_texts()[0]
        self.assertNotIn("ПЕРЕЗАПУСК", text)
        self.assertTrue(text.endswith("Протокол — в системной инструкции.\n\n"
                                      "--wave W1 --plan /run/waves.json: start the wave"), text)
        self.assertNotIn("restart", self.get_state(cfg)["waves"]["W1"])
        # the next wave launched after a hand-over is an ordinary start too
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "awaiting_merge"
        self.put_state(cfg, st)
        self.sent.clear()
        self.assertTrue(self.launch(cfg, "W2"))
        self.assertNotIn("ПЕРЕЗАПУСК", self.first_texts()[0])

    def test_crash_recovery_of_a_restart_sends_the_same_message(self):
        cfg, _ = self.chain()
        wdir, man = self.dead_with_work(cfg)
        with mock.patch.object(wab, "start_session", side_effect=_Crash):
            with self.assertRaises(_Crash):
                self.launch(cfg)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "launching")
        self.assertEqual(self.sent, [])
        self.alive = True
        with mock.patch.object(wab, "drop_stale_btab"):
            wab.resume(cfg, wab.load_state(cfg))
        texts = self.first_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("ПЕРЕЗАПУСК волны W1 (попытка 2)", texts[0])
        self.assertLess(texts[0].index("ПЕРЕЗАПУСК"), texts[0].index("--wave W1 --plan"))
        self.assertTrue(man.exists() and (wdir / "handoff.md").exists())


class W4Round41RestartRules(Base):
    """Round 41. (1) Review MEDIUM: handoff.md may exist before the manifest (a checkpoint during
    the plan check, before `init`); only an existing manifest forbids `state.py init`, a wave with
    handoff.md alone reads it and runs its first `init` after the plan check. (2) Codex P2: a restart
    takes the saved prompt.md; a passed prompt file whose text differs is refused before any
    side effect (status STARTING, the launch intent)."""

    def setUp(self):
        super().setUp()
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("--wave W1 --plan /run/waves.json: start the wave\n", encoding="utf-8")

    launch = W4Round40RestartContinues.launch
    first_texts = W4Round40RestartContinues.first_texts

    def dead_with_handoff_only(self, cfg):
        wdir = wab.wave_dir(cfg, "W1")
        (wdir / "handoff.md").write_text("plan check in progress, no manifest yet\n", encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="dead")}})
        self.alive = False
        return wdir

    def assert_init_allowed_without_manifest(self, text, where):
        # the ban on init is bound to the manifest, not to handoff.md
        text = " ".join(text.split())  # markdown wraps lines anywhere
        self.assertNotIn("handoff.md или manifest", text, where)
        self.assertNotIn("`handoff.md` или manifest", text, where)
        self.assertIn("только handoff.md", text.replace("`", ""), where)
        self.assertIn("первый state.py init", text.replace("`", ""), where)

    def test_restart_with_handoff_but_no_manifest_may_init(self):
        cfg, _ = self.chain()
        wdir = self.dead_with_handoff_only(cfg)
        self.assertTrue(self.launch(cfg))
        text = self.first_texts()[0]
        self.assertIn("ПЕРЕЗАПУСК волны W1", text)
        self.assertIn("state.py init НЕ вызывай", text)  # still there, for the manifest case
        self.assertLess(text.index("manifest"), text.index("state.py init НЕ вызывай"))
        self.assert_init_allowed_without_manifest(text, "first message")
        self.assertFalse((wdir / "superarmanda" / "manifest.json").exists())

    def test_protocol_and_reference_bind_the_init_ban_to_the_manifest(self):
        proto = (WAVES / "PROTOCOL.md").read_text(encoding="utf-8")
        rule1 = proto[proto.index("1. **Старт или продолжение.**"):proto.index("2. **Контрольная точка.**")]
        self.assert_init_allowed_without_manifest(rule1, "PROTOCOL.md rule 1")
        ref = (ROOT / "skills" / "superarmanda" / "references" / "waves.md").read_text(encoding="utf-8")
        start = ref[ref.index("### Старт: `--wave"):ref.index("1. Рабочая копия")]
        self.assert_init_allowed_without_manifest(start, "waves.md «Старт» step 0")
        para = ref[ref.index("Перезапуск — **продолжение, а не чистый старт**"):]
        para = para[:para.index("\n\n")]
        self.assert_init_allowed_without_manifest(para, "waves.md «Перезапуск волны»")

    def relaunchable(self, cfg):
        """A real first launch (prompt.md saved), then the wave dies."""
        self.put_state(cfg, {"current": None, "waves": {}})
        self.alive = False
        self.assertTrue(self.launch(cfg))
        wdir = wab.wave_dir(cfg, "W1")
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "dead"
        self.put_state(cfg, st)
        (wdir / "status").write_text("RUNNING\n", encoding="utf-8")
        self.sent.clear()
        return wdir

    def test_restart_refuses_a_changed_prompt_file_before_any_side_effect(self):
        cfg, _ = self.chain()
        wdir = self.relaunchable(cfg)
        saved = (wdir / "prompt.md").read_bytes()
        before = wab.state_path(cfg).read_bytes()
        self.prompt.write_text("--wave W1 --plan /run/waves.json: a DIFFERENT task\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, r"(?s)prompt\.md.*differs|differs.*prompt\.md"):
            self.launch(cfg)
        self.assertEqual((wdir / "status").read_text(encoding="utf-8"), "RUNNING\n")
        self.assertEqual(wab.state_path(cfg).read_bytes(), before)
        self.assertEqual((wdir / "prompt.md").read_bytes(), saved)
        self.assertEqual(self.sent, [])

    def test_restart_refuses_another_file_with_another_text(self):
        cfg, _ = self.chain()
        wdir = self.relaunchable(cfg)
        other = self.tmp / "other.md"
        other.write_text("--wave W1 --plan /elsewhere.json: other\n", encoding="utf-8")
        before = wab.state_path(cfg).read_bytes()
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            with self.assertRaises(SystemExit):
                wab.launch(cfg, "W1", other)
        self.assertEqual(wab.state_path(cfg).read_bytes(), before)
        self.assertEqual((wdir / "status").read_text(encoding="utf-8"), "RUNNING\n")

    def test_restart_with_the_same_prompt_file_uses_the_saved_copy(self):
        cfg, _ = self.chain()
        wdir = self.relaunchable(cfg)
        saved = (wdir / "prompt.md").read_bytes()
        inode = (wdir / "prompt.md").stat().st_ino  # kept, not rewritten by a temp file + replace
        same = self.tmp / "copy.md"  # another path, the same text: accepted
        same.write_text("﻿--wave W1 --plan /run/waves.json: start the wave\n\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            self.assertTrue(wab.launch(cfg, "W1", same))
        self.assertEqual((wdir / "prompt.md").read_bytes(), saved)
        self.assertEqual((wdir / "prompt.md").stat().st_ino, inode)
        text = self.first_texts()[0]
        self.assertIn("ПЕРЕЗАПУСК волны W1 (попытка 2)", text)
        self.assertTrue(text.endswith("--wave W1 --plan /run/waves.json: start the wave"), text)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["prompt_file"], str(wdir / "prompt.md"))

    def test_restart_without_a_saved_copy_takes_the_passed_file(self):
        cfg, _ = self.chain()
        wdir = self.relaunchable(cfg)
        (wdir / "prompt.md").unlink()
        self.assertTrue(self.launch(cfg))
        self.assertEqual((wdir / "prompt.md").read_text(encoding="utf-8"),
                         "--wave W1 --plan /run/waves.json: start the wave\n")
        self.assertIn("ПЕРЕЗАПУСК волны W1", self.first_texts()[0])

    def test_ordinary_launch_of_the_next_wave_still_writes_its_prompt(self):
        cfg, _ = self.chain()
        wdir = self.relaunchable(cfg)
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "awaiting_merge"
        self.put_state(cfg, st)
        self.prompt.write_text("--wave W2 --plan /run/waves.json: next\n", encoding="utf-8")
        self.assertTrue(self.launch(cfg, "W2"))
        self.assertEqual((wab.wave_dir(cfg, "W2") / "prompt.md").read_text(encoding="utf-8"),
                         "--wave W2 --plan /run/waves.json: next\n")


class TmuxGuard(unittest.TestCase):
    """The guard itself: a socketless tmux must fail loudly, a private one passes the guard."""

    def test_the_environment_is_private(self):
        for var in ("TMUX", "TMUX_PANE", "WAB_TMUX_SOCKET"):
            self.assertNotIn(var, os.environ)
        self.assertTrue(os.environ["TMUX_TMPDIR"].startswith(tempfile.gettempdir()))

    def test_subprocess_refuses_tmux_without_a_socket(self):
        for call in (subprocess.run, subprocess.call, subprocess.check_output, subprocess.Popen):
            with self.assertRaisesRegex(AssertionError, "tmux without a private socket in tests"):
                call(["tmux", "ls"])
        with self.assertRaises(AssertionError):
            subprocess.run(["/usr/bin/tmux", "kill-server"])
        with self.assertRaises(AssertionError):
            subprocess.run("tmux ls", shell=True)
        with self.assertRaises(AssertionError):
            os.system("tmux ls")

    def test_a_private_socket_passes_the_guard(self):
        for argv in (["tmux", "-L", "wabguard-x", "ls"], ["tmux", "-S", "/nonexistent/x.sock", "ls"]):
            try:
                subprocess.run(argv, capture_output=True, timeout=30)
            except AssertionError:
                self.fail(f"guard refused {argv}")
            except (OSError, subprocess.SubprocessError):
                pass  # no tmux or no server: the guard let it through, which is the point
        try:
            subprocess.run("tmux -L wabguard-x ls", shell=True, capture_output=True, timeout=30)
        except AssertionError:
            self.fail("guard refused a shell tmux with -L")

    def test_the_path_shim_refuses_a_socketless_tmux_from_a_script(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed")
        r = subprocess.run(["bash", "-c", "tmux ls"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 97)
        self.assertIn("without a private socket", r.stderr)
        r = subprocess.run(["bash", "-c", "tmux -L wabguard-x ls"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 97, r.stderr)


class W4Round42DashAttemptMetrics(Base):
    """Round 42: the wave row's «Время», «Пик» and «↻» count the earlier attempts (archived by a
    restart) like the turns and commits do: durations and restarts are summed, the peak is the
    maximum. An attempt without `finished` (dead) ran until the next try started; corrupt or
    missing fields are skipped, not a crashed frame. «Контекст» stays the current session's."""

    def dash(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        return dash

    def row(self, dash, w):
        cfg, _ = self.chain()
        st = {"current": "W1", "waves": {"W1": w}}
        zero = {k: 0 for k in ("turns", "tools", "agents", "out", "read")}
        with mock.patch.object(dash, "wave_stats", return_value=zero), \
                mock.patch.object(dash, "wave_commits", return_value=0), \
                mock.patch.object(dash.wab, "tmux_alive", return_value=True):
            table = dash.waves_table(cfg, st)
        return {c.header: str(c._cells[0]) for c in table.columns}

    def test_row_sums_durations_and_restarts_and_takes_the_peak(self):
        dash = self.dash()
        now = time.time()
        w = self.wave_rec(started=now - 600, finished=now, peak=50_000, restarts=1, tokens=10_000,
                          attempts=[
                              # ended: 1 hour; its own end, not the next start
                              {"cwd": self.cwd, "started": now - 10_000, "finished": now - 6_400,
                               "peak": 250_000, "restarts": 2},
                              # dead (no finished): ran until the next try started (now - 600)
                              {"cwd": self.cwd, "started": now - 1_800, "peak": 120_000, "restarts": 3},
                          ])
        cells = self.row(dash, w)
        self.assertEqual(cells["Время"], dash.fmt_dur(3_600 + 1_200 + 600))
        self.assertEqual(cells["Пик"], dash.ktok(250_000))
        self.assertEqual(cells["↻"], "6")
        # the context bar stays the current session's fill, not an accumulated value
        self.assertEqual(cells["Контекст"], str(dash.bar(10_000, 300000, 16)))

    def test_corrupt_attempt_fields_are_skipped(self):
        dash = self.dash()
        now = time.time()
        attempts = ["junk", None,
                    {"started": "x", "finished": now - 100, "peak": "big", "restarts": "2"},
                    {"started": now - 900, "finished": None, "peak": True, "restarts": -1},
                    {"started": float("nan"), "peak": float("inf"), "restarts": 2.5},
                    {"started": now - 5_000, "finished": now - 4_000, "peak": 70_000, "restarts": 1}]
        w = self.wave_rec(started=now - 300, finished=now, peak=40_000, restarts=0, attempts=attempts)
        cells = self.row(dash, w)
        # the record with started now-900 has no usable end of its own: it ends at the nearest
        # later valid start among all records - the current try at now-300 (round 43), +600
        self.assertEqual(cells["Время"], dash.fmt_dur(300 + 1_000 + 600))
        self.assertEqual(cells["Пик"], dash.ktok(70_000))
        self.assertEqual(cells["↻"], "1")
        for bad in ("junk", None, 5, {"x": 1}):
            with self.subTest(attempts=bad):
                cells = self.row(dash, self.wave_rec(started=now - 60, finished=now, peak=1_000,
                                                     restarts=2, attempts=bad))
                self.assertEqual(cells["Время"], dash.fmt_dur(60))
                self.assertEqual(cells["Пик"], dash.ktok(1_000))
                self.assertEqual(cells["↻"], "2")

    def test_header_counts_restarts_of_earlier_attempts(self):
        dash = self.dash()
        from rich.console import Console
        cfg, _ = self.chain()
        w = self.wave_rec(restarts=1, attempts=[{"cwd": self.cwd, "started": 0, "restarts": 4}])
        st = {"current": "W1", "waves": {"W1": w}}
        with mock.patch.object(dash, "wave_stats", return_value={"turns": 0}):
            con = Console(width=200, record=True)
            con.print(dash.header(cfg, st))
        self.assertIn("свежих голов 5", con.export_text())


class W4Round43DashHugeAndOrder(Base):
    """Round 43: an int too big for a float (10**400) in peak/started/finished is not a number
    (math.isfinite would raise OverflowError and cost every frame); a dead attempt ends at the
    nearest later start among all records, not at the next one in list order; the header's
    «в работе» skips a corrupt or missing `started` instead of crashing the frame."""

    dash = W4Round42DashAttemptMetrics.dash
    row = W4Round42DashAttemptMetrics.row

    def test_huge_int_is_not_a_number(self):
        dash = self.dash()
        for x in (10**400, -10**400):
            self.assertIsNone(dash._num(x))
        self.assertEqual(dash._num(10**20), 10**20)

    def test_huge_int_fields_are_skipped_in_row_and_header(self):
        dash = self.dash()
        from rich.console import Console
        now = time.time()
        huge = 10**400
        w = self.wave_rec(started=now - 300, finished=huge, peak=huge, tokens=1_000,
                          attempts=[{"started": huge, "finished": huge, "peak": huge},
                                    {"started": now - 2_000, "finished": now - 1_000, "peak": 30_000},
                                    {"started": now - 5_000, "finished": huge, "peak": 20_000}])
        self.assertEqual(dash.wave_peak(w), 30_000)
        # current: no usable finished -> until now (300); attempt at now-2000: 1000; attempt at
        # now-5000 without a usable end: until the nearest later start (now-2000) -> 3000
        self.assertAlmostEqual(dash.wave_duration(w, now), 4_300, delta=1)
        cells = self.row(dash, w)
        self.assertEqual(cells["Пик"], dash.ktok(30_000))
        self.assertEqual(cells["Время"], dash.fmt_dur(4_300))
        cfg, _ = self.chain()
        bad = self.wave_rec(started=huge, finished=huge, peak=huge,
                            attempts=[{"started": huge, "peak": huge}])
        self.assertEqual(dash.wave_peak(bad), 0)
        self.assertEqual(dash.wave_duration(bad, now), 0)
        st = {"current": "W1", "waves": {"W1": bad}}
        zero = {k: 0 for k in ("turns", "tools", "agents", "out", "read")}
        with mock.patch.object(dash, "wave_stats", return_value=zero), \
                mock.patch.object(dash, "wave_commits", return_value=0), \
                mock.patch.object(dash.wab, "tmux_alive", return_value=True):
            con = Console(width=220, record=True)
            con.print(dash.header(cfg, st))
            con.print(dash.waves_table(cfg, st))
        text = con.export_text()
        self.assertIn("в работе", text)
        self.assertNotIn("OverflowError", text)

    def test_dead_attempt_ends_at_the_nearest_later_start(self):
        dash = self.dash()
        # list order is not time order: the dead try at 300 ends at the current start (500),
        # not at the next entry (100, earlier); the dead try at 100 ends at 300
        w = self.wave_rec(started=500, finished=600,
                          attempts=[{"started": 300}, {"started": 100}])
        self.assertEqual(dash.wave_duration(w, 10_000), 200 + 200 + 100)
        w = self.wave_rec(started=500, finished=600,
                          attempts=[{"started": 300}, {"started": 100, "finished": 150}])
        self.assertEqual(dash.wave_duration(w, 10_000), 200 + 50 + 100)
        # equal or unknown starts are not an end; nothing later known -> nothing added
        w = self.wave_rec(started="x", finished=600,
                          attempts=[{"started": 300}, {"started": 300}, {"started": 100}])
        self.assertEqual(dash.wave_duration(w, 10_000), 200)

    def test_header_skips_corrupt_or_missing_started(self):
        dash = self.dash()
        from rich.console import Console
        cfg, _ = self.chain()
        now = 1_000_000.0
        good = self.wave_rec(name="W2", started=now - 125)
        waves = {"W1": self.wave_rec(started="x"), "W2": good, "W3": self.wave_rec(name="W3")}
        del waves["W3"]["started"]
        for st_waves, expect in ((waves, dash.fmt_dur(125)),
                                 ({"W1": self.wave_rec(started=None)}, dash.fmt_dur(0))):
            with self.subTest(expect=expect), \
                    mock.patch.object(dash, "wave_stats", return_value={"turns": 0}), \
                    mock.patch.object(dash.time, "time", return_value=now):
                con = Console(width=220, record=True)
                con.print(dash.header(cfg, {"current": "W1", "waves": st_waves}))
                self.assertIn(f"в работе {expect}", con.export_text())



class W4Round44DashHeaderDone(Base):
    """Round 44: the header's «волн N/M» counts a wave as finished by the same wave_state the row
    and the pipeline show (DONE or awaiting_merge), not by the raw status file: a finished wave
    whose status file is gone, unreadable or replaced by a directory stays in the count."""

    dash = W4Round42DashAttemptMetrics.dash

    def header_text(self, dash, cfg, st):
        from rich.console import Console
        with mock.patch.object(dash, "wave_stats", return_value={"turns": 0}):
            con = Console(width=220, record=True)
            con.print(dash.header(cfg, st))
        return con.export_text()

    def test_finished_wave_without_status_file_is_counted(self):
        dash = self.dash()
        cfg, _ = self.chain(waves=["W1", "W2", "W3", "W4"])
        for w in ("W1", "W2", "W3", "W4"):
            wab.wave_dir(cfg, w).mkdir(parents=True, exist_ok=True)
        # W1: terminal phase done, status file deleted
        # W2: awaiting_merge, status file replaced by a directory
        (wab.wave_dir(cfg, "W2") / "status").mkdir()
        # W3: no terminal phase yet, the file is gone, the cached last status is DONE
        # W4: current, still running
        self.set_status(cfg, "W4", "RUNNING")
        st = {"current": "W4", "waves": {
            "W1": self.wave_rec(name="W1", phase="done"),
            "W2": self.wave_rec(name="W2", phase="awaiting_merge"),
            "W3": self.wave_rec(name="W3", last_status="DONE"),
            "W4": self.wave_rec(name="W4"),
        }}
        self.assertIn("волн 3/4", self.header_text(dash, cfg, st))
        # the count matches what the rows show
        keys = [dash.wave_state(cfg, st, w)[0] for w in cfg["waves"]]
        self.assertEqual(keys[:3], ["DONE", "awaiting_merge", "DONE"])

    def test_dead_and_pending_waves_are_not_counted(self):
        dash = self.dash()
        cfg, _ = self.chain(waves=["W1", "W2"])
        for w in ("W1", "W2"):
            wab.wave_dir(cfg, w).mkdir(parents=True, exist_ok=True)
        self.set_status(cfg, "W1", "DONE")
        st = {"current": "W1", "waves": {"W1": self.wave_rec(name="W1", phase="dead")}}
        self.assertIn("волн 0/2", self.header_text(dash, cfg, st))


class W4Round45DashHeaderAttemptStart(Base):
    """Round 45: «в работе» in the header starts at the earliest known start of the chain,
    archived tries included: a first wave restarted after dead or not_ready keeps its original
    start in `attempts`, and the timer must not reset to the retry. Corrupt starts of archived
    tries (a string, a bool, NaN, a huge int) are skipped, not a crashed frame."""

    dash = W4Round42DashAttemptMetrics.dash

    def header_text(self, dash, cfg, st, now):
        from rich.console import Console
        with mock.patch.object(dash, "wave_stats", return_value={"turns": 0}), \
                mock.patch.object(dash.time, "time", return_value=now):
            con = Console(width=220, record=True)
            con.print(dash.header(cfg, st))
        return con.export_text()

    def test_restarted_first_wave_keeps_its_original_start(self):
        dash = self.dash()
        cfg, _ = self.chain(waves=["W1", "W2"])
        now = 1_000_000.0
        w1 = self.wave_rec(name="W1", started=now - 100)
        w1["attempts"] = [{"started": now - 5000, "phase": "dead"}]
        st = {"current": "W1", "waves": {"W1": w1}}
        self.assertIn(f"в работе {dash.fmt_dur(5000)}", self.header_text(dash, cfg, st, now))

    def test_corrupt_attempt_starts_are_skipped(self):
        dash = self.dash()
        cfg, _ = self.chain(waves=["W1"])
        now = 1_000_000.0
        w1 = self.wave_rec(name="W1", started=now - 100)
        w1["attempts"] = ["junk", {"started": "yesterday"}, {"started": True},
                          {"started": float("nan")}, {"started": -(10 ** 400)},
                          {"started": now - 300}]
        st = {"current": "W1", "waves": {"W1": w1}}
        self.assertIn(f"в работе {dash.fmt_dur(300)}", self.header_text(dash, cfg, st, now))
        w1["attempts"] = "corrupt"
        self.assertIn(f"в работе {dash.fmt_dur(100)}", self.header_text(dash, cfg, st, now))


class PlanPin(Base):
    """T1: chain.json plan_sha256 pins the approved waves.json before every wave launch."""

    def setUp(self):
        super().setUp()
        self.alive = False
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("go\n", encoding="utf-8")
        p = mock.patch.object(wab, "drop_stale_btab")
        p.start()
        self.addCleanup(p.stop)
        self.plan_bytes = b'{"waves": []}\n'
        self.pin = hashlib.sha256(self.plan_bytes).hexdigest()

    def pinned(self, write=True, **over):
        cfg, path = self.chain(plan_sha256=self.pin, **over)
        if write:
            (cfg["run_dir"] / "waves.json").write_bytes(self.plan_bytes)
        return cfg, path

    def attempt(self, cfg, wave="W1"):
        self.tmux_calls.clear()
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd) as pc:
            self.prepare = pc
            return wab.launch(cfg, wave, self.prompt)

    def refused(self, cfg):
        with self.assertRaises(SystemExit) as cm:
            self.attempt(cfg)
        self.assertTrue(str(cm.exception).startswith("BLOCKED: plan changed since approval"),
                        str(cm.exception))
        self.assertFalse(self.prepare.called)
        self.assertEqual([c for c in self.tmux_calls if c[1] == "new-session"], [])
        st = wab.load_state(cfg)
        self.assertNotEqual(st.get("waves", {}).get("W1", {}).get("phase"), "starting")
        return str(cm.exception)

    def test_matching_pin_launches(self):
        cfg, _ = self.pinned()
        self.assertTrue(self.attempt(cfg))
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), 1)

    def test_changed_plan_refuses_before_any_launch_step(self):
        cfg, _ = self.pinned()
        (cfg["run_dir"] / "waves.json").write_bytes(self.plan_bytes + b" ")
        self.refused(cfg)

    def test_deleted_plan_refuses(self):
        cfg, _ = self.pinned(write=False)
        self.refused(cfg)

    def test_symlinked_or_non_regular_plan_refuses(self):
        cfg, _ = self.pinned(write=False)
        target = self.tmp / "real.json"
        target.write_bytes(self.plan_bytes)
        os.symlink(target, cfg["run_dir"] / "waves.json")
        self.refused(cfg)
        os.unlink(cfg["run_dir"] / "waves.json")
        (cfg["run_dir"] / "waves.json").mkdir()
        self.refused(cfg)

    def test_oversized_plan_refuses(self):
        cfg, _ = self.pinned(write=False)
        (cfg["run_dir"] / "waves.json").write_bytes(b"x" * (1024 * 1024 + 1))
        self.refused(cfg)

    def test_no_pin_does_not_read_waves_json(self):
        cfg, _ = self.chain()
        (cfg["run_dir"] / "waves.json").write_bytes(b"anything")
        with mock.patch.object(wab.os, "open", wraps=os.open) as op:
            self.assertTrue(self.attempt(cfg))
        self.assertFalse([c for c in op.call_args_list if "waves.json" in str(c.args[0])])

    def test_invalid_pin_is_refused_by_load_chain(self):
        for bad in ("", None, 0, False, "A" * 64, "0" * 63, "g" * 64, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as cm:
                    self.chain(plan_sha256=bad) if bad is not None else \
                        wab.load_chain(self.write_raw(plan_sha256=None))
                self.assertIn("plan_sha256", str(cm.exception))

    def write_raw(self, **over):
        doc = {"chain": CHAIN, "run_id": RUN_ID, "repo": "o/r", "waves": ["W1", "W2"],
               "tmux_prefix": "wv-", **over}
        path = self.tmp / "raw-chain.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    # ----- round 4/2: chain.json must live outside run_dir -----
    def raw_at(self, where, **over):
        doc = {"chain": CHAIN, "run_id": RUN_ID, "repo": "o/r", "waves": ["W1", "W2"],
               "tmux_prefix": "wv-", **over}
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(doc), encoding="utf-8")
        return where

    def test_chain_json_inside_run_dir_is_refused(self):  # (a)
        run = self.tmp / "base" / CHAIN / RUN_ID
        for create in (True, False):
            with self.subTest(create=create, where="run_dir itself"):
                path = self.raw_at(run / "chain.json", run_dir=str(self.tmp / "base"))
                with self.assertRaises(SystemExit) as cm:
                    wab.load_chain(path, create=create)
                self.assertIn("chain.json must live outside run_dir", str(cm.exception))
            with self.subTest(create=create, where="subdirectory"):
                path = self.raw_at(run / "sub" / "chain.json", run_dir=str(self.tmp / "base"))
                with self.assertRaises(SystemExit) as cm:
                    wab.load_chain(path, create=create)
                self.assertIn("chain.json must live outside run_dir", str(cm.exception))
        # a relative run_dir that climbs back over the chain file's own directory
        path = self.raw_at(self.tmp / "x" / CHAIN / RUN_ID / "chain.json", run_dir="../..")
        with self.assertRaises(SystemExit) as cm:
            wab.load_chain(path, create=False)
        self.assertIn("chain.json must live outside run_dir", str(cm.exception))

    def test_chain_json_symlinked_into_run_dir_is_refused(self):  # (b)
        run = self.tmp / "base" / CHAIN / RUN_ID
        run.mkdir(parents=True)
        real = self.raw_at(run / "chain.json", run_dir=str(self.tmp / "base"))
        link = self.tmp / "outside" / "chain.json"
        link.parent.mkdir()
        os.symlink(real, link)
        with self.assertRaises(SystemExit) as cm:
            wab.load_chain(link)
        self.assertIn("chain.json must live outside run_dir", str(cm.exception))

    def test_a_run_dir_through_a_symlink_to_the_chain_dir_is_refused(self):
        cfgdir = self.tmp / "cfgdir"
        path = self.raw_at(cfgdir / "chain.json", run_dir=str(self.tmp / "alias"))
        (cfgdir / CHAIN).mkdir()
        os.symlink(cfgdir, self.tmp / "alias")
        # alias/<chain>/<run_id> resolves to cfgdir/<chain>/<run_id>: not containing chain.json
        wab.load_chain(path, create=False)
        os.symlink(self.tmp / "cfgdir", cfgdir / CHAIN / RUN_ID)  # now run_dir resolves to cfgdir itself
        with self.assertRaises(SystemExit) as cm:
            wab.load_chain(path, create=False)
        self.assertIn("chain.json must live outside run_dir", str(cm.exception))

    def test_joint_swap_of_plan_and_pin_inside_run_dir_never_passes(self):  # (c)
        run = self.tmp / "base" / CHAIN / RUN_ID
        run.mkdir(parents=True)
        evil = b'{"waves": ["evil"]}\n'
        (run / "waves.json").write_bytes(evil)
        path = self.raw_at(run / "chain.json", run_dir=str(self.tmp / "base"),
                           plan_sha256=hashlib.sha256(evil).hexdigest())
        with self.assertRaises(SystemExit) as cm:
            wab.load_chain(path)
        self.assertIn("chain.json must live outside run_dir", str(cm.exception))

    def test_ordinary_layouts_still_load(self):  # (d)
        path = self.raw_at(self.tmp / "ok1" / "chain.json")
        self.assertEqual(wab.load_chain(path)["run_dir"], path.parent.resolve() / "runs" / CHAIN / RUN_ID)
        path = self.raw_at(self.tmp / "ok2" / "chain.json", run_dir=str(self.tmp / "elsewhere"))
        self.assertEqual(wab.load_chain(path)["run_dir"], self.tmp / "elsewhere" / CHAIN / RUN_ID)
        path = self.raw_at(self.tmp / "ok3" / "chain.json", run_dir="../shared")
        wab.load_chain(path, create=False)
        # a sibling whose name merely starts like the chain file's directory is not inside it
        path = self.raw_at(self.tmp / "ok4" / "chain.json", run_dir=str(self.tmp / "ok4-runs"))
        wab.load_chain(path)

    def test_dispatcher_launch_of_next_wave_refuses_on_changed_plan(self):
        cfg, _ = self.pinned(merge_gate="auto", base_branch="main")
        self.gh_handler = self.merged_view
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.auto_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd) as pc:
            wab.tick(cfg, wab.load_state(cfg))
        self.assertFalse(pc.called)
        st = self.get_state(cfg)
        self.assertIn("launch of W2 refused", st.get("stopped", ""))
        self.assertNotIn("W2", st["waves"])
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("plan changed since approval", log)

    def test_pin_goes_into_tmux_env_only_when_set(self):
        cfg, _ = self.pinned()
        self.attempt(cfg)
        new = [c for c in self.tmux_calls if c[1] == "new-session"][0]
        self.assertIn(f"WAB_PLAN_SHA256={self.pin}", new)
        cfg2, _ = self.chain()
        self.put_state(cfg2, {})
        self.alive = False
        self.attempt(cfg2)
        new = [c for c in self.tmux_calls if c[1] == "new-session"][0]
        self.assertFalse([a for a in new if str(a).startswith("WAB_PLAN_SHA256")])

    # ----- recovery of a launch that died after the intent was saved -----
    def launching(self, cfg):
        pf = self.tmp / "p.md"
        pf.write_text("go\n", encoding="utf-8")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            phase="launching", sessions=["sid-1"], prompt_file=str(pf))}})

    def recover(self, cfg, path, alive=False):
        self.alive = alive
        self.tmux_calls.clear()
        self.sent.clear()
        wab.watch(cfg, path, max_ticks=2)

    def assert_recovery_refused(self, cfg):
        self.assertEqual([c for c in self.tmux_calls if c[1] == "new-session"], [])
        self.assertEqual(self.sent, [])
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("BLOCKED: plan changed since approval", log)
        self.assertIn("BLOCKED: plan changed since approval",
                      (wab.wave_dir(cfg, "W1") / "status").read_text(encoding="utf-8"))
        self.assertTrue([t for t in self.tg if "plan changed since approval" in t], self.tg)

    def test_recover_launch_refuses_changed_plan(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path)
        self.assert_recovery_refused(cfg)

    def test_recover_launch_refuses_deleted_plan(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        (cfg["run_dir"] / "waves.json").unlink()
        self.recover(cfg, path)
        self.assert_recovery_refused(cfg)

    def test_recover_launch_with_live_window_does_not_send_prompt_on_changed_plan(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path, alive=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "not_ready")

    def test_recover_launch_starting_phase_does_not_send_prompt_on_changed_plan(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "starting"
        self.put_state(cfg, st)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path, alive=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "not_ready")

    def test_recover_launch_with_unchanged_plan_goes_on(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        self.recover(cfg, path)
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), 1)
        self.assertEqual(len(self.sent), 1)

    def test_recover_launch_without_pin_goes_on(self):
        cfg, path = self.chain()
        self.launching(cfg)
        self.recover(cfg, path)
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), 1)
        self.assertEqual(len(self.sent), 1)

    # ----- the typed first prompt waits for Enter when the dispatcher dies -----
    def typed_first_prompt(self, cfg):
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            phase="sending", sessions=["sid-1"], pending_enter="first prompt")}})

    def test_pending_enter_of_first_prompt_is_not_pressed_on_changed_plan(self):
        cfg, path = self.pinned()
        self.typed_first_prompt(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.enters.clear()
        self.recover(cfg, path, alive=True)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.sent, [])
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        self.assertNotIn("pending_enter", w)
        self.assertIn("BLOCKED: plan changed since approval",
                      (wab.wave_dir(cfg, "W1") / "status").read_text(encoding="utf-8"))
        self.assertIn("BLOCKED: plan changed since approval",
                      (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_pending_enter_of_first_prompt_is_pressed_when_plan_unchanged(self):
        cfg, path = self.pinned()
        self.typed_first_prompt(cfg)
        self.enters.clear()
        self.recover(cfg, path, alive=True)
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

    def test_pending_enter_of_first_prompt_is_pressed_without_pin(self):
        cfg, path = self.chain()
        self.typed_first_prompt(cfg)
        self.enters.clear()
        self.recover(cfg, path, alive=True)
        self.assertEqual(self.enters, ["wv-w1"])

    # ----- a refused pin closes the live session that holds the typed, stale prompt -----
    def kills(self):
        return [c for c in self.tmux_calls if "kill-session" in c]

    def test_pin_refusal_after_typed_prompt_kills_session_and_allows_relaunch(self):
        cfg, path = self.pinned()
        self.typed_first_prompt(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.enters.clear()
        self.recover(cfg, path, alive=True)
        self.assertEqual(len(self.kills()), 1)
        self.assertIn("=wv-w1", self.kills()[0])
        self.assertEqual(self.enters, [])
        w = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        self.assertTrue([t for t in self.tg if "закрыта" in t], self.tg)
        self.assertIn("closed", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        # the corrected plan: a repeated launch of the same wave is allowed (no session left)
        (cfg["run_dir"] / "waves.json").write_bytes(self.plan_bytes)
        self.attempt(cfg)
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), 1)

    def test_pin_refusal_with_session_that_survives_kill_says_so(self):
        cfg, path = self.pinned()
        self.typed_first_prompt(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.kill_closes = False
        self.recover(cfg, path, alive=True)
        self.assertEqual(len(self.kills()), 1)
        self.assertTrue([t for t in self.tg if "tmux kill-session -t" in t], self.tg)
        self.assertIn("tmux kill-session -t", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_pin_refusal_in_deliver_first_prompt_kills_live_session(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "starting"
        self.put_state(cfg, st)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path, alive=True)
        self.assertEqual(len(self.kills()), 1)
        self.assertEqual(self.sent, [])

    def _starting_pinned(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        st = self.get_state(cfg)
        st["waves"]["W1"]["phase"] = "starting"
        self.put_state(cfg, st)
        return cfg, path

    def test_plan_changed_during_wait_ready_does_not_paste_prompt(self):
        cfg, path = self._starting_pinned()

        def changing(name, timeout=90):
            (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
            return True

        with unittest.mock.patch.object(wab, "wait_ready", side_effect=changing):
            self.recover(cfg, path, alive=True)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.kills()), 1)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "not_ready")
        self.assertIn("BLOCKED: plan changed since approval",
                      (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_plan_unchanged_during_wait_ready_sends_prompt(self):
        cfg, path = self._starting_pinned()
        with unittest.mock.patch.object(wab, "wait_ready", return_value=True):
            self.recover(cfg, path, alive=True)
        self.assertEqual(self.kills(), [])
        self.assertEqual(len([s for s in self.sent if s[0] == "text"]), 1)

    def test_pin_refusal_in_recover_launch_kills_live_session(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path, alive=True)
        self.assertEqual(len(self.kills()), 1)
        self.assertEqual(self.sent, [])

    def test_pin_refusal_without_session_does_not_kill(self):
        cfg, path = self.pinned()
        self.launching(cfg)
        (cfg["run_dir"] / "waves.json").write_bytes(b"tampered")
        self.recover(cfg, path, alive=False)
        self.assertEqual(self.kills(), [])

    def test_plan_pin_is_tunable(self):
        cfg, _ = self.pinned()
        other = dict(cfg, plan_sha256="1" * 64)
        self.assertEqual(wab._identity(cfg), wab._identity(other))


# ---------------------------------------------------------------- W5: gate.py (pure functions)
import gate  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40
FP = "f" * 64
BOT = {"login": "chatgpt-codex-connector[bot]", "type": "Bot"}
HUMAN = {"login": "chatgpt-codex-connector[bot]", "type": "User"}
CLEAN_BODY = "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `{short}`"


def green_facts(**over):
    facts = {
        "pr": {"state": "open", "merged": False, "draft": False, "head": HEAD, "base": "main"},
        "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}],
        "reviews": [{"user": BOT, "commit_id": HEAD, "state": "COMMENTED"}],
        "review_comments": [], "issue_comments": [], "triggers": [], "resolved": {}, "threads": [],
    }
    facts.update(over)
    return facts


def result(status="pass", head=HEAD, fp=FP, at="2026-10-01T10:05:00Z"):
    return {"status": status, "head": head, "tree_fingerprint": fp, "recorded_at": at, "session_id": "s"}


def green_manifest(**task):
    entry = {"status": "ready_for_pr_review", "decisions": [],
             "results": {"coder": result(), "tester": result(), "cross_provider_reviewer": result()}}
    entry.update(task)
    return {"head": HEAD, "tasks": {"T1": entry}}


def summary(status="✅ **Completed**", short=HEAD[:7], user=BOT, extra=""):
    """A Codex review summary comment as GitHub shows it (table rows of the review)."""
    body = ("<!-- codex-pull-request-review-summary -->\n## Codex Review Summary\n\n"
            "| Task | Status | Commit | Trigger |\n|---|---|---|---|\n"
            f"| 📝 **Code Review** | {status} <relative-time datetime=\"2026-10-01T17:21:42Z\">2026-10-01T17:21:42Z"
            f"</relative-time> | `{short}` | Manual request |\n{extra}")
    return {"id": 3, "user": user, "body": body}


WORK = {"clean": True, "head": HEAD, "fingerprint": FP}


class GateVerdict(unittest.TestCase):
    def ev(self, facts=None, manifest="green", work=None, head=HEAD):
        return gate.evaluate(green_facts() if facts is None else facts, head,
                             green_manifest() if manifest == "green" else manifest, WORK if work is None else work,
                             "main")

    def test_everything_in_order_passes(self):
        v = self.ev()
        self.assertEqual((v["verdict"], v["reasons"], v["unresolved"], v["draft"]), ("pass", [], [], False))

    def test_stale_codex_review_waits(self):  # acceptance 1
        v = self.ev(green_facts(reviews=[{"user": BOT, "commit_id": OLD, "state": "COMMENTED"}]))
        self.assertEqual(v["verdict"], "wait")

    def test_codex_review_pending_or_dismissed_on_head_waits(self):  # acceptance 1
        for state in ("PENDING", "DISMISSED", ""):
            with self.subTest(state=state):
                v = self.ev(green_facts(reviews=[{"user": BOT, "commit_id": HEAD, "state": state}]))
                self.assertEqual(v["verdict"], "wait")

    def test_codex_review_states_that_finish(self):
        for state in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED"):
            with self.subTest(state=state):
                done = gate.codex_on_head(
                    green_facts(reviews=[{"user": BOT, "commit_id": HEAD, "state": state}]), HEAD)
                self.assertTrue(done["done"])

    def test_a_human_with_the_bot_login_is_not_codex(self):
        facts = green_facts(reviews=[{"user": HUMAN, "commit_id": HEAD, "state": "COMMENTED"}])
        self.assertEqual(self.ev(facts)["verdict"], "wait")

    def test_only_a_completed_summary_row_naming_head_finishes(self):  # D1
        short = HEAD[:7]
        cases = [
            ("completed, head", summary(), {short: HEAD}, True),
            ("completed, old sha", summary(short=OLD[:7]), {OLD[:7]: OLD, short: HEAD}, False),
            ("in progress, head", summary(status="⏳ **In progress**"), {short: HEAD}, False),
            ("completed, sha unresolved", summary(), {}, False),
            ("completed, human with the bot login", summary(user=HUMAN), {short: HEAD}, False),
            ("completed, other author", summary(user={"login": "someone", "type": "User"}), {short: HEAD}, False),
            ("sha not alone in its cell", summary(short=short + "` and `" + short), {short: HEAD}, False),
        ]
        for name, comment, resolved, want in cases:
            with self.subTest(case=name):
                facts = green_facts(reviews=[], issue_comments=[comment], resolved=resolved)
                self.assertEqual(gate.codex_on_head(facts, HEAD)["done"], want)
                self.assertEqual(self.ev(facts)["verdict"], "pass" if want else "wait")

    def test_a_row_is_judged_alone_a_completed_one_does_not_vouch_for_another(self):
        mixed = summary(status="⏳ **In progress**", extra="| other | ✅ **Completed** | `1234567` | x |\n")
        facts = green_facts(reviews=[], issue_comments=[mixed], resolved={HEAD[:7]: HEAD, "1234567": OLD})
        self.assertFalse(gate.codex_on_head(facts, HEAD)["done"])

    def test_clean_comment_thumbs_and_edited_triggers_are_no_evidence(self):  # D1
        clean = {"user": BOT, "body": CLEAN_BODY.format(short=HEAD[:7])}
        trigger = {"id": 7, "user": {"login": "me", "type": "User"},
                   "body": f"@codex review\n<!-- superarmanda:codex-review head={HEAD} -->"}
        facts = green_facts(reviews=[], issue_comments=[clean, trigger], resolved={HEAD[:7]: HEAD},
                            pr_reactions=[{"content": "+1", "user": BOT, "created_at": "2099-01-01T00:00:00Z"}],
                            triggers=[{"id": 7, "body": trigger["body"], "reactions": [{"content": "+1", "user": BOT}]}])
        self.assertFalse(gate.codex_on_head(facts, HEAD)["done"])
        self.assertEqual(self.ev(facts)["verdict"], "wait")
    def test_pending_check_waits_and_failed_check_fails_without_a_merge(self):  # acceptance 2
        pending = [{"name": "ci", "status": "completed", "conclusion": "success"},
                   {"name": "e2e", "status": "in_progress", "conclusion": None}]
        v = self.ev(green_facts(check_runs=pending))
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("e2e", v["reasons"][0])
        failed = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
        v = self.ev(green_facts(check_runs=failed))
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("ci", v["reasons"][0])

    def test_success_needs_completed_and_success_not_a_status_word(self):
        for run in ({"name": "x", "status": "completed", "conclusion": "skipped"},
                    {"name": "x", "status": "completed", "conclusion": "neutral"},
                    {"name": "x", "status": "completed", "conclusion": None},
                    {"name": "x", "status": "queued", "conclusion": "success"}):
            with self.subTest(run=run):
                got = gate.checks([run])
                self.assertTrue(got["pending"] or got["failed"])
        self.assertEqual(gate.checks([{"name": "x", "status": "completed", "conclusion": "success"}]),
                         {"total": 1, "pending": [], "failed": []})

    def test_no_check_runs_waits(self):
        self.assertEqual(self.ev(green_facts(check_runs=[]))["verdict"], "wait")

    def test_p1_and_p0_badges_on_head_fail_p2_does_not(self):  # acceptance 3
        def inline(badge, commit=HEAD, user=BOT):
            return {"user": user, "commit_id": HEAD, "original_commit_id": commit,
                    "body": f"**![{badge} Badge](x)** text", "html_url": "u"}
        for badge, want in (("P1", "fail"), ("P0", "fail"), ("P2", "pass")):
            with self.subTest(badge=badge):
                self.assertEqual(self.ev(green_facts(review_comments=[inline(badge)]))["verdict"], want)
        # G1: GitHub moves `commit_id` of an open comment to the latest commit; `original_commit_id`
        # says where it was written: a P1 written on an older commit is not a P1 of HEAD
        old = green_facts(review_comments=[inline("P1", commit=OLD)])
        self.assertEqual(self.ev(old)["verdict"], "pass")
        self.assertEqual(gate.codex_on_head(old, HEAD)["findings"], 0)
        self.assertEqual(gate.codex_on_head(green_facts(review_comments=[inline("P2")]), HEAD)["findings"], 1)
        # and the other way round: written on HEAD, still counted when GitHub keeps commit_id elsewhere
        moved = dict(inline("P1"), commit_id=OLD)
        self.assertEqual(self.ev(green_facts(review_comments=[moved]))["verdict"], "fail")
        self.assertEqual(self.ev(green_facts(review_comments=[inline("P1", user=HUMAN)]))["verdict"], "pass")

    def test_old_open_p1_threads_do_not_fail_but_are_counted_for_the_owner(self):  # G1
        threads = [{"id": "T1", "isResolved": False, "body": "**![P1 Badge](x)** old"},
                   {"id": "T2", "isResolved": False, "body": "![P2 Badge](x) minor"},
                   {"id": "T3", "isResolved": True, "body": "![P0 Badge](x) fixed"}]
        old = {"user": BOT, "commit_id": HEAD, "original_commit_id": OLD, "body": "![P1 Badge](x) old"}
        v = self.ev(green_facts(review_comments=[old], threads=threads))
        self.assertEqual((v["verdict"], v["old_p01"], v["unresolved"]), ("pass", 1, ["T1", "T2"]))

    def test_the_comment_anchor_is_part_of_the_second_collection(self):  # G1 / F2
        a = green_facts(review_comments=[{"id": 1, "user": BOT, "commit_id": HEAD, "original_commit_id": OLD, "body": "x"}])
        b = green_facts(review_comments=[{"id": 1, "user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "x"}])
        self.assertNotEqual(gate.critical(a), gate.critical(b))

    def test_manifest_without_a_coder_pass_on_head_fails(self):  # Codex P1: roles may be recorded out of order
        no_coder = green_manifest()
        del no_coder["tasks"]["T1"]["results"]["coder"]
        v = self.ev(manifest=no_coder)
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("нет coder pass на HEAD", "; ".join(v["reasons"]))
        coder_findings = green_manifest()
        coder_findings["tasks"]["T1"]["results"]["coder"] = result("findings")
        self.assertEqual(self.ev(manifest=coder_findings)["verdict"], "fail")
        coder_old = green_manifest()
        coder_old["tasks"]["T1"]["results"]["coder"] = result(head=OLD)
        self.assertEqual(self.ev(manifest=coder_old)["verdict"], "fail")
        coder_tree = green_manifest()
        coder_tree["tasks"]["T1"]["results"]["coder"] = result(fp="0" * 64)
        self.assertEqual(self.ev(manifest=coder_tree)["verdict"], "fail")

    def test_manifest_without_tester_pass_or_with_another_head_fails(self):  # acceptance 4
        no_tester = green_manifest()
        del no_tester["tasks"]["T1"]["results"]["tester"]
        self.assertEqual(self.ev(manifest=no_tester)["verdict"], "fail")
        tester_old_head = green_manifest()
        tester_old_head["tasks"]["T1"]["results"]["tester"] = result(head=OLD)
        self.assertEqual(self.ev(manifest=tester_old_head)["verdict"], "fail")
        tester_findings = green_manifest()
        tester_findings["tasks"]["T1"]["results"]["tester"] = result("findings")
        self.assertEqual(self.ev(manifest=tester_findings)["verdict"], "fail")
        other = green_manifest()
        other["head"] = OLD
        self.assertEqual(self.ev(manifest=other)["verdict"], "fail")
        for broken in (None, [], {"head": HEAD}, {"head": HEAD, "tasks": {}}):
            with self.subTest(manifest=broken):
                self.assertEqual(self.ev(manifest=broken)["verdict"], "fail")

    def test_task_statuses_needs_decision_and_blocked_fail(self):
        for status in ("needs_decision", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(self.ev(manifest=green_manifest(status=status))["verdict"], "fail")

    def test_unfinished_fix_loop_statuses_fail(self):  # r6 HIGH
        for status in ("needs_fix", "needs_verification", "pending", "in_progress", None):
            with self.subTest(status=status):
                v = self.ev(manifest=green_manifest(status=status))
                self.assertEqual(v["verdict"], "fail")
                self.assertTrue(any("цикл исправлений не завершён" in r for r in v["reasons"]), v["reasons"])
        self.assertEqual(self.ev(manifest=green_manifest(status="ready_for_pr_review"))["verdict"], "pass")

    def test_other_sources_findings_on_head_fail(self):  # r6 HIGH
        for role in ("github_codex_review", "coderabbit"):
            for status in ("findings", "fail"):
                with self.subTest(role=role, status=status):
                    m = green_manifest()
                    m["tasks"]["T1"]["results"][role] = result(status)
                    v = self.ev(manifest=m)
                    self.assertEqual(v["verdict"], "fail")
                    self.assertTrue(any(role in r for r in v["reasons"]), v["reasons"])
            ok = green_manifest()
            ok["tasks"]["T1"]["results"][role] = result("pass")
            self.assertEqual(self.ev(manifest=ok)["verdict"], "pass")
            old = green_manifest()
            old["tasks"]["T1"]["results"][role] = result("findings", head=OLD)
            self.assertEqual(self.ev(manifest=old)["verdict"], "pass")

    def test_needs_fix_is_explained_only_by_the_accepted_limitation(self):  # r6 HIGH
        def manifest(decisions, **task):
            m = green_manifest(**dict({"status": "needs_fix", "decisions": decisions}, **task))
            m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = result("findings", at="2026-10-01T10:05:00Z")
            return m

        def decision(**kw):
            return dict({"source": "cross_provider_reviewer", "decision": "accept_limitation",
                         "note": "n", "recorded_at": "2026-10-01T10:10:00Z"}, **kw)
        self.assertEqual(self.ev(manifest=manifest([decision()]))["verdict"], "pass")
        self.assertEqual(self.ev(manifest=manifest([decision()], fix_cycles=2,
                                                   fix_sources={"cross_provider_reviewer": 2}))["verdict"], "pass")
        # the last decision is not the accepted limitation of the reviewer
        later = decision(source="tester", decision="invariant", recorded_at="2026-10-01T10:20:00Z")
        self.assertEqual(self.ev(manifest=manifest([decision(), later]))["verdict"], "fail")
        self.assertEqual(self.ev(manifest=manifest([]))["verdict"], "fail")
        # a new failed fix-loop after the decision: another source, then the same one
        self.assertEqual(self.ev(manifest=manifest([decision()], fix_cycles=3,
                                                   fix_sources={"cross_provider_reviewer": 2, "tester": 1}))["verdict"],
                         "fail")
        self.assertEqual(self.ev(manifest=manifest([decision()], status="needs_decision"))["verdict"], "fail")
        # reviewer result is pass: needs_fix is not explained by anything
        m = manifest([decision()])
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = result("pass")
        self.assertEqual(self.ev(manifest=m)["verdict"], "fail")

    def test_results_on_another_tree_or_a_dirty_copy_fail(self):  # acceptance 1a
        dirty_result = green_manifest()
        dirty_result["tasks"]["T1"]["results"]["tester"] = result(fp="0" * 64)
        self.assertEqual(self.ev(manifest=dirty_result)["verdict"], "fail")
        dirty_review = green_manifest()
        dirty_review["tasks"]["T1"]["results"]["cross_provider_reviewer"] = result(fp="0" * 64)
        self.assertEqual(self.ev(manifest=dirty_review)["verdict"], "fail")
        self.assertEqual(self.ev(work={"clean": False, "head": HEAD, "fingerprint": FP})["verdict"], "fail")
        self.assertEqual(self.ev(work={"clean": True, "head": OLD, "fingerprint": FP})["verdict"], "fail")
        self.assertEqual(self.ev(work={"clean": True, "head": HEAD, "fingerprint": None})["verdict"], "fail")
        self.assertEqual(self.ev(work={})["verdict"], "fail")

    def test_reviewer_findings_need_a_decision_made_on_this_result(self):  # acceptance 1b
        def manifest(decisions, at="2026-10-01T10:05:00Z"):
            m = green_manifest(decisions=decisions)
            m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = result("findings", at=at)
            return m

        def decision(**kw):
            return dict({"source": "cross_provider_reviewer", "decision": "accept_limitation",
                         "note": "n", "recorded_at": "2026-10-01T10:10:00Z"}, **kw)
        cases = [
            ("no decision", [], "fail"),
            ("older than the result", [decision(recorded_at="2026-10-01T10:00:00Z")], "fail"),
            ("another source", [decision(source="tester")], "fail"),
            ("invariant", [decision(decision="invariant")], "fail"),
            ("cut_surface", [decision(decision="cut_surface")], "fail"),
            ("accept_limitation on this result", [decision()], "pass"),
            ("same instant", [decision(recorded_at="2026-10-01T10:05:00Z")], "pass"),
        ]
        for name, decisions, want in cases:
            with self.subTest(case=name):
                self.assertEqual(self.ev(manifest=manifest(decisions))["verdict"], want)
        other = manifest([decision()])
        other["tasks"]["T1"]["results"]["cross_provider_reviewer"]["status"] = "unavailable"
        self.assertEqual(self.ev(manifest=other)["verdict"], "fail")

    def test_deferred_low_findings_count_only_for_the_exact_result(self):  # #36 defect 3
        state = wab.gate.state

        def deferral(res, source="cross_provider_reviewer", head=HEAD, digest=None):
            return {"source": source, "note": "low -> next wave", "head": head,
                    "result_sha256": digest or state.result_digest(res),
                    "result_recorded_at": res.get("recorded_at"), "recorded_at": res.get("recorded_at")}

        def manifest(deferrals_of, role="cross_provider_reviewer"):
            m = green_manifest()
            res = result("findings")
            m["tasks"]["T1"]["results"][role] = res
            m["tasks"]["T1"]["deferrals"] = deferrals_of(res)
            return m
        for role in ("cross_provider_reviewer", "github_codex_review", "coderabbit"):
            cases = [
                ("no deferral", lambda r: [], "fail"),
                ("deferral of this exact result", lambda r, role=role: [deferral(r, role)], "pass"),
                # a rerun of the same reviewer on the same head in the same second: another result
                ("deferral of another result, same head and second",
                 lambda r, role=role: [deferral(dict(r, session_id="rerun"), role)], "fail"),
                ("deferral on another head", lambda r, role=role: [deferral(r, role, head=OLD)], "fail"),
                ("deferral of another role", lambda r: [deferral(r, "tester")], "fail"),
            ]
            for name, deferrals_of, want in cases:
                with self.subTest(role=role, case=name):
                    v = self.ev(manifest=manifest(deferrals_of, role))
                    self.assertEqual(v["verdict"], want, v["reasons"])
        # deferral never turns coder/tester findings into pass
        for role in ("coder", "tester"):
            with self.subTest(role=role):
                m = manifest(lambda r, role=role: [deferral(r, role)], role)
                self.assertEqual(self.ev(manifest=m)["verdict"], "fail")
        # a deferral does not excuse a non-findings status
        m = manifest(lambda r: [deferral(r)])
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"]["status"] = "error"
        self.assertEqual(self.ev(manifest=m)["verdict"], "fail")

    def test_accepted_findings_count_only_for_the_exact_result_and_are_printed(self):  # #54
        state = wab.gate.state

        def acceptance(res, source="cross_provider_reviewer", severity="medium", head=HEAD, digest=None):
            return {"source": source, "severity": severity, "note": f"{severity} limitation",
                    "head": head, "result_sha256": digest or state.result_digest(res),
                    "result_recorded_at": res.get("recorded_at"), "recorded_at": res.get("recorded_at")}

        def manifest(acceptances_of, role="cross_provider_reviewer"):
            m = green_manifest()
            res = result("findings")
            m["tasks"]["T1"]["results"][role] = res
            m["tasks"]["T1"]["acceptances"] = acceptances_of(res)
            return m
        for role in ("cross_provider_reviewer", "github_codex_review", "coderabbit"):
            for severity in ("low", "medium", "high"):
                with self.subTest(role=role, severity=severity):
                    v = self.ev(manifest=manifest(lambda r: [acceptance(r, role, severity)], role))
                    self.assertEqual(v["verdict"], "pass", v["reasons"])
                    printed = [r for r in v["reasons"] if "accepted" in r]
                    if severity == "low":
                        self.assertEqual(printed, [])
                    else:
                        self.assertEqual(printed, [f"accepted {severity} {role}: {severity} limitation"])
                        self.assertEqual(v["accepted"], printed)
            cases = [
                ("no acceptance", lambda r: []),
                ("acceptance of another result, same head and second",
                 lambda r: [acceptance(dict(r, session_id="rerun"), role)]),
                ("acceptance on another head", lambda r: [acceptance(r, role, head=OLD)]),
                ("acceptance of another role", lambda r: [acceptance(r, "tester")]),
            ]
            for name, acceptances_of in cases:
                with self.subTest(role=role, case=name):
                    v = self.ev(manifest=manifest(acceptances_of, role))
                    self.assertEqual(v["verdict"], "fail", v["reasons"])
        # an acceptance never turns coder/tester findings into pass, nor a non-findings status
        for role in ("coder", "tester"):
            with self.subTest(role=role):
                m = manifest(lambda r, role=role: [acceptance(r, role)], role)
                self.assertEqual(self.ev(manifest=m)["verdict"], "fail")
        m = manifest(lambda r: [acceptance(r)])
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"]["status"] = "error"
        self.assertEqual(self.ev(manifest=m)["verdict"], "fail")

    def test_manifest_forms_of_0_11_0_keep_their_verdict(self):  # #54 compatibility
        state = wab.gate.state
        res = result("findings")
        m = green_manifest(decisions=[{"source": "cross_provider_reviewer", "decision": "accept_limitation",
                                       "note": "n", "recorded_at": "2026-10-01T10:06:00Z"}])
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = res
        self.assertEqual(self.ev(manifest=m)["verdict"], "pass")
        m = green_manifest(deferrals=[{"source": "cross_provider_reviewer", "note": "low", "head": HEAD,
                                       "result_sha256": state.result_digest(res),
                                       "result_recorded_at": res["recorded_at"],
                                       "recorded_at": res["recorded_at"]}])
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = res
        self.assertEqual(self.ev(manifest=m)["verdict"], "pass")
        # everything state.py writes is known to the gate
        full = green_manifest(fix_cycles=0, session_roles={}, fix_sources={}, decision_required_for=None,
                              deferrals=[], acceptances=[], blocked_reason="x")
        full.update({"version": 1, "run_id": "r", "repo": "/r", "base": OLD, "tree_fingerprint": FP,
                     "created_at": "t", "updated_at": "t", "plan": {}, "wave": {}, "position": None,
                     "run": {"index": 1, "max": 2}})
        v = self.ev(manifest=full)
        self.assertEqual((v["verdict"], v["reasons"]), ("pass", []))

    def test_an_unknown_record_type_is_a_closed_refusal(self):  # #54
        m = green_manifest(vetoes=[{"by": "owner"}])
        v = self.ev(manifest=m)
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("manifest: unknown record type 'vetoes' in task T1: this gate cannot judge it",
                      v["reasons"])
        m = green_manifest()
        m["waivers"] = []
        v = self.ev(manifest=m)
        self.assertEqual(v["verdict"], "fail")
        self.assertTrue(any("unknown record type 'waivers'" in r for r in v["reasons"]), v["reasons"])

    def test_a_malformed_manifest_is_a_closed_refusal_not_an_exception(self):  # W1 fix1
        def fails(m, needle):
            v = self.ev(manifest=m)
            self.assertEqual(v["verdict"], "fail", v["reasons"])
            self.assertTrue(any(needle in r for r in v["reasons"]), v["reasons"])
        for bad in (["T1"], "T1", 7):
            with self.subTest(tasks=bad):
                m = green_manifest()
                m["tasks"] = bad
                fails(m, "нет задач")
        for field, bad in (("results", []), ("decisions", {"a": 1}), ("decisions", 5), ("deferrals", "x"),
                           ("acceptances", 3), ("acceptances", {"source": "x"}), ("fix_sources", []),
                           ("fix_cycles", "2"), ("session_roles", [])):
            with self.subTest(field=field, bad=bad):
                m = green_manifest()
                m["tasks"]["T1"][field] = bad
                fails(m, f"поле {field}")
        m = green_manifest()
        m["tasks"]["T1"] = "broken"
        fails(m, "задача T1")
        m = green_manifest()
        m["run"] = "x"
        fails(m, "поле run")
        # non-dict elements inside a list do not crash the acceptance/deferral lookup
        state = wab.gate.state
        res = result("findings")
        m = green_manifest()
        m["tasks"]["T1"]["results"]["cross_provider_reviewer"] = res
        m["tasks"]["T1"]["acceptances"] = ["x", 3, None]
        m["tasks"]["T1"]["deferrals"] = [[], "y"]
        self.assertEqual(self.ev(manifest=m)["verdict"], "fail")
        self.assertEqual(wab.gate.accepted_notes({"tasks": ["T1"]}, HEAD), [])
        self.assertEqual(wab.gate.accepted_notes({"tasks": {"T1": {"results": {"coder": res},
                                                                   "acceptances": 5}}}, HEAD), [])
        self.assertIsNone(state.accepted_record({"acceptances": 5}, "coderabbit", res))

    def test_pr_head_other_than_the_gated_one_waits(self):
        facts = green_facts()
        facts["pr"]["head"] = OLD
        self.assertEqual(self.ev(facts)["verdict"], "wait")

    def test_closed_or_merged_pr_fails_and_a_collection_error_only_waits(self):
        facts = green_facts()
        facts["pr"]["state"] = "closed"
        self.assertEqual(self.ev(facts)["verdict"], "fail")
        facts["pr"]["merged"] = True
        self.assertEqual(self.ev(facts)["verdict"], "fail")
        v = self.ev({"error": "gh api: boom"})
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("boom", v["reasons"][0])

    def test_a_redirected_pr_fails(self):  # r8 HIGH
        facts = green_facts()
        facts["pr"]["base"] = "release"
        v = self.ev(facts)
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("release", v["reasons"][0])
        self.assertIn("main", v["reasons"][0])

    def test_a_p1_badge_in_the_body_of_a_codex_review_on_head_fails(self):  # CodeRabbit on cf32fbd
        def review(body, commit=HEAD, user=BOT):
            return {"user": user, "commit_id": commit, "state": "COMMENTED", "body": body, "html_url": "r"}
        on_head = green_facts(reviews=[review("**![P1 Badge](x)** in the body")])
        self.assertEqual(self.ev(on_head)["verdict"], "fail")
        self.assertEqual(gate.codex_on_head(on_head, HEAD)["findings"], 1)
        self.assertIn("1 замечаний, P0/P1: 1", gate.alarm_text(5, HEAD, on_head))
        for facts in (green_facts(reviews=[review("**![P1 Badge](x)** old", commit=OLD), review("clean")]),
                      green_facts(reviews=[review("![P2 Badge](x) minor")]),
                      green_facts(reviews=[review("clean"), review("**![P1 Badge](x)**", user=HUMAN)])):
            with self.subTest(facts=facts["reviews"]):
                self.assertNotEqual(self.ev(facts)["verdict"], "fail")

    def test_the_review_body_is_critical(self):
        later = green_facts(reviews=[dict(green_facts()["reviews"][0], body="**![P1 Badge](x)** added")])
        self.assertNotEqual(gate.critical(green_facts()), gate.critical(later))

    def test_an_unknown_base_waits_never_passes(self):
        facts = green_facts()
        del facts["pr"]["base"]
        self.assertEqual(self.ev(facts)["verdict"], "wait")
        self.assertEqual(gate.evaluate(green_facts(), HEAD, green_manifest(), WORK, None)["verdict"], "wait")

    def test_the_base_is_critical(self):
        other = green_facts()
        other["pr"]["base"] = "release"
        self.assertNotEqual(gate.critical(green_facts()), gate.critical(other))

    def test_unresolved_threads_and_draft_are_reported_on_a_pass(self):
        facts = green_facts(threads=[{"id": "T_1", "isResolved": False}, {"id": "T_2", "isResolved": True}])
        facts["pr"]["draft"] = True
        v = self.ev(facts)
        self.assertEqual((v["verdict"], v["unresolved"], v["draft"]), ("pass", ["T_1"], True))


class FakeGitHub:
    """`api` and `graphql` over a table of responses; a list route is cut into pages of 100."""

    def __init__(self, routes, threads=None):
        self.routes, self.calls = routes, []
        self.threads = threads if threads is not None else {"nodes": [], "pageInfo": {"hasNextPage": False}}

    def api(self, path):
        self.calls.append(path)
        base, _, query = path.partition("?")
        if base not in self.routes:
            raise gate.CollectError(f"{path}: HTTP 404")
        value = self.routes[base]
        if "per_page=100" not in query:
            return value
        page = int(re.search(r"(?:^|&)page=(\d+)", query).group(1))
        key, items = (None, value) if isinstance(value, list) else next(iter(value.items()))
        part = items[(page - 1) * 100:page * 100]
        return part if key is None else {key: part, "total_count": len(items)}

    def graphql(self, query, variables):
        self.calls.append(("graphql", variables))
        return {"data": {"repository": {"pullRequest": {"reviewThreads": self.threads}}}}


def routes_for(repo="o/r", number=5, **over):
    r = {
        f"repos/{repo}/pulls/{number}": {"state": "open", "merged": False, "draft": False, "head": {"sha": HEAD},
                                       "base": {"ref": "main"}},
        f"repos/{repo}/commits/{HEAD}/check-runs": {"check_runs": [
            {"name": "ci", "status": "completed", "conclusion": "success"}]},
        f"repos/{repo}/pulls/{number}/reviews": [{"user": BOT, "commit_id": HEAD, "state": "COMMENTED"}],
        f"repos/{repo}/pulls/{number}/comments": [],
        f"repos/{repo}/issues/{number}/comments": [],
    }
    r.update(over)
    return r


# ---------------------------------------------------------------- 1.0.2: coderabbit unavailable does not block
def _frozen_gate_100():
    """The 1.0.0 gate (tests/fixtures/gate-1.0.0), loaded under private names with ITS OWN state.py and
    pr_review.py: it resolves siblings relative to its own file, so the current modules are untouched."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "gate-1.0.0" / "scripts" / "waves" / "gate.py"
    spec = importlib.util.spec_from_file_location("frozen_gate_1_0_0", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _with_role(role, res, **task):
    m = green_manifest(**task)
    m["tasks"]["T1"]["results"][role] = res
    return m


class GateCoderabbitUnavailable(unittest.TestCase):  # #72
    def problems(self, manifest, module=None):
        return (module or gate).manifest_problems(manifest, HEAD, FP)

    def covered(self, kind, role="coderabbit"):
        res = result("findings")
        record = {"source": role, "note": "low", "head": HEAD, "result_sha256": gate.state.result_digest(res),
                  "result_recorded_at": res.get("recorded_at"), "recorded_at": res.get("recorded_at")}
        if kind == "acceptances":
            record["severity"] = "medium"
        return _with_role(role, res, **{kind: [record]})

    def test_unavailable_on_head_is_not_a_problem(self):
        self.assertEqual(self.problems(_with_role("coderabbit", result("unavailable"))), [])

    def test_unavailable_on_head_gives_a_pass_verdict(self):
        v = gate.evaluate(green_facts(), HEAD, _with_role("coderabbit", result("unavailable")), WORK, "main")
        self.assertEqual((v["verdict"], v["reasons"]), ("pass", []))

    def test_other_non_pass_statuses_still_block(self):
        for status in ("error", "incomplete", "pending", "findings"):
            with self.subTest(status=status):
                self.assertTrue(self.problems(_with_role("coderabbit", result(status))))

    def test_findings_with_defer_or_accept_pass(self):
        for kind in ("deferrals", "acceptances"):
            with self.subTest(kind=kind):
                self.assertEqual(self.problems(self.covered(kind)), [])

    def test_github_codex_review_unavailable_still_blocks(self):
        self.assertTrue(self.problems(_with_role("github_codex_review", result("unavailable"))))

    def test_missing_tester_and_unresolved_reviewer_findings_still_block(self):
        m = _with_role("coderabbit", result("unavailable"))
        del m["tasks"]["T1"]["results"]["tester"]
        self.assertTrue(self.problems(m))
        self.assertTrue(self.problems(_with_role("cross_provider_reviewer", result("findings"))))

    def test_unavailable_of_another_head_is_not_this_heads_business(self):
        self.assertEqual(self.problems(_with_role("coderabbit", result("unavailable", head=OLD))), [])

    def test_verdicts_match_the_1_0_0_gate_except_coderabbit_unavailable(self):
        old = _frozen_gate_100()
        self.assertIsNot(old.state, gate.state)
        cases = [
            ("all pass", green_manifest(), False),
            ("coderabbit pass", _with_role("coderabbit", result("pass")), False),
            ("coderabbit findings without a decision", _with_role("coderabbit", result("findings")), True),
            ("coderabbit findings + defer", self.covered("deferrals"), False),
            ("coderabbit findings + accept", self.covered("acceptances"), False),
            ("coderabbit error", _with_role("coderabbit", result("error")), True),
            ("coderabbit incomplete", _with_role("coderabbit", result("incomplete")), True),
            ("github_codex_review unavailable", _with_role("github_codex_review", result("unavailable")), True),
            ("github_codex_review findings + defer", self.covered("deferrals", "github_codex_review"), False),
            ("no tester", self._without("tester"), True),
            ("reviewer findings without a decision", _with_role("cross_provider_reviewer", result("findings")), True),
        ]
        for name, manifest, blocked in cases:
            with self.subTest(case=name):
                self.assertEqual(bool(self.problems(manifest, old)), blocked, self.problems(manifest, old))
                self.assertEqual(bool(self.problems(manifest)), blocked, self.problems(manifest))

    def _without(self, role):
        m = green_manifest()
        del m["tasks"]["T1"]["results"][role]
        return m

    def test_the_only_intended_difference_is_coderabbit_unavailable(self):
        old = _frozen_gate_100()
        m = _with_role("coderabbit", result("unavailable"))
        self.assertTrue(self.problems(m, old))  # the 1.0.0 gate refuses it (closed failure is fine)
        self.assertEqual(self.problems(m), [])


class GateCollect(unittest.TestCase):
    def collect(self, fake, head=HEAD):
        return gate.collect("o/r", 5, head, fake.api, fake.graphql)

    def test_collects_every_fact_and_evaluates_to_pass(self):
        fake = FakeGitHub(routes_for())
        facts = self.collect(fake)
        self.assertEqual(gate.evaluate(facts, HEAD, green_manifest(), WORK, "main")["verdict"], "pass")

    def test_check_runs_are_read_page_by_page_and_a_pending_run_on_page_two_waits(self):
        runs = [{"name": f"c{i}", "status": "completed", "conclusion": "success"} for i in range(100)]
        runs.append({"name": "late", "status": "queued", "conclusion": None})
        fake = FakeGitHub(routes_for(**{f"repos/o/r/commits/{HEAD}/check-runs": {"check_runs": runs}}))
        facts = self.collect(fake)
        self.assertEqual(len(facts["check_runs"]), 101)
        self.assertEqual(sum("check-runs" in c for c in fake.calls if isinstance(c, str)), 2)
        v = gate.evaluate(facts, HEAD, green_manifest(), WORK, "main")
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("late", v["reasons"][0])

    def test_too_many_pages_is_a_collection_error(self):
        runs = [{"name": "c", "status": "completed", "conclusion": "success"}] * (100 * gate.MAX_PAGES + 1)
        fake = FakeGitHub(routes_for(**{f"repos/o/r/commits/{HEAD}/check-runs": {"check_runs": runs}}))
        with self.assertRaises(gate.CollectError):
            self.collect(fake)
        self.assertIn("error", gate.gather("o/r", 5, HEAD, fake.api, fake.graphql))

    def test_more_than_100_review_threads_is_a_collection_error(self):
        fake = FakeGitHub(routes_for(), threads={"nodes": [], "pageInfo": {"hasNextPage": True}})
        with self.assertRaises(gate.CollectError):
            self.collect(fake)

    def test_a_failing_api_call_is_an_error_fact_never_a_pass(self):
        routes = routes_for()
        del routes["repos/o/r/pulls/5/reviews"]
        fake = FakeGitHub(routes)
        facts = gate.gather("o/r", 5, HEAD, fake.api, fake.graphql)
        self.assertIn("error", facts)
        self.assertEqual(gate.evaluate(facts, HEAD, green_manifest(), WORK, "main")["verdict"], "wait")

    def test_a_pr_with_another_head_returns_early(self):
        fake = FakeGitHub(routes_for(**{"repos/o/r/pulls/5": {"state": "open", "head": {"sha": OLD}, "base": {"ref": "main"}}}))
        facts = self.collect(fake)
        self.assertEqual(list(facts), ["pr"])
        self.assertEqual(gate.evaluate(facts, HEAD, green_manifest(), WORK, "main")["verdict"], "wait")

    def test_the_base_of_the_pr_is_collected_even_on_an_early_return(self):
        facts = self.collect(FakeGitHub(routes_for()))
        self.assertEqual(facts["pr"]["base"], "main")
        early = self.collect(FakeGitHub(routes_for(**{"repos/o/r/pulls/5": {
            "state": "open", "head": {"sha": OLD}, "base": {"ref": "release"}}})))
        self.assertEqual(early["pr"]["base"], "release")

    def test_summary_commits_are_resolved_and_reactions_are_not_collected(self):  # D1
        short = HEAD[:7]
        comments = [dict(summary(), id=3),
                    {"id": 4, "body": f"@codex review\n<!-- superarmanda:codex-review head={HEAD} -->"}]
        fake = FakeGitHub(routes_for(**{"repos/o/r/issues/5/comments": comments,
                                        f"repos/o/r/commits/{short}": {"sha": HEAD},
                                        "repos/o/r/pulls/5/reviews": []}))
        facts = self.collect(fake)
        self.assertEqual(facts["resolved"], {short: HEAD})
        self.assertNotIn("triggers", facts)
        self.assertFalse(any("reactions" in c for c in fake.calls if isinstance(c, str)))
        self.assertTrue(gate.codex_on_head(facts, HEAD)["done"])
    def test_importing_gate_leaves_the_import_path_alone(self):
        before = list(sys.path)
        import importlib
        importlib.reload(gate)
        self.assertEqual(sys.path, before)
        self.assertTrue(callable(gate.fingerprint))


class GateCommands(unittest.TestCase):
    def test_merge_argv_and_command(self):
        argv = gate.merge_argv("o/r", 12, HEAD)
        self.assertEqual(argv, ["gh", "pr", "merge", "12", "--repo", "o/r", "--squash", "--match-head-commit", HEAD])
        self.assertEqual(gate.merge_command("o/r", 12, HEAD), " ".join(argv))

    def test_unsafe_values_are_refused_before_a_command_is_built(self):
        for repo, number, sha in (("o/r'; touch /tmp/x; '", 1, HEAD), ("o r", 1, HEAD), ("o/r", 0, HEAD),
                                  ("o/r", "1", HEAD), ("o/r", 1, "abc"), ("o/r", 1, HEAD + "; x")):
            with self.subTest(repo=repo, number=number, sha=sha):
                with self.assertRaises(ValueError):
                    gate.merge_argv(repo, number, sha)
    def test_owner_script_only_execs_owner_merge_bound_to_run_and_sha(self):  # D3 / G2
        script = gate.owner_script("/opt/my tools/wab.py", "/runs/it's/chain.json", "W1", "run-1", HEAD)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", script)
        last = script.rstrip().splitlines()[-1]
        self.assertEqual(shlex.split(last), ["exec", "python3", "/opt/my tools/wab.py", "owner-merge",
                                             "/runs/it's/chain.json", "W1", "run-1", HEAD])
        self.assertNotIn("gh ", script)  # no merge of its own: everything is decided by the fresh gate
        done = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_owner_script_names_do_not_collide_across_runs(self):  # Codex P2 on a27908b
        base = {"chain_file": "/r/chain.json", "chain": "a-b", "run_id": "c"}
        names = {wab.owner_script_name(dict(base, **over), "W1", HEAD) for over in (
            {}, {"chain": "a", "run_id": "b-c"}, {"chain_file": "/other/chain.json"}, {"run_id": "d"})}
        self.assertEqual(len(names), 4)  # hyphen-joined parts used to give a-b-c for the first two
        name = wab.owner_script_name(base, "W1", HEAD)
        self.assertEqual(name, wab.owner_script_name(dict(base), "W1", HEAD))  # stable for the same run
        self.assertNotEqual(name, wab.owner_script_name(base, "W2", HEAD))
        self.assertNotEqual(name, wab.owner_script_name(base, "W1", OLD))
        self.assertTrue(name.startswith(f"W1.{HEAD[:12]}.") and name.endswith(".merge"))
        self.assertNotIn("/", name)

    def test_owner_script_refuses_malformed_wave_run_or_sha(self):
        for wave, run_id, sha in (("x'; touch /tmp/x #", "r", HEAD), ("a b", "r", HEAD), ("", "r", HEAD),
                                  ("W1", "r; x", HEAD), ("W1", "", HEAD), ("W1", "r", "abc"),
                                  ("W1", "r", HEAD + "\n"), ("W1", "r", None)):
            with self.subTest(wave=wave, run_id=run_id, sha=sha):
                with self.assertRaises(ValueError):
                    gate.owner_script("/w/wab.py", "/c/chain.json", wave, run_id, sha)

    def test_alarm_text(self):
        facts = green_facts(
            check_runs=[{"name": "ci", "status": "completed", "conclusion": "success"},
                        {"name": "lint", "status": "completed", "conclusion": "failure"}],
            review_comments=[{"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) a"},
                             {"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P2 Badge](x) b"}],
            threads=[{"id": "T", "isResolved": False}])
        text = gate.alarm_text(5, HEAD, facts)
        for part in ("PR #5", HEAD[:12], "1 ok", "lint (failure)", "2 замечаний, P0/P1: 1", "тредов: 1"):
            self.assertIn(part, text)
        old = green_facts(threads=[{"id": "T", "isResolved": False, "body": "![P1 Badge](x) old"},
                                   {"id": "U", "isResolved": False, "body": "plain"}])
        self.assertIn("незакрытых тредов: 2 (из них с P0/P1: 1)", gate.alarm_text(5, HEAD, old))
        # the alarm runs before any gate: an open P1 thread written on HEAD is not "from earlier commits"
        on_head = green_facts(review_comments=[{"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD,
                                                "body": "![P1 Badge](x) now"}],
                              threads=[{"id": "T", "isResolved": False, "body": "![P1 Badge](x) now"}])
        self.assertNotIn("прошлых коммитов", gate.alarm_text(5, HEAD, on_head))
        self.assertIn("незакрытых тредов: 1 (из них с P0/P1: 1)", gate.alarm_text(5, HEAD, on_head))
        self.assertIn("чисто", gate.alarm_text(5, HEAD, green_facts()))

    def test_alarm_ready_needs_completed_checks_and_codex(self):
        self.assertTrue(gate.alarm_ready(green_facts(), HEAD))
        pending = green_facts(check_runs=[{"name": "x", "status": "queued"}])
        self.assertFalse(gate.alarm_ready(pending, HEAD))
        self.assertFalse(gate.alarm_ready(green_facts(check_runs=[]), HEAD))
        self.assertFalse(gate.alarm_ready(green_facts(reviews=[]), HEAD))
        self.assertFalse(gate.alarm_ready({"error": "x"}, HEAD))
        self.assertFalse(gate.alarm_ready({"pr": {"head": OLD}}, HEAD))


# ---------------------------------------------------------------- W5: merge gate and alarm in wab.py
def _cp(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(args, rc, out, err)


class GateBase(Base):
    """A DONE/RUNNING wave of merge_gate "auto" whose GitHub facts, manifest and working copy are
    fixtures; `gh` answers `pr view` (self.view), `pr merge` and `pr ready` (self.merge_rc/_err)."""

    def setUp(self):
        super().setUp()
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts()
        self.manifest = green_manifest()
        self.work = dict(WORK)
        self.view = {"state": "OPEN", "mergeCommit": None, "headRefOid": HEAD, "baseRefName": "main"}
        self.merge_rc, self.merge_err, self.ready_rc = 0, "", 0
        self.find_calls = 0

        def find_pr(cfg, cwd):
            self.find_calls += 1
            if isinstance(self.pr, Exception):
                raise self.pr
            return self.pr

        def patch(name, **kw):
            p = mock.patch.object(wab, name, **kw)
            started = p.start()
            self.addCleanup(p.stop)
            return started
        patch("find_pr", side_effect=find_pr)
        patch("base_branch_of", return_value="main")
        patch("gate_facts", side_effect=lambda cfg, pr: self.facts)
        patch("read_manifest", side_effect=lambda cfg, wave: self.manifest)
        patch("workdir_state", side_effect=lambda cwd: self.work)
        patch("GATE_POLL_SECONDS", new=0)
        patch("ALARM_POLL_SECONDS", new=0)
        self.launch = patch("launch", return_value=True)
        patch("drop_stale_btab")
        self.gh_handler = self.handle

    def handle(self, args):
        if args[:3] == ("gh", "pr", "view"):
            return _cp(args, out=json.dumps(self.view))
        if args[:3] == ("gh", "pr", "merge"):
            return _cp(args, self.merge_rc, "", self.merge_err)
        if args[:3] == ("gh", "pr", "ready"):
            return _cp(args, self.ready_rc, "", "cannot mark ready" if self.ready_rc else "")
        return _cp(args, 1, "", "unexpected gh call")

    def merges(self):
        return [c for c in self.gh_calls if c[:3] == ("gh", "pr", "merge")]

    def start(self, status="DONE", phase="running", **chain):
        chain.setdefault("merge_gate", "auto")
        cfg, path = self.chain(**chain)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase=phase)},
                             "identity": wab._pinned_identity(cfg)})  # what the first launch saves
        if status:
            self.set_status(cfg, "W1", status)
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("PR #7\n", encoding="utf-8")
        self.cfg, self.path = cfg, path
        return cfg

    def tick(self, cfg=None):
        cfg = cfg or self.cfg
        return wab.tick(cfg, wab.load_state(cfg))

    def rec(self, wave="W1"):
        return self.get_state(self.cfg)["waves"][wave]

    def status(self, wave="W1"):
        return (wab.wave_dir(self.cfg, wave) / "status").read_text(encoding="utf-8").strip()

    def log(self):
        return (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8")

    def merge_it(self):
        """One tick that passes the gate and calls merge."""
        self.assertTrue(self.tick())
        self.assertEqual(len(self.merges()), 1)


class MergeGate(GateBase):
    def test_wait_never_merges_and_is_logged_once_per_reason(self):  # acceptance 1, 2
        pending = [{"name": "ci", "status": "completed", "conclusion": "success"},
                   {"name": "e2e", "status": "in_progress", "conclusion": None}]
        cfg = self.start()
        self.facts = green_facts(check_runs=pending)
        for _ in range(3):
            self.assertTrue(self.tick())
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.log().count("merge gate waits"), 1)
        self.assertEqual(self.rec()["phase"], "gate")
        self.assertNotIn("pending_exit", self.rec())  # the window stays open
        self.facts = green_facts(reviews=[])  # another reason
        self.tick()
        self.assertEqual(self.log().count("merge gate waits"), 2)
        self.assertEqual(self.tg, [])

    def test_done_taken_back_while_the_gate_collects_is_not_merged(self):  # Codex P1 on e6e70c6
        red = green_facts(check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}])
        for draft, back, facts in ((False, "RUNNING", None), (True, "RUNNING", None), (False, "", None),
                                   (False, "RUNNING", red), (False, "", red)):  # Codex P2: the fail path too
            with self.subTest(draft=draft, back=back, fail=facts is not None):
                self.gh_calls.clear()
                cfg = self.start()
                self.pr = dict(self.pr, isDraft=draft)
                self.facts = facts or green_facts()
                self.facts["pr"]["draft"] = draft

                def collect(cfg_, pr, back=back):  # the wave rewrites its status mid-collection
                    (wab.wave_dir(cfg, "W1") / "status").write_text(back + ("\n" if back else ""), encoding="utf-8")
                    return self.facts
                with mock.patch.object(wab, "gate_facts", side_effect=collect):
                    self.assertTrue(self.tick())
                self.assertEqual(self.merges(), [])
                self.assertEqual([c for c in self.gh_calls if c[:3] == ("gh", "pr", "ready")], [])
                self.assertNotIn(self.rec()["phase"], ("merging", "done"))
                self.assertEqual(self.rec()["phase"], "running" if back else "gate")
                self.assertNotIn("gate_fail_msg", self.rec())  # no stale failure into a wave at work
                self.assertEqual(self.status(), back)  # its new status is not overwritten with BLOCKED

    def test_a_collection_error_or_another_head_waits(self):
        cfg = self.start()
        self.pr = gate.CollectError("gh: no network")
        self.tick()
        self.pr = {"number": 7, "headRefOid": OLD, "isDraft": False, "state": "OPEN"}  # PR moved on
        self.tick()
        self.assertEqual(self.merges(), [])
        self.assertIn("no network", self.log())
        self.assertEqual(self.rec()["phase"], "gate")

    def test_stale_codex_review_is_not_merged(self):  # acceptance 1
        self.start()
        self.facts = green_facts(reviews=[{"user": BOT, "commit_id": OLD, "state": "COMMENTED"}])
        self.tick()
        self.assertEqual(self.merges(), [])

    def test_gate_is_asked_at_most_once_per_interval(self):
        self.start()
        self.facts = green_facts(reviews=[])
        with mock.patch.object(wab, "GATE_POLL_SECONDS", 60):
            for _ in range(4):
                self.tick()
        self.assertEqual(self.find_calls, 1)

    def test_pass_merges_once_then_waits_for_merged(self):  # acceptance 5
        self.start()
        self.merge_it()
        self.assertEqual(self.merges()[0], tuple(gate.merge_argv("o/r", 7, HEAD)))
        rec = self.rec()
        self.assertEqual((rec["phase"], rec["gate_sha"], rec["gate_pr"]), ("merging", HEAD, 7))
        for _ in range(3):  # still OPEN: nothing is merged again, nothing is launched
            self.assertTrue(self.tick())
        self.assertEqual(len(self.merges()), 1)
        self.launch.assert_not_called()
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        self.assertTrue(self.tick())
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args[0][1], "W2")
        self.assertTrue(self.launch.call_args[1]["by_dispatcher"])
        rec = self.rec()
        self.assertEqual(rec["phase"], "done")
        self.assertTrue(rec["pending_exit"])
        self.assertEqual(len([t for t in self.tg if "волна W1 завершена" in t]), 1)
        self.assertEqual(len(self.merges()), 1)

    def test_a_restarted_watch_in_merging_does_not_merge_again(self):
        self.start()
        self.merge_it()
        for _ in range(2):
            wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(self.rec()["phase"], "merging")
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(self.rec()["phase"], "done")
        self.assertEqual(len(self.merges()), 1)

    def test_a_crash_after_the_merge_mark_never_merges_twice(self):
        cfg = self.start()
        real = wab.sh
        state = {"boom": True}

        def die_in_merge(*args, **kw):
            if args[:3] == ("gh", "pr", "merge") and state["boom"]:
                state["boom"] = False
                raise KeyboardInterrupt
            return real(*args, **kw)
        with mock.patch.object(wab, "sh", side_effect=die_in_merge):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.assertEqual(self.rec()["merge_called"], HEAD)  # the mark was saved BEFORE the call
        for _ in range(2):
            wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(self.merges(), [])

    def test_an_interrupted_merge_is_handed_to_the_owner_once(self):
        cfg = self.start()
        real = wab.sh

        def die_in_merge(*args, **kw):
            if args[:3] == ("gh", "pr", "merge"):
                raise KeyboardInterrupt
            return real(*args, **kw)
        with mock.patch.object(wab, "sh", side_effect=die_in_merge):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.assertEqual(self.rec()["merge_called"], HEAD)
        self.assertNotIn("merge_rc", self.rec())
        shown = "~/.cache/wab/" + wab.owner_script_name(self.cfg, "W1", HEAD)
        with mock.patch.object(wab, "GATE_POLL_SECONDS", 0):
            for _ in range(3):
                self.tick()
        self.assertEqual(self.merges(), [])  # never a second automatic merge
        self.assertEqual(self.status(), f"BLOCKED: merge gate passed; merge result unknown; owner runs {shown}")
        said = [t for t in self.tg if "неизвестен (диспетчер" in t]
        self.assertEqual(len(said), 1)
        self.assertIn(f"Выполни: {shown}", said[0])
        self.assertTrue((self.home / ".cache" / "wab" / wab.owner_script_name(self.cfg, "W1", HEAD)).is_file())
        self.assertEqual(self.log().count("merge result unknown"), 1)
        self.assertEqual([q["text"] for q in self.rec()["questions"]],
                         [f"BLOCKED: merge gate passed; merge result unknown; owner runs {shown}"])
        # the owner merges: the wave completes as usual
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        with mock.patch.object(wab, "GATE_POLL_SECONDS", 0):
            self.tick()
        self.assertEqual(self.rec()["phase"], "done")
        self.assertEqual(self.merges(), [])

    def test_failed_check_blocks_with_the_reason_and_tells_the_window(self):  # acceptance 2
        self.start()
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}])
        self.assertTrue(self.tick())
        self.assertEqual(self.merges(), [])
        self.assertTrue(self.status().startswith("BLOCKED: merge gate: "))
        self.assertIn("ci (failure)", self.status())
        self.assertEqual(self.rec()["phase"], "running")
        self.tick()
        self.assertEqual(self.rec().get("questions", []), [])  # the wave fixes it itself: not the owner's
        told = [s for s in self.sent if s[1] == "wv-w1" and "Гейт мерджа не пройден" in s[2]]
        self.assertEqual(len(told), 1)
        self.assertIn("ci (failure)", told[0][2])
        self.tick()  # the usual BLOCKED notice: the reason, never a merge command
        blocked = [t for t in self.tg if "ждёт тебя" in t]
        self.assertEqual(len(blocked), 1)
        self.assertIn("ci (failure)", blocked[0])
        self.assertNotIn("gh pr merge", blocked[0])

    def failing_send(self, *, typed):
        calls = self.fail_calls = []

        def send(name, text, on_typed=None):
            calls.append(text)
            if typed and on_typed:
                on_typed()  # the text is in the window; Enter is what failed
            raise subprocess.CalledProcessError(1, ["tmux"], stderr="boom")
        return send

    def gate_fail_facts(self, name="ci"):
        return green_facts(check_runs=[{"name": name, "status": "completed", "conclusion": "failure"}])

    def told(self):
        return [s for s in self.sent if "Гейт мерджа не пройден" in s[2]]

    def test_a_failed_delivery_of_the_gate_failure_is_retried(self):  # M1, before the paste
        self.start()
        self.facts = self.gate_fail_facts()
        real = wab.send_text.side_effect
        wab.send_text.side_effect = self.failing_send(typed=False)
        self.assertTrue(self.tick())
        self.assertEqual(self.told(), [])
        self.assertFalse(self.rec()["gate_fail_msg"]["sent"])
        wab.send_text.side_effect = real
        self.tick()
        self.assertEqual(len(self.told()), 1)
        self.assertTrue(self.rec()["gate_fail_msg"]["sent"])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.told()), 1)  # delivered once, never again

    def test_a_failure_after_the_paste_presses_only_enter(self):  # M1, like the checkpoint request
        self.start()
        self.facts = self.gate_fail_facts()
        real = wab.send_text.side_effect
        wab.send_text.side_effect = self.failing_send(typed=True)
        self.tick()
        self.assertEqual(self.rec()["pending_enter"], "gate failure")
        wab.send_text.side_effect = real
        self.tick()
        self.assertEqual(self.told(), [])  # not typed twice
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertTrue(self.rec()["gate_fail_msg"]["sent"])

    def test_a_restarted_watch_delivers_the_pending_gate_failure(self):  # M1
        self.start()
        self.facts = self.gate_fail_facts()
        real = wab.send_text.side_effect
        wab.send_text.side_effect = self.failing_send(typed=False)
        self.tick()
        wab.send_text.side_effect = real
        wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(len(self.told()), 1)
        wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(len(self.told()), 1)

    def test_a_new_episode_replaces_the_pending_text_and_a_moved_on_wave_gets_nothing_stale(self):  # M1
        self.start()
        self.facts = self.gate_fail_facts("old-check")
        real = wab.send_text.side_effect
        wab.send_text.side_effect = self.failing_send(typed=False)
        self.tick()
        self.assertIn("old-check", self.rec()["gate_fail_msg"]["text"])
        wab.send_text.side_effect = real
        self.set_status(self.cfg, "W1", "DONE")  # the wave wrote DONE again before the retry
        self.facts = self.gate_fail_facts("new-check")
        self.tick()
        self.assertEqual(len(self.told()), 1)
        self.assertIn("new-check", self.told()[0][2])
        self.assertNotIn("old-check", self.told()[0][2])
        # a wave that has moved on (RUNNING) is not sent an outdated failure
        self.facts = self.gate_fail_facts("third")
        wab.send_text.side_effect = self.failing_send(typed=False)
        self.set_status(self.cfg, "W1", "DONE")
        self.tick()
        wab.send_text.side_effect = real
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.assertEqual(len(self.told()), 1)
        self.assertNotIn("gate_fail_msg", self.rec())

    def test_status_file_is_replaced_atomically(self):
        self.start()
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}])
        real = os.replace
        seen = []
        with mock.patch("os.replace", side_effect=lambda a, b: (seen.append(str(b)), real(a, b))[1]):
            self.tick()
        self.assertTrue(any(p.endswith("/W1/status") for p in seen), seen)
        self.assertEqual([p for p in os.listdir(wab.wave_dir(self.cfg, "W1")) if p.startswith(".status.")], [])

    def test_failures_that_must_block(self):  # acceptance 3, 4, 1a
        p1 = [{"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) bad", "html_url": "u"}]
        no_tester = green_manifest()
        del no_tester["tasks"]["T1"]["results"]["tester"]
        other_head = green_manifest()
        other_head["head"] = OLD
        dirty_result = green_manifest()
        dirty_result["tasks"]["T1"]["results"]["tester"] = result(fp="0" * 64)
        cases = {
            "P1 on head": (green_facts(review_comments=p1), green_manifest(), WORK),
            "no tester pass": (green_facts(), no_tester, WORK),
            "manifest head": (green_facts(), other_head, WORK),
            "no manifest": (green_facts(), None, WORK),
            "tester on another tree": (green_facts(), dirty_result, WORK),
            "dirty working copy": (green_facts(), green_manifest(), {**WORK, "clean": False}),
            "closed PR": (green_facts(pr={"state": "closed", "merged": False, "draft": False, "head": HEAD, "base": "main"}),
                          green_manifest(), WORK),
        }
        for name, (facts, manifest, work) in cases.items():
            with self.subTest(case=name):
                self.facts, self.manifest, self.work = facts, manifest, work
                self.start()
                self.tick()
                self.assertEqual(self.merges(), [], name)
                self.assertTrue(self.status().startswith("BLOCKED: merge gate: "), (name, self.status()))
                self.assertEqual(self.rec()["phase"], "running", name)

    def test_no_pr_for_the_branch_blocks(self):
        self.start()
        self.pr = None
        self.tick()
        self.assertIn("PR ветки волны не найден", self.status())
        self.assertEqual(self.merges(), [])

    def test_a_blocked_wave_that_writes_done_again_is_gated_again(self):
        self.start()
        self.facts = green_facts(reviews=[])
        self.facts["check_runs"] = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
        self.tick()
        self.assertEqual(self.rec()["phase"], "running")
        self.facts = green_facts()
        self.set_status(self.cfg, "W1", "DONE")
        self.merge_it()

    def test_done_taken_back_while_waiting_returns_to_running(self):
        self.start()
        self.facts = green_facts(reviews=[])
        self.tick()
        self.assertEqual(self.rec()["phase"], "gate")
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.assertEqual(self.rec()["phase"], "running")

    def test_open_threads_get_an_owner_script_and_no_merge(self):  # acceptance 6
        self.start()
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False},
                                          {"id": "PRRT_b", "isResolved": False},
                                          {"id": "PRRT_c", "isResolved": True}])
        self.assertTrue(self.tick())
        self.assertEqual(self.merges(), [])
        script = self.home / ".cache" / "wab" / wab.owner_script_name(self.cfg, "W1", HEAD)
        shown = "~/.cache/wab/" + wab.owner_script_name(self.cfg, "W1", HEAD)  # people see the `~` form
        self.assertTrue(script.is_file())
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding="utf-8")
        self.assertIn("exec python3", text)
        self.assertIn(" owner-merge ", text)
        self.assertTrue(text.rstrip().endswith(f"W1 {RUN_ID} {HEAD}"))
        done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.status(), f"BLOCKED: merge gate passed; 2 unresolved review threads; owner runs {shown}")
        self.assertEqual(self.rec()["phase"], "merging")
        self.assertEqual([q["text"] for q in self.rec()["questions"]],
                         [f"BLOCKED: merge gate passed; 2 unresolved review threads; owner runs {shown}"])
        said = [t for t in self.tg if "незакрытые треды" in t]
        self.assertEqual(len(said), 1)
        self.assertIn(f"Выполни: {shown}", said[0])
        self.assertNotIn("gh pr merge", said[0])
        # the owner runs it: MERGED at the gated head -> the next wave starts
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        self.tick()
        self.launch.assert_called_once()
        self.assertEqual(self.merges(), [])
    def test_the_owner_notice_names_old_p0_p1_threads(self):  # G1
        self.start()
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False, "body": "**![P1 Badge](x)** old"},
                                          {"id": "PRRT_b", "isResolved": False, "body": "nit"}])
        self.tick()
        said = [t for t in self.tg if "незакрытые треды" in t]
        self.assertEqual(len(said), 1)
        self.assertIn("2 (из них P0/P1 из прошлых коммитов: 1)", said[0])

    def test_refused_merge_sends_the_ready_command(self):  # acceptance 5
        self.start()
        self.merge_rc, self.merge_err = 1, "Pull request is not mergeable: required check missing"
        self.assertTrue(self.tick())
        self.assertEqual(len(self.merges()), 1)
        self.assertTrue(self.status().startswith("BLOCKED: merge gate passed; merge refused"))
        self.assertEqual(self.rec()["phase"], "merging")
        sent = [t for t in self.tg if "мердж отклонён" in t]
        self.assertEqual(len(sent), 1)
        shown = "~/.cache/wab/" + wab.owner_script_name(self.cfg, "W1", HEAD)
        self.assertIn(f"Выполни: {shown}", sent[0])
        self.assertNotIn("gh pr merge", sent[0])  # never the raw command: the owner script gates again
        self.assertTrue((self.home / ".cache" / "wab" / wab.owner_script_name(self.cfg, "W1", HEAD)).is_file())
        self.assertIn("not mergeable", sent[0])
        for _ in range(2):
            self.tick()
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(len([t for t in self.tg if "мердж отклонён" in t]), 1)
        # the owner merges by hand: the chain goes on
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        self.tick()
        self.launch.assert_called_once()

    def make_draft(self):
        self.pr["isDraft"] = True
        self.facts["pr"]["draft"] = True

    def undraft(self):
        self.pr["isDraft"] = False
        self.facts["pr"]["draft"] = False

    def readies(self):
        return [c for c in self.gh_calls if c[:3] == ("gh", "pr", "ready")]

    def test_a_draft_pr_is_only_made_ready_then_gated_again_before_the_merge(self):  # r6 HIGH
        self.start()
        self.make_draft()
        self.assertTrue(self.tick())
        self.assertEqual(self.readies(), [("gh", "pr", "ready", "7", "--repo", "o/r")])
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.rec()["phase"], "gate")
        self.assertNotIn("merge_called", self.rec())
        self.assertIn("переведён в ready", self.log())
        # ready started new checks: a pending run is a wait, nothing is merged
        self.undraft()
        self.facts["check_runs"] = [{"name": "ci", "status": "in_progress", "conclusion": None}]
        self.tick()
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.rec()["phase"], "gate")
        # all green on a non-draft PR: the merge, and ready is not asked again
        self.facts = green_facts()
        self.tick()
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(len(self.readies()), 1)
        self.assertEqual(self.rec()["phase"], "merging")

    def test_a_pr_that_stays_draft_after_ready_does_not_loop(self):  # r6 HIGH
        self.start()
        self.make_draft()
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.readies()), 1)
        self.assertEqual(self.merges(), [])
        sent = [t for t in self.tg if "draft" in t]
        self.assertEqual(len(sent), 1)
        self.assertIn("Выполни: ~/.cache/wab/", sent[0])
        self.assertEqual(self.rec()["phase"], "merging")

    def test_a_failed_ready_goes_to_the_owner_script(self):
        self.start()
        self.pr["isDraft"] = True
        self.facts["pr"]["draft"] = True
        self.ready_rc = 1
        self.tick()
        self.assertEqual(self.merges(), [])
        sent = [t for t in self.tg if "перевод в ready отклонён" in t]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("gh pr merge", sent[0])
        self.assertNotIn("gh pr ready", sent[0])
        self.assertIn("Выполни: ~/.cache/wab/", sent[0])

    def test_merged_with_another_head_blocks_and_launches_nothing(self):  # acceptance 1c
        self.start()
        self.merge_it()
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": OLD, "baseRefName": "main"}
        self.assertFalse(self.tick())  # the watch hands the chain over
        self.launch.assert_not_called()
        self.assertTrue(self.status().startswith("BLOCKED: merge gate: PR #7 смержен с HEAD"))
        self.assertEqual(self.rec()["phase"], "awaiting_merge")
        self.assertEqual(len([t for t in self.tg if "смержен с HEAD" in t]), 1)
        self.assertIn("next wave NOT launched", self.log())

    def test_merged_into_another_base_blocks_and_launches_nothing(self):
        self.start()
        self.merge_it()
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD,
                     "baseRefName": "release"}
        self.assertFalse(self.tick())
        self.launch.assert_not_called()
        self.assertEqual(self.rec()["phase"], "awaiting_merge")
        self.assertIn("смержен в release, цепочка ждёт main", self.status())
        self.assertEqual(len([t for t in self.tg if "смержен в release" in t]), 1)

    def test_the_last_wave_merged_into_another_base_does_not_end_the_chain(self):
        cfg = self.start(waves=["W1"])
        self.merge_it()
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD,
                     "baseRefName": "release"}
        self.tick()
        self.assertEqual(self.get_state(cfg)["current"], "W1")
        self.assertNotIn("chain finished", self.log())
        self.assertEqual([t for t in self.tg if "цепочка завершена" in t], [])
        self.assertEqual(self.rec()["phase"], "awaiting_merge")

    def test_merged_with_an_unknown_base_is_not_done(self):
        self.start()
        self.merge_it()
        for base in (None, "missing"):
            self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD}
            if base is None:
                self.view["baseRefName"] = None
            self.assertTrue(self.tick())
        self.launch.assert_not_called()
        self.assertEqual(self.rec()["phase"], "merging")
        self.assertEqual(self.log().count("base not known"), 1)

    def test_an_open_pr_redirected_to_another_base_goes_back_to_the_gate(self):
        self.start()
        self.merge_it()
        self.view = {"state": "OPEN", "mergeCommit": None, "headRefOid": HEAD, "baseRefName": "release"}
        self.assertTrue(self.tick())
        self.assertEqual(self.rec()["phase"], "gate")
        self.launch.assert_not_called()

    def test_closed_pr_blocks_and_launches_nothing(self):
        self.start()
        self.merge_it()
        self.view = {"state": "CLOSED", "mergeCommit": None, "headRefOid": HEAD, "baseRefName": "main"}
        self.assertFalse(self.tick())
        self.launch.assert_not_called()
        self.assertEqual(self.rec()["phase"], "awaiting_merge")
        self.assertTrue(self.status().startswith("BLOCKED: merge gate: PR #7 закрыт без мерджа"))
        self.assertEqual(len(self.rec()["questions"]), 1)
        self.assertIn("закрыт без мерджа", self.rec()["questions"][0]["text"])

    def test_an_unreadable_pr_state_waits(self):
        self.start()
        self.merge_it()
        self.gh_handler = lambda args: _cp(args, 1, "", "HTTP 502")
        self.assertTrue(self.tick())
        self.assertTrue(self.tick())
        self.launch.assert_not_called()
        self.assertEqual(self.log().count("state not read"), 1)

    def test_the_last_wave_ends_the_chain_after_the_merge(self):
        cfg = self.start(waves=["W1"])
        self.merge_it()
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        self.assertFalse(self.tick())
        self.assertIsNone(self.get_state(cfg)["current"])
        self.assertIn("chain finished", self.log())
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)
        self.launch.assert_not_called()

    def test_without_merge_gate_nothing_is_gated_and_the_wave_is_handed_over(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "gate_facts") as facts:
            self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        facts.assert_not_called()  # nothing is gated: no facts, no verdict
        self.assertEqual(self.find_calls, 1)  # only the PR number is asked, once (#34)
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        self.assertEqual(self.get_state(cfg)["waves"]["W1"].get("pr"), 7)

    def test_auto_needs_a_repo(self):
        with self.assertRaises(SystemExit) as ctx:
            self.chain(merge_gate="auto", repo=None)
        self.assertIn("repo", str(ctx.exception))

    def test_merge_notices_are_episodes(self):
        for key in ("merge_owner", "merge_refused", "merge_unknown", "merge_stopped"):
            self.assertIn(key, wab.NOTICE_EPISODE_ENDS)


class GateRecheck(GateBase):  # F2: a pass rests on two equal collections
    def verdict(self, first, second):
        self.start()
        seq = [first, second]
        with mock.patch.object(wab, "gate_facts", side_effect=lambda cfg, pr: seq.pop(0)):
            return wab.gate_check(self.cfg, "W1", self.rec())

    def test_equal_collections_pass(self):
        self.assertEqual(self.verdict(green_facts(), green_facts())["verdict"], "pass")

    def test_a_new_p1_between_the_reads_waits(self):
        p1 = [{"id": 5, "user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) late", "html_url": "u"}]
        v = self.verdict(green_facts(), green_facts(review_comments=p1))
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("факты изменились", v["reasons"][0])

    def test_a_check_run_restarted_between_the_reads_waits(self):
        again = [{"id": 1, "name": "ci", "status": "in_progress", "conclusion": None}]
        first = green_facts(check_runs=[{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}])
        self.assertEqual(self.verdict(first, green_facts(check_runs=again))["verdict"], "wait")

    def test_a_failed_second_collection_waits(self):
        self.assertEqual(self.verdict(green_facts(), {"error": "gh: boom"})["verdict"], "wait")

    def test_other_changes_wait_too(self):
        for name, change in (("thread", {"threads": [{"id": "T", "isResolved": False}]}),
                             ("review", {"reviews": [{"id": 9, "user": BOT, "commit_id": HEAD, "state": "APPROVED"}]}),
                             ("draft", {"pr": {"state": "open", "merged": False, "draft": True, "head": HEAD, "base": "main"}})):
            with self.subTest(change=name):
                self.assertEqual(self.verdict(green_facts(), green_facts(**change))["verdict"], "wait")


class Regate(GateBase):  # D2: the head moved while the wave sat in `merging`
    NEW = "e" * 40

    def moved(self):
        self.pr = {**self.pr, "headRefOid": self.NEW}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": self.NEW, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": self.NEW, "state": "COMMENTED"}])
        self.manifest = green_manifest()
        self.manifest["head"] = self.NEW
        for entry in self.manifest["tasks"].values():
            for r in entry["results"].values():
                r["head"] = self.NEW
        self.work = {**WORK, "head": self.NEW}
        self.view = {"state": "OPEN", "mergeCommit": None, "headRefOid": self.NEW, "baseRefName": "main"}

    def regate(self):
        self.start()
        self.tick()
        self.assertEqual(self.rec()["phase"], "merging")
        old_merges = len(self.merges())
        self.moved()
        self.assertTrue(self.tick())
        rec = self.rec()
        self.assertEqual(rec["phase"], "gate")
        for key in ("gate_sha", "gate_pr", "merge_called", "merge_rc"):
            self.assertNotIn(key, rec)
        self.assertEqual(self.status(), "DONE")
        self.assertIn(f"HEAD сменился после гейта: {HEAD[:12]}→{self.NEW[:12]}, гейт заново", self.log())
        self.assertEqual(len(self.merges()), old_merges)  # the regate itself merges nothing
        return old_merges

    def test_after_a_refused_merge_a_new_head_is_gated_and_merged_by_its_own_pass(self):
        self.merge_rc, self.merge_err = 1, "not mergeable"
        old = self.regate()
        self.merge_rc = 0
        self.tick()
        self.assertEqual(self.merges()[old:], [tuple(gate.merge_argv("o/r", 7, self.NEW))])
        self.assertEqual(self.rec()["gate_sha"], self.NEW)

    def test_while_an_owner_script_waits_a_new_head_is_gated_again(self):
        self.facts = green_facts(threads=[{"id": "T", "isResolved": False}])
        old = self.regate()
        self.moved()
        self.facts["threads"] = [{"id": "T", "isResolved": False}]
        self.tick()
        self.assertEqual(len(self.merges()), old)  # open threads again: owner script, no merge
        self.assertEqual(self.rec()["phase"], "merging")
        self.assertEqual(self.rec()["gate_sha"], self.NEW)

    def test_the_old_sha_is_never_merged_after_the_head_moved(self):
        self.start()
        self.merge_rc = 1
        self.tick()
        self.moved()
        self.facts["check_runs"] = [{"name": "ci", "status": "in_progress", "conclusion": None}]
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.merges()), 1)  # only the refused call at the old sha
        self.assertEqual(self.rec()["phase"], "gate")  # waiting on the new head's CI
        self.facts = green_facts(pr=self.facts["pr"], reviews=self.facts["reviews"])
        self.tick()
        self.assertEqual(len(self.merges()), 2)
        self.assertIn(self.NEW, self.merges()[1])


class OwnerMerge(GateBase):  # D3
    def setUp(self):
        super().setUp()
        self.graphql_calls = []
        self.order = []
        self.graphql_answer = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        real = self.handle

        def handle(args):
            if args[:3] in (("gh", "pr", "ready"), ("gh", "pr", "merge")):
                self.order.append(args[2])
            return real(args)
        self.gh_handler = handle

        def graphql(query, variables):
            self.graphql_calls.append(variables)
            self.order.append("resolve")
            self.on_resolve(variables)
            return self.graphql_answer
        p = mock.patch.object(wab, "gh_graphql", side_effect=graphql)
        p.start()
        self.addCleanup(p.stop)
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False},
                                          {"id": "PRRT_b", "isResolved": True},
                                          {"id": "PRRT_c", "isResolved": False}])
        self.pr["isDraft"] = True
        self.facts["pr"]["draft"] = True
        self.start()
        self.tick()  # the gate passes with open threads: the owner script is written
        self.assertEqual(self.rec()["phase"], "merging")
        self.snapshot = json.dumps(self.rec(), sort_keys=True)
        self.gated = HEAD

    def on_resolve(self, variables):  # by default the closed thread is closed on GitHub too
        for t in self.facts.get("threads", []):
            if t["id"] == variables["id"]:
                t["isResolved"] = True

    def run_it(self):
        return wab.owner_merge(wab.load_chain(self.path), "W1", RUN_ID, self.gated)

    def test_facts_changing_while_threads_close_stop_the_merge(self):  # r13 HIGH
        self.not_draft()
        self.on_resolve = lambda v: self.facts.__setitem__(
            "check_runs", [{"id": 1, "name": "ci", "status": "in_progress", "conclusion": None}])
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("гейт изменился после закрытия тредов", str(ctx.exception))
        self.assertIn("ничего не смержено", str(ctx.exception))
        self.assertEqual(self.merges(), [])
        self.assertNotIn("merge", self.order)

    def test_a_new_p1_while_threads_close_stops_the_merge(self):  # r13 HIGH
        self.not_draft()
        self.on_resolve = lambda v: self.facts.__setitem__("review_comments", [
            {"id": 9, "user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) late", "html_url": "u"}])
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("гейт изменился после закрытия тредов", str(ctx.exception))
        self.assertEqual(self.merges(), [])

    def test_a_new_open_thread_while_threads_close_stops_the_merge(self):  # r13 HIGH
        self.not_draft()
        self.on_resolve = lambda v: self.facts["threads"].append({"id": "PRRT_new", "isResolved": False})
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("появились новые треды, запусти скрипт ещё раз", str(ctx.exception))
        self.assertEqual(self.merges(), [])
        self.assertNotIn("merge", self.order)

    def test_unchanged_facts_after_closing_threads_merge_as_before(self):  # r13 HIGH
        self.not_draft()
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_it()
        self.assertEqual(self.order, ["resolve", "resolve", "merge"])
        self.assertEqual(self.merges(), [tuple(gate.merge_argv("o/r", 7, HEAD))])

    def not_draft(self):  # the owner made it ready earlier: the run that merges sees a non-draft PR
        self.pr["isDraft"] = False
        self.facts["pr"]["draft"] = False

    def refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertEqual(self.order, [])
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.graphql_calls, [])
        self.assertEqual(json.dumps(self.rec(), sort_keys=True), self.snapshot)
        return str(ctx.exception)

    def test_a_draft_is_resolved_and_made_ready_then_the_script_stops(self):  # r6 HIGH
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_it()
        self.assertEqual(self.order, ["resolve", "resolve", "ready"])
        self.assertEqual([v["id"] for v in self.graphql_calls], ["PRRT_a", "PRRT_c"])
        self.assertEqual(self.merges(), [])
        self.assertIn("PR #7 переведён в ready; дождись завершения проверок и запусти скрипт ещё раз", out.getvalue())
        self.assertEqual(json.dumps(self.rec(), sort_keys=True), self.snapshot)  # the wave's record is untouched
        # the second run: the PR is not draft any more, the gate passes again, the merge is pinned
        self.pr["isDraft"] = False
        self.facts = green_facts()
        self.order.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_it()
        self.assertEqual(self.order, ["merge"])
        self.assertEqual(self.merges(), [tuple(gate.merge_argv("o/r", 7, HEAD))])
        self.assertEqual(json.dumps(self.rec(), sort_keys=True), self.snapshot)  # the wave's record is untouched

    def test_a_new_p1_on_the_same_sha_stops_it(self):
        self.facts = green_facts(threads=self.facts["threads"], review_comments=[
            {"id": 9, "user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) late", "html_url": "u"}])
        self.assertIn("fail", self.refused())

    def test_a_pending_check_stops_it(self):
        self.facts = green_facts(threads=self.facts["threads"],
                                 check_runs=[{"id": 1, "name": "ci", "status": "in_progress", "conclusion": None}])
        self.assertIn("wait", self.refused())

    def test_another_head_stops_it(self):
        other = "e" * 40
        self.pr = {**self.pr, "headRefOid": other}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": True, "head": other, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": other, "state": "COMMENTED"}])
        self.manifest = {**green_manifest(), "head": other}
        self.work = {**WORK, "head": other}
        self.refused()

    def test_it_needs_a_gated_wave_in_merging(self):
        st = self.get_state(self.cfg)
        st["waves"]["W1"]["phase"] = "running"
        self.put_state(self.cfg, st)
        self.snapshot = json.dumps(self.rec(), sort_keys=True)
        self.assertIn("not in phase merging", self.refused())

    def test_a_failing_merge_is_reported_with_rc(self):
        self.merge_rc, self.merge_err = 1, "blocked by policy"
        self.not_draft()
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("blocked by policy", str(ctx.exception))

    def test_another_run_or_another_gated_sha_is_refused_before_anything(self):  # G2
        cfg = wab.load_chain(self.path)
        for name, run_id, sha, want in (("run", "2099-01-01", HEAD, "run"), ("sha", RUN_ID, "e" * 40, "gated sha")):
            with self.subTest(case=name):
                self.gh_calls.clear()
                before = self.find_calls
                with self.assertRaises(SystemExit) as ctx:
                    wab.owner_merge(cfg, "W1", run_id, sha)
                self.assertIn(want, str(ctx.exception))
                self.assertEqual((self.gh_calls, self.order, self.graphql_calls), ([], [], []))
                self.assertEqual(self.find_calls, before)  # not even the gate read GitHub
        for bad in (("W1", "r; x", HEAD), ("W1", RUN_ID, "abc"), ("W1", None, None)):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                wab.owner_merge(cfg, *bad)

    def test_a_changed_repo_is_refused_before_any_gh_call(self):  # H1
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["repo"] = "other/repo"  # same run_id, same PR number and sha, another repository
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        self.gh_calls.clear()
        before = self.find_calls
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("identity", str(ctx.exception))
        self.assertEqual((self.gh_calls, self.order, self.graphql_calls, self.find_calls),
                         ([], [], [], before))
        self.assertEqual(json.dumps(self.rec(), sort_keys=True), self.snapshot)

    def test_a_state_without_a_saved_identity_is_refused_and_not_pinned(self):  # H1
        st = self.get_state(self.cfg)
        del st["identity"]
        self.put_state(self.cfg, st)
        with self.assertRaises(SystemExit) as ctx:
            self.run_it()
        self.assertIn("identity", str(ctx.exception))
        self.assertEqual((self.gh_calls, self.order, self.graphql_calls), ([], [], []))
        self.assertNotIn("identity", self.get_state(self.cfg))  # owner-merge writes no pin

    def test_an_unchanged_identity_works_as_before(self):  # H1
        self.not_draft()
        self.run_it()
        self.assertEqual(self.order[-1], "merge")

    def test_the_old_call_form_without_run_and_sha_is_refused(self):  # G2
        for argv in (["wab.py", "owner-merge", str(self.path), "W1"],
                     ["wab.py", "owner-merge", str(self.path), "W1", RUN_ID]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as ctx:
                wab.main(argv)
            self.assertIn("run_id and sha are required", str(ctx.exception))
        self.assertEqual((self.merges(), self.order), ([], []))

    def test_scripts_of_two_runs_do_not_overwrite_each_other(self):  # G2
        cfg_a = wab.load_chain(self.path)
        cfg_b, _ = self.chain(run_id="2099-01-01", merge_gate="auto")
        a = wab.write_owner_script(cfg_a, "W1", HEAD)
        b = wab.write_owner_script(cfg_b, "W1", "e" * 40)
        self.assertNotEqual(a, b)
        self.assertEqual(a.name, wab.owner_script_name(cfg_a, "W1", HEAD))  # the run is in the hash of the name
        self.assertEqual(b.name, wab.owner_script_name(cfg_b, "W1", "e" * 40))
        self.assertNotEqual(wab.owner_script_name(cfg_a, "W1", HEAD), wab.owner_script_name(cfg_b, "W1", HEAD))
        self.assertTrue(a.read_text(encoding="utf-8").rstrip().endswith(f"W1 {RUN_ID} {HEAD}"))
        self.assertTrue(b.read_text(encoding="utf-8").rstrip().endswith("W1 2099-01-01 " + "e" * 40))

    def test_the_reasons_of_the_second_gate_are_masked_in_the_exit_text(self):  # #84
        runs = json.loads(json.dumps(self.facts["check_runs"]))
        for name in ("build password\u200b=Hunter2SecretValue99", "build password=Hunter2SecretValue99",
                     "build ghp_AbCd1234EfGh5678IjKl9012MnOp3456"):
            with self.subTest(name=name):
                self.facts["check_runs"] = json.loads(json.dumps(runs))  # the first gate passes again
                for t in self.facts["threads"]:
                    t["isResolved"] = t["id"] == "PRRT_b"
                self.order.clear()
                self.not_draft()
                self.on_resolve = lambda v, n=name: self.facts.__setitem__(
                    "check_runs", [{"id": 1, "name": n, "status": "completed", "conclusion": "failure"}])
                with self.assertRaises(SystemExit) as ctx:
                    self.run_it()
                out = str(ctx.exception)
                self.assertIn("гейт изменился после закрытия тредов", out)
                self.assertIsNone(leaked(out, ["Hunter2SecretValue99", "AbCd1234EfGh5678"]), out)
                self.assertEqual(self.merges(), [])

    def test_the_value_under_a_hiding_key_is_hidden_in_the_owner_merge_exit(self):  # r3-2
        for key in ("password", "Token", "pass\u200bword", "k\ufe0f", "pa\rss"):
            with self.subTest(key=key):
                self.graphql_answer = {"errors": [{"extensions": {key: "Q7vZk2LmPx9Wt4Yb"}}], "data": None}
                for t in self.facts["threads"]:  # an earlier subtest closed them in the fixture
                    t["isResolved"] = t["id"] == "PRRT_b"
                self.order.clear()
                with self.assertRaises(SystemExit) as ctx:
                    self.run_it()
                self.assertIn("PRRT_a", str(ctx.exception))
                self.assertIsNone(leaked(str(ctx.exception), ["Q7vZk2LmPx9Wt4Yb"]), str(ctx.exception))

    def test_a_collect_error_of_the_resolve_is_masked_in_the_exit_text(self):  # tester-r3-1 F1
        for text in ("gh: password=Hunter2SecretValue99", "gh: password\u200b=Hunter2SecretValue99",
                     "gh: ghp_AbCd1234EfGh5678IjKl9012MnOp3456"):
            with self.subTest(text=text):
                with mock.patch.object(wab, "gh_graphql", side_effect=wab.gate.CollectError(text)):
                    with self.assertRaises(SystemExit) as ctx:
                        self.run_it()
                out = str(ctx.exception)
                self.assertIn("PRRT_a", out)
                self.assertIsNone(leaked(out, ["Hunter2SecretValue99", "AbCd1234EfGh5678"]), out)

    def test_a_thread_is_closed_only_when_the_answer_says_so(self):  # G3
        for name, answer in (("null", {"data": {"resolveReviewThread": None}}),
                             ("false", {"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}}),
                             ("no field", {"data": {}}),
                             ("no data", {}),
                             ("errors", {"errors": [{"message": "Resource not accessible"}], "data": None}),
                             ("truthy string", {"data": {"resolveReviewThread": {"thread": {"isResolved": "true"}}}})):
            with self.subTest(answer=name):
                self.graphql_answer = answer
                for t in self.facts["threads"]:  # an earlier subtest closed them in the fixture
                    t["isResolved"] = t["id"] == "PRRT_b"
                self.order.clear()
                with self.assertRaises(SystemExit) as ctx:
                    self.run_it()
                self.assertIn("PRRT_a", str(ctx.exception))
                self.assertEqual(self.merges(), [])
                self.assertNotIn("ready", self.order)
                self.assertNotIn("merge", self.order)

    def test_cli_command_and_exit_code(self):
        self.not_draft()
        with contextlib.redirect_stdout(io.StringIO()):
            wab.main(["wab.py", "owner-merge", str(self.path), "W1", RUN_ID, HEAD])
        self.assertEqual(self.merges(), [tuple(gate.merge_argv("o/r", 7, HEAD))])
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "queued"}])
        with self.assertRaises(SystemExit) as ctx:
            wab.main(["wab.py", "owner-merge", str(self.path), "W1", RUN_ID, HEAD])
        self.assertNotEqual(ctx.exception.code, 0)


class OwnerMergeHarness(GateBase):
    """gate_check over fixtures; gh_graphql and the order of resolve / ready / merge are recorded."""

    def setUp(self):
        super().setUp()
        self.graphql_calls, self.order = [], []
        self.graphql_answer = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        real = self.handle

        def handle(args):
            if args[:3] in (("gh", "pr", "ready"), ("gh", "pr", "merge")):
                self.order.append(args[2])
            return real(args)
        self.gh_handler = handle

        def graphql(query, variables):
            self.graphql_calls.append(variables)
            self.order.append("resolve")
            for t in self.facts.get("threads", []):  # a closed thread is closed on GitHub too
                if t["id"] == variables["id"]:
                    t["isResolved"] = True
            return self.graphql_answer
        p = mock.patch.object(wab, "gh_graphql", side_effect=graphql)
        p.start()
        self.addCleanup(p.stop)

    def run_owner_merge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.owner_merge(wab.load_chain(self.path), "W1", RUN_ID, HEAD)

    def merged_view(self, head=HEAD):
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": head, "baseRefName": "main"}


class GateBaseRedirect(OwnerMergeHarness):  # r8 HIGH: the PR base is checked at every collection
    def redirected(self):
        facts = green_facts()
        facts["pr"]["base"] = "release"
        return facts

    def test_redirect_after_find_pr_fails_and_nothing_is_merged(self):
        self.start()
        self.facts = self.redirected()
        v = wab.gate_check(self.cfg, "W1", self.rec())
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("release", v["reasons"][0])
        self.assertEqual(self.merges(), [])
        self.tick()
        self.assertEqual(self.merges(), [])

    def test_a_base_change_between_two_collections_waits(self):
        self.start()
        seq = [green_facts(), self.redirected()]
        with mock.patch.object(wab, "gate_facts", side_effect=lambda cfg, pr: seq.pop(0)):
            v = wab.gate_check(self.cfg, "W1", self.rec())
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("факты изменились", v["reasons"][0])
        self.assertEqual(self.merges(), [])

    def test_owner_merge_of_a_redirected_pr_is_refused(self):
        self.start(phase="merging")
        w = self.get_state(self.cfg)
        w["waves"]["W1"]["gate_sha"], w["waves"]["W1"]["gate_pr"] = HEAD, 7
        self.put_state(self.cfg, w)
        self.facts = self.redirected()
        with self.assertRaises(SystemExit) as ctx:
            self.run_owner_merge()
        self.assertIn("release", str(ctx.exception))
        self.assertEqual((self.order, self.merges(), self.graphql_calls), ([], [], []))

    def test_the_right_base_still_passes(self):
        self.start()
        self.assertEqual(wab.gate_check(self.cfg, "W1", self.rec())["verdict"], "pass")


class ExternalOwnerMerge(OwnerMergeHarness):  # R1: the script offered by an external hand-off must work
    def handoff(self):
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False}])
        self.pr["isDraft"] = True
        self.facts["pr"]["draft"] = True
        cfg, path = self.chain(merge_gate="external")
        self.cfg, self.path = cfg, path
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()},
                             "identity": wab._pinned_identity(cfg)})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        rec = self.get_state(cfg)["waves"]["W1"]
        self.assertEqual(rec["phase"], "awaiting_merge")
        return rec

    def test_the_handoff_keeps_the_gated_sha_and_pr(self):
        rec = self.handoff()
        self.assertEqual((rec["gate_sha"], rec["gate_pr"]), (HEAD, 7))

    def test_owner_merge_after_an_external_handoff_resolves_and_readies(self):
        self.handoff()
        self.run_owner_merge()
        self.assertEqual(self.order, ["resolve", "ready"])
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.get_state(self.cfg)["waves"]["W1"]["phase"], "awaiting_merge")

    def test_owner_merge_after_an_external_handoff_refuses_a_moved_head(self):
        self.handoff()
        other = "e" * 40
        self.pr = {**self.pr, "headRefOid": other}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": True, "head": other, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": other, "state": "COMMENTED"}])
        self.manifest = {**green_manifest(), "head": other}
        self.work = {**WORK, "head": other}
        with self.assertRaises(SystemExit):
            self.run_owner_merge()
        self.assertEqual((self.order, self.merges()), ([], []))

    def test_a_regate_on_another_sha_keeps_the_old_script_and_it_refuses_to_merge(self):  # (c)
        rec = self.handoff()
        cfg = wab.load_chain(self.path)
        other = "e" * 40
        a = wab.write_owner_script(cfg, "W1", HEAD)  # what the hand-off wrote
        b = wab.write_owner_script(cfg, "W1", other)  # the re-gate
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_file() and b.is_file())
        self.assertTrue(a.read_text(encoding="utf-8").rstrip().endswith(f"W1 {RUN_ID} {HEAD}"))
        self.assertTrue(b.read_text(encoding="utf-8").rstrip().endswith(f"W1 {RUN_ID} {other}"))
        st = wab.load_state(cfg)
        st["waves"]["W1"]["gate_sha"] = other
        wab.save_state(cfg, st)
        with self.assertRaises(SystemExit):
            self.run_owner_merge()  # the script of the old sha
        self.assertEqual((self.order, self.merges()), ([], []))


class MergedByHand(OwnerMergeHarness):  # R2: a manual merge must not strand the chain
    def refuse_then_merge_by_hand(self, **chain):
        self.start(**chain)
        self.merge_rc, self.merge_err = 1, "blocked by policy"
        self.tick()
        self.assertTrue(self.status().startswith("BLOCKED: merge gate passed; merge refused"))
        self.merged_view()

    def test_a_refused_automerge_then_a_manual_merge_launches_the_next_wave(self):  # (a)
        self.refuse_then_merge_by_hand()
        self.assertTrue(self.tick())
        self.launch.assert_called_once()
        self.assertEqual(self.launch.call_args[0][1], "W2")

    def test_owner_script_after_a_refused_merge_gates_again_and_merges(self):  # P1-b
        self.start()
        self.merge_rc, self.merge_err = 1, "Pull request is not mergeable"
        self.tick()
        self.assertEqual(self.rec()["phase"], "merging")
        self.merge_rc = 0
        self.run_owner_merge()  # phase merging + gate_sha saved by the refusal: accepted, gated anew
        self.assertEqual(len(self.merges()), 2)
        self.assertIn("--match-head-commit", self.merges()[-1])

    def test_owner_merge_then_the_next_wave_launches(self):  # (b)
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False}])
        self.start()
        self.tick()
        self.assertTrue(self.status().startswith("BLOCKED: merge gate passed; 1 unresolved"))
        self.run_owner_merge()
        self.merged_view()
        self.assertTrue(self.tick())
        self.launch.assert_called_once()

    def test_a_crash_between_done_and_launch_is_finished_by_the_next_watch_once(self):  # (c)
        self.refuse_then_merge_by_hand()
        calls = []

        def die_once(*a, **kw):
            calls.append(a[1])
            if len(calls) == 1:
                raise KeyboardInterrupt  # the process dies after the phase was saved
            st = wab.load_state(a[0])  # a real launch makes W2 current
            st["current"] = "W2"
            st["waves"]["W2"] = self.wave_rec("W2")
            self.put_state(a[0], st)
            return True
        self.launch.side_effect = die_once
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        self.assertEqual(self.rec()["phase"], "done")
        self.assertTrue(self.status().startswith("BLOCKED"))  # still the dispatcher's old line
        for _ in range(3):
            wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertEqual(calls, ["W2", "W2"])  # the dead try and exactly one more
        self.assertEqual(len([t for t in self.tg if "волна W1 завершена" in t]), 1)

    def test_the_last_wave_after_a_manual_merge_ends_the_chain(self):  # (d)
        self.refuse_then_merge_by_hand(waves=["W1"])
        self.assertFalse(self.tick())
        self.assertIsNone(self.get_state(self.cfg)["current"])
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)
        self.launch.assert_not_called()

    def test_the_last_wave_cut_short_after_done_still_reports_the_chain_end(self):  # (d)
        self.refuse_then_merge_by_hand(waves=["W1"])
        with mock.patch.object(wab, "close_window", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        for _ in range(2):
            wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)
        self.assertIsNone(self.get_state(self.cfg)["current"])
        self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1)


class ExternalHandoffVerdict(GateBase):
    def handoff(self):
        cfg, path = self.chain(merge_gate="external")
        self.handoff_cfg = cfg
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("PR #7\n", encoding="utf-8")
        self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertEqual(len(self.tg), 1, self.tg)
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        return self.tg[0]

    def test_done_taken_back_while_the_handoff_gate_collects_is_not_handed_over(self):  # Codex P1 on 6c34840
        for back in ("RUNNING", ""):
            with self.subTest(back=back):
                self.tg.clear()
                cfg, path = self.chain(merge_gate="external")
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
                (wab.wave_dir(cfg, "W1") / "result.md").write_text("PR #7\n", encoding="utf-8")

                def collect(cfg_, pr, back=back, cfg=cfg):
                    (wab.wave_dir(cfg, "W1") / "status").write_text(back + ("\n" if back else ""), encoding="utf-8")
                    return self.facts
                with mock.patch.object(wab, "gate_facts", side_effect=collect):
                    self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))  # the watch goes on
                rec = self.get_state(cfg)["waves"]["W1"]
                self.assertNotEqual(rec.get("phase"), "awaiting_merge")
                self.assertFalse(rec.get("pending_exit"))
                self.assertEqual([t for t in self.tg if "сдала PR" in t], [])
                self.assertEqual(self.merges(), [])

    def test_pass_carries_the_merge_command(self):
        text = self.handoff()
        self.assertIn("Гейт мерджа пройден", text)
        self.assertNotIn("gh pr merge", text)  # P1-b: only the owner script, which gates again
        self.assertIn("Выполни: ~/.cache/wab/" + wab.owner_script_name(self.handoff_cfg, "W1", HEAD), text)
        self.assertEqual(self.merges(), [])  # external never merges

    def test_pass_with_open_threads_carries_the_owner_script(self):
        self.facts = green_facts(threads=[{"id": "PRRT_a", "isResolved": False}])
        text = self.handoff()
        self.assertIn("Выполни: ~/.cache/wab/" + wab.owner_script_name(self.handoff_cfg, "W1", HEAD), text)

    def test_fail_and_wait_and_error_are_said_plainly(self):
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}])
        self.assertIn("Гейт мерджа не пройден: «цитата волны: проверки неуспешны: ci (failure)»", self.handoff())

    def test_unreadable_facts_never_stop_the_handoff(self):
        self.pr = gate.CollectError("gh: boom")
        text = self.handoff()
        self.assertIn("Гейт мерджа не проверен", text)
        self.assertIn("boom", text)


class Alarm(GateBase):
    def running(self, **chain):
        return self.start(status="RUNNING", **chain)

    def alarms(self):
        return [s for s in self.sent if "Будильник" in s[2]]

    def test_one_message_per_head_after_checks_and_codex_finish(self):  # acceptance 9
        self.running()
        for _ in range(4):
            self.assertTrue(self.tick())
        self.assertEqual(len(self.alarms()), 1)
        text = self.alarms()[0][2]
        for part in ("PR #7", HEAD[:12], "1 ok", "чисто", "тредов: 0"):
            self.assertIn(part, text)
        self.assertEqual(self.alarms()[0][1], "wv-w1")
        # a new head is a new alarm
        new = "e" * 40
        self.pr = {"number": 7, "headRefOid": new, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": new, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": new, "state": "COMMENTED"}])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.alarms()), 2)
        self.assertIn(new[:12], self.alarms()[1][2])

    def test_pending_checks_or_codex_not_finished_send_nothing(self):  # acceptance 9
        self.running()
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "in_progress", "conclusion": None}])
        for _ in range(3):
            self.tick()
        self.facts = green_facts(reviews=[])
        for _ in range(3):
            self.tick()
        self.facts = green_facts(check_runs=[])
        self.tick()
        self.assertEqual(self.alarms(), [])

    def test_failed_checks_and_findings_are_reported(self):
        self.running()
        self.facts = green_facts(
            check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}],
            review_comments=[{"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) x"}],
            threads=[{"id": "T", "isResolved": False}])
        self.tick()
        text = self.alarms()[0][2]
        for part in ("ci (failure)", "1 замечаний, P0/P1: 1", "тредов: 1"):
            self.assertIn(part, text)

    def test_only_a_running_wave_with_an_open_pr_is_watched(self):
        for name, setup in (
                ("closed PR", lambda: setattr(self, "pr", {**self.pr, "state": "CLOSED"})),
                ("no PR", lambda: setattr(self, "pr", None)),
                ("blocked wave", lambda: self.set_status(self.cfg, "W1", "BLOCKED: q")),
                ("done wave", lambda: self.set_status(self.cfg, "W1", "STARTING"))):
            with self.subTest(case=name):
                self.running()
                self.sent.clear()
                setup()
                self.tick()
                self.assertEqual(self.alarms(), [], name)
        self.running(repo=None, merge_gate=None)
        self.find_calls = 0
        self.tick()
        self.assertEqual(self.find_calls, 0)  # no repo, no GitHub

    def test_gate_errors_are_one_event_and_no_message(self):
        self.running()
        self.pr = gate.CollectError("gh: rate limited")
        for _ in range(3):
            self.tick()
        self.assertEqual(self.alarms(), [])
        self.assertEqual(self.log().count("alarm: PR facts not collected"), 1)
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.facts = {"error": "gh api: boom"}
        self.tick()
        self.assertEqual(self.alarms(), [])
        self.facts = green_facts()
        self.tick()
        self.assertEqual(len(self.alarms()), 1)

    def test_one_error_episode_is_one_event_until_a_clean_collection(self):
        self.running()
        for _ in range(3):
            self.facts = {"error": "gh api: boom"}
            self.tick()
        self.assertEqual(self.log().count("alarm: PR facts not collected"), 1)
        self.facts = green_facts()
        self.tick()  # a clean collection closes the episode
        self.pr = gate.CollectError("gh: down")
        for _ in range(3):
            self.tick()
        self.assertEqual(self.log().count("alarm: PR facts not collected"), 2)
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.facts = {"error": "gh api: boom"}  # the same text again after a failure episode
        for _ in range(3):
            self.tick()
        self.assertEqual(self.log().count("alarm: PR facts not collected"), 3)

    def test_github_is_asked_at_most_once_per_interval(self):
        self.running()
        with mock.patch.object(wab, "ALARM_POLL_SECONDS", 120):
            for _ in range(5):
                self.tick()
        # one poll of the interval + one re-read under the input lock of the single delivery (r2 P1: the
        # price of not sending an alarm about a PR that changed while the lock was awaited); no more
        self.assertEqual(self.find_calls, 2)

    def test_a_failed_delivery_is_retried_not_lost(self):
        self.running()
        real = wab.send_text.side_effect
        wab.send_text.side_effect = lambda *a, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["tmux"]))
        self.tick()
        self.assertEqual(self.alarms(), [])
        wab.send_text.side_effect = real
        self.tick()
        self.assertEqual(len(self.alarms()), 1)

    def crashing_send(self, *, typed):
        def send(name, text, on_typed=None):
            if typed and on_typed:
                on_typed()  # the text is in the window, Enter has not been pressed
            raise KeyboardInterrupt  # the process dies here: nothing after this line runs
        return send

    def crash_tick(self, *, typed):
        real = wab.send_text.side_effect
        wab.send_text.side_effect = self.crashing_send(typed=typed)
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        wab.send_text.side_effect = real

    def restart(self):
        wab.watch(wab.load_chain(self.path), self.path, max_ticks=1)

    def test_a_crash_after_saving_the_intent_before_the_paste_is_delivered_after_restart(self):  # (a) 1
        self.running()
        self.crash_tick(typed=False)
        msg = self.rec()["alarm_msg"]
        self.assertEqual((msg["head"], msg["sent"]), (HEAD, False))
        self.assertEqual(self.alarms(), [])
        self.restart()
        self.assertEqual(len(self.alarms()), 1)
        self.assertTrue(self.rec()["alarm_msg"]["sent"])

    def test_a_crash_after_the_paste_before_enter_restarts_with_enter_only(self):  # (a) 2
        self.running()
        self.crash_tick(typed=True)
        self.assertEqual(self.rec()["pending_enter"], "alarm")
        self.restart()
        self.assertEqual(self.alarms(), [])  # the text is not typed a second time
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertTrue(self.rec()["alarm_msg"]["sent"])
        self.assertNotIn("pending_enter", self.rec())

    def test_a_delivered_alarm_is_never_repeated_even_after_restarts(self):  # (a) 3
        self.running()
        self.tick()
        self.assertEqual(len(self.alarms()), 1)
        self.assertTrue(self.rec()["alarm_msg"]["sent"])
        for _ in range(3):
            self.tick()
            self.restart()
        self.assertEqual(len(self.alarms()), 1)
        self.assertEqual(self.enters, [])

    def test_a_new_head_drops_the_undelivered_old_alarm_and_types_the_new_one(self):  # (a)
        self.running()
        self.crash_tick(typed=True)
        new = "e" * 40
        self.pr = {"number": 7, "headRefOid": new, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": new, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": new, "state": "COMMENTED"}])
        self.tick()
        self.assertEqual(len(self.alarms()), 1)  # only the new text is typed; the old one is not repeated
        self.assertIn(new[:12], self.alarms()[0][2])
        self.assertEqual(self.rec()["alarm_msg"]["head"], new)
        self.assertTrue(self.rec()["alarm_msg"]["sent"])
        self.assertNotIn("pending_enter", self.rec())

    def test_a_wave_that_left_running_gets_no_undelivered_alarm(self):  # (a)
        self.running()
        self.crash_tick(typed=True)
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.restart()
        self.assertEqual(self.alarms(), [])
        self.assertEqual(self.enters, [])
        self.assertNotIn("alarm_msg", self.rec())
        self.assertNotIn("pending_enter", self.rec())

    def test_a_head_that_moved_before_the_retry_never_gets_the_old_text(self):  # r6
        self.running()
        self.crash_tick(typed=False)
        new = "e" * 40
        self.pr = {"number": 7, "headRefOid": new, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": new, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": new, "state": "COMMENTED"}])
        self.tick()
        self.assertEqual(len(self.alarms()), 1)
        self.assertIn(new[:12], self.alarms()[0][2])
        self.assertNotIn(HEAD[:12], self.alarms()[0][2])
        self.assertEqual(self.rec()["alarm_msg"]["head"], new)

    def test_a_head_that_moved_and_is_not_ready_drops_the_old_intent_and_sends_nothing(self):  # r6
        self.running()
        self.crash_tick(typed=False)
        new = "e" * 40
        self.pr = {"number": 7, "headRefOid": new, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": new, "base": "main"}, reviews=[])
        self.tick()
        self.assertEqual(self.alarms(), [])
        self.assertNotIn("alarm_msg", self.rec())

    def test_a_pr_closed_before_the_retry_gets_nothing_and_loses_the_intent(self):  # r6
        self.running()
        self.crash_tick(typed=False)
        self.pr = {**self.pr, "state": "CLOSED"}
        self.tick()
        self.restart()
        self.assertEqual(self.alarms(), [])
        self.assertNotIn("alarm_msg", self.rec())
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.running()
        self.crash_tick(typed=False)
        self.pr = None  # not found at all
        self.tick()
        self.assertEqual(self.alarms(), [])
        self.assertNotIn("alarm_msg", self.rec())

    def test_a_collect_error_before_the_retry_defers_it_and_keeps_the_intent(self):  # r6
        for name, setup in (("find_pr", lambda: setattr(self, "pr", gate.CollectError("gh: boom"))),
                            ("facts", lambda: setattr(self, "facts", {"error": "gh api: boom"}))):
            with self.subTest(case=name):
                self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
                self.facts = green_facts()
                self.sent.clear()
                self.running()
                self.crash_tick(typed=False)
                setup()
                self.tick()
                self.restart()
                self.assertEqual(self.alarms(), [], name)
                msg = self.rec()["alarm_msg"]
                self.assertEqual((msg["head"], msg["sent"]), (HEAD, False))
                self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
                self.facts = green_facts()
                self.tick()
                self.assertEqual(len(self.alarms()), 1, name)
                self.assertTrue(self.rec()["alarm_msg"]["sent"])

    def test_a_retry_before_the_paste_at_the_same_head_carries_the_fresh_verdict(self):  # r5c
        self.running()
        self.crash_tick(typed=False)  # intent saved with the green verdict, nothing typed
        self.assertIn("чисто", self.rec()["alarm_msg"]["text"])
        self.facts = green_facts(
            check_runs=[{"name": "lint", "status": "completed", "conclusion": "failure"}],
            review_comments=[{"user": BOT, "commit_id": HEAD, "original_commit_id": HEAD, "body": "![P1 Badge](x) a"}])
        self.tick()
        self.assertEqual(len(self.alarms()), 1)
        text = self.alarms()[0][2]
        self.assertIn("lint (failure)", text)
        self.assertNotIn("чисто", text)
        self.assertEqual(self.rec()["alarm_msg"]["text"], text)
        self.assertTrue(self.rec()["alarm_msg"]["sent"])


class GateWrappers(Base):
    """gh_api, gh_graphql, find_pr, workdir_state, read_manifest, write_owner_script: the thin layer
    between gate.py and the machine, over a fake `gh`."""

    def setUp(self):
        super().setUp()
        self.find_pr = REAL_FIND_PR

    def gh(self, handler):
        self.gh_handler = handler

    def test_gh_api_returns_json_and_raises_collect_error_otherwise(self):
        self.gh(lambda a: _cp(a, out='{"x": 1}'))
        self.assertEqual(wab.gh_api("repos/o/r/pulls/1?per_page=100&page=1"), {"x": 1})
        self.assertEqual(self.gh_calls[-1], ("gh", "api", "repos/o/r/pulls/1?per_page=100&page=1"))
        self.gh(lambda a: _cp(a, 1, "", "HTTP 404"))
        with self.assertRaises(gate.CollectError) as ctx:
            wab.gh_api("repos/o/r/x")
        self.assertIn("404", str(ctx.exception))
        self.gh(lambda a: _cp(a, out="<html>"))
        with self.assertRaises(gate.CollectError):
            wab.gh_api("repos/o/r/x")

        def timeout(a):
            raise subprocess.TimeoutExpired(a, 60)
        self.gh(timeout)
        with self.assertRaises(gate.CollectError):
            wab.gh_api("repos/o/r/x")

    def test_gh_graphql_types_the_number(self):
        self.gh(lambda a: _cp(a, out='{"data": {}}'))
        self.assertEqual(wab.gh_graphql("query{x}", {"owner": "o", "name": "r", "number": 5}), {"data": {}})
        argv = self.gh_calls[-1]
        self.assertEqual(argv[:3], ("gh", "api", "graphql"))
        self.assertIn("number=5", argv)
        self.assertEqual(argv[argv.index("number=5") - 1], "-F")
        self.assertEqual(argv[argv.index("owner=o") - 1], "-f")

    def git_branch(self, branch="feat/x"):
        REAL_SH("git", "-C", self.cwd, "init", "-q", "-b", branch)
        REAL_SH("git", "-C", self.cwd, "-c", "user.name=t", "-c", "user.email=t@example.com",
                "commit", "-q", "--allow-empty", "-m", "c")

    def test_find_pr_takes_the_open_pr_of_the_branch_else_the_newest(self):
        cfg, _ = self.chain(base_branch="main")
        self.git_branch()
        prs = [{"number": 3, "headRefOid": "1" * 40, "isDraft": False, "state": "MERGED", "baseRefName": "main", **OWN_HEAD},
               {"number": 9, "headRefOid": "2" * 40, "isDraft": False, "state": "CLOSED", "baseRefName": "main", **OWN_HEAD},
               {"number": 5, "headRefOid": "3" * 40, "isDraft": True, "state": "OPEN", "baseRefName": "main", **OWN_HEAD}]
        self.gh(lambda a: _cp(a, out=json.dumps(prs)))
        self.assertEqual(self.find_pr(cfg, self.cwd)["number"], 5)
        argv = self.gh_calls[-1]
        self.assertEqual(argv[:6], ("gh", "pr", "list", "--repo", "o/r", "--head"))
        self.assertEqual(argv[argv.index("--head") + 1], "feat/x")
        self.assertIn("--base", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertIn("baseRefName", argv[argv.index("--json") + 1])
        self.assertIn("all", argv)
        self.gh(lambda a: _cp(a, out=json.dumps(prs[:2])))
        self.assertEqual(self.find_pr(cfg, self.cwd)["number"], 9)
        self.gh(lambda a: _cp(a, out="[]"))
        self.assertIsNone(self.find_pr(cfg, self.cwd))
        self.gh(lambda a: _cp(a, 1, "", "boom"))
        with self.assertRaises(gate.CollectError):
            self.find_pr(cfg, self.cwd)

    def test_find_pr_filters_by_the_base_branch(self):  # P1-a
        cfg, _ = self.chain(base_branch="main")
        self.git_branch()
        prs = [{"number": 4, "headRefOid": "1" * 40, "isDraft": False, "state": "OPEN", "baseRefName": "main", **OWN_HEAD},
               {"number": 8, "headRefOid": "2" * 40, "isDraft": False, "state": "OPEN", "baseRefName": "release", **OWN_HEAD}]
        self.gh(lambda a: _cp(a, out=json.dumps(prs)))
        self.assertEqual(self.find_pr(cfg, self.cwd)["number"], 4)  # the newest one is in a foreign base
        self.gh(lambda a: _cp(a, out=json.dumps(prs[1:])))
        self.assertIsNone(self.find_pr(cfg, self.cwd))  # only a foreign base: no PR of this wave

    def test_find_pr_takes_only_prs_from_the_chain_repository(self):  # Codex P1 on cf32fbd
        cfg, _ = self.chain(base_branch="main")
        self.git_branch()
        own = {"number": 4, "headRefOid": "1" * 40, "isDraft": False, "state": "OPEN", "baseRefName": "main", **OWN_HEAD}
        fork = {"number": 8, "headRefOid": "2" * 40, "isDraft": False, "state": "OPEN", "baseRefName": "main",
                "headRepositoryOwner": {"login": "stranger"}, "headRepository": {"name": "r"}}
        self.gh(lambda a: _cp(a, out=json.dumps([own, fork])))
        self.assertEqual(self.find_pr(cfg, self.cwd)["number"], 4)  # a newer PR of a fork's same-named branch
        argv = self.gh_calls[-1]
        fields = argv[argv.index("--json") + 1]
        self.assertIn("headRepositoryOwner", fields)
        self.assertIn("headRepository", fields)
        # Codex P2 on e6e70c6: gh's default --limit 30 cuts the list before the head-repository filter
        self.assertEqual(argv[argv.index("--limit") + 1], "1000")
        self.gh(lambda a: _cp(a, out=json.dumps([fork])))
        self.assertIsNone(self.find_pr(cfg, self.cwd))  # only a fork's PR: not the wave's
        bare = {k: v for k, v in own.items()}
        bare.pop("headRepositoryOwner"); bare.pop("headRepository")
        self.gh(lambda a: _cp(a, out=json.dumps([bare])))
        self.assertIsNone(self.find_pr(cfg, self.cwd))  # the head repository unknown: never guessed
        twin = dict(own, number=6, headRefOid="3" * 40)
        self.gh(lambda a: _cp(a, out=json.dumps([own, twin])))
        with self.assertRaises(gate.CollectError):  # two open PRs of the wave's branch: ambiguous, not "newest"
            self.find_pr(cfg, self.cwd)

    def test_find_pr_with_an_unknown_base_is_a_collect_error(self):  # P1-a
        cfg, _ = self.chain()  # no base_branch, no origin/HEAD in the working copy
        self.git_branch()
        self.gh(lambda a: _cp(a, out="[]"))
        with self.assertRaises(gate.CollectError) as ctx:
            self.find_pr(cfg, self.cwd)
        self.assertIn("base", str(ctx.exception))
        self.assertEqual([c for c in self.gh_calls if c[:3] == ("gh", "pr", "list")], [])

    def test_find_pr_without_a_branch_or_a_repo(self):
        cfg, _ = self.chain()
        self.assertIsNone(self.find_pr(cfg, self.cwd))  # not even a git repository
        cfg_no_repo, _ = self.chain(repo=None)
        with self.assertRaises(gate.CollectError):
            self.find_pr(cfg_no_repo, self.cwd)

    def test_workdir_state_reads_cleanliness_head_and_fingerprint(self):
        self.git_branch()
        got = wab.workdir_state(self.cwd)
        self.assertTrue(got["clean"])
        self.assertEqual(got["head"], REAL_SH("git", "-C", self.cwd, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(got["fingerprint"], gate.fingerprint(self.cwd))
        (Path(self.cwd) / "dirty.txt").write_text("x\n", encoding="utf-8")
        again = wab.workdir_state(self.cwd)
        self.assertFalse(again["clean"])
        self.assertNotEqual(again["fingerprint"], got["fingerprint"])

    def test_workdir_state_of_a_non_repository_is_unknown_not_an_exception(self):
        got = wab.workdir_state(self.cwd)
        self.assertEqual((got["clean"], got["head"], got["fingerprint"]), (False, None, None))

    def test_read_manifest(self):
        cfg, _ = self.chain()
        self.assertIsNone(wab.read_manifest(cfg, "W1"))
        folder = wab.wave_dir(cfg, "W1") / "superarmanda"
        folder.mkdir()
        (folder / "manifest.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(wab.read_manifest(cfg, "W1"))
        (folder / "manifest.json").write_text("[1]", encoding="utf-8")
        self.assertIsNone(wab.read_manifest(cfg, "W1"))
        (folder / "manifest.json").write_text('{"head": "x", "tasks": {}}', encoding="utf-8")
        self.assertEqual(wab.read_manifest(cfg, "W1")["head"], "x")

    def test_home_form_shows_the_tilde_and_redact_keeps_it(self):  # (b)
        home = Path.home()
        p = home / ".cache" / "wab" / "c-r-W1-abc-merge"
        self.assertEqual(wab.home_form(p), "~/.cache/wab/c-r-W1-abc-merge")
        self.assertEqual(wab.home_form("/etc/x"), "/etc/x")
        self.assertEqual(wab.redact("Выполни: ~/.cache/wab/chain-2026-10-01-W1-" + HEAD[:12] + "-merge"),
                         "Выполни: ~/.cache/wab/chain-2026-10-01-W1-" + HEAD[:12] + "-merge")

    def test_redact_keeps_the_path_of_a_written_owner_script_only(self):  # r20, r21
        cfg, _ = self.chain()
        for sha in ("18d21df306a4dcda4745c2ac1523d3d93c107c2d", "71234567890a" + "b" * 28, "8" * 40,
                    "79123456789" + "c" * 29):
            path = wab.write_owner_script(cfg, "W5", sha)  # a real sha12/id16 may look like a phone or a blob
            line = f"Выполни: {wab.home_form(path)} (заново проверит гейт)"
            self.assertEqual(wab.redact(line), line, path.name)
            # a phone number next to it is still masked
            self.assertIn("[скрыто]", wab.redact(f"{line}, тел. 8 912 345 67 89"))
        # the same shape with no such file (e.g. quoted from the wave's own text) gets no exemption
        secret = "0123456789abcdef0123456789abcdef"
        for fake in (f"~/.cache/wab/{secret}.aaaaaaaaaaaa.bbbbbbbbbbbbbbbb.merge",
                     "~/.cache/wab/W1.71234567890a.0123456789abcdef.merge"):
            self.assertIn("[скрыто]", wab.redact(f"Выполни: {fake}"), fake)
            self.assertNotIn(secret, wab.redact(f"Выполни: {fake}"))

    def test_owner_script_file_is_private_and_replaced_not_appended(self):
        cfg, _ = self.chain()
        path = wab.write_owner_script(cfg, "W1", HEAD)
        self.assertEqual(path.name, wab.owner_script_name(cfg, "W1", HEAD))
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        again = wab.write_owner_script(cfg, "W1", HEAD)
        self.assertEqual(again, path)
        self.assertEqual([p for p in os.listdir(path.parent) if p.startswith(".merge.")], [])
        last = path.read_text(encoding="utf-8").rstrip().splitlines()[-1]
        self.assertEqual(shlex.split(last)[1:], ["python3", str(Path(wab.__file__).resolve()), "owner-merge",
                                                 cfg["chain_file"], "W1", RUN_ID, HEAD])
    def test_redact_keeps_the_merge_command_readable(self):
        command = gate.merge_command("o/r", 7, HEAD)
        self.assertIn(command, wab.redact(f"Выполни: {command}", wab.TG_MESSAGE_LIMIT))


class ManifestDashboard(Base):
    """The dashboard reads the superarmanda manifest of the current wave through `state.py where`."""

    def setUp(self):
        super().setUp()
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.dash = dash
        dash.MANIFEST_CACHE.clear()
        self.state_py = ROOT / "skills" / "superarmanda" / "scripts" / "state.py"

    def state(self, manifest, *args):
        proc = subprocess.run([sys.executable, str(self.state_py), args[0], "--manifest", str(manifest),
                               *args[1:]], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def make_manifest(self, cfg, wave="W1"):
        repo = self.tmp / "gitrepo"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        manifest = cfg["run_dir"] / wave / "superarmanda" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        art = self.tmp / "findings.json"
        art.write_text(json.dumps({"findings": [{"severity": "high"}, {"severity": "medium"}]}),
                       encoding="utf-8")
        self.state(manifest, "init", "--repo", str(repo), "--base", head, "--head", head)
        self.state(manifest, "task-result", "--task", "T1", "--role", "tester", "--status", "pass",
                   "--session-id", "s1", "--head", head)
        self.state(manifest, "task-result", "--task", "T1", "--role", "cross_provider_reviewer",
                   "--status", "findings", "--session-id", "s2", "--head", head, "--artifact", str(art))
        self.state(manifest, "fix-loop", "--task", "T1", "--outcome", "failed",
                   "--source", "cross_provider_reviewer")
        self.state(manifest, "mark", "--task", "T1", "--step", "5", "--safe-point", "true")
        return manifest

    def frame(self, cfg):
        from rich.console import Console
        con = Console(record=True, width=200, height=80, file=io.StringIO(), force_terminal=False)
        con.print(self.dash.safe_render(cfg))
        return con.export_text()

    def running(self, **kw):
        cfg, _ = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(**kw)}})
        self.set_status(cfg, "W1", "RUNNING")
        return cfg

    def test_frame_shows_the_manifest(self):
        cfg = self.running(pr=31)
        self.make_manifest(cfg)
        text = self.frame(cfg)
        for part in ("superarmanda", "T1", "needs_fix", "шаг 6", "coder", "1/2", "1/3",
                     "tester", "pass", "cross_provider_reviewer", "findings", "high 1", "medium 1",
                     "#31", "Codex", "нет результата", "step 6 coder: continue task T1"):
            self.assertIn(part, text)

    def test_pr_falls_back_to_gate_pr_and_the_table_has_a_pr_column(self):
        cfg = self.running(gate_pr=44)
        text = self.frame(cfg)
        self.assertIn("PR", text)
        self.assertIn("#44", text)

    def test_no_manifest_yet(self):
        cfg = self.running()
        self.assertIn("manifest ещё нет", self.frame(cfg))

    def test_run_and_accepted_fields_are_shown_and_junk_does_not_crash(self):
        cfg = self.running()
        base = {"task": "T1", "task_status": "ok", "step": 6, "role": "coordinator", "verdicts": {}}
        for extra, want in (({"run": "2/2", "last_run": True, "accepted": 2}, ("прогон 2/2!", "принято 2")),
                            ({"run": "1/2", "last_run": False, "accepted": 0}, ("прогон 1/2",)),
                            ({"run": ["x"], "accepted": "many", "last_run": None}, ())):
            with self.subTest(extra=extra):
                with mock.patch.object(self.dash, "manifest_where", return_value={**base, **extra}):
                    text = self.frame(cfg)
                self.assertNotIn("кадр не отрисован", text)
                for part in want:
                    self.assertIn(part, text)

    def test_broken_manifest_is_one_line_not_a_crash(self):
        cfg = self.running()
        manifest = cfg["run_dir"] / "W1" / "superarmanda" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{not json", encoding="utf-8")
        text = self.frame(cfg)
        self.assertNotIn("кадр не отрисован", text)
        self.assertIn("W1", text)
        self.assertIn("manifest: state: cannot read manifest", text)
        self.assertNotIn("['", text)

    def test_unreadable_artifact_is_a_question_mark(self):
        cfg = self.running()
        manifest = self.make_manifest(cfg)
        (self.tmp / "findings.json").write_text("nope", encoding="utf-8")
        joined = "\n".join(t.plain for t in self.dash.manifest_lines(cfg, "W1", self.wave_rec()))
        self.assertIn("findings: ?", joined)
        self.assertTrue(manifest.exists())

    def test_a_failed_where_is_retried_after_the_interval(self):
        cfg = self.running()
        manifest = self.make_manifest(cfg)
        real = subprocess.run
        fail = [True]

        def fake(*a, **kw):
            if fail[0]:
                raise subprocess.TimeoutExpired(a[0], 1)
            return real(*a, **kw)

        clock = [1000.0]
        with mock.patch.object(self.dash.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(self.dash.subprocess, "run", side_effect=fake) as run:
            self.assertIn("error", self.dash.manifest_where(cfg, "W1"))
            fail[0] = False
            clock[0] += 5
            self.assertIn("error", self.dash.manifest_where(cfg, "W1"))  # inside the interval: cached
            self.assertEqual(run.call_count, 1)
            clock[0] += self.dash.MANIFEST_POLL_SECONDS
            self.assertEqual(self.dash.manifest_where(cfg, "W1")["task"], "T1")
            self.assertEqual(run.call_count, 2)
            self.dash.manifest_where(cfg, "W1")
            self.assertEqual(run.call_count, 2)
            clock[0] += self.dash.MANIFEST_POLL_SECONDS
            self.dash.manifest_where(cfg, "W1")  # same manifest, but the repository may have moved on
            self.assertEqual(run.call_count, 3)
            manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
            self.dash.manifest_where(cfg, "W1")  # a changed manifest: at once
            self.assertEqual(run.call_count, 4)

    def counts_with_alarm(self, artifact):
        import signal

        def boom(*a):
            raise AssertionError("finding_counts hangs")

        old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(10)
        try:
            return self.dash.finding_counts(str(artifact))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_fifo_artifact_does_not_hang_the_frame(self):
        fifo = self.tmp / "fifo.json"
        os.mkfifo(fifo)
        self.assertIsNone(self.counts_with_alarm(fifo))

    def test_huge_artifact_is_a_question_mark_and_a_regular_one_counts(self):
        big = self.tmp / "big.json"
        big.write_text('{"findings": [], "pad": "' + "x" * (self.dash.ARTIFACT_LIMIT + 10) + '"}',
                       encoding="utf-8")
        self.assertIsNone(self.counts_with_alarm(big))
        ok = self.tmp / "ok.json"
        ok.write_text(json.dumps({"findings": [{"severity": "high"}, {"priority": "P1"}]}), encoding="utf-8")
        self.assertEqual(self.counts_with_alarm(ok), {"high": 1, "P1": 1})

    def test_where_is_cached_by_mtime_and_size(self):
        cfg = self.running()
        self.make_manifest(cfg)
        calls = []
        real = subprocess.run

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)

        with mock.patch.object(self.dash.subprocess, "run", side_effect=spy):
            first = self.dash.manifest_where(cfg, "W1")
            second = self.dash.manifest_where(cfg, "W1")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)


class ChainResult(Base):
    def finished_record(self, **kw):
        base = dict(pr=31, merged={"pr": 31, "sha": "a" * 40, "commit": "b" * 40, "at": time.time()},
                    questions=[{"at": time.time() - 30, "text": "BLOCKED: pick a database"}],
                    attempts=[self.wave_rec(started=time.time() - 600, finished=time.time() - 300)],
                    restarts=2)
        base.update(kw)
        return self.wave_rec(**base)

    def check_file(self, cfg, merged_text):
        path = cfg["run_dir"] / "chain-result.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        for part in ("https://github.com/o/r/pull/31", merged_text, "pick a database", "BLOCKED",
                     "перезапуск", "Время"):
            self.assertIn(part, text)
        self.assertEqual([p for p in os.listdir(path.parent) if p.endswith(".tmp")], [])
        return text

    def test_watch_writes_chain_result_and_one_message(self):
        cfg, path = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.finished_record()}})
        self.set_status(cfg, "W1", "DONE")
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        self.check_file(cfg, "bbbbbbbbbbbb")
        msgs = [t for t in self.tg if "цепочка завершена" in t]
        self.assertEqual(len(msgs), 1, self.tg)
        self.assertIn("chain-result.md", msgs[0])

    def test_done_writes_chain_result_and_one_message(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.finished_record(
            phase="awaiting_merge", merged=None)}})
        wab.main(["wab.py", "done", str(path)])
        wab.main(["wab.py", "done", str(path)])
        text = self.check_file(cfg, "координатором")
        self.assertIn("W1", text)
        msgs = [t for t in self.tg if "цепочка завершена" in t]
        self.assertEqual(len(msgs), 1, self.tg)
        self.assertIn("chain-result.md", msgs[0])

    def test_blocked_status_is_recorded_as_a_question_once(self):
        cfg, path = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "BLOCKED: need a decision")
        wab.watch(cfg, path, max_ticks=1)
        wab.watch(cfg, path, max_ticks=1)
        qs = self.get_state(cfg)["waves"]["W1"]["questions"]
        self.assertEqual(len(qs), 1)
        self.assertIn("need a decision", qs[0]["text"])

    def render(self, cfg, st):
        return wab.write_chain_result(cfg, st)[0].read_text(encoding="utf-8")

    def test_questions_of_earlier_attempts_are_kept(self):
        cfg, _ = self.chain(waves=["W1"])
        old = self.wave_rec(questions=[{"at": 1000.0, "text": "BLOCKED: old question"}])
        cur = self.wave_rec(phase="done", questions=[{"at": 2000.0, "text": "BLOCKED: new question"}],
                            attempts=[old])
        path, summary = wab.write_chain_result(cfg, {"waves": {"W1": cur}})
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index("old question"), text.index("new question"))
        self.assertIn("вопросов 2", summary)

    def test_merge_text_has_three_cases(self):
        cfg, _ = self.chain(waves=["W1"])
        sha = "c" * 40
        cases = [({"merged": {"pr": 1, "sha": sha}}, "смержен (cccccccccccc)"),
                 ({"merged_by": "coordinator"}, "смержен координатором"),
                 ({}, "мердж: нет")]
        for extra, want in cases:
            text = self.render(cfg, {"waves": {"W1": self.wave_rec(phase="done", **extra)}})
            self.assertIn(want, text)
            if not extra:
                self.assertNotIn("координатором", text)

    def test_done_command_marks_the_coordinator(self):
        cfg, path = self.chain(waves=["W1"], merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        wab.main(["wab.py", "done", str(path)])
        self.assertEqual(self.get_state(cfg)["waves"]["W1"].get("merged_by"), "coordinator")
        self.assertIn("смержен координатором", (cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8"))

    def test_file_name_survives_redact_of_a_masked_run_dir(self):
        cfg, _ = self.chain(waves=["W1"])
        fake = Path("/var/folders/7x/k2j3h4l5m6n7p8q9r0s1t2v3w4x5y6z7/T/runs/chain/r1")
        self.assertNotIn("r1", wab.redact(str(fake), wab.TG_MESSAGE_LIMIT))  # the premise: it is masked
        with mock.patch.object(wab, "write_chain_result", return_value=(fake / "chain-result.md", "сводка")):
            text = wab.chain_done_text(cfg, {"waves": {}})
        sent = wab.redact(text, wab.TG_MESSAGE_LIMIT)
        self.assertIn("chain-result.md", sent)
        self.assertIn("цепочка завершена", sent)

    def test_unrepresentable_times_do_not_stop_the_chain(self):
        for how in ("watch", "done"):
            self.tg.clear()
            cfg, path = self.chain(waves=["W1"], merge_gate="external")
            rec = self.finished_record(
                started=1e300, finished=1e300, phase="awaiting_merge" if how == "done" else "running",
                merged=None, questions=[{"at": 1e300, "text": "BLOCKED: q"}],
                attempts=[self.wave_rec(started=1e300, finished=1e300)])
            self.put_state(cfg, {"current": "W1", "waves": {"W1": rec}})
            if how == "watch":
                cfg, path = self.chain(waves=["W1"])
                self.set_status(cfg, "W1", "DONE")
                self.put_state(cfg, {"current": "W1", "waves": {"W1": rec}})
                wab.watch(cfg, path, max_ticks=1)
            else:
                wab.main(["wab.py", "done", str(path)])
            self.assertEqual(len([t for t in self.tg if "цепочка завершена" in t]), 1, (how, self.tg))
            self.assertIsNone(self.get_state(cfg)["current"])

    def test_chain_done_text_survives_any_failure(self):
        cfg, _ = self.chain(waves=["W1"])
        with mock.patch.object(wab, "write_chain_result", side_effect=RuntimeError("boom")):
            self.assertIn("цепочка завершена", wab.chain_done_text(cfg, {"waves": {}}))

    def test_pure_helpers_live_in_wab(self):
        w = self.wave_rec(restarts=1, attempts=[self.wave_rec(restarts=2)])
        self.assertEqual(wab.wave_restarts(w), 3)
        self.assertEqual(len(wab.wave_attempts(w)), 1)


class OwnerHandover(GateBase):  # #36 п.2: the owner merged the wave's PR himself, outside the gate
    def git(self, *args, cwd=None):
        r = REAL_SH("git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd or self.cwd), *args,
                    check=False, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(origin), cwd=self.tmp)
        self.git("init", "-q", "-b", "main")
        Path(self.cwd, "a.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "base")
        self.git("remote", "add", "origin", str(origin))
        self.git("push", "-q", "origin", "main")
        self.git("switch", "-q", "-c", "feat")
        Path(self.cwd, "a.txt").write_text("wave\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "wave")
        self.head = self.git("rev-parse", "HEAD")
        # the owner's squash merge lands on origin/main only (the working copy has not fetched it)
        self.merge = self.git("commit-tree", "HEAD^{tree}", "-p", "origin/main", "-m", "squash")
        self.git("push", "-q", "origin", f"{self.merge}:refs/heads/main")
        self.pr = {"number": 7, "headRefOid": self.head, "isDraft": False, "state": "MERGED"}
        self.view = {"state": "MERGED", "mergeCommit": {"oid": self.merge}, "headRefOid": self.head,
                     "baseRefName": "main"}
        self.facts = green_facts(pr={"state": "merged", "merged": True, "draft": False, "head": self.head,
                                     "base": "main"})
        self.start()
        (self.cfg["run_dir"] / "W1").mkdir(parents=True, exist_ok=True)
        (self.cfg["run_dir"] / "W1" / "next-prompt.md").write_text("/superarmanda --wave W2\n", encoding="utf-8")

    def run_it(self, run_id=RUN_ID, wave="W1"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.owner_handover(wab.load_chain(self.path), wave, run_id)
        return out.getvalue()

    def refused(self, needle, **kw):
        before = wab.state_path(self.cfg).read_bytes()
        with self.assertRaises(SystemExit) as ctx:
            self.run_it(**kw)
        self.assertIn(needle, str(ctx.exception))
        self.assertEqual(wab.state_path(self.cfg).read_bytes(), before)  # nothing written
        return str(ctx.exception)

    def test_handover_hands_the_chain_on(self):
        out = self.run_it()
        rec = self.rec()
        self.assertEqual(rec["phase"], "awaiting_merge")
        self.assertEqual(rec["owner_handover"]["pr"], 7)
        self.assertEqual(rec["owner_handover"]["head"], self.head)
        self.assertEqual(rec["owner_handover"]["merge_commit"], self.merge)
        self.assertTrue(rec["pending_exit"])
        self.assertTrue(any("/exit" in c for c in self.tmux_calls))  # the live window is asked to close
        self.assertIn(f"W1: owner-handover: PR #7 смержен владельцем (head {self.head[:12]}, "
                      f"merge {self.merge[:12]})", self.log())
        self.assertIn(" launch ", out)
        self.assertIn(" W2 ", out)
        self.assertIn(str(self.cfg["run_dir"] / "W1" / "next-prompt.md"), out)
        self.assertIn(" watch ", out)
        st = wab.load_state(self.cfg)
        wab._check_launch_allowed(self.cfg, st, "W2", "wv-w2")  # no refusal: the next wave may follow

    def test_handover_drops_stale_notices_of_the_wave(self):
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["outbox"] = {"blocked": {"text": "old", "next_at": 0}}
        self.put_state(self.cfg, st)
        self.run_it()
        self.assertNotIn("outbox", self.rec())

    def test_the_last_wave_is_finished_with_done(self):
        self.start(waves=["W1"])
        out = self.run_it()
        self.assertIn(" done ", out)
        self.assertNotIn(" launch ", out)
        self.assertEqual(self.rec()["phase"], "awaiting_merge")

    def test_a_dead_wave_without_window_is_handed_over_without_exit(self):
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["phase"] = "dead"
        self.put_state(self.cfg, st)
        self.alive = False
        self.run_it()
        self.assertEqual(self.rec()["phase"], "awaiting_merge")
        self.assertNotIn("pending_exit", self.rec())

    def test_refused_when_the_pr_is_not_merged(self):
        self.view["state"] = "OPEN"
        self.refused("not MERGED")

    def test_refused_when_the_pr_head_is_not_the_wave_head(self):
        self.view["headRefOid"] = "e" * 40
        msg = self.refused("is not the HEAD of the wave")
        self.assertIn(self.head[:12], msg)
        self.assertIn("e" * 12, msg)

    def test_refused_when_the_merge_commit_is_not_in_the_base(self):
        self.view["mergeCommit"] = {"oid": self.head}  # exists, but never reached origin/main
        self.refused("is not an ancestor of origin/main")

    def test_refused_for_another_run(self):
        self.refused("is run", run_id="2026-01-01")

    def test_refused_for_a_wave_that_is_not_current(self):
        self.refused("is not the current wave", wave="W2")

    def test_refused_while_a_dispatcher_holds_the_lock(self):
        DispatcherLock.hold_elsewhere(self, self.cfg)
        self.refused("останови watch")

    def test_refused_in_phase_merging(self):
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["phase"] = "merging"
        self.put_state(self.cfg, st)
        self.refused("merging")

    def test_refused_when_already_handed_over(self):
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["phase"] = "awaiting_merge"
        self.put_state(self.cfg, st)
        self.refused("awaiting_merge")

    def test_refused_without_a_saved_identity(self):
        st = wab.load_state(self.cfg)
        st.pop("identity")
        self.put_state(self.cfg, st)
        self.refused("identity")

    def test_cli_entry(self):
        with contextlib.redirect_stdout(io.StringIO()):
            wab.main(["wab.py", "owner-handover", str(self.path), "W1", RUN_ID])
        self.assertEqual(self.rec()["phase"], "awaiting_merge")

    def test_blocked_line_of_a_pr_merged_outside_the_gate_names_owner_handover(self):
        self.tick()
        status = self.status()
        self.assertTrue(status.startswith("BLOCKED: merge gate: PR уже смержен вне гейта"), status)
        # macOS: /var is /private/var — the line carries the resolved chain path
        self.assertIn(f"owner-handover {self.cfg['chain_file']} W1 {RUN_ID}", status)

    def test_refused_when_next_prompt_is_missing_or_empty(self):
        nxt = self.cfg["run_dir"] / "W1" / "next-prompt.md"
        nxt.unlink()
        self.refused("next-prompt.md of W1 is not usable for W2")
        nxt.write_text("   \n", encoding="utf-8")
        self.refused("next-prompt.md of W1 is not usable for W2")

    def test_refused_when_the_wave_tree_is_dirty(self):
        Path(self.cwd, "a.txt").write_text("edited after the merge\n", encoding="utf-8")
        self.refused("is not clean")
        self.git("checkout", "--", "a.txt")
        Path(self.cwd, "new.txt").write_text("untracked\n", encoding="utf-8")
        self.refused("is not clean")
        Path(self.cwd, "new.txt").unlink()
        self.run_it()  # clean again: handed over
        self.assertEqual(self.rec()["phase"], "awaiting_merge")

    def test_refused_on_untracked_file_with_show_untracked_files_off(self):  # #36 defect 7
        self.git("config", "status.showUntrackedFiles", "no")
        Path(self.cwd, "new.txt").write_text("untracked\n", encoding="utf-8")
        self.refused("is not clean")
        Path(self.cwd, "new.txt").unlink()
        self.run_it()
        self.assertEqual(self.rec()["phase"], "awaiting_merge")

    def test_last_wave_needs_no_next_prompt(self):
        self.start(waves=["W1"])
        (self.cfg["run_dir"] / "W1" / "next-prompt.md").unlink(missing_ok=True)
        self.assertIn(" done ", self.run_it())

    def test_owner_attribution_survives_launch_done_and_chain_result(self):
        self.run_it()
        st = wab.load_state(self.cfg)
        w = st["waves"]["W1"]
        self.assertEqual(wab.merged_by(w), "owner")
        w.pop("owner_handover")
        self.assertEqual(wab.merged_by(w), "coordinator")
        # the last-wave path: `done` keeps the owner's attribution and chain-result.md reports it
        self.start(waves=["W1"])
        self.run_it()
        with contextlib.redirect_stdout(io.StringIO()):
            wab.done_cmd(wab.load_chain(self.path), "W1")
        self.assertEqual(self.rec()["merged_by"], "owner")
        result = (self.cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")
        self.assertIn(f"смержен владельцем ({self.merge[:12]})", result)
        self.assertNotIn("смержен координатором", result)


# ---------------------------------------------------------------- #40: local signals without Telegram
class LocalAttention(Base):
    """Without a Telegram transport a notice leaves ATTENTION in the run directory and a
    display-message on the waves' tmux server; the end of its episode removes the file."""
    SECRET = "password=hunter2hunter2hunter2"

    def setUp(self):
        super().setUp()
        self.clients = "client-a\nclient-b\n"
        self.display_rc = 0
        inner = wab.sh.side_effect

        def fake(*args, **kw):
            if args and args[0] == "tmux" and "list-clients" in args:
                self.tmux_calls.append(args)
                return subprocess.CompletedProcess(args, 0, self.clients, "")
            if args and args[0] == "tmux" and "display-message" in args:
                self.tmux_calls.append(args)
                if self.display_rc:
                    raise subprocess.CalledProcessError(self.display_rc, args, "", "no server")
                return subprocess.CompletedProcess(args, 0, "", "")
            return inner(*args, **kw)
        wab.sh.side_effect = fake

    def blocked(self):
        cfg, _ = self.chain(telegram=None)
        rec = self.wave_rec("W2", phase="running", last_status=f"BLOCKED: вопрос {self.SECRET}")
        st = {"current": "W2", "waves": {"W2": rec}}
        self.put_state(cfg, st)
        st = wab.load_state(cfg)
        w = st["waves"]["W2"]
        wab.put_notice(w, "blocked", "q1", f"wave-autobot: волна W2 BLOCKED: {self.SECRET}\nподробности")
        wab.save_state(cfg, st)
        wab.flush_notices(cfg, st, w)
        return cfg, st, w

    def displays(self):
        return [c for c in self.tmux_calls if "display-message" in c]

    def test_notice_without_telegram_writes_attention_and_display_message(self):
        cfg, st, w = self.blocked()
        att = cfg["run_dir"] / "ATTENTION"
        self.assertTrue(att.is_file())
        text = att.read_text(encoding="utf-8")
        self.assertNotIn("hunter2hunter2", text)
        self.assertIn("W2", text)
        self.assertIn("attach -t wv-w2", text)
        self.assertRegex(text, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ")
        self.assertNotIn("подробности", text)  # only the first line
        shown = self.displays()
        self.assertEqual(len(shown), 2, self.tmux_calls)  # every client of the waves' server
        for call in shown:
            msg = call[-1]
            self.assertNotIn("hunter2hunter2", msg)
            self.assertLessEqual(len(msg), 150)
        self.assertIn("notify(skipped)", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_display_failure_does_not_stop_the_tick_and_leaves_one_event(self):
        self.display_rc = 1
        cfg, st, w = self.blocked()
        self.assertTrue((cfg["run_dir"] / "ATTENTION").is_file())
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("display-message failed"), 1, log)

    def test_running_after_blocked_removes_attention(self):
        cfg, st, w = self.blocked()
        att = cfg["run_dir"] / "ATTENTION"
        self.assertTrue(att.is_file())
        w["last_status"] = "RUNNING"
        wab.save_state(cfg, st)
        self.assertFalse(att.exists())

    def test_ack_removes_attention(self):
        cfg, st, w = self.blocked()
        att = cfg["run_dir"] / "ATTENTION"
        self.assertTrue(att.is_file())
        wab.ack_wave_notices(w)
        wab.save_state(cfg, st)
        self.assertFalse(att.exists())

    def test_attention_is_rewritten_with_the_remaining_signal(self):
        cfg, st, w = self.blocked()
        wab.put_notice(w, "idle", "d1", "wave-autobot: волна W2 молчит 30 мин")
        wab.save_state(cfg, st)
        wab.flush_notices(cfg, st, w)
        att = cfg["run_dir"] / "ATTENTION"
        w["last_status"] = "RUNNING"
        wab.save_state(cfg, st)
        self.assertIn("молчит", att.read_text(encoding="utf-8"))

    def test_display_message_escapes_tmux_formats(self):  # CodeRabbit (security) on #47
        cfg, _ = self.chain(telegram=None)
        wab.display_all("волна W2: #(touch /tmp/pwned) #{session_name}")
        [*_, msg] = self.displays()[-1]
        self.assertNotIn("#(", msg.replace("##", ""))
        self.assertIn("##(touch", msg)
        self.assertIn("##{session_name}", msg)

    def test_attention_command_exit_codes(self):
        cfg, path = self.chain(telegram=None)
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
            wab.main(["wab.py", "attention", str(path)])
        self.assertIn(e.exception.code, (0, None))
        cfg, st, w = self.blocked()
        out = io.StringIO()
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(out):
            wab.main(["wab.py", "attention", str(path)])
        self.assertEqual(e.exception.code, 1)
        self.assertIn("W2", out.getvalue())


# ---------------------------------------------------------------- #42: delivery with a submit check
PROMPT_LINE = "│ ❯ {} │"


class Submit(Base):
    """`say` and the dispatcher's send_text share one «was it submitted» check on the screen."""

    def setUp(self):
        super().setUp()
        self.m = load_orig("wab_submit")  # unpatched send_text / submit; tmux itself is faked below
        self.screens = []  # the screen after each Enter; the last one repeats
        self.m_enters = []
        self.m_calls = []

        def fake_sh(*args, **kw):
            self.m_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        for name, kw in (("sh", {"side_effect": fake_sh}),
                         ("press_enter", {"side_effect": lambda n: self.m_enters.append(n)}),
                         ("pane_text", {"side_effect": self.screen}),
                         ("tmux_alive", {"side_effect": lambda n: True})):
            p = mock.patch.object(self.m, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def screen(self, name):
        i = min(len(self.m_enters), len(self.screens)) - 1
        return self.screens[max(i, 0)] if self.screens else ""

    TEXT = "Решение A: делай миграцию сначала, потом API"
    RULE = "╭" + "─" * 40 + "╮"
    PREVIEW = "\n".join(["история", RULE, PROMPT_LINE.format("[Pasted text #1 +3 lines]"),
                         "  paste again to expand"])
    TYPED = "\n".join(["история", RULE, PROMPT_LINE.format("Решение A: делай миграцию сначала, по")])
    EMPTY = "\n".join(["история", "> Решение A: делай миграцию сначала, потом API", RULE, PROMPT_LINE.format(" ")])

    def say(self, text=None):
        cfg, path = self.chain(telegram=None)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1")}})
        f = self.tmp / "answer.md"
        f.write_text(text or self.TEXT, encoding="utf-8")
        return cfg, path, f

    def test_say_presses_enter_again_until_the_input_is_empty(self):
        cfg, path, f = self.say()
        self.screens = [self.PREVIEW, self.EMPTY]  # after the 1st Enter: preview; after the 2nd: sent
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.main(["wab.py", "say", str(path), "W1", str(f)])
        self.assertEqual(len(self.m_enters), 2, self.m_enters)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("say", log)
        self.assertIn("Решение A", log)
        self.assertTrue(any("paste-buffer" in c for c in self.m_calls), self.m_calls)

    def test_say_fails_when_the_text_never_leaves_the_input(self):
        cfg, path, f = self.say()
        self.screens = [self.TYPED]
        err = io.StringIO()
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            self.m.main(["wab.py", "say", str(path), "W1", str(f)])
        self.assertNotIn(e.exception.code, (0, None))
        self.assertEqual(len(self.m_enters), 1 + self.m.SUBMIT_RETRIES)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("say FAILED", log)
        self.assertIn("Решение A", log)

    def test_say_redacts_the_logged_line_and_takes_no_lock(self):
        cfg, path, f = self.say("секрет password=hunter2hunter2hunter2 тут")
        self.screens = [self.EMPTY]
        before = wab.state_path(cfg).read_text(encoding="utf-8")
        with mock.patch.object(self.m, "_RunLock", side_effect=AssertionError("lock")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.main(["wab.py", "say", str(path), "W1", str(f)])
        self.assertEqual(wab.state_path(cfg).read_text(encoding="utf-8"), before)
        self.assertNotIn("hunter2hunter2", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_dispatcher_does_not_count_a_paste_preview_as_delivered(self):
        cfg, path, f = self.say()
        self.screens = [self.PREVIEW]
        st = self.m.load_state(cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            ok = self.m._deliver(cfg, st, "W1", "alarm", self.m.send_text, self.TEXT)
        self.assertFalse(ok)
        self.assertEqual(st["waves"]["W1"].get("pending_enter"), "alarm")  # the retry presses Enter only
        self.screens = [self.EMPTY]
        self.m_enters.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            ok = self.m._deliver(cfg, st, "W1", "alarm", self.m.send_text, self.TEXT)
        self.assertTrue(ok)
        self.assertNotIn("pending_enter", st["waves"]["W1"])
        self.assertEqual(sum("paste-buffer" in c for c in self.m_calls), 1)  # not pasted twice

    def test_preview_phrase_in_history_does_not_count_as_unsent(self):  # Codex P2 on #47
        history = "\n".join(["> обсуждали «paste again to expand» в прошлом ответе", "история",
                              self.RULE, PROMPT_LINE.format(" ")])
        self.assertIsNone(wab.unsent_reason(history, self.TEXT))
        self.assertIsNotNone(wab.unsent_reason(self.PREVIEW, self.TEXT))  # the real footer still counts

    def test_recovery_with_empty_text_checks_against_the_saved_head(self):  # Codex P2 on #47
        cfg, path, f = self.say()
        self.screens = [self.TYPED]  # pasted, Enter did not send
        st = self.m.load_state(cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.m._deliver(cfg, st, "W1", "/update", self.m.send_text, self.TEXT))
        w = st["waves"]["W1"]
        self.assertEqual(w.get("pending_enter"), "/update")
        self.assertTrue(w.get("pending_text_head"))
        # a restarted dispatcher no longer has the text (advance_pending / recover_update pass "")
        self.m_enters.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.m._deliver(cfg, st, "W1", "/update", self.m.send_text, ""))
        self.screens = [self.EMPTY]
        self.m_enters.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.m._deliver(cfg, st, "W1", "/update", self.m.send_text, ""))
        self.assertNotIn("pending_text_head", st["waves"]["W1"])

    def hold_input_lock(self, cfg, wave="W1"):
        import fcntl
        path = cfg["run_dir"] / wave / "input.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(fh.close)
        return fh

    def test_dispatcher_postpones_while_another_writer_types(self):  # Codex P2 on #47
        cfg, path, f = self.say()
        self.hold_input_lock(cfg)
        self.screens = [self.EMPTY]
        st = self.m.load_state(cfg)
        with mock.patch.object(self.m, "INPUT_LOCK_SECONDS", 0.2), contextlib.redirect_stdout(io.StringIO()):
            ok = self.m._deliver(cfg, st, "W1", "alarm", self.m.send_text, self.TEXT)
        self.assertFalse(ok)
        self.assertFalse(any("paste-buffer" in c for c in self.m_calls), self.m_calls)  # nothing typed
        self.assertNotIn("pending_enter", st["waves"]["W1"])
        self.assertIn("alarm postponed", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_say_does_not_type_while_another_writer_holds_the_input(self):  # Codex P2 on #47
        cfg, path, f = self.say()
        self.hold_input_lock(cfg)
        self.screens = [self.EMPTY]
        with mock.patch.object(self.m, "INPUT_LOCK_SECONDS", 0.2), self.assertRaises(SystemExit) as e, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.m.main(["wab.py", "say", str(path), "W1", str(f)])
        self.assertNotIn(e.exception.code, (0, None))
        self.assertFalse(any("paste-buffer" in c for c in self.m_calls), self.m_calls)
        self.assertIn("say FAILED", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_legacy_pending_enter_without_head_is_reported_unverified(self):  # CodeRabbit on #47
        cfg, path, f = self.say()
        st = self.m.load_state(cfg)
        st["waves"]["W1"]["pending_enter"] = "/update"  # written by an older version: no text head
        self.screens = [self.EMPTY]
        with contextlib.redirect_stdout(io.StringIO()):
            self.m._deliver(cfg, st, "W1", "/update", self.m.send_text, "")
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("delivery NOT verified", log)
        self.assertIn("unverified_enter", st["waves"]["W1"].get("outbox", {}))

    def test_say_refuses_while_the_dispatcher_owes_an_enter(self):  # Codex P2 on #47, round 3
        cfg, path, f = self.say()
        st = self.m.load_state(cfg)
        st["waves"]["W1"]["pending_enter"] = "alarm"
        st["waves"]["W1"]["pending_text_head"] = "будильник"
        self.m.save_state(cfg, st)
        self.screens = [self.EMPTY]
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.m.main(["wab.py", "say", str(path), "W1", str(f)])
        self.assertNotIn(e.exception.code, (0, None))
        self.assertFalse(any("paste-buffer" in c for c in self.m_calls), self.m_calls)
        self.assertIn("owes an Enter", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_say_refuses_a_wave_without_a_launched_session(self):  # Codex P2 on #47, round 4
        cfg, path, f = self.say()
        self.screens = [self.EMPTY]
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
            self.m.main(["wab.py", "say", str(path), "W2", str(f)])  # W2 never launched in this run
        self.assertIn("no launched session", str(e.exception.code))
        self.assertFalse(any("paste-buffer" in c for c in self.m_calls), self.m_calls)

    def test_sent_prompt_in_history_is_not_the_input_line(self):  # live regression W4, 2026-10-03
        sent = "❯ Решение A: делай миграцию сначала, потом API"  # Claude Code draws sent prompts with ❯
        no_box_yet = "\n".join(["история", sent, "✻ Thinking…"])
        self.assertIsNone(wab.unsent_reason(no_box_yet, self.TEXT))
        with_box = "\n".join(["история", sent, "─" * 40, "❯ ", "─" * 40])
        self.assertIsNone(wab.unsent_reason(with_box, self.TEXT))
        still_typed = "\n".join(["история", "─" * 40, "❯ Решение A: делай миграцию", "─" * 40])
        self.assertIsNotNone(wab.unsent_reason(still_typed, self.TEXT))

    def test_unsent_reason_on_screens(self):
        self.assertIsNotNone(wab.unsent_reason(self.PREVIEW, self.TEXT))
        self.assertIsNotNone(wab.unsent_reason(self.TYPED, self.TEXT))
        self.assertIsNone(wab.unsent_reason(self.EMPTY, self.TEXT))


# ---------------------------------------------------------------- #41: decision policy in mandate.md
class DecisionPolicy(Base):
    """A labelled BLOCKED line whose class (and rec) chain.json `decision_policy` allows, outside the
    red zone, is answered by the dispatcher itself — once per episode, capped."""
    POLICY = [{"class": "needs_decision"}, {"class": "blocked_cap", "rec": "new_run"},
              {"class": "question", "rec": "A"}]
    ASK = "BLOCKED: [class=needs_decision rec=invariant red=no] needs_decision T3: гонка; варианты: invariant | cut_surface"

    def mandate(self, policy=None, **over):
        """chain.json with `decision_policy` (None = POLICY, "" = no field) and a pinned mandate.md whose
        «Политика решений» heading is plain text: the policy lives only in chain.json."""
        body = (f"Прогон: {RUN_ID}\nМердж при зелёном CI.\n\n## Политика решений\n"
                f"- auto: class=plan_mismatch\n").encode("utf-8")
        policy = self.POLICY if policy is None else policy
        cfg, path = self.chain(mandate_sha256=hashlib.sha256(body).hexdigest(),
                               decision_policy=policy if policy != "" else None, **over)
        (cfg["run_dir"] / "mandate.md").write_bytes(body)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1")}})
        return cfg, path

    def tick(self, cfg, status=None):
        if status is not None:
            self.set_status(cfg, "W1", status)
        st = wab.load_state(cfg)
        wab.tick(cfg, st)
        return wab.load_state(cfg)

    def answers(self):
        return [t for kind, n, t in self.sent if "РЕШЕНИЕ ПО ПОЛИТИКЕ" in t]

    def log(self, cfg):
        p = cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def decisions(self, cfg):
        p = cfg["run_dir"] / "W1" / "policy-decisions.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    def owner_asked(self):
        return [t for t in self.tg if "ждёт тебя" in t]

    # ----- the label -----
    def test_a_half_sent_answer_dropped_on_a_status_change_is_charged(self):  # Codex P2 on #49, round 2
        cfg, _ = self.mandate()
        st = wab.load_state(cfg)
        w = st["waves"]["W1"]
        w.update(pending_enter="policy answer", pending_text_head="[wab] РЕШЕНИЕ", policy_pending=self.ASK)
        self.put_state(cfg, st)
        st = self.tick(cfg, "RUNNING")  # the wave moved on: the answer may already have been submitted
        w = st["waves"]["W1"]
        self.assertNotIn("policy_pending", w)
        self.assertEqual(wab.auto_answers_used(w), 1)

    def test_status_rechecked_under_the_input_lock(self):  # Codex P2 on #49, round 4
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        real_lock = wab._InputLock.__enter__

        def enter_and_owner_answers(lock):  # while waiting for the lock, `say` answered and the wave moved on
            got = real_lock(lock)
            self.set_status(cfg, "W1", "RUNNING")
            return got
        with mock.patch.object(wab._InputLock, "__enter__", enter_and_owner_answers):
            st = wab.load_state(cfg)
            wab.tick(cfg, st)
        self.assertEqual(self.answers(), [])
        self.assertIn("changed while waiting for the input", self.log(cfg))

    def test_identical_blocked_rewritten_after_an_unseen_running_is_a_new_episode(self):  # Codex P2, #49
        cfg, _ = self.policy_chain() if hasattr(self, "policy_chain") else self.mandate()
        self.tick(cfg, self.ASK)
        self.assertEqual(len(self.answers()), 1)
        # the wave took the answer, wrote RUNNING and then the same BLOCKED line before the next poll
        path = cfg["run_dir"] / "W1" / "status"
        path.write_text("RUNNING", encoding="utf-8")
        st0 = os.stat(path)
        path.write_text(self.ASK, encoding="utf-8")
        os.utime(path, ns=(st0.st_atime_ns, st0.st_mtime_ns + 5_000_000))
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 2)  # answered again, not left waiting silently

    def test_answer_is_bound_to_the_pre_delivery_stamp(self):  # Codex P2 on #49
        cfg, _ = self.mandate()
        path = cfg["run_dir"] / "W1" / "status"
        self.set_status(cfg, "W1", self.ASK)
        real = wab.send_text

        def fast_wave(name, text, on_typed=None):  # the wave answers and re-blocks before send_text returns
            real(name, text, on_typed=on_typed)
            path.write_text("RUNNING", encoding="utf-8")
            st0 = os.stat(path)
            path.write_text(self.ASK, encoding="utf-8")
            os.utime(path, ns=(st0.st_atime_ns, st0.st_mtime_ns + 5_000_000))
        with mock.patch.object(wab, "send_text", fast_wave):
            st = wab.load_state(cfg)
            wab.tick(cfg, st)
        self.assertEqual(len(self.answers()), 1)
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 2)  # the identical rewrite is a new episode

    def test_label_is_parsed(self):
        got = wab.parse_blocked_label(self.ASK)
        self.assertEqual((got["class"], got["rec"], got["red"]), ("needs_decision", "invariant", False))
        self.assertTrue(got["question"].startswith("needs_decision T3"))
        self.assertTrue(wab.parse_blocked_label("BLOCKED: [class=question rec=A red=yes] q")["red"])

    def test_no_label_or_garbage_is_none(self):
        for bad in ("BLOCKED: needs_decision T3: рекомендую invariant",
                    "BLOCKED: merge gate: checks red",
                    "BLOCKED: [class=needs_decision rec=invariant] нет red",
                    "BLOCKED: [class=nonsense rec=A red=no] q",
                    "BLOCKED: [class=question rec=A red=maybe] q",
                    "BLOCKED: [class=question rec=A red=no extra=1] q",
                    "BLOCKED: [class=question class=question rec=A red=no] q",
                    "BLOCKED: [class=question rec=$(rm) red=no] q",
                    "BLOCKED: [class=question rec=A red=no q",
                    "RUNNING", ""):
            with self.subTest(bad=bad):
                self.assertIsNone(wab.parse_blocked_label(bad))

    # ----- the policy (chain.json) -----
    def test_decision_policy_is_loaded(self):
        cfg, _ = self.mandate()
        self.assertEqual(wab.decision_policy(cfg), [{"class": "needs_decision", "rec": None},
                                                    {"class": "blocked_cap", "rec": "new_run"},
                                                    {"class": "question", "rec": "A"}])
        cfg, _ = self.chain()
        self.assertEqual(wab.decision_policy(cfg), [])
        self.assertNotIn("decision_policy", cfg)  # not defaulted: older runs keep their identity
        cfg, _ = self.chain(decision_policy=[])
        self.assertEqual(wab.decision_policy(cfg), [])

    def test_decision_policy_refusals(self):
        for bad in ([{"class": "bogus"}], [{"class": "plan_mismatch"}], [{"class": "merge_gate"}],
                    [{"class": "question", "red": "no"}], [{"class": "question", "color": "x"}],
                    [{"rec": "A"}], [{"class": "question", "rec": "$(rm)"}], [{"class": "question", "rec": ""}],
                    [{"class": "question", "rec": None}], [{"class": "question", "rec": 1}], [{"class": 1}],
                    ["question"], {"class": "question"}, "question", 0):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as cm:
                    self.chain(decision_policy=bad)
                self.assertIn("chain.json: decision_policy", str(cm.exception))
        for cls, word in (("plan_mismatch", "владел"), ("merge_gate", "never")):
            with self.assertRaises(SystemExit) as cm:
                self.chain(decision_policy=[{"class": cls}])
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            self.chain(decision_policy=[{"class": "question", "red": "no"}])
        self.assertIn("red zone", str(cm.exception))

    def test_decision_policy_is_identity_not_tunable(self):
        self.assertNotIn("decision_policy", wab.TUNABLE)
        cfg, path = self.mandate()
        st = wab.load_state(cfg)
        self.assertTrue(wab.check_identity(cfg, st, "launch"))  # what the first launch pins
        pinned = st["identity"]
        cfg2, path2 = self.mandate(policy=[{"class": "question"}])  # edited between launch and watch
        st2 = wab.load_state(cfg2)
        st2["identity"] = pinned
        self.put_state(cfg2, st2)
        with self.assertRaises(SystemExit) as cm:
            wab.watch(cfg2, path2, max_ticks=1)
        self.assertIn("decision_policy", str(cm.exception))

    def test_mandate_heading_is_plain_text(self):
        cfg, _ = self.mandate(policy="")  # mandate.md carries «## Политика решений» with a rule in it
        text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertIn("Политика решений", text)  # no refusal, just text of the mandate
        self.tick(cfg, "BLOCKED: [class=plan_mismatch rec=fix red=no] план")
        self.assertEqual(self.answers(), [])

    def test_max_auto_answers_is_checked(self):
        cfg, _ = self.chain()
        self.assertEqual(cfg["max_auto_answers"], 3)
        for bad in (-1, "3", True, 1.5, None, 10_001):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    self.chain(max_auto_answers=bad) if bad is not None else wab.load_chain(
                        self._chain_with_null())

    def _chain_with_null(self):
        _, path = self.chain()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["max_auto_answers"] = None
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    # ----- the auto-answer -----
    def test_allowed_class_is_answered_once(self):
        cfg, _ = self.mandate()
        st = self.tick(cfg, self.ASK)
        self.assertEqual(len(self.answers()), 1, self.sent)
        self.assertIn("вариант invariant", self.answers()[0])
        self.assertIn("W1: policy auto-answer: class=needs_decision rec=invariant", self.log(cfg))
        [line] = self.decisions(cfg)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ class=needs_decision rec=invariant needs_decision T3")
        self.assertEqual(len([t for t in self.tg if "по политике" in t]), 1, self.tg)
        self.assertEqual(self.owner_asked(), [])
        self.assertEqual(st["waves"]["W1"]["auto_answers"], 1)
        self.tick(cfg)  # the same episode
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 1, self.sent)
        self.assertEqual(len(self.decisions(cfg)), 1)
        self.assertEqual(self.owner_asked(), [])

    def test_rec_named_by_the_rule_must_match(self):
        cfg, _ = self.mandate()
        self.tick(cfg, "BLOCKED: [class=blocked_cap rec=new_run red=no] кап 3/3 T2")
        self.assertEqual(len(self.answers()), 1)

    def test_no_answer_cases(self):
        cases = {
            "red zone": (None, "BLOCKED: [class=needs_decision rec=A red=yes] мердж без мандата?"),
            "class outside the policy": (None, "BLOCKED: [class=plan_mismatch rec=fix red=no] план"),
            "other rec": (None, "BLOCKED: [class=question rec=B red=no] вопрос"),
            "merge_gate": ([{"class": "needs_decision"}],
                           "BLOCKED: [class=merge_gate rec=A red=no] gate"),
            "no label": (None, "BLOCKED: needs_decision T3: рекомендую invariant"),
            "no policy": ("", self.ASK),
        }
        for name, (policy, status) in cases.items():
            with self.subTest(name):
                self.sent.clear()
                self.tg.clear()
                cfg, _ = self.mandate(policy=policy)
                self.tick(cfg, status)
                self.assertEqual(self.answers(), [], self.sent)
                self.assertEqual(len(self.owner_asked()), 1, self.tg)
                self.assertEqual(self.decisions(cfg), [])

    def test_merge_gate_is_never_answered_even_if_a_rule_slipped_through(self):
        cfg, _ = self.mandate()
        with mock.patch.object(wab, "decision_policy", return_value=[{"class": "merge_gate", "rec": None}]):
            self.tick(cfg, "BLOCKED: [class=merge_gate rec=A red=no] gate")
        self.assertEqual(self.answers(), [])
        self.assertEqual(len(self.owner_asked()), 1, self.tg)

    def test_plan_mismatch_label_is_valid_and_goes_to_the_owner(self):
        self.assertEqual(wab.parse_blocked_label("BLOCKED: [class=plan_mismatch rec=fix red=no] p")["class"],
                         "plan_mismatch")

    def test_answer_in_flight_when_the_window_died_is_charged_after_a_restart(self):  # Codex on a46c311
        cfg, _ = self.mandate()
        st = wab.load_state(cfg)
        old = st["waves"]["W1"]
        # _deliver typed the answer and saved pending_enter, then the window died before the count
        old.update(auto_answers=2, pending_enter="policy answer", pending_text_head="[wab] РЕШЕНИЕ",
                   policy_pending="BLOCKED: [class=question rec=A red=no] q", phase="dead")
        st["waves"]["W1"] = self.wave_rec("W1", attempts=[{k: v for k, v in old.items() if k != "outbox"}])
        self.put_state(cfg, st)
        self.assertEqual(wab.auto_answers_used(wab.load_state(cfg)["waves"]["W1"]), 3)
        self.tick(cfg, "BLOCKED: [class=question rec=A red=no] после перезапуска")
        self.assertEqual(self.answers(), [], self.sent)
        self.assertIn("policy cap reached", self.log(cfg))

    def test_enter_only_retry_of_the_answer_in_flight_is_not_blocked_by_the_cap(self):
        cfg, _ = self.mandate()
        st = wab.load_state(cfg)
        st["waves"]["W1"].update(auto_answers=2, pending_enter="policy answer",
                                 pending_text_head="[wab] РЕШЕНИЕ", policy_pending=self.ASK)
        self.put_state(cfg, st)
        st = self.tick(cfg, self.ASK)
        self.assertEqual(self.enters, ["wv-w1"])  # the Enter of the typed answer, nothing typed again
        self.assertEqual(wab.auto_answers_used(st["waves"]["W1"]), 3)
        self.assertNotIn("policy cap reached", self.log(cfg))

    def test_cap_counts_every_attempt_of_the_wave(self):
        cfg, _ = self.mandate()
        for i in range(3):
            self.tick(cfg, f"BLOCKED: [class=question rec=A red=no] вопрос {i}")
        self.assertEqual(len(self.answers()), 3)
        st = wab.load_state(cfg)  # a restart archives the record into `attempts` (as _launch does)
        old = st["waves"]["W1"]
        st["waves"]["W1"] = self.wave_rec("W1", attempts=[{k: v for k, v in old.items() if k != "outbox"}])
        self.put_state(cfg, st)
        self.tick(cfg, "BLOCKED: [class=question rec=A red=no] вопрос после перезапуска")
        self.assertEqual(len(self.answers()), 3, self.sent)
        self.assertIn("policy cap reached", self.log(cfg))
        self.assertEqual(len(self.owner_asked()), 1, self.tg)

    def test_status_changed_before_delivery_is_not_answered(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        real_read = wab.read

        def moved(p, *a, **kw):  # the owner answered in the window: the wave wrote RUNNING meanwhile
            if Path(p).name == "status" and moved.n > 0:
                return "RUNNING"
            moved.n += 1
            return real_read(p, *a, **kw)
        moved.n = 0
        with mock.patch.object(wab, "read", side_effect=moved):
            self.tick(cfg)
        self.assertEqual(self.answers(), [], self.sent)
        self.assertEqual(self.decisions(cfg), [])
        self.assertEqual(self.owner_asked(), [], self.tg)  # a new episode, not a stale question

    def test_successful_retry_closes_the_blocked_signal_and_attention(self):
        cfg, _ = self.mandate(telegram=None)
        calls = []

        def flaky(n, t, **kw):
            calls.append(t)
            if len(calls) == 1:
                raise wab.NotSubmitted("the text is still in the input line")
            self.sent.append(("text", n, t))
        wab.send_text.side_effect = flaky
        with mock.patch.object(wab, "display_all"):
            st = self.tick(cfg, self.ASK)
            self.assertIn("blocked", st["waves"]["W1"].get("attention", {}))
            self.assertTrue((cfg["run_dir"] / "ATTENTION").exists())
            st = self.tick(cfg)
        self.assertEqual(len(self.answers()), 1)
        self.assertNotIn("blocked", st["waves"]["W1"].get("attention", {}))
        self.assertNotIn("blocked", st["waves"]["W1"].get("outbox", {}))
        att = cfg["run_dir"] / "ATTENTION"
        self.assertFalse(att.exists() and "ждёт тебя" in att.read_text(encoding="utf-8"))

    def test_failed_delivery_is_retried_once(self):
        cfg, _ = self.mandate()
        calls = []

        def flaky(n, t, **kw):
            calls.append(t)
            if len(calls) == 1:
                raise wab.NotSubmitted("the text is still in the input line")
            self.sent.append(("text", n, t))
        wab.send_text.side_effect = flaky
        self.tick(cfg, self.ASK)
        self.assertEqual(self.answers(), [])
        self.assertEqual(len(self.owner_asked()), 1, self.tg)
        self.assertEqual(self.decisions(cfg), [])
        self.tick(cfg)
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 1, self.sent)
        self.assertEqual(len(self.decisions(cfg)), 1)

    def test_cap_sends_the_fourth_episode_to_the_owner(self):
        cfg, _ = self.mandate()
        for i in range(5):
            self.tick(cfg, f"BLOCKED: [class=question rec=A red=no] вопрос {i}")
        self.assertEqual(len(self.answers()), 3, self.sent)
        self.assertEqual(self.log(cfg).count("policy cap reached"), 1, self.log(cfg))
        self.assertEqual(len(self.owner_asked()), 2, self.tg)

    def test_cap_is_tuned_live(self):
        # tunable, not identity: a state pinned without the field (or with another value) is the same run
        self.assertIn("max_auto_answers", wab.TUNABLE)
        cfg, _ = self.mandate()
        ident = wab._pinned_identity(cfg)
        cfg2, _ = self.chain(mandate_sha256=cfg["mandate_sha256"], decision_policy=self.POLICY, max_auto_answers=7)
        self.assertEqual(wab._pinned_identity(cfg2), ident)
        cfg, _ = self.mandate(max_auto_answers=0)
        self.tick(cfg, self.ASK)
        self.assertEqual(self.answers(), [])
        self.assertIn("policy cap reached", self.log(cfg))


class W1MaxRuns(Base):  # #39: the run cap of a wave travels with its session
    def test_default_is_two(self):
        cfg, _ = self.chain()
        self.assertEqual(cfg["max_runs"], 2)

    def test_bad_values_refused(self):
        for bad in (0, -1, 1.5, "2", True, False, None, [2]):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    if bad is None:
                        _, path = self.chain()
                        doc = json.loads(path.read_text(encoding="utf-8"))
                        doc["max_runs"] = None
                        path.write_text(json.dumps(doc), encoding="utf-8")
                        wab.load_chain(path)
                    else:
                        self.chain(max_runs=bad)
                self.assertIn("max_runs", str(ctx.exception))

    def test_good_value_and_tunable(self):
        cfg, _ = self.chain(max_runs=5)
        self.assertEqual(cfg["max_runs"], 5)
        self.assertIn("max_runs", wab.TUNABLE)

    def test_env_goes_into_new_session(self):
        for chain_over, want in (({}, "WAB_MAX_RUNS=2"), ({"max_runs": 4}, "WAB_MAX_RUNS=4")):
            with self.subTest(want=want):
                cfg, _ = self.chain(**chain_over)
                self.tmux_calls.clear()
                st = {"waves": {"W1": self.wave_rec(sessions=["s" * 8])}}
                wab.start_session(cfg, st, "W1")
                new = [c for c in self.tmux_calls if c[1] == "new-session"][0]
                self.assertIn(want, new)
                self.assertIn(f"WAB_DIR={wab.wave_dir(cfg, 'W1')}", new)


class W1MaxRunsFile(Base):  # r2 P1: the live cap reaches an already running session
    def cap_file(self, cfg, wave="W1"):
        return wab.wave_dir(cfg, wave) / "max-runs"

    def test_new_session_gets_the_file(self):
        cfg, _ = self.chain(max_runs=4)
        st = {"waves": {"W1": self.wave_rec(sessions=["s" * 8])}}
        wab.start_session(cfg, st, "W1")
        self.assertEqual(self.cap_file(cfg).read_text(encoding="utf-8"), "4\n")

    def test_watch_updates_the_file_when_chain_json_max_runs_changes(self):
        cfg, path = self.chain(max_runs=2)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "RUNNING")
        seen = []

        def fake_tick(c, st):
            seen.append(self.cap_file(c).read_text(encoding="utf-8") if self.cap_file(c).exists() else None)
            if len(seen) == 1:
                doc = json.loads(path.read_text(encoding="utf-8"))
                doc["max_runs"] = 3
                path.write_text(json.dumps(doc), encoding="utf-8")
            return True
        with mock.patch.object(wab, "drop_stale_btab"), mock.patch.object(wab, "tick", side_effect=fake_tick):
            wab.watch(cfg, path, max_ticks=3)
        self.assertEqual(seen, ["2\n", "3\n", "3\n"])
        self.assertEqual([p.name for p in self.cap_file(cfg).parent.iterdir() if p.name.startswith("max-runs")],
                         ["max-runs"])  # no temp leftovers


class W1AfterDoneEdits(GateBase):  # #45: the wave pushes to the PR after DONE
    A, B, C = "a" * 40, "b" * 40, "c" * 40

    def feed(self, *verdicts):
        """gate_check answers from a list: (verdict, head)."""
        seq = list(verdicts)

        def fake(cfg, wave, w):
            kind, head = seq.pop(0) if len(seq) > 1 else seq[0]
            return {"verdict": kind, "reasons": ["r"], "head": head, "number": 7, "unresolved": [],
                    "draft": False, "old_p01": [], "pr": {}}
        p = mock.patch.object(wab, "gate_check", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)

    def count(self):
        return self.log().count("волна правит после DONE")

    def test_one_event_per_head_change(self):
        self.start()
        self.feed(("wait", self.A), ("wait", self.A), ("wait", self.B), ("wait", self.B), ("wait", self.C))
        for _ in range(2):
            self.tick()
        self.assertFalse((self.cfg["run_dir"] / "events.log").exists() and self.count())  # first sight: remembered only
        self.tick()
        self.assertEqual(self.count(), 1)
        self.assertIn(f"W1: волна правит после DONE: {self.A[:12]}→{self.B[:12]}", self.log())
        self.tick()
        self.assertEqual(self.count(), 1)  # the same new head: nothing more
        self.tick()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.rec()["done_head"], self.C)

    def test_gate_fail_resets_the_memory(self):
        self.start()
        self.feed(("wait", self.A), ("fail", self.A), ("wait", self.B))
        self.tick()
        self.tick()
        self.assertNotIn("done_head", self.rec())
        self.set_status(self.cfg, "W1", "DONE")  # the wave fixed and wrote DONE again
        self.tick()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.rec()["done_head"], self.B)

    def test_status_taken_back_resets_the_memory(self):
        self.start()
        self.feed(("wait", self.A))
        self.tick()
        self.assertEqual(self.rec()["done_head"], self.A)
        self.set_status(self.cfg, "W1", "RUNNING")
        self.tick()
        self.assertNotIn("done_head", self.rec())


class W1AfterDoneEditsMerging(Regate):
    def test_head_moved_in_merging_gives_the_event_once(self):
        self.start()
        self.tick()
        self.assertEqual(self.rec()["phase"], "merging")
        self.assertEqual(self.rec()["done_head"], HEAD)
        self.moved()
        self.tick()
        self.assertEqual(self.log().count("волна правит после DONE"), 1)
        self.assertIn(f"волна правит после DONE: {HEAD[:12]}→{self.NEW[:12]}", self.log())
        self.tick()  # the gate on the new head passes and merges: no second event
        self.assertEqual(self.log().count("волна правит после DONE"), 1)


class W1AcceptedOnPass(GateBase):  # pass verdict carries accepted limitations
    def test_event_with_accepted_limitations(self):
        self.start()
        real = wab.gate_check

        def with_notes(cfg, wave, w):
            v = real(cfg, wave, w)
            if v["verdict"] == "pass":
                v["reasons"] = ["accepted medium reviewer: шум в логах"]
            return v
        with mock.patch.object(wab, "gate_check", side_effect=with_notes):
            self.tick()
        self.assertIn("W1: merge gate passed with accepted limitations: accepted medium reviewer: шум в логах",
                      self.log())
        self.assertEqual(len(self.merges()), 1)  # the merge goes on

    def test_no_event_without_notes(self):
        self.start()
        self.tick()
        self.assertNotIn("accepted limitations", self.log())


# ---------------------------------------------------------------- #48 #31 #50 #53: clearing the input line
class InputEmpty(unittest.TestCase):
    """`input_empty_reason` on REAL Claude Code screens (tests/fixtures/screens): the placeholder of an
    empty input is dim text, the typed text is not."""

    def test_empty_screens_are_empty(self):
        for name in ("03-after-cu.ansi", "18-cleared.ansi", "22-folded-cleared-late.ansi"):
            with self.subTest(name):
                self.assertIsNone(wab.input_empty_reason(live(name)))

    def test_screens_with_text_are_not_empty(self):
        for name, part in (("04-folded.ansi", "folded paste"), ("19-folded.ansi", "folded paste")):
            with self.subTest(name):
                self.assertIn(part, wab.input_empty_reason(live(name)))

    def test_plain_screens_cannot_tell_the_placeholder_from_text(self):
        # `capture-pane -p` has no SGR: the placeholder reads as typed text, hence the check reads -e
        self.assertIn("text in the input line", wab.input_empty_reason(live("01-empty.txt")))
        for name in ("02-typed.txt", "07-multiline.txt", "15-multi3-up.txt", "16-iter4.txt"):
            with self.subTest(name):
                self.assertIn("text in the input line", wab.input_empty_reason(live(name)))

    def test_a_remnant_on_a_continuation_line_is_text(self):  # 16-iter4: the first line is empty, the third stays
        self.assertIn("text in the input line", wab.input_empty_reason(live("16-iter4.txt")))

    def test_folded_footer_alone_is_not_success_yet(self):  # 20: empty input, the footer lingers for seconds
        self.assertIn("paste preview", wab.input_empty_reason(live("20-folded-cleared-1s.ansi")))

    def test_no_input_box_is_its_own_reason(self):
        for screen in ("", "Do you trust this folder?\n❯ 1. Yes\n  2. No\n"):
            with self.subTest(screen=screen[:10]):
                self.assertEqual(wab.input_empty_reason(screen), "no input box on the screen")

    def test_a_two_inside_a_compound_colour_is_not_dim(self):  # task review: a false «empty» is worse than a spare round
        rule = "\x1b[38;5;244m" + "─" * 30
        for sgr in ("38;2;255;2;255", "38;5;2", "48;5;2", "58;5;2", "38;2;2;2;2", "38:2::255:2:255", "38:5:2",
                    "1;38;5;2", "48;2;0;2;0", "38;9;2"):
            with self.subTest(sgr=sgr):
                screen = "\n".join(["история", rule, f"\x1b[39m❯\u00a0\x1b[{sgr}mответ\x1b[0m", rule])
                self.assertIn("text in the input line", wab.input_empty_reason(screen))

    def test_a_real_dim_placeholder_with_other_attributes_is_still_dim(self):
        rule = "─" * 30
        for sgr in ("2", "1;2", "38;5;244;2", "2;38;5;2"):
            with self.subTest(sgr=sgr):
                screen = "\n".join(["история", rule, f"❯\u00a0\x1b[{sgr}mTry \"x\"\x1b[0m", rule])
                self.assertIsNone(wab.input_empty_reason(screen))

    def test_a_reset_ends_dim(self):
        rule = "─" * 30
        for off in ("0", "22", ""):
            with self.subTest(off=off):
                screen = "\n".join(["история", rule, f"❯\u00a0\x1b[2mTry\x1b[{off}mтекст", rule])
                self.assertIn("text in the input line", wab.input_empty_reason(screen))

    def test_boxed_prompt_with_a_text_or_blank(self):
        rule = "╭" + "─" * 30 + "╮"
        self.assertIsNone(wab.input_empty_reason("\n".join(["история", rule, "│ ❯          │"])))
        self.assertIn("text in the input line",
                      wab.input_empty_reason("\n".join(["история", rule, "│ ❯ Решение A │"])))


class ClearInput(unittest.TestCase):
    """`clear_input`: success only when the SCREEN shows an empty input; no key on a screen without a box."""

    def setUp(self):
        self.screens = []
        self.keys = 0
        self.polls = 0

        def pane_ansi(name):
            self.polls += 1
            return self.screens[min(self.keys, len(self.screens) - 1)] if self.screens else ""

        def keys(name):
            self.keys += 1
        for attr, kw in (("pane_ansi", {"side_effect": pane_ansi}), ("send_clear_keys", {"side_effect": keys})):
            p = mock.patch.object(wab, attr, **kw)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_already_empty_presses_nothing(self):
        self.screens = [live("18-cleared.ansi")]
        self.assertIsNone(wab.clear_input("wv-w1"))
        self.assertEqual(self.keys, 0)

    def test_no_box_presses_nothing(self):
        self.screens = ["Do you trust this folder?\n❯ 1. Yes\n  2. No\n"]
        self.assertEqual(wab.clear_input("wv-w1"), "no input box on the screen")
        self.assertEqual(self.keys, 0)

    def test_text_is_cleared_by_a_round(self):
        self.screens = [live("19-folded.ansi"), live("18-cleared.ansi")]
        self.assertIsNone(wab.clear_input("wv-w1"))
        self.assertEqual(self.keys, 1)

    def test_a_stuck_input_is_a_failure_after_the_rounds(self):
        self.screens = [live("19-folded.ansi")]
        why = wab.clear_input("wv-w1")
        self.assertIn("folded paste", why)
        self.assertEqual(self.keys, wab.CLEAR_ROUNDS)

    def test_the_box_vanishing_mid_way_stops_the_keys(self):
        self.screens = [live("19-folded.ansi"), "Do you trust this folder?\n❯ 1. Yes\n"]
        self.assertEqual(wab.clear_input("wv-w1"), "no input box on the screen")
        self.assertEqual(self.keys, 1)

    def test_the_footer_is_waited_out_without_keys(self):  # 20 then 22
        self.screens = [live("20-folded-cleared-1s.ansi")]
        polls = []

        def pane_ansi(name):
            polls.append(1)
            return live("22-folded-cleared-late.ansi") if len(polls) > 3 else live("20-folded-cleared-1s.ansi")
        with mock.patch.object(wab, "pane_ansi", side_effect=pane_ansi):
            self.assertIsNone(wab.clear_input("wv-w1"))
        self.assertEqual(self.keys, 0)

    def test_a_footer_that_stays_is_not_success(self):
        self.screens = [live("20-folded-cleared-1s.ansi")]
        self.assertIn("paste preview", wab.clear_input("wv-w1"))
        self.assertEqual(self.keys, 0)


class InputClear(GateBase):
    """#48: a pending_enter is never dropped silently — the input line is cleared and the screen proves it."""

    def running(self, **chain):
        return self.start(status="RUNNING", **chain)

    def alarms(self):
        return [s for s in self.sent if s[0] == "text" and "Будильник" in s[2]]

    def crash_tick(self):
        def send(name, text, on_typed=None):
            if on_typed:
                on_typed()  # the text is in the window, Enter has not been pressed
            raise KeyboardInterrupt
        real = wab.send_text.side_effect
        wab.send_text.side_effect = send
        with self.assertRaises(KeyboardInterrupt):
            self.tick()
        wab.send_text.side_effect = real

    def typed_alarm(self):
        self.running()
        self.crash_tick()
        self.assertEqual(self.rec()["pending_enter"], "alarm")
        self.ansi = live("19-folded.ansi")  # the alarm sits in the input line as a folded paste

    def say(self, text="ответ"):
        f = self.tmp / "say.md"
        f.write_text(text, encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            return wab.say_cmd(self.cfg, "W1", f)

    # ----- #48 -----
    def test_status_change_clears_the_typed_alarm(self):
        self.typed_alarm()
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertEqual(len(self.clear_keys), 1)
        rec = self.rec()
        self.assertNotIn("pending_enter", rec)
        self.assertNotIn("pending_clear", rec)
        self.assertIn("input cleared: alarm", self.log())

    def test_no_empty_line_on_the_screen_is_not_a_success(self):  # the criterion
        self.typed_alarm()
        self.clear_works = False
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        rec = self.rec()
        self.assertEqual(rec["pending_clear"]["what"], "alarm")
        self.assertNotIn("pending_enter", rec)
        self.assertIn("input NOT cleared: alarm", self.log())
        self.assertNotIn("input cleared", self.log())
        for _ in range(3):
            self.tick()
        self.assertEqual(self.log().count("input NOT cleared"), 1)  # one event per episode, not per tick
        self.assertGreater(len(self.clear_keys), 1)  # ...but every tick tries again

    def test_say_refuses_while_the_input_is_not_cleared_and_works_after(self):
        self.typed_alarm()
        self.clear_works = False
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertFalse(self.say())
        self.assertEqual([s for s in self.sent if s[0] == "text"], [])
        self.clear_works = True
        self.tick()
        self.assertNotIn("pending_clear", self.rec())
        self.assertTrue(self.say())
        self.assertEqual([s[0] for s in self.sent if s[0] == "text"], ["text"])
        self.assertEqual(self.sent[-1][0], "text")  # typed after the successful clear

    def test_new_alarm_is_typed_only_after_a_successful_clear(self):
        self.typed_alarm()
        self.clear_works = False
        new = "e" * 40
        self.pr = {"number": 7, "headRefOid": new, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": new, "base": "main"},
                                 reviews=[{"user": BOT, "commit_id": new, "state": "COMMENTED"}])
        self.tick()
        self.tick()
        self.assertEqual(self.alarms(), [])  # not pasted over the old text
        self.assertIn("pending_clear", self.rec())
        self.clear_works = True
        self.tick()
        self.assertEqual(len(self.alarms()), 1)
        kinds = [s[0] for s in self.sent if s[0] in ("clear", "text")]
        self.assertEqual(kinds[-2:], ["clear", "text"])  # the paste comes right after the clear
        self.assertNotIn("pending_clear", self.rec())

    def test_gate_failure_of_a_new_episode_is_not_typed_over_the_old_one(self):
        self.start()
        cfg, st = self.cfg, wab.load_state(self.cfg)
        w = st["waves"]["W1"]
        w.update(pending_enter="gate failure", pending_text_head="[wab] Гейт")
        wab.save_state(cfg, st)
        self.ansi = live("19-folded.ansi")
        wab._gate_failed(cfg, st, "W1", w, wab.wave_dir(cfg, "W1"), "проверки красные")
        kinds = [s[0] for s in self.sent if s[0] in ("clear", "text")]
        self.assertEqual(kinds, ["clear", "text"], self.sent)

    def test_a_closed_window_drops_the_reserve_without_keys(self):
        self.typed_alarm()
        self.clear_works = False
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertIn("pending_clear", self.rec())
        keys = len(self.clear_keys)
        self.alive = False
        st = wab.load_state(self.cfg)
        self.assertTrue(wab._settle_clear(self.cfg, st, "W1", st["waves"]["W1"]))
        self.assertEqual(len(self.clear_keys), keys)
        self.assertNotIn("pending_clear", st["waves"]["W1"])

    def test_no_keys_when_a_dialog_is_on_the_screen(self):
        self.typed_alarm()
        self.ansi = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
        self.set_status(self.cfg, "W1", "BLOCKED: q")
        self.tick()
        self.assertEqual(self.clear_keys, [])
        self.assertIn("no input box", self.log())
        self.assertIn("pending_clear", self.rec())

    # ----- closing the window must not send /exit into a dirty input line -----
    def done_with_typed_alarm(self):
        self.running(merge_gate="external")  # DONE hands the PR over and closes the window at once
        self.crash_tick()
        self.assertEqual(self.rec()["pending_enter"], "alarm")
        self.ansi = live("19-folded.ansi")
        self.set_status(self.cfg, "W1", "DONE")
        (wab.wave_dir(self.cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")

    def exits(self):
        return [c for c in self.tmux_calls if c[1:2] == ("send-keys",) and "/exit" in c]

    def test_exit_waits_for_a_cleared_input(self):
        self.done_with_typed_alarm()
        self.tick()
        w = self.rec()
        self.assertNotIn("pending_enter", w)
        order = [s[0] for s in self.sent if s[0] == "clear"]
        self.assertTrue(order, "the input line was not cleared before /exit")
        self.assertEqual(len(self.exits()), 1)
        first_exit = next(i for i, c in enumerate(self.tmux_calls) if "/exit" in c)
        first_clear = next(i for i, c in enumerate(self.tmux_calls) if c[1:2] == ("send-keys",) and "C-u" in c)
        self.assertLess(first_clear, first_exit)

    def test_exit_is_not_sent_while_the_clear_fails(self):
        self.done_with_typed_alarm()
        self.clear_works = False
        self.tick()
        self.assertEqual(self.exits(), [])
        self.assertTrue(self.rec().get("pending_exit"))  # the intent stays
        self.assertIn("input NOT cleared", self.log())
        self.tick()
        self.assertEqual(self.exits(), [])
        self.clear_works = True
        self.tick()
        self.assertEqual(len(self.exits()), 1)

    def test_exit_is_not_sent_over_a_dirty_screen_without_any_pending(self):
        self.running()
        self.ansi = live("19-folded.ansi")
        self.clear_works = False
        w = wab.load_state(self.cfg)
        w["waves"]["W1"]["pending_exit"] = True
        wab.close_window(self.cfg, w, w["waves"]["W1"])
        self.assertEqual(self.exits(), [])

    # ----- #31 under the lock: the PR is looked at again right before the Enter -----
    def flip_pr_when_the_lock_is_taken(self, pr):
        real = wab._InputLock.__enter__
        done = []

        def enter(lock):
            got = real(lock)
            if not done:
                done.append(1)
                self.pr = pr  # the PR changed while this writer waited for the input lock
            return got
        return mock.patch.object(wab._InputLock, "__enter__", enter)

    def test_head_moved_while_waiting_for_the_lock_gets_no_enter(self):
        self.typed_alarm()
        with self.flip_pr_when_the_lock_is_taken(
                {"number": 7, "headRefOid": "e" * 40, "isDraft": False, "state": "OPEN"}):
            self.tick()
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", self.rec())
        self.assertFalse(self.rec().get("alarm_msg", {}).get("head") == HEAD and self.rec()["alarm_msg"].get("sent"))

    def test_pr_closed_while_waiting_for_the_lock_gets_no_enter(self):
        self.typed_alarm()
        with self.flip_pr_when_the_lock_is_taken(
                {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "CLOSED"}):
            self.tick()
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", self.rec())

    def test_unreadable_pr_under_the_lock_gets_neither_enter_nor_clear(self):
        self.typed_alarm()
        with self.flip_pr_when_the_lock_is_taken(gate.CollectError("gh: down")):
            self.tick()
        self.assertEqual((self.enters, self.clear_keys), ([], []))
        self.assertEqual(self.rec()["pending_enter"], "alarm")  # held; the next tick tries again
        self.tick()  # gh is back, the head is the same: only the Enter
        self.assertEqual(self.enters, [])  # self.pr is still the error: nothing yet
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.ansi = EMPTY_ANSI
        self.tick()
        self.assertEqual(self.enters, ["wv-w1"])

    # ----- r2 P1: the FIRST delivery of an alarm looks at the PR again under the lock too -----
    def test_fresh_alarm_head_moved_while_waiting_for_the_lock_types_nothing(self):
        self.running()
        self.ansi = EMPTY_ANSI
        with self.flip_pr_when_the_lock_is_taken(
                {"number": 7, "headRefOid": "e" * 40, "isDraft": False, "state": "OPEN"}):
            self.tick()
        self.assertEqual((self.alarms(), self.enters), ([], []))
        self.assertNotIn("pending_enter", self.rec())
        self.assertNotIn("alarm_msg", self.rec())  # outdated: dropped

    def test_fresh_alarm_pr_closed_while_waiting_for_the_lock_types_nothing(self):
        self.running()
        self.ansi = EMPTY_ANSI
        with self.flip_pr_when_the_lock_is_taken(
                {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "CLOSED"}):
            self.tick()
        self.assertEqual((self.alarms(), self.enters), ([], []))
        self.assertNotIn("alarm_msg", self.rec())

    def test_fresh_alarm_with_unreadable_pr_under_the_lock_types_nothing_and_retries(self):
        self.running()
        self.ansi = EMPTY_ANSI
        with self.flip_pr_when_the_lock_is_taken(gate.CollectError("gh: down")):
            self.tick()
        self.assertEqual((self.alarms(), self.enters), ([], []))
        self.assertIn("alarm_msg", self.rec())  # the intent stays
        self.assertNotIn("pending_enter", self.rec())
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "OPEN"}
        self.tick()
        self.assertEqual(len(self.alarms()), 1)

    # ----- r2 P2: no input box (a dialog, an empty capture) is not a go for /exit -----
    TRUST = "Do you trust the files in this folder?\n\n❯ 1. Yes, I trust this folder\n  2. No, exit\n"

    def test_exit_is_not_sent_without_an_input_box_and_goes_when_it_appears(self):
        self.running()
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["pending_exit"] = True
        for screen in (self.TRUST, ""):  # a dialog; a failed capture-pane
            self.ansi = screen
            self.assertFalse(wab.close_window(self.cfg, st, st["waves"]["W1"]))
            self.assertFalse(wab.close_window(self.cfg, st, st["waves"]["W1"]))
            self.assertEqual(self.exits(), [])
            self.assertTrue(st["waves"]["W1"].get("pending_exit"))
        self.assertEqual(self.log().count("/exit not sent: no input box"), 1)  # once per episode
        self.ansi = EMPTY_ANSI
        self.assertTrue(wab.close_window(self.cfg, st, st["waves"]["W1"]))
        self.assertEqual(len(self.exits()), 1)

    EXIT_MENU = ("Background tasks are still running:\n  shell · until [ -s /tmp/x ]\n"
                 "  ❯ 1. Exit and stop tasks\n    2. Move to background and exit\n    3. Stay\n"
                 "  Enter to confirm · Esc to cancel\n")

    def test_exit_menu_with_option_1_is_confirmed_by_enter(self):
        self.running()
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["pending_exit"] = True
        self.pane = self.EXIT_MENU
        self.ansi = self.EXIT_MENU  # no input box: the old path held /exit forever
        self.assertTrue(wab.close_window(self.cfg, st, st["waves"]["W1"]))
        self.assertEqual(self.enters, [st["waves"]["W1"]["tmux"]])
        self.assertEqual(self.exits(), [])  # no second /exit typed into the menu
        self.assertIn("/exit menu confirmed", self.log())

    def test_exit_menu_with_another_option_highlighted_is_not_confirmed(self):
        self.running()
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["pending_exit"] = True
        menu = self.EXIT_MENU.replace("❯ 1. Exit", "  1. Exit").replace("    3. Stay", "  ❯ 3. Stay")
        self.pane = self.ansi = menu
        self.assertFalse(wab.close_window(self.cfg, st, st["waves"]["W1"]))
        self.assertEqual(self.enters, [])
        self.assertIn("option 1 not highlighted", self.log())

    def test_exit_menu_quoted_in_the_output_is_not_taken_for_the_dialog(self):
        quoted = ("⏺ The wave noted: «❯ 1. Exit and stop tasks / 2. Move to background and exit / 3. Stay»\n"
                  "  Enter to confirm · Esc to cancel was on screen earlier\n")
        box = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n  ⏵⏵ auto mode on\n"
        for screen in ("⏺ grep found «Exit and stop tasks» in the docs\n", quoted):
            with self.subTest(screen=screen[:30]):
                self.assertIsNone(wab.exit_dialog(screen + box))
                self.assertIsNone(wab.exit_dialog(screen))
        self.assertTrue(wab.exit_dialog(self.EXIT_MENU))

    def test_a_quoted_exit_menu_above_another_dialog_is_not_the_live_menu(self):
        other = ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
                 "  Enter to confirm · Esc to cancel\n")
        self.assertIsNone(wab.exit_dialog(self.EXIT_MENU + other))
        self.assertIsNone(wab.exit_dialog(self.EXIT_MENU + "trailing output line\n"))
        self.assertTrue(wab.exit_dialog("history line\n" + self.EXIT_MENU + "\n\n"))

    def test_close_window_survives_a_clearing_that_raises(self):
        self.done_with_typed_alarm()
        self.clear_works = False

        def boom(name):
            raise subprocess.CalledProcessError(1, ["tmux", "send-keys"])
        with mock.patch.object(wab, "send_clear_keys", side_effect=boom):
            self.tick()
            self.assertEqual(self.exits(), [])
        self.assertTrue(self.rec().get("pending_exit"))
        self.assertIn("/exit not sent", self.log())

    def test_close_window_survives_the_direct_clear_raising(self):
        self.running()
        self.ansi = live("19-folded.ansi")
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["pending_exit"] = True
        with mock.patch.object(wab, "clear_input", side_effect=subprocess.CalledProcessError(1, ["tmux"])):
            self.assertFalse(wab.close_window(self.cfg, st, st["waves"]["W1"]))
        self.assertEqual(self.exits(), [])
        self.assertTrue(st["waves"]["W1"].get("pending_exit"))
        self.assertIn("/exit not sent", self.log())

    # ----- #31 -----
    def test_pending_alarm_on_a_moved_head_is_cleared_not_entered(self):
        self.typed_alarm()
        self.pr = {"number": 7, "headRefOid": "e" * 40, "isDraft": False, "state": "OPEN"}
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "in_progress", "conclusion": None}])
        self.tick()
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", self.rec())

    def test_pending_alarm_of_a_closed_pr_is_cleared_not_entered(self):
        self.typed_alarm()
        self.pr = {"number": 7, "headRefOid": HEAD, "isDraft": False, "state": "MERGED"}
        self.tick()
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", self.rec())

    def test_pending_alarm_on_the_same_head_is_enter_only(self):
        self.typed_alarm()
        self.ansi = EMPTY_ANSI
        self.tick()
        self.assertEqual(self.enters, ["wv-w1"])
        self.assertEqual(self.clear_keys, [])
        self.assertTrue(self.rec()["alarm_msg"]["sent"])

    def test_pending_alarm_with_unreadable_pr_gets_neither_enter_nor_clear(self):
        self.typed_alarm()
        self.pr = gate.CollectError("gh: down")
        self.tick()
        self.assertEqual((self.enters, self.clear_keys), ([], []))
        self.assertEqual(self.rec()["pending_enter"], "alarm")


class InputClearPolicy(Base):
    """#50, #53: a policy answer sitting in the input line when its episode cannot go on."""
    POLICY = DecisionPolicy.POLICY
    ASK = DecisionPolicy.ASK
    mandate = DecisionPolicy.mandate
    tick = DecisionPolicy.tick
    answers = DecisionPolicy.answers
    log = DecisionPolicy.log
    owner_asked = DecisionPolicy.owner_asked

    def pend(self, cfg, **over):
        st = wab.load_state(cfg)
        st["waves"]["W1"].update(pending_enter="policy answer", pending_text_head="[wab] РЕШЕНИЕ",
                                 policy_pending=self.ASK, **over)
        self.put_state(cfg, st)
        self.ansi = live("19-folded.ansi")

    def test_lowered_cap_clears_the_pending_answer(self):  # #50
        cfg, _ = self.mandate(max_auto_answers=1)
        self.pend(cfg, auto_answers=1)
        st = self.tick(cfg, self.ASK)
        w = st["waves"]["W1"]
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", w)
        self.assertNotIn("policy_pending", w)
        self.assertEqual(wab.auto_answers_used(w), 2)  # it may have gone: charged
        self.assertIn("input cleared: policy answer", self.log(cfg))
        self.assertIn("policy cap reached", self.log(cfg))

    def test_precheck_runs_on_the_enter_only_retry(self):  # #53
        cfg, _ = self.mandate()
        self.pend(cfg)
        real_lock = wab._InputLock.__enter__

        def enter_then_status_moves(lock):  # the wave moved on while this writer waited for the lock
            got = real_lock(lock)
            self.set_status(cfg, "W1", "RUNNING")
            return got
        with mock.patch.object(wab._InputLock, "__enter__", enter_then_status_moves):
            self.tick(cfg, self.ASK)
        w = wab.load_state(cfg)["waves"]["W1"]
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", w)
        self.assertNotIn("policy_pending", w)
        self.assertEqual(wab.auto_answers_used(w), 1)  # charged to the cap

    def test_status_change_before_the_retry_clears_the_input(self):
        cfg, _ = self.mandate()
        self.pend(cfg)
        st = self.tick(cfg, "RUNNING")
        self.assertEqual(len(self.clear_keys), 1)
        self.assertEqual(wab.auto_answers_used(st["waves"]["W1"]), 1)
        self.assertNotIn("pending_enter", st["waves"]["W1"])

    # ----- #51: `say` marks the episode it answered -----
    def say_file(self, text="ответ владельца"):
        f = self.tmp / "owner.md"
        f.write_text(text, encoding="utf-8")
        return f

    def marker(self, cfg):
        return cfg["run_dir"] / "W1" / "owner-answered"

    def test_say_holding_the_lock_makes_the_policy_stay_silent(self):
        import threading
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        typed, release = threading.Event(), threading.Event()

        def send(name, text, on_typed=None, **kw):
            if text.startswith("[wab] РЕШЕНИЕ"):
                self.sent.append(("text", name, text))
                return
            if on_typed:
                on_typed()  # the owner's text is in the window
            typed.set()
            release.wait(10)
            self.sent.append(("text", name, "owner"))
        wab.send_text.side_effect = send
        res = {}
        t_say = threading.Thread(target=lambda: res.update(say=wab.say_cmd(cfg, "W1", self.say_file())))
        t_say.start()
        self.assertTrue(typed.wait(10))
        t_pol = threading.Thread(target=lambda: self.tick(cfg))
        t_pol.start()
        threading.Event().wait(0.3)  # the policy tick now waits for the input lock
        release.set()
        t_say.join(10)
        t_pol.join(10)
        self.assertTrue(res["say"])
        self.assertEqual(self.answers(), [], self.sent)  # no second answer on top of the owner's
        st = wab.load_state(cfg)
        self.assertEqual(wab.auto_answers_used(st["waves"]["W1"]), 0)  # the cap is not spent
        self.assertIn("the owner answered this episode (say)", self.log(cfg))
        for _ in range(3):
            self.tick(cfg)
        self.assertEqual(self.log(cfg).count("the owner answered this episode"), 1)  # once per episode
        self.assertEqual(self.owner_asked(), [])  # the owner has answered: no BLOCKED notice

    def test_marker_of_another_stamp_is_a_new_episode(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        wab.write_owner_answered(cfg, "W1")
        path = cfg["run_dir"] / "W1" / "status"
        st0 = os.stat(path)
        path.write_text(self.ASK, encoding="utf-8")  # the same line, but the file was rewritten
        os.utime(path, ns=(st0.st_atime_ns, st0.st_mtime_ns + 5_000_000))
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 1)

    def test_marker_of_the_same_episode_blocks_the_answer(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        wab.write_owner_answered(cfg, "W1")
        self.tick(cfg)
        self.assertEqual(self.answers(), [])

    def test_status_rewritten_between_the_reads_gives_a_marker_of_no_episode(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        path = cfg["run_dir"] / "W1" / "status"
        real_read = wab.read

        def read_then_rewrite(p, *a, **kw):
            text = real_read(p, *a, **kw)
            if Path(p) == path:  # the wave rewrites the line right after the text was read
                st0 = os.stat(path)
                path.write_text(self.ASK + " (new)", encoding="utf-8")
                os.utime(path, ns=(st0.st_atime_ns, st0.st_mtime_ns + 7_000_000))
            return text
        with mock.patch.object(wab, "read", read_then_rewrite):
            wab.write_owner_answered(cfg, "W1")
        self.assertFalse(wab.owner_answered(cfg, "W1", self.ASK))
        self.assertFalse(wab.owner_answered(cfg, "W1", self.ASK + " (new)"))
        self.assertIn("rewritten while the marker was taken", self.log(cfg))
        self.tick(cfg)
        self.assertEqual(len(self.answers()), 1)  # the new episode is answered by the policy

    def test_a_broken_marker_is_no_marker(self):
        cfg, _ = self.mandate(max_auto_answers=20)
        self.set_status(cfg, "W1", self.ASK)
        for junk in ("{not json", "[]", "", '{"status": 1}', '{"status": "x", "stamp": "y"}'):
            self.marker(cfg).write_text(junk, encoding="utf-8")
            self.sent.clear()
            self.tick(cfg, "RUNNING")
            self.tick(cfg, self.ASK)
            self.assertEqual(len(self.answers()), 1, junk)
        self.marker(cfg).unlink()
        self.marker(cfg).mkdir()  # unreadable as a file
        self.sent.clear()
        self.tick(cfg, "RUNNING")
        self.tick(cfg, self.ASK)
        self.assertEqual(len(self.answers()), 1)

    def test_pending_answer_is_cleared_when_the_owner_answered(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        self.pend(cfg)
        wab.write_owner_answered(cfg, "W1")
        st = self.tick(cfg)
        w = st["waves"]["W1"]
        self.assertEqual(self.enters, [])
        self.assertEqual(len(self.clear_keys), 1)
        self.assertNotIn("pending_enter", w)
        self.assertNotIn("policy_pending", w)
        self.assertEqual(wab.auto_answers_used(w), 1)  # it may have gone: charged

    def test_say_does_not_write_the_state_and_writes_the_marker(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        sp = wab.state_path(cfg)
        before = (sp.read_bytes(), os.stat(sp).st_mtime_ns)
        wab.send_text.side_effect = lambda n, t, on_typed=None, **kw: on_typed and on_typed()
        self.assertTrue(wab.say_cmd(cfg, "W1", self.say_file()))
        self.assertEqual((sp.read_bytes(), os.stat(sp).st_mtime_ns), before)
        doc = json.loads(self.marker(cfg).read_text(encoding="utf-8"))
        self.assertEqual(doc["status"], self.ASK)
        self.assertEqual(doc["stamp"], wab._status_stamp(cfg, "W1"))
        self.assertTrue(doc["at"].endswith("Z"))

    def test_marker_is_written_even_if_enter_fails(self):
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)

        def typed_then_fail(name, text, on_typed=None, **kw):
            on_typed()
            raise wab.NotSubmitted("the text stays in the input")
        wab.send_text.side_effect = typed_then_fail
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(wab.say_cmd(cfg, "W1", self.say_file()))
        self.assertTrue(wab.owner_answered(cfg, "W1", self.ASK))

    def test_state_saves_and_marker_writes_do_not_lose_each_other(self):
        import threading
        cfg, _ = self.mandate()
        self.set_status(cfg, "W1", self.ASK)
        N = 150
        errors = []

        def watch():
            try:
                st = wab.load_state(cfg)
                for i in range(N):
                    st["waves"]["W1"]["counter"] = i
                    wab.save_state(cfg, st)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def say():
            try:
                for i in range(N):
                    (cfg["run_dir"] / "W1" / "status").write_text(f"BLOCKED: {i}", encoding="utf-8")
                    wab.write_owner_answered(cfg, "W1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=watch), threading.Thread(target=say)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(wab.load_state(cfg)["waves"]["W1"]["counter"], N - 1)
        doc = json.loads(self.marker(cfg).read_text(encoding="utf-8"))
        self.assertEqual(doc["status"], f"BLOCKED: {N - 1}")
        self.assertEqual([p.name for p in (cfg["run_dir"] / "W1").glob(".owner-answered.*")], [])


# ---------------------------------------------------------------- 0.14.0 (W3): #55, #34, #44, #32
def _attention_exit(cfg):
    with contextlib.redirect_stdout(io.StringIO()):
        return wab.attention_cmd(cfg)


class W3DoneAttention(GateBase):
    """#55: «волна X завершена» is information: it opens no ATTENTION."""

    def test_auto_merge_and_autolaunch_leave_no_attention(self):
        cfg = self.start(telegram=None)
        self.assertTrue(self.tick())  # the gate passes, the merge is requested
        self.view = {"state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "headRefOid": HEAD, "baseRefName": "main"}
        self.assertTrue(self.tick())  # merged: «волна W1 завершена», W2 is launched by the dispatcher
        self.launch.assert_called_once()
        self.assertTrue(self.launch.call_args[1]["by_dispatcher"])
        self.assertIn("notify(skipped): demo: волна W1 завершена", self.log())
        self.assertEqual(_attention_exit(cfg), 0)
        self.assertFalse((cfg["run_dir"] / "ATTENTION").exists())
        self.assertNotIn("done", self.rec().get("attention") or {})

    def test_done_is_an_info_notice(self):
        self.assertIn("done", wab.INFO_NOTICES)


class W3NextLaunchClosesDone(Base):
    """#55 (b): the dispatcher's launch of the next wave closes the «done» signal of the previous one
    (left, for example, by the state of an older version)."""

    def test_a_done_signal_left_by_an_older_version_is_closed_by_the_next_launch(self):
        cfg, _ = self.chain(telegram=None, merge_gate="auto")
        att = {"done": {"at": time.time(), "line": "wave-autobot: волна W1 завершена."}}
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec("W1", phase="done", attention=att, notified={"done": "1"})}})
        self.set_status(cfg, "W1", "DONE")
        wab.save_state(cfg, wab.load_state(cfg))
        self.assertEqual(_attention_exit(cfg), 1)  # the premise: the old state holds the signal
        prompt = self.tmp / "p.md"
        prompt.write_text("go\n", encoding="utf-8")
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            wab.launch(cfg, "W2", prompt, by_dispatcher=True)
        st = self.get_state(cfg)
        self.assertEqual(st["current"], "W2")
        self.assertNotIn("done", st["waves"]["W1"].get("attention") or {})
        self.assertEqual(_attention_exit(cfg), 0)
        self.assertEqual(st["waves"]["W1"].get("notified", {}).get("done"), "1")  # the dedup mark stays

    def test_a_coordinator_launch_still_acknowledges(self):
        cfg, _ = self.chain(telegram=None, merge_gate="auto")
        self.put_state(cfg, {"current": "W1", "waves": {
            "W1": self.wave_rec("W1", phase="done", attention={"idle": {"at": time.time(), "line": "x"}})}})
        wab.save_state(cfg, wab.load_state(cfg))
        prompt = self.tmp / "p.md"
        prompt.write_text("go\n", encoding="utf-8")
        self.alive = False
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            wab.launch(cfg, "W2", prompt)
        self.assertEqual(_attention_exit(cfg), 0)


class W3PrNumber(GateBase):
    """#34: the PR number is kept from the hand-off on, whatever the gate mode or verdict."""

    def test_no_gate_done_before_the_alarm_keeps_the_pr_in_chain_result_and_dashboard(self):
        cfg = self.start(merge_gate=None, waves=["W1"])
        self.assertNotIn("pr", self.rec())
        self.assertFalse(self.tick())  # the last wave without a gate: completed at once, the chain ends
        self.assertEqual(self.find_calls, 1)
        self.assertEqual(self.rec().get("pr"), 7)
        text = (cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")
        self.assertIn("PR: [#7](https://github.com/o/r/pull/7)", text)
        self.assertNotIn("PR: нет", text)
        w = self.rec()
        self.assertEqual(wab._wave_pr(w), 7)
        try:
            import dash
        except ImportError:
            return
        self.assertEqual(dash.wave_pr(w), 7)

    def test_no_gate_handoff_keeps_the_pr(self):
        self.start(merge_gate=None)
        self.assertFalse(self.tick())  # W1 of two: handed to the coordinator
        rec = self.rec()
        self.assertEqual(rec["phase"], "awaiting_merge")
        self.assertEqual(rec.get("pr"), 7)
        self.assertEqual(self.find_calls, 1)

    def retake_done_during_lookup(self, new_status="RUNNING"):
        """find_pr (a slow gh call) during which the wave takes its DONE back."""
        real = self.pr

        def slow(cfg, cwd):
            self.find_calls += 1
            self.set_status(self.cfg, "W1", new_status)
            return real
        p = mock.patch.object(wab, "find_pr", side_effect=slow)
        p.start()
        self.addCleanup(p.stop)

    def test_no_gate_handoff_is_not_made_when_done_is_taken_back_during_the_lookup(self):
        for new in ("RUNNING", "BLOCKED: передумала"):
            with self.subTest(status=new):
                self.tg.clear()
                self.start(merge_gate=None)
                self.retake_done_during_lookup(new)
                self.assertTrue(self.tick())
                rec = self.rec()
                self.assertNotEqual(rec["phase"], "awaiting_merge")
                self.assertFalse(rec.get("pending_exit"))
                self.assertNotIn("handoff", rec.get("notified", {}))
                self.assertFalse(any("сдала PR" in t for t in self.tg), self.tg)

    def test_last_wave_without_a_gate_is_not_completed_when_done_is_taken_back(self):
        cfg = self.start(merge_gate=None, waves=["W1"])
        self.retake_done_during_lookup()
        self.assertTrue(self.tick())
        rec = self.rec()
        self.assertEqual(rec["phase"], "running")
        self.assertIsNotNone(self.get_state(cfg)["current"])
        self.assertNotIn("done", rec.get("notified", {}))
        self.assertFalse(rec.get("pending_exit"))
        self.assertFalse((cfg["run_dir"] / "chain-result.md").exists())
        self.assertFalse(any("завершена" in t for t in self.tg), self.tg)
        # its next, real DONE completes the chain as usual (fresh: the mark was not spent)
        self.set_status(cfg, "W1", "DONE")
        with mock.patch.object(wab, "find_pr", return_value=self.pr):
            self.assertFalse(self.tick())
        self.assertIsNone(self.get_state(cfg)["current"])
        self.assertEqual(self.rec()["phase"], "done")
        self.assertTrue(any("завершена" in t for t in self.tg), self.tg)
        self.assertTrue((cfg["run_dir"] / "chain-result.md").exists())

    def test_a_lookup_that_leaves_the_status_alone_hands_over_as_before(self):
        self.start(merge_gate=None)
        self.assertFalse(self.tick())
        rec = self.rec()
        self.assertEqual(rec["phase"], "awaiting_merge")
        self.assertEqual(rec.get("pr"), 7)
        self.assertTrue(any("сдала PR" in t for t in self.tg), self.tg)

    def test_a_failing_find_pr_never_stops_the_handoff(self):
        self.start(merge_gate=None)
        self.pr = gate.CollectError("gh: boom")
        self.assertFalse(self.tick())
        rec = self.rec()
        self.assertEqual(rec["phase"], "awaiting_merge")
        self.assertNotIn("pr", rec)

    def test_an_already_known_pr_is_not_looked_up_again(self):
        self.start(merge_gate=None, waves=["W1"])
        st = self.get_state(self.cfg)
        st["waves"]["W1"]["pr"] = 31
        self.put_state(self.cfg, st)
        self.assertFalse(self.tick())
        self.assertEqual(self.find_calls, 0)
        self.assertEqual(self.rec()["pr"], 31)

    def test_external_wait_verdict_still_saves_the_number(self):
        cfg = self.start(merge_gate="external", waves=["W1"])
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "in_progress", "conclusion": None}])
        self.assertFalse(self.tick())
        self.assertEqual(self.rec()["phase"], "awaiting_merge")
        self.assertEqual(self.rec().get("pr"), 7)
        self.assertTrue(any("Гейт мерджа ждёт" in t for t in self.tg), self.tg)
        self.alive = False  # the window of the handed-over wave is closed
        wab.main(["wab.py", "done", str(self.path)])
        text = (cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")
        self.assertIn("PR: [#7](https://github.com/o/r/pull/7)", text)

    def test_external_fail_verdict_saves_the_number(self):
        self.start(merge_gate="external")
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}])
        self.assertFalse(self.tick())
        self.assertEqual(self.rec().get("pr"), 7)

    def test_auto_gate_wait_saves_the_number(self):
        self.start()
        self.facts = green_facts(check_runs=[{"name": "ci", "status": "in_progress", "conclusion": None}])
        self.assertTrue(self.tick())
        self.assertEqual(self.rec()["phase"], "gate")
        self.assertEqual(self.rec().get("pr"), 7)

    def test_external_handoff_keeps_the_pr_when_collecting_the_facts_fails(self):
        cfg = self.start(merge_gate="external", waves=["W1"])
        for exc in (gate.CollectError("gh: boom"), RuntimeError("bad facts")):
            with self.subTest(exc=type(exc).__name__):
                st = self.get_state(cfg)
                st["waves"]["W1"] = self.wave_rec(phase="running")
                self.put_state(cfg, st)
                self.set_status(cfg, "W1", "DONE")
                v = None
                with mock.patch.object(wab, "gate_facts", side_effect=exc):
                    v = wab.gate_check(cfg, "W1", self.rec())
                    self.assertEqual(v["verdict"], "wait")  # never a pass
                    self.assertEqual(v["number"], 7)
                    self.assertFalse(self.tick())
                self.assertEqual(self.rec()["phase"], "awaiting_merge")
                self.assertEqual(self.rec().get("pr"), 7)
        self.alive = False
        wab.main(["wab.py", "done", str(self.path)])
        text = (cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")
        self.assertIn("PR: [#7](https://github.com/o/r/pull/7)", text)
        self.assertNotIn("PR: нет", text)

    def test_a_long_multiline_result_does_not_push_the_trusted_tail_out(self):
        cfg = self.start(merge_gate="external")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("\n".join(f"п{i}" for i in range(400)) + "\n",
                                                          encoding="utf-8")
        sha = "7" * 40
        verdict = {"verdict": "pass", "reasons": [], "number": 7, "head": sha, "unresolved": [],
                   "old_p01": [], "draft": False}
        with mock.patch.object(wab, "gate_check", return_value=verdict):
            self.assertFalse(self.tick())
        [msg] = [t for t in self.tg if "сдала PR" in t]
        script = "~/.cache/wab/" + wab.owner_script_name(cfg, "W1", sha)
        self.assertIn(script, msg)
        self.assertIn("Гейт мерджа пройден", msg)
        self.assertIn("за координатором", msg)
        self.assertLessEqual(len(msg), wab.TG_MESSAGE_LIMIT)
        self.assertIn("…", msg)  # the quote was cut, not the dispatcher's own words
        self.assertLessEqual(len(wab.render_notice(wab.quote("а\n" * 2000), 10 ** 9)), wab.TG_LIMIT + 40)


def _frozen_events(cfg):
    p = cfg["run_dir"] / "events.log"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return [l for l in text.splitlines() if "контекст не меряется" in l]


class W3FrozenContext(Base):
    """#44: the bound journal does not move, another journal of the same working copy (owned by no
    other wave) grows: the session was not rebound after /clear. One event per episode."""

    def setUp(self):
        super().setUp()
        self.cfg, self.path = self.chain()
        self.bound = self.transcript("s1", [asst(inp=1000, out=10)])
        self.neighbor = self.bound.parent / "zz.jsonl"
        self.neighbor.write_text(asst(inp=5) + "\n", encoding="utf-8")
        self.pane = "a\nb\nc\n"
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            sessions=["s1"], pane_digest=wab.pane_digest(self.pane), pane_changed=time.time())}})
        self.set_status(self.cfg, "W1", "RUNNING")

    MAX_TICKS = 40  # a mutated CTX_FROZEN_TICKS (10**9) must fail an assert, not hang the run

    def tick(self, n=1, grow=None):
        for _ in range(min(n, self.MAX_TICKS)):
            if grow is not None:
                with open(grow, "a", encoding="utf-8") as f:
                    f.write(asst(inp=7) + "\n")
            wab.tick(self.cfg, wab.load_state(self.cfg))

    def rec(self):
        return self.get_state(self.cfg)["waves"]["W1"]

    def test_one_event_per_episode_and_the_mark_follows_it(self):
        self.tick()  # the first measurement only seeds the watch
        self.tick(wab.CTX_FROZEN_TICKS - 1, grow=self.neighbor)
        self.assertEqual(_frozen_events(self.cfg), [])  # not long enough yet
        self.tick(1, grow=self.neighbor)
        events = _frozen_events(self.cfg)
        self.assertEqual(len(events), 1, events)
        self.assertIn("zz", events[0])
        self.assertIn("s1", events[0])
        self.assertTrue(self.rec().get("ctx_frozen"))
        self.tick(4, grow=self.neighbor)  # the same episode goes on: nothing new
        self.assertEqual(len(_frozen_events(self.cfg)), 1)
        self.assertTrue(self.rec().get("ctx_frozen"))
        self.assertEqual(self.tg, [])  # an event and the dashboard only: no notice
        # the bound journal grows: the episode is over, the mark is gone
        self.tick(1, grow=self.bound)
        self.assertFalse(self.rec().get("ctx_frozen"))
        self.tick(1)
        self.assertEqual(len(_frozen_events(self.cfg)), 1)
        # a new episode: one more event
        self.tick(wab.CTX_FROZEN_TICKS + 1, grow=self.neighbor)
        self.assertEqual(len(_frozen_events(self.cfg)), 2)
        self.assertTrue(self.rec().get("ctx_frozen"))

    def test_the_threshold_is_exact(self):
        self.tick()  # seeds the watch (ticks = 0)
        self.tick(wab.CTX_FROZEN_TICKS - 1, grow=self.neighbor)
        self.assertEqual(_frozen_events(self.cfg), [])
        self.assertFalse(self.rec().get("ctx_frozen"))
        self.tick(1, grow=self.neighbor)  # the CTX_FROZEN_TICKS-th unchanged tick
        self.assertEqual(len(_frozen_events(self.cfg)), 1)
        self.assertTrue(self.rec().get("ctx_frozen"))

    def test_a_quiet_neighbor_is_no_event(self):
        self.tick(wab.CTX_FROZEN_TICKS + 5)
        self.assertEqual(_frozen_events(self.cfg), [])
        self.assertFalse(self.rec().get("ctx_frozen"))

    def test_a_new_journal_appearing_counts_as_growth(self):
        self.tick()
        self.tick(wab.CTX_FROZEN_TICKS)
        (self.bound.parent / "fresh.jsonl").write_text(asst(inp=3) + "\n", encoding="utf-8")
        self.tick(1)
        self.assertEqual(len(_frozen_events(self.cfg)), 1)

    def test_growth_of_a_journal_owned_by_another_wave_is_no_event(self):
        theirs = self.transcript("w2s", [asst(inp=9)])
        old = self.transcript("w2old", [asst(inp=9)])
        st = self.get_state(self.cfg)
        st["waves"]["W2"] = self.wave_rec("W2", phase="done", sessions=["w2s"],
                                          attempts=[self.wave_rec("W2", sessions=["w2old"])])
        self.put_state(self.cfg, st)
        self.neighbor.unlink()
        self.tick()
        for _ in range(min(wab.CTX_FROZEN_TICKS + 4, self.MAX_TICKS)):
            for p in (theirs, old):
                with open(p, "a", encoding="utf-8") as f:
                    f.write(asst(inp=9) + "\n")
            self.tick()
        self.assertEqual(_frozen_events(self.cfg), [])
        self.assertFalse(self.rec().get("ctx_frozen"))

    def await_rebind(self):
        """/clear was sent, the marker of the new session is not found (yet): await_session stays true."""
        st = self.get_state(self.cfg)
        st["waves"]["W1"]["await_session"] = True
        st["waves"]["W1"]["tokens"] = 1000
        self.put_state(self.cfg, st)

    def test_the_watch_works_while_the_rebinding_is_awaited(self):
        self.await_rebind()
        self.tick()  # seeds the watch
        self.tick(wab.CTX_FROZEN_TICKS - 1, grow=self.neighbor)
        self.assertEqual(_frozen_events(self.cfg), [])
        self.tick(1, grow=self.neighbor)
        events = _frozen_events(self.cfg)
        self.assertEqual(len(events), 1, events)
        self.assertTrue(self.rec().get("await_session"))  # still unbound
        self.assertTrue(self.rec().get("ctx_frozen"))
        self.tick(4, grow=self.neighbor)  # the same episode: nothing new
        self.assertEqual(len(_frozen_events(self.cfg)), 1)
        # the new session is bound by its marker: the episode is over, the mark is gone
        self.transcript("s2", [asst(inp=50)], marker=wab.session_marker(self.cfg, "W1"))
        self.tick(1)
        rec = self.rec()
        self.assertFalse(rec.get("await_session"))
        self.assertEqual(rec["sessions"][-1], "s2")
        self.assertFalse(rec.get("ctx_frozen"))
        self.assertEqual(len(_frozen_events(self.cfg)), 1)

    def test_a_journal_of_another_wave_while_awaiting_is_no_event(self):
        self.await_rebind()
        theirs = self.transcript("w2s", [asst(inp=9)])
        st = self.get_state(self.cfg)
        st["waves"]["W2"] = self.wave_rec("W2", phase="done", sessions=["w2s"])
        self.put_state(self.cfg, st)
        self.neighbor.unlink()
        self.tick()
        for _ in range(min(wab.CTX_FROZEN_TICKS + 4, self.MAX_TICKS)):
            with open(theirs, "a", encoding="utf-8") as f:
                f.write(asst(inp=9) + "\n")
            self.tick()
        self.assertEqual(_frozen_events(self.cfg), [])
        self.assertFalse(self.rec().get("ctx_frozen"))

    def test_an_earlier_attempt_of_this_wave_is_not_the_live_journal(self):
        """Intended: a journal of an earlier try (attempts) of THIS wave is excluded like another wave's,
        so its growth is no signal; the bound journal is never excluded."""
        old = self.transcript("old1", [asst(inp=9)])
        st = self.get_state(self.cfg)
        st["waves"]["W1"]["attempts"] = [self.wave_rec("W1", sessions=["old1"])]
        self.put_state(self.cfg, st)
        self.neighbor.unlink()
        self.tick()
        for _ in range(min(wab.CTX_FROZEN_TICKS + 4, self.MAX_TICKS)):
            with open(old, "a", encoding="utf-8") as f:
                f.write(asst(inp=9) + "\n")
            self.tick()
        self.assertEqual(_frozen_events(self.cfg), [])
        self.assertFalse(self.rec().get("ctx_frozen"))
        # a journal that belongs to no one still counts at the same time
        self.neighbor.write_text(asst(inp=5) + "\n", encoding="utf-8")
        self.tick()
        self.tick(wab.CTX_FROZEN_TICKS + 1, grow=self.neighbor)
        self.assertEqual(len(_frozen_events(self.cfg)), 1)

    def test_a_stat_failure_never_stops_the_tick(self):
        self.tick()
        real = Path.stat

        def broken(p, *a, **kw):
            if p.suffix == ".jsonl":
                raise OSError("gone")
            return real(p, *a, **kw)
        with mock.patch.object(Path, "stat", broken):
            self.tick(3)
        self.assertEqual(_frozen_events(self.cfg), [])

    def test_dashboard_shows_the_mark(self):
        try:
            import dash
            from rich.console import Console
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        st = self.get_state(self.cfg)
        st["waves"]["W1"]["ctx_frozen"] = True
        self.put_state(self.cfg, st)

        def render():
            buf = io.StringIO()
            Console(file=buf, width=220, force_terminal=False).print(dash.safe_render(self.cfg))
            return buf.getvalue()
        self.assertEqual(render().count("не меряется"), 2)  # the current panel and the table
        st["waves"]["W1"]["ctx_frozen"] = False
        self.put_state(self.cfg, st)
        self.assertNotIn("не меряется", render())


def unicodedata_category(ch):
    import unicodedata
    return unicodedata.category(ch)


class W3Quote(Base):
    """#32: the wave's own text is a quote: it passes redact WITHOUT the owner-script exemption and
    is marked as a quote, whichever way the message goes out."""
    SECRET = "Zx9Qw7Er5Ty3Ui1Op8As6Df4Gh2Jk0Lm9Nb7Vc5Xz3Wq"
    POLICY = [{"class": "needs_decision"}]
    ASK = "BLOCKED: [class=needs_decision rec=invariant red=no] needs_decision T3: {}; варианты: invariant | cut_surface"

    def setUp(self):
        super().setUp()
        self.shown = []
        for name, target in (("display_all", lambda text: self.shown.append(text)),
                             ("drop_stale_btab", None)):
            p = mock.patch.object(wab, name, side_effect=target) if target else mock.patch.object(wab, name)
            p.start()
            self.addCleanup(p.stop)
        folder = self.home / ".cache" / "wab"
        folder.mkdir(parents=True)
        self.script = f"{self.SECRET}.{'a' * 12}.{'b' * 16}.merge"
        (folder / self.script).write_text("#!/bin/sh\n", encoding="utf-8")
        self.hostile = f"см. ~/.cache/wab/{self.script} и ключ {self.SECRET}"
        self.n = 0

    def chain_for(self, telegram, **over):
        self.n += 1
        over.setdefault("chain", f"c{self.n}")
        if not telegram:
            over["telegram"] = None
        return self.chain(**over)

    def put(self, cfg, **rec):
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", **rec)}})

    def outputs(self, cfg):
        att = cfg["run_dir"] / "ATTENTION"
        return list(self.tg) + list(self.shown) + [att.read_text(encoding="utf-8") if att.exists() else ""]

    # ----- the scenarios: each puts the hostile text into one notice -----
    def blocked(self, cfg):
        self.put(cfg)
        self.set_status(cfg, "W1", f"BLOCKED: вопрос {self.hostile}")
        wab.tick(cfg, wab.load_state(cfg))

    def idle(self, cfg):
        self.pane = "a\nb\nc\n"
        self.put(cfg, pane_digest=wab.pane_digest(self.pane), pane_changed=time.time() - 99999)
        self.set_status(cfg, "W1", f"RUNNING {self.hostile}")
        wab.tick(cfg, wab.load_state(cfg))

    def dead(self, cfg):
        self.put(cfg)
        self.set_status(cfg, "W1", f"RUNNING {self.hostile}")
        self.alive = False
        wab.tick(cfg, wab.load_state(cfg))

    def done(self, cfg):
        self.put(cfg)
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text(f"Итог: {self.hostile}\n", encoding="utf-8")
        wab.tick(cfg, wab.load_state(cfg))

    def handoff(self, cfg):
        self.done(cfg)

    def policy(self, cfg):
        self.put(cfg)
        self.set_status(cfg, "W1", self.ASK.format(self.hostile))
        wab.tick(cfg, wab.load_state(cfg))

    SCENARIOS = {  # name -> (chain overrides, a fragment that proves the notice went out)
        "blocked": ({}, "ждёт тебя"),
        "idle": ({}, "молчит"),
        "dead": ({}, "закрылось"),
        "done": ({"waves": ["W1"]}, "завершена"),
        "handoff": ({"merge_gate": "external"}, "сдала PR"),
        "policy": ({"decision_policy": POLICY}, "развилка закрыта"),
    }

    def run_scenario(self, name, telegram):
        over, _ = self.SCENARIOS[name]
        self.alive = True
        body = b"Run\n\n## Policy\n- auto: class=plan_mismatch\n"
        if name == "policy":
            over = dict(over, mandate_sha256=hashlib.sha256(body).hexdigest())
        cfg, _ = self.chain_for(telegram, **over)
        if name == "policy":
            (cfg["run_dir"] / "mandate.md").write_bytes(body)
        if name == "handoff":
            (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        getattr(self, name)(cfg)
        return cfg

    def test_the_secret_in_a_wave_text_reaches_no_channel(self):
        for name, (_, proof) in self.SCENARIOS.items():
            for telegram in (True, False):
                with self.subTest(scenario=name, telegram=telegram):
                    self.tg.clear()
                    self.shown.clear()
                    cfg = self.run_scenario(name, telegram)
                    if telegram:
                        self.assertTrue(any(proof in t for t in self.tg), (proof, self.tg))
                    else:
                        self.assertTrue(any(proof in t for t in self.shown), (proof, self.shown))
                    for out in self.outputs(cfg):
                        self.assertNotIn(self.SECRET, out)
                        self.assertNotIn("\x02", out)
                        self.assertNotIn("\x03", out)
                    if telegram:
                        self.assertTrue(any("цитата" in t.lower() for t in self.tg), self.tg)

    # ----- #56: every notice and display-message is signed with the chain name -----
    def test_every_notice_starts_with_the_chain_name(self):
        for name, (_, proof) in self.SCENARIOS.items():
            for telegram in (True, False):
                with self.subTest(scenario=name, telegram=telegram):
                    self.tg.clear()
                    self.shown.clear()
                    cfg = self.run_scenario(name, telegram)
                    chain = cfg["chain"]
                    sent = self.tg if telegram else self.shown
                    self.assertTrue(sent, (name, telegram))
                    for text in sent:
                        self.assertTrue(text.startswith(chain), text)
                        self.assertNotIn("wave-autobot", text)
                    if not telegram:
                        path = cfg["run_dir"] / "ATTENTION"
                        att = path.read_text(encoding="utf-8") if path.exists() else ""
                        if "signal:" in att:
                            self.assertRegex(att, r"signal: " + re.escape(chain))

    def test_notify_signs_unsigned_and_legacy_outbox_texts_once(self):
        cfg, _ = self.chain_for(True)
        chain = cfg["chain"]
        wab.notify(cfg, "просто текст")
        wab.notify(cfg, "wave-autobot: старая запись outbox")
        wab.notify(cfg, f"{chain}: уже подписано")
        self.assertEqual(self.tg[-3:], [f"{chain}: просто текст", f"{chain}: старая запись outbox",
                                        f"{chain}: уже подписано"])

    def test_a_hostile_chain_name_cannot_break_the_signature(self):
        cfg, _ = self.chain_for(True)
        cfg["chain"] = "x\n#(touch /tmp/p)\x02y"
        signed = wab.sign(cfg, "тело")
        self.assertEqual(len(signed.splitlines()), 1)
        self.assertNotIn("\x02", signed)
        self.assertTrue(signed.endswith(": тело"))

    def test_no_notice_text_carries_a_hardcoded_wave_autobot_prefix(self):
        import ast
        tree = ast.parse((WAVES / "wab.py").read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and n.body and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docstrings and "wave-autobot: " in n.value]
        # the single legacy marker (an old outbox entry) is allowed: _LEGACY_SIGN
        legacy = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_LEGACY_SIGN" for t in n.targets)]
        self.assertEqual([l for l in bad if l not in legacy], [])

    # ----- #67: the owner-facing BLOCKED notice carries the decision, not only a quote -----
    LABELLED = ("BLOCKED: [class=blocked_cap rec=owner red=no] {question}; Варианты: A поднять кап | B принять как есть")

    def blocked_text(self, status, telegram=True):
        cfg, _ = self.chain_for(telegram)
        self.put(cfg)
        self.set_status(cfg, "W1", status)
        self.tg.clear()
        self.shown.clear()
        wab.tick(cfg, wab.load_state(cfg))
        return cfg, (self.tg if telegram else self.shown)

    def test_blocked_notice_carries_class_red_question_options_and_where_to_answer(self):
        question = "кап 2/2 на задаче T3, ключ ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8 и token=hunter2hunter2"
        cfg, sent = self.blocked_text(self.LABELLED.format(question=question))
        [msg] = [t for t in sent if "ждёт тебя" in t]
        for part in ("blocked_cap", "красная зона: нет", "рекомендация волны: owner", "кап 2/2 на задаче T3", "A поднять кап",
                     "B принять как есть", "attach -t", " say "):
            self.assertIn(part, msg)
        self.assertNotIn("a1B2c3D4e5F6g7H8", msg)
        self.assertNotIn("hunter2hunter2", msg)
        self.assertLessEqual(len(msg), wab.TG_MESSAGE_LIMIT)
        self.assertTrue(msg.startswith(cfg["chain"]))

    def test_blocked_notice_masks_a_secret_that_contains_the_options_separator(self):  # Astra r2
        # the split into question/options must not cut a secret away from its key before redact()
        for value in ("variants:hunter2hunter2", "Варианты:hunter2hunter2", "token=Варианты:hunter2hunter2"):
            with self.subTest(value=value):
                status = f"BLOCKED: [class=question rec=A red=yes] password={value}"
                _cfg, sent = self.blocked_text(status)
                [msg] = [t for t in sent if "ждёт тебя" in t]
                self.assertNotIn("hunter2hunter2", msg)
                self.assertIn("рекомендация волны: A", msg)

    # one character of every invisible class _INVISIBLE covers (W3 invariant): Cc, C1, Cf (zero width,
    # bidi, soft hyphen, word joiner, BOM), Zl, Zp, variation selector, CGJ, Hangul filler, unassigned
    # default-ignorable
    INVISIBLES = ("\x01", "\x85", "\u200b", "\u200e", "\u00ad", "\u2060", "\ufeff", "\u2028", "\u2029",
                  "\ufe0f", "\u034f", "\u3164", "\u2065")

    def test_blocked_notice_masks_a_key_glued_by_an_invisible_character(self):  # Astra r3 (high)
        # an invisible character inside the key hides `password=` from redact(); the separator inside the
        # value would then carry the bare value into the options: the whole line is cleaned first
        for ch in self.INVISIBLES:
            for line in (f"password{ch}=variants:s3cr3tValue9", f"token{ch}=x;Варианты:s3cr3tValue9"):
                with self.subTest(ch=hex(ord(ch)), line=line.split(ch)[0]):
                    _cfg, sent = self.blocked_text(f"BLOCKED: [class=question rec=A red=yes] {line}")
                    [msg] = [t for t in sent if "ждёт тебя" in t]
                    self.assertNotIn("s3cr3t", msg)
                    self.assertIn("рекомендация волны: A", msg)

    def test_blocked_notice_never_shows_what_the_plain_quote_masks(self):  # the class, not the case
        # whatever quote() hides in the whole question stays hidden after the split into question/options
        value = "s3cr3tValue9"
        shapes = ["password={v}", "password=variants:{v}", "token=Варианты:{v}", "x; Варианты: A token={v} | B",
                  "ghp_{v}a1B2c3D4e5F6g7H8i9J0k1L2m3N4", "первая\ntoken={v}; Варианты: A | B"]
        shapes += [f"password{ch}=variants:{{v}}" for ch in self.INVISIBLES]
        shapes += [f"q; Варианты: A s3cr3t{ch}{{v}} | B" for ch in self.INVISIBLES]
        for shape in shapes:
            question = shape.format(v=value)
            if value in wab.quote(question):
                continue  # beyond redact()'s heuristic for any quote: not a property of the split
            with self.subTest(shape=shape):
                _cfg, sent = self.blocked_text(f"BLOCKED: [class=question rec=A red=yes] {question}")
                [msg] = [t for t in sent if "ждёт тебя" in t]
                self.assertNotIn(value, msg)
                self.assertNotIn("s3cr3t", msg)

    def test_blocked_notice_masks_an_invisible_character_in_the_options(self):  # Astra r3 (high)
        for ch in self.INVISIBLES:
            for opt in (f"A token=s3cr3t{ch}Value9 | B нет", f"A pass{ch}word=s3cr3tValue9 | B нет",
                        f"A s3cr3t{ch}Value9 | B нет"):
                with self.subTest(ch=hex(ord(ch)), opt=opt.split(ch)[0]):
                    _cfg, sent = self.blocked_text(
                        f"BLOCKED: [class=question rec=A red=yes] вопрос; Варианты: {opt}")
                    [msg] = [t for t in sent if "ждёт тебя" in t]
                    self.assertNotIn("s3cr3t", msg)
                    self.assertNotIn("Value9", msg)

    def test_blocked_notice_with_a_huge_question_still_fits_and_keeps_the_answer_path(self):
        cfg, sent = self.blocked_text(self.LABELLED.format(question="очень длинный вопрос " * 400))
        [msg] = [t for t in sent if "ждёт тебя" in t]
        self.assertLessEqual(len(msg), wab.TG_MESSAGE_LIMIT)
        self.assertIn("attach -t", msg)
        self.assertIn(" say ", msg)
        self.assertIn("A поднять кап", msg)

    def test_blocked_notice_first_line_only_of_a_multiline_question(self):
        status = self.LABELLED.format(question="первая строка\nвторая строка с подробностями")
        _cfg, sent = self.blocked_text(status)
        [msg] = [t for t in sent if "ждёт тебя" in t]
        self.assertIn("первая строка", msg)
        self.assertNotIn("подробностями", msg)

    def test_blocked_notice_without_or_with_a_broken_label_is_the_plain_quote(self):
        for status in ("BLOCKED: просто вопрос без метки",
                       "BLOCKED: [class=nope rec=owner red=no] испорченная метка",
                       "BLOCKED: [class=blocked_cap rec=owner] нет red"):
            with self.subTest(status=status):
                _cfg, sent = self.blocked_text(status)
                [msg] = [t for t in sent if "ждёт тебя" in t]
                self.assertIn("Цитата волны", msg)
                self.assertIn(status.split("] ")[-1] if "]" in status else "просто вопрос", msg)
                self.assertIn("attach -t", msg)
                self.assertNotIn("Класс:", msg)

    def test_the_quote_is_capped_and_the_message_fits(self):
        cfg, _ = self.chain_for(True, waves=["W1"])
        self.put(cfg)
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("слово " * 3000, encoding="utf-8")
        wab.tick(cfg, wab.load_state(cfg))
        [msg] = [t for t in self.tg if "волна W1 завершена" in t]
        self.assertLessEqual(len(msg), wab.TG_MESSAGE_LIMIT)
        self.assertIn("Целиком:", msg)  # the framing after the quote survives
        self.assertIn("Цитата волны", msg)
        self.assertIn("> ", msg)

    def test_a_secret_is_masked_in_the_outbox_text_too(self):
        cfg, _ = self.chain_for(True)
        text = wab.quote(self.hostile)
        self.assertNotIn(self.SECRET, text)
        self.assertLessEqual(len(wab.quote("x " * 5000)), wab.TG_LIMIT + 8)

    def test_a_wave_cannot_smuggle_the_markers(self):
        evil = f"x\x03 вне цитаты \x02Q {self.hostile}\x07\x1b[31m"
        q = wab.quote(evil)
        inner = q[1:-1]
        for ch in ("\x02", "\x03", "\x07", "\x1b"):
            self.assertNotIn(ch, inner)
        self.assertEqual((q[0], q[-1]), ("\x02", "\x03"))
        self.assertNotIn(self.SECRET, q)

    TOKEN = "sk-ant-abcdefghijklmnopqrstuvwx1234"

    def test_a_control_character_cannot_glue_a_token_to_a_letter(self):
        for ctl in ("\x00", "\x02", "\x03", "\x07", "\x1b", "\x9b", "\u2028", "\r"):
            with self.subTest(ctl=repr(ctl)):
                q = wab.quote(f"a{ctl}{self.TOKEN}")
                self.assertNotIn("ant-abcdefgh", q)
                self.assertNotIn("ant-abcdefgh", wab.render_notice(q))
                cfg, _ = self.chain_for(True)
                for raw in (f"старая запись a{ctl}{self.TOKEN}", f"x\x02a{ctl}{self.TOKEN}\x03 y"):
                    self.assertNotIn("ant-abcdefgh", wab.render_notice(raw))
                    self.assertNotIn("ant-abcdefgh", wab._first_line(raw, 300))

    def test_a_control_character_inside_a_token_does_not_hide_it(self):
        q = wab.quote("key sk-\x00ant-abcdefghijklmnopqrstuvwx1234")
        self.assertNotIn("abcdefghijkl", q)
        self.assertNotIn("abcdefghijkl", wab.render_notice("sk-\x01ant-abcdefghijklmnopqrstuvwx1234"))

    # one representative per class that must never glue or split a secret past the mask
    GLUERS = {
        "Cc": ["\x00", "\x0b", "\x1b", "\x85", "\x9b"],
        "Cf": ["\u200b", "\u200c", "\u200d", "\u200e", "\u202a", "\u202e", "\u2060", "\u2066",
               "\u2069", "\ufeff", "\u00ad", "\u061c", "\u180e", "\U000e0020"],
        "Zl/Zp": ["\u2028", "\u2029"],
        "CR": ["\r", "\r\n"],
    }
    HEAD, TAIL = "sk-ant-api03-AbCdEf", "1234567890xyzQWERTY"

    def secret_gone(self, out):
        self.assertNotIn("AbCdEf", out)
        self.assertNotIn("1234567890", out)
        self.assertNotIn("xyzQWERTY", out)

    def test_invisible_and_control_characters_cannot_glue_or_split_a_secret(self):
        for cls, chars in self.GLUERS.items():
            for ch in chars:
                if cls == "CR":
                    continue  # a line break is a separator, covered by its own test
                with self.subTest(cls=cls, ch=repr(ch)):
                    glued = f"a{ch}{self.HEAD}{self.TAIL}"
                    split = f"ключ {self.HEAD}{ch}{self.TAIL} конец"
                    for raw in (glued, split):
                        self.secret_gone(wab.quote(raw))
                        self.secret_gone(wab.render_notice(wab.quote(raw)))
                        self.secret_gone(wab.render_notice(raw))  # an older entry without markers
                        self.secret_gone(wab.render_notice(f"x\x02{raw}\x03 y"))
                        self.secret_gone(wab._first_line(raw, 300))
                    self.assertIn("конец", wab.render_notice(split))

    def test_every_format_character_of_unicode_masks_its_token(self):
        import sys
        import unicodedata
        planes = [*range(0x20000), *range(0xE0000, 0xE0200)]
        cf = [chr(c) for c in planes if unicodedata.category(chr(c)) == "Cf"]
        self.assertGreater(len(cf), 100)
        for ch in cf:
            out = wab.render_notice(f"{self.HEAD}{ch}{self.TAIL}")
            if "1234567890" in out or "AbCdEf" in out:
                self.fail(f"U+{ord(ch):04X} is not neutralised")

    # Default-ignorable characters outside Cf (Unicode DerivedCoreProperties: Default_Ignorable_Code_Point
    # minus Cf/Cc/Zl/Zp): drawn as nothing, so they glue or split a secret as well.
    IGNORABLE = ["\u034f", "\u115f", "\u1160", "\u17b4", "\u17b5", "\u180b", "\u180c", "\u180d",
                 "\u180f", "\u3164", "\ufe00", "\ufe0e", "\ufe0f", "\uffa0", "\U000e0100",
                 "\U000e01ef"]

    def test_default_ignorable_characters_cannot_glue_or_split_a_secret(self):
        for ch in self.IGNORABLE:
            with self.subTest(ch=f"U+{ord(ch):04X}"):
                glued = f"a{ch}{self.HEAD}{self.TAIL}"
                split = f"ключ {self.HEAD}{ch}{self.TAIL} конец"
                prefix = f"ключ sk-{ch}ant-abcdefghijklmnopqrstuvwx1234 конец"
                for raw in (glued, split, prefix):
                    if raw is prefix:
                        check = lambda out: self.assertNotIn("abcdefghijklmnopqrstuvwx", out)
                    else:
                        check = self.secret_gone
                    check(wab.quote(raw))
                    check(wab.render_notice(wab.quote(raw)))
                    check(wab.render_notice(raw))
                    check(wab.render_notice(f"x\x02{raw}\x03 y"))
                    check(wab._first_line(raw, 300))
                    check(wab._first_line(wab.quote(raw), 300))
                self.assertIn("конец", wab.render_notice(split))
                self.assertIn("конец", wab.render_notice(wab.quote(prefix)))

    # Default-ignorable code points UNASSIGNED in the interpreter's tables (category Cn): a terminal or
    # Telegram draws them as nothing too, so `sk-<U+2065>ant-...` must not leave visible.
    UNASSIGNED_IGNORABLE = ["\u2065", "\U000e0080", "\ufff0", "\ufff8", "\U000e0000", "\U000e0fff"]

    def test_unassigned_default_ignorable_characters_cannot_glue_or_split_a_secret(self):
        import unicodedata
        for ch in self.UNASSIGNED_IGNORABLE:
            with self.subTest(ch=f"U+{ord(ch):04X}"):
                self.assertEqual(unicodedata.category(ch), "Cn")
                glued = f"a{ch}{self.HEAD}{self.TAIL}"
                split = f"ключ {self.HEAD}{ch}{self.TAIL} конец"
                prefix = f"ключ sk-{ch}ant-abcdefghijklmnopqrstuvwx1234 конец"
                for raw in (glued, split, prefix):
                    if raw is prefix:
                        check = lambda out: self.assertNotIn("abcdefghijklmnopqrstuvwx", out)
                    else:
                        check = self.secret_gone
                    check(wab.quote(raw))
                    check(wab.render_notice(wab.quote(raw)))
                    check(wab.render_notice(raw))
                    check(wab.render_notice(f"x\x02{raw}\x03 y"))
                    check(wab._first_line(raw, 300))
                    check(wab._first_line(wab.quote(raw), 300))
                self.assertIn("конец", wab.render_notice(split))
                self.assertIn("конец", wab.render_notice(wab.quote(prefix)))

    def test_every_variation_selector_of_unicode_masks_its_token(self):
        import unicodedata
        planes = [*range(0x20000), *range(0xE0000, 0xE0200)]
        vs = [chr(c) for c in planes if "VARIATION SELECTOR" in unicodedata.name(chr(c), "")]
        self.assertGreaterEqual(len(vs), 16 + 240 + 4)  # FE00-FE0F, E0100-E01EF, Mongolian FVS 1-4
        for ch in vs:
            for raw in (f"{self.HEAD}{ch}{self.TAIL}", f"sk-{ch}ant-abcdefghijklmnopqrstuvwx1234"):
                for out in (wab.quote(raw), wab.render_notice(wab.quote(raw)), wab._first_line(raw, 300)):
                    if any(s in out for s in ("1234567890", "AbCdEf", "abcdefghijklmnopqrstuvwx")):
                        self.fail(f"U+{ord(ch):04X} is not neutralised: {out!r}")

    def test_every_default_ignorable_character_masks_its_token(self):
        import unicodedata
        chars = wab._default_ignorable()
        self.assertTrue(set(self.IGNORABLE + self.UNASSIGNED_IGNORABLE) <= set(chars))
        for ch in chars:  # not Cf/Cc/Zl/Zp (those are in _INVISIBLE by category) and not a space
            self.assertNotIn(unicodedata.category(ch), {"Cf", "Cc", "Zl", "Zp", "Zs"})
        # EVERY code point of Default_Ignorable_Code_Point, assigned or not (Cn), masks its token
        every = [chr(c) for lo, hi in wab._DEFAULT_IGNORABLE_RANGES for c in range(lo, hi + 1)]
        self.assertEqual(len(every), 4174)
        for ch in every:
            self.assertNotIn(unicodedata.category(ch), {"Cc", "Zl", "Zp", "Zs"})
            for raw in (f"{self.HEAD}{ch}{self.TAIL}", f"sk-{ch}ant-abcdefghijklmnopqrstuvwx1234"):
                for out in (wab.quote(raw), wab.render_notice(wab.quote(raw)), wab._first_line(raw, 300)):
                    if any(s in out for s in ("1234567890", "AbCdEf", "abcdefghijklmnopqrstuvwx")):
                        self.fail(f"U+{ord(ch):04X} is not neutralised: {out!r}")

    def test_unicode_spaces_separate_tokens_like_a_plain_space(self):
        for ch in ("\u00a0", "\u1680", "\u2000", "\u2003", "\u200a", "\u202f", "\u205f", "\u3000"):
            with self.subTest(ch=repr(ch)):
                self.assertEqual(unicodedata_category(ch), "Zs")
                # a word next to the space is NOT swallowed by a masked neighbour
                out = wab.render_notice(f"слово{ch}\u200bплохо{ch}нормально")
                self.assertIn("слово", out)
                self.assertIn("нормально", out)
                self.assertNotIn("плохо", out)  # only the token with the invisible character is masked
                self.secret_gone(wab.render_notice(f"a{ch}{self.HEAD}{self.TAIL}"))
                self.secret_gone(wab.quote(f"{self.HEAD}{self.TAIL}{ch}x"))
                # a space cuts a word in two, a plain one and a Unicode one alike
                self.assertEqual(wab.quote(f"{self.HEAD}{ch}{self.TAIL}").replace(ch, " "),
                                 wab.quote(f"{self.HEAD} {self.TAIL}"))

    def test_the_first_line_of_a_quoted_message_keeps_the_quote(self):
        msg = f"wave-autobot: волна W1 завершена.\n{wab.quote('первая строка итога' + chr(10) + 'вторая')}"
        self.assertEqual(wab._first_line(msg, 300), "wave-autobot: волна W1 завершена.")
        only = wab._first_line(wab.quote("Итог: всё хорошо\nвторая"), 300)
        self.assertEqual(only, "Цитата волны: Итог: всё хорошо")
        secret = wab._first_line(wab.quote(f"a\u200b{self.HEAD}{self.TAIL}\nx"), 300)
        self.assertTrue(secret.startswith("Цитата волны: "), secret)
        self.secret_gone(secret)
        self.assertEqual(wab._first_line("", 300), "")

    def test_crlf_text_keeps_its_lines_and_a_lone_cr_separates(self):
        out = wab.render_notice(wab.quote("первая\r\nвторая\rтретья"))
        self.assertEqual(out.splitlines()[1:], ["> первая", "> вторая", "> третья"])
        self.assertNotIn("\r", out)

    def test_two_secrets_one_glued_one_split_by_controls_both_masked(self):
        both = "a\x00sk-ant-abcdefghijklmnopqrstuvwx1234 и sk-\x00ant-zyxwvutsrqponmlkjihgfed9876 конец\r\nвторая"
        for out in (wab.quote(both), wab.render_notice(both), wab.render_notice(wab.quote(both)),
                    wab.render_notice("old " + both)):
            self.assertNotIn("abcdefghijkl", out)
            self.assertNotIn("zyxwvutsrqpo", out)
            self.assertIn("конец", out)
            self.assertIn("вторая", out)  # \r\n is a line break, not a control inside a word

    def test_the_second_redaction_inside_notify_ignores_the_owner_exemption(self):
        cfg, _ = self.chain_for(True)
        raw = f"wave-autobot: цитата\n\x02{self.hostile}\x03\nконец"  # an entry that skipped quote()
        wab.notify(cfg, raw)
        self.assertNotIn(self.SECRET, self.tg[-1])

    def test_old_entries_without_markers_are_handled_as_before(self):
        cfg, _ = self.chain_for(True)
        wab.notify(cfg, "wave-autobot: password=hunter2hunter2 тест")
        self.assertNotIn("hunter2hunter2", self.tg[-1])
        self.assertTrue(self.tg[-1].startswith(cfg["chain"] + ": "), self.tg[-1])  # signed by the chain, #56
        self.assertNotIn("Цитата", self.tg[-1])

    def test_the_dispatchers_own_script_path_stays_whole(self):
        cfg, _ = self.chain_for(True)
        sha = "7" * 40
        path = wab.write_owner_script(cfg, "W1", sha)
        wab.notify(cfg, f"wave-autobot: Гейт мерджа пройден. Выполни: {wab.home_form(path)}\n"
                        f"{wab.quote('результат волны')}")
        self.assertIn(wab.home_form(path), self.tg[-1])
        self.assertIn("результат волны", self.tg[-1])

    def test_the_dispatchers_path_survives_in_the_external_handoff(self):
        cfg, _ = self.chain_for(True, merge_gate="external")
        with mock.patch.object(wab, "gate_check", return_value={
                "verdict": "pass", "reasons": [], "number": 7, "head": HEAD, "unresolved": [],
                "old_p01": [], "draft": False}):
            self.put(cfg)
            self.set_status(cfg, "W1", "DONE")
            (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
            (wab.wave_dir(cfg, "W1") / "result.md").write_text(f"Итог: {self.hostile}\n", encoding="utf-8")
            wab.tick(cfg, wab.load_state(cfg))
        [msg] = [t for t in self.tg if "сдала PR" in t]
        self.assertIn("~/.cache/wab/" + wab.owner_script_name(cfg, "W1", HEAD), msg)
        self.assertNotIn(self.SECRET, msg)


def _unquoted_wave_text(src):
    """put_notice calls whose text holds a wave/external text (a file read, a status, a reason) that
    is not inside quote(): such text would go out unmarked and with the owner-script exemption."""
    import ast
    untrusted = {"status", "why", "refused", "reasons"}
    tree = ast.parse(src)
    bad = []

    def walk(node, quoted):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in ("quote", "blocked_notice"):  # blocked_notice quotes every wave text itself
                quoted = True
            elif name in ("redact", "read") and not quoted:
                bad.append(f"{name}() in a notice text, line {node.lineno}")
        if isinstance(node, ast.Name) and node.id in untrusted and not quoted:
            bad.append(f"{node.id} in a notice text, line {node.lineno}")
        for child in ast.iter_child_nodes(node):
            # the test of `a if why else b` only decides which wording is used: it prints nothing
            walk(child, quoted or (isinstance(node, ast.IfExp) and child is node.test))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "put_notice"
                and len(node.args) >= 4):
            walk(node.args[3], False)
    return bad


class W3NoticeQuoteGuard(Base):
    def test_every_wave_text_in_a_notice_goes_through_quote(self):
        self.assertEqual(_unquoted_wave_text((WAVES / "wab.py").read_text(encoding="utf-8")), [])

    def test_the_guard_sees_an_unquoted_text(self):
        src = ("def f(w, status, wdir):\n"
               "    put_notice(w, 'blocked', status, f'x {redact(status)}')\n"
               "    put_notice(w, 'done', '1', f'x {read(wdir)}')\n"
               "    put_notice(w, 'idle', '1', f'x {status}')\n"
               "    put_notice(w, 'idle', '1', f'x {quote(status)}')\n")
        self.assertEqual(len(_unquoted_wave_text(src)), 4, _unquoted_wave_text(src))


# ---------------------------------------------------------------- W1 (#68): background tails and the idle nudge
PROCS = Path(__file__).resolve().parent.parent / "fixtures" / "processes"
REAL_WAVE_CHILDREN = getattr(wab, "wave_children", None)
REAL_PROCESS_TABLE = getattr(wab, "process_table", None)


def procs(name):
    """A real process-tree snapshot of a wave window (tests/fixtures/processes/README.md)."""
    return (PROCS / name).read_text(encoding="utf-8")


LOOPS_CLAUDE = "2034867"
IDLE_CLAUDE = "2064409"


def _bg_events(cfg, needle):
    p = cfg["run_dir"] / "events.log"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return [l for l in text.splitlines() if needle in l]


class ProcessFacts(Base):
    """A: the process table and the children of the wave's Claude, from `ps` (never `pgrep -f`)."""

    def setUp(self):
        super().setUp()
        self.ps_text, self.ps_rc, self.ps_raise, self.display = procs("ps-claude-bg-loops.txt"), 0, None, LOOPS_CLAUDE
        self.ps_calls = []
        base = wab.sh

        def sh(*args, **kw):
            if args[:1] == ("ps",):
                self.ps_calls.append(args)
                if self.ps_raise:
                    raise self.ps_raise
                return subprocess.CompletedProcess(args, self.ps_rc, self.ps_text, "ps: boom")
            if args[:1] == ("tmux",) and "display-message" in args:
                self.tmux_calls.append(args)
                if self.display is None:
                    return subprocess.CompletedProcess(args, 1, "", "no server")
                return subprocess.CompletedProcess(args, 0, self.display + "\n", "")
            return base(*args, **kw)

        p = mock.patch.object(wab, "sh", side_effect=sh)
        p.start()
        self.addCleanup(p.stop)

    def test_ps_argv_and_table_of_the_live_snapshot(self):
        self.assertEqual(wab.PS_ARGV, ("ps", "-ww", "-A", "-o", "pid=,ppid=,etime=,args="))
        table = REAL_PROCESS_TABLE()
        self.assertEqual(self.ps_calls[0][:6], wab.PS_ARGV)
        self.assertEqual(table[2034867]["name"], "claude")
        self.assertEqual(table[2048167]["ppid"], 2034867)
        self.assertEqual(table[2048167]["age"], 28)
        self.assertEqual(table[2048167]["name"], "bash")
        self.assertEqual(table[1]["age"], 52 * 86400 + 7 * 3600 + 49 * 60 + 39)
        self.assertEqual(table[2048172]["name"], "sleep")
        self.assertIn("eval 'sleep 600'", table[2048169]["args"])

    def test_etime_forms(self):
        for etime, secs in (("02:33", 153), ("1:02:03", 3723), ("3-04:05:06", 3 * 86400 + 4 * 3600 + 5 * 60 + 6)):
            self.ps_text = f"  10     1 {etime} /bin/sleep 1\n"
            self.assertEqual(REAL_PROCESS_TABLE()[10]["age"], secs, etime)

    def test_any_failure_is_one_error(self):
        cases = [("oserror", {"ps_raise": OSError("no ps")}), ("rc", {"ps_rc": 1}),
                 ("timeout", {"ps_raise": subprocess.TimeoutExpired("ps", 10)}),
                 ("garbage", {"ps_text": "not a process table\n"}), ("empty", {"ps_text": ""}),
                 ("half", {"ps_text": procs("ps-claude-idle.txt") + "xx yy\n"})]
        for label, over in cases:
            with self.subTest(label):
                self.ps_text, self.ps_rc, self.ps_raise = procs("ps-claude-bg-loops.txt"), 0, None
                for k, v in over.items():
                    setattr(self, k, v)
                with self.assertRaises(wab.ProcFactsError):
                    REAL_PROCESS_TABLE()

    def test_children_of_the_claude_of_the_pane(self):
        kids = REAL_WAVE_CHILDREN({}, {"tmux": "wv-w1"})
        self.assertEqual(sorted(k["pid"] for k in kids), [2048167, 2048168, 2048169])
        by = {k["pid"]: k for k in kids}
        self.assertEqual([d["name"] for d in by[2048169]["tree"]], ["sleep"])
        self.assertEqual(by[2048169]["tree"][0]["pid"], 2048172)
        self.assertEqual(by[2048169]["name"], "bash")
        self.assertTrue(any("display-message" in c and "#{pane_pid}" in c for c in self.tmux_calls))
        self.assertTrue(any("-t" in c and "=wv-w1:" in c for c in self.tmux_calls if "display-message" in c))

    def test_no_children_of_an_idle_claude(self):
        self.ps_text, self.display = procs("ps-claude-idle.txt"), IDLE_CLAUDE
        self.assertEqual(REAL_WAVE_CHILDREN({}, {"tmux": "wv-w1"}), [])

    def test_pane_pid_failures_and_a_pid_missing_from_the_table(self):
        for label, display in (("tmux fails", None), ("not a number", "abc"), ("empty", ""), ("unknown pid", "999")):
            with self.subTest(label):
                self.display = display
                with self.assertRaises(wab.ProcFactsError):
                    REAL_WAVE_CHILDREN({}, {"tmux": "wv-w1"})

    def test_no_pgrep_f_in_the_dispatcher(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"""["']p(?:grep|kill)["']""")  # no process is ever looked up by pattern


class BgBase(Base):
    """The watch tick with a wave window whose process tree is a real snapshot."""

    NUDGE = 20

    def setUp(self):
        super().setUp()
        if REAL_WAVE_CHILDREN is not None:  # the shared setUp stubs the tree out for every other class
            p = mock.patch.object(wab, "wave_children", REAL_WAVE_CHILDREN)
            p.start()
            self.addCleanup(p.stop)
        self.ps_text, self.ps_rc, self.ps_raise, self.display = procs("ps-claude-idle.txt"), 0, None, IDLE_CLAUDE
        base = wab.sh

        def sh(*args, **kw):
            if args[:1] == ("ps",):
                if self.ps_raise:
                    raise self.ps_raise
                return subprocess.CompletedProcess(args, self.ps_rc, self.ps_text, "ps: boom")
            if args[:1] == ("tmux",) and "display-message" in args:
                if self.display is None:
                    return subprocess.CompletedProcess(args, 1, "", "no server")
                return subprocess.CompletedProcess(args, 0, self.display + "\n", "")
            return base(*args, **kw)

        p = mock.patch.object(wab, "sh", side_effect=sh)
        p.start()
        self.addCleanup(p.stop)
        self.cfg, self.path = self.chain(idle_nudge_minutes=self.NUDGE)
        self.pane = live("30-main.txt")
        self.ansi = live("30-main.ansi")
        self.journal = self.transcript("s1", [asst(inp=1000, out=10)])
        self.agents = self.journal.parent / "s1" / "subagents"
        self.agents.mkdir(parents=True)
        self.age_journals(30 * 60)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            sessions=["s1"], pane_digest=wab.pane_digest(self.pane), pane_changed=time.time() - 30 * 60,
            activity_at=time.time() - 30 * 60)}})
        self.set_status(self.cfg, "W1", "RUNNING")

    def age_journals(self, secs):
        t = time.time() - secs
        for f in [self.journal, *self.agents.glob("*.jsonl")]:
            os.utime(f, (t, t))

    def tick(self, n=1):
        for _ in range(n):
            wab.tick(self.cfg, wab.load_state(self.cfg))

    def rec(self):
        return self.get_state(self.cfg)["waves"]["W1"]

    def patch_state(self, **kw):
        st = self.get_state(self.cfg)
        st["waves"]["W1"].update(kw)
        self.put_state(self.cfg, st)

    def nudges(self):
        return [s for s in self.sent if s[0] == "text" and "[wab] Толчок" in s[2]]

    def only(self, *keep):
        """Keep the pids of `keep` (and their descendants) from the loops snapshot, plus the claude."""
        out = []
        for line in procs("ps-claude-bg-loops.txt").splitlines():
            pid, ppid = line.split()[:2]
            if pid in (LOOPS_CLAUDE, "1") or pid in keep or ppid in keep:
                out.append(line)
        return "\n".join(out) + "\n"

    def loops(self, text=None):
        self.ps_text, self.display = text or procs("ps-claude-bg-loops.txt"), LOOPS_CLAUDE


class BackgroundTails(BgBase):
    """B: shells left over from before /clear, or idle for long, are reported once per episode."""

    def setUp(self):
        super().setUp()
        self.patch_state(pane_changed=time.time())  # the nudge is not under test here
        self.tails = lambda: _bg_events(self.cfg, "фоновые хвосты")

    def test_one_event_per_episode_over_loops_started_before_clear(self):
        self.loops()
        self.patch_state(cleared_at=time.time() - 5)  # the loops are 28 s old: started before /clear
        self.tick()
        ev = self.tails()
        self.assertEqual(len(ev), 1, ev)
        for pid in ("2048167", "2048168", "2048169"):
            self.assertIn(f"pid {pid}", ev[0])
        self.assertIn("запущен до /clear", ev[0])
        self.assertIn("until grep -q done", ev[0])  # the command inside eval '…', not the shell-snapshot prefix
        self.assertNotIn("shell-snapshots", ev[0])
        self.assertEqual(sorted(t["pid"] for t in self.rec()["bg_tails"]), [2048167, 2048168, 2048169])
        self.assertEqual(self.rec()["bg_tails"][0].keys() >= {"pid", "why", "age"}, True)
        self.tick()  # the same snapshot: the same episode
        self.assertEqual(len(self.tails()), 1)
        self.assertEqual(self.tg, [])  # an event and the dashboard only
        self.assertEqual(self.sent, [])  # no killing, no message into the window
        self.ps_text, self.display = procs("ps-claude-idle.txt"), IDLE_CLAUDE  # the tails are gone
        self.tick()
        self.assertNotIn("bg_tails", self.rec())
        self.assertNotIn("bg_tails", self.rec().get("notified", {}))
        self.assertEqual(len(self.tails()), 1)
        self.loops()  # a new episode: one more event
        self.tick()
        self.assertEqual(len(self.tails()), 2)

    def test_a_changed_set_is_a_new_event(self):
        self.loops()
        self.patch_state(cleared_at=time.time() - 5)
        self.tick()
        self.loops(self.only("2048167", "2048169"))
        self.tick()
        self.assertEqual(len(self.tails()), 2)

    def test_loops_started_after_clear_are_not_tails(self):
        self.loops()
        self.patch_state(cleared_at=time.time() - 120)  # /clear two minutes ago: the loops are newer
        self.tick()
        self.assertEqual(self.tails(), [])
        self.assertNotIn("bg_tails", self.rec())

    def test_no_clear_yet_and_young_shells_are_not_tails(self):
        self.loops()
        self.tick()
        self.assertEqual(self.tails(), [])

    def test_the_pgrep_loop_idle_for_long_is_a_tail(self):
        """the live pgrep -f loop (it finds itself): 35 min of `sleep 5` children and nothing else"""
        text = procs("ps-claude-bg-loops.txt").replace("2048168 2034867       00:28", "2048168 2034867       35:28")
        self.loops(text)
        self.tick()
        ev = self.tails()
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("pid 2048168", ev[0])
        self.assertIn("pgrep -f", ev[0])
        self.assertIn("только sleep", ev[0])
        self.assertIn("35 мин", ev[0])
        self.assertNotIn("pid 2048167", ev[0])  # young

    def test_a_long_shell_with_real_work_is_not_a_tail(self):
        text = procs("ps-claude-bg-loops.txt").replace("2048168 2034867       00:28", "2048168 2034867       35:28")
        text = text.replace("2050638 2048168       00:03 sleep 5", "2050638 2048168       00:03 node build.js")
        self.loops(text)
        self.tick()
        self.assertEqual(self.tails(), [])

    def test_the_threshold_constant(self):
        self.assertEqual(wab.LOOSE_SHELL_MINUTES, 30)

    def test_a_long_shell_without_children_is_a_tail(self):
        self.loops(self.only("2048168").replace("2048168 2034867       00:28", "2048168 2034867     1:00:28")
                   .replace("2050638 2048168       00:03 sleep 5\n", ""))
        self.tick()
        self.assertEqual(len(self.tails()), 1)

    def test_ps_error_is_one_event_per_episode_and_the_tick_goes_on(self):
        self.ps_raise = OSError("no ps")
        self.tick(2)
        ev = _bg_events(self.cfg, "дерево процессов недоступно")
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("no ps", ev[0])
        self.ps_raise = None
        self.tick()  # readable again: the episode is over
        self.ps_raise = OSError("no ps")
        self.tick()
        self.assertEqual(len(_bg_events(self.cfg, "дерево процессов недоступно")), 2)

    def test_the_cleared_at_is_saved_with_the_clear(self):
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec(
            phase="clearing", sessions=["s1"])}})
        before = time.time()
        self.tick()
        rec = self.rec()
        self.assertTrue(before - 1 <= rec["cleared_at"] <= time.time() + 1, rec.get("cleared_at"))

    def test_the_dashboard_shows_the_tails(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        w = {"tokens": 1000, "bg_tails": [{"pid": 1, "why": "x", "age": 5}, {"pid": 2, "why": "x", "age": 5}]}
        self.assertIn("⚠ хвосты: 2", dash.ctx_cell(w, 300000, 20).plain)
        self.assertNotIn("хвосты", dash.ctx_cell({"tokens": 1000}, 300000, 20).plain)


class IdleNudge(BgBase):
    """C: a RUNNING wave that has gone silent with nothing alive behind it gets one push."""

    def typed_screen(self, text):
        """The live screen 30-typed with `text` in the input line instead of the owner's draft."""
        lines = live("30-typed.ansi").split("\n")
        i = next(i for i, l in enumerate(lines) if "\u276f\u00a0" in l and "/model" not in l)
        lines[i] = "\x1b[39m\u276f\u00a0" + text
        return "\n".join(lines)

    def typed_nudge(self, minutes=20):
        return self.typed_screen(wab.idle_nudge_text(minutes))

    def test_idle_screen_and_stopped_journals_get_exactly_one_nudge(self):
        self.tick()
        self.assertEqual(len(self.nudges()), 1, self.sent)
        text = self.nudges()[0][2]
        self.assertIn("20+ мин", text)
        self.assertIn("status=RUNNING", text)
        self.assertIn("BLOCKED", text)
        ev = _bg_events(self.cfg, "idle nudge sent")
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("20 min", ev[0])
        self.assertFalse([t for t in self.tg if "Толчок" in t])  # an event and a message into the window only
        self.tick()  # the same episode (the screen did not change): not twice
        self.assertEqual(len(self.nudges()), 1)
        self.assertEqual(len(_bg_events(self.cfg, "idle nudge sent")), 1)

    def test_the_idle_notice_is_untouched(self):
        self.tick()
        self.assertTrue(any("молчит" in t for t in self.tg), self.tg)

    def test_the_nudge_goes_through_deliver(self):
        calls = []
        real = wab._deliver

        def spy(cfg, st, wave, what, send, text, precheck=None):
            calls.append((what, send, precheck is not None))
            return real(cfg, st, wave, what, send, text, precheck=precheck)

        with mock.patch.object(wab, "_deliver", side_effect=spy):
            self.tick()
        self.assertIn(("idle nudge", wab.send_text, True), calls)

    def test_a_fresh_agent_journal_means_no_nudge(self):
        agent = self.agents / "agent-1.jsonl"
        agent.write_text(asst(inp=5) + "\n", encoding="utf-8")  # written just now
        self.tick(2)
        self.assertEqual(self.nudges(), [])

    def test_a_fresh_write_seen_only_by_mtime_means_no_nudge(self):
        self.tick()  # sends the first nudge
        self.sent.clear()
        agent = self.agents / "agent-2.jsonl"
        agent.write_text(asst(inp=5) + "\n", encoding="utf-8")
        self.patch_state(pane_digest="other", pane_changed=time.time() - 30 * 60, activity_at=time.time() - 30 * 60,
                         activity_files={str(agent): os.stat(agent).st_mtime, str(self.journal): os.stat(self.journal).st_mtime})
        self.tick()
        self.assertEqual(self.nudges(), [])

    def test_a_recent_screen_change_means_no_nudge(self):
        self.patch_state(pane_changed=time.time() - 5 * 60)
        self.tick()
        self.assertEqual(self.nudges(), [])

    def test_a_silent_child_means_no_nudge(self):
        self.loops(self.only("2048169"))  # `sleep 600` in a shell: silent but alive
        self.tick(2)
        self.assertEqual(self.nudges(), [])

    def test_any_live_child_means_no_nudge(self):
        self.loops()
        self.tick(2)
        self.assertEqual(self.nudges(), [])
        self.ps_text = procs("ps-claude-idle.txt") + f"{IDLE_CLAUDE[:-1]}7 {IDLE_CLAUDE}   00:10 node server.js\n"
        self.tick()
        self.assertEqual(self.nudges(), [])

    def test_a_waiting_shell_loop_is_live_work(self):
        self.loops(self.only("2048167"))  # `until … do sleep 5; done`: a waiting loop, not an idle child
        self.tick()
        self.assertEqual(self.nudges(), [])

    def test_unreadable_process_tree_means_no_nudge_and_an_event_with_the_reason(self):
        cases = {"oserror": lambda: setattr(self, "ps_raise", OSError("no ps")),
                 "rc": lambda: setattr(self, "ps_rc", 2),
                 "garbage": lambda: setattr(self, "ps_text", "garbage\n"),
                 "display-message": lambda: setattr(self, "display", None)}
        for label, breakit in cases.items():
            with self.subTest(label):
                self.setUp()
                breakit()
                self.tick(2)
                self.assertEqual(self.nudges(), [])
                ev = _bg_events(self.cfg, "дерево процессов недоступно")
                self.assertEqual(len(ev), 1, ev)

    def test_nudge_off_and_bad_values(self):
        self.cfg, _ = self.chain(idle_nudge_minutes=0)
        self.tick(2)
        self.assertEqual(self.nudges(), [])
        self.assertEqual(self.cfg["idle_nudge_minutes"], 0)
        for bad in (-1, "20", True, False, float("nan"), float("inf"), 1441, None, [20]):
            with self.subTest(bad=bad):
                doc = {"chain": CHAIN, "run_id": RUN_ID, "waves": ["W1"], "idle_nudge_minutes": bad}
                p = self.tmp / "cfg" / "bad.json"
                p.write_text(json.dumps(doc), encoding="utf-8")
                with self.assertRaises(SystemExit) as ctx:
                    wab.load_chain(p)
                self.assertIn("idle_nudge_minutes", str(ctx.exception))

    def test_default_is_twenty_and_it_is_tunable(self):
        cfg, _ = self.chain(idle_nudge_minutes=None)
        self.assertEqual(cfg["idle_nudge_minutes"], 20)
        self.assertIn("idle_nudge_minutes", wab.TUNABLE)
        cfg2, _ = self.chain(idle_nudge_minutes=1440)
        self.assertEqual(cfg2["idle_nudge_minutes"], 1440)
        cfg3, _ = self.chain(idle_nudge_minutes=2.5)
        self.assertEqual(cfg3["idle_nudge_minutes"], 2.5)

    def test_live_screens_dialogs_menus_and_typed_text_block_the_nudge(self):
        for name in ("trust", "menu", "typed"):
            with self.subTest(name):
                self.setUp()
                self.pane, self.ansi = live(f"30-{name}.txt"), live(f"30-{name}.ansi")
                self.patch_state(pane_digest=wab.pane_digest(self.pane))
                self.tick(2)
                self.assertEqual(self.nudges(), [])
        self.setUp()
        self.tick()
        self.assertEqual(len(self.nudges()), 1)  # the live main screen: allowed

    def test_the_exit_menu_blocks_the_nudge(self):
        self.pane = ("Exit?\n  ❯ 1. Exit and stop tasks\n    2. Move to background and exit\n"
                     "    3. Stay\n\n Enter to confirm · Esc to cancel\n")
        self.patch_state(pane_digest=wab.pane_digest(self.pane))
        self.tick()
        self.assertEqual(self.nudges(), [])

    def test_the_status_changing_under_the_lock_means_no_nudge(self):
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            self.set_status(self.cfg, "W1", "BLOCKED: need an answer")  # the wave moved while we waited
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter):
            self.tick()
        self.assertEqual(self.nudges(), [])
        self.assertEqual(_bg_events(self.cfg, "idle nudge sent"), [])

    def test_the_screen_changing_under_the_lock_means_no_nudge(self):
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            self.ansi = live("30-typed.ansi")
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter):
            self.tick()
        self.assertEqual(self.nudges(), [])

    def test_a_child_appearing_under_the_lock_means_no_nudge(self):
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            self.loops()
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter):
            self.tick()
        self.assertEqual(self.nudges(), [])

    def test_only_a_running_wave_with_a_bound_session_is_nudged(self):
        for label, st, over in (("not RUNNING", "BLOCKED: q", {}), ("no session", "RUNNING", {"sessions": []}),
                                ("awaiting", "RUNNING", {"await_session": True}),
                                ("pending_enter", "RUNNING", {"pending_enter": "alarm"}),
                                ("checkpoint", "RUNNING", {"phase": "checkpoint", "checkpoint_sent": True,
                                                           "checkpoint_at": time.time()})):
            with self.subTest(label):
                self.setUp()
                self.set_status(self.cfg, "W1", st)
                self.patch_state(**over)
                self.tick(2)
                self.assertEqual(self.nudges(), [])

    def test_a_typed_nudge_whose_wave_left_running_is_abandoned(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок")
        self.set_status(self.cfg, "W1", "BLOCKED: asks")
        self.tick()
        rec = self.rec()
        self.assertNotIn("pending_enter", rec)
        self.assertNotIn("pending_clear", rec)  # cleared on the screen at once
        self.assertEqual(self.nudges(), [])

    def test_an_enter_only_retry_of_a_typed_nudge(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_nudge()  # our text is in the input: the retry must not call it stale
        with mock.patch.object(wab, "submit") as submit:
            self.tick()
        self.assertEqual(self.nudges(), [])  # nothing typed again
        self.assertTrue(submit.called)

    def test_a_journal_written_while_waiting_for_the_lock_means_no_nudge(self):
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            (self.agents / "late.jsonl").write_text(asst(inp=5) + "\n", encoding="utf-8")  # the agent wrote
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter):
            self.tick()
        self.assertEqual(self.nudges(), [])
        self.assertEqual(_bg_events(self.cfg, "idle nudge sent"), [])

    def test_a_screen_changed_while_waiting_for_the_lock_means_no_nudge(self):
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            self.pane = self.pane + "\nthe wave printed something\n"  # empty input, but a new screen
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter):
            self.tick()
        self.assertEqual(self.nudges(), [])

    def test_an_undelivered_nudge_is_cleared_before_another_delivery(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_nudge()  # the nudge text is in the input
        st = wab.load_state(self.cfg)
        self.assertTrue(wab._deliver(self.cfg, st, "W1", "alarm", wab.send_text, "ALARM TEXT"))
        kinds = [k for k, _, t in self.sent]
        self.assertEqual(kinds, ["clear", "text"], self.sent)  # cleared first, then the alarm
        self.assertNotIn("pending_clear", st["waves"]["W1"])
        self.assertNotEqual(st["waves"]["W1"].get("pending_enter"), "idle nudge")

    def test_nothing_is_typed_over_a_nudge_that_cannot_be_cleared(self):
        self.clear_works = False
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_nudge()
        st = wab.load_state(self.cfg)
        self.assertFalse(wab._deliver(self.cfg, st, "W1", "alarm", wab.send_text, "ALARM TEXT"))
        self.assertEqual([t for k, _, t in self.sent if k == "text"], [])
        self.assertTrue(st["waves"]["W1"].get("pending_clear"))  # held: the next delivery tries again

    def test_a_nudge_itself_is_not_abandoned_by_its_own_retry(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_nudge()
        with mock.patch.object(wab, "submit") as submit:
            self.tick()
        self.assertTrue(submit.called)
        self.assertEqual(self.clear_keys, [])

    def test_a_retry_over_the_owners_text_presses_nothing_and_clears_nothing(self):
        for label, text in (("replaced", "owner draft"), ("extended", wab.idle_nudge_text(20) + " и ещё"),
                            ("cut", wab.idle_nudge_text(20)[:40])):
            with self.subTest(label):
                self.setUp()
                self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
                self.ansi = self.typed_screen(text)
                with mock.patch.object(wab, "submit") as submit:
                    self.tick(2)
                self.assertFalse(submit.called)
                self.assertEqual(self.enters, [])
                self.assertEqual(self.clear_keys, [])
                self.assertNotIn("pending_enter", self.rec())
                self.assertNotIn("pending_clear", self.rec())
                self.assertEqual(self.nudges(), [])
                self.assertEqual(len(_bg_events(self.cfg, "idle nudge dropped: the input holds another text")), 1)

    def test_the_owner_edits_the_text_while_waiting_for_the_lock(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_nudge()
        real_enter = wab._InputLock.__enter__

        def enter(lock):
            self.ansi = self.typed_screen("owner draft")
            return real_enter(lock)

        with mock.patch.object(wab._InputLock, "__enter__", enter), mock.patch.object(wab, "submit") as submit:
            self.tick()
        self.assertFalse(submit.called)
        self.assertEqual(self.clear_keys, [])  # the stale path did not clear the draft
        self.assertNotIn("pending_enter", self.rec())
        self.assertNotIn("pending_clear", self.rec())

    def test_another_delivery_leaves_the_owners_text_alone(self):
        self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
        self.ansi = self.typed_screen("owner draft")
        st = wab.load_state(self.cfg)
        self.assertFalse(wab._deliver(self.cfg, st, "W1", "alarm", wab.send_text, "ALARM TEXT"))
        self.assertEqual(self.clear_keys, [])  # the draft is not cleared
        self.assertEqual([t for k, _, t in self.sent if k == "text"], [])  # and nothing typed over it
        self.assertNotIn("pending_enter", st["waves"]["W1"])
        self.assertNotIn("pending_clear", st["waves"]["W1"])

    def test_no_enter_into_a_menu_or_dialog_or_a_blank_capture_on_retry(self):
        for label, pane, ansi in (("menu", live("30-menu.txt"), live("30-menu.ansi")),
                                  ("trust", live("30-trust.txt"), live("30-trust.ansi")),
                                  ("blank capture", "", "")):
            with self.subTest(label):
                self.setUp()
                self.patch_state(pending_enter="idle nudge", pending_text_head="[wab] Толчок: окно")
                self.pane, self.ansi = pane, ansi
                with mock.patch.object(wab, "submit") as submit:
                    self.tick(2)
                self.assertFalse(submit.called)
                self.assertEqual(self.enters, [])
                self.assertEqual(self.nudges(), [])
                self.assertFalse([c for c in self.tmux_calls if "Enter" in c])

    def test_protocol_text(self):
        text = (WAVES / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("TaskStop", text)
        self.assertRegex(text, r"HANDOFF_READY[^\n]*\n?[^\n]*(фонов|TaskStop)|(фонов|TaskStop)[^\n]*HANDOFF_READY")
        self.assertIn("pgrep -f", text)
        self.assertIn("pkill -f", text)
        self.assertIn("по PID", text)
        self.assertIn("[wab] Толчок", text)
        self.assertIn("В полёте", text)


class ChainCleanup(Base):
    """W3 (#57): closing the tmux sessions and panes of a finished chain, only the chain's own.
    Real tmux on the private socket `-L wabtest-<pid>-cc` (the module under test is the unpatched copy)."""

    def setUp(self):
        super().setUp()
        exe = shutil.which("tmux")
        if not exe:
            self.skipTest("tmux is not installed")
        out = subprocess.run([exe, "-V"], capture_output=True, text=True, encoding="utf-8").stdout
        ver = wab.parse_tmux_version(out)
        if ver is None or ver < (3, 2):
            self.skipTest(f"tmux {out.strip()!r} is older than 3.2")
        self.exe = exe
        self.sock = f"wabtest-{os.getpid()}-cc"
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        self.addCleanup(lambda: subprocess.run([exe, "-L", self.sock, "kill-server"],
                                               capture_output=True, env=self.env))
        self.w = load_orig("wab_cleanup")
        self.w.TMUX_SOCKET = self.sock
        self.w._send_telegram = lambda cfg, text: None  # nothing leaves the machine
        self.w.require_tmux = lambda: None

    def tm(self, *args):
        return subprocess.run([self.exe, "-L", self.sock, "-f", "/dev/null", *args], capture_output=True,
                              text=True, encoding="utf-8", env=self.env)

    def session(self, name, tag=None, run_dir=None, cmd="sleep 600", env=None):
        envs = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
        r = self.tm("new-session", "-d", "-s", name, "-x", "80", "-y", "24", *envs, cmd)
        self.assertEqual(r.returncode, 0, r.stderr)
        if tag:
            self.tm("set-option", "-t", f"={name}:", "@wab_run", tag)
            self.tm("set-option", "-p", "-t", f"={name}:", "@wab_run", tag)
        if run_dir:
            self.tm("set-option", "-t", f"={name}:", "@wab_run_dir", str(run_dir))
            self.tm("set-option", "-p", "-t", f"={name}:", "@wab_run_dir", str(run_dir))

    def alive_names(self):
        r = self.tm("list-sessions", "-F", "#{session_name}")
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def pane_ids(self):
        r = self.tm("list-panes", "-a", "-F", "#{pane_id}")
        return set(r.stdout.split()) if r.returncode == 0 else set()

    def cfg(self, **over):
        cfg, path = self.chain(**over)
        return self.w.load_chain(path), path

    def finish(self, cfg):
        (cfg["run_dir"] / "chain-result.md").write_text("done\n", encoding="utf-8")

    def state_two_waves(self, cfg, phase1="done", phase2="awaiting_merge"):
        self.put_state(cfg, {"current": "W2", "identity": self.w._pinned_identity(cfg), "waves": {
            "W1": self.wave_rec("W1", phase=phase1), "W2": self.wave_rec("W2", phase=phase2)}})

    def dash_pane(self, session, cfg, path, new_window=False):
        """A pane of a dashboard of the chain: @wab_run and @wab_open set at pane level."""
        if new_window:
            self.assertEqual(self.tm("new-window", "-t", f"={session}:", "sleep 600").returncode, 0)
        pane = self.tm("display-message", "-p", "-t", f"={session}:", "#{pane_id}").stdout.strip()
        self.tm("set-option", "-p", "-t", pane, "@wab_run", f"{cfg['chain']}/{cfg['run_id']}")
        self.tm("set-option", "-p", "-t", pane, "@wab_run_dir", str(cfg["run_dir"]))
        self.tm("set-option", "-p", "-t", pane, "@wab_open", self.w.wab_open_value(path))
        return pane

    # --- 1. done closes the chain's own wave sessions, only them
    def test_done_closes_own_wave_sessions_and_leaves_foreign_ones(self):
        cfg, path = self.cfg()
        tag = f"{CHAIN}/{RUN_ID}"
        self.state_two_waves(cfg)
        self.session("wv-w1", tag, cfg["run_dir"])
        self.session("wv-w2", tag, cfg["run_dir"])
        self.session("other-w1", "other/2026-01-01", "/nonexistent")  # a live chain of another run
        self.session("plain", None)                                    # not a wab session at all
        self.assertTrue(self.w.done_cmd(cfg))                          # exit 0: the window is gone
        self.assertEqual(self.alive_names(), {"other-w1", "plain"})
        self.assertIn("chain sessions closed", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    # --- 2. two chains in one tmux session: only the pane of the cleaned chain goes
    def test_cleanup_kills_only_its_dashboard_pane(self):
        cfg_a, path_a = self.cfg()
        doc = json.loads(path_a.read_text(encoding="utf-8"))
        doc["run_id"] = "2026-10-02"
        path_b = path_a.parent / "chain-b.json"
        path_b.write_text(json.dumps(doc), encoding="utf-8")
        cfg_b = self.w.load_chain(path_b)
        self.session("dashes", None)
        pane_a = self.dash_pane("dashes", cfg_a, path_a)
        pane_b = self.dash_pane("dashes", cfg_b, path_b, new_window=True)
        self.finish(cfg_a)
        self.put_state(cfg_a, {"current": None, "waves": {}})
        res = self.w.cleanup_cmd(cfg_a)
        self.assertEqual(res["panes"], [pane_a])
        self.assertEqual(self.pane_ids(), {pane_b})
        self.assertEqual(self.alive_names(), {"dashes"})

    # --- 2b. has-session fails after the kill for a reason other than «no such session»: not «closed»
    def test_a_failing_has_session_after_the_kill_is_not_a_closed_session(self):
        cfg, path = self.cfg()
        tag = f"{CHAIN}/{RUN_ID}"
        self.state_two_waves(cfg)
        self.session("wv-w1", tag, cfg["run_dir"])
        # a pane of someone else shares the session: it stays alive after our pane is killed
        self.assertEqual(self.tm("split-window", "-d", "-t", "=wv-w1:", "sleep 600").returncode, 0)
        foreign = [p for p in self.tm("list-panes", "-s", "-t", "=wv-w1:", "-F", "#{pane_id}").stdout.split()
                   if self.tm("show-options", "-p", "-t", p).stdout.strip() == ""]
        self.assertEqual(len(foreign), 1)
        self.finish(cfg)
        real, killed = self.w.tmux, []

        def flaky(*args, **kw):
            if args[:1] == ("kill-pane",):
                killed.append(args)
            if args[:1] == ("has-session",) and killed:  # the server is alive, the answer is lost
                return subprocess.CompletedProcess(args, 1, "", "error connecting to /tmp/x (Connection refused)\n")
            return real(*args, **kw)
        with mock.patch.object(self.w, "tmux", flaky):
            res = self.w.close_chain_sessions(cfg, json.loads((cfg["run_dir"] / "state.json").read_text(encoding="utf-8")))
        self.assertTrue(killed)
        self.assertEqual(res["sessions"], [])  # not confirmed: not claimed
        events = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("could not confirm what was closed (has-session failed)", events)
        self.assertNotIn("chain sessions closed", events)
        self.assertIn(foreign[0], self.pane_ids())  # the foreign pane lives

    def test_session_gone_tells_a_confirmed_absence_from_a_failure(self):
        def answer(rc, err):
            return mock.patch.object(self.w, "tmux", lambda *a, **kw: subprocess.CompletedProcess(a, rc, "", err))
        for rc, err, want in ((0, "", False),
                              (1, "can't find session: wv-w1\n", True),
                              (1, "no server running on /tmp/x\n", True),
                              (1, "error connecting to /tmp/x (No such file or directory)\n", True),
                              (1, "error connecting to /tmp/x (Connection refused)\n", None),
                              (1, "", None), (124, "timeout", None)):
            with answer(rc, err):
                self.assertIs(self.w._session_gone("wv-w1"), want, (rc, err))
        with mock.patch.object(self.w, "tmux", mock.Mock(side_effect=OSError("boom"))):
            self.assertIsNone(self.w._session_gone("wv-w1"))

    # --- 3. refusals and idempotence
    def test_cleanup_refused_with_a_live_dispatcher_or_without_chain_result(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.session("wv-w1", f"{CHAIN}/{RUN_ID}", cfg["run_dir"])
        with self.assertRaises(SystemExit) as ctx:  # not finished
            self.w.cleanup_cmd(cfg)
        self.assertIn("chain not finished", str(ctx.exception))
        self.finish(cfg)
        with wab._RunLock(cfg, "watch"):  # another module copy: a second descriptor on the lock file
            with self.assertRaises(SystemExit) as ctx:
                self.w.cleanup_cmd(cfg)
        self.assertIn("dispatcher of this chain is alive", str(ctx.exception))
        self.assertEqual(self.alive_names(), {"wv-w1"})
        first = self.w.cleanup_cmd(cfg)
        self.assertEqual(first["sessions"], ["wv-w1"])
        events = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        again = self.w.cleanup_cmd(cfg)  # a repeat: no error, nothing closed, no new event
        self.assertEqual((again["sessions"], again["panes"], again["warnings"]), ([], [], []))
        self.assertEqual((cfg["run_dir"] / "events.log").read_text(encoding="utf-8"), events)
        self.w.main(["wab.py", "cleanup", str(path)])  # the CLI too: exit 0

    def test_cleanup_cli_refusal_exits_nonzero(self):
        cfg, path = self.cfg()
        self.put_state(cfg, {"current": "W1", "waves": {}})
        with self.assertRaises(SystemExit) as ctx:
            self.w.main(["wab.py", "cleanup", str(path)])
        self.assertNotIn(ctx.exception.code, (0, None))

    # --- 4. a reused session name belongs to somebody else
    def test_a_session_name_reused_by_another_run_is_not_touched(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", "demo/2026-12-31", "/elsewhere")  # same prefix, another run
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual(res["sessions"], [])
        self.assertEqual(self.alive_names(), {"wv-w1"})
        self.assertIn("not ours", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    # --- 5. no option at all: a warning, not a kill
    def test_a_session_without_the_option_only_warns(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", None)
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual(res["sessions"], [])
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("wv-w1", res["warnings"][0])
        self.assertEqual(self.alive_names(), {"wv-w1"})

    def test_a_foreign_pane_in_our_session_stays_and_the_session_too(self):
        cfg, path = self.cfg()
        tag = f"{CHAIN}/{RUN_ID}"
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", tag, cfg["run_dir"])
        self.assertEqual(self.tm("new-window", "-t", "=wv-w1:", "sleep 600").returncode, 0)
        foreign = self.tm("display-message", "-p", "-t", "=wv-w1:", "#{pane_id}").stdout.strip()
        self.tm("set-option", "-p", "-t", foreign, "@wab_run", "other/1")
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual(res["sessions"], [])
        self.assertEqual(self.alive_names(), {"wv-w1"})
        self.assertEqual(self.pane_ids(), {foreign})

    # --- round 1: never kill-session; a failed pane listing closes nothing
    def test_a_failed_pane_listing_closes_nothing(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", f"{CHAIN}/{RUN_ID}", cfg["run_dir"])
        self.assertEqual(self.tm("new-window", "-t", "=wv-w1:", "sleep 600").returncode, 0)
        foreign = self.tm("display-message", "-p", "-t", "=wv-w1:", "#{pane_id}").stdout.strip()
        self.tm("set-option", "-p", "-t", foreign, "@wab_run", "other/1")
        before = self.pane_ids()
        real, calls = self.w.tmux, []

        def flaky(*args, **kw):
            calls.append(args)
            if args and args[0] == "list-panes":
                return subprocess.CompletedProcess(args, 1, "", "server busy")
            return real(*args, **kw)
        self.w.tmux = flaky
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual((res["sessions"], res["panes"]), ([], []))
        self.assertFalse([c for c in calls if c[0] in ("kill-session", "kill-pane")], calls)
        self.assertEqual(self.alive_names(), {"wv-w1"})
        self.assertEqual(self.pane_ids(), before)

    def test_a_marked_session_whose_panes_have_no_mark_only_warns(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", None)
        self.tm("set-option", "-t", "=wv-w1:", "@wab_run", f"{CHAIN}/{RUN_ID}")  # session level only
        self.tm("set-option", "-t", "=wv-w1:", "@wab_run_dir", str(cfg["run_dir"]))
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual((res["sessions"], res["panes"]), ([], []))
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("no @wab_run mark", res["warnings"][0])
        self.assertEqual(self.alive_names(), {"wv-w1"})

    # --- round 2: ownership = @wab_run AND @wab_run_dir; only confirmed closures are reported
    def test_two_runs_with_the_same_tag_and_session_name_are_told_apart_by_run_dir(self):
        cfg_a, path_a = self.cfg()
        doc = json.loads(path_a.read_text(encoding="utf-8"))
        doc["run_dir"] = str(self.tmp / "runs-b")  # same chain and run_id, another run directory
        path_b = path_a.parent / "chain-b.json"
        path_b.write_text(json.dumps(doc), encoding="utf-8")
        cfg_b = self.w.load_chain(path_b)
        self.assertNotEqual(cfg_a["run_dir"], cfg_b["run_dir"])
        self.state_two_waves(cfg_a)
        self.finish(cfg_a)
        tag = f"{CHAIN}/{RUN_ID}"
        self.session("wv-w1", tag, cfg_b["run_dir"])          # the wave session of the NEW run
        self.session("dashes", None)
        pane_b = self.dash_pane("dashes", cfg_b, path_a)       # its dashboard, opened with A's chain.json
        before = self.pane_ids()
        res = self.w.cleanup_cmd(cfg_a)
        self.assertEqual((res["sessions"], res["panes"]), ([], []))
        self.assertEqual(self.alive_names(), {"wv-w1", "dashes"})
        self.assertEqual(self.pane_ids(), before)
        self.assertIn(pane_b, self.pane_ids())

    def test_a_kill_pane_that_fails_is_not_reported_as_closed(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        self.finish(cfg)
        self.session("wv-w1", f"{CHAIN}/{RUN_ID}", cfg["run_dir"])
        self.session("dashes", None)
        self.dash_pane("dashes", cfg, path)
        real = self.w.tmux

        def failing(*args, **kw):
            if args and args[0] == "kill-pane":
                return subprocess.CompletedProcess(args, 1, "", "cannot kill")
            return real(*args, **kw)
        self.w.tmux = failing
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual((res["sessions"], res["panes"]), ([], []))
        self.assertEqual(self.alive_names(), {"wv-w1", "dashes"})
        events = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("kill-pane failed", events)
        self.assertNotIn("chain sessions closed", events)

    # --- round 3: /exit goes to OUR pane by id, never to the active pane of a shared session
    def shared_session(self, cfg):
        """wv-w2: our pane (%a) plus a foreign pane (the active one) that echoes what is typed."""
        tag = f"{CHAIN}/{RUN_ID}"
        self.session("wv-w2", tag, cfg["run_dir"], cmd="cat")
        ours = self.tm("display-message", "-p", "-t", "=wv-w2:", "#{pane_id}").stdout.strip()
        self.assertEqual(self.tm("new-window", "-t", "=wv-w2:", "cat").returncode, 0)
        foreign = self.tm("display-message", "-p", "-t", "=wv-w2:", "#{pane_id}").stdout.strip()
        self.tm("set-option", "-p", "-t", foreign, "@wab_run", "other/1")
        self.tm("set-option", "-p", "-t", foreign, "@wab_run_dir", "/elsewhere")
        self.assertNotEqual(ours, foreign)
        return ours, foreign

    def screen(self, pane):
        return self.tm("capture-pane", "-p", "-t", pane).stdout

    def test_done_with_a_foreign_pane_in_the_session_closes_ours_and_types_nothing_there(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        st = self.get_state(cfg)
        st["waves"]["W2"]["pending_exit"] = True
        self.put_state(cfg, st)
        ours, foreign = self.shared_session(cfg)
        typed = []
        self.w.clear_input = lambda name: typed.append(name)  # an empty input box: /exit may be typed
        self.w.EXIT_WAIT = 1
        self.assertTrue(self.w.done_cmd(cfg))
        self.assertNotIn(ours, self.pane_ids())
        self.assertIn(foreign, self.pane_ids())
        self.assertNotIn("/exit", self.screen(foreign))
        self.assertFalse(self.get_state(cfg)["waves"]["W2"].get("pending_exit"))
        self.assertTrue(self.w.done_cmd(cfg))  # again: still 0, still nothing typed there
        self.assertNotIn("/exit", self.screen(foreign))
        self.assertNotIn(foreign, typed)

    def test_close_window_types_exit_only_into_our_pane(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        st = self.get_state(cfg)
        st["waves"]["W2"]["pending_exit"] = True
        self.put_state(cfg, st)
        ours, foreign = self.shared_session(cfg)
        seen = []
        self.w.clear_input = lambda name: seen.append(name)
        st = self.get_state(cfg)
        self.assertTrue(self.w.close_window(cfg, st, st["waves"]["W2"]))
        time.sleep(0.3)
        self.assertEqual(seen, [ours])
        self.assertIn("/exit", self.screen(ours))
        self.assertNotIn("/exit", self.screen(foreign))

    def test_a_session_marked_by_another_run_gets_no_exit_and_the_intent_is_dropped(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        st = self.get_state(cfg)
        st["waves"]["W2"]["pending_exit"] = True
        self.put_state(cfg, st)
        self.session("wv-w2", "demo/2026-12-31", "/elsewhere", cmd="cat")
        self.w.clear_input = lambda name: None
        st = self.get_state(cfg)
        self.assertFalse(self.w.close_window(cfg, st, st["waves"]["W2"]))
        self.assertFalse(self.get_state(cfg)["waves"]["W2"].get("pending_exit"))
        pane = next(iter(self.pane_ids()))
        self.assertNotIn("/exit", self.screen(pane))

    # --- round 3: a session found alive by recovery gets its marks
    def test_recover_launch_does_not_mark_a_session_of_another_run(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="launching")}})
        st = self.get_state(cfg)
        with mock.patch.object(wab, "_ours", return_value="foreign"):
            wab.recover_launch(cfg, st, "W1")
        self.assertFalse([c for c in self.tmux_calls if c[1] == "set-option"])

    # --- round 3: stale chains are keyed by (tag, run_dir)
    def test_stale_chains_tells_runs_with_the_same_tag_apart_by_directory(self):
        cfg, path = self.cfg()
        d1, d2 = self.tmp / "old-1", self.tmp / "old-2"
        for d in (d1, d2):
            d.mkdir()
            (d / "chain-result.md").write_text("x", encoding="utf-8")
        tag = f"{CHAIN}/{RUN_ID}"  # the SAME tag as ours
        self.session("old1-w1", tag, d1)
        self.session("old2-w1", tag, d2)
        self.session("mine-w1", tag, cfg["run_dir"])
        found = self.w.stale_chains(cfg)
        self.assertEqual(sorted((f["run_dir"], tuple(f["targets"])) for f in found),
                         sorted([(str(d1.resolve()), ("old1-w1",)), (str(d2.resolve()), ("old2-w1",))]))

    # --- round 4: "could not read the marks" is not "no marks"
    def test_unreadable_marks_mean_no_input_no_kill_and_the_intent_stays(self):
        cfg, path = self.cfg()
        self.state_two_waves(cfg)
        st = self.get_state(cfg)
        st["waves"]["W2"]["pending_exit"] = True
        self.put_state(cfg, st)
        self.session("wv-w2", f"{CHAIN}/{RUN_ID}", cfg["run_dir"], cmd="cat")
        self.finish(cfg)
        real, calls = self.w.tmux, []

        def flaky(*args, **kw):
            calls.append(args)
            if args and args[0] == "show-options":
                return subprocess.CompletedProcess(args, 1, "", "server busy")
            return real(*args, **kw)
        self.w.tmux = flaky
        self.w.clear_input = lambda name: None
        st = self.get_state(cfg)
        self.assertFalse(self.w.close_window(cfg, st, st["waves"]["W2"]))
        self.assertTrue(self.get_state(cfg)["waves"]["W2"].get("pending_exit"))
        res = self.w.cleanup_cmd(cfg)
        self.assertEqual((res["sessions"], res["panes"]), ([], []))
        self.assertFalse([c for c in calls if c[0] in ("send-keys", "kill-pane", "kill-session")], calls)
        self.assertEqual(self.alive_names(), {"wv-w2"})

    # --- round 4: marks go on a pane by id, and only on a confirmed unmarked lone pane
    def test_recover_launch_never_marks_the_active_pane_of_an_already_marked_session(self):
        cfg, path = self.cfg()
        self.session("wv-w1", f"{CHAIN}/{RUN_ID}", cfg["run_dir"], cmd="cat")
        self.assertEqual(self.tm("new-window", "-t", "=wv-w1:", "cat").returncode, 0)
        foreign = self.tm("display-message", "-p", "-t", "=wv-w1:", "#{pane_id}").stdout.strip()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="launching")}})
        st = self.get_state(cfg)
        self.w.recover_launch(cfg, st, "W1")
        self.assertEqual(self.tm("show-options", "-p", "-t", foreign).stdout.strip(), "")

    def test_recover_launch_marks_nothing_in_an_unmarked_session_with_two_panes(self):
        cfg, path = self.cfg()
        self.session("wv-w1", None, cmd="cat")
        self.assertEqual(self.tm("new-window", "-t", "=wv-w1:", "cat").returncode, 0)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="launching")}})
        st = self.get_state(cfg)
        self.w.recover_launch(cfg, st, "W1")
        self.assertEqual(self.tm("show-options", "-t", "=wv-w1:").stdout.count("@wab_run"), 0)
        for pane in self.pane_ids():
            self.assertEqual(self.tm("show-options", "-p", "-t", pane).stdout.strip(), "")
        self.assertIn("not marked", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_recover_launch_marks_a_lone_unmarked_pane_by_id(self):
        cfg, path = self.cfg()
        self.session("wv-w1", None, cmd="cat", env={"WAB_DIR": str(self.w.wave_dir(cfg, "W1")), "WAB_WAVE": "W1"})
        pane = next(iter(self.pane_ids()))
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="launching")}})
        st = self.get_state(cfg)
        self.w.recover_launch(cfg, st, "W1")
        self.assertEqual(self.tm("show-options", "-p", "-v", "-t", pane, "@wab_run").stdout.strip(), f"{CHAIN}/{RUN_ID}")
        self.assertEqual(self.tm("show-options", "-v", "-t", "=wv-w1:", "@wab_run_dir").stdout.strip(), str(cfg["run_dir"]))

    def test_recover_launch_does_not_mark_a_lone_pane_whose_environment_is_not_this_waves(self):
        for env in (None, {"WAB_DIR": "/elsewhere/W1", "WAB_WAVE": "W1"}, "wrong-wave"):
            with self.subTest(env=env):
                self.tm("kill-server")
                cfg, path = self.cfg()
                if env == "wrong-wave":
                    env = {"WAB_DIR": str(self.w.wave_dir(cfg, "W1")), "WAB_WAVE": "W9"}
                self.session("wv-w1", None, cmd="cat", env=env)
                pane = next(iter(self.pane_ids()))
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="launching")}})
                st = self.get_state(cfg)
                self.w.recover_launch(cfg, st, "W1")
                self.assertEqual(self.tm("show-options", "-p", "-t", pane).stdout.strip(), "")
                self.assertEqual(self.tm("show-options", "-t", "=wv-w1:").stdout.count("@wab_run"), 0)
                self.assertIn("session environment is not this wave's",
                              (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    # --- 6. launch of the first wave warns about a FINISHED foreign chain only
    def test_stale_chains_lists_finished_chains_without_a_dispatcher_only(self):
        cfg, path = self.cfg()
        other = self.tmp / "other-run"
        live = self.tmp / "live-run"
        open_run = self.tmp / "open-run"
        for d in (other, live, open_run):
            d.mkdir()
        (other / "chain-result.md").write_text("x", encoding="utf-8")
        (live / "chain-result.md").write_text("x", encoding="utf-8")
        self.session("fin-w1", "fin/1", other)
        self.session("live-w1", "live/1", live)
        self.session("open-w1", "open/1", open_run)  # unfinished: no chain-result.md
        self.session("mine-w1", f"{CHAIN}/{RUN_ID}", cfg["run_dir"])  # our own run: never stale
        (live / "dispatcher.lock").write_text("", encoding="utf-8")
        with open(live / "dispatcher.lock", "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            found = self.w.stale_chains(cfg)
        tags = [f["tag"] for f in found]
        self.assertEqual(tags, ["fin/1"])
        self.assertFalse((other / "dispatcher.lock").exists())  # the check creates nothing

    def test_launch_of_the_first_wave_warns_and_closes_nothing(self):
        cfg, path = self.chain()
        self.alive = False
        found = [{"tag": "old/1", "run_dir": "/x", "targets": ["old-w1"], "chain_file": "/x/chain.json"}]
        prompt = self.tmp / "p.md"
        prompt.write_text("task\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                mock.patch.object(wab, "stale_chains", return_value=found), contextlib.redirect_stderr(err):
            wab.launch(cfg, "W1", prompt)
        self.assertIn("finished chain old/1 still has tmux", err.getvalue())
        self.assertIn("cleanup /x/chain.json", err.getvalue())
        self.assertFalse([c for c in self.tmux_calls if c[1] in ("kill-session", "kill-pane")])

    def test_launch_of_a_later_wave_does_not_look_for_stale_chains(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec("W1", phase="awaiting_merge")}})
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("task\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                mock.patch.object(wab, "stale_chains", return_value=[]) as stale:
            try:
                wab.launch(cfg, "W2", prompt)
            except SystemExit:
                pass
        stale.assert_not_called()

    # --- ownership marks
    def test_register_dash_marks_the_pane_with_the_run_and_unregister_clears_it(self):
        cfg, path = self.cfg()
        self.session("dashes", None)
        pane = self.tm("display-message", "-p", "-t", "=dashes:", "#{pane_id}").stdout.strip()
        self.assertTrue(self.w.register_dash(cfg, path, "dashes", self.sock, pane))
        show = lambda o: self.tm("show-options", "-p", "-v", "-t", pane, o).stdout.strip()
        self.assertEqual(show("@wab_run"), f"{CHAIN}/{RUN_ID}")
        self.assertEqual(show("@wab_run_dir"), str(cfg["run_dir"]))
        self.w.unregister_dash("dashes", self.sock, pane)
        self.assertEqual((show("@wab_run"), show("@wab_run_dir"), show("@wab_open")), ("", "", ""))

    def test_start_session_marks_the_session_and_its_pane(self):
        cfg, path = self.chain()
        self.alive = False
        st = {"current": "W1", "waves": {"W1": self.wave_rec("W1", sessions=["sid-1"], phase="launching")}}
        orig = wab.sh.side_effect

        def with_pane_id(*args, **kw):  # `new-session -P -F #{pane_id}` answers with the new pane's id
            r = orig(*args, **kw)
            if args[:2] == ("tmux", "new-session") or (len(args) > 3 and args[3] == "new-session"):
                return subprocess.CompletedProcess(args, 0, "%7\n", "")
            return r
        wab.sh.side_effect = with_pane_id
        wab.start_session(cfg, st, "W1")
        wab.sh.side_effect = orig
        sets = [c for c in self.tmux_calls if c[1] == "set-option"]
        self.assertTrue(any("%7" in c for c in sets), sets)  # the pane is marked by its id
        tag = f"{CHAIN}/{RUN_ID}"
        self.assertTrue(any("@wab_run" in c and tag in c and "-p" not in c for c in sets), sets)
        self.assertTrue(any("@wab_run" in c and tag in c and "-p" in c for c in sets), sets)
        self.assertTrue(any("@wab_run_dir" in c and str(cfg["run_dir"]) in c for c in sets), sets)


EVENTS = Path(__file__).resolve().parent.parent / "fixtures" / "events"


class DashEvents(unittest.TestCase):
    """«📜 События» for a human (#69): the type of an event -> a short Russian phrase, a folded pulse.
    The fixtures are LIVE logs (tests/fixtures/events/README.md); the lines marked EMITTER FORMAT below
    have no live sample and follow the event(...) calls of wab.py."""

    # live types the fixture is allowed to leave raw (none today; list a type here on purpose)
    RAW_ALLOWED = ()

    def setUp(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.dash = dash

    @staticmethod
    def _msgs(name):
        out = []
        for line in (EVENTS / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(line.partition(" ")[2].partition(" ")[2] if line[:2] == "20" else line)
        return out

    def test_every_live_line_has_a_human_phrase(self):
        raw = []
        total = 0
        for name in ("waves-finish-events.log", "waves-tails-events.log"):
            for msg in self._msgs(name):
                total += 1
                phrase, details, colour, known = self.dash.humanize_event(msg)
                if not known and not msg.startswith(self.RAW_ALLOWED):
                    raw.append(msg[:90])
        self.assertGreater(total, 200)
        self.assertEqual(raw, [], f"unrecognised event types in the live fixture: {raw[:5]}")

    def test_blocked_says_who_is_needed(self):
        h = self.dash.humanize_event
        phrase, details, colour, known = h(
            "W3: BLOCKED: [class=blocked_cap rec=owner red=no] blocked w3: кап 3/3 в прогоне r1")
        self.assertTrue(known)
        self.assertIn("W3", phrase)
        self.assertIn("твоего решения", phrase)
        self.assertIn("попыт", phrase)  # the cap: out of attempts to fix a review finding
        self.assertIn("red", colour)
        self.assertIsNotNone(details)
        self.assertIn("class=blocked_cap", details)
        phrase, _, _, _ = h("W3: BLOCKED: [class=needs_decision rec=invariant red=yes] x")
        self.assertIn("твоего решения", phrase)  # red=yes: the owner
        phrase, _, _, _ = h("W3: BLOCKED: [class=question rec=fix red=no] x")
        self.assertNotIn("твоего", phrase)  # a policy episode: not the owner
        self.assertIn("W3", phrase)
        phrase, _, _, _ = h("W3: BLOCKED: no label at all")
        self.assertIn("твоего", phrase)  # no label: it goes to the owner
        pulse, _, _, known = h("W3: phase=running ctx=199k restarts=0 status=BLOCKED: [class=blocked_cap rec=owner red=no] x")
        self.assertTrue(known)
        self.assertIn("твоего решения", pulse)

    def test_merge_and_checkpoint_phrases(self):
        h = self.dash.humanize_event
        cases = {
            "W1: merge gate passed, merge of PR #12 requested at abcdef123456": ("PR #12", "мердж"),
            "W1: PR #12 MERGED, base not known (PR: 'x', chain: 'y')": ("PR #12", "смерж"),
            "W1: merge gate waits: Codex не завершил ревью": ("гейт", "ждёт"),
            "W1: merge gate failed: Codex: 2 замечания P1": ("гейт", "не пройден"),
            "W2: context 250000 >= 240000, checkpoint requested": ("W2", "контрольн"),
            "W2: handoff ready, /clear + /update (restart #1)": ("W2", "перезапуск"),
            "W2: handoff ready, /clear + /superarmanda --resume (restart #2)": ("W2", "перезапуск"),  # EMITTER FORMAT
            "W2: new session 3ad76723-1140-4b67-9abd-b9b72531cf22 bound by marker": ("W2", "сесси"),
            "W2: policy auto-answer: class=needs_decision rec=invariant": ("W2", "автоответ"),
            "W2: policy cap reached (3/3): class=question rec=fix goes to the owner": ("W2", "лимит"),  # EMITTER FORMAT
            "W2: launched in tmux wf-w2, cwd /tmp/x": ("W2", "запущен"),
            "W2: DONE": ("W2", "готова"),
            "W2: DONE, awaiting merge by the coordinator": ("W2", "мердж"),  # EMITTER FORMAT
            "watch started, ctx_limit=240000": ("слежение", "начато"),
            "watch stopped: no current wave": ("слежение", "остановлено"),
            "chain finished": ("цепочка", "завершена"),
            "chain sessions closed: sessions ['a'], panes ['%1']": ("tmux", "закрыт"),  # EMITTER FORMAT
            "telegram: wave-autobot: стартовала волна W1.": ("уведомление", "отправлено"),
            "telegram FAILED (TimeoutExpired): wave-autobot: x": ("не доставлено", "telegram"),  # EMITTER FORMAT
            "display-message failed (OSError): x": ("не доставлено", "tmux"),  # EMITTER FORMAT
            "notify(skipped): wave-autobot: x": ("не настроен", "уведомл"),  # EMITTER FORMAT
            "W4: pane idle 15+ min, status=RUNNING": ("W4", "молчит"),
            "W4: idle nudge sent (15 min, no live background work)": ("W4", "толчок"),  # EMITTER FORMAT
            "W4: idle nudge dropped: the input holds another text (left untouched)": ("W4", "толчок"),  # EMITTER FORMAT
            "W2: фоновые хвосты в окне волны: pid 3217426 (30 мин без работы)": ("W2", "фонов"),
            "W2: дерево процессов недоступно: ps failed": ("W2", "дерево"),  # EMITTER FORMAT
            "W2: alarm: PR #7 checks completed and Codex finished at abcdef123456": ("PR #7", "проверки"),
            "W2: say: РЕШЕНИЕ КООРДИНАТОРА: вариант new_run": ("W2", "окно"),
            "admission: host policy, cc-autonomy prepare -> /tmp/x": ("клон", "допуск"),
            "W2: workdir /tmp/x detached on origin/main (abc)": ("W2", "рабоч"),
            "W2: window is not in auto mode": ("W2", "авто"),
            "W2: permission prompt on screen": ("W2", "разреш"),  # EMITTER FORMAT
            "W2: tmux session wf-w2 is gone (status=RUNNING)": ("W2", "закрылось"),  # EMITTER FORMAT
            "W2: owner-handover: PR #5 смержен владельцем (head abc, merge def)": ("PR #5", "смерж"),
        }
        for msg, needles in cases.items():
            phrase, details, colour, known = h(msg)
            self.assertTrue(known, msg)
            for n in needles:
                self.assertIn(n.lower(), phrase.lower(), (msg, phrase))
            self.assertLess(len(phrase), 200, msg)

    def test_unknown_line_is_raw_and_shortened(self):
        msg = "W9: something nobody has seen yet " + "x" * 400
        phrase, details, colour, known = self.dash.humanize_event(msg)
        self.assertFalse(known)
        self.assertTrue(phrase.startswith("W9: something nobody"))
        self.assertLessEqual(len(phrase), 140)
        self.assertIsNone(details)

    def test_secrets_are_masked_in_phrase_and_details(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        for msg in (f"W9: unknown thing with {token} inside",
                    f"W1: say: РЕШЕНИЕ: используй {token} для входа",
                    f"W1: BLOCKED: [class=question rec=fix red=no] {token} в цитате",
                    f"W1: phase=running ctx=1k restarts=0 status=BLOCKED: [class=question rec=fix red=no] {token}"):
            phrase, details, colour, known = self.dash.humanize_event(msg)
            self.assertNotIn(token, phrase + (details or ""), msg)
        phrase, details, _, _ = self.dash.humanize_event("W1: BLOCKED: [class=question rec=fix red=no] password=hunter2hunter2 тут")
        self.assertNotIn("hunter2", phrase + (details or ""))

    @staticmethod
    def _pulse(hh, ctx, status="RUNNING", phase="running", restarts=0, wave="W1"):
        return f"2026-10-04 {hh} {wave}: phase={phase} ctx={ctx}k restarts={restarts} status={status}"

    def test_identical_pulses_fold_into_one_line(self):
        lines = [self._pulse(f"10:0{i}:00Z", 100 + i) for i in range(6)]  # ctx grows: not a change
        items = self.dash.fold_events(lines)
        self.assertEqual(len(items), 1, items)
        self.assertIn("без изменений с 10:00 (×6)", items[0][1])

    def test_pulse_change_breaks_the_fold(self):
        lines = ([self._pulse(f"10:0{i}:00Z", 100) for i in range(3)]
                 + [self._pulse(f"10:1{i}:00Z", 100, status="BLOCKED: [class=question rec=fix red=no] q") for i in range(3)])
        items = self.dash.fold_events(lines)
        self.assertEqual(len(items), 2, items)
        self.assertIn("(×3)", items[0][1])
        self.assertIn("(×3)", items[1][1])
        # another event between pulses breaks the fold too; restarts is a change; another wave is one
        mixed = [self._pulse("10:00:00Z", 1), self._pulse("10:01:00Z", 1),
                 "2026-10-04 10:02:00Z W1: DONE", self._pulse("10:03:00Z", 1), self._pulse("10:04:00Z", 1)]
        self.assertEqual(len(self.dash.fold_events(mixed)), 3)
        self.assertEqual(len(self.dash.fold_events([self._pulse("10:00:00Z", 1), self._pulse("10:01:00Z", 1, restarts=1)])), 2)
        self.assertEqual(len(self.dash.fold_events([self._pulse("10:00:00Z", 1), self._pulse("10:01:00Z", 1, wave="W2")])), 2)

    def test_control_characters_never_reach_the_terminal(self):
        from rich.console import Console
        bad = "\x1b[31m red \x1b]0;title\x07 c1\x9b31m \x00 \x7f \x85"
        bad_chars = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
        lines = [f"2026-10-04 10:00:00Z W9: unknown thing {bad}",
                 f"2026-10-04 10:00:01Z W1: say: РЕШЕНИЕ {bad}",
                 f"2026-10-04 10:00:02Z W1: BLOCKED: [class=question rec=fix red=no] {bad}",
                 f"2026-10-04 10:00:03Z W1: phase=running ctx=1k restarts=0 status=BLOCKED: [class=question rec=fix red=no] {bad}",
                 f"no time at all {bad}"]
        for msg in (l[20:] for l in lines[:4]):
            phrase, details, _, _ = self.dash.humanize_event(msg)
            self.assertIsNone(bad_chars.search(phrase + (details or "")), (phrase, details))
        run_dir = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        (run_dir / "events.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        buf = io.StringIO()
        Console(file=buf, width=140, color_system=None).print(self.dash.events_panel({"run_dir": run_dir}))
        self.assertIsNone(re.search("[\x00-\x09\x0b-\x1f\x7f-\x9f]", buf.getvalue()), repr(buf.getvalue()))
        self.assertIn("unknown thing", buf.getvalue())  # the text itself stays

    def test_a_folded_series_shows_the_first_time_and_the_last_state(self):
        lines = [self._pulse(f"10:0{i}:00Z", i) for i in range(6)]  # ctx 0k..5k
        items = self.dash.fold_events(lines)
        self.assertEqual(len(items), 1)
        ts, phrase, details, _ = items[0]
        self.assertIn("без изменений с 10:00 (×6)", phrase)
        self.assertIn("ctx=5k", details)
        self.assertNotIn("ctx=0k", details)

    def test_a_secret_cut_by_the_length_limit_does_not_leak_a_prefix(self):
        secrets = ["ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
                   "sk-ant-api03-" + "Zy9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8Ba7",
                   "password=hunter2hunter2hunter2xyz"]

        def leaks(secret, *texts):
            out = " ".join(t or "" for t in texts)
            value = secret.split("=", 1)[-1] if secret.startswith("password=") else secret
            return [value[i:i + 9] for i in range(len(value) - 8) if value[i:i + 9] in out]

        for secret in secrets:
            for start in range(100, 140, 5):  # the secret crosses the 140 border of a raw line
                msg = "W9: " + "x" * (start - 4) + " " + secret + " tail"
                phrase, details, _, known = self.dash.humanize_event(msg)
                self.assertEqual(leaks(secret, phrase, details), [], (secret, start, phrase))
            for start in range(100, 200, 7):  # ... and the 160 border of the details / the BLOCKED question
                for msg in (f"W1: BLOCKED: [class=question rec=fix red=no] {'q' * start} {secret} tail",
                            f"W1: say: {'q' * start} {secret} tail",
                            f"W1: merge gate failed: {'q' * start} {secret} tail"):
                    phrase, details, _, _ = self.dash.humanize_event(msg)
                    self.assertEqual(leaks(secret, phrase, details), [], (secret, start, msg[:30], details))

    def test_a_long_run_of_pulses_does_not_push_the_event_before_it_out(self):
        from rich.console import Console
        run_dir = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        lines = ["2026-10-04 09:59:00Z W1: launched in tmux wf-w1, cwd /tmp/x"]
        for i in range(1000):
            sec = i % 60
            lines.append(self._pulse(f"10:{i // 60 % 60:02d}:{sec:02d}Z", 100 + i))
        (run_dir / "events.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        buf = io.StringIO()
        Console(file=buf, width=140, color_system=None).print(self.dash.events_panel({"run_dir": run_dir}, n=12))
        out = buf.getvalue()
        self.assertIn("запущен", out)
        self.assertIn("без изменений с 10:00 (×1000)", out)

    def test_an_invisible_character_inside_a_secret_does_not_hide_it(self):
        # one representative of every class of wab._INVISIBLE: Cf (zero width, joiner, word joiner, BOM, soft
        # hyphen, tag), Zl, Hangul fillers, variation selector, CGJ, an unassigned default-ignorable one, bidi
        chars = ["\u200b", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u2028", "\u3164", "\u115f", "\U000e0001",
                 "\ufe0f", "\u034f", "\u2065", "\u202e", "\u2029", "\x00", "\x9b"]
        token = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        for c in chars:
            cases = [("password" + c + "=hunter2hunter2", "hunter2"),
                     ("ghp_" + token[:18] + c + token[18:], token[:9]),
                     ("sk-ant-api03-" + token[:12] + c + token[12:], token[14:23])]
            for secret, fragment in cases:
                for msg in (f"W1: say: {secret}", f"W9: unknown thing {secret} tail",
                            f"W1: BLOCKED: [class=question rec=fix red=no] вопрос {secret}",
                            f"W1: phase=running ctx=1k restarts=0 status=BLOCKED: [class=question rec=fix red=no] {secret}",
                            f"W1: merge gate failed: {secret}"):
                    phrase, details, _, _ = self.dash.humanize_event(msg)
                    out = phrase + " " + (details or "")
                    self.assertNotIn(fragment, out, (hex(ord(c[0])), msg[:40], out))
                    self.assertNotIn("hunter2", out, (hex(ord(c[0])), msg[:40], out))

    def test_wave_names_are_the_dispatchers_names(self):
        h = self.dash.humanize_event
        for wave in ("fix-input", "1", "w2-resume", "9a"):
            phrase, details, _, known = h(f"{wave}: BLOCKED: [class=blocked_cap rec=owner red=no] x")
            self.assertTrue(known, wave)
            self.assertIn(wave, phrase)
            self.assertIn("твоего решения", phrase)
            items = self.dash.fold_events([self._pulse(f"10:0{i}:00Z", i, wave=wave) for i in range(6)])
            self.assertEqual(len(items), 1, (wave, items))
            self.assertIn("(×6)", items[0][1])
            self.assertIn(wave, items[0][1])
        mixed = [self._pulse("10:00:00Z", 1, wave="fix-a"), self._pulse("10:01:00Z", 1, wave="fix-b"),
                 self._pulse("10:02:00Z", 1, wave="fix-a")]
        self.assertEqual(len(self.dash.fold_events(mixed)), 3)  # waves with a hyphen are not glued together

    def test_a_record_is_split_only_at_a_newline(self):
        # str.splitlines also cuts at U+2028/2029, NEL, VT, FF, FS/GS/RS: «password<sep>=hunter2…» fell in two and
        # the second half was shown unmasked. A record of events.log ends at \n (and a CRLF's \r), nowhere else.
        from rich.console import Console
        seps = ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r"]
        lines = []
        for i, sep in enumerate(seps):
            lines.append(f"2026-10-04 10:00:{i:02d}Z W1: say: password{sep}=hunter2hunter2 хвост «Привет»")
        run_dir = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        path = run_dir / "events.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
        buf = io.StringIO()
        Console(file=buf, width=200, color_system=None).print(self.dash.events_panel({"run_dir": run_dir}, n=20))
        self.assertNotIn("hunter2", buf.getvalue())
        self.assertEqual(buf.getvalue().count("в окно волны написали"), len(seps))  # one record each, none split
        for block in (7, 50, 64 * 1024):  # a block border may fall anywhere, in the middle of a UTF-8 character too
            items = self.dash.tail_items(path, 20, block=block)
            self.assertEqual(len(items), len(seps), block)
            self.assertNotIn("hunter2", " ".join(f"{i[1]} {i[2]}" for i in items), block)
        crlf = run_dir / "crlf.log"
        crlf.write_bytes(b"2026-10-04 10:00:00Z W1: DONE\r\n2026-10-04 10:00:01Z chain finished\r\n")
        self.assertEqual([i[1] for i in self.dash.tail_items(crlf, 5)], ["✔ W1 готова", "🏁 цепочка завершена"])

    def test_events_panel_folds_before_it_cuts_the_tail(self):
        from rich.console import Console
        run_dir = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        lines = ["2026-10-04 09:00:00Z W1: launched in tmux wf-w1, cwd /tmp/x"]
        lines += [self._pulse(f"10:{i // 60:02d}:{i % 60:02d}Z", 100 + i) for i in range(50)]
        (run_dir / "events.log").write_text("\n".join(lines) + "\nbroken line without a time\n", encoding="utf-8")
        buf = io.StringIO()
        Console(file=buf, width=120, color_system=None).print(self.dash.events_panel({"run_dir": run_dir}, n=12))
        out = buf.getvalue()
        self.assertIn("запущен", out)  # 50 pulses did not push the launch out of the last 12
        self.assertIn("×50", out)
        self.assertIn("broken line", out)

    def test_events_panel_renders_on_the_live_fixture(self):
        from rich.console import Console
        run_dir = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        shutil.copy(EVENTS / "waves-finish-events.log", run_dir / "events.log")
        buf = io.StringIO()
        Console(file=buf, width=120, color_system=None).print(self.dash.events_panel({"run_dir": run_dir}, n=30))
        self.assertIn("События", buf.getvalue())
        empty = Path(tempfile.mkdtemp(prefix="wabtest-"))
        self.addCleanup(shutil.rmtree, empty, True)
        buf = io.StringIO()
        Console(file=buf, width=120, color_system=None).print(self.dash.events_panel({"run_dir": empty}))
        self.assertIn("событий пока нет", buf.getvalue())


# ---------------------------------------------------------------- #76: the manifest of the CURRENT run
TWO_RUNS = ROOT / "tests" / "fixtures" / "waves" / "two-runs" / "W1"


class CurrentRunManifest(Base):
    """The merge gate, the dashboard and `wab.py status` read the manifest of the wave's last run
    (`runs.json`), not a fixed path. Fixture: the live wave W1 of waves-tails (two runs)."""

    def setUp(self):
        super().setUp()
        self.cfg, self.path = self.chain(waves=["W1"], merge_gate="auto")
        self.wave = self.cfg["run_dir"] / "W1"
        self.wave.mkdir(parents=True, exist_ok=True)
        self.wave = self.wave.resolve()
        self.repo = self.tmp / "gitrepo"
        self.repo.mkdir()
        git = ["git", "-C", str(self.repo), "-c", "user.email=a@b", "-c", "user.name=n"]
        subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        (self.wave / "superarmanda").mkdir()
        for name in ("manifest.json", "manifest-run2.json"):
            text = (TWO_RUNS / "superarmanda" / name).read_text(encoding="utf-8").replace("@REPO@", str(self.repo))
            (self.wave / "superarmanda" / name).write_text(text, encoding="utf-8")
        self.runs = (TWO_RUNS / "runs.json").read_text(encoding="utf-8").replace("@WAVE_DIR@", str(self.wave))
        (self.wave / "runs.json").write_text(self.runs, encoding="utf-8")
        self.r1 = json.loads((self.wave / "superarmanda" / "manifest.json").read_text(encoding="utf-8"))
        self.r2 = json.loads((self.wave / "superarmanda" / "manifest-run2.json").read_text(encoding="utf-8"))

    # ----- the gate over fixtures: only GitHub and the working copy are faked -----
    def gate(self, manifest=None):
        manifest = manifest or self.r2
        head = manifest["head"]
        facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": head, "base": "main"},
                            reviews=[{"user": BOT, "commit_id": head, "state": "COMMENTED"}])
        work = {"clean": True, "head": head, "fingerprint": manifest["tree_fingerprint"]}
        pr = {"number": 7, "headRefOid": head, "isDraft": False, "state": "OPEN"}
        with mock.patch.object(wab, "find_pr", return_value=pr), \
                mock.patch.object(wab, "base_branch_of", return_value="main"), \
                mock.patch.object(wab, "gate_facts", return_value=facts), \
                mock.patch.object(wab, "workdir_state", return_value=work):
            return wab.gate_check(self.cfg, "W1", {"cwd": self.cwd})

    def set_runs(self, doc):
        (self.wave / "runs.json").write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")

    def runs_doc(self):
        return json.loads(self.runs)

    def std(self):
        return self.wave / "superarmanda" / "manifest.json"

    def assertRefused(self, why=None):
        v = self.gate()
        self.assertEqual(v["verdict"], "fail", v)
        self.assertTrue(v["reasons"] and v["reasons"][0].startswith("manifest: "), v["reasons"])
        if why:
            self.assertIn(why, v["reasons"][0])
        return v

    def assertBlockedR1(self, v):
        self.assertEqual(v["verdict"], "fail")
        self.assertTrue(any("blocked" in r for r in v["reasons"]), v["reasons"])

    # ----- a) two runs: the gate judges r2 -----
    def test_gate_judges_the_last_run(self):
        v = self.gate()
        self.assertEqual(v["verdict"], "pass", v)
        self.assertFalse([r for r in v["reasons"] if r.startswith("manifest")], v)

    def test_the_first_run_alone_is_blocked(self):  # what 1.1.0 saw for this wave
        doc = self.runs_doc()
        doc["runs"] = doc["runs"][:1]
        self.set_runs(doc)
        self.assertBlockedR1(self.gate(self.r1))

    # ----- b) no runs.json, or one record: as in 1.1.0 -----
    def test_without_runs_json_the_standard_path_is_read(self):
        (self.wave / "runs.json").unlink()
        self.assertBlockedR1(self.gate(self.r1))  # manifest.json is r1 here
        self.std().write_text(json.dumps(self.r2), encoding="utf-8")
        self.assertEqual(self.gate()["verdict"], "pass")
        self.assertEqual(wab.current_manifest(self.cfg, "W1"), (self.std(), None))

    def test_one_record_is_the_standard_path_of_that_record(self):
        doc = self.runs_doc()
        doc["runs"] = doc["runs"][:1]
        self.set_runs(doc)
        self.assertBlockedR1(self.gate(self.r1))
        self.std().write_text(json.dumps(self.r2), encoding="utf-8")
        self.assertEqual(self.gate()["verdict"], "pass")

    def test_empty_runs_list_is_the_standard_path(self):
        doc = self.runs_doc()
        doc["runs"] = []
        self.set_runs(doc)
        self.assertEqual(wab.current_manifest(self.cfg, "W1"), (self.std(), None))

    def test_no_manifest_at_all_is_not_an_error_of_the_function(self):
        (self.wave / "runs.json").unlink()
        self.std().unlink()
        self.assertEqual(wab.current_manifest(self.cfg, "W1"), (self.std(), None))
        self.assertIsNone(wab.read_manifest(self.cfg, "W1"))

    # ----- c) closed refusals, never a quiet fallback to manifest.json -----
    def test_damaged_runs_json_is_refused(self):
        for text in ("{not json", "[]", "null", json.dumps({"version": 2, "wave": "W1", "runs": []}),
                     json.dumps({"version": 1, "wave": 1, "runs": []}),
                     json.dumps({"version": 1, "wave": "W1", "runs": {}}),
                     json.dumps({"version": 1, "wave": "W1", "runs": [1]})):
            with self.subTest(text=text):
                self.set_runs(text)
                v = self.assertRefused()
                self.assertNotIn("not json", v["reasons"][0])  # a reason, not the file's content

    def test_runs_json_that_is_not_utf8_is_refused(self):
        (self.wave / "runs.json").write_bytes(b"\xff\xfe\x00")
        self.assertRefused()

    def test_runs_json_fifo_without_writer_is_refused_and_does_not_hang(self):
        import signal
        (self.wave / "runs.json").unlink()
        os.mkfifo(self.wave / "runs.json")

        def boom(*a):
            raise AssertionError("current_manifest hangs on a FIFO runs.json")
        old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(20)
        try:
            path, why = wab.current_manifest(self.cfg, "W1")
            self.assertIsNone(path)
            self.assertIn("не обычный файл", why)
            self.assertRefused()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_runs_json_symlink_is_refused(self):
        real = self.wave / "real-runs.json"
        (self.wave / "runs.json").rename(real)
        (self.wave / "runs.json").symlink_to(real)
        self.assertRefused()

    def test_huge_runs_json_is_refused(self):
        (self.wave / "runs.json").write_bytes(b" " * (2 * 1024 * 1024 + 10))
        self.assertRefused("слишком большой")

    def test_a_refusal_keeps_the_other_reasons_even_on_an_early_wait(self):
        self.set_runs("{not json")
        head = self.r2["head"]
        facts = green_facts(pr={"state": "open", "merged": False, "draft": False, "head": head, "base": "main"},
                            check_runs=[{"name": "ci", "status": "in_progress", "conclusion": None}])
        pr = {"number": 7, "headRefOid": head, "isDraft": False, "state": "OPEN"}
        with mock.patch.object(wab, "find_pr", return_value=pr), \
                mock.patch.object(wab, "base_branch_of", return_value="main"), \
                mock.patch.object(wab, "gate_facts", return_value=facts), \
                mock.patch.object(wab, "workdir_state", return_value={"clean": True, "head": head, "fingerprint": "x"}):
            v = wab.gate_check(self.cfg, "W1", {"cwd": self.cwd})
        self.assertEqual(v["verdict"], "fail", v)
        self.assertTrue(v["reasons"][0].startswith("manifest: "), v["reasons"])
        self.assertTrue(any("проверки не завершены" in r for r in v["reasons"]), v["reasons"])
        self.assertNotIn(gate.MANIFEST_MISSING, v["reasons"])

    def test_a_refusal_keeps_merged_outside(self):
        self.set_runs("{not json")
        head = self.r2["head"]
        facts = green_facts(pr={"state": "closed", "merged": True, "draft": False, "head": head, "base": "main"})
        pr = {"number": 7, "headRefOid": head, "isDraft": False, "state": "OPEN"}
        with mock.patch.object(wab, "find_pr", return_value=pr), \
                mock.patch.object(wab, "base_branch_of", return_value="main"), \
                mock.patch.object(wab, "gate_facts", return_value=facts), \
                mock.patch.object(wab, "workdir_state", return_value={"clean": True, "head": head, "fingerprint": "x"}):
            v = wab.gate_check(self.cfg, "W1", {"cwd": self.cwd})
        self.assertEqual(v["verdict"], "fail")
        self.assertIn(gate.MERGED_OUTSIDE, v["reasons"])

    def test_a_directory_instead_of_runs_json_is_refused(self):
        (self.wave / "runs.json").unlink()
        (self.wave / "runs.json").mkdir()
        self.assertRefused()

    def test_runs_json_of_another_wave_is_refused(self):
        doc = self.runs_doc()
        doc["wave"] = "W2"
        self.set_runs(doc)
        self.assertRefused()

    def test_a_record_pointing_to_a_missing_file_is_refused(self):
        (self.wave / "superarmanda" / "manifest-run2.json").unlink()
        self.assertRefused("прогона 2")

    def test_a_record_without_a_usable_manifest_field_is_refused(self):
        for bad in (None, "", 5, ["x"], "manifest-run2.json", "superarmanda/manifest-run2.json"):
            with self.subTest(manifest=bad):
                doc = self.runs_doc()
                doc["runs"][-1]["manifest"] = bad
                self.set_runs(doc)
                self.assertRefused()

    def test_a_path_with_nul_or_a_lone_surrogate_is_refused(self):
        for bad in (f"{self.wave}/superarmanda/\u0000x", "/tmp/\udc80x", f"{self.wave}/\udc80"):
            with self.subTest(manifest=bad):
                doc = self.runs_doc()
                doc["runs"][-1]["manifest"] = bad
                self.set_runs(json.dumps(doc, ensure_ascii=True))
                path, why = wab.current_manifest(self.cfg, "W1")  # must not raise
                self.assertIsNone(path)
                self.assertTrue(why)
                self.assertRefused()
                if "\u0000" in bad:
                    self.assertIn("не разрешается", why)

    def test_a_wave_name_with_nul_does_not_raise(self):
        self.assertEqual(wab.current_manifest(self.cfg, "W1\u0000")[0].name, "manifest.json")

    def test_deeply_nested_runs_json_is_refused_not_raised(self):
        (self.wave / "runs.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        path, why = wab.current_manifest(self.cfg, "W1")
        self.assertIsNone(path)
        self.assertTrue(why)
        self.assertRefused()

    def test_deeply_nested_runs_json_in_a_valid_envelope_is_refused(self):
        text = '{"version": 1, "wave": "W1", "runs": [{"x": ' + "[" * 100000 + "]" * 100000 + "}]}"
        self.set_runs(text)
        self.assertRefused()

    def test_broken_utf8_in_runs_json_is_refused(self):
        (self.wave / "runs.json").write_bytes(b'{"version": 1, "wave": "W1", "runs": ["\xc3\x28"]}')
        self.assertRefused()

    def test_oversized_runs_json_is_refused_in_the_gate(self):
        (self.wave / "runs.json").write_bytes(b" " * (wab.RUNS_LIMIT + 1))
        self.assertRefused("слишком большой")

    def test_current_manifest_never_raises(self):  # the invariant: any failure of a step is a refusal
        steps = ((wab.json, "loads", KeyError("x")), (wab.os, "open", RuntimeError("x")),
                 (wab.os, "fstat", MemoryError("x")), (wab.os, "read", ValueError("x")),
                 (wab.pathlib.Path, "resolve", AttributeError("x")), (wab.os.path, "isabs", TypeError("x")))
        for mod, name, exc in steps:
            with self.subTest(step=name):
                with mock.patch.object(mod, name, side_effect=exc):
                    path, why = wab.current_manifest(self.cfg, "W1")
                self.assertIsNone(path)
                self.assertTrue(why)
                self.assertNotIn("Error", why)  # a reason, not the text of the exception

    def test_a_last_record_that_is_not_an_object_is_refused(self):
        doc = self.runs_doc()
        doc["runs"][-1] = "x"
        self.set_runs(doc)
        self.assertRefused()

    def test_a_path_outside_the_wave_directory_is_refused(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(self.r2), encoding="utf-8")
        doc = self.runs_doc()
        doc["runs"][-1]["manifest"] = str(outside)
        self.set_runs(doc)
        self.assertRefused("вне каталога волны")
        other = self.cfg["run_dir"] / "W2" / "superarmanda"  # a neighbour wave is outside too
        other.mkdir(parents=True)
        (other / "manifest.json").write_text(json.dumps(self.r2), encoding="utf-8")
        doc["runs"][-1]["manifest"] = str(other / "manifest.json")
        self.set_runs(doc)
        self.assertRefused("вне каталога волны")

    def test_dotdot_out_of_the_wave_directory_is_refused(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(self.r2), encoding="utf-8")
        doc = self.runs_doc()
        doc["runs"][-1]["manifest"] = f"{self.wave}/superarmanda/../../../../{outside.name}"
        self.set_runs(doc)
        self.assertRefused()

    def test_a_symlink_file_pointing_outside_is_refused(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(self.r2), encoding="utf-8")
        link = self.wave / "superarmanda" / "manifest-run2.json"
        link.unlink()
        link.symlink_to(outside)
        self.assertRefused("вне каталога волны")

    def test_a_symlink_directory_pointing_outside_is_refused(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "manifest-run2.json").write_text(json.dumps(self.r2), encoding="utf-8")
        shutil.rmtree(self.wave / "superarmanda")
        (self.wave / "superarmanda").symlink_to(outside)
        self.std().write_text(json.dumps(self.r1), encoding="utf-8")
        self.assertRefused("вне каталога волны")

    def test_a_directory_is_not_a_manifest(self):
        target = self.wave / "superarmanda" / "manifest-run2.json"
        target.unlink()
        target.mkdir()
        self.assertRefused()

    def test_a_symlink_inside_the_wave_directory_is_fine(self):
        real = self.wave / "superarmanda" / "real.json"
        link = self.wave / "superarmanda" / "manifest-run2.json"
        link.rename(real)
        link.symlink_to(real)
        self.assertEqual(self.gate()["verdict"], "pass")

    def test_the_reason_is_short_and_has_no_file_content(self):
        self.set_runs('{"secret-token-123": ')
        v = self.assertRefused()
        self.assertNotIn("secret-token-123", " ".join(v["reasons"]))
        self.assertLess(len(v["reasons"][0]), 200)

    # ----- d) the dashboard and `wab.py status` show r2 -----
    def dash(self):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        dash.MANIFEST_CACHE.clear()
        return dash

    def test_dashboard_shows_the_last_run(self):
        dash = self.dash()
        where = dash.manifest_where(self.cfg, "W1")
        self.assertEqual(where.get("run"), "2/2", where)
        joined = "\n".join(t.plain for t in dash.manifest_lines(self.cfg, "W1", self.wave_rec()))
        self.assertIn("прогон 2/2", joined)
        self.assertNotIn("1/2", joined)

    def test_dashboard_refusal_is_one_line_with_the_reason(self):
        dash = self.dash()
        self.set_runs("{not json")
        self.assertIn("error", dash.manifest_where(self.cfg, "W1"))
        joined = "\n".join(t.plain for t in dash.manifest_lines(self.cfg, "W1", self.wave_rec()))
        self.assertIn("manifest: ", joined)
        self.assertNotIn("прогон", joined)

    def test_dashboard_without_any_manifest_is_none(self):
        dash = self.dash()
        (self.wave / "runs.json").unlink()
        self.std().unlink()
        self.assertIsNone(dash.manifest_where(self.cfg, "W1"))

    def test_dashboard_wave_name_with_nul_and_no_runs_json_does_not_raise(self):  # W1 remainder (a), #81
        dash = self.dash()
        (self.wave / "runs.json").unlink()
        self.assertIsNone(dash.manifest_where(self.cfg, "W1\u0000"))

    def test_the_gate_chooses_the_manifest_once(self):  # W1 remainder (c), #81
        real = wab._current_manifest  # the reading of runs.json itself
        for label, setup in (("a pass", lambda: None), ("a refusal", lambda: self.set_runs("{not json"))):
            setup()
            calls = []
            with mock.patch.object(wab, "_current_manifest", side_effect=lambda *a: (calls.append(a), real(*a))[1]):
                self.gate()
            self.assertEqual(len(calls), 1, f"{label}: {calls}")

    def status_text(self):
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wab.status_cmd(self.cfg)
        return buf.getvalue()

    def test_status_shows_the_last_run(self):
        text = self.status_text()
        self.assertIn("прогон 2/2", text)
        self.assertNotIn("1/2", text)
        self.assertIn("шаг", text)
        self.assertIn("вердикты", text)

    def live_cap_setup(self):
        (self.wave / "max-runs").write_text("3\n", encoding="utf-8")
        other = self.tmp / "other-wave"
        other.mkdir()
        (other / "max-runs").write_text("5\n", encoding="utf-8")
        return other

    def test_status_judges_the_run_by_the_cap_of_this_wave(self):
        other = self.live_cap_setup()
        for env in ({}, {"WAB_DIR": str(other)}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("WAB_DIR", None)
                text = self.status_text()
                self.assertIn("прогон 2/3 ", text)
                self.assertNotIn("2/3!", text)

    def test_dashboard_judges_the_run_by_the_cap_of_this_wave(self):
        dash = self.dash()
        other = self.live_cap_setup()
        for env in ({}, {"WAB_DIR": str(other)}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("WAB_DIR", None)
                dash.MANIFEST_CACHE.clear()
                where = dash.manifest_where(self.cfg, "W1")
                self.assertEqual((where.get("run"), where.get("last_run")), ("2/3", False), where)

    def test_status_shows_the_refusal_and_survives_it(self):
        self.set_runs("{not json")
        text = self.status_text()
        self.assertIn("manifest: ", text)
        self.assertIn("W1: tmux=", text)

    def test_status_survives_a_failing_where(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
            text = self.status_text()
        self.assertIn("W1: tmux=", text)
        self.assertIn("manifest: ", text)

    def test_status_of_a_wave_without_manifest_has_no_manifest_line(self):
        (self.wave / "runs.json").unlink()
        self.std().unlink()
        self.assertNotIn("manifest", self.status_text())

    # ----- e) no other reader of the standard path -----
    def test_the_standard_path_literal_lives_only_in_the_shared_function(self):
        hits = {}
        for name in ("wab.py", "gate.py", "dash.py"):
            src = (WAVES / name).read_text(encoding="utf-8")
            hits[name] = len(re.findall(r"""["']manifest\.json["']""", src))
        self.assertEqual(hits, {"wab.py": 1, "gate.py": 0, "dash.py": 0})
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        body = src[src.index("def _current_manifest("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertRegex(body, r"""["']manifest\.json["']""")

    def test_dash_does_not_build_the_manifest_path_itself(self):
        src = (WAVES / "dash.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"""/\s*["']superarmanda["']""")


# ---------------------------------------------------------------- W2 (#81): one way to mask text for a human
REDACT_FIXTURES = ROOT / "tests" / "fixtures" / "redact"
INVISIBLE_CLASSES = {  # one family per class that must never glue or split a secret past the mask
    "zero-width U+200B": "​", "zero-width U+200D": "‍", "zero-width U+2060": "⁠",
    "zero-width U+FEFF": "﻿", "variation selector U+FE0F": "️",
    "variation selector U+E0100": "\U000e0100", "lone CR": "\r",
    "default-ignorable U+034F": "͏", "default-ignorable U+3164": "ㅤ",
    "default-ignorable U+115F": "ᅟ", "default-ignorable U+2065": "⁥",
    "default-ignorable U+00AD": "­",
}
# what str.split() / splitlines() cut at besides ASCII whitespace: a secret torn by them was shown by its tail (tester-3)
SPLIT_CLASSES = {f"split U+{ord(c):04X}": c for c in ["\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\u2028", "\u2029"]}
# the Unicode spaces (Zs) are separators like a plain space (W3Quote pins it): not here
HEX40 = "9f3a7c1e5b2d4086a1c7e93f5d20b84c6a17e9d3"
B64 = "Zm9vYmFyU2VjcmV0S2V5VmFsdWUxMjM0NTY3ODkwYWJjZGVm"


def secret_shapes(i):
    """name -> (the secret as written, the visible values: no 4 characters of them may reach the output)."""
    half = len(HEX40) // 2
    return {
        "key (password<i>=value)": (f"password{i}=Hunter2SecretValue99", ["Hunter2SecretValue99"]),
        "key (pass<i>word=value)": (f"pass{i}word=Hunter2SecretValue99", ["Hunter2SecretValue99"]),
        "value (password=sec<i>ret)": (f"password=Hunter2Sec{i}retValue99", ["Hunter2Sec", "retValue99"]),
        "prefix sk-": (f"s{i}k-ant-api03-AbCdEfGhIjKl1234567890", ["AbCdEfGhIjKl1234567890"]),
        "prefix sk-<i>": (f"sk-{i}ant-api03-AbCdEfGhIjKl1234567890", ["AbCdEfGhIjKl1234567890"]),
        "prefix ghp_": (f"gh{i}p_AbCd1234EfGh5678IjKl9012MnOp3456", ["AbCd1234EfGh5678IjKl9012MnOp3456"]),
        "prefix xoxb-": (f"xox{i}b-1234567890-AbCdEfGhIjKlMn", ["1234567890-AbCdEfGhIjKlMn"]),
        "long hex": (HEX40[:half] + i + HEX40[half:], [HEX40[:half], HEX40[half:]]),
        "base64": (B64[:24] + i + B64[24:], [B64[:24], B64[24:]]),
    }


def live_forms():
    return [l for l in (REDACT_FIXTURES / "live-forms.txt").read_text(encoding="utf-8").splitlines() if l.strip()]


def leaked(out, values):
    """The first 4-character piece of a secret value that is visible in `out`, else None."""
    for v in values:
        for k in range(len(v) - 3):
            if v[k:k + 4] in out:
                return v[k:k + 4]
    return None


def safe_text_fn():
    """wab.safe_text; on a tree without it (the red run before the change) the old `_clean_redact`."""
    return getattr(wab, "safe_text", None) or (lambda t, limit, owner_paths=False: wab._clean_redact(t, limit, owner_paths))


def redact_uses_outside(waves_dir, allowed=("safe_text",)):
    """[(file, line, function)]: every mention of redact / _clean_redact in scripts/waves/*.py that is not
    inside one of the `allowed` functions. (The definition of redact is no mention: a FunctionDef has no Name.)"""
    import ast
    found = []
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name  # the outermost function: a helper inside safe_text is part of it
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in ("redact", "_clean_redact") and func not in allowed:
                found.append((path.name, node.lineno, func))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return found


SINK_CALLS = {"safe_text", "_safe", "_safe_screen", "_masked_line", "event", "notify", "quote", "put_notice",
              "blocked_notice", "render_notice", "_first_line"}
CUTTERS = {"_clip", "_one_line"}
# a slice of THESE is no outside text: git object ids (12 hex of a sha) and `first`, which is the already masked
# first line of a notice (_first_line). Anything else cut before it reaches a sink is the #81 hole.
ALLOWED_CUT_BASE = re.compile(r"^(?:str\()?(?:first|\w*(?:sha|head|oid|merge|old|new)\w*|\w+\['head'\]|"
                              r"target\.stdout\.strip\(\))\)?$")


# (function, sliced text): render_notice splits the text of a notice at its quote markers, every part is masked
# whole afterwards, nothing is dropped; main() passes `argv[3:]`, the list of arguments, not a text.
ALLOWED_CUT_IN = {("render_notice", "text"), ("main", "argv")}


def cut_before_sink(waves_dir):
    """[(file, line, function, source)]: a slice, _clip(...) or _one_line(...) INSIDE the arguments of a call that
    shows text to a human (SINK_CALLS): the text is cut BEFORE it is masked, and the border may fall inside a
    token (`ghp_abcde`). The cut belongs to the limit of safe_text or after it."""
    import ast
    found = []
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in SINK_CALLS:
                    for arg in [*node.args, *(k.value for k in node.keywords)]:
                        for sub in ast.walk(arg):
                            src = None
                            if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
                                src = ast.unparse(sub.value)
                                if ALLOWED_CUT_BASE.match(src) or (func, src) in ALLOWED_CUT_IN:
                                    continue
                            elif isinstance(sub, ast.Call) and (
                                    (isinstance(sub.func, ast.Name) and sub.func.id in CUTTERS)):
                                src = ast.unparse(sub)
                                if sub.args and ALLOWED_CUT_BASE.match(ast.unparse(sub.args[0])):
                                    continue
                            if src is not None:
                                found.append((path.name, sub.lineno, func, src))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return found


# EVERY truncation by length in scripts/waves/*.py: a slice with an upper bound, _clip, _one_line, _head. A new one
# is red until it is listed HERE with a reason, or rewritten as safe_text(text, limit) (the cut after the mask).
# Key: (file, outermost function, the expression as ast.unparse prints it) -> why it is no cut of outside text.
_SHA = "a git object id (12 hex characters): a name, no outside text"
_MASKED = "the text is already masked (safe_text / _first_line / _safe) above: a cut AFTER the mask"
_LIST = "a slice of a LIST (lines, arguments, records), not a cut of a text by length"
_LOGIC = "parsing / comparison inside the code, nothing of it is shown"
_DEDUP = "the key of once_per (dedup of an episode in state.json), it is never shown"
_WORDCUT = "_one_line cuts at a WORD border: a token is never split, no prefix of it is left"
CUT_ALLOWLIST = {}


def _allow(file, func, why, *exprs):
    for e in exprs:
        CUT_ALLOWLIST[(file, func, e)] = why


_allow("wab.py", "_text_head", _LOGIC, "s[:n]")
_allow("wab.py", "unsent_reason", _LOGIC, "rest[:k]", "head[:k]")
_allow("wab.py", "_undimmed", _LOGIC, "line[pos:m.start()]")
_allow("wab.py", "sub", "_LinearEmail.sub() is re.sub of the e-mail rule: it joins the text between its matches back whole, "
       "inside redact() (#85)", "text[pos:m.start()]")
_allow("wab.py", "input_empty_reason", _LOGIC, "raw[top:bottom]")
_allow("wab.py", "input_text", _LOGIC, "raw[top:bottom]")
_allow("wab.py", "_head", "the cut itself: it keeps the mask whole", "text[:n]", "MASK[:k]", "head[:-k]")
_allow("wab.py", "_clip", "the cut itself, after the mask (_require_masked)", "'…'[:limit]")
_allow("wab.py", "render_notice", "splits a notice at its quote markers; every part is masked whole afterwards",
       "text[pos:m.start()]")
_allow("wab.py", "_tick", _LIST, "(w.get('ctx_hist', []) + [tokens])[-120:]")
_allow("wab.py", "auto_mode_off", _LIST + " (lines of the pane, read by the code)", "[l for l in text.splitlines() if l.strip()][-6:]")
_allow("dash.py", "current_panel", _LIST + " (lines of the masked screen)",
       "[l for l in _safe_screen(wab.pane_text(w['tmux'])).splitlines() if l.strip()][-14:]")
_allow("dash.py", "spark", _LIST, "values[-width:]")
_allow("dash.py", "tail_items", _LIST, "items[-n:]")
_allow("dash.py", "ktok", "a number with a precision, not a text", "{n / 1000000:.2f}")
_allow("wab.py", "pane_digest", _LIST, "[l for l in txt.splitlines() if l.strip()][:-2]")
_allow("wab.py", "verify_previous_merge", _SHA, "oid[:12]")
_allow("wab.py", "refresh_workdir", _SHA, "target.stdout.strip()[:12]")
_allow("wab.py", "system_prompt", _LIST, "body.splitlines()[:1]")
_allow("wab.py", "archive_attempt_files", "a random name", "uuid.uuid4().hex[:8]")
_allow("wab.py", "_deliver", _DEDUP, "f'{what}: {_exc_text(e)}'[:150]")
_allow("wab.py", "_procs_unreadable", _DEDUP, "_exc_text(err)[:150]")
_allow("wab.py", "exit_dialog", _LIST, "lines[-4:-1]")
_allow("wab.py", "_gh", _LIST, "args[:2]")
_allow("wab.py", "_one_line", _WORDCUT + " (over text masked by _require_masked)", "text[:cut]")
_allow("wab.py", "owner_script_name", _SHA, "sha[:12]", "hashlib.sha256(ident.encode('utf-8')).hexdigest()[:16]")
_allow("wab.py", "_note_done_head", _SHA, "old[:12]", "head[:12]")
_allow("wab.py", "_gate_passed", _SHA, "sha[:12]")
_allow("wab.py", "_regate", _SHA, "str(old)[:12]", "str(new)[:12]")
_allow("wab.py", "owner_merge", _SHA, "sha[:12]", "w['gate_sha'][:12]", "str(v['head'])[:12]", "str(v2['head'])[:12]")
_allow("wab.py", "owner_merge", _LIST, "merge[:3]")
_allow("wab.py", "owner_handover", _SHA, "str(pr_head)[:12]", "str(head)[:12]", "merge[:12]", "head[:12]")
_allow("wab.py", "_merging_tick", _DEDUP, "_exc_text(e)[:150]")
_allow("wab.py", "_merging_tick", _SHA, "str(head)[:12]", "sha[:12]")
_allow("wab.py", "_alarm_tick", _DEDUP, "_exc_text(e)[:150]", "str(facts['error'])[:150]")
_allow("wab.py", "_alarm_tick", _SHA, "head[:12]")
_allow("wab.py", "_pending_alarm_enter", _DEDUP, "_exc_text(e)[:150]")
_allow("wab.py", "note_question", _LIST, "qs[:-QUESTIONS_CAP]")
_allow("wab.py", "write_chain_result", _SHA, "str(sha)[:12]")
_allow("wab.py", "blocked_notice", "the question was masked by safe_text above; it is split at `Варианты:`", "question[:m.start()]")
_allow("dash.py", "fold_events", "the time of an events.log line `HH:MM:SS`", "line[11:19]", "ts[:5]")
_allow("gate.py", "manifest_problems", _SHA, "str(manifest.get('head'))[:12]", "head[:12]")
_allow("gate.py", "evaluate", _SHA, "str(pr.get('head'))[:12]", "head[:12]", "str(work.get('head'))[:12]")
_allow("gate.py", "owner_script", _SHA, "sha[:12]")
_allow("gate.py", "alarm_text", _SHA, "head[:12]")


def _is_cut_slice(node):
    """A slice that cuts by length: an upper bound, or a lower bound counted from the END (`text[-50:]`)."""
    import ast
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
        return False
    lo = node.slice.lower
    negative = isinstance(lo, ast.UnaryOp) and isinstance(lo.op, ast.USub) or (
        isinstance(lo, ast.Constant) and isinstance(lo.value, int) and lo.value < 0)
    return node.slice.upper is not None or negative


def cut_sites(waves_dir):
    """{(file, function, expression)}: every way to cut a text by length that is not a helper call: a slice with an
    upper bound or from the end, textwrap / shorten, a precision in a format (`f"{t:.50}"`, `"%.50s" % t`).
    The helpers (_clip, _one_line, _head, _masked_line) are no sites: each of them masks FIRST by itself (rule A)."""
    import ast
    found = set()
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            if _is_cut_slice(node):
                found.add((path.name, func, ast.unparse(node)))
            elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if name in ("shorten", "wrap", "fill", "truncate"):
                    found.add((path.name, func, ast.unparse(node)))
            elif isinstance(node, ast.FormattedValue) and node.format_spec is not None:
                spec = ast.unparse(node.format_spec)
                if re.search(r"\.\d", spec):
                    found.add((path.name, func, ast.unparse(node)))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
                    and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) \
                    and re.search(r"%\.\d", node.left.value):
                found.add((path.name, func, ast.unparse(node)))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return found


# the helpers that cut or join text: each must ask for the mask FIRST (_require_masked), and only they (and
# safe_text / redact, which make the mask) may construct Masked
MASKING_HELPERS = {("wab.py", "_clip"), ("wab.py", "_head"), ("wab.py", "_one_line"), ("wab.py", "_masked_line"),
                   ("dash.py", "_clip")}
MASKED_MAKERS = MASKING_HELPERS | {("wab.py", "safe_text"), ("wab.py", "redact"), ("wab.py", "_join_masked"),
                                    ("wab.py", "Masked")}  # Masked: its own strip/lstrip/rstrip/splitlines


def helper_violations(waves_dir):
    """Rule A: a helper of MASKING_HELPERS that does not call _require_masked; rule D: Masked(...) built anywhere
    but in MASKED_MAKERS."""
    import ast
    bad = []
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for (f, name) in MASKING_HELPERS:
            if f == path.name:
                calls = {ast.unparse(c.func) for c in ast.walk(funcs[name]) if isinstance(c, ast.Call)} if name in funcs else set()
                if not calls & {"_require_masked", "wab._require_masked"}:
                    bad.append((path.name, name, "does not call _require_masked"))

        def walk(node, func):
            if isinstance(node, ast.ClassDef) and node.name == "Masked" and func == "<module>":
                func = "Masked"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            if isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn in ("Masked", "wab.Masked") and (path.name, func) not in MASKED_MAKERS:
                    bad.append((path.name, func, "builds Masked: " + ast.unparse(node)[:60]))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return bad


# Rule E. A helper asked with a JOINED text (an f-string, `+`, `"".join`) masks it again WITHOUT the owner_paths policy
# of its parts (the owner's merge script path of a notice became `[скрыто]`): parts are joined by _join_masked.
# Allowed: a join of RAW outside text, where no part carries a policy.
JOIN_ALLOWLIST = {
    ("wab.py", "process_table", "_masked_line(f'ps: {type(e).__name__}: {_exc_text(e)}', 150)"): "raw error text of ps, no policy",
    ("wab.py", "handoff_gate_line", "_masked_line('; '.join(v['reasons']), 300)"): "raw reasons of the gate, no policy",
    ("wab.py", "owner_merge", "_masked_line('; '.join(v['reasons']), 600)"): "raw reasons of the gate, no policy",
    ("wab.py", "owner_merge", "_masked_line('; '.join(v2['reasons']), 600)"): "raw reasons of the second gate, no policy (#84)",
}


def joined_into_helpers(waves_dir):
    import ast
    found = []
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            if isinstance(node, ast.Call) and node.args:
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                a = node.args[0]
                if name in ("_clip", "_head", "_one_line", "_masked_line") and (
                        isinstance(a, (ast.JoinedStr, ast.BinOp))
                        or (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "join")):
                    found.append((path.name, func, ast.unparse(node)))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return sorted(c for c in found if c not in JOIN_ALLOWLIST)


# Rule C. Functions that show text to a human: in them str.split / splitlines / partition run ONLY on a masked
# value (the result of safe_text & co., or a name assigned from one): split on raw text cuts a secret in two.
OUTPUT_FUNCS = {
    "wab.py": {"quote", "_render_quote", "render_notice", "_first_line", "notify", "note_attention", "attention_text",
               "event", "_short_command", "note_question", "write_chain_result", "_policy_question", "_say_first",
               "blocked_notice", "status_cmd", "manifest_status_line", "_write_status", "_gate_failed",
               "handoff_gate_line", "manifest_where_of", "_resolve_why", "say_cmd", "_masked_line", "_one_line",
               "_gh", "chain_label", "owner_merge", "owner_handover", "_hand_to_owner"},
    "dash.py": {"humanize_event", "current_panel", "manifest_lines", "_blocked_phrase", "_pulse_phrase", "_masked",
                "_safe", "_safe_screen", "_clip", "fold_events", "events_panel", "waves_table", "pipeline"},
}
MASKERS = {"safe_text", "_masked_line", "_one_line", "_first_line", "render_notice", "quote", "_safe", "_safe_screen",
           "_require_masked", "_clip", "_head", "_policy_question", "_say_first", "_resolve_why"}
SPLITTERS = {"split", "rsplit", "splitlines", "partition", "rpartition"}
SPLIT_ALLOWED = {}  # (file, function, receiver) -> why: nothing today


def _masked_expr(node, names):
    import ast
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name in MASKERS:
            return True
        if isinstance(f, ast.Attribute):  # masked.strip() / masked.lower() / masked.replace(...)
            return _masked_expr(f.value, names)
    if isinstance(node, ast.Constant):
        return True  # a literal holds no outside text
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Subscript):
        return _masked_expr(node.value, names)
    if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
        return _masked_expr(node.elt, names) or any(_masked_expr(g.iter, names) for g in node.generators)
    if isinstance(node, ast.IfExp):
        return _masked_expr(node.body, names) and _masked_expr(node.orelse, names)
    return False


def _names(target):
    import ast
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def unmasked_splits(waves_dir):
    """Rule C, flow-sensitive: a name is masked only AFTER an assignment of a masked value and until the next
    assignment of an unmasked one; after an if / try / loop only when it is masked on every way through."""
    import ast
    found = []

    def check_expr(node, masked, fn, path):
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)):
            inner = set(masked)
            for g in node.generators:
                check_expr(g.iter, inner, fn, path)
                (inner.update if _masked_expr(g.iter, inner) else inner.difference_update)(_names(g.target))
                for cond in g.ifs:
                    check_expr(cond, inner, fn, path)
            for part in ((node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)):
                check_expr(part, inner, fn, path)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in SPLITTERS:
            if not _masked_expr(node.func.value, masked):
                found.append((path.name, fn.name, ast.unparse(node.func.value)))
        if isinstance(node, ast.NamedExpr):
            check_expr(node.value, masked, fn, path)
            (masked.update if _masked_expr(node.value, masked) else masked.difference_update)(_names(node.target))
            return
        for child in ast.iter_child_nodes(node):
            check_expr(child, masked, fn, path)

    def assign(target, value, masked):
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            for t, v in zip(target.elts, value.elts):
                assign(t, v, masked)
            return
        (masked.update if _masked_expr(value, masked) else masked.difference_update)(_names(target))

    def run(stmts, masked, fn, path):
        for st in stmts:
            if isinstance(st, ast.Assign):
                check_expr(st.value, masked, fn, path)
                for t in st.targets:
                    assign(t, st.value, masked)
            elif isinstance(st, ast.AugAssign):
                check_expr(st.value, masked, fn, path)
                if not (_masked_expr(st.target, masked) and _masked_expr(st.value, masked)):
                    masked.difference_update(_names(st.target))
            elif isinstance(st, ast.AnnAssign):
                if st.value is not None:
                    check_expr(st.value, masked, fn, path)
                    assign(st.target, st.value, masked)
            elif isinstance(st, ast.If):
                check_expr(st.test, masked, fn, path)
                a, b = set(masked), set(masked)
                run(st.body, a, fn, path)
                run(st.orelse, b, fn, path)
                masked.intersection_update(a & b)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                check_expr(st.iter, masked, fn, path)
                body = set(masked)
                (body.update if _masked_expr(st.iter, masked) else body.difference_update)(_names(st.target))
                run(st.body, body, fn, path)
                orelse = set(masked)
                run(st.orelse, orelse, fn, path)
                masked.intersection_update(body & orelse)  # the body may not run at all
            elif isinstance(st, ast.While):
                check_expr(st.test, masked, fn, path)
                body = set(masked)
                run(st.body, body, fn, path)
                masked.intersection_update(body)
            elif isinstance(st, ast.Try):
                body = set(masked)
                run(st.body, body, fn, path)
                run(st.orelse, body, fn, path)
                ways = [body]
                for h in st.handlers:
                    hs = set(masked)  # an exception may come before any assignment of the body
                    run(h.body, hs, fn, path)
                    ways.append(hs)
                after = set.intersection(*ways)
                run(st.finalbody, after, fn, path)
                masked.intersection_update(after)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    check_expr(item.context_expr, masked, fn, path)
                    if item.optional_vars is not None:
                        masked.difference_update(_names(item.optional_vars))
                run(st.body, masked, fn, path)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                run(st.body, set(masked) - {a.arg for a in st.args.args}, fn, path)  # a nested def: its parameters are raw
            else:
                for child in ast.iter_child_nodes(st):
                    if isinstance(child, ast.expr):
                        check_expr(child, masked, fn, path)

    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in OUTPUT_FUNCS.get(path.name, ())):
            run(fn.body, set(), fn, path)
    return sorted(c for c in set(found) if c not in SPLIT_ALLOWED)


# Rule R. In the functions that turn an outside answer (errors, a status, a question, an exception) into text, no
# str() / repr() / `!r` / json.dumps of a value: str() of a list or dict writes an invisible character as a literal
# `\\u200b` that no mask can find. The value goes to safe_text AS IT IS (it masks the leaves of a container first).
STRINGIFY_FUNCS = {"wab.py": {"_resolve_why", "quote", "note_question", "write_chain_result", "_policy_question",
                              "_say_first", "_masked_line", "_one_line", "handoff_gate_line", "say_cmd", "event"},
                   "dash.py": {"_safe", "_safe_screen", "_masked", "_clip", "humanize_event"}}
STRINGIFY_ALLOWED = {
    ("wab.py", "say_cmd", "str(e)"): "NotSubmitted: a message the dispatcher wrote itself, no outside structure",
    ("wab.py", "write_chain_result", "str(sha)"): "a commit id of the record, 12 hex",
}


def stringified_values(waves_dir):
    import ast
    found = []
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in STRINGIFY_FUNCS.get(path.name, ())):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "repr"):
                    found.append((path.name, fn.name, ast.unparse(node)))
                elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("json.dumps", "pformat", "pprint.pformat"):
                    found.append((path.name, fn.name, ast.unparse(node)))
                elif isinstance(node, ast.FormattedValue) and node.conversion in (114, 115):  # !r !s
                    found.append((path.name, fn.name, "!" + chr(node.conversion) + " " + ast.unparse(node.value)))
    return sorted(c for c in set(found) if c not in STRINGIFY_ALLOWED)


def exception_formats(waves_dir):
    """Rule X: an exception turned into text by str() / repr() / format / `%` / an f-string field, on any path (in an
    `except … as e` body, or a parameter named err/exc/error): str(KeyError("password<ZWSP>=x")) is repr of the
    string, the invisible character becomes a literal `\\u200b` and no mask can find it. The way is
    wab._exc_text(e) (safe_text over the masked args); gate.py, which cannot mask, joins the args' leaves (_exc_str)."""
    import ast
    found = []
    names_any = {"err", "exc", "ex", "exception"}
    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func, excs):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            if isinstance(node, ast.ExceptHandler) and node.name:
                excs = excs | {node.name}
            bad = excs | names_any
            if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name) and node.value.id in bad:
                found.append((path.name, func, f"{{{node.value.id}}}"))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "repr", "format") \
                    and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in bad:
                found.append((path.name, func, ast.unparse(node)))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.right, ast.Name) \
                    and node.right.id in bad:
                found.append((path.name, func, ast.unparse(node)))
            for child in ast.iter_child_nodes(node):
                walk(child, func, excs)

        walk(tree, "<module>", frozenset())
    return sorted(set(found))


# Rule S, over EVERY function of the three modules: the errors of an outside answer (a nested structure) go into a
# text only through a masking / leaf-collecting call; an f-string field, `"%s" %`, `.format()` or str()/repr() of
# an expression that names `errors` is red, whatever the function is.
ERRORS_RE = re.compile(r"\berrors\b")
ERRORS_SAFE_CALLS = {"_leaves", "_exc_text", "_exc_str", "_resolve_why", "safe_text", "_masked_line", "_one_line",
                     "_masked_leaves"}


def stringified_errors(waves_dir):
    import ast
    found = []

    def named(v):
        if isinstance(v, ast.Call):
            f = v.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in ERRORS_SAFE_CALLS:
                return False
            if name == "join":  # "sep".join(<safe call>): named only through its arguments
                return any(named(a) for a in v.args)
        return bool(ERRORS_RE.search(ast.unparse(v)))

    for path in sorted(Path(waves_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func == "<module>":
                func = node.name
            hit = None
            if isinstance(node, ast.FormattedValue) and named(node.value):
                hit = "{" + ast.unparse(node.value) + "}"
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and named(node.right):
                hit = ast.unparse(node)
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "format" and any(named(a) for a in node.args):
                    hit = ast.unparse(node)
                elif isinstance(f, ast.Name) and f.id in ("str", "repr", "format") and node.args and named(node.args[0]):
                    hit = ast.unparse(node)
            if hit is not None:
                found.append((path.name, func, hit))
            for child in ast.iter_child_nodes(node):
                walk(child, func)

        walk(tree, "<module>")
    return sorted(set(found))


def unlisted_cuts(waves_dir):
    return sorted(c for c in cut_sites(waves_dir) if c not in CUT_ALLOWLIST)


def stale_allowlist(waves_dir):
    found = cut_sites(waves_dir)
    return sorted(k for k in CUT_ALLOWLIST if k not in found)


class W2EveryCut(Base):
    """The invariant (#81): outside text is cut only by safe_text(text, limit) (or after it); every other cut by
    length is listed in CUT_ALLOWLIST with its reason."""

    def test_every_cut_in_the_scripts_is_listed_with_a_reason(self):
        self.assertEqual(unlisted_cuts(WAVES), [])
        self.assertEqual(stale_allowlist(WAVES), [], "an entry of the allowlist that matches nothing any more")

    def test_every_helper_masks_first_and_only_the_makers_build_masked(self):
        self.assertEqual(helper_violations(WAVES), [])

    def test_no_output_function_splits_a_value_that_is_not_masked(self):
        self.assertEqual(unmasked_splits(WAVES), [])
        self.assertEqual(SPLIT_ALLOWED, {})

    def mutate(self, name, old, new, file="wab.py"):
        tmp = self.tmp / f"cut-{name}"
        shutil.copytree(WAVES, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        src = (tmp / file).read_text(encoding="utf-8")
        self.assertIn(old, src)
        (tmp / file).write_text(src.replace(old, new, 1), encoding="utf-8")
        return tmp

    def test_gh_cut_through_a_variable_is_caught(self):
        tmp = self.mutate("gh", '        why = _masked_line(r.stderr or r.stdout or "", 200) or f"rc={r.returncode}"',
                          '        why = (r.stderr or r.stdout or "").strip().replace("\\n", " ")[:200] or f"rc={r.returncode}"')
        self.assertTrue([c for c in unlisted_cuts(tmp) if c[1] == "_gh"], unlisted_cuts(tmp))

    def test_a_new_cut_in_any_function_is_caught(self):
        for n, expr in enumerate(("text[:120]", "text[-50:]", "textwrap.shorten(text, 50)", "f'{text:.50}'", "'%.50s' % text")):
            with self.subTest(expr=expr):
                tmp = self.mutate(f"new{n}", "def _say_first(text):\n", f"def _say_first(text):\n    s = {expr}\n    event({{}}, s)\n")
                self.assertTrue([c for c in unlisted_cuts(tmp) if c[1] == "_say_first"], unlisted_cuts(tmp))

    def test_a_raw_split_before_the_mask_is_caught(self):
        tmp = self.mutate("rawsplit", "def _gate_failed(cfg, st, wave, w, wdir, reasons):\n",
                          "def _gate_failed(cfg, st, wave, w, wdir, reasons):\n    reasons = ' '.join(reasons.split())\n")
        self.assertIn(("wab.py", "_gate_failed", "reasons"), unmasked_splits(tmp))
        tmp = self.mutate("rawsplit2", "safe_text(text, 10 ** 9).splitlines() if l.strip()]\n    return _clip(lines[0], 120)",
                          "text.splitlines() if l.strip()]\n    return _clip(lines[0], 120)")
        self.assertIn(("wab.py", "_say_first", "text"), unmasked_splits(tmp))

    def test_a_raw_split_before_the_assignment_of_the_mask_is_caught(self):  # tester-r2-1 F1
        tmp = self.mutate("flow", "    text = _one_line(safe_text(text, 10 ** 9, owner_paths=True))",
                          "    text = ' '.join(text.split())\n    text = _one_line(safe_text(text, 10 ** 9, owner_paths=True))")
        self.assertIn(("wab.py", "_write_status", "text"), unmasked_splits(tmp))
        # a branch that masks is not enough: the name is masked after an if only on every way through
        tmp = self.mutate("flow2", "def _say_first(text):\n",
                          "def _say_first(text):\n    if len(text) > 3:\n        text = safe_text(text, 100)\n    first = text.split()\n")
        self.assertIn(("wab.py", "_say_first", "text"), unmasked_splits(tmp))

    def test_no_exception_is_formatted_by_str_or_an_f_string(self):
        self.assertEqual(exception_formats(WAVES), [])

    def test_a_reverted_exception_format_is_caught(self):
        tmp = self.mutate("exc", "event(cfg, f\"{wave}: alarm: PR facts not collected: {_exc_text(e)}\")",
                          "event(cfg, f\"{wave}: alarm: PR facts not collected: {e}\")")
        self.assertTrue([x for x in exception_formats(tmp) if x[1] == "_alarm_tick"], exception_formats(tmp))
        tmp = self.mutate("exc2", "def _say_first(text):\n", "def _say_first(text):\n    try:\n        pass\n    except KeyError as e:\n        s = str(e)\n")
        self.assertIn(("wab.py", "_say_first", "str(e)"), exception_formats(tmp))

    def test_the_errors_of_an_answer_are_never_stringified_in_any_function(self):  # tester-r3-1 F2
        self.assertEqual(stringified_errors(WAVES), [])

    def test_a_stringified_errors_value_is_caught_in_resolve_why_and_in_gate(self):
        old = '        return _masked_line(out.get("errors"), 200)'
        for n, new in enumerate(('        return _masked_line(f"{out.get(\'errors\')}", 200)',
                                 '        return _masked_line("%s" % out.get("errors"), 200)',
                                 '        return _masked_line("{}".format(out.get("errors")), 200)')):
            tmp = self.mutate(f"errs{n}", old, new)
            self.assertTrue([x for x in stringified_errors(tmp) if x[1] == "_resolve_why"], (new, stringified_errors(tmp)))
        tmp = self.mutate("gateerrs", "{' '.join(_leaves(data['errors']))}", "{data['errors']}", file="gate.py")
        self.assertTrue([x for x in stringified_errors(tmp) if x[0] == "gate.py"], stringified_errors(tmp))

    def test_no_outside_value_is_stringified_before_the_mask(self):
        self.assertEqual(stringified_values(WAVES), [])

    def test_str_of_the_errors_in_resolve_why_is_caught_statically_and_by_behaviour(self):  # H1
        tmp = self.mutate("strerr", '        return _masked_line(out.get("errors"), 200)',
                          '        return _masked_line(str(out.get("errors")), 200)')
        self.assertIn(("wab.py", "_resolve_why", "str(out.get('errors'))"), stringified_values(tmp))
        spec = importlib.util.spec_from_file_location("wab_mutant3", tmp / "wab.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        errors = {"errors": [{"message": "password\u200b=Hunter2SecretValue99"}]}
        self.assertIsNotNone(leaked(mod._resolve_why(errors), ["Hunter2SecretValue99"]))
        self.assertIsNone(leaked(wab._resolve_why(errors), ["Hunter2SecretValue99"]))

    def test_a_helper_that_does_not_mask_first_is_caught(self):
        tmp = self.mutate("nomask", 'text = Masked(" ".join(_require_masked(text).split()))\n    if len(text) <= limit:\n        return text\n    cut',
                          'text = Masked(" ".join(str(text).split()))\n    if len(text) <= limit:\n        return text\n    cut')
        self.assertIn(("wab.py", "_one_line", "does not call _require_masked"), helper_violations(tmp))
        self.assertTrue([u for u in unmasked_splits(tmp) if u[1] == "_one_line"])

    def test_masked_built_outside_the_makers_is_caught(self):
        tmp = self.mutate("masked", "def _say_first(text):\n", "def _say_first(text):\n    text = Masked(text)\n")
        self.assertTrue([b for b in helper_violations(tmp) if b[1] == "_say_first"])

    def test_no_helper_is_asked_with_a_joined_text(self):
        self.assertEqual(joined_into_helpers(WAVES), [])

    def test_render_notice_without_join_masked_is_caught_by_the_static_check_and_by_behaviour(self):
        tmp = self.mutate("join", "return _clip(_join_masked(out), limit)", 'return _clip("".join(out), limit)')
        self.assertTrue([j for j in joined_into_helpers(tmp) if j[1] == "render_notice"])
        cfg, _ = self.chain()
        path = wab.write_owner_script(cfg, "W1", "71234567890a" + "0" * 28)
        notice = f"{wab.SIGN}Выполни: {wab.home_form(path)}"
        spec = importlib.util.spec_from_file_location("wab_mutant2", tmp / "wab.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn(path.name, wab.render_notice(notice))
        self.assertNotIn(path.name, mod.render_notice(notice))  # the mutant loses the policy: the test above is red on it

    def test_masked_is_a_str_that_json_and_files_take(self):
        m = wab.safe_text("a b", 10)
        self.assertIsInstance(m, wab.Masked)
        self.assertIsInstance(m, str)
        self.assertEqual(json.dumps({"s": m}), '{"s": "a b"}')
        self.assertIsInstance(wab._one_line("a  b"), wab.Masked)
        self.assertIsInstance(wab._clip("x" * 50, 10), wab.Masked)
        self.assertIsInstance(wab._masked_line("x\u2028y", 10), wab.Masked)

    def test_a_helper_asked_with_raw_text_masks_it(self):
        raw = "password=Hunter2\x1cSecretValue99"
        for fn in (lambda t: wab._one_line(t), lambda t: wab._clip(t, 100), lambda t: wab._head(t, 100),
                   lambda t: wab._masked_line(t, 100), lambda t: self.dash_clip(t)):
            self.assertIsNone(leaked(fn(raw), ["SecretValue99"]))

    def dash_clip(self, t):
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        return dash._clip(t, 100)


class W2CutOutside(Base):
    """The three named sites of tester-2: the text of a gh error, of a failed `resolveReviewThread` and of a failed
    `state.py where`, cut INSIDE a token."""

    def text_for(self, token, visible, cut):
        return "e" * (cut - 1 - visible) + " " + token

    def test_a_gh_error_cut_inside_a_token_leaves_no_prefix(self):
        for name, (token, part) in W2CutBeforeMask.TOKENS.items():
            for visible in (len(token) // 2, len(token) // 2 + 3, 14):
                with self.subTest(token=name, visible=visible):
                    err = self.text_for(token, visible, 200)
                    self.gh_handler = lambda args: subprocess.CompletedProcess(args, 1, "", err)
                    with self.assertRaises(wab.gate.CollectError) as cm:
                        wab._gh("api", "x")
                    cfg, _ = self.chain()
                    with contextlib.redirect_stdout(io.StringIO()):
                        wab.event(cfg, f"W1: {cm.exception}")
                    seen = str(cm.exception) + (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
                    self.assertIsNone(leaked(seen, [part]), seen[-250:])

    def test_a_failed_resolve_thread_cut_inside_a_token_leaves_no_prefix(self):
        fn = getattr(wab, "_resolve_why", None)
        self.assertIsNotNone(fn, "wab._resolve_why: the reason of a thread that is not resolved, masked, then cut")
        for name, (token, part) in W2CutBeforeMask.TOKENS.items():
            for visible in (len(token) // 2, len(token) // 2 + 3, 14):
                with self.subTest(token=name, visible=visible):
                    out = {"errors": [{"message": self.text_for(token, visible, 190)}]}
                    self.assertIsNone(leaked(fn(out), [part]))
        self.assertEqual(fn({}), "isResolved is not true")

    def test_a_failed_where_cut_inside_a_token_leaves_no_prefix(self):
        for name, (token, part) in W2CutBeforeMask.TOKENS.items():
            for visible in (len(token) // 2, len(token) // 2 + 3, 14):
                with self.subTest(token=name, visible=visible):
                    err = OSError(self.text_for(token, visible, 150))
                    with mock.patch.object(wab.subprocess, "run", side_effect=err):
                        out = wab.manifest_where_of(self.tmp / "m.json", self.tmp)
                    self.assertIsNone(leaked(out["error"], [part]), out)

    def test_the_other_error_texts_cut_inside_a_token_leave_no_prefix(self):
        for name, (token, part) in W2CutBeforeMask.TOKENS.items():
            err = self.text_for(token, len(token) // 2, 100)
            with self.subTest(token=name):
                with mock.patch.object(wab, "sh", return_value=subprocess.CompletedProcess([], 1, "", err)):
                    with self.assertRaises(wab.ProcFactsError) as cm:
                        wab.process_table()
                self.assertIsNone(leaked(str(cm.exception), [part]), str(cm.exception))

    def test_gate_errors_are_not_cut_by_gate_and_masked_where_they_are_shown(self):
        import gate
        for name, (token, part) in W2CutBeforeMask.TOKENS.items():
            text = self.text_for(token, len(token) // 2, 200)
            with self.subTest(token=name):
                with self.assertRaises(gate.CollectError) as cm:
                    gate._threads(lambda query, variables: {"errors": text}, "o/r", 1)
                self.assertIn(token, str(cm.exception))  # gate.py (stdlib-only) does not touch the text at all
                self.assertIsNone(leaked(wab._masked_line(cm.exception, 200), [part]))


class W2OwnerScriptPath(Base):
    """The owner's merge script path `~/.cache/wab/<wave>.<sha12>.<id16>.merge` is the dispatcher's own wording:
    it stays whole in every path of a notice (the exemption of owner_paths=True must not be lost by a helper that
    masks again), and is masked inside a quote of the wave and when the file does not exist."""

    def setUp(self):
        super().setUp()
        self.cfg, _ = self.chain()
        path = wab.write_owner_script(self.cfg, "W1", "71234567890a" + "0" * 28)  # sha12 looks like a phone number
        self.path, self.shown = path, wab.home_form(path)
        self.notice = f"{wab.SIGN}волна W1 сдала PR. Выполни: {self.shown} (заново проверит гейт)\n\nещё строка"

    def test_the_path_is_whole_in_every_path_of_a_notice(self):
        self.assertIn(self.shown, wab.render_notice(self.notice))
        self.assertIn(self.shown, wab._first_line(self.notice, 300))
        self.assertIn(self.shown, wab._first_line(f"{wab.SIGN}Выполни: {self.shown}", 300))
        att = wab.attention_text({"waves": {"W1": {"tmux": "wv-w1", "attention": {}}}})
        w = {}
        wab.note_attention(w, "blocked", f"{wab.SIGN}Выполни: {self.shown}", 1.0)
        self.assertIn(self.shown, wab.attention_text({"waves": {"W1": {"tmux": "wv-w1", "attention": w["attention"]}}}))
        self.assertIsNone(att)

    def test_the_path_is_whole_in_telegram_and_in_display_message(self):
        self.tg.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            wab.notify(self.cfg, f"{wab.SIGN}Выполни: {self.shown}")
        self.assertIn(self.shown, "\n".join(self.tg))
        cfg, _ = self.chain(telegram=None)
        shown = []
        with mock.patch.object(wab, "display_all", side_effect=shown.append), contextlib.redirect_stdout(io.StringIO()):
            wab.notify(cfg, f"{wab.SIGN}Выполни: {self.shown}", "W1")
        self.assertIn(self.shown, "\n".join(shown))

    def test_the_path_inside_a_quote_of_the_wave_is_masked(self):
        for out in (wab.render_notice(wab.quote(self.shown)), wab.render_notice(f"x {wab.quote(self.shown)}"),
                    wab._first_line(wab.quote(self.shown), 300)):
            self.assertNotIn(self.path.name, out)

    def test_the_path_of_a_file_that_does_not_exist_is_masked(self):
        self.path.unlink()
        for out in (wab.render_notice(self.notice), wab._first_line(self.notice, 300)):
            self.assertNotIn(self.path.name, out)

    def test_the_status_of_the_dispatcher_keeps_the_path(self):
        wdir = wab.wave_dir(self.cfg, "W1")
        self.assertIn(self.shown, wab._write_status(wdir, f"BLOCKED: merge gate: выполни {self.shown}"))


class W2CutBeforeMask(Base):
    """A caller that cuts the outside text BEFORE it reaches safe_text leaves a token's prefix at the border
    (`ghp_abcde`: too short for any rule, so it is shown). Fixtures: the border falls INSIDE the token."""
    TOKENS = {  # token, the visible part of the value that must not appear
        "ghp_": ("ghp_AbCd1234EfGh5678IjKl9012MnOp3456", "AbCd1234EfGh"),
        "sk-": ("sk-ant-api03-AbCdEfGhIjKl1234567890", "AbCdEfGhIjKl"),
        "password=": ("password=Hunter2SecretValue99", "Hunter2Secret"),
        "hex": (HEX40, HEX40[:12]),
        "base64": (B64, B64[:12]),
    }

    def status_for(self, token, visible, cut=200):
        head = "BLOCKED: "
        pad = "y" * (cut - len(head) - 1 - visible)
        return f"{head}{pad} {token}"

    def test_a_blocked_status_cut_inside_a_token_does_not_show_its_prefix_in_events_and_notices(self):
        for name, (token, part) in self.TOKENS.items():
            for visible in (len(token) // 2, len(token) // 2 + 3, 14):
                with self.subTest(token=name, visible=visible):
                    self.tg.clear()
                    cfg, _ = self.chain()
                    self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(last_status="RUNNING")}})
                    self.set_status(cfg, "W1", self.status_for(token, visible))
                    with contextlib.redirect_stdout(io.StringIO()) as printed:
                        wab.tick(cfg, wab.load_state(cfg))
                    seen = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8") + printed.getvalue() \
                        + "\n".join(self.tg)
                    att = cfg["run_dir"] / "ATTENTION"
                    seen += att.read_text(encoding="utf-8") if att.exists() else ""
                    self.assertIn("BLOCKED", seen)
                    self.assertIsNone(leaked(seen, [part]), seen[-300:])

    def test_one_line_does_not_cut_inside_a_word(self):
        for name, (token, part) in self.TOKENS.items():
            text = "y " * 100 + token
            for limit in range(150, len(text) - 1):
                out = wab._one_line(text, limit)
                self.assertIsNone(leaked(out, [part]), (name, limit, out))
                self.assertLessEqual(len(out), limit)

    def test_the_masked_line_is_masked_before_it_is_cut(self):
        fn = getattr(wab, "_masked_line", None)
        self.assertIsNotNone(fn, "wab._masked_line: mask first, then collapse and cut")
        for name, (token, part) in self.TOKENS.items():
            for limit in range(150, 230):
                self.assertIsNone(leaked(fn(("y " * 100) + token, limit), [part]), (name, limit))
        self.assertIsNone(leaked(fn("password=Hunter2\rSecretValue99", 100), ["SecretValue99"]))

    def test_the_static_check_finds_no_cut_before_a_sink(self):
        self.assertEqual(cut_before_sink(WAVES), [])

    def test_a_cut_before_a_sink_is_caught_by_the_static_check(self):
        tmp = self.tmp / "mut3"
        shutil.copytree(WAVES, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        src = (tmp / "wab.py").read_text(encoding="utf-8")
        good = 'event(cfg, f"{wave}: {safe_text(status, 200)}")'
        self.assertIn(good, src)
        (tmp / "wab.py").write_text(src.replace(good, 'event(cfg, f"{wave}: {status[:200]}")'), encoding="utf-8")
        found = cut_before_sink(tmp)
        self.assertEqual([f[3] for f in found], ["status"], found)
        mutant = (tmp / "wab.py").read_text(encoding="utf-8").replace(
            '{status[:200]}', "{_one_line(status, 200)}")
        (tmp / "wab.py").write_text(mutant, encoding="utf-8")
        self.assertEqual([f[3] for f in cut_before_sink(tmp)], ["_one_line(status, 200)"])


class W2OutputPaths(Base):
    """Every place that shows text to a human, in ONE registry (wab.OUTPUT_PATHS): the test goes by the
    registry. A path that is not here is a path nobody checked for the invisible-character hole (#81)."""

    def setUp(self):
        super().setUp()
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.dash = dash
        dash.MANIFEST_CACHE.clear()

    # ----- the adapters: raw outside text -> what a human gets to see on that path -----
    def out_quote(self, raw):
        return wab.quote(raw)

    def out_render_notice(self, raw):
        return wab.render_notice(raw)

    def out_first_line(self, raw):
        return wab._first_line(raw, 300)

    def out_notify_telegram(self, raw):
        cfg, _ = self.chain()
        self.tg.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            wab.notify(cfg, raw)
        return "\n".join(self.tg)

    def out_notify_display(self, raw):
        cfg, _ = self.chain(telegram=None)
        shown = []
        with mock.patch.object(wab, "display_all", side_effect=shown.append), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.notify(cfg, raw)
        return "\n".join(shown)

    def out_attention(self, raw):
        st = {"waves": {"W1": {"tmux": "wv-w1", "attention": {"blocked": {"at": 1.0, "line": raw}}}}}
        w = {}
        wab.note_attention(w, "blocked", raw, 1.0)
        return wab.attention_text(st) + wab.attention_text({"waves": {"W1": {"tmux": "wv-w1", "attention": w["attention"]}}})

    def out_event_line(self, raw):
        cfg, _ = self.chain()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            wab.event(cfg, raw)
        return (cfg["run_dir"] / "events.log").read_text(encoding="utf-8") + printed.getvalue()

    def out_short_command(self, raw):
        return wab._short_command(f"bash -c eval '{raw}' < /dev/null", limit=1000)

    def out_note_question(self, raw):
        w = {}
        wab.note_question(w, raw, 1.0)
        return w["questions"][-1]["text"]

    def out_chain_result(self, raw):
        cfg, _ = self.chain()
        w = self.wave_rec("W1", phase="done", questions=[{"at": 1.0, "text": raw}])
        wab.write_chain_result(cfg, {"waves": {"W1": w}})
        return (cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")

    def out_policy_question(self, raw):
        fn = getattr(wab, "_policy_question", None)
        if fn is None:  # before the change the line was cut out inline
            return wab.redact((raw.splitlines() or [""])[0], 200)
        return fn({"question": raw})

    def out_say_echo(self, raw):
        fn = getattr(wab, "_say_first", None)
        if fn is None:
            lines = [l for l in raw.splitlines() if l.strip()]
            return wab.redact(lines[0] if lines else "", 120)
        return fn(raw)

    def out_blocked_notice(self, raw):
        status = f"BLOCKED: [class=needs_decision rec=owner red=no] {raw}"
        return wab.blocked_notice(self.chain()[0], "W1", status, "attach") + wab.blocked_notice(
            self.chain()[0], "W1", f"BLOCKED: {raw}", "attach")

    def out_status_file(self, raw):
        cfg, _ = self.chain()
        wdir = wab.wave_dir(cfg, "W1")
        returned = wab._write_status(wdir, raw)
        return returned + "\n" + (wdir / "status").read_text(encoding="utf-8")

    def out_gate_failed(self, raw):
        cfg, _ = self.chain()
        w = self.wave_rec("W1", phase="gate")
        st = {"current": "W1", "waves": {"W1": w}}
        self.put_state(cfg, st)
        self.tg.clear()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            wab._gate_failed(cfg, st, "W1", w, wab.wave_dir(cfg, "W1"), f"проверки неуспешны: {raw} (failure)")
        seen = [w.get("last_status", ""), w["gate_fail_msg"]["text"], (wab.wave_dir(cfg, "W1") / "status").read_text(encoding="utf-8"),
                (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"), printed.getvalue(), "\n".join(self.tg),
                self.dash.wave_status(cfg, "W1", w), str(self.get_state(cfg)["waves"]["W1"].get("last_status"))]
        self.put_state(cfg, st)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wab.status_cmd(cfg)
        return "\n".join(map(str, seen)) + "\n" + buf.getvalue()

    def out_owner_merge_regate(self, raw):
        """owner-merge: the threads are closed, then the SECOND gate fails; its reason names a check-run called `raw`."""
        case = OwnerMerge("test_a_thread_is_closed_only_when_the_answer_says_so")
        case.setUp()
        try:
            case.not_draft()
            case.on_resolve = lambda v: case.facts.__setitem__(
                "check_runs", [{"id": 1, "name": raw, "status": "completed", "conclusion": "failure"}])
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    case.run_it()
                except SystemExit as e:
                    return str(e)
            return "no SystemExit"
        finally:
            case.doCleanups()

    def out_resolve_why(self, raw):
        # the errors of a GraphQL answer are a NESTED structure: key and value both carry the text
        return wab._resolve_why({"errors": [{"message": raw, "extensions": {raw: [raw]}}, (raw,)]})

    def out_dash_waves_table(self, raw):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", raw)  # an unknown status: the table shows it as its label
        return self.render_plain(self.dash.waves_table(cfg, self.get_state(cfg)))

    def out_dash_pipeline(self, raw):
        cfg, _ = self.chain(titles={"W1": raw})  # the title of a wave is shown on the line too (Codex on #85)
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", raw)
        return self.render_plain(self.dash.pipeline(cfg, self.get_state(cfg)))

    def out_status_cmd(self, raw):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", raw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            wab.status_cmd(cfg)
        return buf.getvalue()

    def out_dash_event(self, raw):
        phrase, details, _colour, _known = self.dash.humanize_event(raw)
        return f"{phrase}\n{details or ''}"

    def render_plain(self, renderable):
        from rich.console import Console
        con = Console(width=4000, record=True, file=io.StringIO(), force_terminal=False)
        con.print(renderable)
        return con.export_text()

    def out_dash_current_panel(self, raw):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", raw)
        self.pane = raw
        with mock.patch.object(self.dash, "manifest_lines", return_value=[]):
            return self.render_plain(self.dash.current_panel(cfg, self.get_state(cfg)))

    def out_dash_manifest(self, raw):
        cfg, _ = self.chain()
        where = {"task": raw, "task_status": raw, "step": raw, "role": raw, "next_action": raw,
                 "decision_required_for": raw, "run": raw, "error": None,
                 "verdicts": {"tester": raw, "reviewer": {"artifact": raw}}, "fix_round": {"artifact": raw},
                 "open_findings": [{"role": raw, "status": raw, "x": {"artifact": raw}}]}
        # tester on #85: only the `artifact` of an open finding is a path that is opened and not shown;
        # an `artifact` key anywhere else is text like any other
        for n, sev in enumerate(({raw: raw}, [raw], raw, "\x85" + raw + "\x1c")):  # never str()/strip() before the mask
            art = self.tmp / f"findings-{n}.json"
            art.write_text(json.dumps({"findings": [{"severity": sev}]}), encoding="utf-8")
            where["open_findings"].append({"role": "tester", "status": "failed", "artifact": str(art)})
        with mock.patch.object(self.dash, "manifest_where", return_value=where):
            lines = self.dash.manifest_lines(cfg, "W1", self.wave_rec())
        err = {"error": raw}
        with mock.patch.object(self.dash, "manifest_where", return_value=err):
            lines += self.dash.manifest_lines(cfg, "W1", self.wave_rec())
        return "\n".join(l.plain for l in lines)

    def test_a_severity_that_is_not_a_string_never_reaches_the_dashboard(self):
        """Codex on #85: str() of a nested severity escapes the invisible character of its key
        (`pass\\u200bword`), and the mask no longer sees the hiding key: such an artifact is invalid."""
        for sev in ({"pass\u200bword": "Hunter2"}, ["pass\u200bword=Hunter2"], {"x": {"pass\u200bword": "Hunter2"}}):
            with self.subTest(sev=sev):
                art = self.tmp / "findings.json"
                art.write_text(json.dumps({"findings": [{"severity": sev}]}), encoding="utf-8")
                self.assertIsNone(self.dash.finding_counts(str(art)))
                self.dash.MANIFEST_CACHE.clear()
                cfg, _ = self.chain()
                where = {"task": "W1", "open_findings": [{"role": "tester", "status": "failed", "artifact": str(art)}]}
                with mock.patch.object(self.dash, "manifest_where", return_value=where):
                    lines = self.dash.manifest_lines(cfg, "W1", self.wave_rec())
                self.assertNotIn("hunter2", "\n".join(l.plain for l in lines).lower())
        art.write_text(json.dumps({"findings": [{"severity": "p1"}, {"priority": " HIGH\n"}, {}]}), encoding="utf-8")
        self.assertEqual(self.dash.finding_counts(str(art)), {"P1": 1, "high": 1, "None": 1})
        # Astra on #85: strip() took the invisible character off the edge of an unknown value, and the mask,
        # which hides the whole word for it, saw a plain short word: the unknown value stays as it was
        for edge in ("\x85", "\x1c", "\u200b"):
            with self.subTest(edge=edge):
                self.dash.MANIFEST_CACHE.clear()
                art.write_text(json.dumps({"findings": [{"severity": "Q7vZk2LmPx9Wt4Yb" + edge}]}), encoding="utf-8")
                where = {"task": "W1", "open_findings": [{"role": "tester", "status": "failed", "artifact": str(art)}]}
                with mock.patch.object(self.dash, "manifest_where", return_value=where):
                    lines = self.dash.manifest_lines(self.chain()[0], "W1", self.wave_rec())
                self.assertNotIn("q7vzk2lmpx9wt4yb", "\n".join(l.plain for l in lines).lower())

    def adapters(self):
        return {name[4:]: getattr(self, name) for name in dir(self) if name.startswith("out_")}

    # ----- the registry -----
    def test_every_path_of_the_registry_has_a_test_and_the_other_way_round(self):
        registry = getattr(wab, "OUTPUT_PATHS", None)
        self.assertIsInstance(registry, dict, "wab.OUTPUT_PATHS: the registry of the paths that show text to a human")
        self.assertEqual(set(registry), set(self.adapters()),
                         "a path without a test (or a test of a path nobody registered)")
        for name, what in registry.items():
            self.assertTrue(isinstance(what, str) and what.strip(), f"{name}: no description")

    def nested_forms(self, secret):
        """The text of a secret inside every kind of container (and an exception): what a non-str reaches safe_text as."""
        return {
            "list of dicts": [{"message": f"x {secret} y"}],
            "key of a dict": {f"{secret}": 1},
            "tuple": (f"a {secret}", 1),
            "deep": {"a": [{"b": (f"{secret}",)}]},
            "exception with a list": KeyError([f"x {secret}"]),
            "KeyError with one str": KeyError(f"x {secret}"),
            "ValueError with several": ValueError("a", f"x {secret}"),
            "OSError": OSError(2, f"x {secret}", f"/tmp/{secret}"),
            "exception with a nested structure": RuntimeError({"a": [{"b": f"{secret}"}]}),
            "set": {f"{secret}"},
        }

    def test_a_container_is_masked_by_its_leaves_before_it_is_written(self):
        for cname, ch in {**INVISIBLE_CLASSES, **SPLIT_CLASSES}.items():
            for sname, (secret, values) in secret_shapes(ch).items():
                for fname, obj in self.nested_forms(secret).items():
                    for fn in (lambda o: wab.safe_text(o, 10 ** 6), lambda o: wab._masked_line(o, 10 ** 6),
                               lambda o: wab._one_line(o), lambda o: wab._clip(o, 10 ** 6)):
                        out = fn(obj)
                        piece = leaked(out, values)
                        if piece is not None:
                            self.fail(f"«{piece}» of [{sname}] with {cname} in a {fname} reached {out!r}")

    SECRET_VALUE = "Q7vZk2LmPx9Wt4Yb"

    def secret_key_forms(self):
        """{name: (the errors of an answer, what must not be seen)}: the value under a key that hides it: a key with an
        invisible/control character, a key that looks secret (any case), a structure under such a key."""
        v = self.SECRET_VALUE
        forms = {}
        for cname, ch in {**INVISIBLE_CLASSES, **SPLIT_CLASSES}.items():
            forms[f"invisible key {cname}"] = [{"extensions": {f"pass{ch}word": v}}]
            forms[f"invisible key {cname} (structure)"] = [{"extensions": {f"k{ch}": {"inner": [v]}}}]
        for key in ("password", "Password", "PASSWORD", "api_key", "Api-Key", "token", "accessToken", "secret", "sig",
                    "x-session-id", "client_secret", "connection_string", "dsn", "Authorization-Token"):
            forms[f"secret key {key}"] = [{"extensions": {key: v}}]
            forms[f"secret key {key} (structure)"] = [{"extensions": {key: {"a": [{"b": v}]}}}]
        forms["nested under a secret key"] = {"errors": {"password": {"deeper": {"token": [v]}}}}
        return forms

    def test_the_value_under_a_hiding_key_is_hidden_whole(self):  # r3-2: key and value were masked independently
        import gate
        for name, errors in self.secret_key_forms().items():
            with self.subTest(form=name):
                for out in (wab.safe_text(errors, 10 ** 6), wab._resolve_why({"errors": errors}),
                            wab.safe_text(wab._exc_text(KeyError(errors)), 10 ** 6),
                            wab.safe_text(wab._exc_text(ValueError("a", errors)), 10 ** 6)):
                    self.assertIsNone(leaked(out, [self.SECRET_VALUE]), out)
                with self.assertRaises(gate.CollectError) as cm:
                    gate._threads(lambda q, vv: {"errors": errors}, "o/r", 1)
                self.assertIsNone(leaked(str(cm.exception), [self.SECRET_VALUE]), str(cm.exception))
                self.assertIsNone(leaked(wab.safe_text(cm.exception, 10 ** 6), [self.SECRET_VALUE]))

    def test_the_manifest_block_hides_the_value_under_a_hiding_key(self):
        cfg, _ = self.chain()
        for key in ("password", "Token", "pass\u200bword", "k\ufe0f"):
            where = {"task": "T1", "task_status": "open", "step": 1, "role": "coder", "error": None,
                     "verdicts": {key: self.SECRET_VALUE, "tester": "pass"}, "open_findings": []}
            with mock.patch.object(self.dash, "manifest_where", return_value=where):
                out = "\n".join(l.plain for l in self.dash.manifest_lines(cfg, "W1", self.wave_rec()))
            self.assertIsNone(leaked(out, [self.SECRET_VALUE]), (key, out))
            self.assertIn("tester: pass", out)

    def test_the_value_under_an_ordinary_key_stays_visible(self):
        import gate
        errors = [{"message": "Resource not accessible", "path": ["resolveReviewThread"], "type": "FORBIDDEN",
                   "extensions": {"code": "E1", "status": 403}}]
        for out in (wab.safe_text(errors, 10 ** 6), wab._resolve_why({"errors": errors})):
            for want in ("Resource not accessible", "resolveReviewThread", "FORBIDDEN", "E1", "403"):
                self.assertIn(want, out)
        with self.assertRaises(gate.CollectError) as cm:
            gate._threads(lambda q, vv: {"errors": errors}, "o/r", 1)
        self.assertIn("Resource not accessible", str(cm.exception))

    def test_a_structure_nested_deeper_than_the_limit_leaks_nothing(self):  # B
        for depth in (19, 20, 21, 25, 60):
            deep = "password\u200b=Hunter2SecretValue99"
            for _ in range(depth):
                deep = [{"k": deep}]
            out = wab.safe_text({"errors": deep}, 10 ** 6)
            self.assertIsNone(leaked(out, ["Hunter2SecretValue99"]), (depth, out[:200]))
            import gate
            msg = " ".join(gate._leaves(deep))
            self.assertIsNone(leaked(wab.safe_text(msg, 10 ** 6), ["Hunter2SecretValue99"]), depth)
            self.assertNotIn("\\u200b", msg, depth)
            with self.assertRaises(gate.CollectError) as cm:
                gate._threads(lambda q, v: {"errors": deep}, "o/r", 1)
            self.assertIsNone(leaked(wab.safe_text(cm.exception, 10 ** 6), ["Hunter2SecretValue99"]), depth)

    def test_an_exception_in_an_error_path_is_masked_through_its_args(self):  # A
        secret = "password\u200b=Hunter2SecretValue99"
        for exc in (KeyError(secret), ValueError("a", secret), OSError(2, secret, "/tmp/x"),
                    RuntimeError({"a": [secret]}), KeyError([secret])):
            out = wab.safe_text(exc, 10 ** 6)
            self.assertIsNone(leaked(out, ["Hunter2SecretValue99"]), (repr(exc), out))
            self.assertNotIn("\\u200b", out)
        self.assertEqual(wab.safe_text(KeyError("plain words"), 100), "plain words")  # no repr quotes
        self.assertEqual(wab.safe_text(ValueError("a", "b"), 100), "a b")

    def test_a_failure_path_that_formats_the_exception_masks_it(self):  # f"{e}" in an error path
        secret = "password\u200b=Hunter2SecretValue99"
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        with mock.patch.object(wab, "find_pr", side_effect=KeyError(secret)), \
                mock.patch.object(wab, "gate_facts", side_effect=KeyError(secret)), \
                contextlib.redirect_stdout(io.StringIO()):
            v = wab.gate_check(cfg, "W1", {"cwd": self.cwd})
        self.assertIsNone(leaked(" ".join(v["reasons"]) + wab.safe_text(" ".join(v["reasons"]), 10 ** 6), ["Hunter2SecretValue99"]),
                          v["reasons"])
        with contextlib.redirect_stdout(io.StringIO()):
            wab.event(cfg, f"W1: failed: {wab._exc_text(KeyError(secret))}")
        self.assertIsNone(leaked((cfg["run_dir"] / "events.log").read_text(encoding="utf-8"), ["Hunter2SecretValue99"]))

    def test_a_secret_in_an_unknown_status_is_masked_by_the_dashboard(self):  # H2
        raw = "BLOCKED password\u200b=Hunter2SecretValue99"
        for path in ("dash_waves_table", "dash_pipeline"):
            self.assertIsNone(leaked(self.adapters()[path](raw), ["Hunter2SecretValue99"]), path)

    def test_the_errors_of_a_failed_resolve_are_masked_through_their_leaves(self):  # H1
        out = wab._resolve_why({"errors": [{"message": "password\u200b=Hunter2SecretValue99"}]})
        self.assertIsNone(leaked(out, ["Hunter2SecretValue99"]), out)

    def test_every_path_hides_a_secret_torn_by_a_separator_of_str_split(self):
        adapters = self.adapters()
        for form in live_forms()[:2]:
            for cname, ch in SPLIT_CLASSES.items():
                for sname, (secret, values) in secret_shapes(ch).items():
                    raw = form.replace("@S@", secret)
                    for path, fn in adapters.items():
                        piece = leaked(fn(raw), values)
                        if piece is not None:
                            self.fail(f"{path}: «{piece}» of the secret [{sname}] torn by {cname} reached the output")

    def test_safe_text_masks_the_whole_word_when_a_secret_is_torn_by_a_control_separator(self):
        for cname, ch in SPLIT_CLASSES.items():
            for sname, (secret, values) in secret_shapes(ch).items():
                out = wab.safe_text(f"x {secret} y", 10 ** 6)
                self.assertIsNone(leaked(out, values), (cname, sname, out))
                self.assertIn("x ", out)
        self.assertEqual(wab.safe_text("5\u00a0мин и 3\u2009с", 100), "5\u00a0мин и 3\u2009с")  # an innocent text stays

    def test_one_line_masks_before_it_collapses(self):
        for cname, ch in SPLIT_CLASSES.items():
            out = wab._one_line(f"r password=sec{ch}retvalue99")
            self.assertIsNone(leaked(out, ["retvalue99"]), (cname, out))

    CHEAP_PATHS = ("quote", "render_notice", "first_line", "short_command", "note_question", "policy_question",
                   "say_echo", "blocked_notice", "dash_event", "attention")

    @staticmethod
    def torn_mask(out):
        """A piece of `[скрыто]` without its pair: a prefix not followed by the rest, or a rest not preceded by it."""
        mask = wab.MASK
        for k in range(2, len(mask)):
            if re.search(re.escape(mask[:k]) + "(?!" + re.escape(mask[k:]) + ")", out):
                return mask[:k]
        for k in range(1, len(mask) - 1):
            if re.search("(?<!" + re.escape(mask[:k]) + ")" + re.escape(mask[k:]), out):
                return mask[k:]
        return None

    def test_no_path_tears_the_mask_at_any_limit(self):
        adapters = self.adapters()
        pads = sorted({lim - d for lim in (40, 80, 120, 140, 160, 200, 220, 300, 400) for d in range(0, 30)})
        for token in ("ghp_AbCd1234EfGh5678IjKl9012MnOp3456", "password=Hunter2SecretValue99"):
            for pad in pads:
                raw = "x" * pad + " " + token + " хвост слов"
                for path in self.CHEAP_PATHS:
                    out = adapters[path](raw)
                    piece = self.torn_mask(out)
                    if piece is not None:
                        self.fail(f"{path}: a torn mask «{piece}» at pad {pad}: {out[-80:]!r}")

    def test_the_pulse_phrase_does_not_tear_the_mask(self):  # tester-r2-1 F2
        for pad in range(20, 45):
            msg = f"W1: phase=running ctx=1k restarts=0 status={'x' * pad} ghp_abcdefghijklmnopqrstuvwxyz0123456789"
            phrase, details, _c, _k = self.dash.humanize_event(msg)
            self.assertIsNone(self.torn_mask(f"{phrase}\n{details or ''}"), (pad, phrase))

    def test_the_fixture_is_live_and_clean(self):
        forms = live_forms()
        self.assertGreaterEqual(len(forms), 6)
        for line in forms:
            self.assertEqual(line.count("@S@"), 1, line)

    def test_every_path_hides_every_invisible_class_in_every_shape(self):
        adapters = self.adapters()
        for form in live_forms():
            for cname, ch in INVISIBLE_CLASSES.items():
                for sname, (secret, values) in secret_shapes(ch).items():
                    raw = form.replace("@S@", secret)
                    for path, fn in adapters.items():
                        out = fn(raw)
                        piece = leaked(out, values)
                        if piece is not None:
                            self.fail(f"{path}: «{piece}» of the secret [{sname}] with {cname} reached the output "
                                      f"(form: {form[:50]}…)")

    def test_a_clean_line_passes_through_every_path_not_masked_to_nothing(self):
        # the sanity of the check above: the secret values are not in the fixture text itself
        for form in live_forms():
            for _name, (_secret, values) in secret_shapes("").items():
                self.assertIsNone(leaked(form, values), form)

    def test_the_whole_value_survives_neither_with_nor_without_the_invisible_character(self):
        raw = live_forms()[0].replace("@S@", "password​=секретныйпароль123 конец")
        for path, fn in self.adapters().items():
            self.assertIsNone(leaked(fn(raw), ["секретныйпароль123"]), path)


class W2SafeText(Base):
    SAFE = staticmethod(lambda *a, **k: safe_text_fn()(*a, **k))
    MASK = "[скрыто]"

    def test_the_cut_never_leaves_a_part_of_a_secret_or_breaks_the_mask(self):
        texts = ["ключ password=Hunter2SecretValue99 и ещё слова после",
                 "x sk-ant-api03-AbCdEfGhIjKl1234567890 y zz",
                 f"текст {HEX40} и конец строки",
                 f"a​sk-ant-api03-AbCdEfGhIjKl1234567890 хвост",
                 f"password​=Hunter2SecretValue99 хвост"]
        values = ["Hunter2SecretValue99", "AbCdEfGhIjKl1234567890", HEX40]
        for text in texts:
            for limit in range(3, len(text) + 6):
                with self.subTest(text=text[:20], limit=limit):
                    out = self.SAFE(text, limit)
                    self.assertLessEqual(len(out), limit)
                    self.assertIsNone(leaked(out, values), out)
                    rest = out.replace(self.MASK, "").removesuffix(" …")
                    self.assertNotIn("[", rest, out)
                    self.assertNotIn("скры", rest, out)
                    self.assertNotIn("]", rest, out)

    def test_a_lone_cr_inside_a_word_does_not_leak_the_tail_of_a_secret(self):
        for raw in ("password=Hunter2\rSecretValue99", "x password=Hunter2Sec\rretValue99 y",
                    "sk-ant-api03-AbCdEf\rGhIjKl1234567890", "token=AbCdEfGh1234\r5678IjKlMnOp"):
            with self.subTest(raw=raw):
                out = self.SAFE(raw, 10 ** 6)
                self.assertIsNone(leaked(out, ["SecretValue99", "retValue99", "AbCdEfGhIjKl1234567890",
                                               "GhIjKl", "5678IjKlMnOp"]), out)

    def test_cr_between_innocent_words_stays_a_line_break(self):
        self.assertEqual(self.SAFE("первая\r\nвторая\rтретья", 10 ** 6, False).splitlines(),
                         ["первая", "вторая", "третья"])

    def test_the_mask_is_made_before_the_cut_never_after(self):
        out = self.SAFE("a" * 30 + " password=Hunter2SecretValue99", 45)
        self.assertNotIn("Hunter2", out)
        self.assertNotIn("password=Hun", out)

    def test_redact_itself_does_not_break_the_mask_when_it_cuts(self):
        text = "слово " * 4 + "password=Hunter2SecretValue99 хвост"
        for limit in range(3, len(text)):
            out = wab.redact(text, limit)
            rest = out.replace(self.MASK, "").removesuffix(" …")
            self.assertNotIn("скры", rest, (limit, out))
            self.assertNotIn("[", rest, (limit, out))

    def test_the_registry_and_the_static_check(self):
        self.assertEqual(redact_uses_outside(WAVES), [])

    def test_a_replaced_safe_text_is_caught_by_the_static_check_and_by_the_path_test(self):
        tmp = self.tmp / "mut"
        shutil.copytree(WAVES, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        src = (tmp / "wab.py").read_text(encoding="utf-8")
        self.assertIn("safe_text(text, 300)", src)
        (tmp / "wab.py").write_text(src.replace("safe_text(text, 300)", "redact(text, 300)"),
                                    encoding="utf-8")
        found = redact_uses_outside(tmp)
        self.assertIn("note_question", [f[2] for f in found], found)
        spec = importlib.util.spec_from_file_location("wab_mutant", tmp / "wab.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        w = {}
        mod.note_question(w, "password​=Hunter2SecretValue99", 1.0)
        self.assertIsNotNone(leaked(w["questions"][-1]["text"], ["Hunter2SecretValue99"]))
        w = {}
        wab.note_question(w, "password​=Hunter2SecretValue99", 1.0)  # the real one stays clean
        self.assertIsNone(leaked(w["questions"][-1]["text"], ["Hunter2SecretValue99"]))

    def test_a_replaced_safe_text_in_the_dashboard_is_caught_by_the_static_check(self):
        tmp = self.tmp / "mut2"
        shutil.copytree(WAVES, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        src = (tmp / "dash.py").read_text(encoding="utf-8")
        self.assertIn("wab.safe_text(", src)
        (tmp / "dash.py").write_text(src.replace("wab.safe_text(", "wab.redact(", 1), encoding="utf-8")
        self.assertTrue([f for f in redact_uses_outside(tmp) if f[0] == "dash.py"])

    # Codex on #85: safe_text() went quadratic on a long word (`[^ ]*X[^ ]*` retried from every character), and so did
    # redact() of 1.1.0 (the e-mail rule, the `key=value` rule after every `.`, the label of a sha looked for in the
    # whole text before every match). 16 000 characters took 18 s, 1 MB of mixed text did not end in minutes.
    LINES = ["2026-10-05 09:22:38Z W1: DONE, awaiting merge by the coordinator",
             "ошибка: не удалось выполнить gh pr view 85 (exit 1)", "password=Hunter2SecretValue99 и токен ghp_" + "A" * 36,
             "см. https://github.com/o/r/pull/85#issuecomment-5991616185 и /o/r/commit/" + HEX40,
             "путь /home/user/projects/x/runs/demo/W1/next-prompt.md", "a\u200bsk-ant-api03-AbCdEfGhIjKl1234567890 хвост",
             "строка\rс возвратом каретки", "user@example.com +7 912 345-67-89", "sha " + HEX40, B64]

    def test_safe_text_is_linear_on_a_long_word_and_on_a_megabyte(self):
        for unit in ("a", "1", "a.", "Ab9-_.", "token.", ".sig", "x.token-", "a@", "a\r", "a\u200b", "eyJ" + "A" * 12 + "-"):
            text = (unit * 16000)[:16000]
            with self.subTest(unit=unit):
                start = time.monotonic()
                self.SAFE(text, 10 ** 9)
                self.assertLess(time.monotonic() - start, 0.5, unit)
        text = "\n".join(self.LINES[i % len(self.LINES)] for i in range(40000))[:1_000_000]
        start = time.monotonic()
        out = self.SAFE(text, 10 ** 9)
        # linear, it takes ~0.5 s here (19 rules over 1 MB); quadratic, it took minutes: 2 s keeps CI runners apart
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertIsNone(leaked(out, ["Hunter2SecretValue99", "A" * 36, "AbCdEfGhIjKl1234567890", B64]))

    def test_a_long_key_of_a_structure_is_checked_in_linear_time(self):
        # Astra on #85: _key_hides_value took `(?i)[\w.-]*WORD[\w.-]*` whole, and a key `token`*N + `!` retried the tail
        # after every `token` (quadratic: 100 000 characters took seconds)
        for n in (2000, 20000):
            key = "token" * n + "!"
            for obj in ({key: "Hunter2SecretValue99"}, [{"errors": [{key: 1}]}], ValueError({key: "v"})):
                with self.subTest(n=n, obj=type(obj).__name__):
                    start = time.monotonic()
                    self.SAFE(obj, 10 ** 9)
                    list(gate._leaves(obj))  # the gate asks the same predicate (gate.key_hides_value)
                    self.assertLess(time.monotonic() - start, 0.5)

    def test_the_split_key_check_gives_the_results_of_the_old_one(self):
        old = re.compile(r"(?i)(?:" + wab._SECRET_KEY + r"|(?:proxy-)?authorization)")
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        keys = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 200}
        rnd = random.Random(84)
        toks = ["token", "TOKEN", "sig", "SIG", "authorization", "proxy-", "Proxy-Authorization", "api", "_", "-", "key",
                "secret", ".", "!", " ", "a", "1", "ж", "\u212a", "\"", "=", "private-key", "connection_string", "dsn"]
        keys |= {"".join(rnd.choice(toks) for _ in range(rnd.randint(0, 8))) for _ in range(20000)}
        self.assertEqual([k for k in sorted(keys) if bool(old.fullmatch(k)) != wab._secret_key(k)], [])

    def test_the_linear_rules_give_the_results_of_the_old_ones(self):
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        corpus = sorted({n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                         and len(n.value) < 5000})
        corpus += [p.read_text(encoding="utf-8") for p in sorted(REDACT_FIXTURES.iterdir()) if p.suffix == ".txt"]
        rnd = random.Random(85)
        toks = ["token", "sig", "SIG", ".", "=", ":", " ", "\"", "'", "@", "a", "1", "x@y.zz", "-", "_", "/", "+7", "\r", "\u200b",
                "\n", "\u00a0", HEX40, "sha ", "reviewed_head:", "/commits/", "https://github.com/o/r/", "github.com/x/", B64, "ж",
                ".cache/wab/", "ghp_" + "A" * 36, "[скрыто]", ",", "(", "#", "eyJ", "AAAAAAAAAA", "AAAAAAAAAA."]
        corpus += ["".join(rnd.choice(toks) for _ in range(rnd.randint(0, 16))) for _ in range(20000)]
        spaces = f"[^{wab._SPACES}]*"
        ctrl_word, soft_word = re.compile(spaces + f"[{wab._INVISIBLE}]" + spaces), re.compile(spaces + "\r" + spaces)

        def old_safe_text(text, owner_paths):  # the two word rules of the old safe_text, then the same redact()
            text = ctrl_word.sub(self.MASK, wab._text_of(text, owner_paths).replace("\r\n", "\n"))

            def soft(m):
                glued = m.group(0).replace("\r", "")
                return self.MASK if wab.redact(glued, 10 ** 9, owner_paths) != glued else m.group(0).replace("\r", "\n")
            return wab._clip(wab.Masked(wab.redact(soft_word.sub(soft, text), 10 ** 9, owner_paths)), 10 ** 9)

        old_email = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
        old_key = re.compile(r"(?i)(?<![\w-])([\"']?" + wab._SECRET_KEY + r"[\"']?)(\s*[:=]\s*)"
                             r"(?!\[скрыто\])(?:\"(?:[^\"\\]|\\.)*\"?|'(?:[^'\\]|\\.)*'?|[^\s,;&}\]]+)")
        new_key = next(rx for rx in wab._STRUCTURED if getattr(rx, "pattern", "").startswith("(?i)(?:(?<![\\w.-])"))
        old_jwt = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
        old_of = {id(wab._EMAIL): old_email, id(new_key): old_key, id(wab._JWT): old_jwt}
        structured = [old_of.get(id(rx), rx) for rx in wab._STRUCTURED]
        old_rules = [mock.patch.object(wab, "_STRUCTURED", structured),
                     mock.patch.object(wab, "_labelled_sha", lambda m: bool(
                         re.fullmatch(r"[0-9a-fA-F]{40,64}", m.group(0)) and wab._SHA_LABEL.search(m.string[:m.start()]))),
                     mock.patch.object(wab, "_after_commit_path", lambda m: bool(wab._GITHUB_SHA.search(m.string[:m.start()]))),
                     mock.patch.object(wab, "_within", lambda spans, m: any(a <= m.start() and m.end() <= b for a, b in spans))]
        new = [(wab.redact(t, 10 ** 9, op), self.SAFE(t, 10 ** 9, op)) for t in corpus for op in (False, True)]
        with contextlib.ExitStack() as stack:
            for patch in old_rules:
                stack.enter_context(patch)
            old = [(wab.redact(t, 10 ** 9, op), old_safe_text(t, op)) for t in corpus for op in (False, True)]
        diff = [(t, op) for (t, op), a, b in zip([(t, op) for t in corpus for op in (False, True)], new, old) if a != b]
        self.assertEqual(diff, [])



class Packaging(unittest.TestCase):
    def test_wab_py_is_stdlib_only(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        mods = set(re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", src))
        stdlib = set(sys.stdlib_module_names) | {"gate"}  # gate.py is the sibling module, stdlib-only itself
        self.assertFalse(mods - stdlib, mods - stdlib)
        gate_src = (WAVES / "gate.py").read_text(encoding="utf-8")
        gate_mods = set(re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", gate_src))
        self.assertFalse(gate_mods - set(sys.stdlib_module_names), gate_mods)
        self.assertNotRegex(src, r"curl[^\n]*\|\s*python")

    def test_protocol_describes_the_waves_mode(self):
        text = (WAVES / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("--waves", text)
        self.assertIn("manifest", text)
        self.assertIn("result.md", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
