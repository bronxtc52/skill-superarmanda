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
        self.assertEqual(sorted(self.tg), ["old chain_done", "old done", "old no_next"])


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
        for gate, impl in (("external", True), (None, False), (None, True)):
            for content in (b"", b"  \n\t", b"\xff\xfe bad", "\u200b\u3164".encode()):
                with self.subTest(gate=gate, impl=impl, content=content):
                    self.tg.clear()
                    cfg, _ = self.chain(merge_gate=gate)
                    (cfg["run_dir"] / "events.log").unlink(missing_ok=True)
                    self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                    self.set_status(cfg, "W1", "DONE")
                    (wab.wave_dir(cfg, "W1") / "next-prompt.md").write_bytes(content)
                    with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", impl, create=True), \
                            mock.patch.object(wab, "launch") as launch:
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
        cases = [  # (name, merge_gate, gate implemented, waves, next-prompt.md, then)
            ("awaiting_merge, next wave", "external", True, ["W1", "W2"], b"go\n", None),
            ("awaiting_merge, last wave + done", "external", True, ["W1"], None, "done"),
            ("external, no next-prompt", "external", True, ["W1", "W2"], None, None),
            ("external, unusable next-prompt", "external", True, ["W1", "W2"], b" \n", None),
            ("no gate yet, no next-prompt", None, False, ["W1", "W2"], None, None),
            ("no gate yet, last wave + done", None, False, ["W1"], None, "done"),
            ("internal, last wave", None, True, ["W1"], None, None),
            ("internal, no next-prompt", None, True, ["W1", "W2"], None, None),
            ("internal, unusable next-prompt", None, True, ["W1", "W2"], b"\xff", None),
            ("internal, next launched by the dispatcher", None, True, ["W1", "W2"], b"go\n", "launch"),
        ]
        for name, gate, impl, waves, nxt, then in cases:
            with self.subTest(case=name):
                self.windows = {"wv-w1"}
                self.tmux_calls.clear()
                cfg, path = self.chain(merge_gate=gate, waves=waves)
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                p = wab.wave_dir(cfg, "W1") / "next-prompt.md"
                p.unlink(missing_ok=True)
                if nxt is not None:
                    p.write_bytes(nxt)
                with mock.patch.object(wab, "MERGE_GATE_IMPLEMENTED", impl, create=True), \
                        mock.patch.object(wab, "launch", return_value=True):
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
        cfg, path = self.chain(workdir=str(wd), base_branch="main")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(cwd=str(wd))}})
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

    def test_dispatcher_launch_of_next_wave_refuses_on_changed_plan(self):
        cfg, _ = self.pinned()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
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

    def test_plan_pin_is_tunable(self):
        cfg, _ = self.pinned()
        other = dict(cfg, plan_sha256="1" * 64)
        self.assertEqual(wab._identity(cfg), wab._identity(other))


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
