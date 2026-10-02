#!/usr/bin/env python3
"""wave-autobot live dashboard: `dash.py <chain.json>` (run inside tmux, Ctrl-C to quit).

Needs a terminal: `rich` draws nothing when stdout is a file or a pipe, so without a tty the
command refuses to start. Transcripts are bound to a wave by the session ids in state.json
and read incrementally (wab.TranscriptCache), so a frame costs only what was appended."""
import json
import math
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import time

RICH_MISSING = "dash.py needs the Python package rich: python3 -m pip install --user rich"
try:  # the one third-party package of the waves runtime, not installed with the skill
    from rich import box
    from rich.align import Align
    from rich.console import Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    if __name__ == "__main__":  # the command: one clear line and rc 2, no traceback
        print(RICH_MISSING, file=sys.stderr)
        sys.exit(2)
    raise  # imported as a module: the importer decides

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wab  # noqa: E402

SPARK = "▁▂▃▄▅▆▇█"
STYLE = {  # phase/status -> (icon, colour, label)
    "pending": ("○", "grey50", "в очереди"),
    "STARTING": ("◌", "cyan", "старт"),
    "RUNNING": ("⚙", "bright_green", "работает"),
    "RESUMING": ("↻", "cyan", "свежая голова"),
    "checkpoint": ("💾", "yellow", "handoff"),
    "HANDOFF_READY": ("💾", "yellow", "handoff готов"),
    "BLOCKED": ("✋", "bold red", "ждёт тебя"),
    "DONE": ("✔", "bold green", "готово"),
    "awaiting_merge": ("⏳", "bright_yellow", "ждёт мерджа"),
    "dead": ("✖", "bold red", "окно закрыто"),
}
CACHE = wab.TranscriptCache()  # full counters: no tail limit here
# Ctrl+\ of this dashboard: None - not in tmux (no binding), True - registered by wab, False - refused
# (a foreign binding of the key, an unsafe path...). Only True advertises the key.
BINDING = None


def fmt_dur(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}ч{m:02d}м" if h else f"{m}м{sec % 60:02d}с"


def ktok(n):
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n // 1000}k"


def bar(value, limit, width=24):
    frac = min(value / limit, 1.0) if limit else 0
    fill = int(frac * width)
    colour = "green" if frac < 0.6 else "yellow" if frac < 0.9 else "red"
    t = Text("█" * fill, style=colour)
    t.append("░" * (width - fill), style="grey30")
    t.append(f" {ktok(value)}/{ktok(limit)}", style="bold " + colour)
    return t


def spark(values, limit, width=60):
    vals = values[-width:]
    if not vals:
        return Text("—", style="grey50")
    out = Text()
    for v in vals:
        frac = min(v / limit, 1.0) if limit else 0
        colour = "green" if frac < 0.6 else "yellow" if frac < 0.9 else "red"
        out.append(SPARK[min(int(frac * len(SPARK)), len(SPARK) - 1)], style=colour)
    return out


def wave_stats(w):
    """Counters of ONE wave: its own sessions (state.json `sessions`) and their subagents,
    plus those of its earlier attempts (`attempts`: a restart archives the old record there,
    with its own cwd), each session counted once. A shared workdir never mixes waves:
    nothing is found by directory."""
    s = {"turns": 0, "tools": 0, "out": 0, "read": 0, "agents": 0}
    seen = set()  # a session id is a uuid: the same id in two records is the same session
    attempts = w.get("attempts")
    attempts = [a for a in attempts if isinstance(a, dict)] if isinstance(attempts, list) else []
    for rec in [w, *attempts]:  # a corrupt state.json `attempts` is skipped, not a crashed frame
        cwd = rec.get("cwd") or w["cwd"]
        for sid in rec.get("sessions") or []:
            if sid in seen:
                continue
            seen.add(sid)
            main = CACHE.read(wab.transcript_path(cwd, sid))
            for k in ("turns", "tools", "out", "read"):
                s[k] += main[k]
            for f in sorted((wab.transcript_dir(cwd) / sid / "subagents").glob("*.jsonl")):
                sub = CACHE.read(f)
                s["agents"] += 1
                for k in ("tools", "out", "read"):  # a subagent's turns are not the wave's turns
                    s[k] += sub[k]
    return s


_attempts = wab.wave_attempts  # the pure helpers live in wab (the end-of-chain summary needs them too)
_num = wab.num
wave_duration = wab.wave_duration


def wave_peak(w):
    """The highest context fill over all tries of the wave (a restart does not reset it)."""
    return max((int(p) for p in (_num(r.get("peak")) for r in [w, *_attempts(w)])
                if p is not None and p > 0), default=0)


wave_restarts = wab.wave_restarts


GIT_TIMEOUT = 10  # seconds: the frame is drawn synchronously, a slow repository must not hang it


def commits_since(cwd, started, until=None):
    """Commits in the wave's workdir from its start up to its end (`finished`), so a later wave
    sharing the workdir does not grow a finished wave's counter. None (shown «?») when git cannot
    tell: a timeout, no git, an unreachable workdir or an error exit - unknown, not a false 0."""
    window = [f"--since=@{int(started)}"] + ([f"--until=@{int(until)}"] if until else [])
    try:
        r = subprocess.run(["git", "-C", cwd, "log", "--all", "--oneline", *window],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return len(r.stdout.splitlines()) if r.returncode == 0 else None


def _fixed_commits(n):
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None


def wave_commits(w):
    """A wave's commits: those of the current try plus the counters of its earlier tries
    (`attempts`, archived by a restart; non-objects there are skipped, like in wave_stats).
    An earlier try counts only its fixed number (`commits`, set when it ended): one that never
    got one (not_ready, or a record from before the unknown marker) adds 0 - its `start_rev..HEAD` would count the
    newer try's commits on the shared HEAD. None (shown «?») when any fixed number is unknown
    (null: git could not count when it ended) or corrupt (not a non-negative int)."""
    own = _own_commits(w)
    attempts = w.get("attempts")
    attempts = [a for a in attempts if isinstance(a, dict)] if isinstance(attempts, list) else []
    total = own
    for a in attempts:
        if "commits" not in a:
            continue
        n = _fixed_commits(a["commits"])
        if n is None or total is None:
            return None
        total += n
    return total


def _own_commits(w):
    """The current try: the number fixed when it ended (`commits`), else `start_rev..HEAD` in its
    workdir (its own HEAD only: other branches and fetched refs do not count). A record from before
    `start_rev` existed keeps the time window over the repository. None (shown «?») when the
    count is unknown: frozen as unknown (`commits: null`, git failed when the wave ended), git
    cannot count `start_rev..HEAD` now, or the fixed number is corrupt."""
    if "commits" in w:
        return _fixed_commits(w["commits"])
    if isinstance(w.get("start_rev"), str):
        return wab.count_wave_commits(w.get("cwd"), w["start_rev"])  # None: git cannot tell, «?»
    return commits_since(w["cwd"], w["started"], w.get("finished"))


def wave_status(cfg, wave, w):
    """The status of a wave as the watch decides on it: the file, else (unreadable or mid-rewrite)
    the last known status from the record. One function for the table and the current panel."""
    status = wab.read(cfg["run_dir"] / wave / "status", on_error=None)
    if not status:
        status = w.get("last_status") or ""
        status = status if isinstance(status, str) else ""
    return status


def fmt_commits(n):
    return "?" if n is None else str(n)


# A terminal phase wins over the status file: the file keeps the wave's last word (a BLOCKED
# question of a window that has since closed), the phase is what the watch decided afterwards.
# Every phase of wab.WINDOW_GONE has a row (a test holds it).
TERMINAL_PHASES = {"dead": "dead", "awaiting_merge": "awaiting_merge", "done": "DONE"}
# the states the view shows as a finished wave: the pipeline's solid arrow, the header's count
FINISHED = ("DONE", "awaiting_merge")


def wave_state(cfg, st, wave):
    w = st["waves"].get(wave)
    if not w:
        return "pending", w
    if w.get("phase") in TERMINAL_PHASES:
        return TERMINAL_PHASES[w["phase"]], w
    status = wave_status(cfg, wave, w)
    # the window is gone before the watch noticed it: nobody to answer, no attach
    if st.get("current") == wave and status != "DONE" and not wab.tmux_alive(w["tmux"]):
        return "dead", w
    if status.startswith("BLOCKED"):
        return "BLOCKED", w
    if w.get("phase") == "checkpoint" and status != "HANDOFF_READY":
        return "checkpoint", w
    return status or "STARTING", w


def pipeline(cfg, st):
    t = Text(justify="center")
    for i, wave in enumerate(cfg["waves"]):
        key, _ = wave_state(cfg, st, wave)
        icon, colour, _ = STYLE.get(key, ("?", "white", key))
        current = st.get("current") == wave
        t.append(f" {icon} {wave} ", style=f"{colour} {'reverse' if current else ''}")
        if i < len(cfg["waves"]) - 1:
            done = key in FINISHED
            t.append(" ━━▶ " if done else " ──▷ ", style="green" if done else "grey42")
    titles = cfg.get("titles") or {}
    sub = Text("  ·  ".join(f"{w}: {titles[w]}" for w in cfg["waves"] if w in titles),
               style="grey62", justify="center")
    return Group(t, sub)


def waves_table(cfg, st):
    tb = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
    for col, j in (("Волна", "left"), ("Статус", "left"), ("PR", "right"), ("Время", "right"), ("Контекст", "left"),
                   ("Пик", "right"), ("↻", "right"), ("Ходы", "right"), ("Tools", "right"),
                   ("Агенты", "right"), ("Вывод", "right"), ("Коммиты", "right")):
        tb.add_column(col, justify=j, no_wrap=True)
    now = time.time()
    for wave in cfg["waves"]:
        key, w = wave_state(cfg, st, wave)
        icon, colour, label = STYLE.get(key, ("?", "white", key))
        if not w:
            tb.add_row(Text(wave, style="grey50"), Text(f"{icon} {label}", style=colour), *[""] * 10)
            continue
        s = wave_stats(w)
        tb.add_row(
            Text(wave, style="bold"), Text(f"{icon} {label}", style=colour),
            "" if wave_pr(w) is None else f"#{wave_pr(w)}", fmt_dur(wave_duration(w, now)), bar(w.get("tokens", 0), cfg["ctx_limit"], 16),
            ktok(wave_peak(w)), str(wave_restarts(w)), str(s["turns"]), str(s["tools"]),
            str(s["agents"]), ktok(s["out"]), fmt_commits(wave_commits(w)))
    return tb


MANIFEST_POLL_SECONDS = 15  # `state.py where` is a subprocess: not more often than this per manifest
WHERE_TIMEOUT = 10
MANIFEST_CACHE = {}  # manifest path -> (signature, taken at, result)
STATE_PY = pathlib.Path(__file__).resolve().parent.parent / "state.py"
VERDICT_ROLES = ("tester", "cross_provider_reviewer", "github_codex_review", "coderabbit")
ARTIFACT_LIMIT = 2 * 1024 * 1024  # bytes of a findings artifact read for the frame
SEVERITIES = ("critical", "high", "medium", "low", "P0", "P1", "P2", "P3")


def manifest_where(cfg, wave):
    """`state.py where` of the wave's superarmanda manifest (never parsed here): None when there is
    no manifest yet, {"error": ...} when it cannot be read, else the printed JSON object. An entry
    (a failure too) is reused only while the manifest is unchanged (mtime, size) AND younger than
    MANIFEST_POLL_SECONDS: `where` also depends on the repository, so it is asked again after the
    interval; a changed manifest is asked at once."""
    path = cfg["run_dir"] / wave / "superarmanda" / "manifest.json"
    try:
        stat = path.stat()
    except OSError:
        return None
    sig, now = (stat.st_mtime_ns, stat.st_size), time.time()
    hit = MANIFEST_CACHE.get(path)
    if hit and hit[0] == sig and 0 <= now - hit[1] < MANIFEST_POLL_SECONDS:
        return hit[2]
    try:
        proc = subprocess.run([sys.executable, str(STATE_PY), "where", "--manifest", str(path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=WHERE_TIMEOUT)
        if proc.returncode != 0:
            lines = [l.strip() for l in (proc.stderr or proc.stdout).splitlines() if l.strip()]
            raise ValueError(lines[-1] if lines else f"rc {proc.returncode}")
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError("where: not an object")
    except subprocess.TimeoutExpired:
        result = {"error": f"state.py where: таймаут {WHERE_TIMEOUT} с"}
    except (OSError, ValueError) as e:
        result = {"error": str(e)[:150]}
    MANIFEST_CACHE[path] = (sig, now, result)
    return result


def finding_counts(artifact):
    """{severity: n} of a local findings JSON (`findings[].severity|priority`), None when it is a
    URL, unreadable or not in that shape: shown as «?»."""
    try:
        if not isinstance(artifact, str) or "://" in artifact:
            return None
        fd = os.open(artifact, os.O_RDONLY | os.O_NONBLOCK)  # bytes (decoded below, encoding utf-8); a FIFO without a writer must not block
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = b""
            while len(raw) <= ARTIFACT_LIMIT:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
        if len(raw) > ARTIFACT_LIMIT:
            return None
        data = json.loads(raw.decode("utf-8"))
        items = data["findings"] if isinstance(data, dict) else data
        counts = {}
        for item in items:
            sev = str(item.get("severity") or item.get("priority")).strip()
            sev = sev.lower() if sev.lower() in SEVERITIES else sev.upper()
            counts[sev] = counts.get(sev, 0) + 1
        return counts
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def wave_pr(w):
    merged = w.get("merged")
    for v in (w.get("pr"), w.get("gate_pr"), merged.get("pr") if isinstance(merged, dict) else None):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def manifest_lines(cfg, wave, w):
    """The superarmanda block of the current panel; never raises on a manifest it cannot read."""
    where = manifest_where(cfg, wave)
    head = Text("superarmanda  ", style="bold")
    if where is None:
        return [head.append("manifest ещё нет", style="grey50")]
    if where.get("error"):
        return [head.append(f"manifest: {where['error']}", style="yellow")]
    head.append(f"{where.get('task')} ", style="cyan").append(f"[{where.get('task_status')}]  ", style="grey62")
    head.append(f"шаг {where.get('step')} ({where.get('role')})", style="bright_green")
    fr = where.get("fix_round") if isinstance(where.get("fix_round"), dict) else {}
    srcs = "  ".join(f"{k} {v}" for k, v in fr.items() if k != "total")
    head.append(f"   круг {srcs or '—'}  total {fr.get('total', '—')}", style="yellow")
    verdicts = where.get("verdicts") if isinstance(where.get("verdicts"), dict) else {}
    roles = [*VERDICT_ROLES, *(r for r in verdicts if r not in VERDICT_ROLES)]
    vt = Text("вердикты  ", style="bold")
    for r in roles:
        v = verdicts.get(r)
        vt.append(f"{r}: ", style="grey62").append(f"{v or '—'}  ", style="green" if v == "pass" else "yellow" if v else "grey50")
    open_f = [f for f in where.get("open_findings") or [] if isinstance(f, dict)]
    ft = Text("находки  ", style="bold")
    if not open_f:
        ft.append("нет открытых", style="green")
    for f in open_f:
        counts = finding_counts(f.get("artifact"))
        label = "?" if counts is None else (", ".join(f"{k} {n}" for k, n in sorted(
            counts.items(), key=lambda kv: SEVERITIES.index(kv[0]) if kv[0] in SEVERITIES else 99)) or "0")
        ft.append(f"{f.get('role')} {f.get('status')}: {label}  ", style="red")
    pr = wave_pr(w)
    codex = verdicts.get("github_codex_review") or "нет результата"
    pt = Text("PR  ", style="bold").append(f"#{pr}" if pr is not None else "—", style="cyan")
    pt.append(f"   Codex: {codex}", style="grey78")
    lines = [head, vt, ft, pt]
    if where.get("decision_required_for"):
        lines.append(Text(f"нужно решение: {where['decision_required_for']}", style="bold red"))
    nxt = str(where.get("next_action") or "")
    lines.append(Text("дальше  ", style="bold").append(nxt if len(nxt) <= 110 else nxt[:109] + "…", style="grey78"))
    return lines


def current_panel(cfg, st):
    wave = st.get("current")
    if not wave:
        return Panel(Align.center(Text("цепочка не запущена или завершена", style="grey50")),
                     title="Текущая волна", border_style="grey42")
    w = st["waves"][wave]
    status = wave_status(cfg, wave, w)
    key, _ = wave_state(cfg, st, wave)
    head = Text()
    if BINDING:
        head.append(" Ctrl+\\ ", style="bold black on bright_yellow")
        head.append(" открыть/закрыть волну  ·  ", style="bright_yellow")
    elif BINDING is False:
        head.append("Ctrl+\\ не подключён (см. события)  ·  ", style="grey50")
    head.append(f"{wave}  ", style="bold magenta")
    if key == "dead" or w.get("phase") in TERMINAL_PHASES:
        # no window to attach to; the file's status is only the wave's last word
        _, colour, label = STYLE[key]
        head.append(label, style=colour)
        head.append(f"   последний статус: {status}", style="grey62")
    else:
        head.append(wab.attach_cmd(w["tmux"]), style="bold white on grey23")
        head.append(f"   статус: {status}", style="yellow" if key == "BLOCKED" else "green")
    ctx = Group(Text("Контекст ", style="bold").append(bar(w.get("tokens", 0), cfg["ctx_limit"], 40)),
                Text("История  ", style="bold").append(spark(w.get("ctx_hist", []), cfg["ctx_limit"])))
    lines = [l for l in wab.pane_text(w["tmux"]).splitlines() if l.strip()][-14:]
    screen = Text("\n".join(l[:150] for l in lines), style="grey78")
    try:
        block = manifest_lines(cfg, wave, w)
    except Exception as e:  # noqa: BLE001 - a block of the frame, not the frame
        block = [Text(f"manifest: {type(e).__name__}: {e}", style="yellow")]
    return Panel(Group(head, Text(), ctx, Text(), *block, Text(), Panel(screen, title="экран волны (live)",
                                                         border_style="grey35", box=box.ROUNDED)),
                 title=f"⚙ Текущая волна {wave}", border_style="magenta")


def events_panel(cfg, n=12):
    p = cfg["run_dir"] / "events.log"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:] if p.exists() else []
    t = Text()
    for l in lines:
        ts, _, msg = l.partition(" ")
        colour = ("red" if any(k in msg for k in ("BLOCKED", "gone", "FAILED", "idle", "permission", "auto mode"))
                  else "yellow" if any(k in msg for k in ("checkpoint", "handoff", "/clear"))
                  else "green" if any(k in msg for k in ("DONE", "launched", "finished"))
                  else "grey70")
        t.append(ts[11:19] + " ", style="grey50")
        t.append(msg[:140] + "\n", style=colour)
    return Panel(t or Text("событий пока нет", style="grey50"), title="📜 События", border_style="blue")


def header(cfg, st):
    waves = st["waves"]
    # finished as the row and the pipeline show it, not by the raw status file: a done wave whose
    # file is gone, unreadable or a directory stays in the count; a dead wave does not
    done = sum(1 for w in cfg["waves"] if wave_state(cfg, st, w)[0] in FINISHED)
    # the chain's earliest known start, archived tries included: a wave restarted after dead or
    # not_ready keeps its original start in `attempts`, the timer does not reset to the retry;
    # a corrupt or missing `started` is skipped, not a crashed frame
    started = min((s for s in (_num(r.get("started")) for w in waves.values()
                               for r in [w, *_attempts(w)]) if s is not None),
                  default=time.time())
    restarts = sum(wave_restarts(w) for w in waves.values())
    turns = sum(wave_stats(w)["turns"] for w in waves.values())
    t = Text(justify="center")
    t.append("🌊 wave-autobot ", style="bold bright_cyan")
    t.append(f"· {cfg['chain']} ", style="bold white")
    t.append(f"·  волн {done}/{len(cfg['waves'])}  ", style="green")
    t.append(f"·  в работе {fmt_dur(time.time() - started)}  ", style="white")
    t.append(f"·  свежих голов {restarts}  ", style="yellow")
    t.append(f"·  ходов {turns}  ", style="cyan")
    t.append(f"·  порог {ktok(cfg['ctx_limit'])}  ", style="grey62")
    t.append(time.strftime("·  %H:%M:%S UTC", time.gmtime()), style="grey50")
    return t


def render(cfg):
    st = wab.load_state(cfg)
    lay = Layout()
    lay.split_column(Layout(Panel(header(cfg, st), border_style="bright_cyan"), size=3),
                     Layout(Panel(pipeline(cfg, st), title="Конвейер", border_style="cyan"), size=5),
                     Layout(Panel(waves_table(cfg, st), title="📊 Статистика волн", border_style="cyan"),
                            size=len(cfg["waves"]) + 6),
                     Layout(name="bottom"))
    lay["bottom"].split_row(Layout(current_panel(cfg, st), ratio=3), Layout(events_panel(cfg), ratio=2))
    return lay


def safe_render(cfg):
    """The dispatcher rewrites state.json, status and events.log while we read them:
    a half-written file or a missing key must cost one frame, not the whole view."""
    try:
        return render(cfg)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - any read race; the next frame retries
        return Panel(Text(f"кадр не отрисован: {type(e).__name__}: {e}\nповтор через 3 с",
                          style="yellow"), title="dash", border_style="yellow")


def own_session():
    """(session name, tmux socket id, pane id) of the tmux pane this process runs in, else (None, "", None).
    The server is the one in $TMUX (its socket file name; `default` is the default server)."""
    tmux_env, pane = os.environ.get("TMUX"), os.environ.get("TMUX_PANE")
    if not tmux_env or not pane:
        return None, "", None
    path = tmux_env.split(",")[0]
    # `-L name` looks into $TMUX_TMPDIR (or /tmp) + tmux-<uid>: the short name is valid only for a
    # socket in exactly that directory, else it would name another server in this process
    own_dir = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}")
    if os.path.realpath(os.path.dirname(path)) == os.path.realpath(own_dir):
        sock = os.path.basename(path)
        sock = "" if sock == "default" else sock
    else:
        sock = path  # `tmux -S <path>`: only the full path names this server
    try:
        r = subprocess.run(["tmux", "-S", path, "display-message", "-p", "-t", pane,
                            "#{session_name}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, "", None
    name = r.stdout.strip()
    return (name if r.returncode == 0 and name else None), sock, pane


def main():
    global BINDING
    if len(sys.argv) != 2:
        raise SystemExit("usage: dash.py <chain.json>")
    if not sys.stdout.isatty():
        raise SystemExit("dash.py needs a terminal (tty): run it inside tmux, not redirected "
                         "to a file or a pipe - rich draws nothing there")
    # Ctrl+\ on a session without the binding sends SIGQUIT to the pane process: it must not kill us
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    cfg = wab.load_chain(sys.argv[1], create=False)
    session, sock, pane = own_session()
    if session:  # tmux calls of this process must hit the server we run in, not an env override
        if sock:
            os.environ["WAB_TMUX_SOCKET"] = sock
        else:
            os.environ.pop("WAB_TMUX_SOCKET", None)
    if session:  # Ctrl+\ works in the session the dashboard really runs in: it carries @wab_open
        BINDING = bool(wab.register_dash(cfg, sys.argv[1], session, sock, pane))
    for sig in (signal.SIGTERM, signal.SIGHUP):  # the finally below must run on these too
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    try:
        with Live(safe_render(cfg), refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(3)
                live.update(safe_render(cfg))
    finally:
        if session:  # a session that no longer shows the dashboard must not open the popup on Ctrl+\
            wab.unregister_dash(session, sock, pane)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
