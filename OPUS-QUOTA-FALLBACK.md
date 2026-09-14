# Opus quota fallback acceptance specification

Base: `f10e538485a1d43e6a604214d845aa6e5e6fb34b`.

When a coordinator has recorded a Fable provider quota event or notice (or
current quota evidence already recorded for the run), it may make a separate
review attempt using the fixed `codex-host-opus` profile. The coordinator
retains artifacts for both attempts. This runtime does not select that profile
automatically, accept a caller model, infer quota from arbitrary diagnostics or
assistant text, or retry a failed Fable review with Opus.

`codex-host` remains pinned to requested and observed
`claude-fable-5-1` evidence through the Claude CLI's `fable` selector.
`codex-host-opus` is pinned to the Claude CLI selector and observed metadata
`claude-opus-4-8`. Both profiles retain first-party `claude.ai` subscription
authentication, the current safe-mode command, empty tools/MCP/plugins,
StructuredOutput-only validation, exact packet head and hash validation, and
the refusal-fallback rejection.

The GitHub evidence gate accepts only a fully anchored observed clean comment
from `chatgpt-codex-connector[bot]` with actor type `Bot`, the existing exact
footer, and an API-resolved SHA equal to the requested full SHA. The closing
summary phrase is accepted by property (#442), not by a fixed wording list:
one line, 1–48 characters, containing none of `\n`, `<`, `>`, `[`, `]`,
`` ` ``, `#` anywhere in it, including the first character. Any prose that
breaks that property, any body mutation, finding,
untrusted actor, stale review, or unresolved/mismatched SHA remains non-pass.
