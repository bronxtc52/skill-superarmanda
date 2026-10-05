#!/usr/bin/env python3
"""Contract tests for the local Superarmanda state CLI.

The fixture is created from scratch for every test.  It deliberately calls the
CLI as an outside client, so this verifies the public command contract rather
than internal helper functions.
"""

import copy
import json
import importlib.util
import os
import hashlib
import itertools
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "skills" / "superarmanda" / "scripts" / "state.py"

# A wave dispatcher exports WAB_DIR/WAB_MAX_RUNS; the run counter of `init --from-plan`
# must not leak into fixtures that did not ask for it.
for _key in [k for k in os.environ if k.startswith("WAB_")]:
    os.environ.pop(_key, None)


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

    def reviewer_record(self, packet_hash, session, check=True):
        self.git("commit", "--allow-empty", "-qm", "review target " + session)
        self.resume()
        head = self.head()
        proc = self.record(
            "cross_provider_reviewer",
            session=session,
            reviewed_head=head,
            packet_hash=packet_hash,
            check=check,
        )
        return proc

    def test_packet_hash_bare_hex_is_accepted_and_stored_canonically(self):
        """issue #3: review.py пишет голый 64-hex, state хранит sha256:<hex>."""
        bare = "ab12" * 16
        self.reviewer_record(bare, "reviewer-bare")
        result = self.manifest_data()["tasks"]["implement"]["results"][
            "cross_provider_reviewer"
        ]
        self.assertEqual(result["packet_hash"], "sha256:" + bare)

    def test_packet_hash_prefixed_form_is_still_stored_as_is(self):
        prefixed = "sha256:" + "cd34" * 16
        self.reviewer_record(prefixed, "reviewer-prefixed")
        result = self.manifest_data()["tasks"]["implement"]["results"][
            "cross_provider_reviewer"
        ]
        self.assertEqual(result["packet_hash"], prefixed)

    def test_packet_hash_other_forms_are_rejected(self):
        bad = {
            "uppercase bare": "AB12" * 16,
            "uppercase prefixed": "sha256:" + "AB12" * 16,
            "short bare": "ab12" * 15,
            "long bare": "ab12" * 17,
            "short prefixed": "sha256:" + "ab12" * 15,
            "other prefix": "sha1:" + "ab12" * 16,
            "double prefix": "sha256:sha256:" + "ab12" * 16,
            "non hex": "zz" * 32,
            "empty": "",
        }
        for label, value in bad.items():
            with self.subTest(label=label):
                proc = self.reviewer_record(value, "reviewer-" + label.replace(" ", "-"), check=False)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("packet_hash must be", proc.stderr)

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

    # ---- fix-loop --defer (#36 defect 3): low/P3 findings do not burn the cap ----

    def defer(self, source="cross_provider_reviewer", note="low: event text -> next wave", check=True):
        return self.cli(
            "fix-loop", "--task", "implement", "--defer", "--source", source,
            "--note", note, check=check,
        )

    def commit_change(self, text):
        (self.repo / "tracked.txt").write_text(text, encoding="utf-8")
        self.git("commit", "-qam", text.strip())
        self.resume()

    def test_defer_reviewer_findings_keeps_counters_and_makes_task_ready(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        before = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(before["status"], "in_progress")
        entry = json.loads(self.defer().stdout)
        self.assertEqual(entry["fix_cycles"], 0)
        self.assertEqual(entry["fix_sources"], {})
        self.assertIsNone(entry["decision_required_for"])
        self.assertEqual(entry["decisions"], [])
        self.assertEqual(entry["status"], "ready_for_pr_review")
        [deferral] = entry["deferrals"]
        self.assertEqual(deferral["source"], "cross_provider_reviewer")
        self.assertEqual(deferral["note"], "low: event text -> next wave")
        self.assertEqual(
            deferral["result_recorded_at"],
            entry["results"]["cross_provider_reviewer"]["recorded_at"],
        )
        self.assertGreaterEqual(deferral["recorded_at"], deferral["result_recorded_at"])

    def test_defer_for_optional_reviewers_is_accepted(self):
        for role in ("github_codex_review", "coderabbit"):
            self.record(role, status="findings", session=f"{role}-1")
            entry = json.loads(self.defer(source=role).stdout)
            self.assertEqual(entry["fix_cycles"], 0)
            self.assertEqual(entry["deferrals"][-1]["source"], role)

    def test_defer_rejections(self):
        self.record("coder", session="coder-1")
        self.record("tester", status="findings", session="tester-1")
        tester = self.defer(source="tester", check=False)
        self.assertNotEqual(tester.returncode, 0, tester.stdout)
        # no reviewer result at all
        missing = self.defer(check=False)
        self.assertNotEqual(missing.returncode, 0, missing.stdout)
        self.assertIn("findings", missing.stderr)
        for status in ("pass", "error"):
            kwargs = {}
            if status == "pass":
                kwargs = {"reviewed_head": self.head(), "packet_hash": "sha256:" + "e" * 64}
            self.record("cross_provider_reviewer", status=status, session=f"r-{status}", **kwargs)
            wrong = self.defer(check=False)
            self.assertNotEqual(wrong.returncode, 0, f"{status}: {wrong.stdout}")
        self.record("cross_provider_reviewer", status="findings", session="r-findings")
        for note in ("", "   ", "x" * 501, "two\nlines", "two\rlines"):
            bad = self.defer(note=note, check=False)
            self.assertNotEqual(bad.returncode, 0, repr(note))
        no_note = self.cli(
            "fix-loop", "--task", "implement", "--defer", "--source",
            "cross_provider_reviewer", check=False,
        )
        self.assertNotEqual(no_note.returncode, 0)
        no_source = self.cli(
            "fix-loop", "--task", "implement", "--defer", "--note", "n", check=False,
        )
        self.assertNotEqual(no_source.returncode, 0)
        both = self.cli(
            "fix-loop", "--task", "implement", "--defer", "--outcome", "failed",
            "--source", "cross_provider_reviewer", "--note", "n", check=False,
        )
        self.assertNotEqual(both.returncode, 0, both.stdout)
        with_decision = self.cli(
            "fix-loop", "--task", "implement", "--defer", "--decision", "invariant",
            "--source", "cross_provider_reviewer", "--note", "n", check=False,
        )
        self.assertNotEqual(with_decision.returncode, 0, with_decision.stdout)
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry.get("deferrals", []), [])
        self.assertEqual(entry["fix_cycles"], 0)

    def test_defer_rejects_result_from_a_previous_head(self):
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.commit_change("next head\n")
        stale = self.defer(check=False)
        self.assertNotEqual(stale.returncode, 0, stale.stdout)

    def test_defer_rejected_in_needs_decision_and_blocked(self):
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        for _ in range(2):
            self.cli("fix-loop", "--task", "implement", "--outcome", "failed",
                     "--source", "cross_provider_reviewer")
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["status"], "needs_decision")
        refused = self.defer(check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        data = self.manifest_data()
        data["tasks"]["implement"]["status"] = "blocked"
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        refused = self.defer(check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertEqual(self.manifest_data()["tasks"]["implement"].get("deferrals", []), [])

    def test_old_deferral_does_not_cover_findings_after_resume(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.defer()
        self.commit_change("second head\n")
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "in_progress")

    def test_deferral_does_not_cover_an_identical_rerun_on_same_head_and_second(self):
        # no sleep: the rerun lands in the same second with the same session and arguments
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        first = self.manifest_data()["tasks"]["implement"]["results"]["cross_provider_reviewer"]
        self.defer()
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        entry = self.manifest_data()["tasks"]["implement"]
        rerun = entry["results"]["cross_provider_reviewer"]
        self.assertRegex(rerun["result_id"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(rerun["result_id"], first["result_id"])
        self.assertEqual(entry["status"], "in_progress")

    def test_three_deferred_findings_in_a_row_do_not_block(self):
        # Regression for W2 of the wave-autobot run: cap 3/3 on a low finding.
        for index in range(3):
            if index:
                self.commit_change(f"round {index}\n")
            self.record("coder", session="coder-1")
            self.record("tester", session="tester-1")
            self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
            entry = json.loads(self.defer(note=f"low #{index} -> next wave").stdout)
            self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertEqual(entry["fix_cycles"], 0)
        self.assertEqual(entry["fix_sources"], {})
        self.assertEqual(len(entry["deferrals"]), 3)

    # ---- where.artifacts (#12): links to the current review results, ordinary mode ----

    PACKET = "sha256:" + "a" * 64

    def new_commit(self, text):
        (self.repo / "tracked.txt").write_text(text, encoding="utf-8")
        self.git("commit", "-qam", text.strip())

    def test_where_artifacts_lists_current_results_with_links_in_ordinary_mode(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", session="reviewer-1",
                    reviewed_head=self.head(), packet_hash=self.PACKET)
        before = self.cli("where").stdout
        info = json.loads(before)
        self.assertEqual(self.cli("where").stdout, before)  # read-only
        self.assertIsNone(info["plan_check"])  # ordinary mode: no plan, as before
        self.assertEqual(info["task"], "implement")
        self.assertEqual(info["step"], 7)
        self.assertEqual(info["role"], "github_codex_review")
        self.assertEqual(info["fix_round"]["total"], "0/3")
        self.assertEqual(info["verdicts"], {"coder": "pass", "cross_provider_reviewer": "pass",
                                            "tester": "pass"})
        self.assertEqual([a["role"] for a in info["artifacts"]],
                         ["coder", "cross_provider_reviewer", "tester"])
        reviewer = info["artifacts"][1]
        self.assertEqual(reviewer, {"role": "cross_provider_reviewer", "status": "pass",
                                    "artifact": "evidence", "reviewed_head": self.head(),
                                    "packet_hash": self.PACKET, "session_id": "reviewer-1",
                                    # 1.2.1 (#86): policy fields, empty below risk high
                                    "model": None, "profile": None, "fallback_for": None,
                                    "quota_evidence": None})
        coder = info["artifacts"][0]
        self.assertIsNone(coder["reviewed_head"])
        self.assertIsNone(coder["packet_hash"])
        self.assertEqual(coder["session_id"], "coder-1")

    def test_where_after_head_change_drops_old_results_until_resume(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.cli("fix-loop", "--task", "implement", "--outcome", "failed",
                 "--source", "cross_provider_reviewer")
        task = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(task["fix_cycles"], 1)
        self.assertEqual(task["fix_sources"]["cross_provider_reviewer"], 1)
        self.new_commit("changed\n")
        info = json.loads(self.cli("where").stdout)
        self.assertFalse(info["tree_matches"])
        self.assertTrue(info["next_action"].startswith("run resume"), info["next_action"])
        self.assertEqual(info["verdicts"], {})
        self.assertEqual(info["artifacts"], [])
        denied = self.cli("mark", "--task", "implement", "--step", "4", "--safe-point", "false", check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.resume()
        task = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(task["results"], {})
        self.assertEqual(task["fix_cycles"], 1)
        self.assertEqual(task["fix_sources"]["cross_provider_reviewer"], 1)
        after = json.loads(self.cli("where").stdout)
        self.assertTrue(after["tree_matches"])
        self.assertEqual(after["artifacts"], [])

    def test_where_counts_deferrals_and_reports_done(self):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.record("github_codex_review", status="findings", session="codex-1")
        self.defer()
        self.defer(source="github_codex_review")
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(info["deferred"], 2)
        self.assertEqual(info["task_status"], "ready_for_pr_review")
        self.assertIsNone(info["role"])
        self.assertTrue(info["next_action"].startswith("done"), info["next_action"])
        status = json.loads(self.cli("status").stdout)
        self.assertEqual(len(status["tasks"]["implement"]["deferrals"]), 2)


    # ---- fix-loop --accept (#54): findings of any severity accepted with a note ----

    def accept(self, source="cross_provider_reviewer", severity="medium",
               note="no prod path reaches this", check=True, extra=()):
        args = ["fix-loop", "--task", "implement", "--accept", "--source", source,
                "--note", note]
        if severity is not None:
            args += ["--severity", severity]
        return self.cli(*args, *extra, check=check)

    def reviewed(self, status="findings", session="reviewer-1"):
        self.record("coder", session="coder-1")
        self.record("tester", session="tester-1")
        self.record("cross_provider_reviewer", status=status, session=session)

    def test_accept_and_defer_from_needs_fix_make_the_task_ready(self):  # #60 p.2
        for flag in ("accept", "defer"):
            with self.subTest(flag=flag):
                self.setUp()
                self.reviewed()
                self.cli("fix-loop", "--task", "implement", "--outcome", "failed",
                         "--source", "cross_provider_reviewer")
                self.assertEqual(self.manifest_data()["tasks"]["implement"]["status"], "needs_fix")
                entry = json.loads(getattr(self, flag)().stdout)
                self.assertEqual(entry["status"], "ready_for_pr_review")

    def test_accept_from_needs_fix_keeps_it_while_another_role_is_not_pass(self):  # #60 p.2
        self.record("coder", session="coder-1")
        self.record("tester", status="findings", session="tester-1")
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.cli("fix-loop", "--task", "implement", "--outcome", "failed",
                 "--source", "tester")
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["status"], "needs_fix")
        entry = json.loads(self.accept().stdout)
        self.assertEqual(entry["status"], "needs_fix")

    def test_accept_records_acceptance_and_where_lists_it(self):
        self.reviewed()
        entry = json.loads(self.accept().stdout)
        self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertEqual(entry["fix_cycles"], 0)
        self.assertEqual(entry["fix_sources"], {})
        self.assertEqual(entry["decisions"], [])
        self.assertEqual(entry["deferrals"], [])
        self.assertIsNone(entry["decision_required_for"])
        [item] = entry["acceptances"]
        result = entry["results"]["cross_provider_reviewer"]
        self.assertEqual(item["source"], "cross_provider_reviewer")
        self.assertEqual(item["severity"], "medium")
        self.assertEqual(item["note"], "no prod path reaches this")
        self.assertEqual(item["head"], result["head"])
        self.assertRegex(item["result_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(item["result_recorded_at"], result["recorded_at"])
        self.assertGreaterEqual(item["recorded_at"], item["result_recorded_at"])
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(info["accepted"], 1)
        self.assertEqual(info["deferred"], 0)
        self.assertEqual(info["open_findings"], [])
        self.assertEqual(info["task_status"], "ready_for_pr_review")
        self.assertEqual(
            info["accepted_limitations"],
            [{"source": "cross_provider_reviewer", "severity": "medium",
              "note": "no prod path reaches this", "head": result["head"]}],
        )

    def test_accept_low_counts_but_is_not_listed_as_limitation(self):
        self.reviewed()
        self.accept(severity="low", note="nit")
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(info["accepted"], 1)
        self.assertEqual(info["accepted_limitations"], [])

    def test_old_acceptance_does_not_cover_findings_after_resume(self):
        self.reviewed()
        self.accept(severity="high")
        self.commit_change("second head\n")
        self.reviewed()
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry["status"], "in_progress")
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(
            [item["role"] for item in info["open_findings"]], ["cross_provider_reviewer"]
        )
        # the identical rerun on the same head and second is another result too
        self.commit_change("third head\n")
        self.reviewed()
        self.accept()
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["status"], "in_progress")

    def test_where_lists_only_acceptances_covering_the_current_result(self):  # r2 P2
        self.reviewed()
        self.accept()
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(len(info["accepted_limitations"]), 1)
        self.assertEqual(info["accepted"], 1)
        self.commit_change("another head\n")
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(info["accepted_limitations"], [])
        self.assertEqual(info["accepted"], 0)
        self.assertEqual(self.manifest_data()["tasks"]["implement"]["acceptances"][0]["severity"], "medium")  # history stays

    def test_accept_rejections(self):
        self.record("coder", session="coder-1")
        self.record("tester", status="findings", session="tester-1")
        refused = self.accept(source="tester", check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("tester", refused.stderr)
        # no reviewer result, then a non-findings one
        missing = self.accept(check=False)
        self.assertNotEqual(missing.returncode, 0, missing.stdout)
        self.assertIn("findings", missing.stderr)
        self.record("cross_provider_reviewer", status="error", session="r-error")
        self.assertNotEqual(self.accept(check=False).returncode, 0)
        self.record("cross_provider_reviewer", status="findings", session="r-findings")
        no_severity = self.accept(severity=None, check=False)
        self.assertNotEqual(no_severity.returncode, 0, no_severity.stdout)
        self.assertIn("--severity", no_severity.stderr)
        bad_severity = self.accept(severity="critical", check=False)
        self.assertNotEqual(bad_severity.returncode, 0)
        for note in ("", "   ", "x" * 501, "two\nlines"):
            self.assertNotEqual(self.accept(note=note, check=False).returncode, 0, repr(note))
        for flags in (
            ["--outcome", "failed"],
            ["--decision", "invariant"],
            ["--defer"],
        ):
            both = self.accept(check=False, extra=flags)
            self.assertNotEqual(both.returncode, 0, f"{flags}: {both.stdout}")
        # --severity without --accept is refused
        loose = self.cli(
            "fix-loop", "--task", "implement", "--defer", "--source",
            "cross_provider_reviewer", "--note", "n", "--severity", "low", check=False,
        )
        self.assertNotEqual(loose.returncode, 0, loose.stdout)
        self.assertIn("--severity", loose.stderr)
        entry = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(entry.get("acceptances", []), [])
        self.assertEqual(entry["fix_cycles"], 0)

    def test_accept_rejects_result_from_a_previous_head(self):
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        self.commit_change("next head\n")
        stale = self.accept(check=False)
        self.assertNotEqual(stale.returncode, 0, stale.stdout)

    def test_accept_rejected_in_needs_decision_and_blocked(self):
        self.record("cross_provider_reviewer", status="findings", session="reviewer-1")
        for _ in range(2):
            self.cli("fix-loop", "--task", "implement", "--outcome", "failed",
                     "--source", "cross_provider_reviewer")
        refused = self.accept(check=False)
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("--decision", refused.stderr)
        data = self.manifest_data()
        data["tasks"]["implement"]["status"] = "blocked"
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertNotEqual(self.accept(check=False).returncode, 0)
        self.assertEqual(self.manifest_data()["tasks"]["implement"].get("acceptances", []), [])

    def test_old_manifest_without_acceptances_still_reads(self):
        self.reviewed()
        data = self.manifest_data()
        data["tasks"]["implement"].pop("acceptances", None)
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        info = json.loads(self.cli("where").stdout)
        self.assertEqual(info["accepted"], 0)
        entry = json.loads(self.accept().stdout)
        self.assertEqual(len(entry["acceptances"]), 1)


V060_KEYS = {
    "version",
    "run_id",
    "repo",
    "base",
    "head",
    "tree_fingerprint",
    "tasks",
    "created_at",
    "updated_at",
}


def canonical_sha(obj):
    blob = json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def wave(wave_id, depends_on=(), **overrides):
    value = {
        "id": wave_id,
        "title": f"Title {wave_id}",
        "goal": f"Goal {wave_id}",
        "requirements": f"Requirements {wave_id}",
        "acceptance": [f"Acceptance {wave_id}"],
        "risk": "medium",
        "checks": [{"name": "unit", "cmd": "bash tests/run.sh"}],
        "depends_on": list(depends_on),
    }
    value.update(overrides)
    return value


def plan_doc(waves=None):
    return {
        "version": 1,
        "chain": "chain-one",
        "repo": "owner/name",
        "base_branch": "main",
        "waves": waves if waves is not None else [wave("w1"), wave("w2", ["w1"]), wave("w3")],
    }


class WavesContract(unittest.TestCase):
    """init --from-plan, mark and where (spec 5.1-5.3)."""

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
        self.plan = Path(self.tmp.name) / "waves.json"

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            text=True,
            capture_output=True,
            check=check,
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def write_plan(self, doc=None, raw=None):
        if raw is None:
            raw = json.dumps(doc if doc is not None else plan_doc(), indent=2)
        data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        self.plan.write_bytes(data)
        return data

    def run_state(self, command, *args, manifest=True):
        argv = ["python3", str(STATE), command]
        if manifest:
            argv += ["--manifest", str(self.manifest)]
        return subprocess.run(
            [*argv, *map(str, args)], text=True, capture_output=True
        )

    def cli(self, command, *args):
        proc = self.run_state(command, *args)
        if proc.returncode:
            self.fail(f"{command} failed rc={proc.returncode}: {proc.stderr}")
        return proc

    def init_args(self, selector):
        return [
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
            "--run-id",
            "wave-run",
            "--from-plan",
            selector,
        ]

    def init_wave(self, wave_id="w1", doc=None):
        self.write_plan(doc)
        return self.cli("init", *self.init_args(f"{self.plan}#{wave_id}"))

    def init_wave_raw(self, selector):
        return self.run_state("init", *self.init_args(selector))

    def manifest_data(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def where(self):
        proc = self.cli("where")
        return json.loads(proc.stdout)

    def record(self, role, status="pass", task="implement", artifact="evidence"):
        return self.cli(
            "task-result",
            "--task",
            task,
            "--role",
            role,
            "--status",
            status,
            "--session-id",
            f"{role}-{task}-session",
            "--head",
            self.head(),
            "--artifact",
            artifact,
        )

    def mark(self, task="implement", step=4, safe="false"):
        return self.run_state(
            "mark", "--task", task, "--step", step, "--safe-point", safe
        )

    def resume(self):
        return self.cli(
            "resume",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
        )

    # R2: init --from-plan
    def test_init_from_plan_records_plan_and_wave_copy(self):
        raw = self.write_plan()
        self.cli("init", *self.init_args(f"{self.plan}#w2"))
        data = self.manifest_data()
        selected = plan_doc()["waves"][1]
        self.assertEqual(data["wave"], selected)
        sha = hashlib.sha256(raw).hexdigest()
        self.assertEqual(
            data["plan"],
            {
                "path": str(self.plan.resolve()),
                "sha256": sha,
                "wave": "w2",
                "approved_by": f"plan@{sha}",
                "wave_sha256": canonical_sha(selected),
            },
        )
        # 1.2.1 (#86): new manifests are version 2; the wave's risk is the policy level
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["review_policy"], {"version": "1.2.1", "level": selected["risk"]})
        self.assertEqual(data["base"], self.base)

    # T1: init --expect-sha256 pins the approved plan bytes
    def test_init_expect_sha256_match_creates_manifest(self):
        raw = self.write_plan()
        sha = hashlib.sha256(raw).hexdigest()
        self.cli("init", *self.init_args(f"{self.plan}#w1"), "--expect-sha256", sha)
        self.assertEqual(self.manifest_data()["plan"]["sha256"], sha)

    def test_init_expect_sha256_mismatch_creates_no_manifest(self):
        self.write_plan()
        want = "0" * 64
        proc = self.run_state(
            "init", *self.init_args(f"{self.plan}#w1"), "--expect-sha256", want
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("plan changed since approval", proc.stderr)
        self.assertFalse(self.manifest.exists())

    def test_init_expect_sha256_rejects_invalid_hex(self):
        raw = self.write_plan()
        sha = hashlib.sha256(raw).hexdigest()
        for bad in (sha.upper(), sha[:63], sha + "0", "g" * 64, ""):
            with self.subTest(bad=bad):
                proc = self.run_state(
                    "init", *self.init_args(f"{self.plan}#w1"), "--expect-sha256", bad
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(self.manifest.exists())

    def test_init_expect_sha256_requires_from_plan(self):
        proc = self.run_state(
            "init", "--repo", self.repo, "--base", self.base, "--head", self.head(),
            "--run-id", "legacy", "--expect-sha256", "0" * 64,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--expect-sha256 requires --from-plan", proc.stderr)
        self.assertFalse(self.manifest.exists())

    def test_init_without_from_plan_has_exactly_the_v060_keys(self):
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
            "--run-id",
            "legacy",
        )
        # 1.2.1 (#86): the only addition to the v0.6.0 keys is the review policy
        self.assertEqual(set(self.manifest_data()), V060_KEYS | {"review_policy"})

    def assert_init_rejected(self, selector):
        proc = self.init_wave_raw(selector)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.manifest.exists())
        self.assertFalse(
            Path(self.tmp.name, ".run.json.lock").exists()
            and self.manifest.exists()
        )

    def test_init_from_plan_unknown_wave_id_rejected(self):
        self.write_plan()
        self.assert_init_rejected(f"{self.plan}#nope")

    def test_init_from_plan_hash_without_id_rejected(self):
        self.write_plan()
        self.assert_init_rejected(f"{self.plan}#")
        self.assert_init_rejected(str(self.plan))

    def test_init_from_plan_missing_file_rejected(self):
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_duplicate_json_key_rejected(self):
        raw = json.dumps(plan_doc())
        raw = raw.replace('"chain": "chain-one"', '"chain": "a", "chain": "b"')
        self.write_plan(raw=raw)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_duplicate_key_in_nested_wave_rejected(self):
        raw = json.dumps(plan_doc()).replace('"risk": "medium"', '"risk": "low", "risk": "high"', 1)
        self.write_plan(raw=raw)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_unknown_top_level_key_rejected(self):
        doc = plan_doc()
        doc["extra"] = 1
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_unknown_wave_key_rejected(self):
        doc = plan_doc([wave("w1", extra="x")])
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_missing_key_rejected(self):
        doc = plan_doc()
        del doc["waves"][0]["goal"]
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_bad_risk_rejected(self):
        self.write_plan(plan_doc([wave("w1", risk="extreme")]))
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_wrong_version_rejected(self):
        doc = plan_doc()
        doc["version"] = 2
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_bool_version_rejected(self):
        doc = plan_doc()
        doc["version"] = True
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_empty_required_strings_rejected(self):
        for field in ("title", "goal", "requirements"):
            with self.subTest(field=field):
                self.write_plan(plan_doc([wave("w1", **{field: ""})]))
                self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_empty_waves_or_acceptance_rejected(self):
        self.write_plan(plan_doc([]))
        self.assert_init_rejected(f"{self.plan}#w1")
        self.write_plan(plan_doc([wave("w1", acceptance=[])]))
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_bad_checks_rejected(self):
        bad = [
            [{"name": "n"}],
            [{"name": "n", "cmd": "c", "x": 1}],
            [{"name": 1, "cmd": "c"}],
            "bash",
        ]
        for checks in bad:
            with self.subTest(checks=checks):
                self.write_plan(plan_doc([wave("w1", checks=checks)]))
                self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_bad_wave_ids_rejected(self):
        for bad in (".", "..", "a/b", "a b", ""):
            with self.subTest(bad=bad):
                self.write_plan(plan_doc([wave(bad)]))
                self.assert_init_rejected(f"{self.plan}#{bad or 'x'}")

    def test_init_from_plan_duplicate_wave_id_rejected(self):
        self.write_plan(plan_doc([wave("w1"), wave("w1")]))
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_self_and_forward_dependency_rejected(self):
        self.write_plan(plan_doc([wave("w1", ["w1"])]))
        self.assert_init_rejected(f"{self.plan}#w1")
        self.write_plan(plan_doc([wave("w1", ["w2"]), wave("w2")]))
        self.assert_init_rejected(f"{self.plan}#w1")
        self.write_plan(plan_doc([wave("w1", ["ghost"])]))
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_symlinked_plan_rejected(self):
        real = Path(self.tmp.name) / "real-plan.json"
        real.write_text(json.dumps(plan_doc()), encoding="utf-8")
        self.plan.symlink_to(real)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_non_regular_file_rejected(self):
        self.plan.mkdir()
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_over_one_mib_rejected(self):
        doc = plan_doc([wave("w1", goal="x" * (1024 * 1024 + 1))])
        self.write_plan(doc)
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_invalid_utf8_rejected(self):
        self.write_plan(raw=b'{"version": 1, "chain": "\xff"}')
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_not_an_object_rejected(self):
        self.write_plan(raw="[1, 2]")
        self.assert_init_rejected(f"{self.plan}#w1")

    # R3: mark
    def test_mark_creates_task_and_records_position(self):
        self.init_wave()
        before = self.manifest_data()
        self.cli("mark", "--task", "impl", "--step", 4, "--safe-point", "true")
        data = self.manifest_data()
        position = data["position"]
        self.assertEqual(
            set(position),
            {"task", "step", "safe_point", "head", "tree_fingerprint", "recorded_at"},
        )
        self.assertEqual(position["task"], "impl")
        self.assertEqual(position["step"], 4)
        self.assertIs(position["safe_point"], True)
        self.assertEqual(position["head"], self.head())
        self.assertEqual(position["tree_fingerprint"], before["tree_fingerprint"])
        self.assertEqual(data["tasks"]["impl"]["status"], "pending")
        self.assertEqual(data["tasks"]["impl"]["results"], {})

    def test_mark_does_not_change_existing_task_status(self):
        self.init_wave()
        self.record("coder")
        self.record("tester")
        before = self.manifest_data()["tasks"]["implement"]
        self.cli("mark", "--task", "implement", "--step", 5, "--safe-point", "false")
        after = self.manifest_data()["tasks"]["implement"]
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["results"], after["results"])

    def test_mark_safe_point_false_is_boolean_false(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 6, "--safe-point", "false")
        self.assertIs(self.manifest_data()["position"]["safe_point"], False)

    def test_mark_rejects_bad_arguments_and_leaves_manifest(self):
        self.init_wave()
        before = self.manifest.read_bytes()
        cases = [
            ("impl", 0, "true"),
            ("impl", 8, "true"),
            ("impl", "x", "true"),
            ("impl", 4, "maybe"),
            ("bad/task", 4, "true"),
            ("..", 4, "true"),
            ("", 4, "true"),
        ]
        for task, step, safe in cases:
            with self.subTest(task=task, step=step, safe=safe):
                proc = self.mark(task, step, safe)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(self.manifest.read_bytes(), before)

    def test_mark_after_uncommitted_change_rejected_and_manifest_unchanged(self):
        self.init_wave()
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        before = self.manifest.read_bytes()
        proc = self.mark()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_mark_after_new_commit_rejected_until_resume(self):
        self.init_wave()
        self.git("commit", "--allow-empty", "-qm", "next")
        before = self.manifest.read_bytes()
        proc = self.mark()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.resume()
        self.assertEqual(self.mark().returncode, 0)

    def test_mark_works_on_legacy_manifest_without_wave_keys(self):
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
        )
        self.assertEqual(self.mark().returncode, 0)
        self.assertIn("position", self.manifest_data())

    # R4: where
    def test_where_fresh_marked_task_has_all_fields_defined(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 4, "--safe-point", "false")
        info = self.where()
        self.assertEqual(info["task"], "impl")
        self.assertEqual(info["task_status"], "pending")
        self.assertEqual(info["step"], 4)
        self.assertEqual(info["role"], "coder")
        self.assertEqual(info["verdicts"], {})
        self.assertEqual(info["open_findings"], [])
        self.assertIsNone(info["decision_required_for"])
        self.assertIs(info["tree_matches"], True)
        self.assertIs(info["safe_point"], False)
        self.assertEqual(info["plan_check"], "match")
        self.assertEqual(
            info["fix_round"]["total"], "0/3"
        )
        self.assertIsInstance(info["next_action"], str)
        self.assertNotIn("\n", info["next_action"])
        for key in (
            "tree_matches",
            "plan_check",
            "task",
            "task_status",
            "step",
            "role",
            "fix_round",
            "verdicts",
            "open_findings",
            "decision_required_for",
            "safe_point",
            "next_action",
        ):
            self.assertIn(key, info)

    def test_where_is_read_only(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 4, "--safe-point", "true")
        before = self.manifest.read_bytes()
        self.where()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_where_read_only_even_when_tree_is_stale(self):
        self.init_wave()
        self.git("commit", "--allow-empty", "-qm", "next")
        before = self.manifest.read_bytes()
        info = self.where()
        self.assertIs(info["tree_matches"], False)
        self.assertTrue(info["next_action"].startswith("run resume"))
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_where_on_legacy_manifest_without_new_keys(self):
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
        )
        before = self.manifest.read_bytes()
        info = self.where()
        self.assertIsNone(info["plan_check"])
        self.assertIsNone(info["task"])
        self.assertIsNone(info["task_status"])
        self.assertIsNone(info["safe_point"])
        self.assertEqual(info["step"], 4)
        self.assertEqual(info["role"], "coder")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_where_no_position_picks_first_unfinished_task(self):
        self.init_wave()
        self.record("coder", task="b-task")
        self.record("coder", task="a-task")
        info = self.where()
        self.assertEqual(info["task"], "a-task")
        self.assertEqual(info["role"], "tester")
        self.assertEqual(info["step"], 5)

    def test_where_position_task_wins_over_first_task(self):
        self.init_wave()
        self.record("coder", task="a-task")
        self.cli("mark", "--task", "z-task", "--step", 4, "--safe-point", "false")
        self.assertEqual(self.where()["task"], "z-task")

    def test_where_derives_roles_through_the_review_chain(self):
        self.init_wave()
        self.record("coder")
        self.assertEqual(self.where()["role"], "tester")
        self.record("tester")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (5, "cross_provider_reviewer"))
        self.cli(
            "task-result",
            "--task",
            "implement",
            "--role",
            "cross_provider_reviewer",
            "--status",
            "pass",
            "--session-id",
            "xp-session",
            "--head",
            self.head(),
            "--artifact",
            "evidence",
            "--reviewed-head",
            self.head(),
            "--packet-hash",
            "sha256:" + "a" * 64,
        )
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (7, "github_codex_review"))
        self.assertEqual(info["task_status"], "ready_for_pr_review")
        self.assertEqual(
            info["verdicts"],
            {
                "coder": "pass",
                "tester": "pass",
                "cross_provider_reviewer": "pass",
            },
        )
        self.cli(
            "task-result",
            "--task",
            "implement",
            "--role",
            "github_codex_review",
            "--status",
            "pass",
            "--session-id",
            "gh-session",
            "--head",
            self.head(),
            "--artifact",
            "https://example.invalid/review",
            "--reviewed-head",
            self.head(),
        )
        info = self.where()
        self.assertEqual(info["step"], 7)
        self.assertIsNone(info["role"])
        self.assertTrue(info["next_action"].startswith("done"))

    def test_where_open_findings_and_needs_fix(self):
        self.init_wave()
        self.record("coder")
        self.record("tester", status="findings", artifact="https://x.invalid/t")
        self.cli("fix-loop", "--task", "implement", "--outcome", "failed", "--source", "tester")
        info = self.where()
        self.assertEqual(info["task_status"], "needs_fix")
        self.assertEqual((info["step"], info["role"]), (6, "coder"))
        self.assertEqual(
            info["open_findings"],
            [{"role": "tester", "status": "findings", "artifact": "https://x.invalid/t"}],
        )
        self.assertEqual(info["fix_round"]["tester"], "1/2")
        self.assertEqual(info["fix_round"]["total"], "1/3")

    def test_where_needs_decision_names_the_call(self):
        self.init_wave()
        self.record("coder")
        for _ in range(2):
            self.cli(
                "fix-loop", "--task", "implement", "--outcome", "failed",
                "--source", "tester",
            )
        info = self.where()
        self.assertEqual(info["task_status"], "needs_decision")
        self.assertEqual(info["decision_required_for"], "tester")
        self.assertEqual((info["step"], info["role"]), (6, "coordinator"))
        self.assertIn("fix-loop --decision", info["next_action"])
        self.assertIn("tester", info["next_action"])
        self.assertEqual(info["fix_round"]["tester"], "2/2")

    def test_where_blocked_task(self):
        self.init_wave()
        self.record("coder")
        for source in ("tester", "tester", "coderabbit"):
            proc = self.run_state(
                "fix-loop", "--task", "implement", "--outcome", "failed",
                "--source", source,
            )
            if proc.returncode:
                self.cli(
                    "fix-loop", "--task", "implement", "--decision", "invariant",
                    "--note", "n",
                )
                self.cli(
                    "fix-loop", "--task", "implement", "--outcome", "failed",
                    "--source", source,
                )
        info = self.where()
        self.assertEqual(info["task_status"], "blocked")
        self.assertIsNone(info["step"])
        self.assertTrue(info["next_action"].startswith("BLOCKED"))

    def test_where_plan_unchanged_is_match(self):
        self.init_wave("w2")
        self.assertEqual(self.where()["plan_check"], "match")

    def test_where_selected_wave_edited_is_changed_and_blocked(self):
        self.init_wave("w2")
        doc = plan_doc()
        doc["waves"][1]["goal"] = "Changed goal"
        self.write_plan(doc)
        info = self.where()
        self.assertEqual(info["plan_check"], "changed")
        self.assertTrue(info["next_action"].startswith("BLOCKED: plan changed"))

    def test_where_changed_plan_takes_precedence_over_stale_tree(self):
        self.init_wave("w2")
        doc = plan_doc()
        doc["waves"][1]["goal"] = "Changed goal"
        self.write_plan(doc)
        self.git("commit", "--allow-empty", "-qm", "next")
        info = self.where()
        self.assertTrue(info["next_action"].startswith("BLOCKED: plan changed"))

    def test_where_other_wave_edited_is_still_match(self):
        self.init_wave("w2")
        doc = plan_doc()
        doc["waves"][2]["goal"] = "Changed goal of another wave"
        self.write_plan(doc)
        info = self.where()
        self.assertEqual(info["plan_check"], "match")
        self.assertFalse(info["next_action"].startswith("BLOCKED"))

    def test_where_selected_wave_removed_or_plan_invalid_is_changed(self):
        self.init_wave("w2")
        self.write_plan(plan_doc([wave("w1"), wave("w3")]))
        self.assertEqual(self.where()["plan_check"], "changed")
        self.write_plan(raw="{not json")
        info = self.where()
        self.assertEqual(info["plan_check"], "changed")
        self.assertTrue(info["next_action"].startswith("BLOCKED: plan changed"))

    def test_where_plan_deleted_is_missing_and_not_blocked(self):
        self.init_wave("w2")
        self.plan.unlink()
        info = self.where()
        self.assertEqual(info["plan_check"], "missing")
        self.assertFalse(info["next_action"].startswith("BLOCKED"))
        self.assertEqual(info["role"], "coder")

    def test_where_plan_becomes_symlink_is_changed(self):
        self.init_wave("w2")
        real = Path(self.tmp.name) / "real.json"
        real.write_text(self.plan.read_text(encoding="utf-8"), encoding="utf-8")
        self.plan.unlink()
        self.plan.symlink_to(real)
        self.assertEqual(self.where()["plan_check"], "changed")

    def test_where_step_and_role_follow_results_not_mark(self):
        self.init_wave()
        self.cli("mark", "--task", "t", "--step", 4, "--safe-point", "false")
        self.record("coder", task="t")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (5, "tester"))
        self.assertEqual(info["marked_step"], 4)
        self.assertIn("step 5", info["next_action"])
        self.assertIn("tester", info["next_action"])

    def test_where_marked_step_null_without_position_or_other_task(self):
        self.init_wave()
        self.assertIsNone(self.where()["marked_step"])
        self.record("coder", task="a")
        self.cli("mark", "--task", "b", "--step", 4, "--safe-point", "false")
        self.assertEqual(self.where()["marked_step"], 4)

    def test_init_from_plan_exactly_one_mib_accepted_and_one_more_rejected(self):
        base = json.dumps(plan_doc([wave("w1", goal="")]), indent=2)
        pad = 1024 * 1024 - len(base.encode("utf-8"))
        self.assertGreater(pad, 0)
        raw = base.replace('"goal": ""', '"goal": "' + "x" * pad + '"')
        self.assertEqual(len(raw.encode("utf-8")), 1024 * 1024)
        self.write_plan(raw=raw)
        self.assertEqual(self.init_wave_raw(f"{self.plan}#w1").returncode, 0)
        self.manifest.unlink()
        self.write_plan(raw=raw.replace('"x', '"xx', 1))
        self.assert_init_rejected(f"{self.plan}#w1")

    def test_init_from_plan_directory_and_fifo_give_clean_message(self):
        self.plan.mkdir()
        proc = self.init_wave_raw(f"{self.plan}#w1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Errno", proc.stderr)
        self.assertIn("regular file", proc.stderr)
        self.assertFalse(self.manifest.exists())
        self.plan.rmdir()
        os.mkfifo(self.plan)
        proc = self.init_wave_raw(f"{self.plan}#w1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Errno", proc.stderr)
        self.assertIn("regular file", proc.stderr)
        self.assertFalse(self.manifest.exists())

    PROPERTY_VALUES = [
        None, True, 0, 1.5, "", [], {}, [[]], {"a": 1}, "\ud800", "\udfff"
    ]

    def property_cases(self):
        """Every (path, value) that makes plan_doc() invalid, selected wave = w2."""
        doc = plan_doc()
        paths = [(key,) for key in doc if key != "waves"] + [("waves",)]
        paths += [("waves", 1, key) for key in doc["waves"][1]]
        paths += [
            ("waves", 1, "acceptance", 0),
            ("waves", 1, "checks", 0, "name"),
            ("waves", 1, "checks", 0, "cmd"),
            ("waves", 1, "depends_on", 0),
        ]
        valid = {("waves", 1, "checks"), ("waves", 1, "depends_on")}
        for key_path in (("\ud800",), ("waves", 1, "\udfff"), ("waves", 1, "a\ud800b")):
            mutated = copy.deepcopy(doc)
            target = mutated
            for key in key_path[:-1]:
                target = target[key]
            target[key_path[-1]] = 1
            yield key_path, "surrogate key", mutated
        for path in paths:
            for value in self.PROPERTY_VALUES:
                if path in valid and value == []:
                    continue
                mutated = copy.deepcopy(doc)
                target = mutated
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                yield path, value, mutated

    def test_property_init_rejects_every_bad_value_cleanly(self):
        for path, value, mutated in self.property_cases():
            with self.subTest(path=path, value=value):
                if self.manifest.exists():
                    self.manifest.unlink()
                self.write_plan(mutated)
                proc = self.init_wave_raw(f"{self.plan}#w2")
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertTrue(proc.stderr.startswith("state:"), proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(self.manifest.exists())

    def test_property_where_marks_every_bad_value_as_changed(self):
        self.init_wave("w2")
        for path, value, mutated in self.property_cases():
            with self.subTest(path=path, value=value):
                self.write_plan(mutated)
                proc = self.run_state("where")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                info = json.loads(proc.stdout)
                self.assertEqual(info["plan_check"], "changed")
                self.assertTrue(
                    info["next_action"].startswith("BLOCKED: plan changed")
                )

    def test_init_from_plan_repo_format_enforced(self):
        for bad in ("noslash", "a/b/c", "/b", "a/", "a b/c", "a/b\n"):
            with self.subTest(bad=bad):
                doc = plan_doc()
                doc["repo"] = bad
                self.write_plan(doc)
                self.assert_init_rejected(f"{self.plan}#w1")

    def record_any(self, role, status, task="implement"):
        extra = []
        artifact = "evidence"
        if status == "pass" and role == "cross_provider_reviewer":
            extra = ["--reviewed-head", self.head(), "--packet-hash", "sha256:" + "a" * 64]
        if status == "pass" and role == "github_codex_review":
            artifact = "https://example.invalid/review"
            extra = ["--reviewed-head", self.head()]
        return self.cli(
            "task-result", "--task", task, "--role", role, "--status", status,
            "--session-id", f"{role}-{task}-session", "--head", self.head(),
            "--artifact", artifact, *extra,
        )

    def passes(self, *roles):
        for role in roles:
            self.record_any(role, "pass")

    def test_where_non_pass_findings_route_to_disposition(self):
        cases = [
            (("coder",), "tester", "findings"),
            (("coder", "tester"), "cross_provider_reviewer", "findings"),
            (("coder", "tester", "cross_provider_reviewer"), "github_codex_review", "findings"),
            (("coder", "tester", "cross_provider_reviewer", "github_codex_review"), "coderabbit", "findings"),
        ]
        for before, role, status in cases:
            with self.subTest(role=role, status=status, before=len(before)):
                if self.manifest.exists():
                    self.manifest.unlink()
                self.init_wave()
                self.passes(*before)
                self.record_any(role, status)
                info = self.where()
                self.assertEqual((info["step"], info["role"]), (6, "coordinator"))
                self.assertIn(f"fix-loop", info["next_action"])
                self.assertIn(f"--outcome failed --source {role}", info["next_action"])
                self.assertFalse(info["next_action"].startswith("done"))

    def test_where_error_or_unavailable_is_blocked_at_that_role(self):
        steps = {"tester": 5, "cross_provider_reviewer": 5, "github_codex_review": 7}
        chain = ("coder", "tester", "cross_provider_reviewer", "github_codex_review")
        for role, step in steps.items():
            for status in ("error", "unavailable"):
                with self.subTest(role=role, status=status):
                    if self.manifest.exists():
                        self.manifest.unlink()
                    self.init_wave()
                    self.passes(*chain[: chain.index(role)] if role in chain else chain)
                    self.record_any(role, status)
                    info = self.where()
                    self.assertEqual((info["step"], info["role"]), (step, role))
                    self.assertTrue(
                        info["next_action"].startswith(f"BLOCKED: {role} {status}"),
                        info["next_action"],
                    )

    def test_where_coder_non_pass_routes_to_coder(self):
        for status in ("findings", "incomplete", "error", "unavailable"):
            with self.subTest(status=status):
                if self.manifest.exists():
                    self.manifest.unlink()
                self.init_wave()
                self.record_any("coder", status)
                info = self.where()
                self.assertEqual((info["step"], info["role"]), (4, "coder"))
                self.assertIn("coder", info["next_action"])
                self.assertFalse(info["next_action"].startswith("done"))

    def test_where_done_only_when_no_current_result_is_non_pass(self):
        self.init_wave()
        self.passes("coder", "tester", "cross_provider_reviewer", "github_codex_review")
        self.assertTrue(self.where()["next_action"].startswith("done"))
        self.record_any("coderabbit", "pass")
        self.assertTrue(self.where()["next_action"].startswith("done"))

    def test_where_precedence_disposition_before_coder_progression(self):
        self.init_wave()
        self.record_any("coder", "incomplete")
        self.record_any("tester", "findings")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (6, "coordinator"))
        self.assertIn("--outcome failed --source tester", info["next_action"])

    def test_where_precedence_blocked_before_missing_coder(self):
        self.init_wave()
        self.record_any("cross_provider_reviewer", "unavailable")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (5, "cross_provider_reviewer"))
        self.assertTrue(
            info["next_action"].startswith("BLOCKED: cross_provider_reviewer unavailable")
        )

    def test_where_coder_findings_alone_text(self):
        self.init_wave()
        self.record_any("coder", "findings")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (4, "coder"))
        self.assertEqual(
            info["next_action"],
            "step 4 coder: coder must finish or fix before tester (task implement)",
        )

    # Run 3 fix 2: A (fresh pre-code marks), B (task selection), C (one line)
    def test_where_fresh_precode_mark_sets_coordinator_step(self):
        self.init_wave()
        self.cli("mark", "--task", "t", "--step", 2, "--safe-point", "true")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (2, "coordinator"))
        self.assertEqual(
            info["next_action"], "continue step 2 (requirements/plan) for task t"
        )

    def test_where_stale_precode_mark_does_not_drive_step(self):
        self.init_wave()
        self.cli("mark", "--task", "t", "--step", 2, "--safe-point", "true")
        self.git("commit", "--allow-empty", "-qm", "next")
        self.cli(
            "resume", "--repo", self.repo, "--base", self.base, "--head", self.head()
        )
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (4, "coder"))

    def test_where_precode_mark_with_results_is_derived_not_marked(self):
        self.init_wave()
        self.cli("mark", "--task", "t", "--step", 2, "--safe-point", "true")
        self.record_any("coder", "pass", task="t")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (5, "tester"))

    def test_where_step_4_plus_marks_stay_informational(self):
        self.init_wave()
        self.cli("mark", "--task", "t", "--step", 5, "--safe-point", "true")
        info = self.where()
        self.assertEqual((info["step"], info["role"]), (4, "coder"))
        self.assertEqual(info["marked_step"], 5)

    def test_where_positioned_done_task_not_selected_while_another_is_open(self):
        self.init_wave()
        self.passes_for(
            "a", "coder", "tester", "cross_provider_reviewer", "github_codex_review"
        )
        self.cli("mark", "--task", "a", "--step", 7, "--safe-point", "true")
        self.record_any("coder", "pass", task="b")
        info = self.where()
        self.assertEqual(info["task"], "b")
        self.assertEqual((info["step"], info["role"]), (5, "tester"))

    def test_where_positioned_nonexistent_or_all_complete_selection(self):
        self.init_wave()
        full = ("coder", "tester", "cross_provider_reviewer", "github_codex_review")
        self.passes_for("a", *full)
        self.passes_for("b", *full)
        self.cli("mark", "--task", "b", "--step", 7, "--safe-point", "true")
        info = self.where()
        self.assertEqual(info["task"], "b")
        self.assertTrue(info["next_action"].startswith("done"))

    def test_where_selects_task_with_fresh_codex_or_coderabbit_findings(self):
        full = ("coder", "tester", "cross_provider_reviewer", "github_codex_review")
        for role in ("github_codex_review", "coderabbit"):
            with self.subTest(role=role):
                if self.manifest.exists():
                    self.manifest.unlink()
                self.init_wave()
                self.passes_for("a", "coder", "tester", "cross_provider_reviewer")
                if role == "coderabbit":
                    self.record_any("github_codex_review", "pass", task="a")
                self.record_any(role, "findings", task="a")
                self.passes_for("b", *full)
                self.cli("mark", "--task", "b", "--step", 7, "--safe-point", "true")
                info = self.where()
                self.assertEqual(info["task"], "a")
                self.assertEqual((info["step"], info["role"]), (6, "coordinator"))
                self.assertIn(f"--source {role}", info["next_action"])

    def test_where_ready_task_without_codex_result_is_not_complete(self):
        self.init_wave()
        self.passes_for("a", "coder", "tester", "cross_provider_reviewer")
        self.passes_for("b", "coder", "tester", "cross_provider_reviewer", "github_codex_review")
        self.cli("mark", "--task", "b", "--step", 7, "--safe-point", "true")
        info = self.where()
        self.assertEqual(info["task"], "a")
        self.assertEqual((info["step"], info["role"]), (7, "github_codex_review"))

    def test_where_escapes_nel_in_next_action(self):
        self.init_wave()
        data = self.manifest_data()
        data["tasks"]["a\x85b"] = {
            "status": "blocked", "fix_cycles": 3, "results": {}, "session_roles": {},
        }
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        action = self.where()["next_action"]
        self.assertEqual(len(action.splitlines()), 1)
        self.assertNotIn("\x85", action)

    def test_where_safe_point_is_null_for_a_task_other_than_the_marked_one(self):
        self.init_wave()
        full = ("coder", "tester", "cross_provider_reviewer", "github_codex_review")
        self.passes_for("b", *full)
        self.record_any("coder", "pass", task="a")
        self.cli("mark", "--task", "b", "--step", 7, "--safe-point", "true")
        info = self.where()
        self.assertEqual(info["task"], "a")
        self.assertIsNone(info["safe_point"])
        self.assertIsNone(info["marked_step"])

    def passes_for(self, task, *roles):
        for role in roles:
            self.record_any(role, "pass", task=task)

    def assert_one_line(self, text):
        for ch in text:
            self.assertFalse(
                ch < " " or ch in "\x7f\u2028\u2029", repr(text)
            )

    def test_where_next_action_is_one_line_for_hostile_path_and_task(self):
        for hostile in ("a\nb c", "x\u2028y", "p\u2029q", "t\x01u"):
            with self.subTest(hostile=repr(hostile)):
                self.manifest = Path(self.tmp.name) / f"run {hostile}.json"
                self.init_wave()
                data = self.manifest_data()
                data["tasks"][f"task {hostile}"] = {
                    "status": "blocked", "fix_cycles": 3, "results": {},
                    "session_roles": {},
                }
                self.manifest.write_text(json.dumps(data), encoding="utf-8")
                info = self.where()
                self.assert_one_line(info["next_action"])
                self.assertTrue(info["next_action"].startswith("BLOCKED"))
                # stale tree path quotes the manifest path too
                self.git("commit", "--allow-empty", "-qm", f"n{len(hostile)}{ord(hostile[1])}")
                info = self.where()
                self.assert_one_line(info["next_action"])
                self.assertTrue(info["next_action"].startswith("run resume"))

    # A4: safe-point staleness
    def test_safe_point_true_then_commit_is_stale_false(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 6, "--safe-point", "true")
        self.assertIs(self.where()["safe_point"], True)
        self.git("commit", "--allow-empty", "-qm", "moved on")
        self.assertIs(self.where()["safe_point"], False)

    def test_safe_point_true_then_dirty_tree_is_false(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 6, "--safe-point", "true")
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertIs(self.where()["safe_point"], False)

    def test_safe_point_null_without_position(self):
        self.init_wave()
        self.assertIsNone(self.where()["safe_point"])

    # R11 (W4): a hand-damaged position is reported, not a traceback
    def test_malformed_position_is_reported_not_a_traceback(self):
        self.init_wave()
        self.cli("mark", "--task", "impl", "--step", 4, "--safe-point", "true")
        good = self.manifest_data()["position"]
        bad_values = [
            "text",
            ["task"],
            42,
            {k: v for k, v in good.items() if k != "task"},
            {**good, "task": 7},
            {**good, "step": 0},
            {**good, "step": 8},
            {**good, "step": True},
            {**good, "step": "4"},
            {**good, "safe_point": "true"},
            {**good, "head": None},
            {**good, "tree_fingerprint": 1},
            {**good, "recorded_at": 1},
        ]
        for bad in bad_values:
            with self.subTest(position=bad):
                data = self.manifest_data()
                data["position"] = bad
                self.manifest.write_text(json.dumps(data), encoding="utf-8")
                for command in ("where", "status"):
                    proc = self.run_state(command)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("malformed position", proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)

    def test_absent_or_null_position_is_accepted(self):
        self.init_wave()
        data = self.manifest_data()
        data.pop("position", None)
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_state("where").returncode, 0)
        data["position"] = None
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_state("where").returncode, 0)

    # R5
    def test_read_accepts_manifests_without_new_keys(self):
        self.cli(
            "init",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            self.head(),
        )
        self.assertEqual(self.cli("status").returncode, 0)
        self.assertNotIn("position", self.manifest_data())
        self.assertNotIn("plan", self.manifest_data())


class RunCounter(unittest.TestCase):
    """#39: a durable per-wave run counter outside the worktree (init --from-plan)."""

    # the fixture helpers of WavesContract, without inheriting its tests
    git = WavesContract.git
    head = WavesContract.head
    write_plan = WavesContract.write_plan
    run_state = WavesContract.run_state
    cli = WavesContract.cli
    init_args = WavesContract.init_args
    where = WavesContract.where
    record = WavesContract.record

    def setUp(self):
        WavesContract.setUp(self)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith("WAB_")]:
            os.environ.pop(key, None)
        self.runs = Path(self.tmp.name) / "wabdir" / "runs.json"
        self.write_plan()

    def init_run(self, name, wave_id="w1", extra=(), env=None):
        manifest = Path(self.tmp.name) / f"{name}.json"
        argv = ["python3", str(STATE), "init", "--manifest", str(manifest),
                *map(str, self.init_args(f"{self.plan}#{wave_id}")), *map(str, extra)]
        environment = dict(os.environ, **(env or {}))
        proc = subprocess.run(argv, text=True, capture_output=True, env=environment)
        return manifest, proc

    def runs_data(self):
        return json.loads(self.runs.read_text(encoding="utf-8"))

    def test_cap_refuses_the_third_run_with_different_manifest_paths(self):
        flags = ["--max-runs", 2, "--runs-file", self.runs]
        first, proc = self.init_run("a", extra=flags)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["run"], {"index": 1, "max": 2})
        second, proc = self.init_run("b", extra=flags)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(second.read_text())["run"], {"index": 2, "max": 2})
        third, proc = self.init_run("c", extra=flags)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run cap reached for wave w1: 2/2 runs", proc.stderr)
        self.assertIn("BLOCKED: [class=blocked_cap rec=owner red=no]", proc.stderr)
        self.assertIn(str(first), proc.stderr)
        self.assertFalse(third.exists())
        runs = self.runs_data()["runs"]
        self.assertEqual([item["index"] for item in runs], [1, 2])
        self.assertEqual([item["manifest"] for item in runs], [str(first), str(second)])
        for item in runs:
            self.assertEqual(set(item), {"index", "manifest", "created_at", "plan_sha256"})
            self.assertRegex(item["plan_sha256"], r"^[0-9a-f]{64}$")

    def test_a_new_process_does_not_reset_the_counter(self):
        env = {"WAB_DIR": str(self.runs.parent), "WAB_MAX_RUNS": "1"}
        _manifest, proc = self.init_run("a", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.runs.exists())  # $WAB_DIR/runs.json by default
        again, proc = self.init_run("a2", env=env)  # a restarted session: fresh process
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run cap reached", proc.stderr)
        self.assertFalse(again.exists())

    def test_default_cap_is_two_and_flag_beats_environment(self):
        env = {"WAB_DIR": str(self.runs.parent)}
        for name in ("a", "b"):
            self.assertEqual(self.init_run(name, env=env)[1].returncode, 0)
        self.assertNotEqual(self.init_run("c", env=env)[1].returncode, 0)
        env = {"WAB_DIR": str(self.runs.parent / "other"), "WAB_MAX_RUNS": "1"}
        self.assertEqual(self.init_run("d", extra=["--max-runs", 3], env=env)[1].returncode, 0)
        self.assertEqual(self.init_run("e", extra=["--max-runs", 3], env=env)[1].returncode, 0)

    def test_a_failed_manifest_creation_does_not_count(self):
        flags = ["--max-runs", 1, "--runs-file", self.runs]
        _manifest, proc = self.init_run("bad", wave_id="nope", extra=flags)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.runs.exists())
        self.assertEqual(self.init_run("ok", extra=flags)[1].returncode, 0)

    DRIVER = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("state_kill", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mode, target = sys.argv[2], sys.argv[3]
real = module.write
def patched(path, value):
    if mode == "kill_after_first":
        real(path, value)
        os._exit(9)  # like SIGKILL: no except/finally runs
    if str(path) == target:  # the manifest write fails with an ordinary error
        raise OSError("disk full")
    real(path, value)
module.write = patched
module.init(module.parser().parse_args(sys.argv[4:]))
"""

    def run_driver(self, mode, name, flags):
        manifest = Path(self.tmp.name) / f"{name}.json"
        argv = ["python3", "-c", self.DRIVER, str(STATE), mode, str(manifest), "init",
                "--manifest", str(manifest),
                *map(str, self.init_args(f"{self.plan}#w1")), *map(str, flags)]
        return manifest, subprocess.run(argv, text=True, capture_output=True)

    def test_process_killed_between_the_two_writes_cannot_exceed_the_cap(self):
        flags = ["--max-runs", 1, "--runs-file", self.runs]
        _killed, proc = self.run_driver("kill_after_first", "killed", flags)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        again, proc = self.init_run("again", extra=flags)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run cap reached", proc.stderr)
        self.assertFalse(again.exists())

    def test_ordinary_manifest_write_error_restores_the_previous_counter(self):
        flags = ["--max-runs", 3, "--runs-file", self.runs]
        self.assertEqual(self.init_run("a", extra=flags)[1].returncode, 0)
        before = self.runs.read_text(encoding="utf-8")
        broken, proc = self.run_driver("fail_manifest", "broken", flags)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.runs.read_text(encoding="utf-8"), before)
        self.assertFalse(broken.exists())

    def test_invalid_max_runs_is_refused(self):
        for value in ("0", "-1", "x", "", "1.5", "true", "+2", " 2", "1001"):
            with self.subTest(flag=value):
                manifest, proc = self.init_run(
                    "f", extra=["--max-runs", value, "--runs-file", self.runs])
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertFalse(manifest.exists())
            with self.subTest(env=value):
                manifest, proc = self.init_run(
                    "g", env={"WAB_DIR": str(self.runs.parent), "WAB_MAX_RUNS": value})
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertFalse(manifest.exists())
        self.assertFalse(self.runs.exists())

    def test_flags_require_from_plan_and_a_runs_file(self):
        manifest = Path(self.tmp.name) / "plain.json"
        base = ["python3", str(STATE), "init", "--manifest", str(manifest), "--repo", self.repo,
                "--base", self.base, "--head", self.head()]
        for flags in (["--max-runs", "2"], ["--runs-file", str(self.runs)]):
            proc = subprocess.run([*map(str, base), *flags], text=True, capture_output=True)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("--from-plan", proc.stderr)
            self.assertFalse(manifest.exists())
        manifest, proc = self.init_run("nofile", extra=["--max-runs", 2])
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(manifest.exists())

    def test_without_wab_dir_and_flags_init_is_unchanged(self):
        manifest, proc = self.init_run("plain")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("run", json.loads(manifest.read_text()))
        self.assertFalse(self.runs.exists())
        # WAB_DIR without --from-plan: an ordinary run, the counter is not applied
        plain = Path(self.tmp.name) / "ordinary.json"
        proc = subprocess.run(
            ["python3", str(STATE), "init", "--manifest", str(plain), "--repo", str(self.repo),
             "--base", self.base, "--head", self.head()],
            text=True, capture_output=True,
            env=dict(os.environ, WAB_DIR=str(self.runs.parent)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.runs.exists())
        self.assertNotIn("run", json.loads(plain.read_text()))
        self.manifest = manifest
        self.assertIsNone(self.where()["run"])
        self.assertFalse(self.where()["last_run"])

    def test_runs_file_of_another_wave_or_a_damaged_one_is_refused(self):
        flags = ["--max-runs", 3, "--runs-file", self.runs]
        self.assertEqual(self.init_run("a", wave_id="w1", extra=flags)[1].returncode, 0)
        other, proc = self.init_run("b", wave_id="w2", extra=flags)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("w1", proc.stderr)
        self.assertFalse(other.exists())
        for raw in ("not json", "[]", json.dumps({"version": 2, "wave": "w1", "runs": []}),
                    json.dumps({"version": 1, "wave": "w1", "runs": "x"}),
                    json.dumps({"version": 1, "wave": "w1", "runs": [1]})):
            with self.subTest(raw=raw):
                self.runs.write_text(raw, encoding="utf-8")
                manifest, proc = self.init_run("c", wave_id="w1", extra=flags)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertFalse(manifest.exists())
                self.assertEqual(self.runs.read_text(encoding="utf-8"), raw)

    def test_max_runs_file_beats_env_and_a_raise_reaches_the_old_environment(self):  # r2 P1
        wab = self.runs.parent
        wab.mkdir(parents=True, exist_ok=True)
        env = {"WAB_DIR": str(wab), "WAB_MAX_RUNS": "2"}  # the session's env stays at the old cap
        for name in ("a", "b"):
            self.assertEqual(self.init_run(name, env=env)[1].returncode, 0)
        _m, proc = self.init_run("c", env=env)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("run cap reached", proc.stderr)
        (wab / "max-runs").write_text("3\n", encoding="utf-8")  # the dispatcher saw the owner's raise
        manifest, proc = self.init_run("c2", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(manifest.read_text())["run"], {"index": 3, "max": 3})

    def test_explicit_flag_beats_the_max_runs_file(self):
        wab = self.runs.parent
        wab.mkdir(parents=True, exist_ok=True)
        (wab / "max-runs").write_text("1\n", encoding="utf-8")
        env = {"WAB_DIR": str(wab)}
        self.assertEqual(self.init_run("a", extra=["--max-runs", 3], env=env)[1].returncode, 0)
        self.assertEqual(self.init_run("b", extra=["--max-runs", 3], env=env)[1].returncode, 0)

    def test_damaged_max_runs_file_is_a_closed_refusal(self):
        wab = self.runs.parent
        wab.mkdir(parents=True, exist_ok=True)
        for raw in ("", "\n", "x", "0", "-1", "1.5", "2 3", "1001", "9" * 5000):
            with self.subTest(raw=raw):
                (wab / "max-runs").write_text(raw, encoding="utf-8")
                manifest, proc = self.init_run("d", env={"WAB_DIR": str(wab)})
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("max-runs", proc.stderr)
                self.assertFalse(manifest.exists())
                self.assertFalse(self.runs.exists())

    def test_last_run_does_not_lead_acceptance_findings_out_of_fix_loop(self):  # r2 P1
        flags = ["--max-runs", 1, "--runs-file", self.runs]
        self.manifest, _ = self.init_run("a", extra=flags)
        self.record("coder")
        self.record("tester")
        self.record("cross_provider_reviewer", status="findings")
        info = self.where()
        self.assertTrue(info["last_run"])
        action = info["next_action"]
        self.assertIn("--outcome failed --source cross_provider_reviewer", action)
        self.assertIn("acceptance", action)
        self.assertIn("--defer", action)
        self.assertIn("--accept", action)
        self.assertNotIn("instead of a new fix round", action)

    def set_live_cap(self, value):
        (self.runs.parent).mkdir(parents=True, exist_ok=True)
        (self.runs.parent / "max-runs").write_text(value, encoding="utf-8")

    def where_live(self):
        env = dict(os.environ, WAB_DIR=str(self.runs.parent))
        proc = subprocess.run(
            ["python3", str(STATE), "where", "--manifest", str(self.manifest)],
            text=True, capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_where_follows_the_live_cap_down_and_up(self):  # #60 p.1
        self.manifest, _ = self.init_run(
            "a", extra=["--max-runs", 2, "--runs-file", self.runs])
        info = self.where_live()
        self.assertEqual((info["run"], info["last_run"]), ("1/2", False))
        self.set_live_cap("1\n")
        info = self.where_live()
        self.assertEqual((info["run"], info["last_run"]), ("1/1", True))
        self.set_live_cap("3\n")
        info = self.where_live()
        self.assertEqual((info["run"], info["last_run"]), ("1/3", False))

    def test_where_raising_the_cap_clears_last_run_at_old_max(self):  # #60 p.1
        flags = ["--max-runs", 2, "--runs-file", self.runs]
        self.init_run("a", extra=flags)
        self.manifest, _ = self.init_run("b", extra=flags)
        self.assertTrue(self.where_live()["last_run"])  # no file: manifest value
        self.set_live_cap("3")
        info = self.where_live()
        self.assertEqual((info["run"], info["last_run"]), ("2/3", False))

    def test_where_with_a_broken_live_cap_file_falls_back_to_the_manifest(self):  # #60 p.1
        self.manifest, _ = self.init_run(
            "a", extra=["--max-runs", 2, "--runs-file", self.runs])
        for junk in ("abc", "0", "", "-1", "99999999", "9" * 5000):  # 5000 digits: past int() str limit
            self.set_live_cap(junk)
            info = self.where_live()
            self.assertEqual((info["run"], info["last_run"]), ("1/2", False), junk)

    def test_where_reports_the_run_and_the_last_run_hint(self):
        flags = ["--max-runs", 2, "--runs-file", self.runs]
        first, _ = self.init_run("a", extra=flags)
        second, _ = self.init_run("b", extra=flags)
        self.manifest = first
        self.record("coder")
        self.record("tester")
        self.record("cross_provider_reviewer", status="findings")
        info = self.where()
        self.assertEqual((info["run"], info["last_run"]), ("1/2", False))
        self.assertIn("--outcome failed", info["next_action"])
        self.manifest = second
        self.record("coder")
        self.record("tester")
        self.record("cross_provider_reviewer", status="findings")
        info = self.where()
        self.assertEqual((info["run"], info["last_run"]), ("2/2", True))
        self.assertIn("--defer", info["next_action"])
        self.assertIn("--accept", info["next_action"])
        self.assertIn("--outcome failed", info["next_action"])  # acceptance findings keep the fix-loop


class WhereDerivationExhaustive(unittest.TestCase):
    """derive_step against an independent reference of the normative order."""

    ROLE_ORDER = (
        "coder",
        "tester",
        "cross_provider_reviewer",
        "github_codex_review",
        "coderabbit",
    )
    VALUES = (None, "pass", "findings", "incomplete", "error", "unavailable")

    @staticmethod
    def reference(task_status, verdict):
        """Returns (step, role, blocked, source) per the normative order 1-5."""
        if task_status == "blocked":
            return (None, None, False, None)
        if task_status == "needs_decision":
            return (6, "coordinator", False, None)
        if task_status == "needs_fix":
            return (6, "coder", False, None)
        reviewers = [
            ("tester", 5),
            ("cross_provider_reviewer", 5),
            ("github_codex_review", 7),
        ]
        for name, _ in reviewers:
            if verdict.get(name) == "findings":
                return (6, "coordinator", False, name)
        if verdict.get("coderabbit") == "findings":
            return (6, "coordinator", False, "coderabbit")
        for name, step in reviewers:
            if verdict.get(name) in ("error", "unavailable"):
                return (step, name, True, None)
        for name, step in reviewers:
            if verdict.get(name) == "incomplete":
                return (step, name, False, None)
        if verdict.get("coder") != "pass":
            return (4, "coder", False, None)
        for name, step in reviewers:
            if verdict.get(name) != "pass":
                return (step, name, False, None)
        return (7, None, False, None)

    @staticmethod
    def observed(module, task_status, verdict):
        step, role, note = module.derive_step({"status": task_status}, verdict)
        note = note or ""
        match = re.search(r"--source (\S+)", note)
        return (step, role, note.startswith("BLOCKED"), match and match.group(1))

    def load(self):
        spec = importlib.util.spec_from_file_location("state_precedence", STATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_all_combinations_match_reference(self):
        module = self.load()
        count = 0
        for task_status in ("in_progress", "needs_fix", "needs_decision", "blocked"):
            for combo in itertools.product(self.VALUES, repeat=len(self.ROLE_ORDER)):
                verdict = {
                    role: value
                    for role, value in zip(self.ROLE_ORDER, combo)
                    if value is not None
                }
                expected = self.reference(task_status, verdict)
                actual = self.observed(module, task_status, verdict)
                if actual != expected:
                    self.fail(f"{task_status} {verdict}: {actual} != {expected}")
                count += 1
        self.assertEqual(count, 4 * 6**5)

    def test_named_coderabbit_cases(self):
        module = self.load()
        required = {
            "coder": "pass",
            "tester": "pass",
            "cross_provider_reviewer": "pass",
            "github_codex_review": "pass",
        }
        for status in ("unavailable", "error", "incomplete", "pass"):
            with self.subTest(status=status):
                verdict = dict(required, coderabbit=status)
                self.assertEqual(
                    self.observed(module, "in_progress", verdict),
                    (7, None, False, None),
                )
        verdict = dict(required, coderabbit="findings")
        self.assertEqual(
            self.observed(module, "in_progress", verdict),
            (6, "coordinator", False, "coderabbit"),
        )
        # coderabbit noise never masks a required-role problem
        verdict = dict(required, tester="error", coderabbit="findings")
        self.assertEqual(
            self.observed(module, "in_progress", verdict),
            (6, "coordinator", False, "coderabbit"),
        )

    def test_named_incomplete_cases(self):
        module = self.load()
        full = {
            "coder": "pass",
            "tester": "pass",
            "cross_provider_reviewer": "pass",
            "github_codex_review": "pass",
        }
        cases = [
            ({"coder": "pass", "tester": "incomplete"}, (5, "tester", False, None)),
            (
                {"coder": "pass", "cross_provider_reviewer": "incomplete", "tester": "findings"},
                (6, "coordinator", False, "tester"),
            ),
            (
                {"coder": "pass", "github_codex_review": "incomplete",
                 "cross_provider_reviewer": "error"},
                (5, "cross_provider_reviewer", True, None),
            ),
            (dict(full, github_codex_review="incomplete"), (7, "github_codex_review", False, None)),
        ]
        for verdict, expected in cases:
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    self.observed(module, "in_progress", verdict), expected
                )
        step, role, note = module.derive_step(
            {"status": "in_progress"}, {"coder": "pass", "tester": "incomplete"}
        )
        self.assertIn("re-run tester with the missing context", note)
        self.assertIn("do not record fix-loop", note)

    def test_named_case_findings_beat_error(self):
        module = self.load()
        verdict = {
            "coder": "pass",
            "tester": "findings",
            "cross_provider_reviewer": "error",
        }
        self.assertEqual(
            self.observed(module, "in_progress", verdict),
            (6, "coordinator", False, "tester"),
        )



class AcceptanceIdentity(unittest.TestCase):  # #54: an acceptance covers one exact result
    def setUp(self):
        spec = importlib.util.spec_from_file_location("state_acceptance_identity", STATE)
        self.st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.st)

    def test_is_covered_is_deferral_or_acceptance_of_the_same_result(self):
        st = self.st
        first = {"status": "findings", "head": "h", "recorded_at": "t", "session_id": "s"}
        rerun = dict(first, session_id="other")
        record = {"source": "cross_provider_reviewer", "head": "h",
                  "result_sha256": st.result_digest(first)}
        for key in ("acceptances", "deferrals"):
            entry = {key: [record]}
            self.assertTrue(st.is_covered(entry, "cross_provider_reviewer", first), key)
            self.assertFalse(st.is_covered(entry, "cross_provider_reviewer", rerun), key)
            self.assertFalse(st.is_covered(entry, "github_codex_review", first), key)
            self.assertFalse(st.is_covered(entry, "tester", first), key)
        self.assertTrue(st.is_accepted({"acceptances": [record]}, "cross_provider_reviewer", first))
        self.assertFalse(st.is_accepted({"deferrals": [record]}, "cross_provider_reviewer", first))
        self.assertFalse(st.is_covered({"acceptances": [record]}, "cross_provider_reviewer",
                                       dict(first, status="pass")))


class DeferralIdentity(unittest.TestCase):  # #36 п.3: a deferral covers one exact result
    def setUp(self):
        spec = importlib.util.spec_from_file_location("state_deferral_identity", STATE)
        self.state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.state)

    def test_rerun_on_the_same_head_and_second_needs_its_own_deferral(self):
        st = self.state
        first = {"status": "findings", "head": "a" * 40, "session_id": "s1",
                 "recorded_at": "2026-10-02T18:00:00Z"}
        rerun = dict(first, session_id="s2")  # same head, same second, another result
        entry = {"deferrals": [{"source": "cross_provider_reviewer", "head": first["head"],
                                "result_sha256": st.result_digest(first), "note": "low",
                                "recorded_at": first["recorded_at"]}]}
        self.assertTrue(st.is_deferred(entry, "cross_provider_reviewer", first))
        self.assertFalse(st.is_deferred(entry, "cross_provider_reviewer", rerun))
        self.assertFalse(st.is_deferred(entry, "github_codex_review", first))  # another role
        self.assertFalse(st.is_deferred(entry, "tester", dict(first)))  # tester is never deferred
        moved = dict(first, head="b" * 40)
        self.assertFalse(st.is_deferred(entry, "cross_provider_reviewer", moved))
        # a byte-identical rerun differs only by its result_id, and that is enough
        a, b = dict(first, result_id="1" * 32), dict(first, result_id="2" * 32)
        entry["deferrals"][0]["result_sha256"] = st.result_digest(a)
        self.assertTrue(st.is_deferred(entry, "cross_provider_reviewer", a))
        self.assertFalse(st.is_deferred(entry, "cross_provider_reviewer", b))

class Sha256RepositoryState(unittest.TestCase):
    """issue #2: репозиторий с --object-format=sha256, полные 64-hex ID."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--object-format=sha256", "-b", "main")
        self.git("config", "user.email", "tester@example.invalid")
        self.git("config", "user.name", "State Tester")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "change")
        self.head = self.git("rev-parse", "HEAD")
        self.manifest = Path(self.tmp.name) / "run.json"

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    def cli(self, command, *args):
        return subprocess.run(
            ["python3", str(STATE), command, "--manifest", str(self.manifest), *map(str, args)],
            text=True, capture_output=True,
        )

    def test_init_and_task_result_accept_full_64_hex_ids(self):
        self.assertEqual((len(self.base), len(self.head)), (64, 64))
        proc = self.cli(
            "init", "--repo", self.repo, "--base", self.base, "--head", self.head,
            "--run-id", "sha256-run",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual((data["base"], data["head"]), (self.base, self.head))
        proc = self.cli(
            "task-result", "--task", "implement", "--role", "coder", "--status", "pass",
            "--session-id", "coder-1", "--head", self.head, "--artifact", "evidence",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.cli(
            "task-result", "--task", "implement", "--role", "tester", "--status", "pass",
            "--session-id", "tester-1", "--head", self.head, "--artifact", "evidence",
        )
        proc = self.cli(
            "task-result", "--task", "implement", "--role", "cross_provider_reviewer",
            "--status", "pass", "--session-id", "xp-1", "--head", self.head,
            "--artifact", "evidence", "--reviewed-head", self.head,
            "--packet-hash", "ef56" * 16,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(self.manifest.read_text(encoding="utf-8"))["tasks"]["implement"]["results"]["cross_provider_reviewer"]
        self.assertEqual(result["reviewed_head"], self.head)
        self.assertEqual(result["packet_hash"], "sha256:" + "ef56" * 16)

    def test_abbreviated_head_is_rejected(self):
        self.cli(
            "init", "--repo", self.repo, "--base", self.base, "--head", self.head,
            "--run-id", "sha256-run",
        )
        proc = self.cli(
            "task-result", "--task", "implement", "--role", "coder", "--status", "pass",
            "--session-id", "coder-1", "--head", self.head[:40], "--artifact", "evidence",
        )
        self.assertNotEqual(proc.returncode, 0)


# ---------------------------------------------------------------- 1.2.1 (#86): model by risk, two reviews
REVIEW = ROOT / "skills" / "superarmanda" / "scripts" / "review.py"
GATE = ROOT / "skills" / "superarmanda" / "scripts" / "waves" / "gate.py"
LIVE_MANIFESTS = ROOT / "tests" / "fixtures" / "manifests"
FABLE = "claude-fable-5-1"
SONNET = "claude-sonnet-5-5"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_review_clis():
    """The subscription CLI doubles of the review suite (tests/helpers/superarmanda_review_test.py):
    review reports in these tests are written by review.py itself, not by hand."""
    suite = _load("superarmanda_review_suite", Path(__file__).with_name("superarmanda_review_test.py"))
    return {"claude": suite.MOCK, "codex": suite.CODEX_MOCK}


class PolicyBase(unittest.TestCase):
    """A manifest version 2 made by state.py over a throwaway repository with one change."""

    RISK = "high"
    TASK = "t1"

    def setUp(self):
        for key in [k for k in os.environ if k.startswith("WAB_")] + ["SUPERARMANDA_TZ"]:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.git("config", "user.email", "tester@example.invalid")
        self.git("config", "user.name", "State Tester")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.base = self.head()
        self.change("changed\n")
        self.manifest = self.root / "run.json"
        self.serial = itertools.count(1)
        self.init()

    # ----- repository and state CLI -----
    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)], text=True, capture_output=True, check=True
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def change(self, text):
        (self.repo / "tracked.txt").write_text(text, encoding="utf-8")
        self.git("commit", "-qam", "change")
        return self.head()

    def state(self, command, *args):
        return subprocess.run(
            ["python3", str(STATE), command, "--manifest", str(self.manifest), *map(str, args)],
            text=True, capture_output=True,
        )

    def ok(self, command, *args):
        proc = self.state(command, *args)
        self.assertEqual(proc.returncode, 0, f"{command}: {proc.stderr}")
        return json.loads(proc.stdout)

    def refused(self, command, *args, needle=None):
        """A closed refusal: non-zero code and a byte-identical manifest."""
        before = self.manifest.read_bytes()
        proc = self.state(command, *args)
        self.assertNotEqual(proc.returncode, 0, f"{command} {args} was accepted: {proc.stdout}")
        self.assertEqual(self.manifest.read_bytes(), before, "a refusal must not change the manifest")
        if needle is not None:
            self.assertIn(needle, proc.stderr)
        return proc

    def init(self, *extra, risk=None):
        self.manifest.unlink(missing_ok=True)
        risk = self.RISK if risk is None else risk
        flags = ["--risk", risk] if risk else []
        return self.ok("init", "--repo", self.repo, "--base", self.base, "--head", self.head(),
                       "--run-id", "policy", *flags, *extra)

    def resume(self):
        return self.ok("resume", "--repo", self.repo, "--base", self.base, "--head", self.head())

    def data(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def entry(self, task=None):
        return self.data()["tasks"][task or self.TASK]

    def where(self):
        return self.ok("where")

    def result_args(self, role, status="pass", task=None, session=None, **flags):
        args = ["--task", task or self.TASK, "--role", role, "--status", status,
                "--session-id", session or f"{role}-{next(self.serial)}", "--head", self.head()]
        flags.setdefault("artifact", "evidence")
        for name, value in flags.items():
            if value is not None:
                args += ["--" + name.replace("_", "-"), value]
        return args

    def record(self, role, status="pass", **flags):
        return self.ok("task-result", *self.result_args(role, status, **flags))

    def record_refused(self, role, status="pass", needle=None, **flags):
        return self.refused("task-result", *self.result_args(role, status, **flags), needle=needle)

    # ----- review reports written by review.py over the subscription CLI doubles -----
    def packet(self, requirements="Must review the patch.\n"):
        number = next(self.serial)
        if not hasattr(self, "bin"):
            self.bin = self.root / "bin"
            self.bin.mkdir()
            for name, source in fake_review_clis().items():
                (self.bin / name).write_text(source, encoding="utf-8")
                (self.bin / name).chmod(0o755)
        requirements_file = self.root / f"requirements-{number}.txt"
        requirements_file.write_text(requirements, encoding="utf-8")
        evidence = self.root / f"evidence-{number}.txt"
        evidence.write_text("synthetic checks passed\n", encoding="utf-8")
        path = self.root / f"packet-{number}.json"
        proc = subprocess.run(
            ["python3", str(REVIEW), "packet", "--repo", str(self.repo), "--base", self.base,
             "--head", self.head(), "--requirements", str(requirements_file),
             "--test-evidence", str(evidence), "--output", str(path)],
            text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.packet_path = path
        self.packet_hash = "sha256:" + json.loads(path.read_text(encoding="utf-8"))["packet_hash"]
        return path

    def report(self, profile, mode="success", packet=None):
        """Run review.py for `profile`; return the path of the report it wrote."""
        if packet is None and not hasattr(self, "packet_path"):
            self.packet()
        output = self.root / f"report-{profile}-{next(self.serial)}.json"
        env = os.environ.copy()
        env.update({"PATH": str(self.bin) + os.pathsep + env.get("PATH", ""), "SA_TEST_MODE": mode,
                    "SA_TEST_LOG": str(self.root / "cli-log.jsonl")})
        subprocess.run(
            ["python3", str(REVIEW), "run", "--repo", str(self.repo), "--packet",
             str(packet or self.packet_path), "--profile", profile, "--output", str(output),
             "--timeout", "2" if mode == "timeout" else "20"],
            text=True, capture_output=True, env=env,
        )
        self.assertTrue(output.exists(), f"review.py wrote no report for {profile}/{mode}")
        return output

    def review_flags(self, report, packet_hash=None, **flags):
        return dict(artifact=str(report), reviewed_head=self.head(),
                    packet_hash=packet_hash or self.packet_hash, **flags)

    def review(self, role, profile, mode="success", status="pass", **flags):
        report = self.report(profile, mode)
        return self.record(role, status, **self.review_flags(report, **flags)), report

    def code_and_test(self, model="fable"):
        self.record("coder", model=model)
        self.record("tester", model=model)

    def edited(self, report, **changes):
        """A copy of a review.py report with the named top-level fields replaced (None removes)."""
        value = json.loads(Path(report).read_text(encoding="utf-8"))
        for key, new in changes.items():
            if new is None:
                value.pop(key, None)
            else:
                value[key] = new
        path = self.root / f"edited-{next(self.serial)}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path


class RiskPolicyManifest(PolicyBase):
    """Manifest version 2: the review policy field and its closed validation."""

    def write_plan(self, risk):
        plan = self.root / "waves.json"
        plan.write_text(json.dumps(plan_doc([wave("w1", risk=risk)])), encoding="utf-8")
        return f"{plan}#w1"

    def test_init_writes_version_2_with_policy_level_and_version(self):
        self.assertEqual(self.data()["version"], 2)
        self.assertEqual(self.data()["review_policy"], {"version": "1.2.1", "level": "high"})
        self.manifest.unlink()
        printed = self.ok("init", "--repo", self.repo, "--base", self.base, "--head", self.head())
        self.assertEqual(printed["review_policy"], {"version": "1.2.1", "level": "low"})
        self.assertEqual(self.data()["version"], 2)
        for risk in ("low", "medium", "high"):
            with self.subTest(wave_risk=risk):
                self.manifest.unlink()
                self.ok("init", "--repo", self.repo, "--base", self.base, "--head", self.head(),
                        "--from-plan", self.write_plan(risk))
                self.assertEqual(self.data()["review_policy"], {"version": "1.2.1", "level": risk})
                self.assertEqual(self.data()["version"], 2)

    def test_init_refuses_risk_with_from_plan_and_unknown_risk(self):
        self.manifest.unlink()
        for flags in (["--risk", "high", "--from-plan", self.write_plan("low")], ["--risk", "urgent"],
                      ["--risk", ""]):
            with self.subTest(flags=flags):
                proc = self.state("init", "--repo", self.repo, "--base", self.base, "--head", self.head(), *flags)
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(self.manifest.exists())

    def test_unknown_or_missing_policy_is_a_closed_refusal_for_every_command(self):
        self.code_and_test()
        good = self.data()

        def broken(change):
            value = copy.deepcopy(good)
            change(value)
            return value

        cases = {
            "version 3": broken(lambda v: v.update(version=3)),
            "version string": broken(lambda v: v.update(version="2")),
            "version missing": broken(lambda v: v.pop("version")),
            "policy removed": broken(lambda v: v.pop("review_policy")),
            "policy not an object": broken(lambda v: v.update(review_policy="1.2.1")),
            "policy extra key": broken(lambda v: v["review_policy"].update(extra=1)),
            "policy without level": broken(lambda v: v["review_policy"].pop("level")),
            "policy without version": broken(lambda v: v["review_policy"].pop("version")),
            "unknown policy version": broken(lambda v: v["review_policy"].update(version="9.9.9")),
            "unknown level": broken(lambda v: v["review_policy"].update(level="urgent")),
            "level not a string": broken(lambda v: v["review_policy"].update(level=["high"])),
            "unknown task risk": broken(lambda v: v["tasks"]["t1"].update(risk="urgent")),
            "v1 carrying a policy": broken(lambda v: v.update(version=1)),
        }
        commands = {
            "status": [],
            "where": [],
            "resume": ["--repo", self.repo, "--base", self.base, "--head", self.head()],
            "task-result": self.result_args("tester", model="fable"),
            "fix-loop": ["--task", "t1", "--outcome", "failed", "--source", "tester"],
            "mark": ["--task", "t1", "--step", "4", "--safe-point", "false"],
            "role-model": ["--task", "t1", "--role", "coder"],
            "task-risk": ["--task", "t1", "--risk", "high"],
        }
        for label, value in cases.items():
            self.manifest.write_text(json.dumps(value), encoding="utf-8")
            for command, args in commands.items():
                with self.subTest(case=label, command=command):
                    self.refused(command, *args)

    def test_role_model_follows_the_effective_risk(self):
        for level, model in (("high", FABLE), ("medium", SONNET), ("low", SONNET)):
            self.init(risk=level)
            before = self.manifest.read_bytes()
            for role in ("coder", "tester"):
                with self.subTest(level=level, role=role):
                    answer = self.ok("role-model", "--task", "t1", "--role", role)
                    self.assertEqual(answer, {"task": "t1", "role": role, "risk": level, "model": model,
                                              "policy_version": "1.2.1"})
            self.assertEqual(self.manifest.read_bytes(), before, "role-model is read-only")
        # a task raised to high inside a low run gets Fable; its neighbour keeps Sonnet
        self.ok("task-risk", "--task", "t1", "--risk", "high")
        self.assertEqual(self.ok("role-model", "--task", "t1", "--role", "tester")["model"], FABLE)
        self.assertEqual(self.ok("role-model", "--task", "t2", "--role", "coder")["model"], SONNET)
        self.refused("role-model", "--task", "t1", "--role", "cross_provider_reviewer")

    def test_task_risk_can_be_raised_or_repeated_never_lowered(self):
        self.init(risk="low")
        self.assertEqual(self.ok("task-risk", "--task", "t1", "--risk", "medium")["risk"], "medium")
        self.assertEqual(self.ok("task-risk", "--task", "t1", "--risk", "medium")["risk"], "medium")
        self.refused("task-risk", "--task", "t1", "--risk", "low", needle="lower")
        self.assertEqual(self.ok("task-risk", "--task", "t1", "--risk", "high")["risk"], "high")
        self.refused("task-risk", "--task", "t1", "--risk", "medium", needle="lower")
        self.refused("task-risk", "--task", "t1", "--risk", "urgent")
        self.assertEqual(self.entry()["risk"], "high")
        self.assertEqual(self.where()["risk"], "high")

    def test_high_wave_is_the_floor_for_a_medium_task(self):
        """A medium task of a high wave: one Astra pass is not a pass, and its roles get Fable."""
        self.manifest.unlink()
        self.ok("init", "--repo", self.repo, "--base", self.base, "--head", self.head(),
                "--from-plan", self.write_plan("high"))
        self.ok("task-risk", "--task", "t1", "--risk", "medium")
        self.assertEqual(self.entry()["risk"], "medium")
        for role in ("coder", "tester"):
            self.assertEqual(self.ok("role-model", "--task", "t1", "--role", role)["model"], FABLE)
        self.record_refused("coder", model="sonnet")
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        self.assertEqual(self.entry()["status"], "in_progress")
        where = self.where()
        self.assertEqual((where["risk"], where["role"]), ("high", "second_reviewer"))
        self.assertNotIn("done", where["next_action"])


class HighRiskModels(PolicyBase):
    """Coder and tester of a high-risk task run on Fable, and the result says so."""

    def test_high_coder_and_tester_require_the_fable_model(self):
        for role in ("coder", "tester"):
            for model in (None, "", "sonnet", SONNET, "claude-opus-5-5", "gpt-6-astra"):
                with self.subTest(role=role, model=model):
                    self.record_refused(role, model=model)
        self.assertEqual(self.data()["tasks"], {})
        self.record_refused("coder", needle=FABLE)
        entry = self.record("coder", model=FABLE)
        self.assertEqual(entry["results"]["coder"]["model"], FABLE)

    def test_refusal_of_another_model_names_the_way_out(self):
        """PR #90 P1: a host without Fable (Codex host, #92) is told what to record, not left guessing."""
        for role in ("coder", "tester"):
            for model in (None, "sonnet", "gpt-5.6-terra", "gpt-6-astra", ""):
                with self.subTest(role=role, model=model):
                    message = self.record_refused(role, model=model).stderr
                    for needle in ("claude-fable-5-1", "--status unavailable --model fable",
                                   "no substitution", "not pass"):
                        self.assertIn(needle, message)
        entry = self.record("coder", "unavailable", model="fable")
        self.assertEqual((entry["results"]["coder"]["status"], entry["status"]), ("unavailable", "in_progress"))
        # below high a host is free to run its own models and to leave --model out, as in 1.2.0
        self.init(risk="medium")
        self.record("coder")
        self.record("tester")

    def test_fable_alias_is_stored_canonically_and_lookalikes_are_rejected(self):
        for wrong in ("not-fable", "fable-lite", "Fable", "FABLE", "fable ", " fable", "fable\n",
                      FABLE + "-x", "x-" + FABLE, "claude-fable", "claude-fable-5", ""):
            with self.subTest(model=wrong):
                self.record_refused("coder", model=wrong)
                self.record_refused("tester", status="unavailable", model=wrong)
        self.assertEqual(self.record("coder", model="fable")["results"]["coder"]["model"], FABLE)
        self.assertEqual(self.record("tester", model="fable")["results"]["tester"]["model"], FABLE)

    def test_medium_task_model_is_optional_but_only_from_the_dictionary(self):
        self.init(risk="medium")
        self.assertNotIn("model", self.record("coder")["results"]["coder"])
        self.assertEqual(self.record("coder", model="sonnet")["results"]["coder"]["model"], SONNET)
        self.assertEqual(self.record("tester", model="fable")["results"]["tester"]["model"], FABLE)
        for wrong in ("", "gpt-6-astra", "Sonnet", "sonnet-lite"):
            self.record_refused("tester", model=wrong)
        # the model of a reviewer comes from its verified report, never from a flag
        for role in ("cross_provider_reviewer", "second_reviewer", "github_codex_review", "coderabbit"):
            self.record_refused(role, status="incomplete", model="fable")

    def test_unavailable_fable_is_recorded_and_never_replaced_by_a_weaker_model(self):
        for role, others in (("coder", ()), ("tester", ("coder",))):
            with self.subTest(role=role):
                self.init()
                for other in others:
                    self.record(other, model="fable")
                entry = self.record(role, "unavailable", model="fable")
                self.assertEqual(
                    (entry["results"][role]["status"], entry["results"][role]["model"]), ("unavailable", FABLE)
                )
                self.assertNotEqual(entry["status"], "ready_for_pr_review")
                where = self.where()
                self.assertEqual(where["role"], role)
                self.assertTrue(where["next_action"].startswith("BLOCKED"), where["next_action"])
                self.assertIn("weaker model", where["next_action"])
                self.assertNotIn("must finish", where["next_action"])
                self.assertEqual(
                    next(a for a in where["artifacts"] if a["role"] == role)["model"], FABLE
                )
                # Sonnet cannot take the role over, whatever status it reports
                for status in ("pass", "unavailable", "error"):
                    self.record_refused(role, status, model="sonnet")
                self.record_refused(role, "unavailable")
                self.assertEqual(self.entry()["results"][role]["status"], "unavailable")

    def test_risk_raised_after_results_takes_readiness_back(self):
        self.init(risk="medium")
        self.record("coder", model="sonnet")
        self.record("tester")
        self.review("cross_provider_reviewer", "claude-host")
        self.assertEqual(self.entry()["status"], "ready_for_pr_review")
        raised = self.ok("task-risk", "--task", "t1", "--risk", "high")
        self.assertEqual(raised["status"], "in_progress")
        where = self.where()
        self.assertEqual((where["step"], where["role"]), (4, "coder"))
        self.assertIn(FABLE, where["next_action"])
        self.assertIn(SONNET, where["next_action"])
        # redoing the roles on Fable and adding both verified reviews makes it ready again
        self.code_and_test()
        self.assertEqual(self.where()["role"], "cross_provider_reviewer")  # recorded without a verified report
        self.review("cross_provider_reviewer", "claude-host")
        self.review("second_reviewer", "codex-host")
        self.assertEqual(self.entry()["status"], "ready_for_pr_review")


class HighRiskReviews(PolicyBase):
    """A high-risk task passes only with two verified reviews: Astra (claude-host) and Fable (codex-host)."""

    def test_one_astra_pass_is_not_enough_and_astra_plus_fable_passes(self):
        self.code_and_test()
        entry, astra = self.review("cross_provider_reviewer", "claude-host")
        self.assertEqual(entry["status"], "in_progress")
        where = self.where()
        self.assertEqual((where["step"], where["role"]), (5, "second_reviewer"))
        entry, fable = self.review("second_reviewer", "codex-host")
        self.assertEqual(entry["status"], "ready_for_pr_review")
        for role, profile, report in (("cross_provider_reviewer", "claude-host", astra),
                                      ("second_reviewer", "codex-host", fable)):
            stored = entry["results"][role]
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["gate_ready"])
            self.assertEqual(stored["profile"], profile)
            self.assertEqual(stored["artifact"], str(report))
            self.assertEqual(stored["artifact_sha256"], hashlib.sha256(report.read_bytes()).hexdigest())
            self.assertEqual(stored["packet_hash"], self.packet_hash)
        where = self.where()
        self.assertEqual((where["step"], where["role"]), (7, "github_codex_review"))
        self.assertEqual(where["review_policy"], {"version": "1.2.1", "level": "high"})
        self.assertEqual({a["role"]: a["profile"] for a in where["artifacts"]},
                         {"coder": None, "tester": None, "cross_provider_reviewer": "claude-host",
                          "second_reviewer": "codex-host"})
        self.record("github_codex_review", reviewed_head=self.head(), artifact="https://example.invalid/pr/1")
        self.assertIn("done", self.where()["next_action"])

    def test_roles_may_swap_profiles_but_the_pair_must_be_astra_and_fable(self):
        self.code_and_test()
        self.review("cross_provider_reviewer", "codex-host")
        entry, _ = self.review("second_reviewer", "claude-host")
        self.assertEqual(entry["status"], "ready_for_pr_review")

    def test_two_astra_reviews_or_fable_without_astra_do_not_pass(self):
        for profile in ("claude-host", "codex-host"):
            with self.subTest(profile=profile):
                self.init()
                self.code_and_test()
                self.review("cross_provider_reviewer", profile)
                # another review of the same profile; the CLI doubles reuse one session id, so the
                # report of the second run gets its own (one session cannot fill both roles)
                again = self.edited(self.report(profile), session_id="another-review-session")
                entry = self.record("second_reviewer", **self.review_flags(again))
                self.assertEqual(entry["status"], "in_progress")
                where = self.where()
                self.assertNotIn("done", where["next_action"])
                self.assertEqual((where["step"], where["role"]), (5, "second_reviewer"))
                self.assertIn("claude-host", where["next_action"])
                self.assertIn("codex-host", where["next_action"])

    def test_reviews_of_different_packets_do_not_pass(self):
        self.code_and_test()
        self.packet("First packet.\n")
        self.review("cross_provider_reviewer", "claude-host")
        self.packet("Another packet of the same head.\n")
        entry, _ = self.review("second_reviewer", "codex-host")
        self.assertEqual(entry["status"], "in_progress")
        where = self.where()
        self.assertEqual(where["role"], "second_reviewer")
        self.assertIn("packet", where["next_action"])

    def test_fable_review_failure_never_passes_and_never_falls_back(self):
        """auth, timeout and an unavailable Fable: recorded, not a pass, no Sonnet/Astra stand-in."""
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        for mode, category in (("claude_auth_unauthorized", "auth"), ("timeout", "timeout")):
            with self.subTest(mode=mode):
                failed = self.report("codex-host", mode)
                self.assertEqual(json.loads(failed.read_text(encoding="utf-8"))["error_category"], category)
                # the failed report cannot be recorded as a pass or as findings
                for status in ("pass", "findings"):
                    self.record_refused("second_reviewer", status, **self.review_flags(failed))
                for status in ("error", "unavailable"):
                    entry = self.record("second_reviewer", status, artifact=str(failed))
                    self.assertEqual(entry["status"], "in_progress")
                    where = self.where()
                    self.assertEqual((where["step"], where["role"]), (5, "second_reviewer"))
                    self.assertTrue(where["next_action"].startswith("BLOCKED"), where["next_action"])
                    self.assertIn("codex-host-opus", where["next_action"])
                    self.assertIn("quota", where["next_action"])
        # without a quota report the Opus route is closed, and Astra twice is not a pair
        opus = self.report("codex-host-opus")
        self.record_refused("second_reviewer", **self.review_flags(opus))
        self.record_refused("second_reviewer", **self.review_flags(opus, quota_evidence=str(failed)))
        again = self.edited(self.report("claude-host"), session_id="another-review-session")
        entry = self.record("second_reviewer", **self.review_flags(again))
        self.assertEqual(entry["status"], "in_progress")
        # an unverifiable report (not written by review.py) is never a second review
        self.record_refused("second_reviewer", reviewed_head=self.head(), packet_hash=self.packet_hash)
        self.record_refused("second_reviewer", artifact=str(self.root / "missing.json"),
                            reviewed_head=self.head(), packet_hash=self.packet_hash)

    def test_fable_quota_report_opens_the_opus_fallback_and_both_artifacts_are_recorded(self):
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        quota = self.report("codex-host", "quota_failure")
        evidence = json.loads(quota.read_text(encoding="utf-8"))
        self.assertEqual((evidence["profile"], evidence["status"], evidence["error_category"]),
                         ("codex-host", "error", "quota"))
        opus = self.report("codex-host-opus")
        entry = self.record("second_reviewer", **self.review_flags(opus, quota_evidence=str(quota)))
        self.assertEqual(entry["status"], "ready_for_pr_review")
        stored = entry["results"]["second_reviewer"]
        self.assertEqual(stored["profile"], "codex-host-opus")
        self.assertEqual(stored["fallback_for"], "codex-host")
        self.assertEqual(stored["artifact"], str(opus))
        self.assertEqual(stored["artifact_sha256"], hashlib.sha256(opus.read_bytes()).hexdigest())
        self.assertEqual(stored["quota_evidence"],
                         {"artifact": str(quota), "sha256": hashlib.sha256(quota.read_bytes()).hexdigest()})
        shown = next(a for a in self.where()["artifacts"] if a["role"] == "second_reviewer")
        self.assertEqual((shown["profile"], shown["fallback_for"]), ("codex-host-opus", "codex-host"))
        self.assertEqual(shown["quota_evidence"], stored["quota_evidence"])

    def test_opus_without_evidence_or_with_foreign_evidence_does_not_count(self):
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        old_head_quota = self.report("codex-host", "quota_failure")
        self.change("second change\n")
        self.resume()
        self.code_and_test()
        self.packet()
        self.review("cross_provider_reviewer", "claude-host")
        opus = self.report("codex-host-opus")
        quota = self.report("codex-host", "quota_failure")
        packet_path, packet_hash = self.packet_path, self.packet_hash
        foreign_quota = self.report("codex-host", "quota_failure",
                                    packet=self.packet("Another packet of the new head.\n"))
        self.assertNotEqual(self.packet_hash, packet_hash)
        self.packet_path, self.packet_hash = packet_path, packet_hash
        cases = {
            "no evidence": None,
            "evidence of another head": old_head_quota,
            "evidence of another packet": foreign_quota,
            "evidence without head and packet (old format)":
                self.edited(quota, reviewed_head=None, state_packet_hash=None),
            "evidence without packet": self.edited(quota, state_packet_hash=None),
            "evidence is an auth error": self.edited(quota, error_category="auth"),
            "evidence is not an error": self.edited(quota, status="incomplete"),
            "evidence claims gate_ready": self.edited(quota, gate_ready=True),
            "evidence of astra": self.edited(quota, profile="claude-host"),
            "evidence of opus itself": self.edited(quota, profile="codex-host-opus"),
            "evidence is the opus pass": opus,
            "evidence is missing": self.root / "no-such-report.json",
        }
        for label, evidence in cases.items():
            with self.subTest(case=label):
                flags = self.review_flags(opus, quota_evidence=None if evidence is None else str(evidence))
                self.record_refused("second_reviewer", **flags)
        self.assertNotIn("second_reviewer", self.entry()["results"])
        # a stored Opus result that lost its evidence is not a second review either
        self.record("second_reviewer", **self.review_flags(opus, quota_evidence=str(quota)))
        self.assertEqual(self.entry()["status"], "ready_for_pr_review")
        value = self.data()
        del value["tasks"]["t1"]["results"]["second_reviewer"]["quota_evidence"]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.record("tester", model="fable")
        self.assertEqual(self.entry()["status"], "in_progress")
        self.assertIn("quota", self.where()["next_action"])

    def test_auth_failure_that_mentions_quota_is_not_quota_evidence(self):
        """tester-1 F1: a failed Fable run is quota evidence only on the provider's own signal."""
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        opus = self.report("codex-host-opus")
        for mode in ("auth_quota_word", "auth_quota_word_stderr", "model_quota_word",
                     "model_forged_event_text", "quota_word_stderr", "rate_limit_word_stderr",
                     "quota_and_auth_stderr", "quota_and_auth_code", "quota_duplicate_error_key"):
            with self.subTest(mode=mode):
                failed = self.report("codex-host", mode)
                self.assertNotEqual(json.loads(failed.read_text(encoding="utf-8"))["error_category"], "quota")
                self.record_refused("second_reviewer", **self.review_flags(opus, quota_evidence=str(failed)))
        self.assertEqual(self.entry()["status"], "in_progress")
        self.assertNotIn("second_reviewer", self.entry()["results"])
        # the provider's signal (out of credits, session limit) still opens the fallback
        for mode in ("quota_credits", "quota_failure", "quota_exit_zero"):
            with self.subTest(mode=mode):
                quota = self.report("codex-host", mode)
                entry = self.record("second_reviewer", **self.review_flags(opus, quota_evidence=str(quota)))
                self.assertEqual(entry["status"], "ready_for_pr_review")

    def test_report_must_agree_with_its_profile_and_one_review_cannot_fill_both_roles(self):
        """tester-1 F2: relabelling one field of a report, or reusing one review, is refused."""
        self.code_and_test()
        astra = self.report("claude-host")
        fable = self.report("codex-host")
        opus = self.report("codex-host-opus")
        self.record("cross_provider_reviewer", **self.review_flags(astra))
        reformatted = self.root / "reformatted.json"
        reformatted.write_text(json.dumps(json.loads(astra.read_text(encoding="utf-8")), indent=4),
                               encoding="utf-8")
        cases = {
            "astra relabelled as fable": self.edited(astra, profile="codex-host"),
            "astra relabelled as opus": self.edited(astra, profile="codex-host-opus"),
            "opus relabelled as fable": self.edited(opus, profile="codex-host"),
            "fable relabelled as astra": self.edited(fable, profile="claude-host"),
            "the same report again": astra,
            "the same review, other bytes": reformatted,
            "requested model of another profile": self.edited(fable, requested_model="gpt-6-astra"),
            "requested model is the full id": self.edited(fable, requested_model=FABLE),
            "no requested model": self.edited(fable, requested_model=None),
            "observed models of another profile": self.edited(fable, observed_models=["gpt-6-astra"]),
            "sonnet observed": self.edited(fable, observed_models={SONNET: {"inputTokens": 1}}),
            "no observed models": self.edited(fable, observed_models=None),
            "observed models is a string": self.edited(fable, observed_models=FABLE),
            "no review session": self.edited(fable, session_id=None),
            "empty review session": self.edited(fable, session_id=""),
        }
        for label, report in cases.items():
            with self.subTest(case=label):
                self.record_refused("second_reviewer", **self.review_flags(report))
        self.assertEqual(self.entry()["status"], "in_progress")
        entry = self.record("second_reviewer", **self.review_flags(fable))
        self.assertEqual(entry["status"], "ready_for_pr_review")
        # readiness names it too, should two results of one review ever sit in a manifest
        value = self.data()
        results = value["tasks"]["t1"]["results"]
        results["second_reviewer"]["review_session_id"] = results["cross_provider_reviewer"]["review_session_id"]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.record("tester", model="fable")
        self.assertEqual(self.entry()["status"], "in_progress")
        self.assertIn("same review", self.where()["next_action"])

    def test_opus_is_refused_once_fable_reviewed_this_head_and_packet(self):
        """tester-2 F1: the fallback stands in for a Fable that gave no review, never over one."""
        for status, mode in (("findings", "findings"), ("pass", "success")):
            for fable_role, opus_role in (("second_reviewer", "second_reviewer"),
                                          ("cross_provider_reviewer", "second_reviewer")):
                with self.subTest(fable=status, fable_role=fable_role, opus_role=opus_role):
                    self.init()
                    self.code_and_test()
                    quota = self.report("codex-host", "quota_failure")
                    # its own session: the CLI doubles give Fable and Opus one session id
                    opus = self.edited(self.report("codex-host-opus"), session_id="opus-review-session")
                    if fable_role != "cross_provider_reviewer":
                        self.review("cross_provider_reviewer", "claude-host")
                    self.review(fable_role, "codex-host", mode, status=status)
                    # open findings in the very role are refused one rule earlier (no rerun over them)
                    needle = "open findings" if (status, fable_role) == ("findings", opus_role) else "codex-host review"
                    self.record_refused(opus_role, needle=needle,
                                        **self.review_flags(opus, quota_evidence=str(quota)))
                    self.assertEqual(self.entry()["results"][fable_role]["profile"], "codex-host")
                    self.assertEqual(self.entry()["results"][fable_role]["status"], status)
        # covered findings of Fable are still a Fable review: Opus cannot replace them either
        self.init()
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        self.review("second_reviewer", "codex-host", "findings", status="findings")
        self.ok("fix-loop", "--task", "t1", "--defer", "--source", "second_reviewer", "--note", "low: nit")
        self.record_refused("second_reviewer", needle="codex-host review",
                            **self.review_flags(opus, quota_evidence=str(quota)))
        # a Fable that only failed (quota, then auth) leaves the fallback open, as before
        self.init()
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        self.record("second_reviewer", "error", artifact=str(quota))
        entry = self.record("second_reviewer", **self.review_flags(opus, quota_evidence=str(quota)))
        self.assertEqual(entry["status"], "ready_for_pr_review")

    def test_open_findings_of_a_review_are_not_replaced_without_a_disposition(self):
        """A second run of the same profile on the same HEAD cannot wipe findings of a high-risk task."""
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        self.review("second_reviewer", "codex-host", "findings", status="findings")
        clean = self.edited(self.report("codex-host"), session_id="second-fable-run")
        for status, flags in (("pass", self.review_flags(clean)), ("error", {}), ("unavailable", {}),
                              ("incomplete", {})):
            with self.subTest(status=status):
                self.record_refused("second_reviewer", status, needle="fix-loop", **flags)
        self.assertEqual(self.entry()["results"]["second_reviewer"]["status"], "findings")
        # the other role and the non-review roles are not held by it
        self.record("tester", model="fable")
        self.review("cross_provider_reviewer", "claude-host")
        # a recorded disposition releases the role
        self.ok("fix-loop", "--task", "t1", "--accept", "--source", "second_reviewer",
                "--severity", "low", "--note", "false positive: context was missing")
        entry = self.record("second_reviewer", **self.review_flags(clean))
        self.assertEqual(entry["status"], "ready_for_pr_review")
        # after a fix the HEAD is new: resume drops the findings and the cycle goes on
        self.review("second_reviewer", "codex-host", "findings", status="findings")
        self.ok("fix-loop", "--task", "t1", "--outcome", "failed", "--source", "second_reviewer")
        self.change("fixed\n")
        self.resume()
        self.code_and_test()
        self.packet()
        self.review("cross_provider_reviewer", "claude-host")
        entry, _ = self.review("second_reviewer", "codex-host")
        self.assertEqual(entry["status"], "ready_for_pr_review")
        # below high, and so for 1.2.0 manifests, a rerun replaces findings as before
        self.init(risk="medium")
        self.record("coder")
        self.record("tester")
        self.record("cross_provider_reviewer", "findings")
        entry = self.record("cross_provider_reviewer", reviewed_head=self.head(), packet_hash="ab12" * 16)
        self.assertEqual(entry["status"], "ready_for_pr_review")

    def test_no_command_promotes_a_high_task_with_one_review(self):
        """Fable review, missing context a: every path that recomputes a status uses the task's risk."""
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")

        def assert_not_ready():
            self.assertNotEqual(self.entry()["status"], "ready_for_pr_review")
            self.assertNotEqual(self.ok("status")["tasks"]["t1"]["status"], "ready_for_pr_review")
            where = self.where()
            self.assertEqual((where["step"], where["role"]), (5, "second_reviewer"))
            self.assertNotEqual(where["task_status"], "ready_for_pr_review")

        assert_not_ready()
        self.resume()
        assert_not_ready()
        self.ok("mark", "--task", "t1", "--step", "5", "--safe-point", "true")
        assert_not_ready()
        self.assertEqual(self.ok("fix-loop", "--task", "t1", "--outcome", "pass")["status"], "needs_verification")
        assert_not_ready()
        self.record("tester", model="fable")
        assert_not_ready()
        self.ok("task-risk", "--task", "t1", "--risk", "high")
        assert_not_ready()
        # the status functions have no default that falls back to the rules of 1.2.0
        module = _load("superarmanda_state_signatures", STATE)
        for name in ("update_task_status", "task_ready", "settle_after_cover", "is_complete"):
            with self.subTest(function=name):
                with self.assertRaises(TypeError):
                    getattr(module, name)(*([self.entry()] + ([self.head(), "t"] if name == "is_complete" else [])))

    def test_raised_task_risk_survives_resume_onto_a_new_head(self):
        """Fable review, missing context b."""
        self.init(risk="low")
        self.ok("task-risk", "--task", "t1", "--risk", "high")
        self.record("coder", model="fable")
        self.change("next\n")
        self.assertEqual(self.resume()["invalidated_tasks"], ["t1"])
        self.assertEqual(self.entry()["risk"], "high")
        self.assertEqual(self.where()["risk"], "high")
        self.assertEqual(self.ok("role-model", "--task", "t1", "--role", "coder")["model"], FABLE)
        self.record_refused("coder", model="sonnet")
        self.refused("task-risk", "--task", "t1", "--risk", "medium")

    def test_reviewed_head_of_the_second_reviewer_must_be_the_result_head(self):
        """Fable review, missing context c."""
        self.code_and_test()
        fable = self.report("codex-host")
        for wrong in (self.base, "0" * 40, self.head()[:12], "HEAD"):
            with self.subTest(reviewed_head=wrong):
                flags = self.review_flags(fable)
                flags["reviewed_head"] = wrong
                self.record_refused("second_reviewer", needle="reviewed_head", **flags)
        for wrong in ("sha256:" + "0" * 64, "ab12" * 16, "nonsense"):
            with self.subTest(packet_hash=wrong):
                self.record_refused("second_reviewer", **self.review_flags(fable, packet_hash=wrong))
        stored = self.record("second_reviewer", **self.review_flags(fable))["results"]["second_reviewer"]
        self.assertEqual((stored["reviewed_head"], stored["packet_hash"]), (self.head(), self.packet_hash))
        self.init(risk="medium")
        self.record_refused("second_reviewer", reviewed_head=self.base, packet_hash="ab12" * 16,
                            needle="reviewed_head")

    def test_observed_models_of_a_report_are_checked_as_far_as_live_reports_allow(self):
        """tester-2 F4: the primary model must be there AND verified; Astra observes exactly one."""
        self.code_and_test()
        astra = self.report("claude-host")
        fable = self.report("codex-host")
        body = json.loads(fable.read_text(encoding="utf-8"))
        unverified = dict(body["capabilities"], primary_model_verified=False)
        cases = {
            "astra with a second model": self.edited(astra, observed_models=["gpt-6-astra", SONNET]),
            "astra observed twice": self.edited(astra, observed_models=["gpt-6-astra", "gpt-6-astra"]),
            "astra as an object": self.edited(astra, observed_models={"gpt-6-astra": {}}),
            "fable as a list": self.edited(fable, observed_models=[FABLE]),
            "fable pass with unverified primary model": self.edited(fable, capabilities=unverified),
            "fable pass without capabilities": self.edited(fable, capabilities=None),
            "fable usage is not an object": self.edited(fable, observed_models={FABLE: 1}),
        }
        for label, report in cases.items():
            with self.subTest(case=label):
                self.record_refused("second_reviewer", **self.review_flags(report))
        # ancillary modelUsage entries beside a verified primary model are legitimate (review-contract.md)
        ancillary = self.edited(fable, observed_models=dict(body["observed_models"],
                                                             **{"claude-haiku-4-5-20251001": {"inputTokens": 1}}))
        self.record("cross_provider_reviewer", **self.review_flags(astra))
        entry = self.record("second_reviewer", **self.review_flags(ancillary))
        self.assertEqual(entry["status"], "ready_for_pr_review")

    def opus_report(self):
        # its own session: the CLI doubles give Fable and Opus one session id
        return self.edited(self.report("codex-host-opus"), session_id="opus-review-session")

    def test_opus_stays_refused_after_the_fable_result_is_overwritten(self):
        """tester-3 T3-1: what Fable said about this HEAD cannot be unsaid by a later record."""
        for status, mode, wipe in (("findings", "findings", "unavailable"), ("findings", "findings", "error"),
                                   ("pass", "success", "error"), ("pass", "success", "unavailable"),
                                   ("pass", "success", "incomplete")):
            with self.subTest(fable=status, wipe=wipe):
                self.init()
                self.code_and_test()
                quota = self.report("codex-host", "quota_failure")  # made before Fable answered
                opus = self.opus_report()
                self.review("cross_provider_reviewer", "claude-host")
                self.review("second_reviewer", "codex-host", mode, status=status)
                if status == "findings":
                    self.ok("fix-loop", "--task", "t1", "--accept", "--source", "second_reviewer",
                            "--severity", "high", "--note", "accepted")
                flags = self.review_flags(opus, quota_evidence=str(quota))
                self.record_refused("second_reviewer", needle="codex-host review", **flags)
                self.record("second_reviewer", wipe)
                self.record_refused("second_reviewer", needle="codex-host review", **flags)
                self.record_refused("cross_provider_reviewer", needle="codex-host review", **flags)
                self.assertEqual(self.entry()["status"], "in_progress")
                where = self.where()
                self.assertNotIn("done", where["next_action"])
                if status == "findings":
                    # the accepted limitation of this HEAD is still reported
                    self.assertEqual(where["accepted"], 1)
                    self.assertEqual([(a["source"], a["severity"], a["note"]) for a in where["accepted_limitations"]],
                                     [("second_reviewer", "high", "accepted")])
                # a new commit is another HEAD: there the fallback is open again
                self.change(f"after {status} {wipe}\n")
                self.resume()
                self.code_and_test()
                self.packet()
                self.review("cross_provider_reviewer", "claude-host")
                quota = self.report("codex-host", "quota_failure")
                entry = self.record("second_reviewer",
                                    **self.review_flags(self.opus_report(), quota_evidence=str(quota)))
                self.assertEqual(entry["status"], "ready_for_pr_review")
                self.assertEqual(self.where()["accepted_limitations"], [])

    def test_open_findings_survive_resume_there_and_back(self):
        """tester-3 T3-2: findings on a HEAD are not wiped by touching the tree and resuming twice."""
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        _, findings = self.review("second_reviewer", "codex-host", "findings", status="findings")
        clean = self.edited(self.report("codex-host"), session_id="second-fable-run")
        self.record_refused("second_reviewer", needle="open findings", **self.review_flags(clean))
        head = self.head()
        (self.repo / "junk.txt").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.resume()["invalidated_tasks"], ["t1"])
        # the same HEAD over a touched tree is no way round either
        self.record_refused("second_reviewer", "unavailable", needle="open findings")
        (self.repo / "junk.txt").unlink()
        self.resume()
        self.assertEqual((self.head(), self.entry()["results"]), (head, {}))
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        for status, flags in (("pass", self.review_flags(clean)), ("unavailable", {}), ("incomplete", {})):
            with self.subTest(status=status):
                self.record_refused("second_reviewer", status, needle="open findings", **flags)
        self.assertEqual(self.entry()["status"], "in_progress")
        where = self.where()
        self.assertEqual((where["step"], where["role"]), (6, "coordinator"))
        self.assertIn("open findings", where["next_action"])
        self.assertIn("second_reviewer", where["next_action"])
        self.assertEqual([(f["role"], f["status"], f["artifact"]) for f in where["open_findings"]],
                         [("second_reviewer", "findings", str(findings))])
        # the way out on this HEAD is the same disposition as before, bound to that very result
        accepted = self.ok("fix-loop", "--task", "t1", "--accept", "--source", "second_reviewer",
                           "--severity", "medium", "--note", "known limitation")
        self.assertEqual(accepted["status"], "in_progress")
        entry = self.record("second_reviewer", **self.review_flags(clean))
        self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertEqual((entry["fix_cycles"], len(entry["acceptances"])), (0, 1))
        where = self.where()
        self.assertEqual([(a["source"], a["severity"]) for a in where["accepted_limitations"]],
                         [("second_reviewer", "medium")])
        self.assertEqual(where["open_findings"], [])

    def test_ordinary_cycle_and_reruns_without_a_verdict_stay_free(self):
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        # incomplete and error before any verdict are repeated freely
        for status in ("incomplete", "error", "unavailable", "incomplete"):
            self.record("second_reviewer", status)
        self.review("second_reviewer", "codex-host", "findings", status="findings")
        self.ok("fix-loop", "--task", "t1", "--outcome", "failed", "--source", "second_reviewer")
        self.change("fixed\n")
        self.resume()
        self.code_and_test()
        self.packet()
        self.review("cross_provider_reviewer", "claude-host")
        entry, _ = self.review("second_reviewer", "codex-host")
        self.assertEqual(entry["status"], "ready_for_pr_review")
        history = entry["review_history"]
        self.assertEqual([(h["role"], h["status"], h["profile"]) for h in history], [
            ("cross_provider_reviewer", "pass", "claude-host"), ("second_reviewer", "incomplete", None),
            ("second_reviewer", "error", None), ("second_reviewer", "unavailable", None),
            ("second_reviewer", "incomplete", None), ("second_reviewer", "findings", "codex-host"),
            ("cross_provider_reviewer", "pass", "claude-host"), ("second_reviewer", "pass", "codex-host"),
        ])
        self.assertEqual(set(history[-1]), {"role", "profile", "status", "head", "tree_fingerprint", "packet_hash",
                                            "result_id", "result_sha256", "artifact", "recorded_at"})
        self.assertEqual(len({h["head"] for h in history}), 2)
        self.assertEqual(history[-1]["result_id"], entry["results"]["second_reviewer"]["result_id"])

    def test_history_is_kept_only_for_high_risk_tasks_of_version_2(self):
        self.assertIn("no task yet", self.where()["next_action"])  # a high run without a task
        self.init(risk="medium")
        self.record("coder")
        self.record("tester")
        self.record("cross_provider_reviewer", "findings")
        self.record("cross_provider_reviewer", "findings", session="again")
        self.assertNotIn("review_history", self.entry())
        # raising the risk takes the current review results into the history: they cannot be wiped now
        raised = self.ok("task-risk", "--task", "t1", "--risk", "high")
        self.assertEqual([(h["role"], h["status"]) for h in raised["review_history"]],
                         [("cross_provider_reviewer", "findings")])
        self.record_refused("cross_provider_reviewer", "incomplete", needle="open findings")
        # version 1: no key, and a manifest carrying it is refused
        value = self.data()
        value["version"] = 1
        del value["review_policy"]
        del value["tasks"]["t1"]["risk"]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.refused("where", needle="version 2")
        del value["tasks"]["t1"]["review_history"]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        entry = self.record("cross_provider_reviewer", reviewed_head=self.head(), packet_hash="ab12" * 16)
        self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertNotIn("review_history", entry)

    def test_quota_evidence_is_only_for_the_opus_profile(self):
        self.code_and_test()
        quota = self.report("codex-host", "quota_failure")
        for role, profile in (("cross_provider_reviewer", "claude-host"), ("second_reviewer", "codex-host")):
            with self.subTest(profile=profile):
                report = self.report(profile)
                self.record_refused(role, **self.review_flags(report, quota_evidence=str(quota)))
        self.record_refused("coder", model="fable", quota_evidence=str(quota))
        self.init(risk="medium")
        self.record_refused("cross_provider_reviewer",
                            **self.review_flags(self.report("codex-host-opus"), quota_evidence=str(quota)))

    def test_report_must_be_a_verified_review_of_this_head_and_packet(self):
        self.code_and_test()
        good = self.report("claude-host")
        good_hash = self.packet_hash
        findings = self.report("codex-host", "findings")
        body = json.loads(good.read_text(encoding="utf-8"))
        link = self.root / "link.json"
        link.symlink_to(good)
        directory = self.root / "report-dir"
        directory.mkdir()
        not_json = self.root / "not-json.json"
        not_json.write_text("not json", encoding="utf-8")
        array = self.root / "array.json"
        array.write_text("[]", encoding="utf-8")
        huge = self.root / "huge.json"
        huge.write_text(json.dumps(dict(body, padding="x" * (2 * 1024 * 1024))), encoding="utf-8")
        cases = {
            "gate_ready false as pass": self.edited(good, gate_ready=False),
            "gate_ready missing": self.edited(good, gate_ready=None),
            "gate_ready truthy string": self.edited(good, gate_ready="true"),
            "report status differs": self.edited(good, status="findings"),
            "unknown profile": self.edited(good, profile="sonnet-host"),
            "profile missing": self.edited(good, profile=None),
            "another packet": self.edited(good, state_packet_hash="sha256:" + "0" * 64),
            "packet missing (old report)": self.edited(good, state_packet_hash=None),
            "another head": self.edited(good, response=dict(body["response"], reviewed_head="0" * 40)),
            "no response": self.edited(good, response=None),
            "symlink": link,
            "directory": directory,
            "not json": not_json,
            "not an object": array,
            "oversized": huge,
            "missing file": self.root / "absent.json",
        }
        for label, report in cases.items():
            with self.subTest(case=label):
                self.record_refused("cross_provider_reviewer", **self.review_flags(report))
        # the flags must name the report's packet, and the three of them are mandatory
        self.record_refused("cross_provider_reviewer", **self.review_flags(good, packet_hash="sha256:" + "1" * 64))
        for missing in ("artifact", "reviewed_head", "packet_hash"):
            flags = self.review_flags(good)
            flags[missing] = None
            for status in ("pass", "findings"):
                with self.subTest(missing=missing, status=status):
                    self.record_refused("cross_provider_reviewer", status, **flags)
        # findings need a verified model and tool isolation as well
        capabilities = json.loads(findings.read_text(encoding="utf-8"))["capabilities"]
        for label, broken in {
            "model not verified": dict(capabilities, primary_model_verified=False),
            "tools unverified": dict(capabilities, tool_isolation="unverified"),
            "tools missing": {k: v for k, v in capabilities.items() if k != "tool_isolation"},
        }.items():
            with self.subTest(case=label):
                self.record_refused("second_reviewer", "findings",
                                    **self.review_flags(self.edited(findings, capabilities=broken)))
        self.record_refused("second_reviewer", "pass", **self.review_flags(findings))
        self.assertEqual(self.packet_hash, good_hash)
        self.assertNotIn("cross_provider_reviewer", self.entry()["results"])
        self.record("cross_provider_reviewer", **self.review_flags(good))
        self.record("second_reviewer", "findings", **self.review_flags(findings))
        self.assertEqual(self.entry()["results"]["second_reviewer"]["profile"], "codex-host")

    def test_second_reviewer_findings_go_through_the_same_fix_loop_rules(self):
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        entry, _ = self.review("second_reviewer", "codex-host", "findings", status="findings")
        self.assertEqual(entry["status"], "in_progress")
        where = self.where()
        self.assertEqual((where["step"], where["role"]), (6, "coordinator"))
        self.assertIn("--source second_reviewer", where["next_action"])
        deferred = self.ok("fix-loop", "--task", "t1", "--defer", "--source", "second_reviewer",
                           "--note", "low: wording -> next wave")
        self.assertEqual(deferred["status"], "ready_for_pr_review")
        # a rerun is another result: the deferral does not cover it
        entry, _ = self.review("second_reviewer", "codex-host", "findings", status="findings")
        self.assertEqual(entry["status"], "in_progress")
        accepted = self.ok("fix-loop", "--task", "t1", "--accept", "--source", "second_reviewer",
                           "--severity", "medium", "--note", "accepted limitation")
        self.assertEqual(accepted["status"], "ready_for_pr_review")
        failed = self.ok("fix-loop", "--task", "t1", "--outcome", "failed", "--source", "second_reviewer")
        self.assertEqual((failed["status"], failed["fix_sources"]), ("needs_fix", {"second_reviewer": 1}))
        again = self.ok("fix-loop", "--task", "t1", "--outcome", "failed", "--source", "second_reviewer")
        self.assertEqual((again["status"], again["decision_required_for"]), ("needs_decision", "second_reviewer"))

    def test_medium_task_of_version_2_passes_with_one_astra_as_in_1_2_0(self):
        for level in ("medium", "low"):
            with self.subTest(level=level):
                self.init(risk=level)
                self.record("coder")
                self.record("tester")
                # the report is not dereferenced: any artifact string works, as before
                entry = self.record("cross_provider_reviewer", reviewed_head=self.head(),
                                    packet_hash="ab12" * 16, artifact="not-a-file")
                self.assertEqual(entry["status"], "ready_for_pr_review")
                self.assertNotIn("profile", entry["results"]["cross_provider_reviewer"])
                where = self.where()
                self.assertEqual((where["step"], where["role"], where["risk"]), (7, "github_codex_review", level))

    def test_merge_gate_reads_a_version_2_manifest_without_unknown_records(self):
        gate = _load("superarmanda_gate_for_policy", GATE)
        self.code_and_test()
        self.review("cross_provider_reviewer", "claude-host")
        quota = self.report("codex-host", "quota_failure")
        self.record("second_reviewer",
                    **self.review_flags(self.report("codex-host-opus"), quota_evidence=str(quota)))
        self.ok("mark", "--task", "t1", "--step", "7", "--safe-point", "true")
        value = self.data()
        self.assertEqual(len(value["tasks"]["t1"]["review_history"]), 2)
        self.assertEqual(gate.manifest_problems(value, value["head"], value["tree_fingerprint"]), [])
        self.ok("task-risk", "--task", "t1", "--risk", "high")
        value = self.data()
        self.assertEqual(gate.manifest_problems(value, value["head"], value["tree_fingerprint"]), [])
        # the gate still refuses what it does not know
        value["surprise"] = 1
        self.assertTrue(any("unknown record type" in p for p in
                            gate.manifest_problems(value, value["head"], value["tree_fingerprint"])))


class LiveVersion1Manifests(PolicyBase):
    """Manifests of finished chains (tests/fixtures/manifests/README.md) keep the rules of 1.2.0."""

    RISK = "low"

    def load_live(self, name):
        raw = (LIVE_MANIFESTS / name).read_text(encoding="utf-8")
        state_module = _load("superarmanda_state_for_fixture", STATE)
        fixture = json.loads(raw)
        bound = [
            (task, kind, index)
            for task, entry in fixture["tasks"].items()
            for kind in ("deferrals", "acceptances")
            for index, record in enumerate(entry.get(kind, []))
            if record["result_sha256"] == state_module.result_digest(entry["results"].get(record["source"]))
        ]
        tree = self.data()["tree_fingerprint"]  # of the throwaway repository, from state.py init
        for mark, value in (("@REPO@", str(self.repo.resolve())), ("@BASE@", self.base),
                            ("@HEAD@", self.head()), ("@TREE@", tree), ("@PATH@", str(self.root / "live"))):
            raw = raw.replace(mark, value)
        value = json.loads(raw)
        for task, kind, index in bound:  # the substitution changed the results the records name
            record = value["tasks"][task][kind][index]
            record["result_sha256"] = state_module.result_digest(value["tasks"][task]["results"][record["source"]])
        self.manifest.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        (self.TASK,) = value["tasks"]
        return value

    def assert_rules_of_1_2_0(self, name, risk):
        live = self.load_live(name)
        self.assertEqual((live["version"], live["wave"]["risk"]), (1, risk))
        self.assertNotIn("review_policy", live)
        status = self.ok("status")
        self.assertTrue(status["tree_matches"])
        self.assertEqual(status["tasks"][self.TASK]["status"], "ready_for_pr_review")
        where = self.where()
        self.assertIn("done", where["next_action"])
        self.assertEqual((where["risk"], where["review_policy"]), (None, None))
        # recomputed by today's state.py: no model, one Astra result, still ready
        for role in ("coder", "tester"):
            entry = self.record(role)
            self.assertNotIn("model", entry["results"][role])
        entry = self.record("cross_provider_reviewer", reviewed_head=self.head(),
                            packet_hash="ab12" * 16, artifact="not-a-file")
        self.assertEqual(entry["status"], "ready_for_pr_review")
        self.assertEqual(set(entry["results"]["cross_provider_reviewer"]),
                         {"status", "head", "session_id", "artifact", "reviewed_head", "packet_hash",
                          "tree_fingerprint", "recorded_at", "result_id"})
        self.assertNotIn("second_reviewer", self.where()["next_action"])
        self.assertEqual(self.data()["version"], 1)
        self.assertNotIn("review_policy", self.data())

    def test_live_high_wave_manifest_of_version_1_keeps_the_rules_of_1_2_0(self):
        self.assert_rules_of_1_2_0("v1-high-waves-gate-redact-W1.json", "high")

    def test_live_medium_wave_manifest_passes_with_one_astra_review(self):
        self.assert_rules_of_1_2_0("v1-medium-waves-tails-W2.json", "medium")

    def test_new_flags_and_commands_are_refused_on_version_1(self):
        self.load_live("v1-high-waves-gate-redact-W1.json")
        quota = self.root / "quota.json"
        quota.write_text("{}", encoding="utf-8")
        needle = "requires manifest version 2"
        self.record_refused("coder", model="fable", needle=needle)
        self.record_refused("tester", model="sonnet", needle=needle)
        self.record_refused("second_reviewer", needle=needle)
        self.record_refused("cross_provider_reviewer", reviewed_head=self.head(), packet_hash="ab12" * 16,
                            quota_evidence=str(quota), needle=needle)
        self.refused("task-risk", "--task", self.TASK, "--risk", "high", needle=needle)
        self.refused("role-model", "--task", self.TASK, "--role", "coder", needle=needle)
        self.refused("fix-loop", "--task", self.TASK, "--outcome", "failed", "--source", "second_reviewer",
                     needle=needle)
        self.refused("fix-loop", "--task", self.TASK, "--defer", "--source", "second_reviewer",
                     "--note", "n", needle=needle)

    def test_version_2_high_manifest_without_the_policy_field_is_refused(self):
        self.init(risk="high")
        self.code_and_test()
        value = self.data()
        del value["review_policy"]
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        for command, args in (("status", []), ("where", []),
                              ("task-result", self.result_args("tester", model="fable"))):
            with self.subTest(command=command):
                self.refused(command, *args, needle="review_policy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
