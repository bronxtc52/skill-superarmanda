"""Съёмка state.json диспетчера 1.4.1 (коммит 2fdd5a7) в точке офлайн-цепочки e2e: волна W2 получила автоответ политики
на развилку blocked_cap, уведомления лежат в outbox (Telegram «лежит»). Запуск только из корня рабочей копии на 2fdd5a7:

    python3 -I <путь к этому файлу> <каталог вывода>

Обвязка — tests/helpers/superarmanda_waves_e2e_test.py ТОГО ЖЕ коммита, без правок: настоящий wab.py 1.4.1 ведёт цепочку
на виртуальных часах. Правки только здесь, в скрипте (описаны в README рядом):
  * chain.json сценария получает `max_auto_answers: 1` (как режим `remnants` текущего теста) и блок `telegram`,
    а `_send_telegram` бросает OSError — уведомления остаются в outbox;
  * перед каждым сном диспетчера скрипт читает state.json и останавливает `watch` (исключение BaseException), как
    только у W2 записан автоответ (`auto_answers == 1`, без `pending_enter`) и `policy_answer` лежит в outbox.
Снятый state.json обезличивается плейсхолдерами (см. scrub)."""
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest import mock

REPO = Path.cwd()
sys.path.insert(0, str(REPO / "tests" / "helpers"))
import superarmanda_waves_e2e_test as E  # noqa: E402

wab = E.wab
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
EPOCH = (1.5e9, 2.5e9)  # числа state.json в этом диапазоне — моменты времени


class Stop(BaseException):
    pass


got = {}


def setup(self, real=E.World.setup):
    got["world"] = self
    real(self)
    chain = json.loads(self.chain_file.read_text(encoding="utf-8"))
    chain["max_auto_answers"] = 1
    chain["telegram"] = {"keyvault": "kv-e2e-offline", "token_secret": "tg-token", "chat_secret": "tg-chat"}
    self.chain_file.write_text(json.dumps(chain, indent=1), encoding="utf-8")


def actor_init(self, *a, real=E.Actor.__init__, **kw):
    real(self, *a, **kw)
    got["actor"] = self


def sleep(self, seconds, real=E.Clock.sleep):
    w = got.get("world")
    if w is not None and (w.run_dir / "state.json").exists():
        rec = json.loads((w.run_dir / "state.json").read_text(encoding="utf-8"))["waves"].get("W2") or {}
        if rec.get("auto_answers") == 1 and "pending_enter" not in rec and "policy_answer" in (rec.get("outbox") or {}):
            got["now"] = time.time()  # виртуальное «сейчас» диспетчера
            raise Stop()
    return real(self, seconds)


def git_names(world):
    """sha -> тема коммита по всем коммитам рабочей копии и origin (темы сценария уникальны)."""
    names = {}
    for args in (("-C", str(world.workdir)), ("--git-dir", str(world.origin))):
        out = world.run("git", *args, "log", "--all", "--format=%H %s").stdout
        for line in out.splitlines():
            sha, subject = line.split(" ", 1)
            names[sha] = subject
    subjects = list(names.values())
    assert len(set(subjects)) == len(subjects), f"темы коммитов не уникальны: {subjects}"
    return names


def scrub(doc, world, actor, now):
    root = str(world.root)
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(world.workdir))
    sids = {sid: f"@SID:{s.wave}:{i}@" for s in actor.sessions.values() for i, sid in enumerate(s.sids)}
    revs = git_names(world)

    def text(s):
        s = s.replace(slug, "@SLUG@").replace(str(world.home), "@HOME@").replace(root, "@ROOT@")
        for sid, ph in sids.items():
            s = s.replace(sid, ph)
        def rev(m):  # полный sha коммита -> @REV:<тема>@, сокращённый (в тексте сообщения) -> @REV<длина>:<тема>@
            h = m.group(0)
            if h in revs:
                return f"@REV:{revs[h]}@"
            full = [sha for sha in revs if sha.startswith(h)]
            return f"@REV{len(h)}:{revs[full[0]]}@" if len(full) == 1 and len(h) >= 7 else h
        return re.sub(r"\b[0-9a-f]{7,40}\b", rev, s)

    def walk(node, key=None, wave=None):
        if key in ("policy_answered_stamp", "stamp") and isinstance(node, list):
            return f"@STAMP:{wave}@"  # [inode, mtime_ns] файла status на машине съёмки
        if isinstance(node, dict):
            return {text(k): walk(v, k, wave or (k if k in ("W1", "W2") else None)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, None, wave) for v in node]
        if isinstance(node, str):
            return text(node)
        if isinstance(node, (int, float)) and not isinstance(node, bool) and EPOCH[0] <= node <= EPOCH[1]:
            return f"@T:{node - now:+.6f}@"
        return node
    out = walk(doc)
    left = [h for h in re.findall(r"\b[0-9a-f]{40}\b", json.dumps(out)) if h in revs]
    assert not left, left
    assert root not in json.dumps(out, ensure_ascii=False)
    return out


with mock.patch.object(E.World, "setup", setup), mock.patch.object(E.Actor, "__init__", actor_init), \
        mock.patch.object(E.Clock, "sleep", sleep), \
        mock.patch.object(wab, "_send_telegram", side_effect=OSError("telegram down")):
    try:
        E.run_scenario("normal")
        raise SystemExit("цепочка дошла до конца без точки съёмки")
    except Stop:
        pass

world, actor = got["world"], got["actor"]
doc = json.loads((world.run_dir / "state.json").read_text(encoding="utf-8"))
(OUT / "state.json").write_text(json.dumps(scrub(doc, world, actor, got["now"]), ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
print("W2 status:", (world.run_dir / "W2" / "status").read_text(encoding="utf-8").strip()[:90])
print("state.json ->", OUT / "state.json")
