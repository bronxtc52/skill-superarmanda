#!/usr/bin/env python3
"""scripts/run-tests.sh: параллельные наборы (W4 waves-tails2 п.5).

Каждый тест подставляет свой каталог наборов через TESTS_DIR: фейковые `*.test.sh` пишут в общий журнал
`start <имя>` / `end <имя>` и печатают свою строку — по ним видно порядок запуска, перекрытие и порядок вывода."""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-tests.sh"

FAKE = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "start {name}" >> "$RUN_TESTS_FAKE_LOG"
    echo "out of {name}"
    echo "err of {name}" >&2
    sleep {sleep}
    echo "end {name}" >> "$RUN_TESTS_FAKE_LOG"
    exit {rc}
    """)

# набор, который пишет в журнал окружение, что видит: локаль и запрет байткода
ENV_FAKE = textwrap.dedent("""\
    #!/usr/bin/env bash
    echo "env {name} LC_ALL=${{LC_ALL-unset}} PYTHONDONTWRITEBYTECODE=${{PYTHONDONTWRITEBYTECODE-unset}}" >> "$RUN_TESTS_FAKE_LOG"
    """)

# локали, где порядок glob отличается от байтового (C): регистр и пунктуация сравниваются иначе
NATURAL_LOCALES = ("en_US.UTF-8", "en_US.utf8", "de_DE.UTF-8", "de_DE.utf8")


def natural_locale():
    """Первая доступная локаль из NATURAL_LOCALES или None (`locale -a` пишет имена в своём регистре)."""
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10).stdout.split()
    except OSError:
        return None
    norm = {h.lower().replace("-", ""): h for h in have}
    for want in NATURAL_LOCALES:
        if want.lower().replace("-", "") in norm:
            return norm[want.lower().replace("-", "")]
    return None


class RunTests(unittest.TestCase):
    def suites(self, **spec):
        """spec: имя -> (sleep, rc)."""
        d = Path(tempfile.mkdtemp(prefix="run-tests-"))
        self.addCleanup(shutil.rmtree, d, True)
        for name, (sleep, rc) in spec.items():
            (d / f"{name}.test.sh").write_text(FAKE.format(name=name, sleep=sleep, rc=rc), encoding="utf-8")
        self.log = d / "fake.log"
        return d

    def run_script(self, tests_dir, jobs=None, extra=None):
        # OMP_NUM_THREADS / OMP_THREAD_LIMIT меняют ответ nproc — число CPU задаёт только сам тест
        env = {k: v for k, v in os.environ.items() if k not in ("RUN_TESTS_JOBS", "OMP_NUM_THREADS", "OMP_THREAD_LIMIT")}
        env.update(TESTS_DIR=str(tests_dir), RUN_TESTS_FAKE_LOG=str(self.log))
        if jobs is not None:
            env["RUN_TESTS_JOBS"] = str(jobs)
        env.update(extra or {})
        return subprocess.run(["/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120)

    def events(self):
        return self.log.read_text(encoding="utf-8").split("\n")[:-1] if self.log.exists() else []

    def blocks(self, out):
        """Порядок появления заголовков `▶ <имя>` в stdout."""
        return re.findall(r"(?m)^▶ (\S+)$", out)

    def test_output_comes_in_the_order_of_the_suites_whatever_finishes_first(self):
        d = self.suites(a=(0.8, 0), b=(0, 3), c=(0.3, 0))
        p = self.run_script(d, jobs=3)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(self.blocks(p.stdout), ["a", "b", "c"])
        # вывод набора — целым блоком под своим заголовком (stdout и stderr набора буферизованы вместе)
        for name in ("a", "b", "c"):
            with self.subTest(name=name):
                block = re.search(rf"(?ms)^▶ {name}\n(.*?)(?=^▶ |\Z)", p.stdout).group(1)
                self.assertIn(f"out of {name}", block)
                self.assertIn(f"err of {name}", block)
        self.assertRegex(p.stdout, r"(?m)^  ✓ a$")
        self.assertRegex(p.stdout + p.stderr, r"(?m)^  ✗ b$")
        self.assertIn("failed suites: b", p.stderr)
        ev = self.events()
        self.assertLess(ev.index("end b"), ev.index("end a"), ev)  # b закончился первым, а напечатан после a

    def test_the_suites_run_at_the_same_time(self):
        d = self.suites(a=(1, 0), b=(1, 0), c=(1, 0))
        p = self.run_script(d, jobs=3)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ev = self.events()
        starts = [i for i, e in enumerate(ev) if e.startswith("start")]
        ends = [i for i, e in enumerate(ev) if e.startswith("end")]
        self.assertLess(max(starts), min(ends), ev)  # все три стартовали до первого конца (без замера времени:
        # этот набор сам идёт параллельно с тяжёлыми, секундомер под нагрузкой дал бы ложное падение)

    def test_at_most_jobs_suites_at_once(self):
        d = self.suites(a=(0.3, 0), b=(0.3, 0), c=(0.3, 0), d=(0.3, 0), e=(0.3, 0))
        p = self.run_script(d, jobs=2)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        running = peak = 0
        for e in self.events():
            running += 1 if e.startswith("start") else -1
            peak = max(peak, running)
        self.assertEqual(peak, 2, self.events())
        self.assertEqual(self.blocks(p.stdout), ["a", "b", "c", "d", "e"])

    def test_jobs_1_is_sequential_in_the_order_of_the_suites(self):
        d = self.suites(superarmanda_x=(0, 0), **{"superarmanda-state": (0.1, 0), "aaa": (0.1, 1), "zzz": (0, 0)})
        p = self.run_script(d, jobs=1)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        names = sorted(["superarmanda_x", "superarmanda-state", "aaa", "zzz"])  # порядок glob
        want = []
        for n in names:
            want += [f"start {n}", f"end {n}"]
        self.assertEqual(self.events(), want)
        self.assertEqual(self.blocks(p.stdout), names)
        self.assertIn("failed suites: aaa", p.stderr)

    def env_suites(self, *names):
        d = self.suites()
        for name in names:
            (d / f"{name}.test.sh").write_text(ENV_FAKE.format(name=name), encoding="utf-8")
        return d

    def test_suite_order_is_byte_order_whatever_the_locale(self):
        """CI macOS W4: порядок наборов — байтовый (как LC_ALL=C), а не порядок glob локали: в en_US «-» и «_»,
        регистр и пунктуация сравниваются иначе. Имена подобраны так, что C-порядок и порядок en_US различны
        целиком; локаль самих наборов при этом не меняется. Нет en_US/de_DE — прогон в локали хоста, имена те же."""
        names = ["ab", "a-c", "Zed", "superarmanda_x", "superarmanda-state"]
        want = sorted(names, key=lambda n: n.encode())  # байтовый порядок
        loc = natural_locale()
        extra = {"LC_ALL": loc, "LANG": loc} if loc else {}
        for jobs in (1, 3):
            with self.subTest(jobs=jobs, locale=loc):
                d = self.env_suites(*names)
                p = self.run_script(d, jobs=jobs, extra=extra)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.blocks(p.stdout), want)
                seen = {e.split()[1]: e.split()[2] for e in self.events()}
                self.assertEqual(sorted(seen), sorted(names))
                want_lc = f"LC_ALL={loc}" if loc else f"LC_ALL={os.environ.get('LC_ALL', 'unset')}"
                self.assertEqual(set(seen.values()), {want_lc}, seen)  # локаль наборов — как у вызова
        if loc:  # доказательство, что имена различают порядки: та же локаль даёт в glob другой порядок
            d = self.env_suites(*names)
            glob = subprocess.run(["/bin/bash", "-c", 'cd "$1" && for f in *.test.sh; do echo "${f%.test.sh}"; done',
                                   "-", str(d)], capture_output=True, text=True, env={**os.environ, **extra})
            self.assertNotEqual(glob.stdout.split(), want, f"{loc} sorts like C — the test proves nothing")

    def test_suites_never_write_bytecode(self):
        """CI W4: наборы идут с PYTHONDONTWRITEBYTECODE=1 — параллельный набор не пишет `__pycache__` в фикстуры,
        которые в это же время проверяет standalone-layout."""
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                p = self.run_script(self.env_suites("a", "b"), jobs=jobs, extra={"PYTHONDONTWRITEBYTECODE": ""})
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual([e.split()[-1] for e in self.events()], ["PYTHONDONTWRITEBYTECODE=1"] * 2)

    def test_long_suites_start_first(self):
        slow = ["superarmanda-state", "superarmanda-waves-e2e", "superarmanda-waves", "superarmanda-review"]
        spec = {n: (0.6, 0) for n in slow}
        spec.update({"aaa": (0, 0), "bbb": (0, 0), "zzz": (0, 0)})
        d = self.suites(**spec)
        p = self.run_script(d, jobs=4)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        starts = [e[6:] for e in self.events() if e.startswith("start")]
        self.assertEqual(set(starts[:4]), set(slow), starts)
        # печать — в исходном порядке наборов (порядок glob, тот же, что у последовательного режима)
        self.log.unlink()
        self.assertEqual(self.blocks(p.stdout), self.blocks(self.run_script(d, jobs=1).stdout))
        self.assertEqual(sorted(self.blocks(p.stdout)), sorted(spec))

    def test_all_green_is_zero_and_any_red_is_nonzero(self):
        for jobs in (1, 3):
            with self.subTest(jobs=jobs, case="green"):
                p = self.run_script(self.suites(a=(0, 0), b=(0, 0)), jobs=jobs)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with self.subTest(jobs=jobs, case="last red"):
                p = self.run_script(self.suites(a=(0, 0), b=(0.2, 5)), jobs=jobs)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("failed suites: b", p.stderr)

    def fake_cpus(self, n):
        """PATH с подставными `nproc` и `sysctl` (ветка macOS), отвечающими `n`: число CPU хоста тест не видит."""
        d = Path(tempfile.mkdtemp(prefix="run-tests-cpus-"))
        self.addCleanup(shutil.rmtree, d, True)
        for tool in ("nproc", "sysctl"):
            (d / tool).write_text(f"#!/bin/sh\necho {n}\n", encoding="utf-8")
            (d / tool).chmod(0o755)
        return {"PATH": f"{d}{os.pathsep}{os.environ.get('PATH', '')}"}

    def test_default_is_the_number_of_cpus(self):
        """Внешнее ревью W4: без RUN_TESTS_JOBS — столько сразу, сколько CPU; при одном CPU — последовательная
        ветка без строки «parallel». Число CPU подставлено, а не взято с хоста: тест одинаков на 1 и на 64 CPU."""
        with self.subTest(cpus=1):
            p = self.run_script(self.suites(a=(0, 0), b=(0, 0)), extra=self.fake_cpus(1))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertNotIn("parallel", p.stderr)
            self.assertEqual(self.blocks(p.stdout), ["a", "b"])
        with self.subTest(cpus=4):
            p = self.run_script(self.suites(a=(0, 0), b=(0, 0)), extra=self.fake_cpus(4))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("run-tests: 4 parallel", p.stderr)
        with self.subTest(cpus=3, branch="sysctl"):  # macOS: nproc нет (здесь — падает), число даёт sysctl hw.ncpu
            extra = self.fake_cpus(3)
            nproc = Path(extra["PATH"].split(os.pathsep)[0]) / "nproc"
            nproc.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            p = self.run_script(self.suites(a=(0, 0), b=(0, 0)), extra=extra)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("run-tests: 3 parallel", p.stderr)

    def test_a_bad_jobs_value_is_refused(self):
        for bad in ("0", "-1", "x", "1.5", ""):
            with self.subTest(bad=bad):
                p = self.run_script(self.suites(a=(0, 0)), jobs=bad)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("RUN_TESTS_JOBS", p.stderr)
                self.assertEqual(self.events(), [])

    def test_no_suites_is_exit_2(self):
        d = Path(tempfile.mkdtemp(prefix="run-tests-empty-"))
        self.addCleanup(shutil.rmtree, d, True)
        self.log = d / "fake.log"
        self.assertEqual(self.run_script(d, jobs=2).returncode, 2)

    def test_the_script_keeps_to_bash_3_2(self):
        """macOS: /bin/bash 3.2 — ни `wait -n`, ни ассоциативных массивов, ни mapfile/readarray, ни ${x,,}."""
        src = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for bad in (r"wait\s+-n", r"declare\s+-A", r"local\s+-A", r"\bmapfile\b", r"\breadarray\b",
                    r"\$\{\w+,,", r"\$\{\w+\^\^", r"wait\s+-p", r"\|&", r"&>>"):
            with self.subTest(bad=bad):
                self.assertIsNone(re.search(bad, code), bad)

    def test_ci_job_name_and_timeout_are_unchanged(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("timeout-minutes: 40", ci)
        self.assertRegex(ci, r"(?m)^  tests:$")
        self.assertIn("run: /bin/bash scripts/run-tests.sh", ci)


if __name__ == "__main__":
    unittest.main(verbosity=2)
