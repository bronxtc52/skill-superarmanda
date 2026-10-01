#!/usr/bin/env python3
"""wave-autobot live dashboard: `dash.py <chain.json>` (run inside tmux, Ctrl-C to quit).

Needs a terminal: `rich` draws nothing when stdout is a file or a pipe, so without a tty the
command refuses to start. Transcripts are bound to a wave by the session ids in state.json
and read incrementally (wab.TranscriptCache), so a frame costs only what was appended."""
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

from rich import box
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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


def commits_since(cwd, started, until=None):
    """Commits in the wave's workdir from its start up to its end (`finished`), so a later wave
    sharing the workdir does not grow a finished wave's counter."""
    window = [f"--since=@{int(started)}"] + ([f"--until=@{int(until)}"] if until else [])
    r = subprocess.run(["git", "-C", cwd, "log", "--all", "--oneline", *window],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return len(r.stdout.splitlines()) if r.returncode == 0 else 0


def wave_state(cfg, st, wave):
    w = st["waves"].get(wave)
    if not w:
        return "pending", w
    status = wab.read(cfg["run_dir"] / wave / "status")
    if status.startswith("BLOCKED"):
        return "BLOCKED", w
    if w.get("phase") == "awaiting_merge":
        return "awaiting_merge", w
    if w.get("phase") == "checkpoint" and status != "HANDOFF_READY":
        return "checkpoint", w
    if st.get("current") == wave and not wab.tmux_alive(w["tmux"]) and status != "DONE":
        return "dead", w
    return status or "STARTING", w


def pipeline(cfg, st):
    t = Text(justify="center")
    for i, wave in enumerate(cfg["waves"]):
        key, _ = wave_state(cfg, st, wave)
        icon, colour, _ = STYLE.get(key, ("?", "white", key))
        current = st.get("current") == wave
        t.append(f" {icon} {wave} ", style=f"{colour} {'reverse' if current else ''}")
        if i < len(cfg["waves"]) - 1:
            done = key in ("DONE", "awaiting_merge")
            t.append(" ━━▶ " if done else " ──▷ ", style="green" if done else "grey42")
    titles = cfg.get("titles", {})
    sub = Text("  ·  ".join(f"{w}: {titles[w]}" for w in cfg["waves"] if w in titles),
               style="grey62", justify="center")
    return Group(t, sub)


def waves_table(cfg, st):
    tb = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
    for col, j in (("Волна", "left"), ("Статус", "left"), ("Время", "right"), ("Контекст", "left"),
                   ("Пик", "right"), ("↻", "right"), ("Ходы", "right"), ("Tools", "right"),
                   ("Агенты", "right"), ("Вывод", "right"), ("Коммиты", "right")):
        tb.add_column(col, justify=j, no_wrap=True)
    now = time.time()
    for wave in cfg["waves"]:
        key, w = wave_state(cfg, st, wave)
        icon, colour, label = STYLE.get(key, ("?", "white", key))
        if not w:
            tb.add_row(Text(wave, style="grey50"), Text(f"{icon} {label}", style=colour), *[""] * 9)
            continue
        s = wave_stats(w)
        end = w.get("finished") or now
        tb.add_row(
            Text(wave, style="bold"), Text(f"{icon} {label}", style=colour),
            fmt_dur(end - w["started"]), bar(w.get("tokens", 0), cfg["ctx_limit"], 16),
            ktok(w.get("peak", 0)), str(w.get("restarts", 0)), str(s["turns"]), str(s["tools"]),
            str(s["agents"]), ktok(s["out"]), str(commits_since(w["cwd"], w["started"], w.get("finished"))))
    return tb


def current_panel(cfg, st):
    wave = st.get("current")
    if not wave:
        return Panel(Align.center(Text("цепочка не запущена или завершена", style="grey50")),
                     title="Текущая волна", border_style="grey42")
    w = st["waves"][wave]
    status = wab.read(cfg["run_dir"] / wave / "status")
    head = Text()
    if BINDING:
        head.append(" Ctrl+\\ ", style="bold black on bright_yellow")
        head.append(" открыть/закрыть волну  ·  ", style="bright_yellow")
    elif BINDING is False:
        head.append("Ctrl+\\ не подключён (см. события)  ·  ", style="grey50")
    head.append(f"{wave}  ", style="bold magenta")
    head.append(wab.attach_cmd(w["tmux"]), style="bold white on grey23")
    head.append(f"   статус: {status}", style="yellow" if status.startswith("BLOCKED") else "green")
    ctx = Group(Text("Контекст ", style="bold").append(bar(w.get("tokens", 0), cfg["ctx_limit"], 40)),
                Text("История  ", style="bold").append(spark(w.get("ctx_hist", []), cfg["ctx_limit"])))
    lines = [l for l in wab.pane_text(w["tmux"]).splitlines() if l.strip()][-14:]
    screen = Text("\n".join(l[:150] for l in lines), style="grey78")
    return Panel(Group(head, Text(), ctx, Text(), Panel(screen, title="экран волны (live)",
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
    done = sum(1 for w in cfg["waves"] if wab.read(cfg["run_dir"] / w / "status") == "DONE")
    started = min((w["started"] for w in waves.values()), default=time.time())
    restarts = sum(w.get("restarts", 0) for w in waves.values())
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
