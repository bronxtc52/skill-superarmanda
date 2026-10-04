"""`ps` stand-in for tests/helpers/superarmanda_waves_e2e_test.py.

The dispatcher reads the process tree of a wave window with ONE call, exactly
`ps -ww -A -o pid=,ppid=,etime=,args=` (wab.PS_ARGV); any other argv is refused with exit 2 and logged. The
answer is the live `ps` shape (right-aligned pid and ppid, etime as `[[dd-]hh:]mm:ss`, the command line to
the end of the line), built from the world of the tmux stand-in ($FAKE_TMUX_STATE):
  * pid 1 and, for every pane that hosts a wave's Claude (`claude: true`), a row `<pane_pid> 1 … claude …`;
  * the rows of $FAKE_TMUX_STATE/ps-extra.json, written by the test: [{pid, under (a session name: the
    parent is that session's Claude) or ppid, etime, args}] - the background work of a wave.
"""
import json
import os
import sys

STATE = os.environ["FAKE_TMUX_STATE"]
EXPECTED = ["-ww", "-A", "-o", "pid=,ppid=,etime=,args="]


def jlog(name, obj):
    with open(os.path.join(STATE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main(argv):
    jlog("ps_calls.jsonl", {"argv": argv})
    if argv != EXPECTED:
        sys.stderr.write("ps: only the full table of the dispatcher is expected in this test\n")
        sys.exit(2)
    rows = [(1, 0, "52-13:25:41", "/usr/lib/systemd/systemd --system --deserialize=173")]
    claude = {}
    sessions = os.path.join(STATE, "sessions")
    for name in sorted(os.listdir(sessions)) if os.path.isdir(sessions) else []:
        if not name.endswith(".json"):
            continue
        with open(os.path.join(sessions, name), encoding="utf-8") as f:
            s = json.load(f)
        for p in s["panes"]:
            if p["claude"]:
                claude[s["name"]] = p["pid"]
                rows.append((p["pid"], 1, "10:00", "claude " + " ".join(s["command"][1:])))
            else:
                rows.append((p["pid"], 1, "10:00", "-sh"))
    try:
        with open(os.path.join(STATE, "ps-extra.json"), encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        extra = []
    for r in extra:
        ppid = claude.get(r["under"]) if "under" in r else r["ppid"]
        if ppid is not None:
            rows.append((r["pid"], ppid, r["etime"], r["args"]))
    for pid, ppid, etime, args in rows:
        print(f"{pid:>7} {ppid:>7} {etime:>11} {args}")


if __name__ == "__main__":
    main(sys.argv[1:])
