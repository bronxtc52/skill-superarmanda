#!/usr/bin/env python3
"""1.4.0 (#108, T4 and T5): the Fable limit of a wave window and the local Fable usage count.

T4: a wave session that runs on Fable and hits the subscription limit (a main-thread assistant line of its
transcript with `error: "rate_limit"` or `apiErrorStatus: 429`) is closed and started again on Opus with the
resume message; the chain does not stop, the restarts/runs of the wave are not charged.
T5: scripts/waves/fable_usage.py counts the weighted Fable tokens of the local Claude Code journals; `wab.py
launch` writes the count as an event and never refuses on it.

Every tmux/claude interaction goes through the patched module functions of the shared Base (the TMUX guard
of superarmanda_waves_test is installed by importing it). The journals of the usage count are the live sample
`tests/fixtures/transcripts/fable-usage-journal.jsonl` (see its README); a synthetic variant is derived from a
named live line only where the sample lacks the form.
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

    WAVE_TASK = "Задача волны W1: сделай фичу X."

    def setup_wave(self, model=FABLE, lines=None, handoff=True, **rec):
        self.cfg, self.path = self.chain(model=model)
        self.transcript("s1", [asst(inp=1000)] + list(lines if lines is not None else limit_lines()[:1]))
        prompt = self.tmp / "w1-prompt.md"
        prompt.write_text(self.WAVE_TASK + "\n", encoding="utf-8")
        rec.setdefault("prompt_file", str(prompt))
        w = self.wave_rec(sessions=["s1"], **rec)
        self.put_state(self.cfg, {"current": "W1", "waves": {"W1": w}, "defaults": "1.4"})
        self.set_status(self.cfg, "W1", "RUNNING")
        if handoff:  # the usual case: the wave already passed a WAB-CHECKPOINT
            (wab.wave_dir(self.cfg, "W1") / "handoff.md").write_text("# handoff\n", encoding="utf-8")

    def write_manifest(self):
        path = wab.wave_dir(self.cfg, "W1") / "superarmanda" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 2}\n', encoding="utf-8")
        return path

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

    def test_the_session_model_is_saved_before_the_window_is_created(self):
        # the dispatcher dies right after `tmux new-session` (Codex P2 on #109): the model of the live window
        # is already on disk, so a later edit of the tunable chain.json `model` cannot hide the Fable session
        self.setup_wave()
        st = wab.load_state(self.cfg)
        st["waves"]["W1"].pop("session_model", None)
        wab.save_state(self.cfg, st)
        real = wab.mark_owner

        def die(*a, **k):
            raise KeyboardInterrupt("dispatcher killed after new-session")
        wab.mark_owner = die
        try:
            with self.assertRaises(KeyboardInterrupt):
                wab.start_session(self.cfg, wab.load_state(self.cfg), "W1")
        finally:
            wab.mark_owner = real
        self.assertEqual(self.w().get("session_model"), FABLE)

    def test_owner_edit_of_the_chain_model_does_not_hide_the_fable_session(self):
        # the window was started on Fable; the owner sets `model` of chain.json to Opus and restarts watch:
        # the session still runs on Fable, its limit is still the Fable limit (session_model, #108)
        self.setup_wave()
        st = wab.load_state(self.cfg)
        wab.start_session(self.cfg, st, "W1")
        self.assertEqual(self.w()["session_model"], FABLE)
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["phase"] = "running"
        wab.save_state(self.cfg, st)
        self.cfg, _ = self.chain(model=OPUS)
        self.tick()
        w = self.w()
        self.assertEqual((w.get("model_override"), w.get("fable_switches")), (OPUS, 1))
        self.assertEqual(w["session_model"], OPUS)  # the new window is on Opus, and it is recorded
        self.assertEqual(len(self.kills()), 1)

    def test_the_short_fable_selector_is_the_fable_session(self):
        # `"model": "fable"` is an accepted selector of the CLI (Codex P2 on #109): its limit is the Fable limit
        self.setup_wave(model="fable")
        self.tick()
        w = self.w()
        self.assertEqual((w.get("model_override"), w.get("fable_switches")), (OPUS, 1))
        self.assertEqual(len(self.kills()), 1)

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

    def test_unconfirmed_switch_is_shown_as_waiting_for_the_delivery(self):
        self.setup_wave(phase="switching", fable_switch={"sid": "s2", "step": "unconfirmed", "text": "x"})
        self.alive = True
        st = wab.load_state(self.cfg)
        self.assertEqual(self.dash.wave_state(self.cfg, st, "W1")[0], "switch_unconfirmed")
        self.assertIn("ждёт подтверждения", self.render_text())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wab.status_cmd(self.cfg)
        self.assertIn("step=unconfirmed", out.getvalue())

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

    def unknown_delivery(self, why="text in the input line"):
        """The resume text was pasted, the dispatcher died before it recorded the paste; the restarted tick
        sees no marker in the new session's journal and `why` from input_empty_reason."""
        self.setup_wave()

        def die_before_record(name, text, on_typed=None):
            self.sent.append(("text", name, text))
            raise _Crash()
        wab.send_text.side_effect = die_before_record
        with self.assertRaises(_Crash):
            self.tick()
        wab.send_text.side_effect = lambda n, t, **kw: self.sent.append(("text", n, t))
        self.tg.clear()
        with mock.patch.object(wab, "input_empty_reason", return_value=why):
            self.tick()

    def unconfirmed_notices(self):
        return [t for t in self.tg if "дошло ли продолжение" in t]

    def test_unknown_delivery_with_text_in_the_input_is_not_resent_and_not_done(self):
        # Codex P2 / CodeRabbit on #109: «NOT resent» is not the end of the switch, the owner is told at once
        self.unknown_delivery()
        w = self.w()
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual((w["phase"], w["fable_switch"]["step"]), ("switching", "unconfirmed"))
        self.assertIn("NOT resent", self.events())
        self.assertEqual(len(self.unconfirmed_notices()), 1)
        self.assertNotIn("updating", w.get("outbox", {}))  # delivered, not dropped as a stale episode

    def test_no_input_box_is_the_same_unconfirmed_delivery(self):
        self.unknown_delivery(why=wab.NO_INPUT_BOX)
        w = self.w()
        self.assertEqual((w["phase"], w["fable_switch"]["step"]), ("switching", "unconfirmed"))
        self.assertEqual(len(self.resumes()), 1)
        self.assertEqual(len(self.unconfirmed_notices()), 1)

    def test_unconfirmed_delivery_waits_without_resending_or_renotifying(self):
        self.unknown_delivery()
        for why in ("text in the input line", None, wab.NO_INPUT_BOX):
            with mock.patch.object(wab, "input_empty_reason", return_value=why):
                self.tick()
        w = self.w()
        self.assertEqual((w["phase"], w["fable_switch"]["step"]), ("switching", "unconfirmed"))
        self.assertEqual(len(self.resumes()), 1)  # never sent blindly, not even into an empty input
        self.assertEqual(len(self.unconfirmed_notices()), 1)

    def test_marker_in_the_new_journal_completes_the_unconfirmed_switch(self):
        self.unknown_delivery()
        sid = self.w()["fable_switch"]["sid"]
        self.transcript(sid, [asst(inp=10)], marker=wab.session_marker(self.cfg, "W1"))
        self.tick()
        w = self.w()
        self.assertEqual(w["phase"], "running")
        self.assertNotIn("fable_switch", w)
        self.assertIn("resume delivered", self.events())
        self.assertEqual(len(self.resumes()), 1)

    def test_unconfirmed_switch_with_the_window_gone_is_a_dead_window(self):
        self.unknown_delivery()
        self.alive = False
        self.tick()
        self.assertEqual(self.w()["phase"], "dead")
        self.assertEqual(len(self.resumes()), 1)

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


class FableSwitchMessage(FableLimitBase):
    """Codex P1 on #109: the Fable limit may come before the first WAB-CHECKPOINT, when handoff.md does not
    exist. The switch message must not send the new Opus session to a missing handoff.md."""

    def switch_text(self):
        texts = [s[2] for s in self.sent if s[0] == "text" and SWITCH_NOTE in s[2]]
        self.assertEqual(len(texts), 1, self.sent)
        return texts[0]

    def test_handoff_without_manifest_sends_the_ordinary_resume(self):
        self.setup_wave()
        self.tick()
        text = self.switch_text()
        self.assertTrue(text.startswith("/superarmanda --wave W1 --resume"))
        self.assertIn("handoff.md", text)
        self.assertNotIn("--manifest", text)

    def test_manifest_without_handoff_names_the_manifest_explicitly(self):
        self.setup_wave(handoff=False)
        path = self.write_manifest()
        self.tick()
        text = self.switch_text()
        self.assertTrue(text.startswith("/superarmanda --wave W1 --resume"))
        self.assertIn(f"--manifest {path}", text)
        self.assertIn(wab.session_marker(self.cfg, "W1"), text)
        self.assertNotIn("по manifest из", text)  # not pointed at the missing handoff.md
        self.assertIn("state.py init не вызывай", text)

    def test_manifest_and_handoff_still_name_the_manifest(self):
        self.setup_wave()  # a checkpoint before init, then init: handoff.md may not name the manifest
        path = self.write_manifest()
        self.tick()
        self.assertIn(f"--manifest {path}", self.switch_text())

    def test_nothing_written_yet_resends_the_wave_task(self):
        self.setup_wave(handoff=False)
        self.tick()
        text = self.switch_text()
        self.assertFalse(text.startswith("/superarmanda"))
        self.assertIn(self.WAVE_TASK, text)
        self.assertIn(wab.session_marker(self.cfg, "W1"), text)
        self.assertEqual(self.w()["phase"], "running")

    def test_the_choice_is_fixed_before_sending(self):
        self.setup_wave(handoff=False)
        with mock.patch.object(wab, "_deliver", return_value=False):  # postponed after the window is ready
            self.tick()
        self.assertEqual(self.w()["phase"], "switching")
        self.write_manifest()  # appears afterwards: the saved choice stays
        self.tick()
        text = self.switch_text()
        self.assertIn(self.WAVE_TASK, text)
        self.assertNotIn("--manifest", text)



def user_line(text, timestamp, side=False, content=None):
    """The live typed-user line of `first-session-head.jsonl` (Claude Code 2.1.289) with its text and time set."""
    for raw in FIXTURES.joinpath("first-session-head.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(raw) if raw.strip() else None
        if isinstance(d, dict) and d.get("type") == "user":
            break
    else:
        raise AssertionError("first-session-head.jsonl has no user line")
    d["message"]["content"] = content if content is not None else text
    d["timestamp"] = timestamp
    d["isSidechain"] = side
    return json.dumps(d, ensure_ascii=False)


def at(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class FableLimitWhileBlocked(FableLimitBase):
    """#110 (Codex P2 on #109): the wave on Fable got an answer to its BLOCKED (the owner in the window, the
    decision policy, a merge gate failure), went on working and hit the limit while the status file still says
    BLOCKED. The limit is fresh when its line is newer than the BLOCKED line of the status file; the answer
    typed after the BLOCKED goes into the message of the Opus session (the new session does not see the old
    journal). A wave that only waits for the owner calls nobody: no fresh limit, no switch."""

    BLOCKED = "BLOCKED: выбрать вариант A или B"
    LIMIT_AT = "2026-10-10T10:00:00.000Z"  # the time limit_lines() puts in the live rate-limit line

    def blocked_wave(self, lines, blocked_at="2026-10-10T09:00:00.000Z"):
        self.setup_wave(lines=lines)
        self.set_status(self.cfg, "W1", self.BLOCKED)
        status = wab.wave_dir(self.cfg, "W1") / "status"
        os.utime(status, (at(blocked_at), at(blocked_at)))

    def switch_text(self):
        texts = [s[2] for s in self.sent if s[0] == "text" and SWITCH_NOTE in s[2]]
        self.assertEqual(len(texts), 1, self.sent)
        return texts[0]

    def test_limit_after_an_answer_switches_and_carries_the_answer(self):
        self.blocked_wave([user_line("Ответ владельца: вариант B", "2026-10-10T09:30:00.000Z"),
                           limit_lines()[0]])
        self.assertTrue(self.tick())
        w = self.w()
        self.assertEqual(w["model_override"], OPUS)
        self.assertEqual(w["fable_switches"], 1)
        self.assertEqual(len(self.new_sessions()), 1)
        text = self.switch_text()
        self.assertIn("Ответ владельца: вариант B", text)
        self.assertIn(self.BLOCKED, text)
        self.assertIn(wab.session_marker(self.cfg, "W1"), text)
        self.assertIn("fable limit: W1 → relaunch on claude-opus-5-5", self.events())

    def test_waiting_for_the_owner_is_not_switched(self):
        self.blocked_wave([user_line("ещё до вопроса", "2026-10-10T08:30:00.000Z")])
        self.tick()
        self.assertEqual(self.new_sessions(), [])
        self.assertNotIn("model_override", self.w())

    def test_an_answer_without_a_limit_is_not_switched(self):
        self.blocked_wave([user_line("Ответ владельца: вариант B", "2026-10-10T09:30:00.000Z")])
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_a_limit_older_than_the_blocked_line_is_not_switched(self):
        self.blocked_wave(limit_lines()[:1], blocked_at="2026-10-10T11:00:00.000Z")
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_a_limit_line_without_a_time_is_not_switched(self):
        line = json.loads(limit_lines()[0])
        line.pop("timestamp")
        self.blocked_wave([json.dumps(line)])
        self.tick()
        self.assertEqual(self.new_sessions(), [])

    def test_only_the_answers_after_the_blocked_line_are_carried(self):
        self.blocked_wave([
            user_line("до вопроса", "2026-10-10T08:30:00.000Z"),
            user_line("субагент", "2026-10-10T09:20:00.000Z", side=True),
            user_line("", "2026-10-10T09:25:00.000Z",
                      content=[{"type": "tool_result", "tool_use_id": "t", "content": "вывод инструмента"}]),
            user_line("Ответ: B", "2026-10-10T09:30:00.000Z"),
            user_line("и ещё: без миграции", "2026-10-10T09:31:00.000Z"),
            limit_lines()[0]])
        self.tick()
        text = self.switch_text()
        self.assertIn("Ответ: B", text)
        self.assertIn("и ещё: без миграции", text)
        self.assertLess(text.index("Ответ: B"), text.index("и ещё: без миграции"))
        for absent in ("до вопроса", "субагент", "вывод инструмента"):
            self.assertNotIn(absent, text)

    def test_no_answer_found_asks_the_question_again(self):
        # the limit came after the BLOCKED line without a typed message in this journal (an answer the journal
        # did not keep): the new session is told to ask again under a new line, so the owner is notified again
        self.blocked_wave(limit_lines()[:1])
        self.tick()
        self.assertEqual(len(self.new_sessions()), 1)
        text = self.switch_text()
        self.assertIn(self.BLOCKED, text)
        self.assertIn(wab.FABLE_BLOCKED_ASK_AGAIN, text)

    def test_a_long_answer_is_cut(self):
        self.blocked_wave([user_line("Я" * 20000, "2026-10-10T09:30:00.000Z"), limit_lines()[0]])
        self.tick()
        self.assertLess(len(self.switch_text()), 20000)

    def switched(self, lines):
        self.blocked_wave(lines)
        self.tick()
        sid = self.w()["sessions"][-1]
        self.assertNotEqual(sid, "s1")
        self.transcript(sid, [asst(inp=10)], marker=wab.session_marker(self.cfg, "W1"))
        return sid

    def test_the_old_blocked_line_is_not_taken_up_again_after_the_switch(self):
        # reviewer on #110: the status file still holds the old line while the Opus session reads its message
        self.switched(limit_lines()[:1])
        for _ in range(2):
            self.tick()
        w = self.w()
        self.assertNotIn("blocked", w["notified"])  # no notice of the line handed to the Opus session
        self.assertEqual(len(self.new_sessions()), 1)
        self.assertEqual(w["fable_blocked_episode"]["status"], self.BLOCKED)

    def test_the_line_asked_again_is_a_new_episode(self):
        self.switched(limit_lines()[:1])
        self.set_status(self.cfg, "W1", self.BLOCKED + " (повтор после смены модели)")
        self.tick()
        w = self.w()
        self.assertNotIn("fable_blocked_episode", w)
        self.assertIn("blocked", w["notified"])  # the owner is asked again

    def test_the_same_line_written_anew_is_a_new_episode(self):
        self.switched(limit_lines()[:1])
        self.set_status(self.cfg, "W1", self.BLOCKED)
        status = wab.wave_dir(self.cfg, "W1") / "status"
        later = time.time() + 5
        os.utime(status, (later, later))
        self.tick()
        self.assertNotIn("fable_blocked_episode", self.w())
        self.assertIn("blocked", self.w()["notified"])

    def test_the_same_line_anew_notifies_even_if_the_owner_was_notified_before(self):
        # CodeRabbit on #111: the owner got the notice of the line before the limit; the dedup by text must not
        # swallow the notice of the same line written anew by the Opus session
        self.blocked_wave([user_line("Ответ: B", "2026-10-10T09:30:00.000Z"), limit_lines()[0]])
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["notified"]["blocked"] = self.BLOCKED  # an earlier tick saw the line and told the owner
        st["waves"]["W1"]["last_status"] = self.BLOCKED
        wab.save_state(self.cfg, st)
        self.tick()
        self.transcript(self.w()["sessions"][-1], [asst(inp=10)], marker=wab.session_marker(self.cfg, "W1"))
        self.set_status(self.cfg, "W1", self.BLOCKED)
        later = time.time() + 5
        os.utime(wab.wave_dir(self.cfg, "W1") / "status", (later, later))
        with mock.patch.object(wab, "put_notice", wraps=wab.put_notice) as put:
            self.tick()
        self.assertIn("blocked", [c.args[3] for c in put.call_args_list])

    def test_a_gate_failure_is_not_typed_into_the_refused_window(self):
        # CodeRabbit on #111: the fresh limit is checked before the retry of a gate failure
        self.BLOCKED = "BLOCKED: merge gate: CI красный"
        self.blocked_wave([limit_lines()[0]])
        st = wab.load_state(self.cfg)
        st["waves"]["W1"]["gate_fail_msg"] = {"text": "сбой гейта: почини CI", "sent": False}
        wab.save_state(self.cfg, st)
        self.tick()
        self.assertEqual(len(self.new_sessions()), 1)
        self.assertFalse(any("сбой гейта" in str(x[2]) for x in self.sent if len(x) > 2), self.sent)

    def test_an_empty_status_mid_rewrite_keeps_the_episode(self):
        self.switched(limit_lines()[:1])
        (wab.wave_dir(self.cfg, "W1") / "status").write_text("", encoding="utf-8")
        self.tick()
        self.assertIn("fable_blocked_episode", self.w())
        self.assertNotIn("blocked", self.w()["notified"])

    def test_a_policy_answer_in_flight_is_charged_and_not_resent(self):
        self.blocked_wave([user_line("ответ политики", "2026-10-10T09:30:00.000Z"), limit_lines()[0]])
        st = wab.load_state(self.cfg)
        st["waves"]["W1"].update(pending_enter="policy answer", policy_pending=self.BLOCKED, auto_answers=1)
        wab.save_state(self.cfg, st)
        self.tick()
        w = self.w()
        self.assertEqual(w["auto_answers"], 2)
        self.assertNotIn("policy_pending", w)
        self.assertNotIn("pending_enter", w)

    def test_a_gate_failure_in_flight_is_not_resent(self):
        self.BLOCKED = "BLOCKED: merge gate: CI красный"
        self.blocked_wave([limit_lines()[0]])
        st = wab.load_state(self.cfg)
        st["waves"]["W1"].update(pending_enter="gate failure", gate_fail_msg={"text": "сбой гейта", "sent": False})
        wab.save_state(self.cfg, st)
        with mock.patch.object(wab, "_deliver", return_value=False):  # Enter still not confirmed on this tick
            self.tick()
        self.assertTrue(self.w()["gate_fail_msg"]["sent"])
        self.assertEqual(self.w()["phase"], "switching")

    def test_a_watch_restart_mid_switch_still_carries_the_answer(self):
        self.blocked_wave([user_line("Ответ: B", "2026-10-10T09:30:00.000Z"), limit_lines()[0]])
        with mock.patch.object(wab, "_fable_switch_tick", side_effect=_Crash):
            with self.assertRaises(_Crash):
                self.tick()
        self.assertEqual(self.w()["phase"], "switching")
        self.tick()  # a fresh watch carries the saved switch on
        self.assertIn("Ответ: B", self.switch_text())

    def test_service_lines_are_not_an_answer(self):
        self.blocked_wave([user_line("[Request interrupted by user]", "2026-10-10T09:30:00.000Z"),
                           user_line("<command-name>/compact</command-name>", "2026-10-10T09:31:00.000Z"),
                           limit_lines()[0]])
        self.tick()
        text = self.switch_text()
        self.assertIn(wab.FABLE_BLOCKED_ASK_AGAIN, text)
        self.assertNotIn("Request interrupted", text)

    def test_the_tail_read_drops_the_cut_first_line(self):
        path = self.transcript("tail", [user_line("Я" * 3000, "2026-10-10T09:30:00.000Z"),
                                        user_line("последний", "2026-10-10T09:31:00.000Z")])
        size = path.stat().st_size
        last = len(path.read_bytes().split(b"\n")[-2]) + 1
        self.assertEqual(wab._user_texts_since(path, at("2026-10-10T09:00:00.000Z"), tail=last + 100), ["последний"])
        self.assertEqual(len(wab._user_texts_since(path, at("2026-10-10T09:00:00.000Z"), tail=size)), 2)

    def test_a_running_wave_switches_as_before(self):
        self.setup_wave()  # RUNNING, limit line older than the status file: the 1.4.0 path, not this one
        self.tick()
        self.assertEqual(len(self.new_sessions()), 1)
        self.assertNotIn("BLOCKED", self.switch_text())


# ---------------------------------------------------------------- T5
NOW = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
W = fable_usage.WEIGHTS


def live_journal():
    """The live sample of the journals (fixture `fable-usage-journal.jsonl`, see the README): 26 lines of one
    main-thread session (an Opus answer, then five Fable answers, 2026-09-12) and of one Fable subagent journal
    (`isSidechain: true`, three answers, 2026-10-03), parsed."""
    raw = FIXTURES.joinpath("fable-usage-journal.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in raw if x.strip()]


LIVE = live_journal()
MAIN_DAY = "2026-09-12T05:00:00Z"  # an hour after the main-thread lines (04:12-04:14)
SUB_DAY = "2026-10-03T12:00:00Z"  # an hour and a half after the subagent lines (10:36-10:37)


def final_units(lines):
    """Independent expectation: each Fable answer (message.id) by its LAST block (the largest apiBlockIndex), the
    line Claude Code writes with the final usage, in units."""
    last = {}
    for d in lines:
        m = d.get("message") or {}
        if d.get("type") == "assistant" and m.get("model") == FABLE:
            if m["id"] not in last or d["apiBlockIndex"] > last[m["id"]]["apiBlockIndex"]:
                last[m["id"]] = d
    return sum(sum(d["message"]["usage"][k] * w for k, w in W.items()) for d in last.values()) / 1_000_000


def dump(lines):
    return [json.dumps(d, ensure_ascii=False) for d in lines]


MAIN = [d for d in LIVE if d.get("isSidechain") is False]
SUB = [d for d in LIVE if d.get("isSidechain") is True]


def scaled(units, hours_ago=1):
    """For the budget arithmetic only: the live Fable line msg_fx04 moved to `hours_ago` before NOW with its usage
    replaced by output tokens worth `units` (every other key of the live line is kept)."""
    d = json.loads(json.dumps(next(x for x in MAIN if x["type"] == "assistant" and x["message"]["id"] == "msg_fx04")))
    d["timestamp"] = (NOW - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    d["message"]["usage"].update(input_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                 output_tokens=int(units * 1_000_000 / 5))
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

    def live(self):
        """The sample laid out as Claude Code does: the session journal and its subagent journal beside it."""
        self.journal("p/session-fx-1.jsonl", dump(MAIN))
        self.journal("p/session-fx-1/subagents/agent-fx-1.jsonl", dump(SUB))

    def count(self, now=NOW, **kw):
        return fable_usage.usage(self.root, now=now, **kw)

    def test_the_live_sample_has_the_shapes_the_tests_rely_on(self):
        ids = [d["message"]["id"] for d in LIVE if d["type"] == "assistant"]
        self.assertGreater(len(ids), len(set(ids)))  # repeats of one message.id: one line per content block
        self.assertTrue(SUB and MAIN)
        self.assertIn("claude-opus-5", {d["message"].get("model") for d in LIVE if d["type"] == "assistant"})
        grows = [d["message"]["usage"]["output_tokens"] for d in SUB
                 if d["type"] == "assistant" and d["message"]["id"] == "msg_fx07"]
        self.assertEqual(grows, [7, 451])  # the subagent writes the partial output first, the final number last

    def test_weights_on_one_live_line(self):
        d = next(x for x in MAIN if x["type"] == "assistant" and x["message"]["id"] == "msg_fx04")
        u = d["message"]["usage"]
        self.assertAlmostEqual(fable_usage.weighted(u), u["input_tokens"] + 1.25 * u["cache_creation_input_tokens"]
                               + 0.1 * u["cache_read_input_tokens"] + 5 * u["output_tokens"])
        self.assertAlmostEqual(fable_usage.weighted(u), 13934.1)
        self.journal("p/a.jsonl", dump([d]))
        self.assertAlmostEqual(self.count(now=MAIN_DAY)["day_units"], 0.0139, places=4)

    def test_repeats_of_one_answer_count_once_with_the_final_usage(self):
        self.live()
        r = self.count(now=SUB_DAY)
        self.assertTrue(r["valid"])
        # the subagent journal: msg_fx07 is 7 then 451 output tokens, msg_fx08 51, 51, 51 then 465
        self.assertAlmostEqual(final_units(SUB), 0.0610, places=4)
        self.assertAlmostEqual(r["day_units"], round(final_units(SUB), 4), places=4)
        self.assertAlmostEqual(r["week_units"], round(final_units(SUB), 4), places=4)

    def test_sidechain_counts_a_fable_subagent_spends_the_limit_too(self):
        self.live()
        week = self.count(now="2026-10-04T12:00:00Z")["week_units"]
        self.assertGreater(week, 0)
        self.assertAlmostEqual(week, round(final_units(SUB), 4), places=4)

    def test_other_models_do_not_count(self):
        # the main-thread sample opens with a live Opus answer (two lines): only the five Fable answers count
        self.journal("p/a.jsonl", dump(MAIN))
        r = self.count(now=MAIN_DAY)
        self.assertAlmostEqual(final_units(MAIN), 0.0610, places=4)
        self.assertAlmostEqual(r["week_units"], round(final_units(MAIN), 4), places=4)
        only_opus = [d for d in MAIN if d["type"] != "assistant" or d["message"]["model"] != FABLE]
        self.journal("p/a.jsonl", dump(only_opus))
        self.assertEqual(self.count(now=MAIN_DAY)["week_units"], 0)

    def test_synthetic_cli_lines_users_and_junk_do_not_count(self):
        # `<synthetic>` lines are the live rate-limit fixture; the users are the live tool_result lines
        users = [d for d in MAIN if d["type"] == "user"]
        self.journal("p/a.jsonl", limit_lines() + dump(users) + ["not json", "{broken"])
        self.assertEqual(self.count(now="2026-10-10T11:00:00Z")["week_units"], 0)

    def test_windows_day_and_rolling_seven_days(self):
        self.live()
        main, sub = round(final_units(MAIN), 4), round(final_units(SUB), 4)
        cases = {  # --now -> (day, week)
            MAIN_DAY: (main, main),  # the subagent lines are three weeks in the future: not counted
            "2026-09-13T04:00:00Z": (main, main),  # 23 h 48 min after the first main-thread answer: inside the day
            "2026-09-13T04:15:00Z": (0, main),  # 24 h after the last one: out of the day, inside the 7 days
            "2026-09-19T04:00:00Z": (0, main),  # 6 days 23 h 48 min after the first: still inside the 7 days
            "2026-09-19T04:15:00Z": (0, 0),  # 7 days after the last: past the window
            SUB_DAY: (sub, sub),
            "2026-10-09T12:00:00Z": (0, sub),
        }
        for now, (day, week) in cases.items():
            r = self.count(now=now)
            self.assertAlmostEqual(r["day_units"], day, places=4, msg=now)
            self.assertAlmostEqual(r["week_units"], week, places=4, msg=now)

    def test_the_same_answer_in_two_journals_counts_once(self):
        # live: 82 Fable answers of this machine sit in two session journals each (a resumed session copies them)
        self.journal("p/session-fx-1.jsonl", dump(MAIN))
        self.journal("p/session-fx-2.jsonl", dump(MAIN))
        self.assertAlmostEqual(self.count(now=MAIN_DAY)["week_units"], round(final_units(MAIN), 4), places=4)

    def test_the_live_sample_has_no_unkeyed_line(self):
        # every Fable line of the live sample carries message.id; the counter reports lines without it (unkeyed)
        # instead of guessing a key for a form never seen live (Codex P1 on #109)
        self.journal("p/session-fx-1.jsonl", dump(MAIN))
        self.assertEqual(self.count(now=MAIN_DAY)["unkeyed"], 0)

    def test_budget_and_threshold(self):
        self.journal("p/a.jsonl", [scaled(64)])  # 64 units
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
        self.journal("p/a.jsonl", [scaled(2)])
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
        # the live Fable line msg_fx04 an hour ago, its usage replaced by `out_tokens` of output only
        rec = json.loads(scaled(out_tokens * 5 / 1_000_000))
        rec["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600))
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

    def test_the_count_runs_after_the_wave_window_is_started(self):
        # P2 (Codex on #109): on a machine with big journals the count takes a while; it must not hold the start
        self.journal(2_000_000)
        cfg, _ = self.chain()
        order, start = [], wab.start_session
        usage = fable_usage.usage

        def started(*a, **kw):
            order.append("start_session")
            return start(*a, **kw)

        def counted(*a, **kw):
            order.append("count")
            return usage(*a, **kw)

        with mock.patch.object(wab, "start_session", side_effect=started), \
                mock.patch.object(fable_usage, "usage", side_effect=counted):
            self.assertTrue(self.launch(cfg))
        self.assertEqual(order, ["start_session", "count"])
        self.assertIn("7 дней 10.0 из 80 ед.", self.events(cfg))

    def test_a_failure_of_the_whole_usage_event_does_not_stop_the_launch(self):
        cfg, _ = self.chain()
        with mock.patch.object(wab, "fable_usage_event", side_effect=RuntimeError("boom")):
            self.assertTrue(self.launch(cfg))
        self.assertEqual(self.get_state(cfg)["waves"]["W1"]["phase"], "running")

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
