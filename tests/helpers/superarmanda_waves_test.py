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

import atexit
import contextlib
import hashlib
import io
import json
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

# ---------- TMUX_GUARD: no test may touch a tmux server it did not create ----------
def _install_tmux_guard():
    tmpdir = tempfile.mkdtemp(prefix="wabtmux-")
    atexit.register(shutil.rmtree, tmpdir, True)
    os.environ["TMUX_TMPDIR"] = tmpdir
    for var in ("TMUX", "TMUX_PANE", "WAB_TMUX_SOCKET"):
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
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


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
        self.pane = ""
        self.ready = True
        self.tg = []
        self.cwd = str(self.tmp / "clone")
        Path(self.cwd).mkdir()

        def fake_sh(*args, **kw):
            if args and args[0] == "tmux":
                self.tmux_calls.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return REAL_SH(*args, **kw)

        def patch(name, **kw):
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)

        patch("sh", side_effect=fake_sh)
        patch("tmux_alive", side_effect=lambda name: self.alive)
        patch("pane_text", side_effect=lambda name: self.pane)
        patch("send_text", side_effect=lambda n, t, **kw: self.sent.append(("text", n, t)))
        patch("send_command", side_effect=lambda n, t, **kw: self.sent.append(("cmd", n, t)))
        self.enters = []
        patch("press_enter", side_effect=lambda n: self.enters.append(n))
        patch("wait_ready", side_effect=lambda name, timeout=90: self.ready)
        patch("require_tmux")
        # the auto-merge branch is W5; legacy launch tests exercise it, the fail-closed ones turn it off
        patch("MERGE_GATE_IMPLEMENTED", new=True, create=True)
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
        update = [s for s in self.sent if s[2].startswith("/update")]
        self.assertEqual(len(update), 1)
        self.assertIn(f"[wab:{CHAIN}/{RUN_ID}/W1]", update[0][2])
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
        cfg, _ = self.chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("PR #1\n", encoding="utf-8")
        (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "launch", return_value=True) as launch:
            self.assertTrue(wab.tick(cfg, wab.load_state(cfg)))
        launch.assert_called_once()
        self.assertEqual(launch.call_args[0][1], "W2")
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "done")

    def test_done_is_not_notified_twice_when_watch_restarts_before_the_next_launch(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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

    def test_arbitrary_git_directory_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        self.assertIsNotNone(wab.admitted_workdir(plain))
        self.assertIsNotNone(wab.admitted_workdir(self.tmp / "not-a-repo"))

    def test_extra_entry_in_the_root_is_refused(self):
        root = self.make_root(extra="stray")
        self.assertIsNotNone(wab.admitted_workdir(root / "checkout"))

    def test_correct_signature_is_accepted(self):
        root = self.make_root()
        self.assertIsNone(wab.admitted_workdir(root / "checkout"))

    def test_derived_worktree_is_accepted(self):
        root = self.make_root()
        co = root / "checkout"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
        subprocess.run(["git", "-C", str(co), "commit", "-q", "--allow-empty", "-m", "i"],
                       check=True, env={**os.environ, **env})
        wt = self.tmp / "wt"
        subprocess.run(["git", "-C", str(co), "worktree", "add", "-q", "-b", "x", str(wt)], check=True)
        self.assertIsNone(wab.admitted_workdir(wt))

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

    def test_host_policy_keeps_the_signature_check_and_prepare(self):
        self.host_policy()
        plain = self.tmp / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        cfg, _ = self.chain(workdir=str(plain))
        with self.assertRaises(SystemExit):
            wab.prepare_clone(cfg)
        root = self.make_root()
        cfg, _ = self.chain(workdir=str(root / "checkout"))
        self.assertEqual(wab.prepare_clone(cfg), str((root / "checkout").resolve()))
        self.assertIn("host policy", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        cfg, _ = self.chain()
        out = json.dumps({"ok": True, "admitted_path": "/x/y"})
        with mock.patch.object(wab, "sh", return_value=subprocess.CompletedProcess([], 0, out, "")) as m:
            self.assertEqual(wab.prepare_clone(cfg), "/x/y")
        self.assertIn("prepare", m.call_args[0])

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
        updates = [s for s in self.sent if s[0] == "text" and s[2].startswith("/update")]
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
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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
        cfg, path = self.chain()
        self.alive = False
        self.ready = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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
        self.assertEqual([x for x in self.sent if x[2].startswith("/update")], [])
        self.assertEqual((self.w(cfg)["phase"], self.w(cfg)["restarts"]), ("running", 1))

    def test_watch_exits_nonzero_when_the_next_wave_does_not_reach_running(self):
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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
        w = self.wave_rec(phase="sending", pending_enter="first prompt")
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
        self.assertEqual((self.enters, len(self.tg)), (["wv-w1"], 1))

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
                    self.assertEqual(len(setc), 1)
                    self.assertEqual(list(setc[0][2 + n:6 + n]), ["-p", "-t", "%7", "@wab_open"])
                    self.assertEqual(setc[0][6 + n], f"{WAVES / 'wab-open'} {path.resolve()}")
                    self.assertIn("source-file", {c[1 + n] for c in self.tmux_calls})
                    for call in self.tmux_calls:
                        self.assertEqual(list(call[1:1 + n]), flag, call)
                        self.assertNotIn(call[1 + n], ("-L", "-S"), call)

    def test_unregister_unsets_the_option_on_the_same_server(self):
        wab.unregister_dash("my-work", "/p/x.sock", "%7")
        self.assertEqual(self.tmux_calls[-1],
                         ("tmux", "-S", "/p/x.sock", "set-option", "-p", "-u", "-t", "%7", "@wab_open"))
        wab.unregister_dash("my-work", "/p/x.sock")  # no pane known: the session option, as before
        self.assertEqual(self.tmux_calls[-1],
                         ("tmux", "-S", "/p/x.sock", "set-option", "-u", "-t", "=my-work:", "@wab_open"))

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
        for bad in ("externl", "EXTERNAL", "auto", 1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    self.chain(merge_gate=bad)
                self.assertIn("merge_gate", str(ctx.exception))
        for ok in (None, "", "external"):
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

    def test_without_merge_gate_done_hands_off_until_w5_exists(self):
        cfg, path = self.chain()
        self.alive = False
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        nxt = wab.wave_dir(cfg, "W1") / "next-prompt.md"
        nxt.write_text("go\n", encoding="utf-8")
        with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", False, create=True), \
                mock.patch.object(wab, "launch") as launch, mock.patch.object(wab, "drop_stale_btab"):
            wab.watch(cfg, path, max_ticks=5)
        launch.assert_not_called()
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "awaiting_merge")
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("merge gate not implemented yet (W5); handing off to coordinator", log)
        self.assertIn(f"wab.py launch {path.resolve()} W2 {nxt}", log)

    def test_last_wave_without_merge_gate_finishes_as_before(self):
        cfg, _ = self.chain(waves=["W1"])
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", False, create=True):
            self.assertFalse(wab.tick(cfg, wab.load_state(cfg)))
        self.assertIsNone(self.get_state(cfg)["current"])
        self.assertIn("chain finished", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_external_done_without_next_prompt_stops_loudly(self):
        for gate, impl in (("external", True), (None, False)):
            with self.subTest(gate=gate):
                self.tg.clear()
                cfg, path = self.chain(merge_gate=gate)
                (cfg["run_dir"] / "events.log").unlink(missing_ok=True)  # shared run dir between subtests
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", impl, create=True):
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
        with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", False):
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
        cfg, _ = self.chain()
        self.alive = False
        old = {"value": "x", "text": "old notice", "next_at": 0}
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(outbox={"no_next": old})}})
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
        self.assertEqual(sorted(t for t in self.tg if t.startswith("old")), ["old chain_done"])

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
        cfg, path = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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
        self.assertEqual(self.count("при передаче /update"), 0, self.tg)

    def test_updating_is_delivered_once_while_unknown(self):
        self.updating()
        self.restore()
        self.tick()
        self.restore()
        self.assertEqual(self.count("при передаче /update"), 1, self.tg)

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
        self.assertEqual(sorted(self.tg), ["old chain_done", "old done", "old no_next"])


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


class Packaging(unittest.TestCase):
    def test_wab_py_is_stdlib_only(self):
        src = (WAVES / "wab.py").read_text(encoding="utf-8")
        mods = set(re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", src))
        stdlib = set(sys.stdlib_module_names)
        self.assertFalse(mods - stdlib, mods - stdlib)
        self.assertNotRegex(src, r"curl[^\n]*\|\s*python")

    def test_protocol_describes_the_waves_mode(self):
        text = (WAVES / "PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("--waves", text)
        self.assertIn("manifest", text)
        self.assertIn("result.md", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
