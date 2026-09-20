#!/usr/bin/env python3
"""Synthetic App Server contract tests; no live account or credentials involved."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "skills/superarmanda/scripts/codex_review.py"
spec = importlib.util.spec_from_file_location("codex_review", ADAPTER)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

MOCK = r"""#!/usr/bin/env python3
import json, os, sys, time
assert not any(key in os.environ for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_LOG_USER_PROMPTS", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "NODE_TLS_REJECT_UNAUTHORIZED"))
mode=os.environ.get("SA_TEST_MODE", os.environ.get("SA_MODE", "success"))
log=os.environ.get("SA_TEST_LOG") or os.environ["SA_LOG"]
open(log,"w").write(json.dumps({"program":"codex","kind":"review","argv":sys.argv[1:],"disable_telemetry":os.environ.get("DISABLE_TELEMETRY"),"otel":os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),"node_tls":os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED")})+"\n")
def out(x): print(json.dumps(x), flush=True)
def response(i, result): out({"id":i,"result":result})
config={"model_provider":"openai","forced_login_method":"chatgpt","web_search":"disabled","project_doc_max_bytes":0,"orchestrator":{"mcp":{"enabled":False},"skills":{"enabled":False}},"chatgpt_base_url":"https://chatgpt.com/backend-api","features":{"hooks":False,"goals":False,"memories":False,"skill_search":False,"skill_mcp_dependency_install":False,"tool_suggest":False,"sleep_tool":False,"apps":False,"browser_use":False,"browser_use_external":False,"computer_use":False,"image_generation":False,"multi_agent":False,"plugins":False,"remote_plugin":False,"shell_tool":False,"unified_exec":False,"view_image":False,"code_mode":False,"code_mode_host":False},"mcp_servers":{},"notify":[],"hooks":{},"plugins":{},"apps":{},"otel":{"exporter":"none","trace_exporter":"none","metrics_exporter":"none","log_user_prompt":False},"model_providers":{},"model_catalog_json":None,"model_instructions_file":None,"experimental_instructions_file":None,"experimental_thread_store_endpoint":None}
config["skills"]={"include_instructions":False}
for line in sys.stdin:
 r=json.loads(line); i=r["id"]; m=r["method"]
 if mode=="timeout": time.sleep(5)
 if mode=="malformed": print("{",flush=True); continue
 if mode=="duplicate": print('{"id":%d,"id":%d,"result":{}}'%(i,i),flush=True); continue
 if mode=="oversize": print("x"*(1024*1024+1),flush=True); continue
 if mode=="error": out({"id":i,"error":{"code":-1}}); continue
 if m=="initialize":
  out({"method":"configWarning","params":{"message":"synthetic warning"}})
  response(i,{})
 elif m=="config/read":
  if mode=="contamination": config["mcp_servers"]={"evil":{}}
  if mode=="experimental_instructions": config["experimental_instructions_file"]="/unsafe"
  if mode=="missing_skills": config.pop("skills")
  if mode=="missing_project_docs": config.pop("project_doc_max_bytes")
  if mode=="bad_project_docs": config["project_doc_max_bytes"]=True
  if mode=="nonzero_project_docs": config["project_doc_max_bytes"]=1
  if mode=="enabled_skill_instructions": config["skills"]["include_instructions"]=True
  if mode in ("missing_otel", "missing_hooks", "missing_apps"): config.pop(mode.removeprefix("missing_"))
  if mode=="missing_features": config.pop("features")
  if mode=="missing_feature": config["features"].pop("hooks")
  if mode=="enabled_hook": config["features"]["hooks"]=True
  if mode=="backend": config["chatgpt_base_url"]="https://invalid.example"
  if mode=="missing_backend": config.pop("chatgpt_base_url")
  if mode=="telemetry": config["otel"]["exporter"]={"otlp-http":{"endpoint":"https://invalid.example"}}
  if mode=="hook_command": config["hooks"]={"SessionStart":[{"command":"not-executed"}]}
  response(i,{"config":config})
 elif m=="account/read":
  account={"type":"api" if mode=="auth" else "chatgpt","planType":"free" if mode=="free" else "pro"}
  response(i,account if mode=="bare_auth" else {"requiresOpenaiAuth":mode!="auth_not_required","account":account})
 elif m=="thread/start":
  if mode=="model": response(i,{"model":"other","modelProvider":"openai","thread":{"id":"t"}})
  else:
   if mode in ("thread_started_model","thread_started_provider","thread_started_wrong_id","correct_thread_started_identity"):
    started={"thread":{"id":"other" if mode=="thread_started_wrong_id" else "t"}}
    if mode=="thread_started_model": started.update({"model":"other","modelProvider":"openai"})
    elif mode=="thread_started_provider": started.update({"model":"gpt-6-astra","modelProvider":"other"})
    elif mode=="correct_thread_started_identity": started.update({"model":"gpt-6-astra","modelProvider":"openai"})
    out({"method":"thread/started","params":started})
   response(i,{"model":"gpt-6-astra","modelProvider":"openai","approvalPolicy":"never" if mode=="approval_policy" else "on-request","sandbox":{"type":"readOnly","networkAccess":False},"thread":{"id":"t","model":"other" if mode=="nested_thread_model" else "gpt-6-astra","modelProvider":"openai"}})
 elif m=="turn/start":
  if mode=="early_wrongid": out({"method":"item/completed","params":{"threadId":"wrong","turnId":"u","item":{"type":"agentMessage","text":"{}"}}})
  if mode=="server_request": out({"id":99,"method":"tool/request","params":{}})
  if mode=="main_server_request": response(i,{"turn":{"id":"u"}}); out({"id":99,"method":"tool/request","params":{}}); continue
  if mode in ("wrong_start_model","wrong_start_provider","conflicting_start_provider","correct_start_identity"):
   started={"threadId":"t","turnId":"u","turn":{"id":"u"}}
   if mode=="wrong_start_model": started.update({"model":"other","modelProvider":"openai"})
   elif mode=="wrong_start_provider": started.update({"model":"gpt-6-astra","modelProvider":"other"})
   elif mode=="conflicting_start_provider": started.update({"model":"gpt-6-astra","modelProvider":"openai","model_provider":"other"})
   else: started.update({"model":"gpt-6-astra","modelProvider":"openai"})
   out({"method":"turn/started","params":started})
  if mode=="early":
   out({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"agentMessage","text":json.dumps({"ok":True})}}})
   out({"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed"}}})
  response(i,{"turn":{"id":"u","model":"other" if mode=="nested_turn_model" else "gpt-6-astra","modelProvider":"openai"}})
  if mode=="tool": out({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"commandExecution"}}})
  elif mode=="approval": out({"method":"commandExecution/requestApproval","params":{}})
  elif mode=="wrongid": out({"method":"turn/completed","params":{"threadId":"other","turn":{"id":"u","status":"completed"}}})
  elif mode=="missing": pass
  elif mode=="missing_ids":
   out({"method":"item/completed","params":{"item":{"type":"agentMessage","text":"{}"}}})
   out({"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed"}}})
  else:
   out({"method":"item/started","params":{"threadId":"t","turnId":"u","item":{"type":"userMessage"}}})
   out({"method":"thread/tokenUsage/updated","params":{"threadId":"t","tokenUsage":{}}})
   out({"method":"account/rateLimits/updated","params":{"rateLimits":{}}})
   out({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"userMessage"}}})
   out({"method":"item/started","params":{"threadId":"t","turnId":"u","item":{"type":"reasoning"}}})
   out({"method":"item/reasoning/textDelta","params":{"threadId":"t","turnId":"u","delta":"thinking"}})
   out({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"reasoning"}}})
   out({"method":"item/started","params":{"threadId":"t","turnId":"u","item":{"type":"agentMessage"}}})
   out({"method":"item/agentMessage/delta","params":{"threadId":"t","turnId":"u","delta":"answer"}})
   if mode=="long_stream":
    for _ in range(600): out({"method":"item/agentMessage/delta","params":{"threadId":"t","turnId":"u","delta":"chunk"}})
   out({"method":"item/completed","params":{"threadId":"t","turnId":"u","item":{"type":"agentMessage","text":json.dumps({"ok":True})}}})
   out({"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed","usage":{"output_tokens":True if mode=="usage_bool" else 2}}}})
"""


class Contract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bindir = root / "bin"
        bindir.mkdir()
        self.log = root / "argv.json"
        codex = bindir / "codex"
        codex.write_text(MOCK)
        codex.chmod(0o755)
        self.env = {
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "SA_TEST_LOG": str(self.log),
            "OPENAI_API_KEY": "must-not-pass",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "private-telemetry",
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_LOG_USER_PROMPTS": "1",
            "SSL_CERT_FILE": "private-ca",
            "SSL_CERT_DIR": "private-dir",
            "CURL_CA_BUNDLE": "private-bundle",
            "REQUESTS_CA_BUNDLE": "private-requests",
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "HOME": str(root / "home"),
        }

    def invoke(self, mode="success", timeout=5):
        env = {**self.env, "SA_TEST_MODE": mode}
        return adapter.run_review(
            "review", {"type": "object"}, timeout, env, self.temp.name
        )

    def test_success_uses_spawn_overrides_and_empty_environments(self):
        response, meta = self.invoke()
        self.assertEqual(response, {"ok": True})
        self.assertTrue(meta["primary_model_verified"])
        self.assertTrue(meta["no_execution_tools"])
        record = json.loads(self.log.read_text())
        argv = record["argv"]
        self.assertIn("app-server", argv)
        self.assertNotIn("--ignore-user-config", argv)
        for needle in (
            "mcp_servers={}",
            "notify=[]",
            "hooks={}",
            "plugins={}",
            "apps={}",
            'model_provider="openai"',
        ):
            self.assertIn(needle, argv)
        self.assertEqual(record["disable_telemetry"], "1")
        self.assertIsNone(record["otel"])
        self.assertIsNone(record["node_tls"])

    def test_contamination_and_identity_fail_closed(self):
        cases = [
            (mode, "config")
            for mode in (
                "contamination",
                "missing_features",
                "missing_feature",
                "enabled_hook",
                "backend",
                "missing_backend",
                "telemetry",
                "hook_command",
                "experimental_instructions",
                "missing_otel",
                "missing_hooks",
                "missing_apps",
                "missing_skills",
                "missing_project_docs",
                "bad_project_docs",
                "nonzero_project_docs",
                "enabled_skill_instructions",
            )
        ] + [
            (mode, "auth")
            for mode in ("auth", "free", "bare_auth", "auth_not_required")
        ]
        cases += [
            ("model", "identity"),
            ("approval_policy", "identity"),
            ("nested_turn_model", "identity"),
            ("nested_thread_model", "identity"),
            ("wrong_start_model", "identity"),
            ("wrong_start_provider", "identity"),
            ("thread_started_model", "identity"),
            ("thread_started_provider", "identity"),
            ("thread_started_wrong_id", "protocol"),
            ("conflicting_start_provider", "identity"),
            ("missing_ids", "protocol"),
        ]
        for mode, category in cases:
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "review CLI failed: " + category),
            ):
                self.invoke(mode)

    def test_tool_approval_malformed_and_ids_fail_closed(self):
        cases = (
            ("tool", "execution"),
            ("approval", "execution"),
            ("malformed", "protocol"),
            ("duplicate", "protocol"),
            ("oversize", "protocol"),
            ("error", "protocol"),
            ("wrongid", "protocol"),
            ("missing", "timeout"),
            ("early_wrongid", "protocol"),
            ("server_request", "execution"),
            ("main_server_request", "execution"),
        )
        for mode, category in cases:
            timeout = 0.25 if mode == "missing" else 5
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "review CLI failed: " + category),
            ):
                self.invoke(mode, timeout)

    def test_usage_boolean_counts_are_not_reported(self):
        _, metadata = self.invoke("usage_bool")
        self.assertNotIn("output_tokens", metadata["usage"])

    def test_turn_started_identity_can_be_correct_or_omitted(self):
        self.assertEqual(self.invoke("correct_start_identity")[0], {"ok": True})
        self.assertEqual(
            self.invoke("correct_thread_started_identity")[0], {"ok": True}
        )
        self.assertEqual(self.invoke()[0], {"ok": True})

    def test_environment_scrub_preserves_login_paths_without_api_key(self):
        value = adapter.scrubbed_environment(
            {
                "HOME": "/login",
                "CODEX_HOME": "/bad",
                "OPENAI_API_KEY": "bad",
                "PATH": "/x",
            }
        )
        self.assertEqual(value["HOME"], "/login")
        self.assertNotIn("OPENAI_API_KEY", value)
        self.assertEqual(value["CODEX_HOME"], "/bad")
        self.assertNotIn(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            adapter.scrubbed_environment({"OTEL_EXPORTER_OTLP_ENDPOINT": "private"}),
        )
        self.assertFalse(
            {"SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"}
            & adapter.scrubbed_environment(
                {
                    "SSL_CERT_FILE": "private",
                    "SSL_CERT_DIR": "private",
                    "CURL_CA_BUNDLE": "private",
                    "REQUESTS_CA_BUNDLE": "private",
                }
            ).keys()
        )
        self.assertNotIn(
            "NODE_TLS_REJECT_UNAUTHORIZED",
            adapter.scrubbed_environment({"NODE_TLS_REJECT_UNAUTHORIZED": "0"}),
        )

    def test_environment_scrub_keeps_sandbox_proxy_only_under_marker(self):
        proxy = {
            key: "http://sandbox:token@localhost:3128"
            for key in (
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "no_proxy", "all_proxy",
            )
        }
        other = {
            "CLOUDSDK_PROXY_ADDRESS": "bad",
            "DOCKER_HTTP_PROXY": "bad",
            "GRPC_PROXY": "bad",
            "RSYNC_PROXY": "bad",
            "OPENAI_API_KEY": "bad",
        }
        for marker in ("SANDBOX_RUNTIME", "CLAUDE_CODE_HOST_HTTP_PROXY_PORT"):
            with self.subTest(marker=marker):
                value = adapter.scrubbed_environment(
                    {**proxy, **other, marker: "1", "HOME": "/login"}
                )
                self.assertEqual({k: value.get(k) for k in proxy}, proxy)
                self.assertEqual(value.get(marker), "1")
                self.assertFalse(set(other) & value.keys())
                self.assertEqual(value["HOME"], "/login")
        without = adapter.scrubbed_environment({**proxy, **other, "HOME": "/login"})
        self.assertFalse((set(proxy) | set(other)) & without.keys())
        # A marker beside a remote proxy is an alternate route, not the sandbox.
        remote = dict(proxy, HTTPS_PROXY="http://proxy.corp.example:3128")
        forged = adapter.scrubbed_environment({**remote, "SANDBOX_RUNTIME": "1"})
        self.assertFalse(set(proxy) & forged.keys())
        for value in ("http://user:pw@127.0.0.1:3128", "localhost:3128", "http://[::1]:3128"):
            with self.subTest(value=value):
                kept = adapter.scrubbed_environment(
                    {"HTTPS_PROXY": value, "SANDBOX_RUNTIME": "1"}
                )
                self.assertEqual(kept.get("HTTPS_PROXY"), value)

    def test_long_delta_stream_and_packet_sized_prompt_are_accepted(self):
        response, _ = self.invoke("long_stream", timeout=2)
        self.assertEqual(response, {"ok": True})
        response, _ = adapter.run_review(
            "header\n" + "p" * (512 * 1024),
            {"type": "object"},
            2,
            self.env,
            self.temp.name,
        )
        self.assertEqual(response, {"ok": True})
        response, _ = adapter.run_review(
            ('😀"\\\n' * 70000), {"type": "object"}, 2, self.env, self.temp.name
        )
        self.assertEqual(response, {"ok": True})

    def test_delta_stream_byte_limit_can_fail_closed(self):
        previous = adapter.MAX_STREAM_BYTES
        self.addCleanup(setattr, adapter, "MAX_STREAM_BYTES", previous)
        adapter.MAX_STREAM_BYTES = 10000
        response, _ = self.invoke(timeout=2)
        self.assertEqual(response, {"ok": True})
        with self.assertRaisesRegex(ValueError, "review CLI failed: protocol"):
            self.invoke("long_stream", timeout=2)

    def test_early_turn_events_are_replayed_after_turn_start_response(self):
        response, _ = self.invoke("early")
        self.assertEqual(response, {"ok": True})


if __name__ == "__main__":
    unittest.main()
