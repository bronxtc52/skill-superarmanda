#!/usr/bin/env python3
"""1.4.0 (#108, T4 and T5): the Fable limit of a wave window and the local Fable usage count.

T4: a wave session that runs on Fable and hits the subscription limit (a main-thread assistant line of its
transcript with `error: "rate_limit"` or `apiErrorStatus: 429`) is closed and started again on Opus with the
resume message; the chain does not stop, the restarts/runs of the wave are not charged.
T5: scripts/waves/fable_usage.py counts the weighted Fable tokens of the local Claude Code journals; `wab.py
launch` writes the count as an event and never refuses on it.

Every tmux/claude interaction goes through the patched module functions of the shared Base (the TMUX guard
of superarmanda_waves_test is installed by importing it); the journals are synthetic.
"""

import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from superarmanda_waves_test import Base, WAVES, asst  # noqa: E402  (installs the TMUX guard)

import wab  # noqa: E402

sys.path.insert(0, str(WAVES))
import fable_usage  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts"
FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5-5"
SWITCH_NOTE = "модель переключена на Opus из-за лимита Fable; отметь это в PR"


def limit_lines():
    """The two live rate-limit lines of the fixture with their placeholders filled in (main thread, sidechain)."""
    out = []
    for n, raw in enumerate(FIXTURES.joinpath("rate-limit-assistant.jsonl").read_text(encoding="utf-8").splitlines()):
        if not raw.strip():
            continue
        text = (raw.replace("{TIMESTAMP}", "2026-10-10T10:00:00.000Z").replace("{SESSION_ID}", "s1")
                .replace("{REQUEST_ID}", "req_x"))
        for i in range(1, 12):
            text = text.replace("{UUID:%d}" % i, f"u{n}-{i}")
        out.append(text)
    return out


class FableLimitBase(Base):
    def setUp(self):
        super().setUp()
        inner = wab.sh.side_effect

        def window_appears(*args, **kw):  # a mocked `tmux new-session` really opens the window
            r = inner(*args, **kw)
            if args and args[0] == "tmux" and args[1:2] == ("new-session",):
                self.alive = True
            return r
        wab.sh.side_effect = window_appears

    def setup_wave(self, model=FABLE, lines=None, **rec):
        self.cfg, self.path = self.chain(model=model)
        self.transcript("s1", [asst(inp=1000)] + list(lines if lines is not None else limit_lines()[:1]))
        w = self.wave_rec(sessions=["s1"], **rec)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}, "defaults": "1.4"})
        self.set_status(self.cfg, "W1", "RUNNING")

    def w(self):
        return self.get_state(self.cfg)["waves"]["W1"]

    def events(self):
        p = self.cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def new_sessions(self):
        return [list(c) for c in self.tmux_calls if c[1] == "new-session"]

    def kills(self):
        return [c for c in self.tmux_calls if c[1] == "kill-session"]

    def resumes(self):
        return [s for s in self.sent if s[0] == "text" and s[2].startswith("/superarmanda --wave W1 --resume")]

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, wab.load_state(self.cfg))


class FableLimitSwitch(FableLimitBase):
    def test_fable_limit_relaunches_the_wave_on_opus(self):
        self.setup_wave()
        self.assertTrue(self.tick())
        w = self.w()
        self.assertIn("fable limit: W1 → relaunch on claude-opus-5-5", self.events())
        self.assertEqual(w["model_override"], OPUS)
        self.assertEqual(w["fable_switches"], 1)
        self.assertEqual(w["phase"], "running")
        self.assertEqual(w["restarts"], 0)  # not a fresh-head restart
        self.assertNotIn("restart", w)  # not a relaunch try either: max_runs and the attempts stay untouched
        self.assertNotIn("attempts", w)
        self.assertEqual(len(self.kills()), 1)
        started = self.new_sessions()
        self.assertEqual(len(started), 1)
        argv = started[0]
        self.assertEqual(argv[argv.index("--model") + 1], OPUS)
        sid = argv[argv.index("--session-id") + 1]
        self.assertNotEqual(sid, "s1")
        self.assertEqual(w["sessions"], ["s1", sid])
        sent = self.resumes()
        self.assertEqual(len(sent), 1)
        self.assertIn(SWITCH_NOTE, sent[0][2])
        self.assertIn(wab.session_marker(self.cfg, "W1"), sent[0][2])
        self.assertFalse(any(s[2].strip().startswith("/model") for s in self.sent))  # never /model in the TUI
        # the next tick: the new session (no limit line) goes on, nothing more happens
        self.transcript(sid, [asst(inp=10)], marker=wab.session_marker(self.cfg, "W1"))
        self.tick()
        self.assertEqual((len(self.new_sessions()), len(self.resumes()), self.w()["fable_switches"]), (1, 1, 1))

    def test_limit_on_opus_after_the_switch_does_not_switch_again(self):
        self.setup_wave()
        self.tick()
        sid = self.w()["sessions"][-1]
        self.transcript(sid, limit_lines()[:1], marker=wab.session_marker(self.cfg, "W1"))
        for _ in range(2):
            self.tick()
        self.assertEqual((len(self.new_sessions()), len(self.kills()), self.w()["fable_switches"]), (1, 1, 1))
        self.assertEqual(self.events().count("fable limit:"), 1)

    def test_limit_of_another_model_is_left_alone(self):
        self.setup_wave(model=OPUS)
        self.tick()
        self.assertEqual((self.new_sessions(), self.kills()), ([], []))
        self.assertNotIn("model_override", self.w())
        self.assertNotIn("fable limit", self.events())

    def test_limit_of_the_cli_default_model_is_left_alone(self):
        self.setup_wave(model=None)  # a legacy chain: the CLI default, not known to be Fable
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": self.w()}})
        self.cfg, _ = self.chain(model=None)
        self.tick()
        self.assertEqual((self.new_sessions(), self.kills()), ([], []))

    def test_already_on_opus_by_override_is_left_alone(self):
        self.setup_wave(model_override=OPUS, fable_switches=1)
        self.tick()
        self.assertEqual((self.new_sessions(), self.kills()), ([], []))
        self.assertEqual(self.w()["fable_switches"], 1)

    def test_an_ordinary_error_is_left_alone(self):
        other = json.loads(limit_lines()[0])
        other.update(error="model_not_found", apiErrorStatus=404)
        server = json.loads(limit_lines()[0])
        server.update(error="server_error", apiErrorStatus=500)
        self.setup_wave(lines=[json.dumps(other), json.dumps(server)])
        self.tick()
        self.assertEqual((self.new_sessions(), self.kills()), ([], []))
        self.assertNotIn("model_override", self.w())

    def test_the_text_of_the_message_is_not_read(self):
        # the words of a limit message in an ordinary assistant line: no field, no switch
        line = {"type": "assistant", "message": {"model": FABLE, "content": [
            {"type": "text", "text": "You've hit your session limit · resets 11:50am (UTC)"}]}}
        self.setup_wave(lines=[json.dumps(line)])
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_status_429_alone_is_the_limit_too(self):
        line = json.loads(limit_lines()[0])
        line.pop("error")
        self.setup_wave(lines=[json.dumps(line)])
        self.tick()
        self.assertEqual(self.w()["model_override"], OPUS)

    def test_a_subagent_limit_is_not_the_wave_session(self):
        self.setup_wave(lines=limit_lines()[1:2])  # isSidechain: a subagent of its own role model
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_blocked_wave_is_not_switched(self):
        self.setup_wave()
        self.set_status(self.cfg, "W1", "BLOCKED: вопрос владельцу")
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_status_shows_the_override(self):
        self.setup_wave()
        self.tick()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.status_cmd(self.cfg)
        self.assertIn("model=claude-opus-5-5 (лимит Fable)", out.getvalue())

    def test_a_manual_restart_of_the_wave_keeps_opus(self):
        self.setup_wave()
        self.tick()
        w = self.w()
        w["phase"] = "dead"
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}, "defaults": "1.4"})
        self.alive = False
        prompt = self.tmp / "p.md"
        prompt.write_text("task\n", encoding="utf-8")
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.launch(self.cfg, "W1", prompt)
        argv = self.new_sessions()[-1]
        self.assertEqual(argv[argv.index("--model") + 1], OPUS)
        self.assertEqual(self.w()["model_override"], OPUS)
        self.assertEqual(self.w()["fable_switches"], 1)


class FableLimitDashboard(FableLimitBase):
    def setUp(self):
        super().setUp()
        try:
            import dash
        except ImportError as exc:
            self.skipTest(f"rich is not installed: {exc}")
        self.dash = dash

    def render_text(self):
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, width=200, force_terminal=False).print(self.dash.safe_render(self.cfg))
        return buf.getvalue()

    def test_switched_wave_shows_opus_on_the_fable_limit(self):
        self.setup_wave()
        self.tick()
        self.assertIn("claude-opus-5-5 (лимит Fable)", self.render_text())

    def test_switching_phase_is_not_shown_as_a_dead_window(self):
        self.setup_wave()
        with mock.patch.object(wab, "_deliver", return_value=False):
            self.tick()
        self.alive = False
        st = wab.load_state(self.cfg)
        self.assertEqual(self.dash.wave_state(self.cfg, st, "W1")[0], "switching")


class _Crash(BaseException):
    pass


class FableLimitCrashSafety(FableLimitBase):
    def crash_on(self, pred):
        inner = wab.sh.side_effect

        def crashing(*args, **kw):
            if pred(args):
                wab.sh.side_effect = inner  # once
                raise _Crash()
            return inner(*args, **kw)
        wab.sh.side_effect = crashing

    def test_crash_before_kill_session_switches_once(self):
        self.setup_wave()
        self.crash_on(lambda a: a[:2] == ("tmux", "kill-session"))
        with self.assertRaises(_Crash):
            self.tick()
        w = self.w()
        self.assertEqual((w["phase"], w["fable_switches"], w["model_override"]), ("switching", 1, OPUS))
        self.tick()
        self.assertEqual((len(self.new_sessions()), len(self.resumes()), self.w()["fable_switches"]), (1, 1, 1))
        self.assertEqual(self.w()["phase"], "running")
        self.assertEqual(self.events().count("fable limit:"), 1)

    def test_crash_before_new_session_starts_exactly_one_with_the_saved_id(self):
        self.setup_wave()
        self.crash_on(lambda a: a[:2] == ("tmux", "new-session"))
        with self.assertRaises(_Crash):
            self.tick()
        sid = self.w()["fable_switch"]["sid"]
        self.assertEqual(self.new_sessions(), [])
        self.tick()
        started = self.new_sessions()
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][started[0].index("--session-id") + 1], sid)
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.w()["sessions"], ["s1", sid])

    def test_crash_after_new_session_does_not_start_a_second_window(self):
        self.setup_wave()
        inner = wab.sh.side_effect

        def crashing(*args, **kw):
            r = inner(*args, **kw)
            if args[:2] == ("tmux", "new-session"):
                wab.sh.side_effect = inner
                raise _Crash()
            return r
        wab.sh.side_effect = crashing
        with self.assertRaises(_Crash):
            self.tick()
        self.assertTrue(self.alive)
        self.tick()
        self.assertEqual((len(self.new_sessions()), len(self.resumes())), (1, 1))
        self.assertEqual(self.w()["phase"], "running")

    def test_crash_after_typing_presses_only_enter(self):
        self.setup_wave()

        def typed_then_die(name, text, on_typed=None):
            self.sent.append(("text", name, text))
            on_typed()
            raise _Crash()
        wab.send_text.side_effect = typed_then_die
        with self.assertRaises(_Crash):
            self.tick()
        self.assertEqual(self.w()["pending_enter"], wab.FABLE_SWITCH_WHAT)
        wab.send_text.side_effect = lambda n, t, **kw: self.sent.append(("text", n, t))
        with mock.patch.object(wab, "submit") as submit:
            self.tick()
        self.assertEqual(len(self.resumes()), 1)  # not typed again
        self.assertEqual(submit.call_count, 1)  # the Enter only
        self.assertEqual(self.w()["phase"], "running")

    def test_unknown_delivery_with_text_in_the_input_is_not_resent(self):
        self.setup_wave()

        def die_before_record(name, text, on_typed=None):
            self.sent.append(("text", name, text))
            raise _Crash()  # pasted, the dispatcher died before it recorded the paste
        wab.send_text.side_effect = die_before_record
        with self.assertRaises(_Crash):
            self.tick()
        wab.send_text.side_effect = lambda n, t, **kw: self.sent.append(("text", n, t))
        with mock.patch.object(wab, "input_empty_reason", return_value="text in the input line"):
            self.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.w()["phase"], "running")
        self.assertIn("NOT resent", self.events())

    def test_postponed_delivery_with_an_empty_input_is_sent_later(self):
        self.setup_wave()
        with mock.patch.object(wab, "_deliver", return_value=False):
            self.tick()
        self.assertEqual(self.w()["phase"], "switching")
        self.tick()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(self.w()["phase"], "running")

    def test_window_never_ready_stops_like_a_first_launch(self):
        self.setup_wave()
        self.ready = False
        self.tick()
        w = self.w()
        self.assertEqual(w["phase"], "not_ready")
        self.assertEqual(w["model_override"], OPUS)
        self.assertEqual(self.resumes(), [])

    def test_watch_restart_mid_switch_carries_it_on(self):
        self.setup_wave()
        self.crash_on(lambda a: a[:2] == ("tmux", "new-session"))
        with self.assertRaises(_Crash):
            self.tick()
        with mock.patch.object(wab, "drop_stale_btab"), contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, self.path, max_ticks=1)
        self.assertEqual((len(self.new_sessions()), len(self.resumes())), (1, 1))
        self.assertEqual(self.w()["phase"], "running")


# ---------------------------------------------------------------- T5
NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)


def stamp(hours_ago):
    return (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def line(hours_ago, model=FABLE, inp=0, create=0, read=0, out=0, mid=None, rid=None, kind="assistant"):
    msg = {"model": model, "role": "assistant", "usage": {"input_tokens": inp, "cache_creation_input_tokens": create,
                                                          "cache_read_input_tokens": read, "output_tokens": out}}
    if mid is not None:
        msg["id"] = mid
    d = {"type": kind, "timestamp": stamp(hours_ago), "message": msg}
    if rid is not None:
        d["requestId"] = rid
    return json.dumps(d)


class FableUsage(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.root = Path(tempfile.mkdtemp(prefix="fableusage-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def journal(self, rel, lines):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def count(self, **kw):
        return fable_usage.usage(self.root, now=NOW, **kw)

    def test_weights_one_unit_per_million_weighted_tokens(self):
        self.journal("p/a.jsonl", [line(1, inp=1_000_000, mid="m1"), line(1, create=1_000_000, mid="m2"),
                                   line(1, read=1_000_000, mid="m3"), line(1, out=1_000_000, mid="m4")])
        r = self.count()
        self.assertTrue(r["valid"])
        self.assertAlmostEqual(r["day_units"], 1 + 1.25 + 0.1 + 5)
        self.assertAlmostEqual(r["week_units"], 7.35)

    def test_other_models_users_and_synthetic_lines_do_not_count(self):
        self.journal("p/a.jsonl", [line(1, model=OPUS, out=1_000_000, mid="o"),
                                   line(1, model="<synthetic>", out=1_000_000, mid="s"),
                                   line(1, out=1_000_000, mid="u", kind="user"),
                                   "not json", "{broken",
                                   line(1, out=1_000_000, mid="f")])
        self.assertAlmostEqual(self.count()["week_units"], 5)

    def test_windows_day_and_rolling_seven_days(self):
        self.journal("p/a.jsonl", [line(1, inp=1_000_000, mid="a"), line(23.9, inp=1_000_000, mid="b"),
                                   line(25, inp=1_000_000, mid="c"), line(24 * 6.9, inp=1_000_000, mid="d"),
                                   line(24 * 7.1, inp=1_000_000, mid="e"), line(-1, inp=1_000_000, mid="future")])
        r = self.count()
        self.assertAlmostEqual(r["day_units"], 2)
        self.assertAlmostEqual(r["week_units"], 4)

    def test_duplicates_by_message_id_then_request_id(self):
        self.journal("p/a.jsonl", [line(1, out=1_000_000, mid="m1", rid="r1"), line(1, out=1_000_000, mid="m1", rid="r2"),
                                   line(1, out=1_000_000, rid="r9"), line(1, out=1_000_000, rid="r9"),
                                   line(1, out=1_000_000), line(1, out=1_000_000)])
        # the same message.id in a subagent journal of another directory is the same answer too
        self.journal("p/s1/subagents/agent-1.jsonl", [line(1, out=1_000_000, mid="m1")])
        self.assertAlmostEqual(self.count()["week_units"], 5 * 4)  # m1, r9 and the two without a key

    def test_budget_and_threshold(self):
        self.journal("p/a.jsonl", [line(1, out=12_800_000, mid="x")])  # 64 units
        r = self.count()
        self.assertEqual((r["budget"], r["pct"], r["over_threshold"]), (80, 80.0, True))
        r = self.count(budget=100)
        self.assertEqual((r["pct"], r["over_threshold"]), (64.0, False))
        r = self.count(budget=100, threshold=0.6)
        self.assertTrue(r["over_threshold"])

    def test_empty_or_missing_root_is_rc3_and_not_valid(self):
        for root in (self.root, self.root / "absent"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = fable_usage.main(["--root", str(root), "--now", NOW.isoformat()])
            doc = json.loads(out.getvalue())
            self.assertEqual(rc, 3)
            self.assertIs(doc["valid"], False)
            self.assertNotIn("pct", {k for k, v in doc.items() if v == 0})  # never «0%»

    def test_module_is_stdlib_only(self):
        import re as _re
        src = (WAVES / "fable_usage.py").read_text(encoding="utf-8")
        mods = set(_re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", src))
        self.assertFalse(mods - set(sys.stdlib_module_names), mods)

    def test_cli_prints_the_json(self):
        self.journal("p/a.jsonl", [line(1, inp=2_000_000, mid="x")])
        r = subprocess.run([sys.executable, str(WAVES / "fable_usage.py"), "--root", str(self.root),
                            "--now", "2026-10-10T12:00:00Z", "--budget", "10", "--threshold", "0.1"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual({k: doc[k] for k in ("day_units", "week_units", "budget", "pct", "over_threshold", "valid")},
                         {"day_units": 2.0, "week_units": 2.0, "budget": 10, "pct": 20.0, "over_threshold": True,
                          "valid": True})


class LaunchFableUsage(Base):
    def prompt(self):
        p = self.tmp / "p.md"
        p.write_text("task\n", encoding="utf-8")
        return p

    def launch(self, cfg):
        self.alive = False  # no window of W1 yet
        with mock.patch.object(wab, "prepare_clone", return_value=self.cwd), \
                contextlib.redirect_stdout(io.StringIO()):
            return wab.launch(cfg, "W1", self.prompt())

    def events(self, cfg):
        return (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")

    def journal(self, out_tokens):
        d = self.home / ".claude" / "projects" / "-x"
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600))
        rec = {"type": "assistant", "timestamp": ts,
               "message": {"id": "m", "model": FABLE, "usage": {"output_tokens": out_tokens}}}
        (d / "s.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    def test_usage_event_on_launch(self):
        self.journal(2_000_000)  # 10 units of 80
        cfg, _ = self.chain()
        self.assertTrue(self.launch(cfg))
        ev = self.events(cfg)
        self.assertIn("fable-usage:", ev)
        self.assertIn("7 дней 10.0 из 80 ед.", ev)
        self.assertNotIn("Fable ≥80%", ev)

    def test_over_threshold_warns_but_launches(self):
        self.journal(2_000_000)  # 10 units of a budget of 12
        cfg, _ = self.chain(fable_budget_units=12)
        self.assertTrue(self.launch(cfg))
        self.assertIn("Fable ≥80% недельного бюджета: ревью Fable может упереться в лимит", self.events(cfg))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

    def test_not_counted_is_an_event_not_zero_and_not_a_refusal(self):
        cfg, _ = self.chain()  # no journals under HOME
        self.assertTrue(self.launch(cfg))
        ev = self.events(cfg)
        self.assertIn("fable-usage: не посчитан (", ev)
        self.assertNotIn("0.0%", ev)

    def test_an_error_of_the_count_is_an_event(self):
        cfg, _ = self.chain()
        with mock.patch.object(fable_usage, "usage", side_effect=RuntimeError("boom")):
            self.assertTrue(self.launch(cfg))
        self.assertIn("fable-usage: не посчитан (RuntimeError", self.events(cfg))

    def test_budget_field_is_checked_and_not_defaulted_into_the_config(self):
        cfg, path = self.chain()
        self.assertNotIn("fable_budget_units", cfg)
        doc = json.loads(path.read_text(encoding="utf-8"))
        for bad in (0, -1, True, "80", None, float("inf")):
            path.write_text(json.dumps({**doc, "fable_budget_units": bad}), encoding="utf-8")
            with self.assertRaises(SystemExit, msg=repr(bad)):
                wab.load_chain(path)
        cfg, _ = self.chain(fable_budget_units=40.5)
        self.assertEqual(cfg["fable_budget_units"], 40.5)

    def test_budget_is_tunable_not_identity(self):
        self.assertIn("fable_budget_units", wab.TUNABLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
