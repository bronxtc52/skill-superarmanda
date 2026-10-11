#!/usr/bin/env python3
"""Замер цепочки волн (#86): прогоны, фикс-круги и развилки blocked_cap по волнам каталога прогона.

  metrics.py <run_dir> [--json]

Инвариант: любое значение из runs.json/manifest/events.log проверяется до использования; файл вне каталога
волны не читается; любой испорченный ввод (в том числе JSON глубокой вложенности) → problems, код 1,
никогда traceback.

<run_dir> — каталог прогона цепочки (`runs/<цепочка>/<run_id>`). Читается только он, ничего не пишется:
  * `W*/runs.json` — прогоны волны (state.py `init --from-plan`); у каждого свой manifest. runs.json проверяется
    как в `state.read_runs`: версия — целое 1, `wave` = имя каталога волны, `index` каждого прогона — положительное
    целое, номера не повторяются, сам runs.json лежит внутри каталога волны, иначе волна «нет данных». Файл прогона ищется сначала среди `W*/superarmanda/manifest*.json` по `run.index`
    внутри (файлы переименовывают: так в живых каталогах manifest.json держит второй прогон), затем по пути из
    runs.json, затем по его имени рядом; путь принимается только внутри каталога волны (после разрешения
    симлинков), ведущий наружу заменяется именем файла в `superarmanda/`; файл, называющий себя другим
    прогоном, не подставляется; manifest, чей `run.index` не равен номеру прогона из runs.json
    (или испорчен), — «нет данных», manifest без `run` (старый формат) засчитывается;
  * без `runs.json` — один прогон по `W*/superarmanda/manifest.json` (цепочки до 1.2.0 его не вели);
  * `events.log` — развилки blocked_cap.

По волне:
  * прогонов  — число прогонов (manifest);
  * фикс-круги — сумма `tasks.<id>.fix_cycles` по всем manifest волны;
  * blocked_cap — число развилок. Одна развилка — один эпизод: непрерывный период, когда текущий статус волны (строки `BLOCKED…`,
    `DONE`/`RUNNING`, `phase=… status=`, ответ политики) — одна и та же строка `BLOCKED: [class=blocked_cap …`;
    повтор той же строки без смены статуса не считается, тот же текст после иного статуса — новая развилка;
    успешный `say:` — ответ на развилку и закрывает эпизод.

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
STATUS_LINE = re.compile(r"^\S+ \S+ (W\d+): (.*)$")
PHASE_STATUS = re.compile(r"phase=\S+ .*?\bstatus=(.*)$")
BARE_STATUS = re.compile(r"[A-Z][A-Z_]*\Z")
BLOCKED_CAP = re.compile(r"BLOCKED: \[class=blocked_cap\b")
RUNS_FILE_VERSION = 1  # как state.RUNS_FILE_VERSION
SAY_SERVICE = ("say: owner-answered marker not written", "say: status rewritten while the marker")
NO_DATA = "нет данных"


def _load(path):
    """(объект JSON, None) или (None, причина) — никогда не бросает."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"нет файла {path.name}"
    except RecursionError:
        return None, f"{path.name} не читается (слишком глубокая вложенность)"
    except (OSError, ValueError) as e:
        return None, f"{path.name} не читается ({type(e).__name__})"


def _inside(path, wave_dir):
    """Файл лежит внутри каталога волны ПОСЛЕ разрешения симлинков (чужой файл не читается никогда)."""
    try:
        return path.resolve().is_relative_to(wave_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _is_index(value):
    """Номер прогона — положительное целое, не bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _runs_of(wave_dir, runs_file):
    """(список прогонов, None) или (None, причина): те же проверки, что у state.read_runs."""
    if not _inside(runs_file, wave_dir):
        return None, "runs.json ведёт вне каталога волны"
    data, why = _load(runs_file)
    if why:
        return None, why
    if (not isinstance(data, dict) or not _is_index(data.get("version")) or data["version"] != RUNS_FILE_VERSION or not isinstance(data.get("wave"), str)
            or not isinstance(data.get("runs"), list) or not all(isinstance(r, dict) for r in data["runs"])):
        return None, "runs.json неподдерживаемого формата"
    if data["wave"] != wave_dir.name:
        return None, f"runs.json принадлежит волне {data['wave']}, а не {wave_dir.name}"
    if not data["runs"]:
        return None, "runs.json не содержит списка прогонов"
    return data["runs"], None


def _manifest_paths(wave_dir, problems):
    """Файлы manifest волны: по runs.json, а без него — manifest.json. [] с причиной в `problems`, если не из чего."""
    runs_file = wave_dir / "runs.json"
    if not runs_file.exists():
        fallback = wave_dir / "superarmanda" / "manifest.json"
        if fallback.is_file() and _inside(fallback, wave_dir):
            return [(fallback, None)]
        return _fail(problems, "нет ни runs.json, ни superarmanda/manifest.json")
    runs, why = _runs_of(wave_dir, runs_file)
    if why:
        return _fail(problems, why)
    indices = [r.get("index") for r in runs if _is_index(r.get("index"))]
    if len(indices) != len(set(indices)):
        return _fail(problems, "в runs.json повторяется номер прогона")
    by_index = _by_index(wave_dir)
    claimed = {p.resolve(): i for i, p in by_index.items()}
    found = []
    for r in runs:
        index, raw = r.get("index"), r.get("manifest")
        if not _is_index(index):
            problems.append(f"в runs.json у прогона неверный номер (тип {type(index).__name__})")
            found.append((None, index))
            continue
        path = by_index.get(index)  # файл, который сам называет себя этим прогоном, — надёжнее имени
        if path is None and isinstance(raw, str) and raw and "\0" not in raw:
            path = Path(raw)
            if not path.is_absolute():
                path = wave_dir / path
            if not (_inside(path, wave_dir) and path.is_file()):
                # пути нет или он ведёт вне каталога волны (каталог перенесли/скопировали): то же имя рядом
                path = wave_dir / "superarmanda" / Path(raw).name
            if not _inside(path, wave_dir) or claimed.get(path.resolve(), index) != index:
                path = None  # чужой файл или файл называет себя другим прогоном: не подставлять
        if path is None or not path.is_file():
            problems.append(f"manifest прогона {index} не найден")
            found.append((None, index))
            continue
        found.append((path, index))
    return found


def _by_index(wave_dir):
    """{номер прогона: файл} по `run.index` внутри `superarmanda/manifest*.json` (файлы переименовывают).
    Файлы, ведущие вне каталога волны (симлинк наружу), не читаются."""
    out = {}
    for path in sorted((wave_dir / "superarmanda").glob("manifest*.json")):
        if not _inside(path, wave_dir):
            continue
        data, why = _load(path)
        run = data.get("run") if isinstance(data, dict) else None
        index = run.get("index") if isinstance(run, dict) else None
        if not why and _is_index(index):
            out.setdefault(index, path)
    return out


def _fail(problems, why):
    problems.append(why)
    return []


def _fix_cycles(path, problems, index=None):
    """Фикс-круги manifest прогона `index` (None — прогон без runs.json): заявленный в файле номер обязан совпасть."""
    if path is None:
        return None
    data, why = _load(path)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if why or not isinstance(tasks, dict):
        problems.append(why or f"{path.name}: нет tasks")
        return None
    if "run" in data:  # без `run` — manifest старого формата; заявленный номер — испорченный или чужой ввод
        declared = data["run"].get("index") if isinstance(data["run"], dict) else None
        if not _is_index(declared) or (index is not None and declared != index):
            problems.append(f"{path.name}: номер прогона в run не {index if index is not None else 'положительное целое'}")
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
    """{волна: число развилок} по тексту events.log.

    Эпизод — непрерывный период, когда ТЕКУЩИЙ статус волны — именно эта строка blocked_cap. Статус волны меняют
    строки `<волна>: BLOCKED: …`, `<волна>: DONE|RUNNING|…`, `phase=… status=<…>`, ответ политики, успешный
    `say: …` (но не `say FAILED` и не служебные строки say о маркере) и запуск окна;
    остальные (alarm, merge gate…) его не сообщают и эпизод не рвут. Строки других волн не влияют."""
    counts, current = {}, {}
    for line in events.splitlines():
        m = STATUS_LINE.match(line)
        if not m:
            continue
        wave, rest = m.groups()
        phase = PHASE_STATUS.match(rest)
        if phase:
            status = phase.group(1)
            prev = current.get(wave)
            if prev and BLOCKED_CAP.match(prev) and (prev.startswith(status) or status.startswith(prev)):
                continue  # phase= несёт обрезанный текст той же строки: тот же статус
        elif (rest.startswith(("policy auto-answer:", "launched ")) or rest.startswith("say: ")
              and not rest.startswith(SAY_SERVICE)):
            status = None  # развилка закрыта ответом (политики или владельца: успешный say) / окно запущено заново
        elif rest.startswith("BLOCKED: ") or BARE_STATUS.match(rest):
            status = rest
        else:
            continue
        if status is not None and BLOCKED_CAP.match(status) and current.get(wave) != status:
            counts[wave] = counts.get(wave, 0) + 1
        current[wave] = status
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
        cycles = [_fix_cycles(p, problems, i) for p, i in manifests]
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
