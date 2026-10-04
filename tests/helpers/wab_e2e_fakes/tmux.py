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


def need(value):
    s = load(target(value))
    if s is None:
        fail(f"can't find session: {target(value)}")
    return s


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
        if text.strip() == "/exit":  # claude leaves, the window goes with it
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
        s = {"name": None, "cwd": None, "env": {}, "command": [], "input": "", "folded": False}
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-d":
                i += 1
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
        jlog("new_sessions.jsonl", {k: s[k] for k in ("name", "cwd", "env", "command")})
        return
    if cmd == "kill-session":
        name = target(args[args.index("-t") + 1])
        if not load(name):
            fail(f"can't find session: {name}")
        os.unlink(spath(name))
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
        jlog("display.jsonl", {"args": args})
        return
    if cmd in ("list-keys", "set-option", "source-file", "unbind-key", "bind-key"):
        return
    jlog("calls.jsonl", {"unsupported": cmd, "argv": argv})
    fail(f"unsupported tmux command in the test stand-in: {cmd}", 2)


if __name__ == "__main__":
    main(sys.argv[1:])
