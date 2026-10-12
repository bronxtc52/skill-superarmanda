"""Capture state.json of the 1.5.1 dispatcher (commit cf1db1c) on the three in-flight paths of w2-legacy-1.4, Telegram
down, together with the wave's text store (`W1/.texts`: the texts behind the eids of 1.4.3+). The scenario is the 1.4
capture's, unchanged; only scrub() also copies the store. Run from the repo root at commit cf1db1c only:
TMUX_TMPDIR=$(mktemp -d) python3 -I tests/fixtures/waves/w2-state-1.5.1/capture.py <out_dir>"""
import json, shutil, sys, os
from pathlib import Path
from unittest import mock
REPO = Path.cwd()
sys.path.insert(0, str(REPO / "tests" / "helpers"))
import superarmanda_waves_test as T
wab = T.wab
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)


def scrub(self, cfg, name):
    raw = wab.state_path(cfg).read_text(encoding="utf-8")
    raw = raw.replace(str(cfg["run_dir"]), "@RUN_DIR@").replace(self.cwd, "@REPO@").replace(str(self.tmp), "@TMP@")
    (OUT / f"{name}.state.json").write_text(raw, encoding="utf-8")
    shutil.rmtree(OUT / f"{name}.texts", ignore_errors=True)
    shutil.copytree(cfg["run_dir"] / "W1" / ".texts", OUT / f"{name}.texts")  # names: the eids; contents: harness lines
    st = (cfg["run_dir"] / "W1" / "status").read_text(encoding="utf-8")
    print(name, "status:", st.strip()[:80])


class Gen(T.FixedAnswerPolicy):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(wab, "_send_telegram", side_effect=OSError("telegram down"))
        p.start()
        self.addCleanup(p.stop)

    def blocked_then_relaunched(self, cfg):
        # an earlier try of W1 asked the owner (unlabelled BLOCKED), then the wave was restarted by hand:
        # the old record goes to `attempts` (the same by-hand restart as test_acc07_acc09_...)
        self.tick(cfg, "BLOCKED: вопрос владельцу ранней попытки")
        st = wab.load_state(cfg)
        old = st["waves"]["W1"]
        st["waves"]["W1"] = dict(self.wave_rec("W1"), attempts=[{k: v for k, v in old.items() if k not in ("attempts", "outbox")}])
        self.put_state(cfg, st)
        self.tick(cfg, "RUNNING")

    def test_answered(self):
        cfg, _ = self.mandate(policy=self.BOTH, max_runs=5)
        self.fork(cfg)
        self.blocked_then_relaunched(cfg)
        self.tick(cfg, self.ND.format(rec="invariant"))
        assert len(self.answers()) == 1
        scrub(self, cfg, "answered")

    def test_refused(self):
        cfg, _ = self.mandate(policy=self.BOTH, max_runs=5)
        self.fork(cfg)
        self.blocked_then_relaunched(cfg)
        self.tick(cfg, self.ND.format(rec="invariant"))  # answered: the key is in policy_keys
        self.tick(cfg, "RUNNING")
        self.tick(cfg, self.ND.format(rec="cut_surface"))  # the same fork again: refused, to the owner
        assert len(self.answers()) == 1
        scrub(self, cfg, "refused")

    def test_typed(self):
        cfg, _ = self.mandate(policy=self.BOTH, max_runs=5)
        self.fork(cfg)
        self.blocked_then_relaunched(cfg)

        def typed_then_watch_dies(name, text, on_typed=None, **kw):
            self.sent.append(("text", name, text))
            on_typed()
            raise KeyboardInterrupt
        with mock.patch.object(wab, "send_text", typed_then_watch_dies):
            try:
                self.tick(cfg, self.ND.format(rec="invariant"))
            except KeyboardInterrupt:
                pass
        scrub(self, cfg, "typed")


if __name__ == "__main__":
    import unittest
    unittest.main(argv=[sys.argv[0], "-v", "Gen.test_answered", "Gen.test_refused", "Gen.test_typed"])
