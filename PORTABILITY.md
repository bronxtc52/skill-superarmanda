# Portable fork specification

Version 0.5.0 makes `superarmanda` usable from an independently owned fork.
The standalone workflow uses an ordinary isolated Git feature worktree unless
the active host or project policy requires its own admission integration. Such
a policy remains authoritative: missing host integration blocks autonomous
work; this repository neither ships it nor bypasses it.

`skills/superarmanda/scripts/install-skill.py` installs the skill directory as
a symlink for Claude, Codex, or both. It uses only Python's standard library.
The source is the skill directory that contains the installer, and `--target-home`
provides an explicit, isolated target for testing or managed installations.
Without it Claude uses the current user's `.claude`; Codex uses `CODEX_HOME`
when it is set, otherwise the current user's `.codex`. It never changes those
locations, authentication, Git configuration, remotes, or network state.

Installation is safe to repeat only for an identical live symlink. A foreign
symlink, dangling symlink, regular file, or directory at a destination is an
error and is never replaced. For `--client both`, every destination is checked
before directories or links are created, so a known conflict leaves neither
client partially installed. Filesystem permission and I/O failures after that
preflight are reported and are not claimed to be transactional.

For portability, destination topology treats casefolded, Unicode NFD-normalized
spellings as equivalent when detecting ancestor/descendant paths. This can
reject nested-looking names that are distinct on a case-sensitive filesystem.

The runtime remains self-contained. State and review packets work against a
synthetic local Git repository without network access. Subscription review
adapters retain their fixed model and evidence checks; unavailable required
review leaves a PR draft. Native coordinator, coder, and tester roles select
explicit models available on their host. `codex-host-opus` remains an explicit
supported initial profile. It is also the only permitted fallback after a
confirmed unavailable Fable quota attempt; no runner routes to it automatically.
