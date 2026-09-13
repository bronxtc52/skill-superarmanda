#!/usr/bin/env python3
"""Acceptance tests for portable, non-destructive skill installation."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


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
