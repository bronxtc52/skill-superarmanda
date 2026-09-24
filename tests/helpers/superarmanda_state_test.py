#!/usr/bin/env python3
"""Contract tests for the local Superarmanda state CLI.

The fixture is created from scratch for every test.  It deliberately calls the
CLI as an outside client, so this verifies the public command contract rather
than internal helper functions.
"""

import json
import importlib.util
import os
import hashlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "skills" / "superarmanda" / "scripts" / "state.py"


class StateContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.git("config", "user.email", "tester@example.invalid")
        self.git("config", "user.name", "State Tester")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.base = self.head()
        self.manifest = Path(self.tmp.name) / "run.json"
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.base,
            "--run-id",
            "one",
        )

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            text=True,
            capture_output=True,
            check=check,
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def cli(self, command, *args, check=True):
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                command,
                "--manifest",
                str(self.manifest),
                *map(str, args),
            ],
            text=True,
            capture_output=True,
        )
        if check and proc.returncode:
            self.fail(f"{command} failed rc={proc.returncode}: {proc.stderr}")
        return proc

    def record(
        self,
        role,
        status="pass",
        session=None,
        head=None,
        check=True,
        reviewed_head=None,
        packet_hash=None,
    ):
        args = [
            "task-result",
            "--task",
            "implement",
            "--role",
            role,
            "--status",
            status,
            "--session-id",
            session or f"{role}-session",
            "--head",
            head or self.head(),
            "--artifact",
            "evidence",
        ]
        if reviewed_head is not None:
            args.extend(["--reviewed-head", reviewed_head])
        if packet_hash is not None:
            args.extend(["--packet-hash", packet_hash])
        return self.cli(*args, check=check)

    def resume(self, head=None, check=True):
        return self.cli(
            "resume",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            head or self.head(),
            check=check,
        )

    def manifest_data(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def init_manifest_at_head(self, name="submodule-run.json"):
        self.manifest = Path(self.tmp.name) / name
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
            "--run-id",
            "submodules",
        )

    def add_submodule(self, name, source):
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q", source, name
        )

    def make_submodule_source(self, name):
        source = Path(self.tmp.name) / name
        source.mkdir()
        subprocess.run(
            ["git", "-C", str(source), "init", "-q", "-b", "main"], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "config",
                "user.email",
                "tester@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "config", "user.name", "State Tester"],
            check=True,
        )
        (source / "module.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "module.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
        return source

    # Negative cases come first.  Each must fail for the stated bad input;
    # otherwise a later green workflow test would only prove a self-consistent
    # but unsafe implementation.
    def test_rejects_stale_result_after_actual_head_changes_even_if_caller_lies(self):
        self.record("coder")
        self.git("commit", "--allow-empty", "-qm", "new head")

        # A caller can retain the old argument after another process commits.
        # Resume must bind the manifest to the repository's real HEAD and
        # invalidate the old result, rather than accepting this stale claim.
        stale = self.resume(head=self.base, check=False)
        self.assertNotEqual(stale.returncode, 0, stale.stdout + stale.stderr)

    def test_init_rejects_a_supplied_head_that_is_not_in_the_repository(self):
        fake_manifest = Path(self.tmp.name) / "fake-head.json"
        fake_head = "f" * 40
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(fake_manifest),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                fake_head,
                "--run-id",
                "fake-head",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(fake_manifest.exists())

    def test_clean_commit_invalidates_ready_results_before_explicit_resume(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            reviewed_head=self.head(),
            packet_hash="sha256:" + "b" * 64,
        )
        self.assertEqual(
            self.manifest_data()["tasks"]["implement"]["status"], "ready_for_pr_review"
        )
        self.git("commit", "--allow-empty", "-qm", "unreviewed clean commit")

        # `status` is read-only, but it must never report an old HEAD as ready.
        # A clean commit has no diff, so comparing only the worktree fingerprint
        # is insufficient here.
        report = json.loads(self.cli("status").stdout)
        self.assertFalse(report.get("tree_matches"))
        self.assertNotEqual(
            report["tasks"]["implement"]["status"], "ready_for_pr_review"
        )

    def test_rejects_stale_result_after_tracked_or_untracked_change(self):
        self.record("coder")
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        rejected = self.record("tester", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.resume()
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["results"], {})

    def test_unignored_empty_directories_are_rejected_but_ignored_or_gitkept_are_allowed(
        self,
    ):
        empty = self.repo / "empty"
        empty.mkdir()
        self.assertNotEqual(self.cli("status", check=False).returncode, 0)
        self.assertNotEqual(self.record("coder", check=False).returncode, 0)
        empty.rmdir()
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore state directory")
        self.resume()
        (self.repo / "ignored").mkdir()
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])
        kept = self.repo / "kept"
        kept.mkdir()
        (kept / ".gitkeep").write_text("", encoding="utf-8")
        self.git("add", "kept/.gitkeep")
        self.git("commit", "-qm", "track gitkeep")
        self.resume()
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])

    def test_nested_unignored_empty_directory_is_rejected(self):
        nested = self.repo / "parent" / "child"
        nested.mkdir(parents=True)
        rejected = self.cli("status", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unignored empty directory parent/child", rejected.stderr)

    def test_nonignored_directory_with_only_ignored_children_is_rejected(self):
        (self.repo / ".gitignore").write_text("parent/ignored/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore child directory")
        self.resume()
        (self.repo / "parent" / "ignored").mkdir(parents=True)
        rejected = self.cli("status", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unignored empty directory parent", rejected.stderr)

    def test_nonignored_directory_with_only_ignored_file_is_rejected(self):
        (self.repo / ".gitignore").write_text("parent/ignored.txt\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore child file")
        self.resume()
        parent = self.repo / "parent"
        parent.mkdir()
        (parent / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        rejected = self.cli("status", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unignored empty directory parent", rejected.stderr)

    def test_tracked_ignore_matching_file_remains_fingerprint_visible(self):
        (self.repo / ".gitignore").write_text("parent/tracked.txt\n", encoding="utf-8")
        parent = self.repo / "parent"
        parent.mkdir()
        (parent / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("add", "-f", "parent/tracked.txt")
        self.git("commit", "-qm", "tracked ignored path")
        self.resume()
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])

    def test_ignored_directories_are_not_traversed(self):
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        ignored = self.repo / "ignored"
        ignored.mkdir()
        spec = importlib.util.spec_from_file_location("state_prune_test", STATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = os.scandir

        def checked(path):
            self.assertNotEqual(Path(path), ignored)
            return original(path)

        with mock.patch.object(module.os, "scandir", side_effect=checked):
            module._reject_unignored_empty_directories(self.repo, set())

    def test_manifest_symlink_is_rejected_before_parsing_target(self):
        private = Path(self.tmp.name) / "private.json"
        private.write_text("deliberately invalid JSON", encoding="utf-8")
        self.manifest = Path(self.tmp.name) / "alias.json"
        self.manifest.symlink_to(private)
        result = self.cli("status", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be symlinks", result.stderr)

    def test_untracked_symlink_fingerprint_does_not_read_outside_target(self):
        outside = Path(self.tmp.name) / "outside-target"
        outside.write_text("first target contents\n", encoding="utf-8")
        link = self.repo / "external-link"
        os.symlink(outside, link)
        self.resume()

        # The repository tracks the symlink pathname, not bytes of a file it
        # points to outside the repository.  Mutating that target therefore
        # must not invalidate local evidence or disclose external contents.
        outside.write_text("changed external contents\n", encoding="utf-8")
        report = json.loads(self.cli("status").stdout)
        self.assertTrue(report["tree_matches"])

        self.record("coder")
        (self.repo / "new.txt").write_text("untracked\n", encoding="utf-8")
        rejected = self.record("tester", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.resume()
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["results"], {})

    def test_fingerprint_accepts_clean_initialized_submodules_and_detects_recursive_mutations(
        self,
    ):
        leaf_source = self.make_submodule_source("leaf-source")
        child_source = self.make_submodule_source("child-source")
        subprocess.run(
            [
                "git",
                "-C",
                str(child_source),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(leaf_source),
                "nested",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(child_source), "commit", "-qm", "nested"], check=True
        )
        self.add_submodule("child", child_source)
        self.git("commit", "-qm", "child submodule")
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        )
        self.init_manifest_at_head()
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])

        cases = (
            ("gitlink-head", self.repo / "child"),
            ("index", self.repo / "child" / "module.txt"),
            ("tracked-worktree", self.repo / "child" / "module.txt"),
            ("untracked", self.repo / "child" / "untracked.txt"),
            ("nested-untracked", self.repo / "child" / "nested" / "untracked.txt"),
        )
        for name, target in cases:
            with self.subTest(name=name):
                self.git(
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                )
                subprocess.run(
                    ["git", "-C", str(self.repo / "child"), "reset", "--hard", "-q"],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.repo / "child"), "clean", "-fdq"], check=True
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repo / "child" / "nested"),
                        "reset",
                        "--hard",
                        "-q",
                    ],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.repo / "child" / "nested"), "clean", "-fdq"],
                    check=True,
                )
                if name == "gitlink-head":
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target),
                            "-c",
                            "user.name=State Tester",
                            "-c",
                            "user.email=tester@example.invalid",
                            "commit",
                            "--allow-empty",
                            "-qm",
                            "same tree new HEAD",
                        ],
                        check=True,
                    )
                elif name == "index":
                    target.write_text("staged\n", encoding="utf-8")
                    subprocess.run(
                        ["git", "-C", str(target.parent), "add", target.name],
                        check=True,
                    )
                elif name == "tracked-worktree":
                    target.write_text("changed\n", encoding="utf-8")
                else:
                    target.write_text("untracked\n", encoding="utf-8")
                self.assertFalse(
                    json.loads(self.cli("status").stdout)["tree_matches"], name
                )

    def test_uninitialized_gitlink_accepts_missing_or_empty_but_not_hidden_content(
        self,
    ):
        self.git(
            "update-index", "--add", "--cacheinfo", f"160000,{self.base},modules/child"
        )
        self.git("commit", "-qm", "uninitialized gitlink")
        self.init_manifest_at_head("absent.json")
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])
        child = self.repo / "modules" / "child"
        child.mkdir(parents=True)
        self.init_manifest_at_head("empty.json")
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])
        (child / "hidden.txt").write_text("untracked content\n")
        self.assertNotEqual(self.cli("status", check=False).returncode, 0)

    def test_old_form_submodule_is_supported_and_ancestor_symlink_is_rejected(self):
        source = self.make_submodule_source("old-form-source")
        child = self.repo / "modules" / "child"
        child.parent.mkdir()
        subprocess.run(["git", "clone", "-q", str(source), str(child)], check=True)
        self.assertTrue((child / ".git").is_dir())
        self.git("add", "modules/child")
        self.git("commit", "-qm", "old form gitlink")
        self.init_manifest_at_head()
        self.assertTrue(json.loads(self.cli("status").stdout)["tree_matches"])
        outside = Path(self.tmp.name) / "moved-modules"
        child.parent.rename(outside)
        child.parent.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.cli("status", check=False).returncode, 0)

    def test_gitlink_boundary_frames_identical_untracked_entries(self):
        source = self.make_submodule_source("framed-source")
        self.add_submodule("zchild", source)
        self.git("commit", "-qm", "child submodule")
        child_file = self.repo / "zchild" / "same.txt"
        child_file.write_text("identical untracked bytes\n", encoding="utf-8")
        child_manifest = Path(self.tmp.name) / "child-state.json"
        child = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(child_manifest),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "child-state",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        child_file.unlink()
        (self.repo / "same.txt").write_text(
            "identical untracked bytes\n", encoding="utf-8"
        )
        parent_manifest = Path(self.tmp.name) / "parent-state.json"
        parent = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(parent_manifest),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "parent-state",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(parent.returncode, 0, parent.stderr)
        self.assertNotEqual(
            json.loads(child_manifest.read_text())["tree_fingerprint"],
            json.loads(parent_manifest.read_text())["tree_fingerprint"],
        )

    def test_submodule_cannot_route_through_external_common_directory(self):
        source = self.make_submodule_source("external-common-source")
        child = self.repo / "child"
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "--detach",
                str(child),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        original = Path(
            subprocess.check_output(
                ["git", "-C", str(child), "rev-parse", "--git-dir"], text=True
            ).strip()
        )
        expected = self.repo / ".git" / "modules" / "child"
        expected.parent.mkdir()
        original.rename(expected)
        (expected / "commondir").write_text(str(source / ".git") + "\n")
        (child / ".git").write_text("gitdir: ../.git/modules/child\n")
        oid = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},child")
        (self.repo / ".gitmodules").write_text(
            '[submodule "child"]\n\tpath = child\n\turl = ../external-common-source\n'
        )
        self.git("add", ".gitmodules")
        self.git("commit", "-qm", "routed gitlink")
        self.manifest = Path(self.tmp.name) / "routed.json"
        result = self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        requirements = Path(self.tmp.name) / "requirements.txt"
        requirements.write_text("synthetic requirements\n")
        packet = Path(self.tmp.name) / "packet.json"
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "skills/superarmanda/scripts/review.py"),
                "packet",
                "--repo",
                str(self.repo),
                "--base",
                self.head(),
                "--head",
                self.head(),
                "--requirements",
                str(requirements),
                "--test-evidence",
                str(requirements),
                "--output",
                str(packet),
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(packet.exists())

    def test_rejects_one_session_id_for_two_required_roles(self):
        self.record("coder", session="shared")
        duplicate = self.record("tester", session="shared", check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("session_id", duplicate.stderr)

    def test_repo_subdirectory_is_canonicalized_and_sibling_changes_invalidate(self):
        subdir = self.repo / "nested"
        subdir.mkdir()
        (subdir / ".keep").write_text("nested\n", encoding="utf-8")
        self.git("add", "nested/.keep")
        self.git("commit", "-qm", "add nested directory")
        manifest = Path(self.tmp.name) / "subdir-run.json"
        init = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(manifest),
                "--repo",
                str(subdir),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "subdir",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(init.returncode, 0, init.stderr)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["repo"], str(self.repo.resolve()))

        self.manifest = manifest
        self.record("coder", session="subdir-coder")
        (self.repo / "sibling-untracked.txt").write_text("mutation\n", encoding="utf-8")
        rejected = self.record("tester", session="subdir-tester", check=False)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        resumed = self.resume()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["results"], {})

    def test_manifest_in_sibling_of_subdirectory_must_be_ignored_by_git_root(self):
        subdir = self.repo / "nested"
        subdir.mkdir()
        (subdir / ".keep").write_text("nested\n", encoding="utf-8")
        self.git("add", "nested/.keep")
        self.git("commit", "-qm", "add nested directory")
        manifest = self.repo / "sibling-run.json"
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(manifest),
                "--repo",
                str(subdir),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "unsafe-manifest",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(manifest.exists())

    def test_untracked_executable_bit_is_part_of_the_fingerprint(self):
        script = self.repo / "local-hook.sh"
        script.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
        script.chmod(0o644)
        self.resume()
        self.record("coder", session="mode-coder")
        script.chmod(0o755)
        rejected = self.record("tester", session="mode-tester", check=False)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)

    def test_v3_regular_file_hash_streams_with_the_existing_framing(self):
        spec = importlib.util.spec_from_file_location("state_streaming", STATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.repo / "tracked.txt"
        oid = self.git("ls-files", "-s", path.name).stdout.split()[1].encode()
        expected = hashlib.sha256()
        expected.update(b"superarmanda-tree-fingerprint-v3\0")
        module.digest_field(expected, b"tracked-path", b"tracked.txt")
        module.digest_field(expected, b"tracked-mode", b"100644")
        module.digest_field(expected, b"tracked-oid", oid)
        mode = path.lstat().st_mode
        module.digest_field(
            expected,
            b"mode",
            f"{stat.S_IFMT(mode)}:{mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)}".encode(),
        )
        module.digest_field(expected, b"file", b"base\n")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError):
            self.assertEqual(module.fingerprint(self.repo), expected.hexdigest())

    def test_v3_regular_file_hash_reads_in_chunks_and_rejects_length_change(self):
        spec = importlib.util.spec_from_file_location("state_streaming", STATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = self.repo / "tracked.txt"
        original_fdopen = module.os.fdopen
        reads = []

        class Reader:
            def __init__(self, file, mutate=False):
                self.file, self.mutate = file, mutate
                self.changed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def fileno(self):
                return self.file.fileno()

            def read(self, count=-1):
                reads.append(count)
                value = self.file.read(count)
                if self.mutate and not self.changed:
                    path.write_bytes(path.read_bytes() + b"changed")
                    self.changed = True
                return value

        def bounded_fdopen(descriptor, mode):
            return Reader(original_fdopen(descriptor, mode))

        digest = hashlib.sha256()
        with mock.patch.object(module.os, "fdopen", side_effect=bounded_fdopen):
            module._hash_worktree_entry(digest, path, self.repo)
        self.assertTrue(reads)
        self.assertTrue(all(0 < count <= 64 * 1024 for count in reads))

        def changing_fdopen(descriptor, mode):
            return Reader(original_fdopen(descriptor, mode), mutate=True)

        with mock.patch.object(module.os, "fdopen", side_effect=changing_fdopen):
            with self.assertRaises(SystemExit):
                module._hash_worktree_entry(hashlib.sha256(), path, self.repo)

    def test_v3_rejects_tracked_file_below_a_symlinked_parent(self):
        spec = importlib.util.spec_from_file_location("state_boundary", STATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        directory = self.repo / "tracked-directory"
        directory.mkdir()
        (directory / "file.txt").write_text("inside\n", encoding="utf-8")
        self.git("add", "tracked-directory/file.txt")
        self.git("commit", "-qm", "tracked nested file")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("outside\n", encoding="utf-8")
        (directory / "file.txt").unlink()
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SystemExit):
            module.fingerprint(self.repo)

    def test_session_identity_is_unique_for_the_whole_run_but_retries_are_allowed(self):
        self.record("coder", session="coder-a")
        # Retrying the same task and role is allowed for an interrupted command.
        self.record("coder", session="coder-a")
        other_task = self.cli(
            "task-result",
            "--task",
            "verify",
            "--role",
            "tester",
            "--status",
            "pass",
            "--session-id",
            "coder-a",
            "--head",
            self.head(),
            "--artifact",
            "evidence",
            check=False,
        )
        self.assertNotEqual(
            other_task.returncode, 0, other_task.stdout + other_task.stderr
        )
        same_role_other_task = self.cli(
            "task-result",
            "--task",
            "verify",
            "--role",
            "coder",
            "--status",
            "pass",
            "--session-id",
            "coder-a",
            "--head",
            self.head(),
            "--artifact",
            "evidence",
            check=False,
        )
        self.assertNotEqual(
            same_role_other_task.returncode,
            0,
            same_role_other_task.stdout + same_role_other_task.stderr,
        )
        (self.repo / "tracked.txt").write_text("resume mutation\n", encoding="utf-8")
        self.resume()
        after_resume = self.cli(
            "task-result",
            "--task",
            "verify",
            "--role",
            "tester",
            "--status",
            "pass",
            "--session-id",
            "coder-a",
            "--head",
            self.head(),
            "--artifact",
            "evidence",
            check=False,
        )
        self.assertNotEqual(
            after_resume.returncode, 0, after_resume.stdout + after_resume.stderr
        )

    def test_legacy_v1_fingerprint_is_stale_and_its_session_history_is_migrated(self):
        legacy_session = "legacy-coder"
        old_fingerprint = hashlib.sha256(b"").hexdigest()
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": "legacy",
                    "repo": str(self.repo.resolve()),
                    "base": self.base,
                    "head": self.head(),
                    "tree_fingerprint": old_fingerprint,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "tasks": {
                        "implement": {
                            "status": "ready_for_pr_review",
                            "fix_cycles": 0,
                            "session_roles": {legacy_session: "coder"},
                            "results": {
                                "coder": {
                                    "status": "pass",
                                    "head": self.head(),
                                    "session_id": legacy_session,
                                    "artifact": "evidence",
                                    "reviewed_head": None,
                                    "packet_hash": None,
                                    "tree_fingerprint": old_fingerprint,
                                    "recorded_at": "2026-01-01T00:00:00+00:00",
                                },
                                "tester": {
                                    "status": "pass",
                                    "head": self.head(),
                                    "session_id": "legacy-tester",
                                    "artifact": "evidence",
                                    "reviewed_head": None,
                                    "packet_hash": None,
                                    "tree_fingerprint": old_fingerprint,
                                    "recorded_at": "2026-01-01T00:00:00+00:00",
                                },
                                "cross_provider_reviewer": {
                                    "status": "pass",
                                    "head": self.head(),
                                    "session_id": "legacy-reviewer",
                                    "artifact": "evidence",
                                    "reviewed_head": self.head(),
                                    "packet_hash": "sha256:" + "a" * 64,
                                    "tree_fingerprint": old_fingerprint,
                                    "recorded_at": "2026-01-01T00:00:00+00:00",
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        report = json.loads(self.cli("status").stdout)
        self.assertFalse(report["tree_matches"])
        stale_fix_loop = self.cli(
            "fix-loop", "--task", "implement", "--outcome", "pass", check=False
        )
        self.assertNotEqual(
            stale_fix_loop.returncode,
            0,
            stale_fix_loop.stdout + stale_fix_loop.stderr,
        )
        stale = self.record("tester", session="new-tester", check=False)
        self.assertNotEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        self.resume()
        for session, role in (
            (legacy_session, "tester"),
            ("legacy-tester", "coder"),
            ("legacy-reviewer", "tester"),
        ):
            with self.subTest(session=session):
                reused = self.cli(
                    "task-result",
                    "--task",
                    f"other-{role}",
                    "--role",
                    role,
                    "--status",
                    "pass",
                    "--session-id",
                    session,
                    "--head",
                    self.head(),
                    "--artifact",
                    "evidence",
                    check=False,
                )
                self.assertNotEqual(reused.returncode, 0, reused.stdout + reused.stderr)

    def test_conflicting_legacy_per_task_session_ownership_stops_resume_without_rewrite(
        self,
    ):
        shared = "legacy-conflict"
        data = self.manifest_data()
        data["tasks"] = {
            "implement": {
                "status": "pending",
                "fix_cycles": 0,
                "results": {},
                "session_roles": {shared: "coder"},
            },
            "verify": {
                "status": "pending",
                "fix_cycles": 0,
                "results": {},
                "session_roles": {shared: "tester"},
            },
        }
        self.manifest.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        before = self.manifest.read_bytes()
        resumed = self.resume(check=False)
        self.assertNotEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_legacy_subdir_repo_with_unignored_root_manifest_rejects_before_lock_or_write(
        self,
    ):
        subdir = self.repo / "nested"
        subdir.mkdir()
        (subdir / ".keep").write_text("nested\n", encoding="utf-8")
        self.git("add", "nested/.keep")
        self.git("commit", "-qm", "add nested directory")
        manifest = self.repo / "legacy-root-manifest.json"
        legacy = self.manifest_data()
        legacy["repo"] = str(subdir.resolve())
        legacy["head"] = self.head()
        legacy["tree_fingerprint"] = hashlib.sha256(b"").hexdigest()
        manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
        for command in (
            ("status",),
            (
                "fix-loop",
                "--task",
                "implement",
                "--outcome",
                "failed",
                "--source",
                "tester",
            ),
        ):
            with self.subTest(command=command[0]):
                manifest.write_text(
                    json.dumps(legacy, sort_keys=True), encoding="utf-8"
                )
                before = manifest.read_bytes()
                proc = subprocess.run(
                    [
                        "python3",
                        str(STATE),
                        command[0],
                        "--manifest",
                        str(manifest),
                        *command[1:],
                    ],
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(manifest.read_bytes(), before)
                self.assertFalse(manifest.with_name(f".{manifest.name}.lock").exists())

    def test_fix_loop_pass_cannot_restore_ready_state_after_head_or_tree_changes(self):
        self.record("coder", session="fix-coder")
        self.record("tester", session="fix-tester")
        self.record(
            "cross_provider_reviewer",
            session="fix-reviewer",
            reviewed_head=self.head(),
            packet_hash="sha256:" + "b" * 64,
        )
        self.git("commit", "--allow-empty", "-qm", "unreviewed commit")
        stale = self.cli(
            "fix-loop", "--task", "implement", "--outcome", "pass", check=False
        )
        self.assertNotEqual(stale.returncode, 0, stale.stdout + stale.stderr)

    def test_session_id_cannot_change_roles_after_resume_invalidates_old_result(self):
        self.record("coder", session="shared")
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.resume()
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["results"], {})

        # Invalidating evidence never makes the former coder session a valid
        # independent tester.  Role/session separation belongs to the run,
        # rather than only the currently retained result map.
        reused = self.record("tester", session="shared", check=False)
        self.assertNotEqual(reused.returncode, 0, reused.stdout + reused.stderr)

    def test_rejects_forged_reviewed_head_and_persists_matching_packet_hash(self):
        self.git("commit", "--allow-empty", "-qm", "review target")
        new_head = self.head()
        self.resume()

        # The old base is a real commit, so this proves the CLI verifies the
        # review target rather than merely accepting syntactically valid SHA.
        forged = self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            reviewed_head=self.base,
            packet_hash="sha256:forged",
            check=False,
        )
        self.assertNotEqual(forged.returncode, 0, forged.stdout + forged.stderr)

        packet_hash = "sha256:" + "a" * 64
        self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            reviewed_head=new_head,
            packet_hash=packet_hash,
        )
        result = self.manifest_data()["tasks"]["implement"]["results"][
            "cross_provider_reviewer"
        ]
        self.assertEqual(
            (result["reviewed_head"], result["packet_hash"]), (new_head, packet_hash)
        )

    def test_reviewer_pass_requires_reviewed_head_and_packet_hash(self):
        missing_head = self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            packet_hash="sha256:" + "c" * 64,
            check=False,
        )
        self.assertNotEqual(
            missing_head.returncode, 0, missing_head.stdout + missing_head.stderr
        )
        missing_hash = self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            reviewed_head=self.head(),
            check=False,
        )
        self.assertNotEqual(
            missing_hash.returncode, 0, missing_hash.stdout + missing_hash.stderr
        )

    def test_rejects_malformed_manifest_and_stale_head_result(self):
        self.manifest.write_text('{"version": 1, "tasks": []}', encoding="utf-8")
        malformed = self.cli("status", check=False)
        self.assertNotEqual(malformed.returncode, 0)

        # Restore a valid manifest, then prove task results cannot be recorded
        # against an obsolete reviewed head.
        self.manifest.unlink()
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.base,
            "--run-id",
            "two",
        )
        self.git("commit", "--allow-empty", "-qm", "later")
        stale = self.record("coder", head=self.base, check=False)
        self.assertNotEqual(stale.returncode, 0)

    def test_coder_or_optional_coderabbit_pass_cannot_complete_task(self):
        self.record("coder")
        self.assertEqual(
            self.manifest_data()["tasks"]["implement"]["status"], "in_progress"
        )
        self.record("coderabbit")
        self.assertNotEqual(
            self.manifest_data()["tasks"]["implement"]["status"], "ready_for_pr_review"
        )

    def test_all_three_required_distinct_role_passes_only_make_pr_review_ready(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record(
            "cross_provider_reviewer",
            session="reviewer-1",
            reviewed_head=self.head(),
            packet_hash="sha256:" + "d" * 64,
        )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertNotIn("complete", entry["status"])

    def test_three_failed_fix_cycles_stay_blocked_across_resume(self):
        # Three distinct sources keep every per-source counter below the
        # needs_decision threshold, so this exercises the unchanged global
        # cap in isolation from the new per-source gate.
        for source in ("tester", "github_codex_review", "coderabbit"):
            self.cli(
                "fix-loop",
                "--task",
                "implement",
                "--outcome",
                "failed",
                "--source",
                source,
            )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual((entry["status"], entry["fix_cycles"]), ("blocked", 3))
        self.resume()
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual((entry["status"], entry["fix_cycles"]), ("blocked", 3))
        retry = self.cli(
            "fix-loop", "--task", "implement", "--outcome", "pass", check=False
        )
        self.assertNotEqual(retry.returncode, 0)

    def test_fix_loop_failed_requires_a_known_source(self):
        missing = self.cli(
            "fix-loop", "--task", "implement", "--outcome", "failed", check=False
        )
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)

        unknown = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "coder",
            check=False,
        )
        self.assertNotEqual(unknown.returncode, 0, unknown.stdout + unknown.stderr)

    def test_fix_loop_pass_rejects_source(self):
        rejected = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "pass",
            "--source",
            "tester",
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)

    def test_second_failed_from_same_source_needs_decision_and_blocks_progress(self):
        self.record("coder", session="coder-before-decision")

        first = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.assertEqual(json.loads(first.stdout)["status"], "needs_fix")

        second = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = json.loads(second.stdout)
        self.assertEqual(entry["status"], "needs_decision")
        self.assertEqual(entry["decision_required_for"], "tester")
        self.assertEqual(entry["fix_sources"]["tester"], 2)

        # In needs_decision, task-result and both fix-loop outcomes are all
        # rejected regardless of role or outcome direction.
        blocked_result = self.record("tester", check=False)
        self.assertNotEqual(
            blocked_result.returncode, 0, blocked_result.stdout + blocked_result.stderr
        )
        blocked_pass = self.cli(
            "fix-loop", "--task", "implement", "--outcome", "pass", check=False
        )
        self.assertNotEqual(
            blocked_pass.returncode, 0, blocked_pass.stdout + blocked_pass.stderr
        )
        blocked_failed = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
            check=False,
        )
        self.assertNotEqual(
            blocked_failed.returncode, 0, blocked_failed.stdout + blocked_failed.stderr
        )

        # resume() must not clear needs_decision even though it invalidates
        # the now-stale coder result for the changed tree.
        (self.repo / "tracked.txt").write_text("resume mutation\n", encoding="utf-8")
        self.resume()
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "needs_decision")
        self.assertEqual(entry["decision_required_for"], "tester")
        self.assertEqual(entry["fix_sources"]["tester"], 2)
        self.assertEqual(entry["results"], {})

    def test_decision_requires_needs_decision_state_and_a_valid_note(self):
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        too_early = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "ok",
            check=False,
        )
        self.assertNotEqual(too_early.returncode, 0, too_early.stdout + too_early.stderr)

        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "needs_decision")

        empty_note = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "   ",
            check=False,
        )
        self.assertNotEqual(
            empty_note.returncode, 0, empty_note.stdout + empty_note.stderr
        )

        long_note = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "x" * 501,
            check=False,
        )
        self.assertNotEqual(long_note.returncode, 0, long_note.stdout + long_note.stderr)

        multiline_note = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "line one\nline two",
            check=False,
        )
        self.assertNotEqual(
            multiline_note.returncode, 0, multiline_note.stdout + multiline_note.stderr
        )

        both_flags = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--decision",
            "invariant",
            "--note",
            "ok",
            check=False,
        )
        self.assertNotEqual(
            both_flags.returncode, 0, both_flags.stdout + both_flags.stderr
        )

        accepted = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "keep the retry budget, this is the same nil-check edge",
        )
        entry = json.loads(accepted.stdout)
        self.assertEqual(entry["status"], "needs_fix")
        self.assertIsNone(entry["decision_required_for"])
        self.assertEqual(len(entry["decisions"]), 1)
        self.assertEqual(entry["decisions"][0]["source"], "tester")
        self.assertEqual(entry["decisions"][0]["decision"], "invariant")

    def test_decision_source_must_match_decision_required_for(self):
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "needs_decision")
        self.assertEqual(entry["decision_required_for"], "tester")

        mismatched = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "wrong source guess",
            "--source",
            "coderabbit",
            check=False,
        )
        self.assertNotEqual(
            mismatched.returncode, 0, mismatched.stdout + mismatched.stderr
        )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "needs_decision")
        self.assertEqual(entry["decision_required_for"], "tester")
        self.assertEqual(entry["decisions"], [])

        matched = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "invariant",
            "--note",
            "matches the pending source",
            "--source",
            "tester",
        )
        entry = json.loads(matched.stdout)
        self.assertEqual(entry["status"], "needs_fix")
        self.assertIsNone(entry["decision_required_for"])
        self.assertEqual(entry["decisions"][0]["source"], "tester")

    def test_status_preserves_needs_decision_and_decision_required_for_on_changed_tree(
        self,
    ):
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "needs_decision")

        before = self.manifest.read_bytes()
        (self.repo / "tracked.txt").write_text(
            "status changed tree\n", encoding="utf-8"
        )
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "change tree without resume")

        report = json.loads(self.cli("status").stdout)
        self.assertFalse(report["tree_matches"])
        reported = report["tasks"]["implement"]
        self.assertEqual(reported["status"], "needs_decision")
        self.assertEqual(reported["decision_required_for"], "tester")

        # status is read-only: the manifest on disk must be byte-identical.
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_third_failed_call_from_the_same_source_hits_the_global_cap_after_a_decision(
        self,
    ):
        # One decision buys exactly one more round; the very next failed call
        # is also the third GLOBAL fix cycle, so the unchanged 3-cycle cap
        # wins over a second needs_decision for the same source.
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--decision",
            "cut_surface",
            "--note",
            "trim the surface tester keeps re-flagging",
        )
        third = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = json.loads(third.stdout)
        self.assertEqual((entry["status"], entry["fix_cycles"]), ("blocked", 3))
        self.assertEqual(entry["fix_sources"]["tester"], 3)

    def test_third_failed_call_from_a_different_source_still_hits_the_global_cap(self):
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "github_codex_review",
        )
        third = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = json.loads(third.stdout)
        # tester's own per-source count reaches 2 on this very call, which
        # would normally require a decision, but it is also the third global
        # cycle, and the cap takes priority.
        self.assertEqual((entry["status"], entry["fix_cycles"]), ("blocked", 3))
        self.assertEqual(entry["fix_sources"]["tester"], 2)

    def test_legacy_manifest_without_fix_sources_accepts_failed_with_source(self):
        data = self.manifest_data()
        data["tasks"] = {
            "implement": {
                "status": "pending",
                "fix_cycles": 0,
                "results": {},
                "session_roles": {},
            }
        }
        self.manifest.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        result = self.cli(
            "fix-loop",
            "--task",
            "implement",
            "--outcome",
            "failed",
            "--source",
            "tester",
        )
        entry = json.loads(result.stdout)
        self.assertEqual(entry["status"], "needs_fix")
        self.assertEqual(entry["fix_sources"], {"tester": 1})
        self.assertEqual(entry["decisions"], [])
        self.assertIsNone(entry["decision_required_for"])

    def test_manifest_binds_one_run_to_its_repo_base_and_head(self):
        data = self.manifest_data()
        self.assertEqual(
            (data["run_id"], data["repo"], data["base"], data["head"]),
            ("one", str(self.repo.resolve()), self.base, self.base),
        )
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        mismatch = self.cli(
            "resume",
            "--repo",
            other,
            "--base",
            self.base,
            "--head",
            self.base,
            check=False,
        )
        self.assertNotEqual(mismatch.returncode, 0)

    def test_manifest_lock_aliases_are_rejected_before_every_lifecycle_operation(self):
        """Lock acquisition must not open a hardlink to unrelated tracked data."""
        victim = self.repo / "lock-victim.txt"
        victim.write_text("lock victim bytes\n", encoding="utf-8")
        victim.chmod(0o640)
        self.git("add", "lock-victim.txt")
        self.git("commit", "-qm", "lock victim")
        # The initial manifest belongs to the old HEAD, which also gives resume
        # and task-result a realistic stale-state setup without touching victim.
        before, mode = victim.read_bytes(), victim.stat().st_mode
        lock = self.manifest.with_name(f".{self.manifest.name}.lock")
        for label, args in (
            ("status", ("status",)),
            (
                "resume",
                (
                    "resume",
                    "--repo",
                    self.repo,
                    "--base",
                    self.base,
                    "--head",
                    self.head(),
                ),
            ),
            (
                "task-result",
                (
                    "task-result",
                    "--task",
                    "implement",
                    "--role",
                    "coder",
                    "--status",
                    "pass",
                    "--session-id",
                    "lock-coder",
                    "--head",
                    self.head(),
                    "--artifact",
                    "evidence",
                ),
            ),
            (
                "fix-loop",
                (
                    "fix-loop",
                    "--task",
                    "implement",
                    "--outcome",
                    "failed",
                    "--source",
                    "tester",
                ),
            ),
        ):
            with self.subTest(operation=label):
                if lock.exists() or lock.is_symlink():
                    lock.unlink()
                os.link(victim, lock)
                proc = self.cli(*args, check=False)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(victim.read_bytes(), before)
                self.assertEqual(victim.stat().st_mode, mode)
        if lock.exists() or lock.is_symlink():
            lock.unlink()

    def test_init_rejects_a_preexisting_hardlinked_lock_without_writing_manifest(self):
        manifest = Path(self.tmp.name) / "new-run.json"
        lock = manifest.with_name(f".{manifest.name}.lock")
        victim = self.repo / "init-lock-victim.txt"
        victim.write_text("init lock victim\n", encoding="utf-8")
        victim.chmod(0o600)
        self.git("add", "init-lock-victim.txt")
        self.git("commit", "-qm", "init lock victim")
        before, mode = victim.read_bytes(), victim.stat().st_mode
        os.link(victim, lock)
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(manifest),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "hardlinked-lock",
            ],
            text=True,
            capture_output=True,
        )
        try:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(manifest.exists())
            self.assertEqual(victim.read_bytes(), before)
            self.assertEqual(victim.stat().st_mode, mode)
        finally:
            if lock.exists() or lock.is_symlink():
                lock.unlink()

    def test_v2_fingerprint_evidence_is_invalidated_by_v3_migration_without_losing_history(
        self,
    ):
        self.record("coder", session="legacy-v2-coder")
        legacy = self.manifest_data()
        # Synthesize the documented v2 domain explicitly.  The migration test
        # must not accidentally use whichever fingerprint the current program
        # happens to emit during setup.
        v2 = hashlib.sha256()
        v2.update(b"superarmanda-tree-fingerprint-v2\0")
        v2.update(
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "diff",
                    "--no-ext-diff",
                    "--binary",
                    "HEAD",
                ]
            )
        )
        legacy["tree_fingerprint"] = v2.hexdigest()
        legacy["tasks"]["implement"]["results"]["coder"]["tree_fingerprint"] = (
            v2.hexdigest()
        )
        legacy["tasks"]["implement"]["fix_cycles"] = 2
        legacy["tasks"]["implement"]["status"] = "needs_fix"
        self.manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

        resumed = self.resume()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        migrated = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(migrated["results"], {})
        self.assertEqual(migrated["fix_cycles"], 2)
        self.assertEqual(migrated["session_roles"], {"legacy-v2-coder": "coder"})
        self.assertEqual(migrated["status"], "pending")

    def test_legacy_missing_session_roles_is_restored_before_resume_invalidation(self):
        self.record("coder", session="legacy-missing-map")
        legacy = self.manifest_data()
        del legacy["tasks"]["implement"]["session_roles"]
        self.manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

        self.git("commit", "--allow-empty", "-qm", "new head invalidates legacy")
        resumed = self.resume()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        migrated = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(migrated["results"], {})
        self.assertEqual(migrated["session_roles"], {"legacy-missing-map": "coder"})
        reused = self.record("tester", session="legacy-missing-map", check=False)
        self.assertNotEqual(reused.returncode, 0, reused.stdout + reused.stderr)

    def test_explicit_null_legacy_session_roles_remains_malformed(self):
        legacy = self.manifest_data()
        legacy["tasks"] = {
            "implement": {
                "status": "pending",
                "fix_cycles": 0,
                "session_roles": None,
                "results": {},
            }
        }
        self.manifest.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
        before = self.manifest.read_bytes()
        report = self.cli("status", check=False)
        self.assertNotEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_fingerprint_uses_real_tracked_bytes_despite_assume_unchanged_textconv(
        self,
    ):
        source = self.repo / "fingerprint.txt"
        source.write_text("committed\n", encoding="utf-8")
        attributes = self.repo / ".gitattributes"
        attributes.write_text("fingerprint.txt diff=constant\n", encoding="utf-8")
        self.git("add", "fingerprint.txt", ".gitattributes")
        self.git("commit", "-qm", "fingerprint textconv fixture")
        self.resume()
        self.record("coder", session="fingerprint-coder")
        driver_log = Path(self.tmp.name) / "fingerprint-textconv-ran"
        driver = Path(self.tmp.name) / "constant-textconv"
        driver.write_text(
            "#!/bin/sh\nprintf ran > \"$SA_FINGERPRINT_TEXTCONV_LOG\"\nprintf 'constant output\\n'\n",
            encoding="utf-8",
        )
        driver.chmod(0o755)
        self.git("config", "diff.constant.textconv", str(driver))
        source.write_text("WORKTREE ONLY\n", encoding="utf-8")
        self.git("update-index", "--assume-unchanged", "fingerprint.txt")
        try:
            env = os.environ.copy()
            env["SA_FINGERPRINT_TEXTCONV_LOG"] = str(driver_log)
            proc = subprocess.run(
                ["python3", str(STATE), "status", "--manifest", str(self.manifest)],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["tree_matches"])
            self.assertFalse(
                driver_log.exists(), "fingerprint executed configured textconv"
            )
        finally:
            self.git("update-index", "--no-assume-unchanged", "fingerprint.txt")

    def test_ignored_directory_manifest_is_allowed_but_lexical_symlink_aliases_are_not(
        self,
    ):
        (self.repo / ".gitignore").write_text(
            ".superarmanda/\nignored-manifest-link\nignored-manifest-parent\n",
            encoding="utf-8",
        )
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignored manifest fixtures")
        direct = self.repo / ".superarmanda" / "run.json"

        def invoke(manifest, command, *args):
            return subprocess.run(
                [
                    "python3",
                    str(STATE),
                    command,
                    "--manifest",
                    str(manifest),
                    *map(str, args),
                ],
                text=True,
                capture_output=True,
            )

        initialized = invoke(
            direct,
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
            "--run-id",
            "ignored-direct",
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(invoke(direct, "status").returncode, 0)
        self.assertEqual(
            invoke(
                direct,
                "resume",
                "--repo",
                self.repo,
                "--base",
                self.base,
                "--head",
                self.head(),
            ).returncode,
            0,
        )

        external = Path(self.tmp.name) / "external-manifests"
        external.mkdir()
        (external / "run.json").symlink_to(self.manifest)
        final_link = self.repo / "ignored-manifest-link"
        final_link.symlink_to(self.manifest)
        parent_link = self.repo / "ignored-manifest-parent"
        parent_link.symlink_to(external, target_is_directory=True)
        for label, manifest, lock in (
            ("final", final_link, final_link.with_name(".ignored-manifest-link.lock")),
            ("parent", parent_link / "run.json", external / ".run.json.lock"),
        ):
            with self.subTest(alias=label):
                before = self.manifest.read_bytes()
                proc = invoke(manifest, "status")
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.manifest.read_bytes(), before)
                self.assertFalse(lock.exists() or lock.is_symlink())

    def test_external_symlink_to_tracked_manifest_is_rejected_without_lock_artifacts(
        self,
    ):
        data = self.manifest_data()
        tracked = self.repo / "tracked-manifest.json"
        tracked.write_text(json.dumps(data), encoding="utf-8")
        self.git("add", "tracked-manifest.json")
        self.git("commit", "-qm", "tracked manifest target")
        alias = Path(self.tmp.name) / "external-to-tracked-manifest.json"
        alias.symlink_to(tracked)
        lock = alias.with_name(".external-to-tracked-manifest.json.lock")
        proc = subprocess.run(
            ["python3", str(STATE), "status", "--manifest", str(alias)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(lock.exists() or lock.is_symlink())
        self.assertEqual(tracked.read_text(encoding="utf-8"), json.dumps(data))

    def test_lock_final_symlinks_and_nonregular_endpoints_fail_without_following_or_blocking(
        self,
    ):
        lock = self.manifest.with_name(f".{self.manifest.name}.lock")
        if lock.exists() or lock.is_symlink():
            lock.unlink()
        victim = Path(self.tmp.name) / "lock-symlink-victim"
        victim.write_text("preserve external lock target\n", encoding="utf-8")
        before = victim.read_bytes()
        for label, target in (
            ("existing", victim),
            ("dangling", Path(self.tmp.name) / "must-not-create"),
        ):
            with self.subTest(kind=label):
                if lock.exists() or lock.is_symlink():
                    lock.unlink()
                lock.symlink_to(target)
                try:
                    proc = self.cli("status", check=False)
                    self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(victim.read_bytes(), before)
                    self.assertFalse(label == "dangling" and target.exists())
                finally:
                    if lock.exists() or lock.is_symlink():
                        lock.unlink()

        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO fixtures are unavailable on this platform")
        if lock.exists() or lock.is_symlink():
            lock.unlink()
        os.mkfifo(lock)
        process = subprocess.Popen(
            ["python3", str(STATE), "status", "--manifest", str(self.manifest)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("state status blocked while opening a FIFO lock")
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
        finally:
            if lock.exists() or lock.is_symlink():
                lock.unlink()

    def test_gitignored_lock_hardlinked_to_tracked_file_is_still_rejected(self):
        (self.repo / ".gitignore").write_text(".runs/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore run directory")
        manifest = self.repo / ".runs" / "run.json"
        lock = manifest.with_name(".run.json.lock")
        victim = self.repo / "tracked-lock-target.txt"
        victim.write_text("tracked lock target\n", encoding="utf-8")
        self.git("add", "tracked-lock-target.txt")
        self.git("commit", "-qm", "tracked lock target")
        before = victim.read_bytes()
        manifest.parent.mkdir()
        os.link(victim, lock)
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(manifest),
                "--repo",
                str(self.repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "ignored-hardlink-lock",
            ],
            text=True,
            capture_output=True,
        )
        try:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(manifest.exists())
            self.assertEqual(victim.read_bytes(), before)
        finally:
            if lock.exists() or lock.is_symlink():
                lock.unlink()

    def test_manifest_path_with_dotdot_through_symlink_is_rejected_before_locking(self):
        tracked = self.repo / "run.json"
        tracked.write_text(self.manifest.read_text(encoding="utf-8"), encoding="utf-8")
        self.git("add", "run.json")
        self.git("commit", "-qm", "tracked manifest victim")
        nested = self.repo / "sub"
        nested.mkdir()
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        supplied = alias / ".." / "run.json"
        lock = self.repo / ".run.json.lock"
        proc = subprocess.run(
            ["python3", str(STATE), "status", "--manifest", str(supplied)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(lock.exists() or lock.is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
