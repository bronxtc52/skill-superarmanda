"""Stateful `tmux` stand-in for tests/helpers/superarmanda_waves_e2e_test.py.

It speaks exactly the argv wab.py sends (new-session, has-session, kill-session, send-keys,
load-buffer, paste-buffer, capture-pane, list-clients, display-message, list-keys, ...) and keeps
its world in a directory ($FAKE_TMUX_STATE): one JSON file per session, a call log, an inbox
of every message submitted with Enter, a log of the new-session commands.

Nothing is run: the command given to new-session (`claude ...`) is only recorded, the test plays
the session. A call without `-S`/`-L` is refused with exit 97 (the same rule the real-tmux guard of
the sibling test enforces) and still logged.

Screens are NOT invented: they are cut from the live Claude Code snapshots in
tests/fixtures/screens ($FAKE_TMUX_FIXTURES). Only the text of the input line (and the number of
folded lines) is replaced:
  empty input       01-empty.txt (-p) / 18-cleared.ansi (-p -e)
  1..3 typed lines  the same skeleton, the input rows replaced
  4+ lines pasted   04-folded.ansi (-e) / the same without SGR (-p): «[Pasted text #1 +K lines]»
Panes (#57): a session owns a list of panes ({id, pid, claude, opts}); `%N` ids and pane_pid values are
allocated by counters in $FAKE_TMUX_STATE. `claude: true` is the pane that hosts the wave's Claude: `/exit` closes
THAT pane (a session lives on while another pane, e.g. one the owner split off, is left). User options
(`@wab_run`, `@wab_run_dir`, `@wab_open`) live on the session or on the pane and are read back exactly as tmux 3.4
answers (checked on a private socket: `show-options -v` of an unset option is rc 1 `invalid option: <name>`,
`kill-pane` of the last pane ends the session, no session at all is `no server running on <socket>`).
Model limit: the first Enter on a folded paste only «expands» it (the dispatcher's own comment on PASTE_PREVIEW)
but no snapshot of the expanded 4+ line input exists, so the screen stays the folded one; the second Enter submits.
"""
import json
import os
import re
import sys

STATE = os.environ["FAKE_TMUX_STATE"]
FIXTURES = os.environ["FAKE_TMUX_FIXTURES"]
SESSIONS = os.path.join(STATE, "sessions")
BUFFERS = os.path.join(STATE, "buffers")
SGR = re.compile(r"\x1b\[[0-9;:]*m")


def jlog(name, obj):
    with open(os.path.join(STATE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def fail(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def spath(name):
    return os.path.join(SESSIONS, name + ".json")


def load(name):
    try:
        with open(spath(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(s):
    os.makedirs(SESSIONS, exist_ok=True)
    tmp = spath(s["name"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, spath(s["name"]))


def target(value):
    return value.lstrip("=").split(":")[0]


def counter(name):
    path = os.path.join(STATE, name + ".seq")
    try:
        with open(path, encoding="utf-8") as f:
            n = int(f.read())
    except FileNotFoundError:
        n = 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(n + 1))
    return n


def new_pane(claude):
    n = counter("pane")
    return {"id": f"%{n}", "pid": 4100 + n, "claude": claude, "opts": {}}


def all_sessions():
    try:
        names = sorted(f[:-5] for f in os.listdir(SESSIONS) if f.endswith(".json"))
    except FileNotFoundError:
        return []
    return [s for s in (load(n) for n in names) if s]


def locate(value):
    """(session, pane) of a target: a pane id `%N`, or a session (`=name:`; its first pane stands for «active»)."""
    if re.fullmatch(r"%\d+", value):
        for s in all_sessions():
            for p in s["panes"]:
                if p["id"] == value:
                    return s, p
        return None, None
    s = load(target(value))
    return s, (s["panes"][0] if s and s["panes"] else None)


def no_target(value):
    if re.fullmatch(r"%\d+", value):
        fail(f"can't find pane: {value}")
    fail(f"can't find session: {target(value)}")


def need(value):
    s, _ = locate(value)
    if s is None:
        no_target(value)
    return s


def no_server():
    fail("no server running on " + os.environ.get("FAKE_TMUX_SOCKET", "the private socket"))


def opt_args(args, with_value=("-t", "-F")):
    """(flags, {-t: v, -F: v}, positionals) of a tmux command's args."""
    flags, vals, rest, i = set(), {}, [], 0
    while i < len(args):
        a = args[i]
        if a in with_value:
            vals[a] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif a.startswith("-") and len(a) > 1 and not rest:
            flags.add(a)
            i += 1
        else:
            rest.append(a)
            i += 1
    return flags, vals, rest


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read().split("\n")


def render(s, ansi):
    text = s["input"]
    if not text:
        return "\n".join(fixture("18-cleared.ansi" if ansi else "01-empty.txt"))
    if s["folded"]:
        lines = [re.sub(r"\+\d+ lines", "+%d lines" % text.count("\n"), l) for l in fixture("04-folded.ansi")]
        return "\n".join(lines if ansi else [SGR.sub("", l) for l in lines])
    base = fixture("18-cleared.ansi" if ansi else "01-empty.txt")
    rows = text.split("\n")
    first = ("\x1b[39m" if ansi else "") + "❯ " + rows[0]
    return "\n".join(base[:37] + [first] + ["  " + r for r in rows[1:]] + base[38:])


def enter(s):
    text = s["input"]
    if s["folded"] and not s.get("expanded"):
        # wab.py's own live observation (PASTE_PREVIEW): the first Enter on a folded paste only expands
        # it. No snapshot of the expanded input exists, so the screen stays the folded one; the
        # dispatcher must see «paste again to expand» and press Enter once more.
        s["expanded"] = True
        save(s)
        return
    s["input"], s["folded"], s["expanded"] = "", False, False
    if text.strip():
        n = 0
        try:
            with open(os.path.join(STATE, "inbox.jsonl"), encoding="utf-8") as f:
                n = sum(1 for _ in f)
        except FileNotFoundError:
            pass
        jlog("inbox.jsonl", {"seq": n + 1, "session": s["name"], "text": text})
        if text.strip() == "/exit":  # claude leaves: its pane goes, the session with its last pane
            s["panes"] = [p for p in s["panes"] if not p["claude"]]
            if s["panes"]:
                save(s)
            else:
                os.unlink(spath(s["name"]))
            return
    save(s)


def keys(s, names):
    for key in names:
        if key == "Enter":
            enter(s)
            if load(s["name"]) is None:
                return
            s = load(s["name"])
        elif key == "C-u":  # erase the current line up to the cursor (the end); a folded paste goes whole
            if s["folded"]:
                s["input"], s["folded"], s["expanded"] = "", False, False
            else:
                rows = s["input"].split("\n")
                rows[-1] = ""
                s["input"] = "\n".join(rows)
        elif key == "BSpace":
            if s["input"].endswith("\n"):
                s["input"] = s["input"][:-1]
            elif s["input"]:
                s["input"] = s["input"][:-1]
        # C-e, C-k, Down, ... : the cursor is always at the end, nothing to do
    save(s)


def main(argv):
    rest = list(argv)
    sock = None
    while len(rest) > 1 and rest[0] in ("-S", "-L"):
        sock, rest = (rest[0], rest[1]), rest[2:]
    jlog("calls.jsonl", {"argv": argv, "sock": list(sock) if sock else None})
    if sock is None:
        fail("tmux without a private socket in tests: tmux " + " ".join(argv), 97)
    if rest == ["-V"]:
        print("tmux 3.4")
        return
    cmd, args = rest[0], rest[1:]
    if cmd == "has-session":
        sys.exit(0 if load(target(args[args.index("-t") + 1])) else 1)
    if cmd == "new-session":
        s = {"name": None, "cwd": None, "env": {}, "command": [], "input": "", "folded": False,
             "panes": [new_pane(True)], "opts": {}}
        i = 0
        print_pane = False
        while i < len(args):
            a = args[i]
            if a in ("-d", "-P"):
                print_pane = print_pane or a == "-P"
                i += 1
            elif a == "-F":  # (#57) `-P -F #{pane_id}`: the new pane's id
                i += 2
            elif a in ("-s", "-c", "-x", "-y", "-e"):
                if a == "-s":
                    s["name"] = args[i + 1]
                elif a == "-c":
                    s["cwd"] = args[i + 1]
                elif a == "-e":
                    k, _, v = args[i + 1].partition("=")
                    s["env"][k] = v
                i += 2
            else:
                s["command"] = args[i:]
                break
        if load(s["name"]):
            fail(f"duplicate session: {s['name']}")
        save(s)
        if "WAB_WAVE" in s["env"]:  # the sessions the dispatcher starts; the owner's own are not the Actor's business
            jlog("new_sessions.jsonl", {k: s[k] for k in ("name", "cwd", "env", "command")})
        if print_pane:
            print(s["panes"][0]["id"])
        return
    if cmd == "kill-session":
        name = target(args[args.index("-t") + 1])
        if not load(name):
            fail(f"can't find session: {name}")
        os.unlink(spath(name))
        return
    if cmd == "kill-pane":
        s, pane = locate(args[args.index("-t") + 1])
        if pane is None:
            no_target(args[args.index("-t") + 1])
        s["panes"] = [p for p in s["panes"] if p["id"] != pane["id"]]
        if s["panes"]:
            save(s)
        else:
            os.unlink(spath(s["name"]))
        return
    if cmd == "split-window":  # the owner splits a pane off a session (a shell next to the wave's Claude)
        flags, vals, _ = opt_args(args)
        s = need(vals["-t"])
        pane = new_pane(False)
        s["panes"].append(pane)
        save(s)
        if "-P" in flags:
            print(vals.get("-F", "#{pane_id}").replace("#{pane_id}", pane["id"]))
        return
    if cmd == "set-option":
        flags, vals, rest = opt_args(args)
        name = rest[0] if rest else ""
        if "-g" in flags or "-t" not in vals or not name.startswith("@"):
            return  # global / window options of the key binding code: nothing to keep
        s, pane = locate(vals["-t"])
        if s is None:
            no_target(vals["-t"])
        bucket = pane["opts"] if "-p" in flags else s["opts"]
        if "-u" in flags:
            bucket.pop(name, None)
        else:
            bucket[name] = rest[1] if len(rest) > 1 else ""
        save(s)
        return
    if cmd == "show-options":
        flags, vals, rest = opt_args(args)
        if "-t" not in vals:
            if "-v" in flags:
                fail("invalid option: " + (rest[0] if rest else ""), 1)
            return
        s, pane = locate(vals["-t"])
        if s is None:
            fail(f"no such session: {vals['-t']}")
        bucket = pane["opts"] if "-p" in flags else s["opts"]
        if "-v" in flags:
            if not rest or rest[0] not in bucket:
                fail("invalid option: " + (rest[0] if rest else ""), 1)
            print(bucket[rest[0]])
            return
        for name, value in bucket.items():
            print(f'{name} "{value}"' if " " in value else f"{name} {value}")
        return
    if cmd == "list-panes":
        flags, vals, _ = opt_args(args)
        if "-a" in flags:
            sessions = all_sessions()
            if not sessions:
                no_server()
        else:
            sessions = [need(vals["-t"])]
        for s in sessions:
            for p in s["panes"]:
                print(vals.get("-F", "#{pane_id}").replace("#{pane_id}", p["id"])
                      .replace("#{session_name}", s["name"]).replace("#{pane_pid}", str(p["pid"])))
        return
    if cmd == "list-sessions":
        sessions = all_sessions()
        if not sessions:
            no_server()
        for s in sessions:
            print(f"{s['name']}: 1 windows (created Sat Oct  4 12:00:00 2026)")
        return
    if cmd == "capture-pane":
        s = need(args[args.index("-t") + 1])
        kind = "empty" if not s["input"] else "folded" if s["folded"] else "typed"
        jlog("screens.jsonl", {"session": s["name"], "kind": kind, "ansi": "-e" in args})
        sys.stdout.write(render(s, "-e" in args) + "\n")
        return
    if cmd == "send-keys":
        s = need(args[args.index("-t") + 1])
        tail = args[args.index("-t") + 2:]
        if tail and tail[0] == "-l":
            s["input"] += tail[1] if len(tail) > 1 else ""
            save(s)
        else:
            keys(s, tail)
        return
    if cmd == "load-buffer":
        name = args[args.index("-b") + 1]
        os.makedirs(BUFFERS, exist_ok=True)
        with open(os.path.join(BUFFERS, name), "w", encoding="utf-8") as f:
            f.write(sys.stdin.read())
        return
    if cmd == "paste-buffer":
        s = need(args[args.index("-t") + 1])
        path = os.path.join(BUFFERS, args[args.index("-b") + 1])
        with open(path, encoding="utf-8") as f:
            s["input"] += f.read()
        s["folded"] = s["input"].count("\n") + 1 >= 4  # 4+ pasted lines fold (screens/README.md)
        if "-d" in args:
            os.unlink(path)
        save(s)
        return
    if cmd == "list-clients":
        print("client0")
        return
    if cmd == "display-message":
        if "-p" in args:  # `-p -t <target> #{pane_pid}`: the pid of the pane's process (tmux prints it as is)
            s, pane = locate(args[args.index("-t") + 1])
            if pane is None:
                no_target(args[args.index("-t") + 1])
            print(args[-1].replace("#{pane_pid}", str(pane["pid"])).replace("#{pane_id}", pane["id"]))
            return
        jlog("display.jsonl", {"args": args})
        return
    if cmd in ("list-keys", "source-file", "unbind-key", "bind-key"):
        return
    jlog("calls.jsonl", {"unsupported": cmd, "argv": argv})
    fail(f"unsupported tmux command in the test stand-in: {cmd}", 2)


if __name__ == "__main__":
    main(sys.argv[1:])
