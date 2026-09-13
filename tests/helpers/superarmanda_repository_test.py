#!/usr/bin/env python3
"""Repository-boundary contract tests for Superarmanda review/state CLIs.

Set ``SA_REVIEW_SCRIPT`` and ``SA_STATE_SCRIPT`` to exercise an immutable
candidate without copying this independently authored helper into it.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REVIEW = Path(
    os.environ.get("SA_REVIEW_SCRIPT", ROOT / "skills/superarmanda/scripts/review.py")
)
STATE = Path(
    os.environ.get("SA_STATE_SCRIPT", ROOT / "skills/superarmanda/scripts/state.py")
)
PR_REVIEW = Path(
    os.environ.get(
        "SA_PR_REVIEW_SCRIPT", ROOT / "skills/superarmanda/scripts/pr_review.py"
    )
)

MOCK = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

log = Path(os.environ["SA_REPOSITORY_LOG"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "claude" and args[:4] == ["--safe-mode", "auth", "status", "--json"]:
    log.open("a").write("auth\n")
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}))
    raise SystemExit(0)
log.open("a").write("review\n")
if os.environ.get("SA_REPOSITORY_MODE") == "filename503":
    print("line503 in network_filename.txt", file=sys.stderr)
    raise SystemExit(1)
packet = json.loads(sys.stdin.read().split("\n", 1)[1])
print(json.dumps({"type":"system", "subtype":"init", "session_id":"s", "model":"claude-fable-5-1", "tools":["StructuredOutput"], "mcp_servers":[], "plugins":[]}))
response = {"status":"pass", "reviewed_head":packet["packet"]["head"], "packet_hash":packet["packet_hash"], "findings":[], "missing_context":[]}
print(json.dumps({"type":"assistant", "message":{"model":"claude-fable-5-1", "content":[{"type":"tool_use", "name":"StructuredOutput", "input":response}]}}))
print(json.dumps({"type":"result", "subtype":"success", "is_error":False, "result":json.dumps(response), "structured_output":response, "modelUsage":{"claude-fable-5-1":{"inputTokens":1}}}))
"""


class RepositoryContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("checkout", "-qb", "main")
        self.git("config", "user.email", "repository@example.invalid")
        self.git("config", "user.name", "Repository Tester")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "base")
        self.base = self.head()
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8")
        self.git("commit", "-am", "change", "-q")
        self.requirements = self.root / "requirements.txt"
        self.evidence = self.root / "evidence.txt"
        self.requirements.write_text("requirements\n", encoding="utf-8")
        self.evidence.write_text("evidence\n", encoding="utf-8")
        self.packet_path = self.root / "packet.json"
        self.result_path = self.root / "result.json"
        self.log = self.root / "mock.log"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(MOCK, encoding="utf-8")
        claude.chmod(0o755)
        gh = self.bin / "gh"
        gh.write_text(
            '#!/bin/sh\nprintf \'gh\\n\' >> "$SA_REPOSITORY_LOG"\nprintf \'%s\\n\' \'{"head": {"sha": "synthetic"}, "draft": true}\'\n',
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def git(self, *args, check=True, env=None):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            text=True,
            capture_output=True,
            check=check,
            env=env,
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def packet(self, output=None, context=(), env=None):
        command = [
            "python3",
            str(REVIEW),
            "packet",
            "--repo",
            str(self.repo),
            "--base",
            self.base,
            "--head",
            self.head(),
            "--requirements",
            str(self.requirements),
            "--test-evidence",
            str(self.evidence),
            "--output",
            str(output or self.packet_path),
        ]
        for value in context:
            # argparse otherwise interprets a literal repository path beginning
            # with '-' as another option before review.py can validate it.
            command += [f"--context={value}"]
        return subprocess.run(command, text=True, capture_output=True, env=env)

    def review(self, packet=None, env=None):
        return subprocess.run(
            [
                "python3",
                str(REVIEW),
                "run",
                "--repo",
                str(self.repo),
                "--packet",
                str(packet or self.packet_path),
                "--profile",
                "codex-host",
                "--output",
                str(self.result_path),
                "--timeout",
                "1",
            ],
            text=True,
            capture_output=True,
            env=env,
        )

    def mock_env(self, mode="success"):
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "SA_REPOSITORY_LOG": str(self.log),
                "SA_REPOSITORY_MODE": mode,
            }
        )
        return env

    def test_git_environment_routing_cannot_replace_the_declared_repository(self):
        rogue = self.root / "rogue"
        rogue.mkdir()
        subprocess.run(["git", "-C", str(rogue), "init", "-q"], check=True)
        poison = {
            "GIT_DIR": str(rogue / ".git"),
            "GIT_WORK_TREE": str(rogue),
            "GIT_INDEX_FILE": str(rogue / "index"),
            "GIT_CONFIG_PARAMETERS": "'color.ui=always'",
        }
        for name, value in poison.items():
            with self.subTest(variable=name):
                if self.packet_path.exists():
                    self.packet_path.unlink()
                env = os.environ.copy()
                env[name] = value
                proc = self.packet(env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                packet = json.loads(self.packet_path.read_text(encoding="utf-8"))[
                    "packet"
                ]
                self.assertEqual(packet["head"], self.head())
                self.assertIn("changed", packet["diff"])

    def test_filter_and_fsmonitor_drivers_are_never_executed_before_safe_rejection(
        self,
    ):
        marker = self.root / "driver-ran"
        driver = self.root / "driver"
        driver.write_text(
            '#!/bin/sh\nprintf ran > "$SA_DRIVER_MARKER"\ncat\n', encoding="utf-8"
        )
        driver.chmod(0o755)
        (self.repo / ".gitattributes").write_text(
            "app.txt filter=unsafe\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "unsafe filter fixture")
        self.git("config", "filter.unsafe.clean", str(driver))
        self.git("config", "core.fsmonitor", str(driver))
        env = os.environ.copy()
        env["SA_DRIVER_MARKER"] = str(marker)
        proc = self.packet(env=env)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(marker.exists(), "configured repository driver was executed")

    def test_rehashed_packet_tampering_is_rejected_before_authentication(self):
        self.assertEqual(self.packet().returncode, 0)
        original = json.loads(self.packet_path.read_text(encoding="utf-8"))
        for label, mutate in (
            (
                "diff",
                lambda packet: packet.__setitem__(
                    "diff", packet["diff"].replace("changed", "forged")
                ),
            ),
            (
                "context",
                lambda packet: packet.__setitem__(
                    "context", [{"path": "app.txt", "content": "forged\n"}]
                ),
            ),
        ):
            with self.subTest(part=label):
                value = json.loads(json.dumps(original))
                mutate(value["packet"])
                body = json.dumps(
                    value["packet"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                value["packet_hash"] = hashlib.sha256(body).hexdigest()
                value["byte_size"] = len(body)
                tampered = self.root / f"tampered-{label}.json"
                tampered.write_text(json.dumps(value), encoding="utf-8")
                if self.log.exists():
                    self.log.unlink()
                proc = self.review(tampered, self.mock_env())
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(
                    self.log.exists(),
                    "tampered packet reached mocked auth or review CLI",
                )

    def test_common_git_metadata_is_not_an_external_output_or_manifest_location(self):
        linked = self.root / "linked"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-q",
                "-b",
                "linked",
                str(linked),
            ],
            check=True,
        )
        common = Path(
            subprocess.check_output(
                ["git", "-C", str(linked), "rev-parse", "--git-common-dir"], text=True
            ).strip()
        )
        if not common.is_absolute():
            common = (linked / common).resolve()
        output = common / "superarmanda-packet.json"
        with self.subTest(kind="packet"):
            proc = subprocess.run(
                [
                    "python3",
                    str(REVIEW),
                    "packet",
                    "--repo",
                    str(linked),
                    "--base",
                    self.base,
                    "--head",
                    self.head(),
                    "--requirements",
                    str(self.requirements),
                    "--test-evidence",
                    str(self.evidence),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(output.exists())
        # Keep the subsequent manifest subcase independent on an unsafe baseline.
        if output.exists():
            output.unlink()
        manifest = common / "superarmanda-run.json"
        with self.subTest(kind="manifest"):
            state = subprocess.run(
                [
                    "python3",
                    str(STATE),
                    "init",
                    "--manifest",
                    str(manifest),
                    "--repo",
                    str(linked),
                    "--base",
                    self.base,
                    "--head",
                    self.head(),
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(state.returncode, 0, state.stdout + state.stderr)
            self.assertFalse(manifest.exists())

    def test_state_accepts_initialized_submodule_in_linked_worktree(self):
        """Git stores a linked worktree's modules below its per-worktree gitdir."""
        source = self.root / "submodule-source"
        source.mkdir()
        subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "config",
                "user.email",
                "repository@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "config",
                "user.name",
                "Repository Tester",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(source), "commit", "--allow-empty", "-qm", "base"],
            check=True,
        )
        base = self.head()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(source),
                "dependency",
            ],
            check=True,
        )
        self.git("commit", "-am", "add submodule", "-q")
        linked = self.root / "linked"
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "worktree",
                "add",
                "-qb",
                "linked",
                str(linked),
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(linked),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "-q",
            ],
            check=True,
        )
        manifest = self.root / "linked-submodule-run.json"
        proc = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(manifest),
                "--repo",
                str(linked),
                "--base",
                base,
                "--head",
                self.head(),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(manifest.exists())
        packet = self.root / "linked-submodule-packet.json"
        proc = subprocess.run(
            [
                "python3",
                str(REVIEW),
                "packet",
                "--repo",
                str(linked),
                "--base",
                base,
                "--head",
                self.head(),
                "--requirements",
                str(self.requirements),
                "--test-evidence",
                str(self.evidence),
                "--output",
                str(packet),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(packet.exists())

    def test_literal_dash_context_and_filename503_do_not_change_safe_behavior(self):
        context = self.repo / "-context.txt"
        context.write_text("literal path\n", encoding="utf-8")
        self.git("add", "--", "-context.txt")
        self.git("commit", "-qm", "literal context")
        proc = self.packet(context=("-context.txt",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        packet = json.loads(self.packet_path.read_text(encoding="utf-8"))["packet"]
        self.assertEqual(
            packet["context"], [{"path": "-context.txt", "content": "literal path\n"}]
        )
        proc = self.review(env=self.mock_env("filename503"))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.log.read_text(encoding="utf-8").splitlines(), ["auth", "review"]
        )

    def test_alias_paths_cannot_bypass_review_pr_or_state_boundaries(self):
        """Keep alias spelling: resolving the fixture itself hides this regression."""
        # Build a valid packet before introducing the spelling alias.  The
        # result boundary must independently reject its later output pathname.
        self.assertEqual(self.packet().returncode, 0)
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        alias = self.root / "parent-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        aliased_repo = alias / "repo"
        self.repo.rename(real_parent / "repo")
        self.repo = real_parent / "repo"
        self.packet_path = self.root / "safe-packet.json"
        self.result_path = self.root / "safe-result.json"
        (self.repo / ".gitignore").write_text(".runs/\nescape\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "alias boundary fixtures")
        self.assertEqual(self.packet().returncode, 0)
        outside = self.root / "outside"
        outside.mkdir()
        escape = self.repo / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.git("status", "--porcelain=v1").stdout, "")
        malformed_packet = self.root / "malformed-packet.json"
        malformed_packet.write_text("{}\n", encoding="utf-8")

        for label, output in (
            ("alias-repo", aliased_repo / "packet.json"),
            ("ignored-escape", aliased_repo / "escape" / "packet.json"),
        ):
            with self.subTest(kind=label):
                violations = []
                proc = self.packet(output=output)
                if proc.returncode == 0:
                    violations.append("packet accepted alias output")
                if output.exists():
                    violations.append("packet created alias output")
                    output.unlink()
                for packet_label, packet in (
                    ("normal", self.packet_path),
                    ("malformed", malformed_packet),
                ):
                    proc = subprocess.run(
                        [
                            "python3",
                            str(REVIEW),
                            "run",
                            "--repo",
                            str(aliased_repo),
                            "--packet",
                            str(packet),
                            "--profile",
                            "codex-host",
                            "--output",
                            str(output),
                            "--timeout",
                            "1",
                        ],
                        text=True,
                        capture_output=True,
                        env=self.mock_env(),
                    )
                    if proc.returncode == 0:
                        violations.append(
                            f"{packet_label} review accepted alias output"
                        )
                    if output.exists():
                        violations.append(f"{packet_label} review created alias output")
                        output.unlink()
                before_pr_log = (
                    self.log.read_text(encoding="utf-8").splitlines()
                    if self.log.exists()
                    else []
                )
                pr = subprocess.run(
                    [
                        "python3",
                        str(PR_REVIEW),
                        "check",
                        "--repo",
                        "example/repo",
                        "--pr",
                        "1",
                        "--head",
                        self.head(),
                        "--worktree",
                        str(aliased_repo),
                        "--output",
                        str(output),
                    ],
                    text=True,
                    capture_output=True,
                    env=self.mock_env(),
                )
                if pr.returncode == 0:
                    violations.append("pr_review accepted alias output")
                after_pr_log = (
                    self.log.read_text(encoding="utf-8").splitlines()
                    if self.log.exists()
                    else []
                )
                if "gh" in after_pr_log[len(before_pr_log) :]:
                    violations.append("pr_review reached gh for alias output")
                if output.exists():
                    violations.append("pr_review created alias output")
                    output.unlink()
                self.assertEqual(violations, [], "; ".join(violations))

        ordinary = aliased_repo / ".runs" / "ordinary.json"
        state = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(ordinary),
                "--repo",
                str(aliased_repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "alias-ordinary",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(state.returncode, 0, state.stderr)
        escaped_manifest = aliased_repo / "escape" / "run.json"
        state = subprocess.run(
            [
                "python3",
                str(STATE),
                "init",
                "--manifest",
                str(escaped_manifest),
                "--repo",
                str(aliased_repo),
                "--base",
                self.base,
                "--head",
                self.head(),
                "--run-id",
                "alias-escape",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(state.returncode, 0, state.stdout + state.stderr)
        self.assertFalse(escaped_manifest.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
