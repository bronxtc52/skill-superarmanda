#!/usr/bin/env python3
"""Black-box contract tests for the bounded Superarmanda review adapters."""

import ast
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "skills" / "superarmanda" / "scripts" / "review.py"

MOCK = r"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

assert not any(key in os.environ for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_TLS_REJECT_UNAUTHORIZED"))
name = Path(sys.argv[0]).name
argv = sys.argv[1:]
mode = os.environ.get("SA_TEST_MODE", "success")
log = Path(os.environ["SA_TEST_LOG"])
record = {"program": name, "argv": argv, "stdin": "", "api_key": os.environ.get("OPENAI_API_KEY"), "token": os.environ.get("ANTHROPIC_AUTH_TOKEN"), "otel": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"), "disable_telemetry": os.environ.get("DISABLE_TELEMETRY"), "node_tls": os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED")}
is_claude_auth = name == "claude" and argv[:4] == ["--safe-mode", "auth", "status", "--json"]
is_codex_auth = name == "codex" and argv[:2] == ["login", "status"]
if is_claude_auth:
    record["kind"] = "auth"
    log.open("a", encoding="utf-8").write(json.dumps(record) + "\n")
    if mode == "claude_auth_unauthorized":
        print("unauthorized subscription", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro" if mode == "claude_pro" else "max"}))
    raise SystemExit(0)
if is_codex_auth:
    record["kind"] = "auth"
    log.open("a", encoding="utf-8").write(json.dumps(record) + "\n")
    if mode == "codex_api_hint":
        print("Configure a ChatGPT API key to continue", file=sys.stderr)
    else:
        print("Logged in using ChatGPT", file=sys.stderr)
    raise SystemExit(0)
record["kind"] = "review"
record["stdin"] = sys.stdin.read()
log.open("a", encoding="utf-8").write(json.dumps(record) + "\n")
if mode == "timeout":
    time.sleep(3)
if mode == "change_during_run":
    sync = Path(os.environ["SA_TEST_SYNC"])
    sync.write_text("started", encoding="utf-8")
    while not sync.with_suffix(".go").exists():
        time.sleep(.01)
packet = json.loads(record["stdin"].split("\n", 1)[1])
response = {"status": "pass", "reviewed_head": packet["packet"]["head"], "packet_hash": packet["packet_hash"], "findings": [], "missing_context": []}
if mode == "pass_findings":
    response["findings"] = [{"severity": "high", "file": "x.py", "line": 1, "scenario": "bad input", "evidence": "bad", "recommendation": "fix"}]
if mode == "findings":
    response["status"] = "findings"
    response["findings"] = [{"severity": "high", "file": "x.py", "line": 1, "scenario": "bad input", "evidence": "bad", "recommendation": "fix"}]
if mode == "wrong_head":
    response["reviewed_head"] = "0" * 40
if mode == "wrong_hash":
    response["packet_hash"] = "sha256:" + "0" * 64
if mode == "invalid_schema":
    del response["missing_context"]
if mode == "status_array":
    response["status"] = []
if mode == "severity_array":
    response["status"] = "findings"
    response["findings"] = [{"severity": [], "file": "x.py", "line": 1, "scenario": "bad input", "evidence": "bad", "recommendation": "fix"}]
if mode == "transport_failure":
    print("temporary network failure", file=sys.stderr)
    raise SystemExit(1)
if mode == "quota_failure":
    print("quota exhausted", file=sys.stderr)
    raise SystemExit(1)
if mode == "cli_reported_timeout":
    print("timed out", file=sys.stderr)
    raise SystemExit(1)
if mode == "author_word_failure":
    print("the author supplied an invalid response", file=sys.stderr)
    raise SystemExit(1)
if name == "claude":
    # The installed Claude CLI emits newline-delimited stream-json: an init
    # event followed by a terminal result event.  Do not accept its old single
    # JSON object format in this adapter.
    if mode == "claude_old_single_json":
        print(json.dumps({"is_error": False, "result": json.dumps(response), "structured_output": response}))
    else:
        requested = argv[argv.index("--model") + 1]
        expected = {"fable": "claude-fable-5-1", "claude-opus-4-8": "claude-opus-4-8"}[requested]
        init = {"type": "system", "subtype": "init", "session_id": "claude-session", "model": expected, "tools": ["StructuredOutput"], "mcp_servers": [], "plugins": []}
        if mode == "claude_tools": init["tools"] = ["Read"]
        if mode == "claude_tools_nonlist": init["tools"] = "StructuredOutput"
        if mode == "claude_missing_tools": del init["tools"]
        if mode in ("claude_primary_mismatch", "opus_wrong_init"): init["model"] = "claude-opus-5"
        if mode == "opus_mcp": init["mcp_servers"] = ["unexpected"]
        if mode == "opus_plugins": init["plugins"] = ["unexpected"]
        if mode == "claude_assistant_before_init":
            print(json.dumps({"type": "assistant", "message": {"model": expected, "content": [{"type": "tool_use", "name": "StructuredOutput", "input": response}]}}))
        print(json.dumps(init))
        if mode == "claude_duplicate_init":
            duplicate_init = dict(init)
            duplicate_init["model"] = "claude-opus-5"
            print(json.dumps(duplicate_init))
        if mode == "claude_structured_mismatch":
            tool_input = {**response, "packet_hash": "wrong"}
        else:
            tool_input = response
        tool_name = "Read" if mode == "claude_execution_tool" else "StructuredOutput"
        if mode == "claude_null_content":
            print(json.dumps({"type": "assistant", "message": {"model": None, "content": None}}))
        elif mode == "claude_string_message":
            print(json.dumps({"type": "assistant", "message": "not an object"}))
        elif mode not in ("claude_missing_assistant", "opus_missing_assistant"):
            assistant_model = None if mode == "claude_null_model" else expected
            if mode == "opus_wrong_assistant": assistant_model = "claude-fable-5-1"
            block = {"type": "tool_use", "name": tool_name, "input": tool_input}
            if mode in ("claude_server_tool", "claude_unknown_block"):
                block = {
                    "type": "server_tool_use"
                    if mode == "claude_server_tool"
                    else "unknown_block",
                    "name": "Read",
                }
            print(json.dumps({"type": "assistant", "message": {"model": assistant_model, "content": [block]}}))
        usage = [] if mode == "claude_model_usage_array" else {expected: {"inputTokens": 1}}
        if mode in ("claude_missing_primary_usage", "opus_wrong_usage"): usage = {"claude-haiku-4-5": {"inputTokens": 1}}
        terminal = {"type": "result", "subtype": "success", "is_error": mode == "claude_error", "result": json.dumps(response), "structured_output": response, "modelUsage": usage}
        if mode == "claude_missing_model_usage": del terminal["modelUsage"]
        if mode == "claude_missing_is_error": del terminal["is_error"]
        if mode == "claude_null_is_error": terminal["is_error"] = None
        if mode == "claude_string_is_error": terminal["is_error"] = "false"
        if mode == "claude_zero_is_error": terminal["is_error"] = 0
        if mode == "opus_refusal":
            print(json.dumps({"type": "system", "subtype": "model_refusal_fallback"}))
        encoded_terminal = json.dumps(terminal)
        if mode == "claude_duplicate_is_error":
            encoded_terminal = encoded_terminal.replace(
                '"is_error": false', '"is_error": true, "is_error": false', 1
            )
        print(encoded_terminal)
        if mode == "claude_duplicate_result": print(encoded_terminal)
else:
    if mode == "codex_failed":
        print(json.dumps({"type": "turn.failed"}))
    elif mode == "codex_array_event":
        print(json.dumps([]))
    elif mode == "codex_null_item":
        print(json.dumps({"type": "item.completed", "item": None}))
    elif mode == "codex_tool":
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "text": "no"}}))
        print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}))
    elif mode == "codex_no_terminal":
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}}))
    else:
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}}))
        print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}))
"""


# Reuse the synthetic protocol peer, then supply this suite's packet response.
_server_fixture = ast.parse(
    Path(__file__).with_name("superarmanda_codex_review_test.py").read_text()
)
CODEX_MOCK = next(
    ast.literal_eval(node.value)
    for node in _server_fixture.body
    if isinstance(node, ast.Assign)
    and any(
        isinstance(target, ast.Name) and target.id == "MOCK" for target in node.targets
    )
)
assert 'open(log,"w").write(' in CODEX_MOCK
CODEX_MOCK = (
    CODEX_MOCK.replace('open(log,"w").write(', 'open(log,"a").write(', 1)
    .replace(
        'log=os.environ.get("SA_TEST_LOG") or os.environ["SA_LOG"]',
        'mode={"codex_api_hint":"auth","codex_failed":"error","codex_tool":"tool",'
        '"codex_no_terminal":"missing","codex_array_event":"malformed",'
        '"codex_null_item":"malformed"}.get(mode,mode)\n'
        'log=os.environ.get("SA_TEST_LOG") or os.environ["SA_LOG"]',
    )
    .replace(
        'r=json.loads(line); i=r["id"]; m=r["method"]',
        'r=json.loads(line); i=r["id"]; m=r["method"]\n'
        ' open(log+".rpc","a").write(m+"\\n")',
    )
    .replace(
        'elif m=="turn/start":',
        'elif m=="turn/start":\n'
        '  envelope=json.loads(r["params"]["input"][0]["text"].split("\\n",1)[1])\n'
        '  packet_response={"status":"pass","reviewed_head":envelope["packet"]["head"],'
        '"packet_hash":envelope["packet_hash"],"findings":[],"missing_context":[]} ',
    )
    .replace('json.dumps({"ok":True})', "json.dumps(packet_response)")
)


class ReviewContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        self.git("config", "user.email", "tester@example.invalid")
        self.git("config", "user.name", "Review Tester")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "base")
        self.base = self.head()
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8")
        self.git("commit", "-am", "change", "-q")
        self.head_value = self.head()
        self.requirements = self.root / "requirements.txt"
        self.evidence = self.root / "evidence.txt"
        self.requirements.write_text("Must review the patch.\n", encoding="utf-8")
        self.evidence.write_text("synthetic checks passed\n", encoding="utf-8")
        self.packet_path = self.root / "packet.json"
        self.result_path = self.root / "result.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex"):
            path = self.bin / name
            path.write_text(CODEX_MOCK if name == "codex" else MOCK, encoding="utf-8")
            path.chmod(0o755)
        self.log = self.root / "cli-log.jsonl"

    def git(self, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            text=True,
            capture_output=True,
            check=check,
        )

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def command(self, action, *args):
        return ["python3", str(REVIEW), action, *map(str, args)]

    def packet(
        self,
        output=None,
        head=None,
        max_bytes=None,
        context=(),
        cwd=None,
        requirements=None,
        evidence=None,
        timeout=None,
    ):
        argv = self.command(
            "packet",
            "--repo",
            self.repo,
            "--base",
            self.base,
            "--head",
            head or self.head(),
            "--requirements",
            requirements or self.requirements,
            "--test-evidence",
            evidence or self.evidence,
            "--output",
            output or self.packet_path,
        )
        for item in context:
            argv += ["--context", item]
        if max_bytes is not None:
            argv += ["--max-bytes", str(max_bytes)]
        return subprocess.run(
            argv, text=True, capture_output=True, cwd=cwd, timeout=timeout
        )

    def mock_env(self, mode="success", **extra):
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "SA_TEST_MODE": mode,
                "SA_TEST_LOG": str(self.log),
                "DISABLE_TELEMETRY": "1",
                "SSL_CERT_FILE": "private-ca",
                "SSL_CERT_DIR": "private-dir",
                "CURL_CA_BUNDLE": "private-bundle",
                "REQUESTS_CA_BUNDLE": "private-requests",
                "NODE_TLS_REJECT_UNAUTHORIZED": "0",
                # These must be absent from both mocked subscription CLIs.
                "OPENAI_API_KEY": "must-not-reach-cli",
                "ANTHROPIC_AUTH_TOKEN": "must-not-reach-cli",
            }
        )
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def review_run(
        self, profile, mode="success", timeout=2, packet=None, output=None, **extra
    ):
        proc = subprocess.run(
            self.command(
                "run",
                "--repo",
                self.repo,
                "--packet",
                packet or self.packet_path,
                "--profile",
                profile,
                "--output",
                output or self.result_path,
                "--timeout",
                timeout,
            ),
            text=True,
            capture_output=True,
            env=self.mock_env(mode, **extra),
        )
        return proc

    def logs(self):
        if not self.log.exists():
            return []
        return [
            json.loads(row) for row in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def result(self):
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def assert_packet_ok(self):
        proc = self.packet()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_subscription_environment_removes_runtime_loader_knobs(self):
        spec = importlib.util.spec_from_file_location("superarmanda_review", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = dict(module.os.environ)
        try:
            module.os.environ.update(
                {
                    "CLAUDE_CONFIG_DIR": "/existing-claude-login",
                    "UNRELATED_CONFIG": "not-executed",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "private-telemetry",
                    "DISABLE_TELEMETRY": "1",
                    "LD_PRELOAD": "not-executed",
                    "LD_LIBRARY_PATH": "not-executed",
                    "DYLD_INSERT_LIBRARIES": "not-executed",
                    "NODE_PATH": "not-executed",
                    "NODE_EXTRA_CA_CERTS": "not-executed",
                    "NODE_TLS_REJECT_UNAUTHORIZED": "0",
                    "BUN_INSTALL": "not-executed",
                }
            )
            scrubbed = module.scrubbed_environment()
        finally:
            module.os.environ.clear()
            module.os.environ.update(original)
        self.assertFalse(
            {
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
                "DYLD_INSERT_LIBRARIES",
                "NODE_PATH",
                "NODE_EXTRA_CA_CERTS",
                "NODE_TLS_REJECT_UNAUTHORIZED",
                "BUN_INSTALL",
            }
            & scrubbed.keys()
        )
        self.assertEqual(scrubbed["CLAUDE_CONFIG_DIR"], "/existing-claude-login")
        self.assertNotIn("UNRELATED_CONFIG", scrubbed)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", scrubbed)
        self.assertEqual(scrubbed["DISABLE_TELEMETRY"], "1")
        self.assertFalse(
            {"SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"}
            & scrubbed.keys()
        )

    def test_environment_scrub_keeps_sandbox_proxy_only_under_marker(self):
        spec = importlib.util.spec_from_file_location("scrub_proxy_review", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        proxy = {
            key: "http://sandbox:token@localhost:3128"
            for key in (
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "no_proxy", "all_proxy",
            )
        }
        other = {"CLOUDSDK_PROXY_ADDRESS": "bad", "GRPC_PROXY": "bad", "RSYNC_PROXY": "bad"}
        original = dict(module.os.environ)

        def scrub(extra):
            try:
                for key in (*proxy, *other, "SANDBOX_RUNTIME", "CLAUDE_CODE_HOST_HTTP_PROXY_PORT"):
                    module.os.environ.pop(key, None)
                module.os.environ.update({**proxy, **other, **extra})
                return module.scrubbed_environment()
            finally:
                module.os.environ.clear()
                module.os.environ.update(original)

        for marker in ("SANDBOX_RUNTIME", "CLAUDE_CODE_HOST_HTTP_PROXY_PORT"):
            with self.subTest(marker=marker):
                scrubbed = scrub({marker: "1"})
                self.assertEqual({k: scrubbed.get(k) for k in proxy}, proxy)
                self.assertEqual(scrubbed.get(marker), "1")
                self.assertFalse(set(other) & scrubbed.keys())
        self.assertFalse((set(proxy) | set(other)) & scrub({}).keys())

    def test_packet_loader_rejects_fifo_and_reads_at_most_the_declared_bound(self):
        spec = importlib.util.spec_from_file_location("bounded_packet_reader", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fifo = self.root / "packet.fifo"
        os.mkfifo(fifo)
        fifo_probe = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('packet_reader', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.load_packet(sys.argv[2], 16)
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""
        try:
            fifo_result = subprocess.run(
                [sys.executable, "-c", fifo_probe, str(REVIEW), str(fifo)],
                text=True,
                capture_output=True,
                timeout=2,
            )
        except subprocess.TimeoutExpired:
            self.fail("packet FIFO reader did not fail promptly")
        self.assertEqual(fifo_result.returncode, 0, fifo_result.stderr)

        oversized = self.root / "oversized-packet.json"
        oversized.write_bytes(b"x" * 64)
        original_fdopen = module.os.fdopen
        requests = []
        case = self

        class BoundedReader:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def fileno(self):
                return self.file.fileno()

            def read(self, count=-1):
                requests.append(count)
                case.assertLessEqual(count, 17)
                return self.file.read(count)

        def guarded_fdopen(descriptor, mode):
            return BoundedReader(original_fdopen(descriptor, mode))

        with mock.patch.object(module.os, "fdopen", side_effect=guarded_fdopen):
            with self.assertRaises(ValueError):
                module.load_packet(oversized, 16)
        self.assertEqual(requests, [17])

    # Packet rejection tests are deliberately first: a green review cannot make
    # a malformed, stale, oversized, binary, or escaped packet acceptable.
    def test_submodule_ignore_cannot_hide_dirt_and_filters_never_execute(self):
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.repo),
            "child",
        )
        self.git("config", "-f", ".gitmodules", "submodule.child.ignore", "all")
        self.git("add", ".gitmodules", "child")
        self.git("commit", "-qm", "ignored submodule")
        self.assert_packet_ok()
        child = self.repo / "child"
        (child / "app.txt").write_text("dirty child\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        built = self.packet(output=self.root / "dirty-packet.json")
        self.assertNotEqual(built.returncode, 0)
        self.assertFalse((self.root / "dirty-packet.json").exists())
        reviewed = self.review_run("codex-host")
        self.assertNotEqual(reviewed.returncode, 0)
        self.assertEqual(self.logs(), [])

        marker = self.root / "filter-ran"
        driver = self.root / "filter-driver"
        driver.write_text(
            "#!/bin/sh\nprintf executed > " + shlex.quote(str(marker)) + "\ncat\n"
        )
        driver.chmod(0o755)
        (child / ".gitattributes").write_text("app.txt filter=probe\n")
        subprocess.run(
            [
                "git",
                "-C",
                str(child),
                "config",
                "filter.probe.clean",
                shlex.quote(str(driver)),
            ],
            check=True,
        )
        self.assertNotEqual(
            self.packet(output=self.root / "filter-packet.json").returncode, 0
        )
        self.assertNotEqual(self.review_run("codex-host").returncode, 0)
        self.assertFalse(marker.exists(), "submodule clean filter executed")
        self.assertEqual(self.logs(), [])

    def test_nested_ignore_cannot_hide_dirty_module_from_packet_or_run(self):
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.repo),
            "child",
        )
        child = self.repo / "child"

        def child_git(*args):
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str(child),
                    "-c",
                    "user.name=Review Tester",
                    "-c",
                    "user.email=tester@example.invalid",
                    *args,
                ],
                check=True,
                capture_output=True,
            )

        child_git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.repo),
            "nested",
        )
        child_git("config", "-f", ".gitmodules", "submodule.nested.ignore", "all")
        child_git("add", ".gitmodules", "nested")
        child_git("commit", "-qm", "nested ignore")
        self.git("add", ".gitmodules", "child")
        self.git("commit", "-qm", "nested module")
        self.assert_packet_ok()
        (child / "nested" / "app.txt").write_text("dirty nested\n")
        (child / "nested" / "untracked.txt").write_text("hidden untracked\n")
        self.assertEqual(
            self.git("status", "--porcelain", "--ignore-submodules=none").stdout, ""
        )
        self.assertNotEqual(
            self.packet(output=self.root / "dirty-nested.json").returncode, 0
        )
        self.assertNotEqual(self.review_run("codex-host").returncode, 0)
        self.assertEqual(self.logs(), [])

    def test_submodule_filter_audit_prevents_clean_filter_execution(self):
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.repo),
            "child",
        )
        child = self.repo / "child"
        (child / ".gitattributes").write_text("app.txt filter=probe\n")
        subprocess.run(["git", "-C", str(child), "add", ".gitattributes"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(child),
                "-c",
                "user.name=Review Tester",
                "-c",
                "user.email=tester@example.invalid",
                "commit",
                "-qm",
                "filter attributes",
            ],
            check=True,
            capture_output=True,
        )
        self.git("add", ".gitmodules", "child")
        self.git("commit", "-qm", "filter module")
        self.assert_packet_ok()
        marker = self.root / "filter-executed"
        driver = self.root / "clean-driver"
        driver.write_text(
            "#!/bin/sh\nprintf executed > " + shlex.quote(str(marker)) + "\ncat\n"
        )
        driver.chmod(0o755)
        subprocess.run(
            [
                "git",
                "-C",
                str(child),
                "config",
                "filter.probe.clean",
                shlex.quote(str(driver)),
            ],
            check=True,
        )
        app = child / "app.txt"
        stamp = app.stat()
        os.utime(app, (stamp.st_atime, stamp.st_mtime + 2))
        self.assertNotEqual(
            self.packet(output=self.root / "filter-output.json").returncode, 0
        )
        self.assertNotEqual(self.review_run("codex-host").returncode, 0)
        self.assertFalse(marker.exists(), "child clean filter executed")
        self.assertEqual(self.logs(), [])

    def test_packet_hash_is_stable_for_the_same_clean_head(self):
        first = self.root / "first.json"
        second = self.root / "second.json"
        self.assertEqual(self.packet(output=first).returncode, 0)
        self.assertEqual(self.packet(output=second).returncode, 0)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        value = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(value["packet"]["head"], self.head())

    def test_packet_diff_is_the_exact_git_binary_diff_including_final_whitespace(self):
        source = self.repo / "trailing.txt"
        source.write_text("text  \n", encoding="utf-8")
        self.git("add", "trailing.txt")
        self.git("commit", "-qm", "preserve trailing whitespace")
        expected = self.git(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--no-renames",
            "--diff-algorithm=histogram",
            "--unified=3",
            "--inter-hunk-context=0",
            "--no-indent-heuristic",
            "--no-relative",
            "--binary",
            self.base,
            self.head(),
        ).stdout
        proc = self.packet()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        actual = json.loads(self.packet_path.read_text(encoding="utf-8"))["packet"][
            "diff"
        ]
        self.assertEqual(actual, expected)
        self.assertTrue(actual.endswith("+text  \n"))

    def test_packet_records_input_labels_without_absolute_paths(self):
        proc = self.packet()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        packet = json.loads(self.packet_path.read_text(encoding="utf-8"))["packet"]
        self.assertEqual(packet["requirements"]["path"], self.requirements.name)
        self.assertEqual(packet["test_evidence"]["path"], self.evidence.name)
        self.assertNotIn(str(self.root), json.dumps(packet, ensure_ascii=False))

    def test_packet_output_cannot_alias_requirements_or_evidence(self):
        for label, input_path in (
            ("requirements", self.requirements),
            ("evidence", self.evidence),
        ):
            before = input_path.read_bytes()
            for alias_kind in ("same", "symlink", "hardlink"):
                with self.subTest(input=label, alias=alias_kind):
                    alias = input_path
                    if alias_kind == "symlink":
                        alias = self.root / f"{label}-symlink"
                        alias.symlink_to(input_path)
                    elif alias_kind == "hardlink":
                        alias = self.root / f"{label}-hardlink"
                        os.link(input_path, alias)
                    try:
                        proc = self.packet(output=alias)
                        self.assertNotEqual(
                            proc.returncode, 0, proc.stdout + proc.stderr
                        )
                        self.assertEqual(input_path.read_bytes(), before)
                    finally:
                        input_path.write_bytes(before)
                        if alias != input_path and alias.exists():
                            alias.unlink()

    def test_packet_output_cannot_hardlink_a_tracked_context_source(self):
        context = self.repo / "context.txt"
        context.write_text("tracked context\n", encoding="utf-8")
        self.git("add", "context.txt")
        self.git("commit", "-qm", "add tracked context")
        before = context.read_bytes()
        output = self.root / "context-hardlink"
        os.link(context, output)
        try:
            proc = self.packet(output=output, context=("context.txt",))
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(context.read_bytes(), before)
        finally:
            context.write_bytes(before)
            if output.exists():
                output.unlink()

    def test_review_output_cannot_alias_packet_before_auth_or_cli(self):
        self.assert_packet_ok()
        before = self.packet_path.read_bytes()
        for alias_kind in ("same", "symlink", "hardlink"):
            with self.subTest(alias=alias_kind):
                self.packet_path.write_bytes(before)
                if self.log.exists():
                    self.log.unlink()
                alias = self.packet_path
                if alias_kind == "symlink":
                    alias = self.root / "packet-symlink"
                    alias.symlink_to(self.packet_path)
                elif alias_kind == "hardlink":
                    alias = self.root / "packet-hardlink"
                    os.link(self.packet_path, alias)
                try:
                    proc = subprocess.run(
                        self.command(
                            "run",
                            "--repo",
                            self.repo,
                            "--packet",
                            self.packet_path,
                            "--profile",
                            "codex-host",
                            "--output",
                            alias,
                            "--timeout",
                            1,
                        ),
                        text=True,
                        capture_output=True,
                        env=self.mock_env(),
                    )
                    self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(self.packet_path.read_bytes(), before)
                    self.assertEqual(self.logs(), [])
                finally:
                    self.packet_path.write_bytes(before)
                    if alias != self.packet_path and alias.exists():
                        alias.unlink()

    def test_packet_accepts_context_relative_to_repo_when_called_elsewhere(self):
        context = self.repo / "review-context.txt"
        context.write_text("only this source file\n", encoding="utf-8")
        self.git("add", "review-context.txt")
        self.git("commit", "-qm", "context source")
        # The calling cwd deliberately differs from --repo.  A relative context
        # is part of the packet interface, so it must resolve against --repo.
        proc = self.packet(context=("review-context.txt",), cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(self.packet_path.read_text(encoding="utf-8"))["packet"][
                "context"
            ],
            [{"path": "review-context.txt", "content": "only this source file\n"}],
        )

    def test_relative_context_is_always_resolved_from_the_canonical_git_root(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("root-relative context\n", encoding="utf-8")
        self.git("add", "nested/file.txt")
        self.git("commit", "-qm", "add nested context")
        root_packet = self.root / "root-context.json"
        nested_packet = self.root / "nested-context.json"
        root_proc = self.packet(output=root_packet, context=("nested/file.txt",))
        nested_proc = subprocess.run(
            self.command(
                "packet",
                "--repo",
                nested,
                "--base",
                self.base,
                "--head",
                self.head(),
                "--requirements",
                self.requirements,
                "--test-evidence",
                self.evidence,
                "--context",
                "nested/file.txt",
                "--output",
                nested_packet,
            ),
            text=True,
            capture_output=True,
        )
        self.assertEqual(root_proc.returncode, 0, root_proc.stderr)
        self.assertEqual(nested_proc.returncode, 0, nested_proc.stderr)
        root_value = json.loads(root_packet.read_text(encoding="utf-8"))
        nested_value = json.loads(nested_packet.read_text(encoding="utf-8"))
        self.assertEqual(
            root_value["packet"]["context"], nested_value["packet"]["context"]
        )
        self.assertEqual(root_value["packet_hash"], nested_value["packet_hash"])

    def test_review_schema_uses_claude_supported_draft07_dialect(self):
        schema = json.loads(
            (
                ROOT
                / "skills"
                / "superarmanda"
                / "schemas"
                / "review-result.schema.json"
            ).read_text(encoding="utf-8")
        )
        # Claude 2.1.263 rejected the former draft 2020-12 declaration during
        # the schema probe; draft-07 is the compatible dialect it accepted.
        self.assertEqual(schema["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertIn("status", schema["required"])
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["pass", "findings", "incomplete", "error"],
        )

    def test_packet_rejects_dirty_head_binary_oversize_and_external_context(self):
        (self.repo / "dirty.txt").write_text("untracked\n", encoding="utf-8")
        self.assertNotEqual(self.packet().returncode, 0)
        (self.repo / "dirty.txt").unlink()

        self.git("commit", "--allow-empty", "-qm", "later")
        self.assertNotEqual(self.packet(head=self.head_value).returncode, 0)

        huge = self.root / "huge-requirements.txt"
        huge.write_text("x" * 4096, encoding="utf-8")
        original = self.requirements.read_text(encoding="utf-8")
        self.requirements.write_text(huge.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertNotEqual(self.packet(max_bytes=200).returncode, 0)
        self.requirements.write_text(original, encoding="utf-8")

        external = self.root / "outside.txt"
        external.write_text("no", encoding="utf-8")
        self.assertNotEqual(self.packet(context=(external,)).returncode, 0)

        (self.repo / "binary.dat").write_bytes(b"\0binary")
        self.git("add", "binary.dat")
        self.git("commit", "-qm", "binary")
        self.assertNotEqual(self.packet().returncode, 0)

    def test_packet_text_inputs_are_bounded_regular_files(self):
        self.assertEqual(self.packet().returncode, 0)
        spec = importlib.util.spec_from_file_location("bounded_text_reader", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for label in ("requirements", "evidence"):
            with self.subTest(label=label):
                fifo = self.root / (label + ".fifo")
                os.mkfifo(fifo)
                try:
                    proc = self.packet(
                        requirements=fifo if label == "requirements" else None,
                        evidence=fifo if label == "evidence" else None,
                        timeout=2,
                    )
                except subprocess.TimeoutExpired:
                    self.fail(label + " FIFO reader did not fail promptly")
                self.assertNotEqual(proc.returncode, 0)

                oversized = self.root / (label + ".oversized")
                oversized.write_text("x" * 201, encoding="utf-8")
                proc = self.packet(
                    max_bytes=200,
                    requirements=oversized if label == "requirements" else None,
                    evidence=oversized if label == "evidence" else None,
                )
                self.assertNotEqual(proc.returncode, 0)

                original_fdopen = module.os.fdopen
                reads = []
                case = self

                class BoundedReader:
                    def __init__(self, file):
                        self.file = file

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return self.file.__exit__(*args)

                    def fileno(self):
                        return self.file.fileno()

                    def read(self, count=-1):
                        reads.append(count)
                        case.assertLessEqual(count, 17)
                        return self.file.read(count)

                def guarded_fdopen(descriptor, mode):
                    return BoundedReader(original_fdopen(descriptor, mode))

                with mock.patch.object(module.os, "fdopen", side_effect=guarded_fdopen):
                    with self.assertRaises(ValueError):
                        module.text_file(oversized, label, 16)
                self.assertEqual(reads, [17])

    def test_context_blob_is_sized_before_its_bytes_are_loaded(self):
        context = self.repo / "oversized-context.txt"
        context.write_text("x" * 201, encoding="utf-8")
        self.git("add", context.name)
        self.git("commit", "-qm", "large context blob")
        spec = importlib.util.spec_from_file_location("bounded_context", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_git_raw = module.git_raw

        def no_blob_read(repo, *arguments):
            if arguments[:2] == ("cat-file", "blob"):
                self.fail("oversized blob was loaded before its size was rejected")
            return original_git_raw(repo, *arguments)

        with mock.patch.object(module, "git_raw", side_effect=no_blob_read):
            with self.assertRaises(ValueError):
                module.repo_file(self.repo, self.head(), context.name, 200)
        self.assertNotEqual(
            self.packet(context=(context.name,), max_bytes=200).returncode, 0
        )

    def test_contexts_share_one_packet_byte_budget(self):
        spec = importlib.util.spec_from_file_location("aggregate_contexts", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for empty in (False, True):
            names = tuple(
                f"context-{empty}-{index}.txt" for index in range(8 if empty else 2)
            )
            for name in names:
                (self.repo / name).write_text("" if empty else "x" * 101)
            self.git("add", *names)
            self.git("commit", "-qm", "aggregate context bytes")
            args = types.SimpleNamespace(
                repo=self.repo,
                base=self.base,
                head=self.head(),
                requirements=self.requirements,
                test_evidence=self.evidence,
                output=self.root / "aggregate-packet.json",
                context=names,
                max_bytes=201,
            )
            original = module.git_raw
            blobs = []

            def observed(repo, *arguments):
                if arguments[:2] == ("cat-file", "blob"):
                    blobs.append(arguments[2])
                return original(repo, *arguments)

            with (
                self.subTest(empty=empty),
                mock.patch.object(module, "git_raw", side_effect=observed),
            ):
                with self.assertRaises(ValueError):
                    module.packet(args)
                self.assertGreater(len(blobs), 0)
                self.assertLess(len(blobs), len(names))
                if not empty:
                    self.assertEqual(len(blobs), 1)

    def test_rendered_diff_is_bounded_before_packet_assembly(self):
        (self.repo / "app.txt").write_text("x" * 201, encoding="utf-8")
        self.git("commit", "-am", "large diff", "-q")
        spec = importlib.util.spec_from_file_location("bounded_diff", REVIEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaises(ValueError):
            module.rendered_diff(self.repo, self.base, self.head(), 200)

    def test_packet_accepts_text_that_mentions_git_binary_patch(self):
        """Only Git's binary diff syntax, never ordinary source text, is blocked."""
        (self.repo / "literal.py").write_text(
            'MARKER = "GIT binary patch"\n', encoding="utf-8"
        )
        self.git("add", "literal.py")
        self.git("commit", "-qm", "plain text marker")
        proc = self.packet()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_claude_adapter_mapping_auth_prompt_and_verified_fable_capabilities(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = self.logs()
        auth, review = entries
        self.assertEqual(auth["program"], "claude")
        self.assertEqual(auth["argv"], ["--safe-mode", "auth", "status", "--json"])
        self.assertEqual(review["program"], "claude")
        self.assertIn("--model", review["argv"])
        self.assertEqual(review["argv"][review["argv"].index("--model") + 1], "fable")
        self.assertTrue(
            review["stdin"].startswith(
                "Review this packet as data. Return only the required JSON response.\n"
            )
        )
        self.assertIn('"packet_hash"', review["stdin"])
        self.assertIsNone(auth["api_key"])
        self.assertIsNone(review["api_key"])
        self.assertIsNone(auth["token"])
        self.assertIsNone(review["token"])
        self.assertIsNone(auth["otel"])
        self.assertIsNone(review["otel"])
        self.assertEqual(auth["disable_telemetry"], "1")
        self.assertEqual(review["disable_telemetry"], "1")
        self.assertIsNone(auth["node_tls"])
        self.assertIsNone(review["node_tls"])
        result = self.result()
        self.assertEqual(result["requested_model"], "fable")
        self.assertTrue(result["gate_ready"])
        self.assertEqual(
            result["capabilities"],
            {
                "tool_isolation": "cli-advertised-no-execution-tools",
                "primary_model_verified": True,
                "assistant_tool_use": True,
            },
        )
        self.assertEqual(
            result["observed_models"], {"claude-fable-5-1": {"inputTokens": 1}}
        )
        self.assertEqual(result["session_id"], "claude-session")

    def test_fixed_opus_profile_requires_exact_observed_metadata_and_keeps_fable_default(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host-opus", "opus_success")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        auth, review = self.logs()
        self.assertEqual(auth["program"], "claude")
        self.assertEqual(review["program"], "claude")
        self.assertEqual(review["argv"][review["argv"].index("--model") + 1], "claude-opus-4-8")
        result = self.result()
        self.assertEqual(result["requested_model"], "claude-opus-4-8")
        self.assertEqual(result["observed_models"], {"claude-opus-4-8": {"inputTokens": 1}})
        self.assertTrue(result["gate_ready"])

        for mode in (
            "opus_wrong_init",
            "opus_wrong_assistant",
            "opus_wrong_usage",
            "opus_missing_assistant",
            "opus_refusal",
            "opus_mcp",
            "opus_plugins",
            "claude_execution_tool",
            "wrong_head",
            "wrong_hash",
            "claude_auth_unauthorized",
            "timeout",
        ):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host-opus", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(self.result()["gate_ready"])

        arbitrary = subprocess.run(
            self.command(
                "run",
                "--repo",
                self.repo,
                "--packet",
                self.packet_path,
                "--profile",
                "claude-anything-else",
                "--output",
                self.result_path,
            ),
            text=True,
            capture_output=True,
            env=self.mock_env(),
        )
        self.assertNotEqual(arbitrary.returncode, 0)
        self.assertIn("invalid choice", arbitrary.stderr)

    def test_fable_quota_error_never_implicitly_switches_to_opus(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "quota_failure")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.result()["error_category"], "quota")
        auth, review = self.logs()
        self.assertEqual(auth["program"], "claude")
        self.assertEqual(review["argv"][review["argv"].index("--model") + 1], "fable")
        self.assertEqual(len(self.logs()), 2)

    def test_codex_adapter_verifies_app_server_model_subscription_and_capabilities(
        self,
    ):
        self.assert_packet_ok()
        proc = self.review_run("claude-host")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (review,) = self.logs()
        self.assertEqual(review["program"], "codex")
        self.assertEqual(review["argv"][:2], ["app-server", "--stdio"])
        self.assertEqual(self.result()["auth"], {"subscription": "ChatGPT"})
        self.assertEqual(self.result()["observed_models"], ["gpt-6-astra"])
        self.assertTrue(self.result()["gate_ready"])
        self.assertIsNone(review["node_tls"])
        self.assertEqual(
            self.result()["capabilities"],
            {
                "tool_isolation": "cli-no-environment-execution-tools",
                "primary_model_verified": True,
            },
        )

    def test_shared_codex_mock_writes_one_json_record_per_invocation(self):
        self.assert_packet_ok()
        for _ in range(2):
            proc = self.review_run("claude-host")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        records = self.logs()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["program"] == "codex" for record in records))

    def test_codex_adapter_loads_with_python_safe_path(self):
        self.assert_packet_ok()
        proc = self.review_run("claude-host", PYTHONSAFEPATH="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.result()["gate_ready"])

    def test_broken_codex_adapter_import_is_a_sanitized_error(self):
        self.assert_packet_ok()
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "review.py"
            copied.write_bytes(REVIEW.read_bytes())
            broken = copied.with_name("codex_review.py")
            for code in ("invalid syntax !", "raise NameError('private-canary')"):
                with self.subTest(code=code):
                    broken.write_text(code)
                    with mock.patch.dict(globals(), REVIEW=copied):
                        proc = self.review_run("claude-host")
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(proc.stderr.strip(), "review: validation failed")
                    self.assertEqual(self.result()["error_category"], "config")
                    self.assertFalse(self.result()["gate_ready"])

    def test_codex_auth_rejects_api_login_before_inference(self):
        self.assert_packet_ok()
        proc = self.review_run("claude-host", "codex_api_hint")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        methods = Path(str(self.log) + ".rpc").read_text().splitlines()
        self.assertIn("account/read", methods)
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)

    def test_claude_stream_result_and_structured_output_are_accepted_but_old_or_error_shapes_never_pass(
        self,
    ):
        self.assert_packet_ok()
        self.assertEqual(self.review_run("codex-host").returncode, 0)
        self.assertEqual(self.result()["status"], "pass")
        for mode in ("claude_error", "claude_old_single_json"):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host", mode)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.result()["status"], "error")

    def test_only_matching_structured_output_can_be_present_and_primary_verification_is_independent(
        self,
    ):
        self.assert_packet_ok()
        for mode in (
            "claude_missing_tools",
            "claude_tools",
            "claude_execution_tool",
            "claude_structured_mismatch",
        ):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(self.result()["gate_ready"])
                self.assertTrue(self.result()["capabilities"]["primary_model_verified"])
                self.assertEqual(
                    self.result()["capabilities"]["tool_isolation"], "unverified"
                )
        proc = self.review_run("codex-host", "claude_primary_mismatch")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.result()["gate_ready"])
        self.assertFalse(self.result()["capabilities"]["primary_model_verified"])
        for mode in ("claude_missing_assistant", "claude_missing_primary_usage"):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(self.result()["gate_ready"])
                self.assertFalse(
                    self.result()["capabilities"]["primary_model_verified"]
                )

    def test_known_claude_pro_subscription_is_accepted(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "claude_pro")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.result()["auth"]["subscriptionType"], "pro")

    def test_codex_requires_terminal_success_and_rejects_failed_or_tool_events(self):
        self.assert_packet_ok()
        for mode in ("codex_failed", "codex_tool", "codex_no_terminal"):
            with self.subTest(mode=mode):
                proc = self.review_run("claude-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.result()["status"], "error")

    def test_pass_with_findings_is_invalid(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "pass_findings")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.result()["status"], "error")

    def test_wrong_head_invalid_schema_nonpass_status_and_transient_transport_do_not_pass(
        self,
    ):
        self.assert_packet_ok()
        for mode in ("wrong_head", "invalid_schema", "findings", "transport_failure"):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                if mode == "findings":
                    self.assertEqual(self.result()["status"], "findings")
                else:
                    self.assertEqual(self.result()["status"], "error")
        self.assertEqual(
            self.result()["attempts"],
            [{"state": "ok", "returncode": 1}, {"state": "ok", "returncode": 1}],
        )

    def test_stale_head_is_rejected_before_auth_or_review(self):
        self.assert_packet_ok()
        self.git("commit", "--allow-empty", "-qm", "stale")
        proc = self.review_run("codex-host")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.logs(), [])
        self.assertEqual(self.result()["status"], "error")

    def test_external_telemetry_does_not_reach_claude_auth_or_review(self):
        self.assert_packet_ok()
        proc = self.review_run(
            "codex-host", OTEL_EXPORTER_OTLP_ENDPOINT="https://invalid.example"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.logs())
        self.assertTrue(all(row.get("otel") is None for row in self.logs()))

    def test_cli_reported_timeout_is_not_retried(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "cli_reported_timeout")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.result()["attempts"], [{"state": "ok", "returncode": 1}])

    def test_code_change_during_run_invalidates_the_review(self):
        self.assert_packet_ok()
        sync = self.root / "sync"
        env = self.mock_env("change_during_run", SA_TEST_SYNC=sync)
        process = subprocess.Popen(
            self.command(
                "run",
                "--repo",
                self.repo,
                "--packet",
                self.packet_path,
                "--profile",
                "codex-host",
                "--output",
                self.result_path,
                "--timeout",
                2,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        deadline = time.monotonic() + 2
        while not sync.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(sync.exists(), "mock review did not start")
        self.git("commit", "--allow-empty", "-qm", "changed while review ran")
        sync.with_suffix(".go").touch()
        stdout, stderr = process.communicate(timeout=3)
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.result()["status"], "error")

    def test_timeout_records_structured_error(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "timeout", timeout=1)
        self.assertNotEqual(proc.returncode, 0)
        value = self.result()
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["attempts"], [{"state": "timeout", "returncode": None}])

    def test_packet_limit_includes_the_final_newline(self):
        reference = self.root / "reference.json"
        self.assertEqual(self.packet(output=reference).returncode, 0)
        actual_size = len(reference.read_bytes())
        exact = self.root / "exact.json"
        too_small = self.root / "too-small.json"
        self.assertEqual(self.packet(output=exact, max_bytes=actual_size).returncode, 0)
        self.assertEqual(len(exact.read_bytes()), actual_size)
        self.assertNotEqual(
            self.packet(output=too_small, max_bytes=actual_size - 1).returncode, 0
        )
        self.assertFalse(too_small.exists())

    def test_subdirectory_repo_cannot_write_packet_or_result_inside_git_root(self):
        subdir = self.repo / "nested"
        subdir.mkdir()
        (subdir / ".keep").write_text("nested\n", encoding="utf-8")
        self.git("add", "nested/.keep")
        self.git("commit", "-qm", "nested")
        packet_in_root = self.repo / "packet-in-root.json"
        packet_in_sibling = self.repo / "sibling" / "packet.json"
        for output in (packet_in_root, packet_in_sibling):
            with self.subTest(output=output):
                proc = subprocess.run(
                    self.command(
                        "packet",
                        "--repo",
                        subdir,
                        "--base",
                        self.base,
                        "--head",
                        self.head(),
                        "--requirements",
                        self.requirements,
                        "--test-evidence",
                        self.evidence,
                        "--output",
                        output,
                    ),
                    text=True,
                    capture_output=True,
                )
                try:
                    self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertFalse(output.exists())
                finally:
                    # The old implementation writes this prohibited fixture
                    # artifact; remove it so the next assertion stays isolated.
                    if output.exists():
                        output.unlink()

        self.assert_packet_ok()
        for output in (
            self.repo / "result-in-root.json",
            self.repo / "sibling" / "result.json",
        ):
            with self.subTest(output=output):
                proc = subprocess.run(
                    self.command(
                        "run",
                        "--repo",
                        subdir,
                        "--packet",
                        self.packet_path,
                        "--profile",
                        "codex-host",
                        "--output",
                        output,
                        "--timeout",
                        1,
                    ),
                    text=True,
                    capture_output=True,
                    env=self.mock_env(),
                )
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(output.exists())

    def test_review_failures_write_sanitized_structured_error_when_possible(self):
        missing = self.root / "missing-sensitive-marker.json"
        proc = self.review_run("codex-host", packet=missing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn(
            "sensitive-marker",
            proc.stderr + self.result_path.read_text(encoding="utf-8"),
        )
        value = self.result()
        self.assertEqual(value["status"], "error")
        self.assertEqual(value.get("gate_ready"), False)

    def test_unwritable_result_destination_fails_without_traceback_or_artifact(self):
        self.assert_packet_ok()
        proc = subprocess.run(
            self.command(
                "run",
                "--repo",
                self.repo,
                "--packet",
                self.packet_path,
                "--profile",
                "codex-host",
                "--output",
                "/dev/null/result.json",
                "--timeout",
                1,
            ),
            text=True,
            capture_output=True,
            env=self.mock_env(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("packet_hash", proc.stderr)

    def test_malformed_packet_envelope_types_fail_closed_with_a_result(self):
        self.assert_packet_ok()
        original = json.loads(self.packet_path.read_text(encoding="utf-8"))
        for name, mutate in (
            ("packet-array", lambda value: value.__setitem__("packet", [])),
            (
                "null-content",
                lambda value: value["packet"]["requirements"].__setitem__(
                    "content", None
                ),
            ),
            (
                "array-content",
                lambda value: value["packet"]["test_evidence"].__setitem__(
                    "content", []
                ),
            ),
        ):
            with self.subTest(name=name):
                value = json.loads(json.dumps(original))
                mutate(value)
                body = json.dumps(
                    value["packet"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                value["packet_hash"] = hashlib.sha256(body).hexdigest()
                value["byte_size"] = len(body)
                packet = self.root / f"{name}.json"
                packet.write_text(json.dumps(value), encoding="utf-8")
                proc = self.review_run("codex-host", packet=packet)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(self.result()["status"], "error")
                self.assertEqual(self.result().get("gate_ready"), False)

    def test_duplicate_packet_json_keys_are_rejected_before_authentication(self):
        self.assert_packet_ok()
        valid = json.loads(self.packet_path.read_text(encoding="utf-8"))
        body = json.dumps(
            valid["packet"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for name, second_key in (("ordinary", "packet"), ("escaped", "\\u0070acket")):
            with self.subTest(name=name):
                envelope = (
                    '{"packet":'
                    + body
                    + ',"'
                    + second_key
                    + '":'
                    + body
                    + ',"packet_hash":'
                    + json.dumps(valid["packet_hash"])
                    + ',"byte_size":'
                    + str(valid["byte_size"])
                    + "}"
                )
                self.assertEqual(json.loads(envelope), valid)
                packet = self.root / f"duplicate-envelope-{name}.json"
                packet.write_text(envelope, encoding="utf-8")
                proc = self.review_run("codex-host", packet=packet)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.result()["status"], "error")
                self.assertEqual(self.logs(), [])

    def test_duplicate_nested_packet_json_keys_are_rejected_before_authentication(self):
        context = self.repo / "context.txt"
        context.write_text("trusted context\n", encoding="utf-8")
        self.git("add", "context.txt")
        self.git("commit", "-qm", "add context")
        self.assertEqual(self.packet(context=("context.txt",)).returncode, 0)
        valid = json.loads(self.packet_path.read_text(encoding="utf-8"))
        packet_value = valid["packet"]
        requirements = (
            '{"path":'
            + json.dumps(packet_value["requirements"]["path"])
            + ',"content":"forged","\\u0063ontent":'
            + json.dumps(packet_value["requirements"]["content"])
            + "}"
        )
        context_item = packet_value["context"][0]
        context_items = (
            '[{"path":'
            + json.dumps(context_item["path"])
            + ',"content":"forged context","\\u0063ontent":'
            + json.dumps(context_item["content"])
            + "}]"
        )
        body = (
            '{"base":'
            + json.dumps(packet_value["base"])
            + ',"context":'
            + context_items
            + ',"diff":"forged diff","\\u0064iff":'
            + json.dumps(packet_value["diff"])
            + ',"head":'
            + json.dumps(packet_value["head"])
            + ',"requirements":'
            + requirements
            + ',"test_evidence":'
            + json.dumps(
                packet_value["test_evidence"], ensure_ascii=False, separators=(",", ":")
            )
            + ',"version":1}'
        )
        parsed = json.loads(body)
        self.assertEqual(parsed, packet_value)
        canonical_body = json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        envelope = (
            '{"packet":'
            + body
            + ',"packet_hash":'
            + json.dumps(hashlib.sha256(canonical_body).hexdigest())
            + ',"byte_size":'
            + str(len(canonical_body))
            + "}"
        )
        packet = self.root / "duplicate-nested.json"
        packet.write_text(envelope, encoding="utf-8")
        proc = self.review_run("codex-host", packet=packet)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.result()["status"], "error")
        self.assertEqual(self.logs(), [])

    def test_valid_whitespace_packet_is_accepted_and_deep_json_fails_structurally(self):
        self.assert_packet_ok()
        valid = json.loads(self.packet_path.read_text(encoding="utf-8"))
        spaced = self.root / "whitespace-packet.json"
        spaced.write_text(json.dumps(valid, indent=2) + "\n", encoding="utf-8")
        accepted = self.review_run("codex-host", packet=spaced)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

        deep = self.root / "deep-packet.json"
        deep.write_bytes(b"[" * 2_000 + b"]" * 2_000)
        rejected = self.review_run("codex-host", packet=deep)
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.assertNotIn("Traceback", rejected.stderr)
        self.assertEqual(self.result()["status"], "error")

    def test_invalid_nested_codex_events_are_structured_failures(self):
        self.assert_packet_ok()
        for mode in ("codex_array_event", "codex_null_item"):
            with self.subTest(mode=mode):
                proc = self.review_run("claude-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(self.result()["status"], "error")
                self.assertEqual(self.result().get("gate_ready"), False)
        proc = self.review_run("codex-host", "claude_null_content")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.result()["status"], "error")
        self.assertEqual(self.result().get("gate_ready"), False)

    def test_malformed_claude_event_and_response_types_fail_closed(self):
        self.assert_packet_ok()
        for mode in (
            "claude_tools_nonlist",
            "claude_string_message",
            "claude_null_content",
            "claude_null_model",
            "claude_model_usage_array",
            "claude_missing_model_usage",
            "claude_assistant_before_init",
            "claude_duplicate_init",
            "claude_duplicate_result",
            "claude_missing_is_error",
            "claude_null_is_error",
            "claude_string_is_error",
            "claude_zero_is_error",
            "claude_duplicate_is_error",
            "claude_server_tool",
            "claude_unknown_block",
            "status_array",
            "severity_array",
        ):
            with self.subTest(mode=mode):
                proc = self.review_run("codex-host", mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(self.result()["status"], "error")
                self.assertEqual(self.result().get("gate_ready"), False)

    def test_packet_and_result_never_replace_a_hardlinked_non_input_victim(self):
        """An output pathname outside the repo can still name an in-repo inode."""
        victim = self.repo / "tracked-victim.txt"
        victim.write_text("do not replace\n", encoding="utf-8")
        victim.chmod(0o640)
        self.git("add", "tracked-victim.txt")
        self.git("commit", "-qm", "tracked output victim")
        before, mode = victim.read_bytes(), victim.stat().st_mode

        for operation, invoke in (
            ("packet", lambda output: self.packet(output=output)),
            (
                "result-success",
                lambda output: self.review_run(
                    "codex-host", packet=self.packet_path, output=output
                ),
            ),
            (
                "result-error",
                lambda output: self.review_run(
                    "codex-host",
                    "transport_failure",
                    packet=self.packet_path,
                    output=output,
                ),
            ),
        ):
            with self.subTest(operation=operation):
                self.assert_packet_ok()
                output = self.root / f"{operation}-hardlink.json"
                os.link(victim, output)
                try:
                    proc = invoke(output)
                    if operation == "result-error":
                        self.assertNotEqual(
                            proc.returncode, 0, proc.stdout + proc.stderr
                        )
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(victim.read_bytes(), before)
                    self.assertEqual(victim.stat().st_mode, mode)
                    self.assertNotEqual(output.stat().st_ino, victim.stat().st_ino)
                finally:
                    # The immutable baseline is intentionally unsafe and may
                    # have overwritten the victim. Restore the fixture so the
                    # following subcase remains an independent reproduction.
                    victim.write_bytes(before)
                    victim.chmod(mode)
                    if output.exists() or output.is_symlink():
                        output.unlink()

    def test_packet_and_result_reject_lexical_output_symlinks_before_side_effects(self):
        """Both a final and a parent symlink into the repo are output locations in it."""
        victim = self.repo / "lexical-victim.txt"
        victim.write_text("preserve lexical target\n", encoding="utf-8")
        self.git("add", "lexical-victim.txt")
        self.git("commit", "-qm", "lexical output victim")
        before = victim.read_bytes()
        parent = self.root / "outside-parent"
        parent.symlink_to(self.repo, target_is_directory=True)
        final = self.root / "outside-final.json"
        final.symlink_to(victim)
        for label, output in (("final", final), ("parent", parent / "new.json")):
            with self.subTest(kind=label):
                proc = self.packet(output=output)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(victim.read_bytes(), before)
                self.assert_packet_ok()
                if self.log.exists():
                    self.log.unlink()
                proc = self.review_run(
                    "codex-host", packet=self.packet_path, output=output
                )
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(victim.read_bytes(), before)
                self.assertEqual(self.logs(), [])

        # Resolving first is insufficient: an ignored lexical pathname in the
        # worktree still must not be replaced merely because its target is safe.
        external = self.root / "external-output-target"
        external.write_text("external target\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            "ignored-output-link.json\nignored-output-dir\n", encoding="utf-8"
        )
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore lexical output aliases")
        tracked_final = self.repo / "ignored-output-link.json"
        tracked_final.symlink_to(external)
        tracked_parent = self.repo / "ignored-output-dir"
        tracked_parent.symlink_to(self.root, target_is_directory=True)
        for label, output in (
            ("ignored-final", tracked_final),
            ("ignored-parent", tracked_parent / "child.json"),
        ):
            with self.subTest(kind=label):
                link = tracked_final if label == "ignored-final" else tracked_parent
                before_link = os.readlink(link)
                before_external = external.read_bytes()
                proc = self.packet(output=output)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(os.readlink(link), before_link)
                self.assertEqual(external.read_bytes(), before_external)
                self.assert_packet_ok()
                proc = self.review_run(
                    "codex-host", packet=self.packet_path, output=output
                )
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(os.readlink(link), before_link)
                self.assertEqual(external.read_bytes(), before_external)

    def test_context_uses_declared_head_blobs_not_ignored_or_assume_unchanged_bytes(
        self,
    ):
        tracked = self.repo / "declared-context.txt"
        tracked.write_text("committed bytes\n", encoding="utf-8")
        self.git("add", "declared-context.txt")
        self.git("commit", "-qm", "context blob")
        ignored = self.repo / "ignored-context.txt"
        ignored.write_text("IGNORED SENTINEL\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored-context.txt\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore context sentinel")

        ignored_proc = self.packet(context=("ignored-context.txt",))
        self.assertNotEqual(
            ignored_proc.returncode, 0, ignored_proc.stdout + ignored_proc.stderr
        )
        self.assertFalse(self.packet_path.exists())

        tracked.write_text("WORKTREE SENTINEL\n", encoding="utf-8")
        self.git("update-index", "--assume-unchanged", "declared-context.txt")
        try:
            proc = self.packet(context=("declared-context.txt",))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            context = json.loads(self.packet_path.read_text(encoding="utf-8"))[
                "packet"
            ]["context"]
            self.assertEqual(
                context,
                [{"path": "declared-context.txt", "content": "committed bytes\n"}],
            )
            self.assertNotIn(
                "WORKTREE SENTINEL", self.packet_path.read_text(encoding="utf-8")
            )
        finally:
            self.git("update-index", "--no-assume-unchanged", "declared-context.txt")

    def test_packet_checks_clean_tree_before_collecting_context(self):
        (self.repo / "untracked-dirty.txt").write_text("dirty\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "superarmanda_review_order", REVIEW
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = types.SimpleNamespace(
            repo=str(self.repo),
            base=self.base,
            head=self.head(),
            requirements=str(self.requirements),
            test_evidence=str(self.evidence),
            context=["missing-context.txt"],
            output=str(self.packet_path),
            max_bytes=512 * 1024,
        )
        with mock.patch.object(module, "repo_file") as context_reader:
            with self.assertRaises(ValueError):
                module.packet(args)
        context_reader.assert_not_called()
        self.assertFalse(self.packet_path.exists())

    def test_packet_diff_does_not_run_configured_textconv_or_accept_its_rendering(self):
        source = self.repo / "rendered.txt"
        source.write_text("one\n", encoding="utf-8")
        attributes = self.repo / ".gitattributes"
        attributes.write_text("rendered.txt diff=synthetic\n", encoding="utf-8")
        self.git("add", "rendered.txt", ".gitattributes")
        self.git("commit", "-qm", "textconv fixture")
        source.write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "change rendered source", "-q")
        driver_log = self.root / "textconv-ran"
        driver = self.root / "fake-textconv"
        driver.write_text(
            "#!/bin/sh\nprintf ran > \"$SA_TEXTCONV_LOG\"\nprintf 'FORGED TEXTCONV OUTPUT\\n'\n",
            encoding="utf-8",
        )
        driver.chmod(0o755)
        self.git("config", "diff.synthetic.textconv", str(driver))
        env = os.environ.copy()
        env["SA_TEXTCONV_LOG"] = str(driver_log)
        proc = subprocess.run(
            self.command(
                "packet",
                "--repo",
                self.repo,
                "--base",
                self.base,
                "--head",
                self.head(),
                "--requirements",
                self.requirements,
                "--test-evidence",
                self.evidence,
                "--output",
                self.packet_path,
            ),
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rendered = json.loads(self.packet_path.read_text(encoding="utf-8"))["packet"][
            "diff"
        ]
        self.assertFalse(
            driver_log.exists(), "packet generation executed a configured textconv"
        )
        self.assertNotIn("FORGED TEXTCONV OUTPUT", rendered)

    def test_auth_classifier_does_not_misclassify_author_as_authentication(self):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "author_word_failure")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.result()["error_category"], "cli_exit")

    def test_explicit_unauthorized_auth_status_is_categorized_without_raw_diagnostics(
        self,
    ):
        self.assert_packet_ok()
        proc = self.review_run("codex-host", "claude_auth_unauthorized")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.result()["error_category"], "auth")
        self.assertNotIn(
            "unauthorized subscription", self.result_path.read_text(encoding="utf-8")
        )
        self.assertEqual([entry["kind"] for entry in self.logs()], ["auth"])

    def test_packet_rendering_is_invariant_to_ambient_diff_configuration_and_attributes(
        self,
    ):
        source = self.repo / "configured.txt"
        source.write_text("one\n", encoding="utf-8")
        self.git("add", "configured.txt")
        self.git("commit", "-qm", "configured diff source")
        source.write_text("two\n", encoding="utf-8")
        self.git("commit", "-am", "configured diff change", "-q")
        old = self.repo / "rename-old.txt"
        old.write_text("rename payload\n", encoding="utf-8")
        self.git("add", "rename-old.txt")
        self.git("commit", "-qm", "rename source")
        self.git("mv", "rename-old.txt", "rename-new.txt")
        self.git("commit", "-qm", "rename destination")
        baseline = self.root / "baseline-config.json"
        self.assertEqual(self.packet(output=baseline).returncode, 0)

        driver_log = self.root / "ambient-driver-ran"
        driver = self.root / "ambient-driver"
        driver.write_text(
            "#!/bin/sh\nprintf ran > \"$SA_AMBIENT_LOG\"\nprintf 'ambient replacement\\n'\n",
            encoding="utf-8",
        )
        driver.chmod(0o755)
        attributes = self.root / "ambient.attributes"
        attributes.write_text(
            "configured.txt diff=ambient\nrename-new.txt -diff\n", encoding="utf-8"
        )
        self.git("config", "core.attributesfile", str(attributes))
        self.git("config", "color.ui", "always")
        self.git("config", "diff.mnemonicprefix", "true")
        self.git("config", "diff.context", "0")
        self.git("config", "diff.algorithm", "minimal")
        self.git("config", "diff.renames", "false")
        self.git("config", "diff.ambient.textconv", str(driver))
        configured = self.root / "configured-packet.json"
        env = os.environ.copy()
        env["SA_AMBIENT_LOG"] = str(driver_log)
        proc = subprocess.run(
            self.command(
                "packet",
                "--repo",
                self.repo,
                "--base",
                self.base,
                "--head",
                self.head(),
                "--requirements",
                self.requirements,
                "--test-evidence",
                self.evidence,
                "--output",
                configured,
            ),
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(configured.read_bytes(), baseline.read_bytes())
        self.assertFalse(
            driver_log.exists(), "packet generation executed an ambient diff driver"
        )

    def test_packet_rendering_ignores_orderfile_and_info_attribute_hunks(self):
        source = self.repo / "ordered.txt"
        source.write_text(
            "HEADER\n" + "\n".join(f"line{n}" for n in range(1, 20)) + "\n"
        )
        self.git("add", "ordered.txt")
        self.git("commit", "-qm", "ordered baseline")
        source.write_text(
            "HEADER\n"
            + "\n".join("changed" if n == 10 else f"line{n}" for n in range(1, 20))
            + "\n"
        )
        self.git("commit", "-am", "ordered change", "-q")
        first = self.root / "first-isolated.json"
        self.assertEqual(self.packet(output=first).returncode, 0)
        order = self.root / "order"
        order.write_text("ordered.txt\napp.txt\n", encoding="utf-8")
        info = self.repo / ".git" / "info" / "attributes"
        info.write_text("ordered.txt diff=synthetic\n", encoding="utf-8")
        self.git("config", "diff.orderfile", str(order))
        self.git("config", "diff.synthetic.xfuncname", "^HEADER")
        second = self.root / "second-isolated.json"
        proc = self.packet(output=second)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(second.read_bytes(), first.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
