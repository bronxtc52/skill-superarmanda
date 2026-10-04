"""A `claude` that must never run: the tmux stand-in only records the new-session command.
If anything executes it, the test fails (the call is logged and the exit code is non-zero)."""
import json
import os
import sys

with open(os.path.join(os.environ.get("FAKE_TMUX_STATE", "."), "claude_calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
sys.stderr.write("the real claude is not available in this test\n")
sys.exit(99)
