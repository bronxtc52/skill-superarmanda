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


FROZEN_MARK = "⚠ не меряется"  # the bound journal stands still while another one of the wave grows (#44)


BG_TAILS_MARK = "⚠ хвосты"  # background shells left from before /clear or idle for long (#68)


def ctx_cell(w, limit, width):
    """The context bar, with the frozen-context mark when the dispatcher found the session unbound."""
    cell = bar(w.get("tokens", 0), limit, width)
    if w.get("ctx_frozen"):
        cell.append(" " + FROZEN_MARK, style="bold yellow")
    tails = w.get("bg_tails")
    if isinstance(tails, list) and tails:  # shells nobody waits for: the dispatcher saw them (#68)
        cell.append(f" {BG_TAILS_MARK}: {len(tails)}", style="bold yellow")
    return cell


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
            tb.add_row(Text(wave, style="grey50"), Text(f"{icon} {_safe(label)}", style=colour), *[""] * 10)
            continue
        s = wave_stats(w)
        tb.add_row(
            Text(wave, style="bold"), Text(f"{icon} {_safe(label)}", style=colour),
            "" if wave_pr(w) is None else f"#{wave_pr(w)}", fmt_dur(wave_duration(w, now)), ctx_cell(w, cfg["ctx_limit"], 16),
            ktok(wave_peak(w)), str(wave_restarts(w)), str(s["turns"]), str(s["tools"]),
            str(s["agents"]), ktok(s["out"]), fmt_commits(wave_commits(w)))
    return tb


MANIFEST_POLL_SECONDS = 15  # `state.py where` is a subprocess: not more often than this per manifest
WHERE_TIMEOUT = 10
MANIFEST_CACHE = {}  # manifest path -> (signature, taken at, result)
VERDICT_ROLES = ("tester", "cross_provider_reviewer", "github_codex_review", "coderabbit")
ARTIFACT_LIMIT = 2 * 1024 * 1024  # bytes of a findings artifact read for the frame
SEVERITIES = ("critical", "high", "medium", "low", "P0", "P1", "P2", "P3")


def manifest_where(cfg, wave):
    """`state.py where` of the wave's superarmanda manifest (never parsed here): None when there is
    no manifest yet, {"error": ...} when it cannot be read, else the printed JSON object. An entry
    (a failure too) is reused only while the manifest is unchanged (mtime, size) AND younger than
    MANIFEST_POLL_SECONDS: `where` also depends on the repository, so it is asked again after the
    interval; a changed manifest is asked at once."""
    path, why = wab.current_manifest(cfg, wave)
    if path is None:
        return {"error": why}
    try:
        stat = path.stat()
    except (OSError, ValueError):  # ValueError: a NUL in the name of the wave (no runs.json to refuse it)
        return None
    sig, now = (stat.st_mtime_ns, stat.st_size), time.time()
    hit = MANIFEST_CACHE.get(path)
    if hit and hit[0] == sig and 0 <= now - hit[1] < MANIFEST_POLL_SECONDS:
        return hit[2]
    result = wab.manifest_where_of(path, cfg["run_dir"] / wave, WHERE_TIMEOUT)
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


def _safe_mark():
    return wab.MASK


def _masked(value, key=None):
    """`where` (what state.py printed) with every text in it masked by safe_text: the block shows names, tasks,
    next actions and errors of the wave. The `artifact` path stays as it is: it is only OPENED (finding_counts),
    never shown."""
    if isinstance(value, str):
        return value if key == "artifact" else _safe(value)
    if isinstance(value, dict):
        # a key that hides its value (invisible character, secret word) takes the value whole, like wab._masked_leaves
        return {(_safe(k) if isinstance(k, str) else k): (_safe_mark() if wab._key_hides_value(k) else _masked(v, k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def manifest_lines(cfg, wave, w):
    """The superarmanda block of the current panel; never raises on a manifest it cannot read."""
    where = manifest_where(cfg, wave)
    if isinstance(where, dict):
        where = _masked(where)
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
    run = where.get("run")
    if isinstance(run, str) and run:  # `index/max` of the wave's runs; the last one is marked
        head.append(f"   прогон {run}{'!' if where.get('last_run') is True else ''}", style="cyan")
    acc = where.get("accepted")
    if isinstance(acc, int) and not isinstance(acc, bool) and acc > 0:
        head.append(f"   принято {acc}", style="grey62")
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
        ft.append(f"{f.get('role')} {f.get('status')}: {_safe(label)}  ", style="red")
    pr = wave_pr(w)
    codex = verdicts.get("github_codex_review") or "нет результата"
    pt = Text("PR  ", style="bold").append(f"#{pr}" if pr is not None else "—", style="cyan")
    pt.append(f"   Codex: {codex}", style="grey78")
    lines = [head, vt, ft, pt]
    if where.get("decision_required_for"):
        lines.append(Text(f"нужно решение: {where['decision_required_for']}", style="bold red"))
    nxt = str(where.get("next_action") or "")
    lines.append(Text("дальше  ", style="bold").append(nxt if len(nxt) <= 110 else wab._head(nxt, 109) + "…", style="grey78"))
    return lines


def _safe_screen(text):
    return wab.safe_text(text, 10 ** 7, owner_paths=False)


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
        head.append(f"   последний статус: {_safe(status)}", style="grey62")
    else:
        head.append(wab.attach_cmd(w["tmux"]), style="bold white on grey23")
        head.append(f"   статус: {_safe(status)}", style="yellow" if key == "BLOCKED" else "green")
    ctx = Group(Text("Контекст ", style="bold").append_text(ctx_cell(w, cfg["ctx_limit"], 40)),
                Text("История  ", style="bold").append(spark(w.get("ctx_hist", []), cfg["ctx_limit"])))
    # the screen of the wave is shown too: masked as a whole BEFORE it is cut into lines and into 150 characters
    lines = [l for l in _safe_screen(wab.pane_text(w["tmux"])).splitlines() if l.strip()][-14:]
    screen = Text("\n".join(wab._head(l, 150) for l in lines), style="grey78")
    try:
        block = manifest_lines(cfg, wave, w)
    except Exception as e:  # noqa: BLE001 - a block of the frame, not the frame
        block = [Text(_safe(f"manifest: {type(e).__name__}: {wab._exc_text(e)}"), style="yellow")]
    return Panel(Group(head, Text(), ctx, Text(), *block, Text(), Panel(screen, title="экран волны (live)",
                                                         border_style="grey35", box=box.ROUNDED)),
                 title=f"⚙ Текущая волна {wave}", border_style="magenta")


# ---------- «📜 События»: an event type -> a short phrase for a human ----------
# The full line stays in events.log; here it is a phrase with an emoji, the technical part (class=,
# SHA, pid, paths) goes grey on a second line. Everything shown passes wab.safe_text(owner_paths=False):
# the text of an event may quote the wave.
_WAVE = "(?P<w>" + wab.WAVE_NAME.pattern + ")"  # the dispatcher's own notion of a wave name
_PULSE = re.compile(_WAVE + r": phase=(?P<phase>\S*) ctx=(?P<ctx>\S*) restarts=(?P<restarts>\S*) status=(?P<status>.*)", re.S)
_BLOCKED_ANY = re.compile(_WAVE + r": BLOCKED:(?P<rest>.*)", re.S)
_CAP_WHY = {  # BLOCKED class -> why the wave waits (plain words)
    "blocked_cap": "кончились попытки исправить замечание ревью",
    "needs_decision": "нужно решение по спорному месту",
    "question": "у волны вопрос",
    "plan_mismatch": "план расходится с принятым",
    "merge_gate": "гейт мерджа не пройден",
}
OWNER_RED, POLICY_YELLOW, OK_GREEN, INFO_GREY, WARN_YELLOW = "bold red", "yellow", "green", "grey70", "yellow"
_NEEDS_OWNER_CLASSES = ("plan_mismatch", "merge_gate")


def _clip(text, limit):
    text = wab.Masked(" ".join(wab._require_masked(text).split()))  # masked FIRST, then collapsed and cut
    return text if len(text) <= limit else wab.Masked(wab._head(text, limit - 1).rstrip() + "…")


def _blocked_phrase(wave, rest):
    """BLOCKED text (after `BLOCKED:`) -> (phrase, details, colour). Who is needed comes from the label:
    rec=owner, red=yes, a class only the owner answers or no valid label -> the owner; else the policy."""
    label = wab.parse_blocked_label("BLOCKED:" + rest)
    if label is None:
        return (f"⏸ {wave} ждёт твоего решения", _clip(rest, 200) or None, OWNER_RED)
    why = _CAP_WHY.get(label["class"], "нужно решение")
    owner = label["rec"] == "owner" or label["red"] or label["class"] in _NEEDS_OWNER_CLASSES
    who = "твоего решения" if owner else "решения (политика или координатор)"
    tech = f"class={label['class']} rec={label['rec']} red={'yes' if label['red'] else 'no'}"
    details = tech + (f" · {_clip(label['question'], 160)}" if label["question"] else "")
    return (f"⏸ {wave} ждёт {who}: {why}", details, OWNER_RED if owner else POLICY_YELLOW)


def _pulse_phrase(m):
    wave, status = m["w"], m["status"]
    if status.startswith("BLOCKED:"):
        return _blocked_phrase(wave, status[len("BLOCKED:"):])
    word = {"RUNNING": "работает", "STARTING": "стартует", "RESUMING": "поднимается после /clear",
            "HANDOFF_READY": "готов handoff", "DONE": "закончила работу"}.get(status, wab._head(status, 40))
    where = f", фаза {m['phase']}" if m["phase"] not in ("running", "") else ""
    return (f"⚙ {wave} {word}{where}", f"ctx={m['ctx']} restarts={m['restarts']}", INFO_GREY)


def _wave_rules():
    """(regex, builder) for lines `Wn: <text>`; the builder gets the match and returns (phrase, details, colour)."""
    w = lambda rx: re.compile(_WAVE + r": " + rx, re.S)  # noqa: E731
    return [
        (w(r"merge gate passed, merge of PR #(?P<n>\d+) requested at (?P<sha>\S+)"),
         lambda m: (f"✅ {m['w']}: PR #{m['n']} отправлен в мердж", f"sha {m['sha']}", OK_GREEN)),
        (w(r"merge gate passed with accepted limitations: (?P<t>.*)"),
         lambda m: (f"✅ {m['w']}: гейт мерджа пройден с принятыми оговорками", m["t"], OK_GREEN)),
        (w(r"merge gate passed with (?P<k>\d+) open threads(?P<t>.*)"),
         lambda m: (f"⏸ {m['w']}: гейт пройден, но {m['k']} открытых тредов — нужен ты (скрипт владельца)", m["t"].lstrip("; "), OWNER_RED)),
        (w(r"merge gate passed, (?P<t>.*)"),
         lambda m: (f"⏸ {m['w']}: гейт пройден, мердж за тобой", m["t"], OWNER_RED)),
        (w(r"merge gate waits: (?P<t>.*)"),
         lambda m: (f"⏳ {m['w']}: гейт мерджа ждёт", m["t"], WARN_YELLOW)),
        (w(r"merge gate failed: (?P<t>.*)"),
         lambda m: (f"❌ {m['w']}: гейт мерджа не пройден", m["t"], OWNER_RED)),
        (w(r"PR #(?P<n>\d+) MERGED(?P<t>.*)"),
         lambda m: (f"🎉 {m['w']}: PR #{m['n']} смержен", m["t"].lstrip(", "), OK_GREEN)),
        (w(r"PR #(?P<n>\d+) merge result unknown; handed to the owner: (?P<t>.*)"),
         lambda m: (f"⏸ {m['w']}: итог мерджа PR #{m['n']} неизвестен, нужен ты", m["t"], OWNER_RED)),
        (w(r"owner-handover: PR #(?P<n>\d+) смержен владельцем(?P<t>.*)"),
         lambda m: (f"🎉 {m['w']}: PR #{m['n']} смержен тобой", m["t"].strip(" ()"), OK_GREEN)),
        (w(r"alarm: PR #(?P<n>\d+) checks completed and Codex finished at (?P<sha>\S+)"),
         lambda m: (f"🔔 {m['w']}: PR #{m['n']} — проверки и ревью Codex завершены", f"head {m['sha']}", INFO_GREY)),
        (w(r"alarm: PR facts not collected: (?P<t>.*)"),
         lambda m: (f"⚠ {m['w']}: не удалось собрать факты о PR", m["t"], WARN_YELLOW)),
        (w(r"context (?P<c>\d+) >= (?P<l>\d+), checkpoint requested"),
         lambda m: (f"💾 {m['w']}: контекст разросся, просим контрольную точку", f"ctx {m['c']} из {m['l']}", WARN_YELLOW)),
        (w(r"handoff ready, (?P<t>.*?) \(restart #(?P<n>\d+)\)"),
         lambda m: (f"💾 {m['w']}: handoff готов, перезапуск головы №{m['n']}", m["t"], WARN_YELLOW)),
        (w(r"new session (?P<sid>\S+) bound by marker"),
         lambda m: (f"🔗 {m['w']}: новая сессия после /clear подхвачена", None, INFO_GREY)),
        (w(r"policy auto-answer: (?P<t>.*)"),
         lambda m: (f"🤖 {m['w']}: автоответ по политике, ответ владельца не нужен", m["t"], POLICY_YELLOW)),
        (w(r"policy cap reached \((?P<u>\d+)/(?P<c>\d+)\)(?P<t>.*)"),
         lambda m: (f"⏸ {m['w']}: лимит автоответов исчерпан ({m['u']}/{m['c']}), дальше решаешь ты", m["t"].lstrip(": "), OWNER_RED)),
        (w(r"policy answer not sent: (?P<t>.*)"),
         lambda m: (f"🤖 {m['w']}: автоответ не отправлен", m["t"], INFO_GREY)),
        (w(r"launched in tmux (?P<s>\S+?),? cwd (?P<d>.*)"),
         lambda m: (f"🚀 {m['w']} запущена в окне {m['s']}", m["d"], OK_GREEN)),
        (w(r"DONE, (?P<t>.*)"),
         lambda m: (f"✔ {m['w']} готова, ждёт мерджа", m["t"], OK_GREEN)),
        (w(r"DONE\s*$"),
         lambda m: (f"✔ {m['w']} готова", None, OK_GREEN)),
        (w(r"pane idle (?P<m>\S+) min, status=(?P<s>.*)"),
         lambda m: (f"💤 {m['w']} молчит {m['m']} мин", f"status={m['s']}", OWNER_RED)),
        (w(r"idle nudge sent \((?P<t>.*)\)"),
         lambda m: (f"👉 {m['w']}: толчок молчащему окну отправлен", m["t"], WARN_YELLOW)),
        (w(r"idle nudge dropped: (?P<t>.*)"),
         lambda m: (f"👉 {m['w']}: толчок отменён, в поле ввода чужой текст", m["t"], INFO_GREY)),
        (w(r"фоновые хвосты в окне волны: (?P<t>.*)"),
         lambda m: (f"🧵 {m['w']}: в окне висят фоновые хвосты", m["t"], WARN_YELLOW)),
        (w(r"дерево процессов недоступно: (?P<t>.*)"),
         lambda m: (f"⚠ {m['w']}: дерево процессов недоступно, хвосты не проверены", m["t"], WARN_YELLOW)),
        (w(r"say: (?P<t>.*)"),
         lambda m: (f"💬 {m['w']}: в окно волны написали", m["t"], INFO_GREY)),
        (w(r"workdir (?P<d>\S+) detached on (?P<t>.*)"),
         lambda m: (f"📂 {m['w']}: рабочая копия готова", f"{m['d']} · {m['t']}", INFO_GREY)),
        (w(r"window is not in auto mode"),
         lambda m: (f"⚠ {m['w']}: окно вышло из авто-режима", None, OWNER_RED)),
        (w(r"permission prompt on screen"),
         lambda m: (f"⚠ {m['w']}: на экране запрос разрешения", None, OWNER_RED)),
        (w(r"tmux session (?P<s>\S+) is gone(?P<t>.*)"),
         lambda m: (f"✖ {m['w']}: окно {m['s']} закрылось", m["t"].strip(" ()"), OWNER_RED)),
    ]


def _plain_rules():
    p = lambda rx: re.compile(rx, re.S)  # noqa: E731
    return [
        (p(r"watch started(?P<t>.*)"), lambda m: ("👀 слежение начато", m["t"].lstrip(", "), OK_GREEN)),
        (p(r"watch stopped: (?P<t>.*)"), lambda m: ("🛑 слежение остановлено", m["t"], WARN_YELLOW)),
        (p(r"chain finished"), lambda m: ("🏁 цепочка завершена", None, OK_GREEN)),
        (p(r"chain sessions closed: (?P<t>.*)"), lambda m: ("🧹 окна tmux цепочки закрыты", m["t"], OK_GREEN)),
        (p(r"telegram FAILED (?P<t>.*)"), lambda m: ("⚠ уведомление не доставлено (telegram)", m["t"], OWNER_RED)),
        (p(r"display-message failed (?P<t>.*)"), lambda m: ("⚠ уведомление не доставлено (tmux)", m["t"], OWNER_RED)),
        (p(r"notify\(skipped\): (?P<t>.*)"), lambda m: ("📪 Telegram не настроен, уведомление только в tmux", m["t"], WARN_YELLOW)),
        (p(r"telegram: (?P<t>.*)"), lambda m: ("📨 уведомление отправлено", m["t"], INFO_GREY)),
        (p(r"Ctrl\+\\ (?P<t>.*)"), lambda m: ("⌨ клавиша Ctrl+\\ — окно волны поверх дашборда", m["t"], INFO_GREY)),
        (p(r"admission: (?P<t>.*)"), lambda m: ("🛂 клон репозитория: допуск и выбор копии", m["t"], INFO_GREY)),
    ]


_WAVE_RULES, _PLAIN_RULES = _wave_rules(), _plain_rules()


def _safe(text):
    """Everything of the wave or the manifest that the dashboard shows: wab.safe_text, nothing else (#81)."""
    return wab.safe_text(text, 400, owner_paths=False)


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1: ESC/CSI/OSC would reach the terminal


def humanize_event(msg):
    """One events.log message (without the time) -> (phrase, details, colour, known). `known` is False
    for a type nobody described: the phrase is then the raw line cut to ~140 characters. Phrase and
    details are masked with wab.safe_text; the raw line is not changed in events.log."""
    # Masking comes FIRST, over the WHOLE line, the way notices are masked: a token that holds an invisible or
    # control character is masked whole (it may glue or split a secret). Every cut below (phrases, details,
    # the raw line) then works on masked text, so a limit cannot split a secret into a piece redact() misses.
    msg = wab.safe_text(msg, 10 ** 7, owner_paths=False)  # a lone \r in a word is handled by safe_text itself
    msg = _CONTROL.sub("·", msg).strip()  # what is left (a tab, a line break): a visible sign for the terminal
    result = None
    m = _PULSE.fullmatch(msg)
    if m:
        result = _pulse_phrase(m)
    else:
        m = _BLOCKED_ANY.fullmatch(msg)
        if m:
            result = _blocked_phrase(m["w"], m["rest"])
        else:
            for rx, build in _WAVE_RULES + _PLAIN_RULES:
                m = rx.fullmatch(msg)
                if m:
                    result = build(m)
                    break
    if result is None:
        return (wab._head(_safe(msg), 140), None, INFO_GREY, False)
    phrase, details, colour = result
    return (_safe(phrase), _clip(_safe(details), 160) if details else None, colour, True)


def _pulse_key(msg):
    m = _PULSE.fullmatch(msg.strip())
    return (m["w"], m["phase"], m["status"], m["restarts"]) if m else None


def fold_events(lines):
    """Raw events.log lines -> [(time `HH:MM:SS`, phrase, details, colour)]. Consecutive pulses that keep
    the wave, phase, status and restarts fold into one «без изменений с HH:MM (×N)» (ctx may grow); any
    other event, or a changed pulse, ends the series. A line without a time is shown as it is."""
    items, series = [], None  # series: [key, first_hhmm, count, last_ts, last message]

    def flush():
        nonlocal series
        if series is None:
            return
        key, first, count, ts, last = series
        phrase, details, colour, _ = humanize_event(last)
        if count > 1:
            phrase = f"{phrase} · без изменений с {first} (×{count})"
        items.append((ts, phrase, details, colour))
        series = None

    for line in lines:
        if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ ", line):  # `YYYY-MM-DD HH:MM:SSZ msg`
            ts, msg = line[11:19], line[20:].lstrip()
        else:
            ts, msg = "", line
        key = _pulse_key(msg)
        if key is not None:
            if series is not None and series[0] == key:
                series[2] += 1
                series[3] = ts
                series[4] = msg  # the state shown is the LAST pulse's, the time is the first's
                continue
            flush()
            series = [key, ts[:5], 1, ts, msg]
            continue
        flush()
        phrase, details, colour, _ = humanize_event(msg)
        items.append((ts, phrase, details, colour))
    flush()
    return items


EVENTS_READ_LIMIT = 4 * 1024 * 1024  # bytes of events.log read from the end at most


def tail_items(path, n, limit=EVENTS_READ_LIMIT, block=64 * 1024):
    """The last `n` folded items of events.log. The file is read from the END in growing blocks until the
    fold gives n+1 items (so the oldest one shown is a whole series, however many pulses it holds), the
    start of the file, or `limit` bytes: then what there is is shown. Never reads the whole of a huge file.
    A record ends at b"\\n" only (and the \\r of a CRLF): str.splitlines would also cut at U+2028/2029, NEL, VT, FF,
    FS/GS/RS and split a secret in two before it is masked. Bytes are split first, so a block border in the
    middle of a UTF-8 character costs only the (dropped) cut first line."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            while True:
                want = min(block, limit, size)
                f.seek(size - want)
                chunks = f.read(want).split(b"\n")
                if want < size:
                    chunks = chunks[1:]  # the first line of a block is cut: drop it
                lines = [c.decode("utf-8", errors="replace").removesuffix("\r") for c in chunks]
                items = fold_events([l for l in lines if l.strip()])
                if len(items) > n or want >= size or want >= limit:
                    return items[-n:]
                block *= 4
    except OSError:
        return []


def events_panel(cfg, n=12):
    t = Text()
    for ts, phrase, details, colour in tail_items(cfg["run_dir"] / "events.log", n):
        t.append((ts + " ") if ts else "", style="grey50")
        t.append(phrase + "\n", style=colour)
        if details:
            t.append("         " + details + "\n", style="grey50")
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
        return Panel(Text(f"кадр не отрисован: {type(e).__name__}: {_safe(e)}\nповтор через 3 с",
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
