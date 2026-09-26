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
spawns=log+".spawns"
open(spawns,"a").write(json.dumps({"argv":sys.argv[1:]})+"\n")
spawn=sum(1 for _ in open(spawns))
methods=log+".methods"
# Codex merges table overrides with the user's config; only dotted
# `<table>.<name>.enabled=false` overrides reach existing entries.
disabled=set()
for a, b in zip(sys.argv[1:], sys.argv[2:]):
 if a=="-c" and b.endswith(".enabled=false") and b.count(".")==2: disabled.add(tuple(b[:-len(".enabled=false")].split(".")))
def out(x): print(json.dumps(x), flush=True)
def response(i, result): out({"id":i,"result":result})
config={"model_provider":"openai","forced_login_method":"chatgpt","web_search":"disabled","project_doc_max_bytes":0,"orchestrator":{"mcp":{"enabled":False},"skills":{"enabled":False}},"chatgpt_base_url":"https://chatgpt.com/backend-api","features":{"hooks":False,"goals":False,"memories":False,"skill_search":False,"skill_mcp_dependency_install":False,"tool_suggest":False,"sleep_tool":False,"apps":False,"browser_use":False,"browser_use_external":False,"computer_use":False,"image_generation":False,"multi_agent":False,"plugins":False,"remote_plugin":False,"shell_tool":False,"unified_exec":False,"view_image":False,"code_mode":False,"code_mode_host":False},"mcp_servers":{},"notify":[],"hooks":{},"plugins":{},"apps":{},"otel":{"exporter":"none","trace_exporter":"none","metrics_exporter":"none","log_user_prompt":False},"model_providers":{},"model_catalog_json":None,"model_instructions_file":None,"experimental_instructions_file":None,"experimental_thread_store_endpoint":None}
config["skills"]={"include_instructions":False}
for line in sys.stdin:
 r=json.loads(line); i=r["id"]; m=r["method"]
 open(methods,"a").write(json.dumps({"spawn":spawn,"method":m})+"\n")
 if mode=="timeout": time.sleep(5)
 if mode=="malformed": print("{",flush=True); continue
 if mode=="duplicate": print('{"id":%d,"id":%d,"result":{}}'%(i,i),flush=True); continue
 if mode=="oversize": print("x"*(1024*1024+1),flush=True); continue
 if mode=="error": out({"id":i,"error":{"code":-1}}); continue
 if m=="initialize":
  out({"method":"configWarning","params":{"message":"synthetic warning"}})
  response(i,{})
 elif m=="config/read":
  user={}
  if mode in ("user_tables","user_mcp","ignore_disable","mcp_running","mcp_tools","mcp_status_cursor","mcp_status_empty_entry","mcp_status_no_tools","mcp_status_null_tools","mcp_status_omits_server","mcp_status_unknown_server","mcp_status_duplicate","mcp_status_error","mcp_status_missing","late_entry","non_bool_enabled","status_notification"): user["mcp_servers"]={"node_repl":{"command":"x","enabled":True},"reporting_db":{"command":"y"}}
  if mode in ("user_tables","user_plugins","late_entry","ignore_plugin_disable"): user["plugins"]={"github@openai-curated":{"enabled":True},"browser@openai-bundled":{"enabled":True}}
  if mode=="late_entry" and spawn>1: user["mcp_servers"]["added_later"]={"command":"z","enabled":True}
  if mode=="bad_name": user["mcp_servers"]={"a.b":{"command":"x","enabled":True}}
  if mode=="quoted_name": user["plugins"]={'x"y':{"enabled":True}}
  if mode=="too_many": user["mcp_servers"]={"s%d"%n:{"command":"x"} for n in range(300)}
  if mode=="bad_table": user["mcp_servers"]=["not-a-table"]
  for table, entries in user.items():
   if isinstance(entries, dict):
    for name, entry in entries.items():
     if (table, name) in disabled and not (mode=="ignore_disable" and name=="node_repl") and not (mode=="ignore_plugin_disable" and table=="plugins"): entry["enabled"]="false" if mode=="non_bool_enabled" else False
   config[table]=entries
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
 elif m=="mcpServerStatus/list":
  if mode=="mcp_status_error": out({"id":i,"error":{"code":-32601}}); continue
  if mode=="mcp_status_missing": out({"id":i,"result":{}}); continue
  if mode=="status_notification": out({"method":"mcpServer/startupStatus/updated","params":{"name":"node_repl"}})
  data=[]
  for name, entry in (config.get("mcp_servers") or {}).items():
   running=entry.get("enabled") is not False or (mode=="mcp_running" and name=="node_repl")
   data.append({"name":name,"runtimeStatus":None,"pluginId":None,"httpOrigin":None,"serverInfo":{"name":name} if running else None,"serverCapabilities":{} if running else None,"tools":{"t":{}} if running or (mode=="mcp_tools" and name=="node_repl") else {},"toolsError":None,"resources":[],"resourceTemplates":[],"authStatus":"unsupported"})
  if mode=="mcp_status_empty_entry": data[0]={}
  if mode=="mcp_status_no_tools": data[0].pop("tools")
  if mode=="mcp_status_null_tools": data[0]["tools"]=None
  if mode=="mcp_status_omits_server": data.pop()
  if mode=="mcp_status_unknown_server": data.append(dict(data[0],name="unlisted"))
  if mode=="mcp_status_duplicate": data.append(dict(data[0]))
  response(i,{"data":data,"nextCursor":"more" if mode=="mcp_status_cursor" else None})
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
  if mode.startswith("settings"):
   ts={"disabledPluginIds":[],"approvalPolicy":"on-request","approvalsReviewer":"user","sandboxPolicy":{"type":"readOnly","networkAccess":False},"activePermissionProfile":None,"model":"gpt-6-astra","modelProvider":"openai","effort":"medium","collaborationMode":{"mode":"default","settings":{"model":"gpt-6-astra"}},"multiAgentMode":"explicitRequestOnly"}
   tid="t"
   if mode=="settings_write": ts["sandboxPolicy"]={"type":"workspaceWrite","networkAccess":False}
   if mode=="settings_network": ts["sandboxPolicy"]["networkAccess"]=True
   if mode=="settings_approval": ts["approvalPolicy"]="never"
   if mode=="settings_model": ts["model"]="other"
   if mode=="settings_provider": ts["modelProvider"]="other"
   if mode=="settings_no_model": ts.pop("model")
   if mode=="settings_collab_model": ts["collaborationMode"]["settings"]["model"]="other"
   if mode=="settings_profile": ts["activePermissionProfile"]={"name":"full"}
   if mode=="settings_auto_reviewer": ts["approvalsReviewer"]="auto_review"
   if mode=="settings_no_reviewer": ts.pop("approvalsReviewer")
   if mode=="settings_null_reviewer": ts["approvalsReviewer"]=None
   if mode=="settings_collab_type": ts["collaborationMode"]="default"
   if mode=="settings_thread": tid="other"
   if mode=="settings_missing": ts=None
   out({"method":"thread/settings/updated","params":{"threadId":tid,"threadSettings":ts}})
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


    def spawns(self):
        path = Path(str(self.log) + ".spawns")
        return [json.loads(line)["argv"] for line in path.read_text().splitlines()]

    def methods(self):
        path = Path(str(self.log) + ".methods")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_user_mcp_and_plugins_are_disabled_per_process_and_proven_inert(self):
        for mode in ("user_tables", "user_mcp", "user_plugins"):
            with self.subTest(mode=mode):
                for suffix in (".spawns", ".methods"):
                    Path(str(self.log) + suffix).unlink(missing_ok=True)
                response, meta = self.invoke(mode)
                self.assertEqual(response, {"ok": True})
                self.assertTrue(meta["no_execution_tools"])
                spawns = self.spawns()
                self.assertEqual(len(spawns), 2)
                probe, review = spawns
                self.assertEqual(review[: len(probe)], probe)
                extra = review[len(probe):]
                expected = []
                if mode != "user_plugins":
                    expected += ["mcp_servers.node_repl.enabled=false", "mcp_servers.reporting_db.enabled=false"]
                if mode != "user_mcp":
                    expected += ["plugins.browser@openai-bundled.enabled=false", "plugins.github@openai-curated.enabled=false"]
                self.assertEqual(extra[0::2], ["-c"] * len(expected))
                self.assertEqual(sorted(extra[1::2]), sorted(expected))
                calls = self.methods()
                self.assertEqual(
                    [c["method"] for c in calls if c["spawn"] == 1],
                    ["initialize", "config/read"],
                )
                second = [c["method"] for c in calls if c["spawn"] == 2]
                self.assertEqual(
                    second[:4],
                    ["initialize", "config/read", "mcpServerStatus/list", "account/read"],
                )

    def test_empty_user_tables_use_one_process_and_still_prove_mcp_inert(self):
        self.invoke()
        self.assertEqual(len(self.spawns()), 1)
        self.assertIn("mcpServerStatus/list", [c["method"] for c in self.methods()])

    def test_disable_isolation_fails_closed(self):
        cases = (
            ("ignore_disable", "config"),
            ("ignore_plugin_disable", "config"),
            ("non_bool_enabled", "config"),
            ("late_entry", "config"),
            ("mcp_running", "config"),
            ("mcp_tools", "config"),
            ("mcp_status_cursor", "config"),
            ("mcp_status_missing", "config"),
            ("mcp_status_empty_entry", "config"),
            ("mcp_status_no_tools", "config"),
            ("mcp_status_null_tools", "config"),
            ("mcp_status_omits_server", "config"),
            ("mcp_status_unknown_server", "config"),
            ("mcp_status_duplicate", "config"),
            ("bad_name", "config"),
            ("quoted_name", "config"),
            ("too_many", "config"),
            ("bad_table", "config"),
            ("mcp_status_error", "protocol"),
            ("status_notification", "protocol"),
        )
        for mode, category in cases:
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(ValueError, "review CLI failed: " + category),
            ):
                self.invoke(mode)
            reached = {c["method"] for c in self.methods()}
            forbidden = {"account/read", "thread/start", "turn/start"}
            if not mode.startswith(("mcp_", "status_")):
                # Listing MCP status may start enabled servers, so it must
                # never happen before the effective config is proven safe.
                forbidden.add("mcpServerStatus/list")
            with self.subTest(mode=mode, check="stops_before_forbidden_methods"):
                self.assertFalse(reached & forbidden, mode)
            for suffix in (".spawns", ".methods"):
                Path(str(self.log) + suffix).unlink(missing_ok=True)

    def test_thread_settings_update_is_accepted_only_when_it_repeats_the_contract(self):
        self.assertEqual(self.invoke("settings")[0], {"ok": True})
        cases = (
            ("settings_write", "identity"),
            ("settings_network", "identity"),
            ("settings_approval", "identity"),
            ("settings_model", "identity"),
            ("settings_provider", "identity"),
            ("settings_no_model", "identity"),
            ("settings_collab_model", "identity"),
            ("settings_profile", "identity"),
            ("settings_auto_reviewer", "identity"),
            ("settings_no_reviewer", "identity"),
            ("settings_null_reviewer", "identity"),
            ("settings_collab_type", "identity"),
            ("settings_thread", "protocol"),
            ("settings_missing", "protocol"),
        )
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
