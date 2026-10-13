#!/usr/bin/env python3
"""Замер цепочки волн (#86): прогоны, фикс-круги и развилки blocked_cap по волнам каталога прогона.

  metrics.py <run_dir> [--json]

Инвариант: любое значение из runs.json/manifest/events.log проверяется до использования; файл вне каталога
волны не читается; любой испорченный ввод (в том числе JSON глубокой вложенности) → problems, код 1,
никогда traceback.

Инвариант идентичности: каждый файл, который читается (`runs.json` волны, каждый `superarmanda/manifest*.json` —
и засчитанный, и просмотренный при поиске по `run.index`, — `events.log`), открывается ОДНОЙ функцией
`_read_checked` (JSON — через `_read_json` → `_runs_of` для runs.json и `_manifest_checked` для manifest в
`_homes`, `_by_index`, `_legacy_claims` и `_fix_cycles`; `events.log` — прямо из `measure`) и читается из того же
дескриптора, который проверен:
  * запись каталога есть (`lstat`: битый симлинк — присутствующий, но нечитаемый файл, а не «нет файла», и
    fallback на manifest.json при нём не срабатывает); путь разрешается один раз, результат лежит в каталоге
    волны (events.log — прогона) и открывается именно он с O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC; открытый дескриптор
    обязан быть ровно разрешённым путём (Linux — путь `/proc/self/fd` равен ему, иначе — st_dev/st_ino, см.
    `_read_checked`: без /proc промежуточные каталоги не защищены); fstat того же дескриптора — только обычный файл не больше
    `MAX_JSON_BYTES` (`MAX_LOG_BYTES` для events.log; FIFO, каталог, устройство, огромный файл — отказ без
    зависания), и читается тот же дескриптор. Любой OSError (нет доступа, битый симлинк) — причина, не traceback;
  * формат и идентичность: runs.json — `version` 1, `wave` = имя каталога волны, `runs` — список; manifest —
    `version` целое 1 или 2 (не bool, не 1.0, не строка; как state.check_schema), а если есть ключ `wave` —
    это объект с `id`, равным имени каталога волны (`W4`). Manifest без `wave` и без `run` (цепочки до 1.2.0)
    читается как раньше;
  * `run.index` однозначен: два РАЗНЫХ файла (`st_dev`, `st_ino`) волны с одним номером — отказ, а не первый по
    имени; два пути (симлинки внутри волны) на один файл — один прогон. Файл, не прошедший проверку при поиске по
    номеру (чужая волна, испорченный, FIFO), — отказ с его причиной, а не пропуск и не «дубль».
  * один физический файл и один каталог волны — см. инвариант прогонов I1–I4 ниже;
  * граница чтения одна — каталог прогона: волна (`W*`, в том числе симлинк) засчитывается, только если после
    разрешения симлинков лежит внутри него; симлинк волны внутри прогона допустим, наружу (чужая цепочка) — отказ.
    Корень волны разрешается ОДИН раз (`_wave_roots`), и всё дальше читается от него: подмена симлинка волны после
    проверки границы чтение не уводит; имя волны для сверки `wave`/`runs.json` — имя записи `W*`.
Нарушение — строка в `problems` с причиной, волна «нет данных», код 1, без traceback.

Инвариант прогонов (W4 #113, класс целиком; проверяется перебором в MetricsScript):
  I1. Каждый физический manifest (`st_dev`, `st_ino`) даёт не больше одного прогона во всём каталоге прогона, и
      только если прошёл проверку (`_read_checked` + `_manifest_fields` + номер `run` + tasks/fix_cycles в
      `_fix_cycles`). Отвергнутый файл не даёт прогона ни в одной ветке (runs.json / легаси с `wave` / легаси без
      `wave`): волна с ним — «нет данных» и по прогонам, а не «прогонов 1, фикс-круги нет данных». Единственная
      оговорка — прогон из runs.json, у которого файла НЕТ (`manifest прогона N не найден`): прогон был, его
      записал state.py до manifest, считается в runs, фикс-круги «нет данных». I1 это не нарушает: у потерянного
      прогона нет inode, повторить его нечем. Файл-самозванец (есть, но называет себя другим прогоном, чужой волны,
      лежит в другой волне) не потерян — он отвергнут, и прогона не даёт.
  I2. total.runs = сумма runs волн (волна «нет данных» не входит), и total.runs ≤ число различных физических
      проверенных manifest + число потерянных; ни один inode не участвует в runs двух волн: у каждого физического
      manifest один дом (`_homes`) — первая по номеру волна, где он лежит внутри корня и прошёл проверку (иначе —
      первая, где лежит); любое его появление в другой волне — отказ до чтения (`_foreign`), внутри волны —
      `counted` (ключ дескриптора регистрируется ДО сверки полей).
  I3. Корень волны: каталог-не-симлинк имеет приоритет перед симлинком на него, независимо от порядка номеров
      (`_wave_roots`): W1 -> W2 засчитывает W2, а W1 — «тот же, что у W2», её файлы не читаются; из нескольких
      симлинков на один каталог — первый по номеру. Так же для файла: симлинк наружу волны в manifest другой волны
      не дом, засчитывается настоящий файл.
  I4. Одна причина на один факт: повтор физического файла (тот же путь, другая запись пути, симлинк файла или
      каталога `superarmanda`, hardlink, `cp -al`, путь runs.json в другую волну) — всегда «тот же файл, что у …»
      в любой ветке, а не «вне каталога волны», «принадлежит волне», «заявлен двумя файлами» или «не найден»;
      повтор каталога волны — «тот же, что у Wn».

<run_dir> — каталог прогона цепочки (`runs/<цепочка>/<run_id>`). Читается только он, ничего не пишется:
  * `W*/runs.json` — прогоны волны (state.py `init --from-plan`); у каждого свой manifest. runs.json проверяется
    как в `state.read_runs`: версия — целое 1, `wave` = имя каталога волны, `index` каждого прогона — положительное
    целое, номера не повторяются, сам runs.json лежит внутри каталога волны, иначе волна «нет данных». Файл прогона ищется сначала среди `W*/superarmanda/manifest*.json` по `run.index`
    внутри (файлы переименовывают: так в живых каталогах manifest.json держит второй прогон), затем по пути из
    runs.json, затем по его имени рядом. Запись пути, которая ЕСТЬ и чей каталог (после разрешения симлинков) лежит
    внутри волны, читается как есть — любого вида: обычный файл с любым именем, битый симлинк, FIFO, каталог, симлинк
    наружу; не прошла `_read_checked` — отвергнута (runs «нет данных», I1), а не подменена именем. Имя файла в
    `superarmanda/` подставляется, только если записи нет или её каталог вне волны (каталог перенесли/скопировали);
    нет и подстановки — «manifest прогона N не найден» (потерянный прогон, оговорка I1); файл, называющий себя другим
    прогоном, — тот же физический файл у двух прогонов: отказ «тот же файл», а не подстановка; manifest, чей `run.index` не равен номеру прогона из runs.json
    (или испорчен), — «нет данных», manifest без `run` (старый формат) засчитывается;
  * без `runs.json` — один прогон по `W*/superarmanda/manifest.json` (цепочки до 1.2.0 его не вели); другой
    физический файл волны, ВАЛИДНО заявляющий `run.index`, — отказ, а не молчаливый пропуск; посторонний
    `manifest*.json` (не читается, не разбирается, чужой волны, без `run`) не мешает, как в 1.5.0; поиск по номеру
    (`_by_index`) — только при `runs.json`, и там отказ, если `run.index` файла в `runs.json` нет;
  * запись `W*`, которая не каталог (висячий симлинк, петля, файл), — волна «нет данных», а не исчезает из таблицы;
  * `events.log` — развилки blocked_cap.

По волне:
  * прогонов  — число прогонов (manifest); волна с отвергнутым или повторённым физическим manifest — «нет
    данных» (I1);
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
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path

WAVE_DIR = re.compile(r"W(\d+)\Z")
STATUS_LINE = re.compile(r"^\S+ \S+ (W\d+): (.*)$")
PHASE_STATUS = re.compile(r"phase=\S+ .*?\bstatus=(.*)$")
BARE_STATUS = re.compile(r"[A-Z][A-Z_]*\Z")
BLOCKED_CAP = re.compile(r"BLOCKED: \[class=blocked_cap\b")
RUNS_FILE_VERSION = 1  # как state.RUNS_FILE_VERSION
MANIFEST_VERSIONS = (1, 2)  # как state.MANIFEST_VERSIONS
# служебные строки say пишутся видом `say-note:` (wab.SAY_NOTE) и под `say: ` не попадают; эти легаси-префиксы —
# только для распознавания строк журналов, записанных до 1.6.0
SAY_SERVICE = ("say: owner-answered marker not written", "say: status rewritten while the marker")
NO_DATA = "нет данных"
MAX_JSON_BYTES = 16 << 20  # runs.json и manifest: живые — килобайты; больше — испорченный ввод, не чтение в память
MAX_LOG_BYTES = 64 << 20  # events.log длинной цепочки — мегабайты; больше — отказ, а не MemoryError
DELETED_SUFFIX = " (deleted)"  # пометка ядра в конце /proc/self/fd/<fd> у удалённого файла
MAX_FIX_CYCLES = 1000  # кап state.py — 3 круга (+ решения владельца); больше — испорченный ввод, не печатаемое число


def _real(path):
    """Путь после разрешения симлинков (`os.path.realpath`: петля и нет доступа не бросают) или None."""
    try:
        return Path(os.path.realpath(path))
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _within(real, root):
    """Уже разрешённый путь `real` лежит внутри разрешённого `root`."""
    base = _real(root)
    return real is not None and base is not None and (real == base or base in real.parents)


def _inside(path, wave_dir):
    """Файл лежит внутри каталога волны ПОСЛЕ разрешения симлинков (чужой файл не читается никогда)."""
    return _within(_real(path), wave_dir)


def _key(path):
    """(st_dev, st_ino) файла или каталога по пути (с переходом по симлинкам) или None — только для сверки
    идентичности, не для чтения (читается всё через `_read_checked`)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _same_file(name, whose):
    """Единая причина «один физический файл у двух прогонов» — и в `_manifest_paths`, и в `_fix_cycles`."""
    return f"{name}: тот же файл, что у {whose}; один файл не может быть двумя прогонами"


def _whose(home):
    """Подпись владельца физического файла для причины «тот же файл»: (волна, номер прогона или None)."""
    wave, run = home
    return f"прогона {run} волны {wave}" if run is not None else f"волны {wave}"


def _foreign(path, wave, homes):
    """Причина «тот же файл», если физический файл по пути (с переходом по симлинкам: только stat, не чтение) —
    manifest ДРУГОЙ волны прогона (I1/I4): его дом (`_homes`) — другая волна. Иначе None. Проверяется ДО чтения и
    до сверки полей, поэтому у повтора одна причина в любой ветке, а не «вне каталога волны»/«принадлежит волне»."""
    home = homes.get(_key(path)) if homes else None
    if home is not None and home[0] != wave:
        return _same_file(path.name, _whose(home))
    return None


def _entry_in(path, wave_dir):
    """Запись пути есть (`lexists`, без перехода по симлинку: битый симлинк — тоже запись), и её каталог после
    разрешения симлинков лежит внутри волны. Такую запись читают как есть, а не подменяют именем (I1)."""
    return os.path.lexists(path) and _inside(path.parent, wave_dir)


def _is_kind(path, kind=stat.S_ISREG):
    """Путь (с переходом по симлинкам) — обычный файл (`kind` — каталог и т. п.); любой OSError — «нет», а не
    исключение (Path.is_file/is_dir бросают PermissionError)."""
    try:
        return kind(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _os_reason(name, e, link):
    """Причина отказа по OSError: нет файла, битый симлинк, нет доступа или имя ошибки."""
    if e.errno in (errno.ENOENT, errno.ENOTDIR):
        return f"{name} не читается (битый симлинк)" if link else f"нет файла {name}"
    if e.errno in (errno.EACCES, errno.EPERM):
        return f"{name} не читается (нет доступа)"
    return f"{name} не читается ({type(e).__name__})"


def _opened_is(fd, real):
    """Открытый дескриптор — ровно файл `real` (уже разрешённый и проверенный внутри каталога волны), а не любой
    файл волны. Linux: путь `/proc/self/fd/<fd>` (его ведёт ядро, подмена любого компонента пути до open видна)
    равен `real`. Без /proc (macOS): (st_dev, st_ino) дескриптора совпадают с повторным stat `real` — это ловит
    подмену самого файла, но не промежуточного каталога, вернувшегося на место до stat."""
    try:
        opened = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        opened = None
    if opened is not None:
        # сначала путь как есть: имя самого файла может оканчиваться на « (deleted)»; только при несовпадении хвост —
        # пометка ядра (её ядро дописывает только в конец; в середине — имя каталога)
        return Path(opened) == real or (opened.endswith(DELETED_SUFFIX)
                                        and Path(opened.removesuffix(DELETED_SUFFIX)) == real)
    try:
        st, again = os.fstat(fd), os.stat(real)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (again.st_dev, again.st_ino)


def _read_checked(path, root, limit=MAX_JSON_BYTES):
    """Единая проверка файла и чтение из того же дескриптора: (байты, (st_dev, st_ino), None) или (None, None,
    причина) — никогда не бросает (любой OSError — отказ с причиной: нет файла, битый симлинк, нет доступа) и не
    зависает. Запись каталога есть (битый симлинк — файл есть, но не читается, а не «нет файла»); путь разрешается
    ОДИН раз, результат обязан лежать внутри `root`, и открывается именно он — с O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC
    (FIFO не блокирует открытие, симлинк на месте последнего компонента — отказ). После open — сверка, что открыт
    тот самый файл (`_opened_is`); fstat того же дескриптора — только обычный файл не больше `limit` байт, и
    читается этот же дескриптор, не больше `limit` + 1 байт. На Linux подмена любого компонента пути между
    разрешением и open — отказ; без /proc (macOS) промежуточные каталоги пути не защищены от подмены тем, кто
    пишет в каталог прогона: ловится только подмена самого файла."""
    name = path.name
    try:
        link = stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError as e:
        return None, None, _os_reason(name, e, False)
    except ValueError:
        return None, None, f"{name} не читается (ValueError)"
    real = _real(path)
    if not _within(real, root):
        return None, None, f"{name} ведёт вне каталога волны"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as e:
        return None, None, _os_reason(name, e, link)
    try:
        if not _opened_is(fd, real):
            return None, None, f"{name} подменён при открытии (вне каталога волны или другой файл)"
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, None, f"{name} не читается (не файл)"
        too_big = f"{name} не читается (больше {limit >> 20} МиБ)"
        if st.st_size > limit:
            return None, None, too_big
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > limit:  # файл дописали после fstat
            return None, None, too_big
        return b"".join(chunks), (st.st_dev, st.st_ino), None
    except OSError as e:
        return None, None, _os_reason(name, e, link)
    finally:
        os.close(fd)


def _read_json(path, wave_dir):
    """(объект JSON, ключ файла, None) или (None, None, причина): `_read_checked` и разбор того же содержимого."""
    raw, key, why = _read_checked(path, wave_dir)
    if why:
        return None, None, why
    try:
        return json.loads(raw.decode("utf-8")), key, None
    except RecursionError:
        return None, None, f"{path.name} не читается (слишком глубокая вложенность)"
    except ValueError as e:
        return None, None, f"{path.name} не читается ({type(e).__name__})"


def _manifest_checked(path, wave_dir, wave):
    """(manifest, ключ файла, None) или (None, None, причина): `_read_json` и проверки `_manifest_fields`.
    `wave_dir` — уже разрешённый корень волны (граница чтения), `wave` — имя записи волны (`W4`)."""
    data, key, why = _read_json(path, wave_dir)
    if why:
        return None, None, why
    data, why = _manifest_fields(path, wave, data)
    return (None, None, why) if why else (data, key, None)


def _manifest_fields(path, wave, data):
    """(manifest, None) или (None, причина) по уже прочитанному объекту: версия 1/2 и, если есть `wave`, — эта волна."""
    if not isinstance(data, dict):
        return None, f"{path.name}: не объект manifest"
    version = data.get("version")
    if type(version) is not int or version not in MANIFEST_VERSIONS:
        return None, f"{path.name}: version не целое 1 или 2"
    if "wave" in data:
        declared = data["wave"]
        wave_id = declared.get("id") if isinstance(declared, dict) else None
        if not isinstance(wave_id, str):
            return None, f"{path.name}: wave не объект с id волны"
        if wave_id != wave:
            return None, f"{path.name} принадлежит волне {wave_id}, а не {wave}"
    return data, None


def _is_index(value):
    """Номер прогона — положительное целое, не bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _runs_of(wave_dir, runs_file, wave):
    """(список прогонов, None) или (None, причина): те же проверки, что у state.read_runs."""
    data, _, why = _read_json(runs_file, wave_dir)
    if why:
        return None, why
    if (not isinstance(data, dict) or not _is_index(data.get("version")) or data["version"] != RUNS_FILE_VERSION or not isinstance(data.get("wave"), str)
            or not isinstance(data.get("runs"), list) or not all(isinstance(r, dict) for r in data["runs"])):
        return None, "runs.json неподдерживаемого формата"
    if data["wave"] != wave:
        return None, f"runs.json принадлежит волне {data['wave']}, а не {wave}"
    if not data["runs"]:
        return None, "runs.json не содержит списка прогонов"
    return data["runs"], None


def _legacy_claims(wave_dir, wave, fallback, homes):
    """Без runs.json (волна 1.5.0): (имена других физических файлов `manifest*.json`, ВАЛИДНО заявляющих прогон
    (`run.index` — положительное целое), причина «тот же файл» или None). Файл, который не читается, не
    разбирается, чужой волны или без `run`, — посторонний и, как в 1.5.0, не мешает засчитать manifest.json;
    читается он той же `_read_checked`. Файл, физически являющийся manifest другой волны прогона (`_foreign`), —
    не посторонний, а повтор: отказ «тот же файл»."""
    _, own, _ = _read_checked(fallback, wave_dir)  # нечитаемый manifest.json назовёт _fix_cycles
    out = []
    for path in sorted((wave_dir / "superarmanda").glob("manifest*.json")):
        if foreign := _foreign(path, wave, homes):
            return out, foreign
        data, key, why = _manifest_checked(path, wave_dir, wave)
        run = data.get("run") if not why else None
        if isinstance(run, dict) and _is_index(run.get("index")) and key != own:
            out.append(path.name)
    return out, None


def _manifest_paths(wave_dir, problems, wave, homes=None):
    """Файлы manifest волны: по runs.json, а без него — manifest.json. [] с причиной в `problems`, если не из чего.
    `wave_dir` — корень волны, разрешённый ОДИН раз в `measure`; `wave` — имя записи волны; `homes` — дом каждого
    физического manifest прогона (`_homes`): файл другой волны — отказ «тот же файл» в любой ветке."""
    runs_file = wave_dir / "runs.json"
    if not os.path.lexists(runs_file):  # битый симлинк runs.json — не «нет файла»: идёт через отказ runs.json
        fallback = wave_dir / "superarmanda" / "manifest.json"
        if not os.path.lexists(fallback):
            return _fail(problems, "нет ни runs.json, ни superarmanda/manifest.json")
        # засчитывается один manifest.json; другой файл волны, валидно называющий себя прогоном, молча не выпадает
        other, foreign = _legacy_claims(wave_dir, wave, fallback, homes)
        if foreign:
            return _fail(problems, foreign)
        if other:
            return _fail(problems, "без runs.json засчитывается только manifest.json, а прогоном (run.index) "
                                   f"называет себя и {', '.join(other)}")
        return [(fallback, None)]  # идентичность проверит _fix_cycles
    by_index, keys, why = _by_index(wave_dir, wave, homes)
    if why:
        return _fail(problems, why)
    runs, why = _runs_of(wave_dir, runs_file, wave)
    if why:
        return _fail(problems, why)
    indices = [r.get("index") for r in runs if _is_index(r.get("index"))]
    if len(indices) != len(set(indices)):
        return _fail(problems, "в runs.json повторяется номер прогона")
    # state.py пишет runs.json ДО manifest: файл с номером, которого нет в runs.json, — испорченный ввод, не пропуск
    unlisted = sorted(f"{p.name} (run.index {i})" for i, p in by_index.items() if i not in indices)
    if unlisted:
        return _fail(problems, f"прогона нет в runs.json, а manifest его называет: {', '.join(unlisted)}")
    claimed = {keys[i]: i for i in by_index}  # физический файл (st_dev, st_ino), заявивший номер прогона
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
            if foreign := _foreign(path, wave, homes):
                # путь runs.json ведёт в manifest другой волны: не «потерян» (файл есть) и не подменяется именем
                return _fail(problems, foreign)
            if not _entry_in(path, wave_dir):
                # записи нет или её каталог вне волны (каталог прогона перенесли/скопировали): то же имя рядом. Запись,
                # которая ЕСТЬ в волне (любого вида: файл с любым именем, битый симлинк, FIFO, каталог, симлинк
                # наружу), не подменяется — её отвергнет `_read_checked` в `_fix_cycles` (I1: runs «нет данных»)
                path = wave_dir / "superarmanda" / Path(raw).name
                if not os.path.lexists(path):
                    path = None  # нет и подстановки: прогон потерян (оговорка I1)
            if path is not None and (other := claimed.get(_key(path), index)) != index:
                # файл называет себя другим прогоном, а тот есть в runs.json (unlisted выше) — значит, это тот же
                # физический файл у двух прогонов (симлинк, hardlink, другая запись пути): одна причина и «нет
                # данных» по прогонам, а не «manifest не найден» при засчитанном прогоне
                return _fail(problems, _same_file(path.name, f"прогона {other} волны {wave}"))
        if path is None or not os.path.lexists(path):
            problems.append(f"manifest прогона {index} не найден")
            found.append((None, index))
            continue
        found.append((path, index))
    return found


def _by_index(wave_dir, wave, homes=None):
    """({номер прогона: файл}, {номер и путь: (st_dev, st_ino)}, None) по `run.index` внутри
    `superarmanda/manifest*.json` (файлы переименовывают) или (None, None, причина). Каждый файл проходит ту же проверку идентичности (`_manifest_checked`), что и засчитанный:
    файл вне каталога волны, не обычный файл (FIFO), испорченный или чужой волны — отказ с его причиной, а не
    пропуск и не «дубль». Номер, заявленный двумя РАЗНЫМИ файлами, — отказ; два пути (симлинки внутри волны) на
    один файл (`st_dev`, `st_ino`) — один прогон."""
    out, keys = {}, {}  # keys: {номер: ключ файла} и {путь: ключ файла} каждого просмотренного manifest
    for path in sorted((wave_dir / "superarmanda").glob("manifest*.json")):
        if foreign := _foreign(path, wave, homes):
            return None, None, foreign
        data, key, why = _manifest_checked(path, wave_dir, wave)
        if why:
            return None, None, why
        keys[path] = key
        run = data.get("run")
        index = run.get("index") if isinstance(run, dict) else None
        if _is_index(index):
            if index in out and keys[index] != key:
                return None, None, f"run.index {index} заявлен двумя файлами: {out[index].name} и {path.name}"
            out.setdefault(index, path)
            keys[index] = key
    return out, keys, None


def _fail(problems, why):
    problems.append(why)
    return []


def _fix_cycles(path, problems, wave_dir, wave, index=None, counted=None, homes=None):
    """(фикс-круги, отвергнут) manifest прогона `index` (None — прогон без runs.json): заявленный в файле номер
    обязан совпасть. Отвергнутый файл (не прочитан, не прошёл `_manifest_fields`, повтор, испорчены tasks) — не
    прогон: (None, True), волна «нет данных» и по прогонам. Прогон из runs.json без файла (`path` None) — (None,
    False): потерянный manifest, прогон был; неверный номер в runs.json — (None, True).
    `counted` — {(st_dev, st_ino): (волна, номер)} уже прочитанных файлов каталога прогона (ключ — от открытого и
    прочитанного дескриптора `_read_checked`, регистрируется ДО сверки полей): повтор физического файла (тот же
    путь, другая запись пути, симлинк, hardlink) — одна причина «тот же файл», а не второе слагаемое. `homes` —
    дом каждого manifest прогона: файл другой волны — «тот же файл» до чтения (`_foreign`)."""
    if path is None:
        return None, not _is_index(index)
    if foreign := _foreign(path, wave, homes):
        problems.append(foreign)
        return None, True
    data, key, why = _read_json(path, wave_dir)
    if not why and counted is not None:
        if key in counted:
            problems.append(_same_file(path.name, _whose(counted[key])))
            return None, True
        counted[key] = (wave, index)
    if not why:
        data, why = _manifest_fields(path, wave, data)
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if why or not isinstance(tasks, dict):
        problems.append(why or f"{path.name}: нет tasks")
        return None, True
    if "run" in data:  # без `run` — manifest старого формата; заявленный номер — испорченный или чужой ввод
        declared = data["run"].get("index") if isinstance(data["run"], dict) else None
        if not _is_index(declared) or (index is not None and declared != index):
            problems.append(f"{path.name}: номер прогона в run не {index if index is not None else 'положительное целое'}")
            return None, True
    total = 0
    for name, entry in tasks.items():
        cycles = entry.get("fix_cycles") if isinstance(entry, dict) else None
        if isinstance(cycles, bool) or not isinstance(cycles, int) or not 0 <= cycles <= MAX_FIX_CYCLES:
            problems.append(f"{path.name}: fix_cycles задачи {name} не число от 0 до {MAX_FIX_CYCLES}")
            return None, True
        total += cycles
    return total, False


def blocked_cap_by_wave(events):
    """{волна: число развилок} по тексту events.log.

    Эпизод — непрерывный период, когда ТЕКУЩИЙ статус волны — именно эта строка blocked_cap. Статус волны меняют
    строки `<волна>: BLOCKED: …`, `<волна>: DONE|RUNNING|…`, `phase=… status=<…>`, ответ политики, успешный
    `say: …` (но не `say FAILED`, не служебный `say-note: …` и не легаси-строки SAY_SERVICE) и запуск окна;
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


def _wave_roots(run_dir, waves):
    """I3: {имя волны: (корень, None)} для засчитываемых волн и {имя: (None, причина)} для остальных. Корень
    разрешается ОДИН раз. Один каталог волны (`st_dev`, `st_ino` корня) — одна волна: его хозяин — запись-каталог
    (не симлинк), а при нескольких симлинках — первая по номеру; остальные записи — «тот же, что у Wn», и их
    файлы не читаются. Порядок номеров не важен: W1 -> W2 засчитывает W2."""
    out, entries = {}, []
    for d in waves:
        try:
            if not _is_kind(d, stat.S_ISDIR):  # висячий симлинк, петля, файл — запись волны есть, каталога нет
                out[d.name] = (None, "каталог волны не читается (битый симлинк)" if os.path.islink(d)
                               else "запись волны — не каталог")
            elif not _within(root := _real(d), run_dir):
                # граница одна — каталог прогона: волна-симлинк наружу (чужая цепочка) не читается и не засчитывается
                out[d.name] = (None, "каталог волны ведёт вне каталога прогона")
            else:
                entries.append((os.path.islink(d), len(entries), d.name, root, _key(root)))
        except OSError as e:
            out[d.name] = (None, f"каталог волны не читается ({type(e).__name__})")
    owners = {}
    for is_link, _, name, root, key in sorted(entries):  # сначала каталоги, потом симлинки; внутри — по номеру
        if key is not None and key in owners:
            out[name] = (None, f"каталог волны — тот же, что у {owners[key]}; одна волна не считается дважды")
            continue
        if key is not None:
            owners[key] = name
        out[name] = (root, None)
    return {d.name: out[d.name] for d in waves}  # порядок номеров волн — для «первой по номеру» в `_homes`


def _homes(roots):
    """I1/I4: дом каждого физического manifest прогона — {(st_dev, st_ino): (волна, номер прогона или None)}.
    Кандидаты — `superarmanda/manifest*.json` каждой засчитываемой волны, лежащие внутри её корня (симлинк наружу
    — не дом). Дом — первая по номеру волна, где файл прошёл `_manifest_checked` (hardlink-копия с чужим `wave` не
    перебивает настоящую волну); если нигде не прошёл — первая, где он лежит. Любое появление того же файла в
    другой волне — «тот же файл, что у волны <дом>» (`_foreign`), независимо от ветки и порядка."""
    seen, order = {}, 0
    for name, (root, _) in roots.items():  # roots — в порядке номеров волн
        if root is None:
            continue
        try:
            paths = sorted((root / "superarmanda").glob("manifest*.json"))
        except OSError:
            continue
        for path in paths:
            key = _key(path)
            if key is None or not _inside(path, root):
                continue
            data, _, why = _manifest_checked(path, root, name)
            run = data.get("run") if not why else None
            index = run.get("index") if isinstance(run, dict) and _is_index(run.get("index")) else None
            seen.setdefault(key, []).append((why is not None, order, name, index))
            order += 1
    homes = {}
    for key, cands in seen.items():
        _, _, name, index = min(cands)  # прошедший проверку раньше отвергнутого, затем — первый по номеру волны
        homes[key] = (name, index)
    return homes


def measure(run_dir):
    run_dir = Path(run_dir)
    try:
        # каталог волны или симлинк (в том числе висячий: волна не исчезает молча, а «нет данных»); W*-файл — тоже
        waves = sorted((d for d in run_dir.iterdir() if WAVE_DIR.match(d.name)),
                       key=lambda d: int(WAVE_DIR.match(d.name).group(1))) if _is_kind(run_dir, stat.S_ISDIR) else []
    except OSError:
        waves = []  # каталог прогона не читается — как каталог без волн, код 2
    roots = _wave_roots(run_dir, waves)  # I3: корень каждой волны разрешён ОДИН раз, хозяин каталога — не симлинк
    homes = _homes(roots)  # I1/I4: дом каждого физического manifest прогона
    counted = {}  # (st_dev, st_ino) прочитанных manifest — по всему каталогу прогона, не по волне
    raw, _, forks_problem = _read_checked(run_dir / "events.log", run_dir, MAX_LOG_BYTES)  # та же проверка: FIFO не вешает замер
    forks = None
    if not forks_problem:
        try:
            forks = blocked_cap_by_wave(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            forks_problem = f"events.log не читается ({type(e).__name__})"
    rows = []
    for d in waves:
        problems, refused = [], True
        root, why = roots[d.name]
        manifests, cycles = [], [None]
        try:
            if why:
                problems.append(why)
            else:
                # дальше читается от `root`, а не от `d`: подмена симлинка волны после проверки границы не уводит
                # чтение в чужую цепочку
                manifests = _manifest_paths(root, problems, d.name, homes)
                results = [_fix_cycles(p, problems, root, d.name, i, counted, homes) for p, i in manifests]
                cycles = [c for c, _ in results]
                refused = any(r for _, r in results)
        except OSError as e:  # каталог волны не читается (права): «нет данных», не traceback
            manifests, cycles, refused = [], [None], True
            problems.append(f"каталог волны не читается ({type(e).__name__})")
        rows.append({
            "wave": d.name,
            # I1: прогон — только проверенный manifest или потерянный (из runs.json, файла нет); волна с отвергнутым
            # или повторённым файлом — «нет данных» и по прогонам, как волна-симлинк наружу
            "runs": len(manifests) if manifests and not refused else None,
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
    text = json.dumps(result, ensure_ascii=False, indent=2) if args.json else table(result)
    # в problems попадает текст из чужих файлов (одиночный суррогат и т. п.): вывод не роняет ни один символ
    enc = sys.stdout.encoding or "utf-8"
    print(text.encode(enc, "backslashreplace").decode(enc, "replace"))
    if not result["ok"]:
        print("metrics.py: часть данных не прочитана (см. «нет данных»), код выхода 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
