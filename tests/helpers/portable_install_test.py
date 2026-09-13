#!/usr/bin/env python3
"""Acceptance tests for portable, non-destructive skill installation."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class PortableInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fork = self.root / "a fork with spaces"
        shutil.copytree(ROOT, self.fork, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.skill = self.fork / "skills" / "superarmanda"
        self.installer = self.skill / "scripts" / "install-skill.py"
        self.home = self.root / "isolated home"
        subprocess.run(["git", "init", "-q", str(self.fork)], check=True)
        subprocess.run(["git", "-C", str(self.fork), "config", "user.email", "fork@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.fork), "config", "user.name", "Fork Owner"], check=True)
        subprocess.run(["git", "-C", str(self.fork), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.fork), "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(self.fork), "remote", "add", "origin", "https://example.invalid/owner/fork.git"], check=True)

    def install(self, client, check=True):
        return subprocess.run(
            ["python3", str(self.installer), "install", "--client", client, "--target-home", str(self.home)],
            text=True, capture_output=True, check=check,
        )

    def installer_module(self):
        spec = importlib.util.spec_from_file_location("portable_installer", self.installer)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fork_path_install_is_idempotent_and_preserves_origin(self):
        self.install("both")
        claude = self.home / ".claude" / "skills" / "superarmanda"
        codex = self.home / ".codex" / "skills" / "superarmanda"
        self.assertTrue(claude.is_symlink())
        self.assertTrue(codex.is_symlink())
        self.assertEqual(claude.resolve(), self.skill.resolve())
        self.assertEqual(codex.resolve(), self.skill.resolve())
        self.install("both")
        origin = subprocess.run(["git", "-C", str(self.fork), "remote", "get-url", "origin"], text=True, capture_output=True, check=True)
        self.assertEqual(origin.stdout.strip(), "https://example.invalid/owner/fork.git")

    def test_foreign_file_directory_and_dangling_link_are_refused(self):
        cases = ("file", "directory", "dangling", "foreign-link")
        for kind in cases:
            with self.subTest(kind=kind):
                target = self.home / ".claude" / "skills" / "superarmanda"
                self.install("claude")
                target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "file":
                    target.write_text("foreign", encoding="utf-8")
                    sentinel = target.read_text(encoding="utf-8")
                elif kind == "directory":
                    target.mkdir()
                    (target / "sentinel").write_text("foreign", encoding="utf-8")
                    sentinel = (target / "sentinel").read_text(encoding="utf-8")
                elif kind == "foreign-link":
                    foreign = self.root / "foreign skill"
                    foreign.mkdir()
                    (foreign / "sentinel").write_text("foreign", encoding="utf-8")
                    target.symlink_to(foreign, target_is_directory=True)
                    sentinel = (foreign / "sentinel").read_text(encoding="utf-8")
                else:
                    target.symlink_to(self.root / "missing")
                    sentinel = None
                result = self.install("claude", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(target.exists() or target.is_symlink())
                if kind == "file":
                    self.assertEqual(target.read_text(encoding="utf-8"), sentinel)
                elif kind == "directory":
                    self.assertEqual((target / "sentinel").read_text(encoding="utf-8"), sentinel)
                elif kind == "foreign-link":
                    self.assertTrue(target.is_symlink())
                    self.assertEqual((target.resolve() / "sentinel").read_text(encoding="utf-8"), sentinel)
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()

    def test_both_preflights_before_creating_any_client_destination(self):
        codex = self.home / ".codex" / "skills" / "superarmanda"
        self.install("codex")
        codex.unlink()
        codex.parent.mkdir(parents=True, exist_ok=True)
        codex.write_text("foreign", encoding="utf-8")
        result = self.install("both", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".claude" / "skills" / "superarmanda").exists())
        self.assertEqual(codex.read_text(encoding="utf-8"), "foreign")

    def test_incomplete_skill_source_is_refused_before_destinations(self):
        (self.skill / "SKILL.md").unlink()
        result = self.install("both", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_codex_home_convention_is_used_without_target_home(self):
        codex_home = self.root / "custom codex home"
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        subprocess.run(
            ["python3", str(self.installer), "install", "--client", "codex"],
            env=env, check=True,
        )
        self.assertEqual(
            (codex_home / "skills" / "superarmanda").resolve(),
            self.skill.resolve(),
        )

    def test_live_directory_symlink_target_home_is_supported(self):
        backing_home = self.root / "real target home"
        backing_home.mkdir()
        self.home.symlink_to(backing_home, target_is_directory=True)
        self.install("both")
        self.assertEqual(
            (backing_home / ".claude" / "skills" / "superarmanda").resolve(),
            self.skill.resolve(),
        )
        self.assertEqual(
            (backing_home / ".codex" / "skills" / "superarmanda").resolve(),
            self.skill.resolve(),
        )

    def test_dangling_or_file_target_home_is_refused_without_writes(self):
        for kind in ("dangling", "file"):
            with self.subTest(kind=kind):
                if kind == "dangling":
                    self.home.symlink_to(self.root / "missing target home")
                else:
                    self.home.write_text("foreign home", encoding="utf-8")
                result = self.install("both", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".claude").exists())
                self.assertFalse((self.home / ".codex").exists())
                if kind == "dangling":
                    self.assertTrue(self.home.is_symlink())
                    self.home.unlink()
                else:
                    self.assertEqual(self.home.read_text(encoding="utf-8"), "foreign home")
                    self.home.unlink()

    def test_empty_target_home_is_refused_before_home_resolution(self):
        installer = self.installer_module()
        with mock.patch.object(installer, "client_home", side_effect=AssertionError("home lookup")):
            with self.assertRaises(ValueError):
                installer.install(SimpleNamespace(client="both", target_home=""))

    def test_omitted_target_home_uses_mocked_client_homes(self):
        installer = self.installer_module()
        fake_home = self.root / "mock client homes"

        def client_home(client, target_home):
            self.assertIsNone(target_home)
            return fake_home / (".claude" if client == "claude" else ".codex")

        with mock.patch.object(installer, "client_home", side_effect=client_home):
            installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertEqual(
            (fake_home / ".claude" / "skills" / "superarmanda").resolve(),
            self.skill.resolve(),
        )
        self.assertEqual(
            (fake_home / ".codex" / "skills" / "superarmanda").resolve(),
            self.skill.resolve(),
        )

    def test_colliding_client_destinations_install_once_for_same_and_alias_paths(self):
        for kind in ("same", "alias"):
            with self.subTest(kind=kind):
                installer = self.installer_module()
                real_home = self.root / f"shared {kind} home"
                if kind == "alias":
                    real_home.mkdir()
                    alias_home = self.root / f"shared {kind} alias"
                    alias_home.symlink_to(real_home, target_is_directory=True)
                else:
                    alias_home = real_home

                def client_home(client, target_home):
                    self.assertIsNone(target_home)
                    return real_home if client == "claude" else alias_home

                with mock.patch.object(installer, "client_home", side_effect=client_home):
                    installer.install(SimpleNamespace(client="both", target_home=None))
                self.assertEqual(
                    (real_home / "skills" / "superarmanda").resolve(),
                    self.skill.resolve(),
                )

    def test_colliding_foreign_target_is_refused_without_replacement(self):
        installer = self.installer_module()
        shared_home = self.root / "foreign shared home"
        target = shared_home / "skills" / "superarmanda"
        target.parent.mkdir(parents=True)
        target.write_text("foreign", encoding="utf-8")
        with mock.patch.object(installer, "client_home", return_value=shared_home):
            with self.assertRaises(ValueError):
                installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign")

    def test_nested_client_destinations_are_rejected_before_writes(self):
        for topology in ("direct", "alias", "present", "present-alias"):
            for order in ("claude-first", "codex-first"):
                with self.subTest(topology=topology, order=order):
                    installer = self.installer_module()
                    label = f"nested-{topology}-{order}"
                    real_home = self.root / f"{label} real home"
                    ancestor_home = real_home
                    if topology in ("alias", "present-alias"):
                        real_home.mkdir()
                        alias_home = self.root / f"{label} alias home"
                        alias_home.symlink_to(real_home, target_is_directory=True)
                        nested_home = alias_home / "skills" / "superarmanda" / label
                    else:
                        nested_home = real_home / "skills" / "superarmanda" / label
                    ancestor_target = real_home / "skills" / "superarmanda"
                    if topology in ("present", "present-alias"):
                        ancestor_target.parent.mkdir(parents=True)
                        ancestor_target.symlink_to(self.skill, target_is_directory=True)
                    homes = (
                        {"claude": ancestor_home, "codex": nested_home}
                        if order == "claude-first"
                        else {"claude": nested_home, "codex": ancestor_home}
                    )
                    source_write = self.skill / label
                    try:
                        with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]):
                            with self.assertRaises(ValueError):
                                installer.install(SimpleNamespace(client="both", target_home=None))
                        self.assertEqual(ancestor_target.exists(), topology.startswith("present"))
                        self.assertFalse(source_write.exists())
                    finally:
                        if source_write.exists():
                            shutil.rmtree(source_write)

    def test_casefold_nested_destinations_are_rejected_before_writes(self):
        for order in ("claude-first", "codex-first"):
            with self.subTest(order=order):
                installer = self.installer_module()
                root = self.root / f"casefold nested {order}"
                label = f"casefold-{order}"
                ancestor_home = root / ".claude"
                nested_home = root / ".CLAUDE" / "skills" / "superarmanda" / label
                homes = (
                    {"claude": ancestor_home, "codex": nested_home}
                    if order == "claude-first"
                    else {"claude": nested_home, "codex": ancestor_home}
                )
                with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]):
                    with self.assertRaises(ValueError):
                        installer.install(SimpleNamespace(client="both", target_home=None))
                self.assertFalse((ancestor_home / "skills" / "superarmanda").exists())
                self.assertFalse((self.skill / label).exists())

    def test_unicode_equivalent_nested_destinations_are_rejected_before_writes(self):
        installer = self.installer_module()
        root = self.root / "unicode nested"
        label = "unicode-nested"
        composed_home = root / "caf\u00e9"
        decomposed_home = root / "cafe\u0301" / "skills" / "superarmanda" / label
        homes = {"claude": composed_home, "codex": decomposed_home}
        with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]):
            with self.assertRaises(ValueError):
                installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertFalse((composed_home / "skills" / "superarmanda").exists())
        self.assertFalse((self.skill / label).exists())

    def test_case_variant_equal_targets_recheck_before_second_write(self):
        installer = self.installer_module()
        root = self.root / "case variant equal"
        homes = {"claude": root / ".claude", "codex": root / ".CLAUDE"}
        original_preflight = installer.preflight
        calls = 0

        def case_insensitive_preflight(destination):
            nonlocal calls
            calls += 1
            if calls == 4:
                return "present"
            return original_preflight(destination)

        with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]), mock.patch.object(installer, "preflight", side_effect=case_insensitive_preflight), mock.patch.object(installer.os, "symlink", wraps=installer.os.symlink) as symlink:
            installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertTrue((homes["claude"] / "skills" / "superarmanda").is_symlink())
        self.assertEqual(symlink.call_count, 1)

    def test_case_variant_equal_targets_point_to_skill_on_current_filesystem(self):
        installer = self.installer_module()
        root = self.root / "case variant actual"
        homes = {"claude": root / ".claude", "codex": root / ".CLAUDE"}
        with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]):
            installer.install(SimpleNamespace(client="both", target_home=None))
            installer.install(SimpleNamespace(client="both", target_home=None))
        for home in homes.values():
            self.assertEqual(
                (home / "skills" / "superarmanda").resolve(), self.skill.resolve()
            )

    def test_source_contained_destinations_are_refused_without_source_changes(self):
        external_home = self.root / "external home"
        alias = self.root / "source alias"
        alias.symlink_to(self.skill, target_is_directory=True)
        case_variant = self.skill.parent / self.skill.name.upper()
        for client, source_home in (
            ("claude", self.skill),
            ("codex", self.skill),
            ("claude", alias),
            ("codex", alias),
            ("claude", case_variant),
            ("codex", case_variant),
        ):
            with self.subTest(client=client, source_home=source_home):
                installer = self.installer_module()
                before = {
                    path.relative_to(self.skill): path.read_bytes()
                    for path in self.skill.rglob("*")
                    if path.is_file()
                }
                homes = {"claude": external_home, "codex": external_home}
                homes[client] = source_home
                try:
                    with mock.patch.object(installer, "client_home", side_effect=lambda name, _: homes[name]):
                        with self.assertRaises(ValueError):
                            installer.install(SimpleNamespace(client="both", target_home=None))
                    after = {
                        path.relative_to(self.skill): path.read_bytes()
                        for path in self.skill.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(after, before)
                finally:
                    nested = self.skill / "skills"
                    if nested.exists():
                        shutil.rmtree(nested)

    def test_external_identical_link_remains_idempotent(self):
        installer = self.installer_module()
        home = self.root / "outside source"
        with mock.patch.object(installer, "client_home", return_value=home):
            installer.install(SimpleNamespace(client="both", target_home=None))
            installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertEqual(
            (home / "skills" / "superarmanda").resolve(), self.skill.resolve()
        )

    def test_bind_alias_nested_destinations_are_refused_before_writes(self):
        for order in ("claude-first", "codex-first"):
            with self.subTest(order=order):
                installer = self.installer_module()
                real_root = self.root / f"bind real {order}"
                alias_root = self.root / f"bind alias {order}"
                real_root.mkdir()
                alias_root.mkdir()
                ancestor_home = real_root
                nested_home = alias_root / "skills" / "superarmanda" / "nested"
                homes = (
                    {"claude": ancestor_home, "codex": nested_home}
                    if order == "claude-first"
                    else {"claude": nested_home, "codex": ancestor_home}
                )
                actual_identity = installer.filesystem_identity

                def bind_identity(path):
                    if Path(path) == alias_root:
                        return actual_identity(real_root)
                    return actual_identity(path)

                with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]), mock.patch.object(installer, "filesystem_identity", side_effect=bind_identity):
                    with self.assertRaises(ValueError):
                        installer.install(SimpleNamespace(client="both", target_home=None))
                self.assertFalse((ancestor_home / "skills" / "superarmanda").exists())
                self.assertFalse((nested_home / "skills" / "superarmanda").exists())

    def test_bind_alias_source_containment_is_refused_before_writes(self):
        installer = self.installer_module()
        alias_root = self.root / "source bind alias"
        alias_root.mkdir()
        external_home = self.root / "source bind external"
        actual_identity = installer.filesystem_identity

        def bind_identity(path):
            if Path(path) == alias_root:
                return actual_identity(self.skill)
            return actual_identity(path)

        homes = {"claude": external_home, "codex": alias_root}
        with mock.patch.object(installer, "client_home", side_effect=lambda client, _: homes[client]), mock.patch.object(installer, "filesystem_identity", side_effect=bind_identity):
            with self.assertRaises(ValueError):
                installer.install(SimpleNamespace(client="both", target_home=None))
        self.assertFalse((alias_root / "skills" / "superarmanda").exists())

    def test_installed_path_builds_state_and_packet_for_local_git_project(self):
        self.install("codex")
        installed = self.home / ".codex" / "skills" / "superarmanda" / "scripts"
        project = self.root / "synthetic project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
        (project / "app.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "app.txt"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)
        base = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        (project / "app.txt").write_text("change\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "commit", "-am", "change", "-q"], check=True)
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
        manifest = self.root / "state.json"
        subprocess.run(["python3", str(installed / "state.py"), "init", "--manifest", str(manifest), "--repo", str(project), "--base", base, "--head", head], check=True)
        requirements = self.root / "requirements.txt"
        evidence = self.root / "evidence.txt"
        packet = self.root / "packet.json"
        requirements.write_text("portable packet\n", encoding="utf-8")
        evidence.write_text("local test\n", encoding="utf-8")
        subprocess.run(["python3", str(installed / "review.py"), "packet", "--repo", str(project), "--base", base, "--head", head, "--requirements", str(requirements), "--test-evidence", str(evidence), "--output", str(packet)], check=True)
        self.assertTrue(manifest.is_file())
        self.assertTrue(packet.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
