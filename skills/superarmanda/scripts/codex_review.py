#!/usr/bin/env python3
"""Fail-closed, subscription-only adapter for Codex App Server.

This module deliberately does not provide OS isolation.  It removes Codex
execution surfaces at process start and requires empty App Server environments;
the event stream is then treated as an auditable source gate.
"""

import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

MODEL = "gpt-6-astra"
PROVIDER = "openai"
MAX_PROMPT = 512 * 1024 + 1024
# JSON escaping can expand arbitrary Unicode input by up to six bytes per
# source byte; retain space for the request envelope and caller schema.
MAX_INPUT = 6 * MAX_PROMPT + 64 * 1024
MAX_LINE = 1024 * 1024
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 4096
MAX_DISABLED_ENTRIES = 256
# Codex splits `-c` key paths on "." and keeps quotes literally, so only
# names that are addressable as bare path segments can be disabled.
ENTRY_NAME = re.compile(r"[A-Za-z0-9_@-]{1,128}")
DISABLED_TABLES = ("mcp_servers", "plugins")
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "hooks",
    "multi_agent",
    "apps",
    "plugins",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "view_image",
    "code_mode",
    "code_mode_host",
    "goals",
    "memories",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "sleep_tool",
)


def _fail(category):
    raise ValueError("review CLI failed: " + category)


def _no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("protocol")
        value[key] = item
    return value


def _json(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_LINE:
        _fail("protocol")
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("protocol")
    if not isinstance(value, dict):
        _fail("protocol")
    return value


def scrubbed_environment(source=None):
    """Preserve the login location, while excluding alternate auth/routing knobs."""
    source = os.environ if source is None else source
    blocked = re.compile(
        r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|OPENROUTER|LITELLM|PROXY|"
        r"BASE[_-]?URL|BEDROCK|VERTEX|FOUNDRY|CREDENTIAL|ROUTING|LOADER|"
        r"CONFIG|PLUGIN|EXTENSION|PYTHONPATH|NODE_(OPTIONS|PATH|EXTRA_CA_CERTS|TLS_REJECT_UNAUTHORIZED)|OTEL_|TELEMETRY|EXPORTER|ENDPOINT|SSL_CERT_FILE|SSL_CERT_DIR|CURL_CA_BUNDLE|REQUESTS_CA_BUNDLE|"
        r"^(LD_|DYLD_|BUN_))",
        re.I,
    )
    # Do not set HOME/CODEX_HOME: the existing subscription login stays where it is.
    return {
        k: v
        for k, v in source.items()
        if not k.startswith("GIT_")
        and ((k == "DISABLE_TELEMETRY" and v == "1") or not blocked.search(k))
    }


def app_server_argv(disabled=()):
    # Every override is passed before the server initializes.  Do not add
    # --ignore-user-config: the login's established location must remain usable.
    argv = ["codex", "app-server", "--stdio"]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    for override in (
        'model_provider="openai"',
        "model_providers={}",
        "mcp_servers={}",
        "notify=[]",
        'web_search="disabled"',
        'forced_login_method="chatgpt"',
        "project_doc_max_bytes=0",
        "orchestrator.mcp.enabled=false",
        "orchestrator.skills.enabled=false",
        "skills.include_instructions=false",
        "hooks={}",
        "plugins={}",
        "apps={}",
        "otel={}",
        'otel.exporter="none"',
        'otel.trace_exporter="none"',
        'otel.metrics_exporter="none"',
        "otel.log_user_prompt=false",
        'chatgpt_base_url="https://chatgpt.com/backend-api"',
    ):
        argv += ["-c", override]
    # Table overrides above are merged with the user's config rather than
    # replacing it, so each user entry is switched off for this process only.
    for table, name in disabled:
        argv += ["-c", f"{table}.{name}.enabled=false"]
    return argv


def disable_overrides(config):
    """Return `(table, name)` pairs that must be disabled for this process."""
    if not isinstance(config, dict):
        _fail("config")
    pairs = []
    for table in DISABLED_TABLES:
        entries = config.get(table)
        if entries in (None, {}):
            continue
        if not isinstance(entries, dict):
            _fail("config")
        for name in entries:
            if not isinstance(name, str) or not ENTRY_NAME.fullmatch(name):
                _fail("config")
            pairs.append((table, name))
    if len(pairs) > MAX_DISABLED_ENTRIES:
        _fail("config")
    return sorted(pairs)


def _disabled_table(value):
    if value in (None, {}):
        return True
    return isinstance(value, dict) and all(
        isinstance(entry, dict) and entry.get("enabled") is False
        for entry in value.values()
    )


def _require_inert_mcp(result):
    """Runtime proof that no MCP server was started or exposes tools."""
    data = result.get("data")
    if not isinstance(data, list) or result.get("nextCursor") is not None:
        _fail("config")
    for status in data:
        if not isinstance(status, dict):
            _fail("config")
        for key in ("serverInfo", "serverCapabilities", "runtimeStatus"):
            if status.get(key) is not None:
                _fail("config")
        for key in ("tools", "resources", "resourceTemplates"):
            if status.get(key) not in (None, {}, []):
                _fail("config")


def _unsafe_config(config):
    if not isinstance(config, dict):
        return True
    if (
        config.get("model_provider") != PROVIDER
        or config.get("forced_login_method") != "chatgpt"
        or config.get("web_search") != "disabled"
        or type(config.get("project_doc_max_bytes")) is not int
        or config["project_doc_max_bytes"] != 0
    ):
        return True
    orchestrator = config.get("orchestrator")
    if not isinstance(orchestrator, dict):
        return True
    for section in ("mcp", "skills"):
        setting = orchestrator.get(section)
        if not isinstance(setting, dict) or setting.get("enabled") is not False:
            return True
    features = config.get("features")
    if not isinstance(features, dict) or any(
        features.get(name) is not False for name in DISABLED_FEATURES
    ):
        return True
    if config.get("chatgpt_base_url") != "https://chatgpt.com/backend-api":
        return True
    skills = config.get("skills")
    if not isinstance(skills, dict) or skills.get("include_instructions") is not False:
        return True
    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or any(value != [] for value in hooks.values()):
        return True
    apps = config.get("apps")
    if not isinstance(apps, dict) or set(apps) - {"_default"}:
        return True
    otel = config.get("otel")
    if (
        not isinstance(otel, dict)
        or any(
            otel.get(key) != "none"
            for key in ("exporter", "trace_exporter", "metrics_exporter")
        )
        or otel.get("log_user_prompt") is not False
    ):
        return True
    # Non-empty tables are accepted only when every entry is explicitly
    # disabled; `_require_inert_mcp` then checks the live server state.
    if not all(_disabled_table(config.get(table)) for table in DISABLED_TABLES):
        return True
    unsafe = (
        "model_catalog_json",
        "model_instructions_file",
        "experimental_instructions_file",
        "notify",
        "model_providers",
        "experimental_thread_store_endpoint",
        "external_store",
        "telemetry",
    )
    for key in unsafe:
        value = config.get(key)
        if value not in (None, False, "", [], {}):
            return True
    for key, value in config.items():
        # Known custom-route settings must be absent. The canonical first-party
        # ChatGPT backend is explicitly allowed; other benign instruction
        # toggles are not evidence of a custom model instruction file.
        if key in {"chatgpt_base_url", "chatgpt_backend_url"}:
            if value != "https://chatgpt.com/backend-api":
                return True
        elif key in {
            "model_catalog_endpoint",
            "model_provider_endpoint",
            "external_store_endpoint",
        } and value not in (None, False, "", [], {}):
            return True
    return False


class _Server:
    def __init__(self, deadline, env, cwd, disabled=()):
        try:
            self.process = subprocess.Popen(
                app_server_argv(disabled),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError:
            _fail("spawn")
        self.deadline = deadline
        self.next_id = 1
        self.events = 0
        self.stream_bytes = 0
        self.buffer = b""
        self.pending = deque()

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass

    def send(self, method, params):
        ident = self.next_id
        self.next_id += 1
        wire = json.dumps(
            {"id": ident, "method": method, "params": params}, separators=(",", ":")
        )
        if len(wire) > MAX_INPUT:
            _fail("input")
        payload = (wire + "\n").encode()
        offset = 0
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            while offset < len(payload):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    _fail("timeout")
                selected = selectors.DefaultSelector()
                try:
                    selected.register(self.process.stdin, selectors.EVENT_WRITE)
                    if not selected.select(remaining):
                        _fail("timeout")
                    offset += os.write(self.process.stdin.fileno(), payload[offset:])
                finally:
                    selected.close()
        except (BrokenPipeError, OSError):
            _fail("transport")
        finally:
            try:
                os.set_blocking(self.process.stdin.fileno(), True)
            except OSError:
                pass
        return ident

    def receive(self):
        while b"\n" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                _fail("timeout")
            selected = selectors.DefaultSelector()
            try:
                selected.register(self.process.stdout, selectors.EVENT_READ)
                if not selected.select(remaining):
                    _fail("timeout")
                chunk = os.read(self.process.stdout.fileno(), 65536)
            finally:
                selected.close()
            if not chunk:
                _fail("transport")
            self.buffer += chunk
            if len(self.buffer) > MAX_LINE:
                _fail("protocol")
        line, self.buffer = self.buffer.split(b"\n", 1)
        self.stream_bytes += len(line) + 1
        if self.stream_bytes > MAX_STREAM_BYTES:
            _fail("protocol")
        message = _json(line)
        if message.get("method") not in {
            "item/agentMessage/delta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryDelta",
        }:
            self.events += 1
        if self.events > MAX_EVENTS:
            _fail("protocol")
        return message

    def request(self, method, params):
        ident = self.send(method, params)
        while True:
            message = self.receive()
            if "id" in message and "method" in message:
                _fail("execution")
            if "id" in message:
                if message.get("id") != ident:
                    _fail("protocol")
                if "error" in message or not isinstance(message.get("result"), dict):
                    _fail("protocol")
                return message["result"]
            _reject_event(message)
            self.pending.append(message)

    def event(self):
        return self.pending.popleft() if self.pending else self.receive()


def _reject_event(message, thread_id=None, turn_id=None):
    method = message.get("method")
    if not isinstance(method, str):
        _fail("protocol")
    # Passive account/rate-limit/reasoning metadata is expected. Requests,
    # approvals and all tool items are containment failures.
    if method not in {
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryDelta",
        "turn/started",
        "turn/completed",
        "thread/started",
        "thread/settings/updated",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "account/updated",
        "account/rateLimits/updated",
        "configWarning",
        "warning",
        "remoteControl/status/changed",
    }:
        _fail("execution" if "approval" in method.casefold() else "protocol")
    params = message.get("params")
    if not isinstance(params, dict):
        _fail("protocol")
    scoped = method.startswith("item/") or method.startswith("turn/")
    if thread_id is not None and scoped and params.get("threadId") != thread_id:
        _fail("protocol")
    if (
        turn_id is not None
        and method.startswith("item/")
        and params.get("turnId") != turn_id
    ):
        _fail("protocol")
    if method.startswith("turn/"):
        _optional_identity(params)
        turn = params.get("turn")
        if turn is not None:
            if not isinstance(turn, dict):
                _fail("protocol")
            _optional_identity(turn)
        if turn_id is not None:
            if params.get("turnId") not in (None, turn_id) or (
                isinstance(turn, dict) and turn.get("id") not in (None, turn_id)
            ):
                _fail("protocol")
    if method == "thread/settings/updated":
        _thread_settings(params)
    if method.startswith("thread/"):
        _optional_identity(params)
        thread = params.get("thread")
        if thread is not None:
            if not isinstance(thread, dict):
                _fail("protocol")
            _optional_identity(thread)
        if thread_id is not None:
            if params.get("threadId") not in (None, thread_id) or (
                isinstance(thread, dict) and thread.get("id") not in (None, thread_id)
            ):
                _fail("protocol")
    item = params.get("item")
    if item is not None:
        if not isinstance(item, dict):
            _fail("protocol")
        kind = str(item.get("type", "")).casefold()
        if kind not in {
            "agentmessage",
            "agent_message",
            "usermessage",
            "user_message",
            "reasoning",
        }:
            _fail("execution")


def _thread_settings(params):
    """Accept a settings echo only when it repeats the verified thread contract."""
    settings = params.get("threadSettings")
    if not isinstance(params.get("threadId"), str) or not isinstance(settings, dict):
        _fail("protocol")
    sandbox = settings.get("sandboxPolicy")
    if (
        not isinstance(sandbox, dict)
        or sandbox.get("type") != "readOnly"
        or sandbox.get("networkAccess") is not False
        or settings.get("approvalPolicy") != "on-request"
        or settings.get("activePermissionProfile") is not None
        or settings.get("approvalsReviewer") not in (None, "user")
    ):
        _fail("identity")
    _identity(settings)
    collaboration = settings.get("collaborationMode")
    if collaboration is not None:
        nested = collaboration.get("settings") if isinstance(collaboration, dict) else None
        if not isinstance(nested, dict):
            _fail("identity")
        _optional_identity(nested)


def _identity(result):
    if result.get("model") != MODEL or not (
        "modelProvider" in result or "model_provider" in result
    ):
        _fail("identity")
    _optional_identity(result)


def _optional_identity(result):
    if "model" in result and result["model"] != MODEL:
        _fail("identity")
    for key in ("modelProvider", "model_provider"):
        if key in result and result[key] != PROVIDER:
            _fail("identity")


def _account(result):
    account = result.get("account")
    if result.get("requiresOpenaiAuth") is not True:
        _fail("auth")
    if not isinstance(account, dict):
        _fail("auth")
    if account.get("type") != "chatgpt" or account.get("planType") not in {
        "plus",
        "pro",
        "team",
        "enterprise",
        "business",
        "go",
        "prolite",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "edu",
        "edu_plus",
        "edu_pro",
    }:
        _fail("auth")
    return "ChatGPT"


def _read_config(server, cwd):
    server.request(
        "initialize",
        {
            "clientInfo": {"name": "superarmanda", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    return server.request("config/read", {"cwd": cwd, "includeLayers": False}).get(
        "config"
    )


def run_review(prompt, schema, timeout, env=None, cwd=None):
    """Return `(structured_response, safe_metadata)` or a sanitized ValueError."""
    if not isinstance(prompt, str) or not isinstance(schema, dict) or timeout <= 0:
        _fail("input")
    if len(prompt.encode("utf-8")) > MAX_PROMPT:
        _fail("input")
    cwd = str(Path(cwd or os.getcwd()).resolve())
    env = scrubbed_environment(env)
    deadline = time.monotonic() + timeout
    server = _Server(deadline, env, cwd)
    try:
        # The first process only reports which user entries exist.  It never
        # reaches account, thread or turn requests.
        config = _read_config(server, cwd)
        disabled = disable_overrides(config)
        if disabled:
            server.close()
            server = _Server(deadline, env, cwd, disabled)
            config = _read_config(server, cwd)
        if _unsafe_config(config):
            _fail("config")
        _require_inert_mcp(server.request("mcpServerStatus/list", {}))
        subscription = _account(server.request("account/read", {"refreshToken": False}))
        thread = server.request(
            "thread/start",
            {
                "model": MODEL,
                "modelProvider": PROVIDER,
                "cwd": cwd,
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "environments": [],
                "ephemeral": True,
                "allowProviderModelFallback": False,
                "dynamicTools": [],
                "developerInstructions": "Review supplied text only. Do not use tools.",
            },
        )
        _identity(thread)
        if thread.get("approvalPolicy") != "on-request":
            _fail("identity")
        sandbox = thread.get("sandbox")
        if isinstance(sandbox, dict):
            sandbox_ok = (
                sandbox.get("type") == "readOnly"
                and sandbox.get("networkAccess") is False
            )
        else:
            sandbox_ok = False
        if not sandbox_ok:
            _fail("identity")
        thread_obj = thread.get("thread")
        if isinstance(thread_obj, dict):
            _optional_identity(thread_obj)
        thread_id = thread_obj.get("id") if isinstance(thread_obj, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            _fail("protocol")
        turn = server.request(
            "turn/start",
            {
                "threadId": thread_id,
                "model": MODEL,
                "effort": "medium",
                "environments": [],
                "outputSchema": schema,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )
        turn_obj = turn.get("turn")
        _optional_identity(turn)
        if isinstance(turn_obj, dict):
            _optional_identity(turn_obj)
        turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            _fail("protocol")
        messages, completed = [], None
        while completed is None:
            event = server.event()
            if "id" in event and "method" in event:
                _fail("execution")
            method = event.get("method")
            if method == "turn/completed":
                _reject_event(event, thread_id, turn_id)
                params = event.get("params")
                if not isinstance(params, dict) or params.get("threadId") != thread_id:
                    _fail("protocol")
                final_turn = params.get("turn")
                if (
                    not isinstance(final_turn, dict)
                    or final_turn.get("id") != turn_id
                    or final_turn.get("status") != "completed"
                ):
                    _fail("completion")
                _optional_identity(final_turn)
                completed = final_turn
                continue
            _reject_event(event, thread_id, turn_id)
            item = event.get("params", {}).get("item")
            if (
                method == "item/completed"
                and isinstance(item, dict)
                and str(item.get("type", "")).casefold()
                in {
                    "agentmessage",
                    "agent_message",
                }
            ):
                text = item.get("text")
                if not isinstance(text, str):
                    _fail("protocol")
                messages.append(text)
        if len(messages) != 1:
            _fail("completion")
        response = _json(messages[0])
        usage = completed.get("usage", {})
        if not isinstance(usage, dict):
            _fail("protocol")
        safe_usage = {
            k: v
            for k, v in usage.items()
            if k
            in {"input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"}
            and isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 0
        }
        return response, {
            "primary_model_verified": True,
            "no_execution_tools": True,
            "session_id": thread_id,
            "turn_id": turn_id,
            "observed_models": [MODEL],
            "auth": subscription,
            "usage": safe_usage,
        }
    finally:
        server.close()
