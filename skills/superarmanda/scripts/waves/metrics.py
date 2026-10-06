#!/usr/bin/env python3
"""Замер цепочки волн (#86): прогоны, фикс-круги и развилки blocked_cap по волнам каталога прогона.

  metrics.py <run_dir> [--json]

<run_dir> — каталог прогона цепочки (`runs/<цепочка>/<run_id>`). Читается только он, ничего не пишется:
  * `W*/runs.json` — прогоны волны (state.py `init --from-plan`); у каждого свой manifest. Файл прогона ищется
    сначала среди `W*/superarmanda/manifest*.json` по `run.index` внутри (файлы переименовывают: так в живых
    каталогах manifest.json держит второй прогон), затем по пути из runs.json, затем по его имени;
  * без `runs.json` — один прогон по `W*/superarmanda/manifest.json` (цепочки до 1.2.0 его не вели);
  * `events.log` — развилки blocked_cap.

По волне:
  * прогонов  — число прогонов (manifest);
  * фикс-круги — сумма `tasks.<id>.fix_cycles` по всем manifest волны;
  * blocked_cap — число развилок. Одна развилка — один эпизод: строки `<волна>: BLOCKED: [class=blocked_cap …`
    журнала; подряд идущая такая же строка той же волны (повтор той же записи) не считается.

Отсутствующий или испорченный файл молча не пропускается: волна получает пометку «нет данных» с причиной
(в таблице — под ней, в `--json` — поле `problems`), числа, которые не из чего посчитать, — `null`, в итог они
не входят, а код выхода 1. Нет каталога или в нём нет ни одной волны — код 2. Всё прочитано — код 0.
Только стандартная библиотека.
"""

import argparse
import json
import re
import sys
from pathlib import Path

WAVE_DIR = re.compile(r"W(\d+)\Z")
BLOCKED_CAP = re.compile(r"^\S+ \S+ (W\d+): BLOCKED: (\[class=blocked_cap\b.*)$")
NO_DATA = "нет данных"


def _load(path):
    """(объект JSON, None) или (None, причина) — никогда не бросает."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"нет файла {path.name}"
    except (OSError, ValueError) as e:
        return None, f"{path.name} не читается ({type(e).__name__})"


def _manifest_paths(wave_dir, problems):
    """Файлы manifest волны: по runs.json, а без него — manifest.json. [] с причиной в `problems`, если не из чего."""
    runs_file = wave_dir / "runs.json"
    if not runs_file.exists():
        fallback = wave_dir / "superarmanda" / "manifest.json"
        return [fallback] if fallback.exists() else _fail(problems, "нет ни runs.json, ни superarmanda/manifest.json")
    data, why = _load(runs_file)
    runs = data.get("runs") if isinstance(data, dict) else None
    if why or not isinstance(runs, list) or not runs or not all(isinstance(r, dict) for r in runs):
        return _fail(problems, why or "runs.json не содержит списка прогонов")
    by_index = _by_index(wave_dir)
    found = []
    for r in runs:
        raw = r.get("manifest")
        path = by_index.get(r.get("index"))  # файл, который сам называет себя этим прогоном, — надёжнее имени
        if path is None and isinstance(raw, str) and raw:
            path = Path(raw)
            if not path.exists():
                path = wave_dir / "superarmanda" / path.name  # каталог прогона перенесли: то же имя рядом
        if path is None or not path.exists():
            problems.append(f"manifest прогона {r.get('index')} не найден")
            found.append(None)
            continue
        found.append(path)
    return found


def _by_index(wave_dir):
    """{номер прогона: файл} по `run.index` внутри `superarmanda/manifest*.json` (файлы переименовывают)."""
    out = {}
    for path in sorted((wave_dir / "superarmanda").glob("manifest*.json")):
        data, why = _load(path)
        run = data.get("run") if isinstance(data, dict) else None
        index = run.get("index") if isinstance(run, dict) else None
        if not why and isinstance(index, int) and not isinstance(index, bool):
            out.setdefault(index, path)
    return out


def _fail(problems, why):
    problems.append(why)
    return []


def _fix_cycles(path, problems):
    if path is None:
        return None
    data, why = _load(path)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if why or not isinstance(tasks, dict):
        problems.append(why or f"{path.name}: нет tasks")
        return None
    total = 0
    for name, entry in tasks.items():
        cycles = entry.get("fix_cycles") if isinstance(entry, dict) else None
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            problems.append(f"{path.name}: fix_cycles задачи {name} не число")
            return None
        total += cycles
    return total


def blocked_cap_by_wave(events):
    """{волна: число развилок} по тексту events.log; одна развилка — один эпизод (подряд повтор строки не считается)."""
    counts, last = {}, {}
    for line in events.splitlines():
        m = BLOCKED_CAP.match(line)
        if not m:
            continue
        wave, text = m.groups()
        if last.get(wave) != text:
            counts[wave] = counts.get(wave, 0) + 1
        last[wave] = text
    return counts


def measure(run_dir):
    run_dir = Path(run_dir)
    waves = sorted((d for d in run_dir.iterdir() if d.is_dir() and WAVE_DIR.match(d.name)),
                   key=lambda d: int(WAVE_DIR.match(d.name).group(1))) if run_dir.is_dir() else []
    events_file = run_dir / "events.log"
    try:
        forks = blocked_cap_by_wave(events_file.read_text(encoding="utf-8"))
        forks_problem = None
    except (OSError, UnicodeDecodeError) as e:
        forks, forks_problem = None, f"events.log не читается ({type(e).__name__})"
    rows = []
    for d in waves:
        problems = []
        manifests = _manifest_paths(d, problems)
        cycles = [_fix_cycles(p, problems) for p in manifests]
        rows.append({
            "wave": d.name,
            "runs": len(manifests) if manifests else None,  # прогон с потерянным manifest считается: он был
            "fix_cycles": sum(cycles) if cycles and None not in cycles else None,
            "blocked_cap": None if forks is None else forks.get(d.name, 0),
            "problems": problems + ([forks_problem] if forks_problem else [])})
    ok = bool(rows) and not any(r["problems"] for r in rows)
    total = {k: sum(r[k] for r in rows if r[k] is not None) for k in ("runs", "fix_cycles", "blocked_cap")}
    return {"run_dir": str(run_dir), "waves": rows, "total": total, "ok": ok}


def table(result):
    cell = lambda v: NO_DATA if v is None else str(v)  # noqa: E731
    lines = ["| Волна | Прогонов | Фикс-круги | Развилки blocked_cap |", "|---|---|---|---|"]
    for r in result["waves"]:
        lines.append(f"| {r['wave']} | {cell(r['runs'])} | {cell(r['fix_cycles'])} | {cell(r['blocked_cap'])} |")
    t = result["total"]
    lines.append(f"| **Итого** | {t['runs']} | {t['fix_cycles']} | {t['blocked_cap']} |")
    for r in result["waves"]:
        lines += [f"{r['wave']}: {NO_DATA} — {p}" for p in r["problems"]]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="замер цепочки волн: прогоны, фикс-круги, развилки blocked_cap")
    ap.add_argument("run_dir", help="каталог прогона цепочки (runs/<цепочка>/<run_id>)")
    ap.add_argument("--json", action="store_true", help="вывод для машины вместо таблицы")
    args = ap.parse_args(argv)
    result = measure(args.run_dir)
    if not result["waves"]:
        print(f"metrics.py: в {args.run_dir} нет каталога прогона с волнами W*", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else table(result))
    if not result["ok"]:
        print("metrics.py: часть данных не прочитана (см. «нет данных»), код выхода 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
