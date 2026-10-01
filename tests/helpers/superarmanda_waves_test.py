#!/usr/bin/env python3
"""Acceptance tests A1-A13 for the wave dispatcher in scripts/waves/.

Nothing here talks to a user's tmux server: every tmux/claude/Telegram interaction goes
through module functions that are patched, TMUX is removed from the environment and
TMUX_TMPDIR points into a throw-away directory. The only real tmux use is A8, on a private
`-L wabtest-<pid>` socket that is killed afterwards.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

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
        patch("send_text", side_effect=lambda n, t: self.sent.append(("text", n, t)))
        patch("send_command", side_effect=lambda n, t: self.sent.append(("cmd", n, t)))
        patch("wait_ready", side_effect=lambda name, timeout=90: self.ready)
        patch("require_tmux")
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
        keep = ["commit 3f2a9c1 and 3f2a9c1d4e5b6a7988776655443322110099aabb", "see PR #12 and #345",
                "date 2026-10-01 at 12:34:56", "wave W4 ctx 300000 tokens", "tokenizer notes; session closed",
                "version 3.4.1 build 20261001"]
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text, 1200), text)

    def test_limits(self):
        long = "слово " * 400
        self.assertLessEqual(len(wab.redact(long)), 602)
        self.assertLessEqual(len(wab.redact(long, 1200)), 1202)
        self.assertEqual(wab.redact("short"), "short")

    def test_notify_redacts_and_caps_the_whole_message(self):
        cfg, _ = self.chain()
        wab.notify(cfg, "leak password=hunter2hunter2 " + "я" * 3000)
        self.assertEqual(len(self.tg), 1)
        self.assertNotIn("hunter2hunter2", self.tg[0])
        self.assertLessEqual(len(self.tg[0]), 1202)

    def test_quoted_wave_text_is_capped_at_600(self):
        cfg, _ = self.chain()
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
        self.set_status(cfg, "W1", "DONE")
        (wab.wave_dir(cfg, "W1") / "result.md").write_text("ж" * 5000, encoding="utf-8")
        with mock.patch.object(wab, "launch"):
            wab.tick(cfg, wab.load_state(cfg))
        self.assertTrue(self.tg)
        self.assertLess(self.tg[0].count("ж"), 620)

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

        def fake(*args, **kw):
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, "value\n", "")

        orig = load_orig("wab_orig")
        with mock.patch.object(orig, "sh", side_effect=fake), \
                mock.patch.object(orig.urllib.request, "urlopen") as urlopen:
            orig._send_telegram(cfg, "hi")
        flat = [" ".join(a) for a in seen]
        self.assertTrue(all("kv-test" in f for f in flat), flat)
        self.assertTrue(any("tok" in f for f in flat) and any("chat" in f for f in flat))
        urlopen.assert_called_once()
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

    def test_launch_refuses_an_unadmitted_workdir(self):
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
        with mock.patch.object(wab, "bind_popup"):
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

    def boom_cmd(self, *a):
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
        def boom_text(name, text):
            raise RuntimeError("dispatcher died")
        wab.send_text.side_effect = boom_text
        with self.assertRaises(RuntimeError):
            wab.tick(self.cfg, wab.load_state(self.cfg))
        w = self.w()
        self.assertEqual((w["phase"], w["restarts"], w["await_session"]), ("updating", 0, True))
        self.assertEqual(self.kinds(), (1, 0))
        self.heal()
        for _ in range(2):
            with mock.patch.object(wab, "bind_popup"):
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
        with mock.patch.object(wab, "bind_popup"):
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

        def delivered_then_died(name, text):
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
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "launch", return_value=True) as ln:
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
        with mock.patch.object(wab, "bind_popup"):
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
        with mock.patch.object(wab, "bind_popup"):
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

        def fake_launch(c, wave, prompt):
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
        with mock.patch.object(wab, "bind_popup") as bind:
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
        with mock.patch.object(wab, "bind_popup"):
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
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
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


class RegistryLock(Base):
    def make_chain(self, n):
        d = self.tmp / f"c{n}"
        d.mkdir()
        doc = {"chain": f"ch{n}", "run_id": RUN_ID, "repo": "o/r", "waves": ["W1"], "tmux_prefix": f"p{n}-"}
        path = d / "chain.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return wab.load_chain(path), path

    def test_a_held_registry_lock_refuses_the_binding_without_touching_anything(self):
        cfg, path = self.chain()
        lock = wab.registry_path().with_name("dashes.lock")
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
             "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)", str(lock)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with mock.patch.object(wab, "REGISTRY_LOCK_TIMEOUT", 0.3):
            wab.bind_popup(cfg, path)  # must not raise
        self.assertFalse(wab.registry_path().exists())
        self.assertEqual([c for c in self.tmux_calls if c[1] == "source-file"], [])
        self.assertIn("registry", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_concurrent_registrations_keep_every_chain(self):
        import threading
        chains = [self.make_chain(n) for n in range(8)]
        gate = threading.Barrier(len(chains))
        errors = []

        def run(cfg, path):
            try:
                gate.wait()
                wab.bind_popup(cfg, path)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=c) for c in chains]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        reg = json.loads(wab.registry_path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(reg), sorted(f"p{n}-dash" for n in range(8)))
        last = max(chains, key=lambda c: (c[0]["run_dir"] / "keys.tmux").stat().st_mtime_ns)
        conf = (last[0]["run_dir"] / "keys.tmux").read_text(encoding="utf-8")
        self.assertTrue(all(f"p{n}-dash" in conf for n in range(8)))


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
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "tick", side_effect=fake_tick):
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
        with mock.patch.object(wab, "bind_popup"):
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
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "launch") as again:
            wab.watch(cfg, path, max_ticks=1)
        again.assert_not_called()
        self.assertEqual(len([c for c in self.tmux_calls if c[1] == "new-session"]), news)
        self.assertEqual(self.get_state(cfg)["waves"]["W2"]["phase"], "running")

    def test_resume_in_awaiting_merge_exits_with_the_same_hint(self):
        cfg, path = self.chain(merge_gate="external")
        self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec(phase="awaiting_merge")}})
        self.set_status(cfg, "W1", "DONE")
        with mock.patch.object(wab, "bind_popup"):
            wab.watch(cfg, path, max_ticks=50)
        self.assertIn("handed to the coordinator", (cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        self.lock_is_free(cfg)

    def test_last_wave_ends_the_watch_in_both_modes(self):
        for gate in (None, "external"):
            with self.subTest(gate=gate):
                cfg, path = self.chain(waves=["W1"], merge_gate=gate)
                self.put_state(cfg, {"current": "W1", "waves": {"W1": self.wave_rec()}})
                self.set_status(cfg, "W1", "DONE")
                with mock.patch.object(wab, "bind_popup"):
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
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "prepare_clone", return_value=self.cwd):
            wab.watch(cfg, path, max_ticks=3)
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("next wave W2 not started: TUI not ready", log)
        self.assertNotIn("no current wave", log)


class AtMostOnce(Base):
    def crash_then_tick(self, cfg, **extra):
        with mock.patch.object(wab, "notify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wab.tick(cfg, wab.load_state(cfg))
        with mock.patch.object(wab, "notify") as again, mock.patch.object(wab, "launch", return_value=True):
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
        with mock.patch.object(wab, "notify") as again, mock.patch.object(wab, "bind_popup"):
            wab.watch(cfg, cfg_path(cfg), max_ticks=1)
        self.assertFalse([c for c in again.call_args_list if "стартовала" in c[0][1]])


def cfg_path(cfg):
    return cfg["chain_file"]


class Resume(Base):
    def run_watch(self, cfg, path, ticks=1):
        with mock.patch.object(wab, "bind_popup"), mock.patch.object(wab, "launch") as launch:
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
    def reg(self):
        opener = str(WAVES / "wab-open")
        return {
            "wv-dash": [opener, "/srv/a/chain.json"],
            "xy-dash": [opener, "/srv/b/chain.json"],
        }

    def test_conf_covers_both_chains_and_leaves_w_and_shift_tab_alone(self):
        conf = wab.keys_conf(self.reg())
        self.assertIn("wv-dash", conf)
        self.assertIn("xy-dash", conf)
        self.assertIn("/srv/a/chain.json", conf)
        self.assertIn("/srv/b/chain.json", conf)
        self.assertIn("ignore-size", conf)
        self.assertEqual(len(re.findall(r"(?m)^bind-key\b", conf)), 1)
        self.assertNotRegex(conf, r"bind(-key)?\s[^\n]*\bW\b")
        self.assertNotIn("BTab", conf)
        self.assertNotIn("unbind", conf)

    def test_unsafe_values_are_refused(self):
        opener = str(WAVES / "wab-open")
        bad = [
            ["/tmp/a b/chain.json"], ['/tmp/a"b/chain.json'], ["/tmp/a$(x)/chain.json"],
            ["/tmp/a;b/chain.json"], ["/tmp/a`b/chain.json"], ["relative/chain.json"],
            ["/tmp/a\nbind-key x y/chain.json"], ["/tmp/a{b/chain.json"], ["/tmp/a#b/chain.json"],
        ]
        for (chain,) in bad:
            with self.subTest(chain=chain):
                with self.assertRaises(ValueError):
                    wab.keys_conf({"wv-dash": [opener, chain]})
        with self.assertRaises(ValueError):
            wab.keys_conf({"wv-dash;kill-server": [opener, "/srv/a/chain.json"]})
        with self.assertRaises(ValueError):
            wab.keys_conf({"wv-dash": ["/o pener", "/srv/a/chain.json"]})

    def test_generated_file_parses_in_a_private_tmux_server(self):
        exe = shutil.which("tmux")
        if not exe:
            self.skipTest("tmux is not installed: cannot check keys.tmux syntax")
        out = subprocess.run([exe, "-V"], capture_output=True, text=True, encoding="utf-8").stdout
        ver = wab.parse_tmux_version(out)
        if ver is None or ver < (3, 2):
            self.skipTest(f"tmux {out.strip()!r} is older than 3.2: source-file -n is unavailable")
        conf = self.tmp / "keys.tmux"
        conf.write_text(wab.keys_conf(self.reg()), encoding="utf-8")
        broken = self.tmp / "broken.tmux"
        broken.write_text('bind-key -n "C-\\\\" { if-shell -F "x" {\n', encoding="utf-8")
        sock = f"wabtest-{os.getpid()}"
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}

        def parse(path):
            return subprocess.run([exe, "-L", sock, "-f", "/dev/null", "start-server", ";",
                                   "source-file", "-n", str(path)],
                                  capture_output=True, text=True, encoding="utf-8", env=env)
        try:
            good = parse(conf)
            bad = parse(broken)
        finally:
            subprocess.run([exe, "-L", sock, "kill-server"], capture_output=True, env=env)
        self.assertEqual((good.returncode, good.stderr.strip()), (0, ""), good.stderr)
        self.assertNotEqual(bad.returncode, 0, "the syntax check must be able to fail")

    def test_bind_popup_registers_prunes_and_refuses(self):
        cfg, path = self.chain()
        reg_path = wab.registry_path()
        reg_path.parent.mkdir(parents=True)
        alive = self.tmp / "other.json"
        alive.write_text("{}", encoding="utf-8")
        reg_path.write_text(json.dumps({
            "old-dash": [str(WAVES / "wab-open"), str(alive)],
            "gone-dash": [str(WAVES / "wab-open"), str(self.tmp / "missing.json")],
        }), encoding="utf-8")
        wab.bind_popup(cfg, path)
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(reg), ["old-dash", "wv-dash"])
        conf = (cfg["run_dir"] / "keys.tmux").read_text(encoding="utf-8")
        self.assertIn("old-dash", conf)
        self.assertNotIn("gone-dash", conf)
        self.assertTrue([c for c in self.tmux_calls if c[1] == "source-file"])
        self.assertEqual(list(reg_path.parent.glob("*.tmp")), [])

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
        wab.bind_popup(cfg, path)
        return [c for c in calls if c[1] == "unbind-key"]

    def test_bind_popup_removes_only_the_stale_wab_btab_binding(self):
        stale = 'bind-key -T root BTab if-shell -F "#{m:*ignore-size*,#{client_flags}}" { send-keys BTab }'
        self.assertEqual(len(self.run_bind(stale)), 1)

    def test_bind_popup_leaves_a_foreign_btab_binding_alone(self):
        self.assertEqual(self.run_bind("bind-key -T root BTab select-pane -t :.-"), [])
        self.assertEqual(self.run_bind(""), [])

    def test_bind_popup_refuses_an_unsafe_path_without_touching_tmux(self):
        spaced = self.tmp / "my dir"
        spaced.mkdir()
        cfg, path = self.chain()
        bad = spaced / "chain.json"
        bad.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        wab.bind_popup(cfg, bad)
        self.assertEqual([c for c in self.tmux_calls if c[1] == "source-file"], [])
        self.assertFalse(wab.registry_path().exists())
        log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        self.assertIn("refused", log)

    def test_wab_open_contract(self):
        script = WAVES / "wab-open"
        self.assertTrue(os.access(script, os.X_OK))
        text = script.read_text(encoding="utf-8")
        self.assertIn("tmux -u attach", text)
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

    def test_mandate_is_decoded_as_utf8_whatever_the_locale(self):
        body = f"Прогон: {RUN_ID}\nкириллица\n".encode("utf-8")
        cfg, _ = self.chain(mandate_sha256=hashlib.sha256(body).hexdigest())
        (cfg["run_dir"] / "mandate.md").write_bytes(body)
        with mock.patch("locale.getpreferredencoding", return_value="ANSI_X3.4-1968"):
            text = wab.system_prompt(cfg).read_text(encoding="utf-8")
        self.assertIn("кириллица", text)


class ChainConfig(Base):
    def test_run_directory_layout(self):
        cfg, path = self.chain()
        self.assertEqual(cfg["run_dir"], path.parent / "runs" / CHAIN / RUN_ID)
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
        text = self.render_text(cfg)
        self.assertIn("Ctrl+\\", text)
        self.assertIn("W1", text)

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
