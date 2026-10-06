#!/usr/bin/env python3
"""Local, atomic state for one Superarmanda run; no service or global ledger."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import uuid
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Fable subagent roles of the ordinary pipeline (1.2.3, #86 п.5-9): the architect of step 1, the
# internal review before the external packet, the triage of external findings, the investigator of
# a bug and the final check against the requirements. Manifest version 2 only; always Fable.
# The results of architect, triage and investigator are records: they change neither the task
# status, nor readiness, nor the merge gate. Under the review policy 1.2.4 internal_reviewer and
# final_check are part of the readiness of a high-risk task (fable_role_gaps); under the policy
# 1.2.1 they are records too.
FABLE_ROLES = ("architect", "internal_reviewer", "triage", "investigator", "final_check")
ROLES = {
    "coder",
    "tester",
    "cross_provider_reviewer",
    "github_codex_review",
    "coderabbit",
    "second_reviewer",
    *FABLE_ROLES,
}
STATUSES = {"pass", "findings", "incomplete", "error", "unavailable"}
# The internal Fable review as a fix-loop source: its rounds spend no fix cycle and are counted
# apart (`internal_rounds` of the task), at most MAX_INTERNAL_ROUNDS, and only before the first
# external review of the task in this run.
INTERNAL_SOURCE = "internal_reviewer"
MAX_INTERNAL_ROUNDS = 3
FIX_SOURCES = {
    "cross_provider_reviewer", "second_reviewer", "github_codex_review", "coderabbit", "tester",
    INTERNAL_SOURCE,
}
DECISIONS = {"invariant", "cut_surface", "accept_limitation"}
# Reviewer channels whose low/P3-only findings may be deferred to the remainder
# of the next wave/task without spending the fix cap (#36). Tester is excluded:
# a failed check is never a nit.
DEFER_SOURCES = {"cross_provider_reviewer", "second_reviewer", "github_codex_review", "coderabbit"}
# The roles of an external review: the first task-result of any of them (at any status) is the
# first external packet of the task and closes the internal fix-loop source for the run.
EXTERNAL_REVIEW_ROLES = DEFER_SOURCES
MAX_DECISION_NOTE_LENGTH = 500
# Every key state.py writes into a manifest. The merge gate refuses a manifest
# carrying a key outside these sets: it cannot judge a record type it does not know.
MANIFEST_KEYS = {
    "version", "run_id", "repo", "base", "head", "tree_fingerprint", "tasks",
    "created_at", "updated_at", "plan", "wave", "position", "run", "review_policy",
}
TASK_KEYS = {
    "status", "fix_cycles", "results", "session_roles", "fix_sources", "decisions",
    "decision_required_for", "deferrals", "acceptances", "blocked_reason", "risk",
    "review_history", "internal_rounds", "external_review", "internal_review",
}
# Task keys only manifest version 2 carries.
V2_TASK_KEYS = ("risk", "review_history", "internal_rounds", "external_review", "internal_review")
DEFAULT_MAX_RUNS = 2
MAX_RUNS_LIMIT = 1000
RUNS_FILE_VERSION = 1
MAX_PLAN_BYTES = 1024 * 1024
NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RISKS = {"low", "medium", "high"}
RISK_ORDER = ("low", "medium", "high")
# Manifest version 1 is judged by the rules of 1.2.0 (one cross-provider review, no model in
# the result). Version 2 carries `review_policy` and is judged by the policy below (#86).
MANIFEST_VERSIONS = (1, 2)
# The version of the review policy is the compatibility boundary of the rules (not of the shape):
# `init` writes POLICY_VERSION, and every rule takes the version from the manifest it judges.
# 1.2.1: the model by risk and two reviews for high (1.2.1-1.2.3). 1.2.4: for a high-risk task
# internal_reviewer and final_check are mandatory as well (MANDATORY_ROLES_POLICIES).
POLICY_VERSION = "1.2.4"
POLICY_VERSIONS = {"1.2.1", POLICY_VERSION}
MANDATORY_ROLES_POLICIES = {"1.2.4"}
MANDATORY_FABLE_ROLES = ("internal_reviewer", "final_check")
POLICY_KEYS = {"version", "level"}
V2_ONLY_ROLES = {"second_reviewer", *FABLE_ROLES}
# Closed dictionary of coder/tester models. Matching is exact: no trim, no case folding, no prefix.
FABLE_MODEL = "claude-fable-5-1"
SONNET_MODEL = "claude-sonnet-5-5"
MODEL_ALIASES = {
    FABLE_MODEL: FABLE_MODEL,
    SONNET_MODEL: SONNET_MODEL,
    "fable": FABLE_MODEL,
    "sonnet": SONNET_MODEL,
}
MODEL_ROLES = ("coder", "tester")
# The two task reviews of a high-risk task, and the review.py profiles their reports may carry:
# exactly one claude-host (Astra) and one codex-host (Fable); codex-host-opus stands in for
# codex-host only with a quota error report of codex-host for the same HEAD and packet.
REVIEW_ROLES = ("cross_provider_reviewer", "second_reviewer")
ASTRA_PROFILE = "claude-host"
FABLE_PROFILE = "codex-host"
OPUS_PROFILE = "codex-host-opus"
REVIEW_PROFILES = {ASTRA_PROFILE, FABLE_PROFILE, OPUS_PROFILE}
MAX_REPORT_BYTES = 1024 * 1024
# Keys of a `review_history` item that the rules read; each must be a string.
HISTORY_TEXT_KEYS = ("role", "status", "head", "result_sha256")
PLAN_KEYS = {"version", "chain", "repo", "base_branch", "waves"}
WAVE_KEYS = {
    "id",
    "title",
    "goal",
    "requirements",
    "acceptance",
    "risk",
    "checks",
    "depends_on",
}


def fail(message):
    raise SystemExit(f"state: {message}")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def check_position(position):
    """`position` is optional; when present it must have the shape `mark` writes."""
    if position is None:
        return
    text_keys = ("task", "head", "tree_fingerprint", "recorded_at")
    step = position.get("step") if isinstance(position, dict) else None
    if not (
        isinstance(position, dict)
        and all(isinstance(position.get(key), str) for key in text_keys)
        and type(step) is int
        and 1 <= step <= 7
        and type(position.get("safe_point")) is bool
    ):
        fail("malformed position in manifest")


def check_schema(value):
    """The one schema check behind every command that reads a manifest. Closed: an unknown
    version, or a version 2 without a well-formed `review_policy`, is refused, never guessed."""
    version = value.get("version") if isinstance(value, dict) else None
    if type(version) is not int or version not in MANIFEST_VERSIONS:
        fail("unsupported or malformed manifest")
    tasks = value.get("tasks")
    if not isinstance(tasks, dict):
        fail("unsupported or malformed manifest")
    if version == 1:
        # Version 1 keeps the shape of 1.2.0: nothing of the policy may ride on it.
        if "review_policy" in value:
            fail("malformed manifest: review_policy requires manifest version 2")
        for entry in tasks.values():
            if not isinstance(entry, dict):
                continue
            used = set(entry.get("results") or ()) if isinstance(entry.get("results"), dict) else set()
            if isinstance(entry.get("session_roles"), dict):
                used |= {role for role in entry["session_roles"].values() if isinstance(role, str)}
            if any(key in entry for key in V2_TASK_KEYS) or used & V2_ONLY_ROLES:
                fail(
                    "malformed manifest: task risk, review_history, internal_rounds, "
                    "external_review, internal_review, second_reviewer and the Fable subagent roles "
                    "require manifest version 2"
                )
        return
    policy = value.get("review_policy")
    if "review_policy" not in value:
        fail("manifest version 2 has no review_policy: refusing to judge it")
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        fail(f"malformed review_policy: must be an object with exactly {sorted(POLICY_KEYS)}")
    if not isinstance(policy["version"], str) or policy["version"] not in POLICY_VERSIONS:
        fail(f"unknown review_policy version: this state.py knows {sorted(POLICY_VERSIONS)}")
    if not isinstance(policy["level"], str) or policy["level"] not in RISKS:
        fail(f"review_policy level must be one of {sorted(RISKS)}")
    for name, entry in tasks.items():
        if isinstance(entry, dict) and "risk" in entry and not (
            isinstance(entry["risk"], str) and entry["risk"] in RISKS
        ):
            fail(f"task {name} risk must be one of {sorted(RISKS)}")
        history = entry.get("review_history", []) if isinstance(entry, dict) else []
        if not isinstance(history, list) or not all(
            isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in HISTORY_TEXT_KEYS)
            for item in history
        ):
            fail(f"task {name} review_history is malformed")
        rounds = entry.get("internal_rounds", []) if isinstance(entry, dict) else []
        if not isinstance(rounds, list) or not all(
            isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in ("head", "result_sha256"))
            for item in rounds
        ):
            fail(f"task {name} internal_rounds is malformed")
        # the marker is absent (no external review yet, or a manifest of 1.2.2) or an object:
        # state.py never writes `null`, so a null is a hand edit, not «no packet yet»
        if isinstance(entry, dict) and "external_review" in entry and not (
            isinstance(entry["external_review"], dict)
            and isinstance(entry["external_review"].get("role"), str)
        ):
            fail(f"task {name} external_review is malformed")
        credit = entry.get("internal_review") if isinstance(entry, dict) else None
        if isinstance(entry, dict) and "internal_review" in entry and not (
            isinstance(credit, dict)
            and all(isinstance(credit.get(key), str) for key in ("status", "head"))
        ):
            fail(f"task {name} internal_review is malformed")


def require_v2(data, what):
    if data["version"] != 2:
        fail(f"{what} requires manifest version 2 (version 1 is judged by the rules of 1.2.0)")


def task_risk(data, entry):
    """Effective risk of a task: the higher of the run's policy level and the task's own risk.
    None for manifest version 1, which has no policy."""
    if data["version"] != 2:
        return None
    level = data["review_policy"]["level"]
    own = (entry or {}).get("risk", level)
    return max(level, own, key=RISK_ORDER.index)


def policy_version(data):
    """The version of the review policy the manifest is judged by; None for manifest version 1."""
    return data["review_policy"]["version"] if data["version"] == 2 else None


def role_model(risk):
    return FABLE_MODEL if risk == "high" else SONNET_MODEL


def read(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    check_schema(value)
    check_position(value.get("position"))
    session_owners(value)
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(directory, *arguments):
    try:
        return (
            subprocess.check_output(
                git_argv(directory, *arguments),
                stderr=subprocess.PIPE,
                env=git_environment(),
            )
            .decode()
            .strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"").decode(errors="replace").strip()
        fail(f"git {' '.join(arguments)} failed" + (f": {detail}" if detail else ""))


def git_argv(directory, *arguments):
    return [
        "git",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(directory),
        *arguments,
    ]


def git_environment():
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return env


def git_optional(directory, *arguments):
    return subprocess.run(
        git_argv(directory, *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=git_environment(),
        check=False,
    )


def repo(value):
    """Return the canonical Git worktree root for a supplied local path."""
    location = str(Path(value).resolve())
    return git(location, "rev-parse", "--show-toplevel")


def commit(directory, ref):
    return git(directory, "rev-parse", "--verify", f"{ref}^{{commit}}")


def validate_revision_pair(directory, base, head):
    base, head = commit(directory, base), commit(directory, head)
    actual = commit(directory, "HEAD")
    if head != actual:
        fail("--head does not match repository HEAD")
    if git(directory, "merge-base", base, head) != base:
        fail("--base must be an ancestor of --head")
    return base, head


def safe_lock_fd(lock_path):
    flags = os.O_RDWR | os.O_CREAT
    if not hasattr(os, "O_NOFOLLOW"):
        fail("platform does not support safe lock opening")
    nofollow = os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags | nofollow | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            before = os.lstat(lock_path)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                fail("lock must be a single-link regular file")
            fd = os.open(lock_path, flags | nofollow)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(fd)
                fail("lock changed while opening")
        except OSError as exc:
            fail(f"cannot open lock: {exc}")
    except OSError as exc:
        fail(f"cannot create lock: {exc}")
    current = os.fstat(fd)
    try:
        final = os.lstat(lock_path)
    except OSError as exc:
        os.close(fd)
        fail(f"cannot validate lock: {exc}")
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
    ):
        os.close(fd)
        fail("lock must be a single-link regular file")
    return fd


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    fd = safe_lock_fd(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


MAX_SUBMODULE_DEPTH = 32


def fingerprint(directory):
    """Hash raw index/worktree state; v3 deliberately does not render diffs."""
    digest = hashlib.sha256()
    digest.update(b"superarmanda-tree-fingerprint-v3\0")
    _fingerprint_tree(digest, Path(directory), set(), set(), 0)
    return digest.hexdigest()


def _audit_filters(directory):
    filters = git_optional(
        directory, "config", "--includes", "--get-regexp", r"^filter\."
    )
    if filters.returncode == 0:
        fail("cannot fingerprint repository with configured filters")
    if filters.returncode != 1:
        fail("cannot audit repository filters")


def _fingerprint_tree(digest, directory, gitdirs, roots, depth):
    if depth > MAX_SUBMODULE_DEPTH:
        fail("cannot fingerprint submodule nesting beyond limit")
    _audit_filters(directory)
    tracked = git_raw(directory, "ls-files", "-s", "-z")
    gitlinks = set()
    for entry in (item for item in tracked.split(b"\0") if item):
        try:
            metadata, raw_name = entry.split(b"\t", 1)
            mode, _oid, stage = metadata.split()
        except ValueError:
            fail("cannot fingerprint malformed index")
        if stage != b"0":
            fail("cannot fingerprint unmerged index")
        digest_field(digest, b"tracked-path", raw_name)
        digest_field(digest, b"tracked-mode", mode)
        digest_field(digest, b"tracked-oid", _oid)
        path = Path(directory, os.fsdecode(raw_name))
        if mode == b"160000":
            gitlinks.add(raw_name)
            _hash_gitlink(digest, path, directory, gitdirs, roots, depth + 1)
        else:
            _hash_worktree_entry(digest, path, directory)
    _reject_unignored_empty_directories(Path(directory), gitlinks)
    names = git_raw(
        directory, "ls-files", "--others", "--exclude-standard", "-z"
    ).split(b"\0")
    for raw_name in sorted(name for name in names if name):
        digest_field(digest, b"untracked-path", raw_name)
        _hash_worktree_entry(digest, Path(directory, os.fsdecode(raw_name)), directory)


def _reject_unignored_empty_directories(directory, gitlinks):
    """Reject state invisible to v3: unignored empty directories.

    This is a validation gate, not a v4 fingerprint encoding.  It deliberately
    never follows symlinks or descends into Git metadata or gitlink roots.
    """

    def visit(path, relative):
        if relative.parts:
            checked = git_optional(
                directory, "check-ignore", "-q", "--", relative.as_posix() + "/"
            )
            if checked.returncode == 0:
                return False
            if checked.returncode != 1:
                fail(f"cannot check ignore status for directory {relative}")
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            fail(f"cannot inspect directory {path}: {exc}")
        visible_children = 0
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                fail(f"cannot inspect directory entry {entry.path}: {exc}")
            child_relative = relative / entry.name
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                raw_child = os.fsencode(child_relative.as_posix())
                if raw_child not in gitlinks:
                    if visit(Path(entry.path), child_relative):
                        visible_children += 1
                else:
                    visible_children += 1
            else:
                checked = git_optional(
                    directory, "check-ignore", "-q", "--", child_relative.as_posix()
                )
                if checked.returncode == 1:
                    visible_children += 1
                elif checked.returncode != 0:
                    fail(f"cannot check ignore status for file {child_relative}")
        if relative.parts and not visible_children:
            fail(f"cannot fingerprint unignored empty directory {relative}")
        return True

    visit(directory, Path())


def _hash_gitlink(digest, path, parent, gitdirs, roots, depth):
    _reject_symlink_ancestors(path, parent)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        digest_field(digest, b"gitlink-uninitialized", b"missing")
        return
    except OSError as exc:
        fail(f"cannot fingerprint submodule {path}: {exc}")
    if not stat.S_ISDIR(mode):
        fail(f"cannot fingerprint gitlink that is not a directory {path}")
    endpoint = path / ".git"
    try:
        endpoint_mode = os.lstat(endpoint).st_mode
    except FileNotFoundError:
        try:
            if any(path.iterdir()):
                fail(f"cannot fingerprint nonempty uninitialized gitlink {path}")
        except OSError as exc:
            fail(f"cannot inspect uninitialized gitlink {path}: {exc}")
        digest_field(digest, b"gitlink-uninitialized", b"empty")
        return
    except OSError as exc:
        fail(f"cannot fingerprint submodule endpoint {path}: {exc}")
    if stat.S_ISLNK(endpoint_mode) or not (
        stat.S_ISREG(endpoint_mode) or stat.S_ISDIR(endpoint_mode)
    ):
        fail(f"cannot fingerprint unsafe submodule endpoint {path}")
    if stat.S_ISDIR(endpoint_mode):
        gitdir = endpoint.resolve()
    else:
        try:
            content = endpoint.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read submodule endpoint {path}: {exc}")
        prefix = "gitdir: "
        if not content.startswith(prefix) or "\n" not in content:
            fail(f"cannot fingerprint malformed submodule endpoint {path}")
        target = content[len(prefix) :].splitlines()[0]
        if not target:
            fail(f"cannot fingerprint unsafe submodule endpoint {path}")
        target_path = Path(target)
        gitdir = (
            target_path.resolve()
            if target_path.is_absolute()
            else (path / target_path).resolve()
        )
    parent_common = Path(git(parent, "rev-parse", "--git-common-dir"))
    if not parent_common.is_absolute():
        parent_common = (Path(parent) / parent_common).resolve()
    parent_gitdir = Path(git(parent, "rev-parse", "--git-dir"))
    if not parent_gitdir.is_absolute():
        parent_gitdir = (Path(parent) / parent_gitdir).resolve()
    metadata = (parent_common / "modules", parent_gitdir / "modules")
    if (
        not any(_contains(modules, gitdir) for modules in metadata)
        and gitdir != endpoint.resolve()
    ):
        fail(f"cannot fingerprint submodule outside parent metadata {path}")
    root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    actual_gitdir = Path(git(path, "rev-parse", "--git-dir"))
    if not actual_gitdir.is_absolute():
        actual_gitdir = (path / actual_gitdir).resolve()
    common_gitdir = Path(git(path, "rev-parse", "--git-common-dir"))
    if not common_gitdir.is_absolute():
        common_gitdir = path / common_gitdir
    if (
        root != path.resolve()
        or actual_gitdir != gitdir
        or common_gitdir.resolve() != gitdir
    ):
        fail(f"cannot fingerprint submodule with unsafe Git routing {path}")
    key = (str(gitdir), str(root))
    if key in gitdirs or str(root) in roots:
        fail("cannot fingerprint cyclic submodule")
    gitdirs.add(key)
    roots.add(str(root))
    digest_field(digest, b"gitlink-initialized", b"")
    digest_field(digest, b"gitlink-head", git(root, "rev-parse", "HEAD").encode())
    _fingerprint_tree(digest, root, gitdirs, roots, depth)
    # Frame nested entries so moving identical untracked bytes across the
    # submodule boundary cannot preserve the parent fingerprint.
    digest_field(digest, b"gitlink-end", b"")
    gitdirs.remove(key)
    roots.remove(str(root))


def _reject_symlink_ancestors(path, parent):
    try:
        relative = path.relative_to(parent)
    except ValueError:
        fail("cannot fingerprint submodule outside its parent")
    current = Path(parent)
    for component in relative.parts:
        current /= component
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                fail(f"cannot fingerprint symlinked submodule path {path}")
        except FileNotFoundError:
            return
        except OSError as exc:
            fail(f"cannot inspect submodule path {path}: {exc}")


def git_raw(directory, *arguments):
    try:
        return subprocess.check_output(
            git_argv(directory, *arguments), env=git_environment()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f"cannot fingerprint repository: {exc}")


def _hash_worktree_entry(digest, path, root):
    try:
        if not _contains(Path(root).resolve(), path.parent.resolve()):
            fail(f"cannot fingerprint path outside repository {path}")
        entry = os.lstat(path)
        mode = entry.st_mode
        digest_field(
            digest,
            b"mode",
            f"{stat.S_IFMT(mode)}:{mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)}".encode(),
        )
        if stat.S_ISLNK(mode):
            digest_field(digest, b"symlink", os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(mode):
            _digest_regular_file(digest, path, entry)
        else:
            fail(f"cannot fingerprint unsupported file {path}")
    except FileNotFoundError:
        digest_field(digest, b"missing", b"")
    except OSError as exc:
        fail(f"cannot fingerprint file {path}: {exc}")


def _digest_regular_file(digest, path, entry):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        opened = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
            or opened.st_size != entry.st_size
            or opened.st_mtime_ns != entry.st_mtime_ns
            or opened.st_ctime_ns != entry.st_ctime_ns
        ):
            fail(f"cannot fingerprint changed file {path}")
        digest.update(b"file\0" + str(opened.st_size).encode("ascii") + b"\0")
        total = 0
        while total <= opened.st_size:
            chunk = file.read(min(64 * 1024, opened.st_size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > opened.st_size:
                fail(f"cannot fingerprint changed file {path}")
            digest.update(chunk)
        final = os.fstat(file.fileno())
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        fail(f"cannot fingerprint changed file {path}")
    if (
        total != opened.st_size
        or final.st_size != opened.st_size
        or current.st_size != opened.st_size
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or final.st_mtime_ns != opened.st_mtime_ns
        or final.st_ctime_ns != opened.st_ctime_ns
        or current.st_mtime_ns != opened.st_mtime_ns
        or current.st_ctime_ns != opened.st_ctime_ns
    ):
        fail(f"cannot fingerprint changed file {path}")


def digest_field(digest, label, value):
    digest.update(label + b"\0" + str(len(value)).encode("ascii") + b"\0" + value)


def _contains(parent, child):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _root_suffix(path, root):
    inside = False
    for ancestor in (path, *path.parents):
        resolved = ancestor.resolve(strict=False)
        if resolved == root:
            return path.relative_to(ancestor)
        if _contains(root, resolved):
            inside = True
    return None if inside else False


def _git_roots(directory):
    roots = []
    for argument in ("--git-dir", "--git-common-dir"):
        value = Path(git(directory, "rev-parse", argument))
        roots.append(
            (Path(directory) / value).resolve()
            if not value.is_absolute()
            else value.resolve()
        )
    return roots


def safe_manifest_path(path, directory):
    """Validate lexical, physical and Git metadata boundaries before a write."""
    path = Path(os.fspath(path))
    if ".." in path.parts:
        fail("manifest path must not contain '..'")
    path = path if path.is_absolute() else Path.cwd() / path
    lock_path = path.with_name(f".{path.name}.lock")
    root = Path(directory).resolve()
    metadata = _git_roots(directory)
    for candidate in (path, lock_path):
        if candidate.is_symlink():
            fail("manifest and lock must not be symlinks")
        physical_parent = candidate.parent.resolve()
        physical_final = candidate.resolve(strict=False)
        if any(_root_suffix(candidate, item) is not False for item in metadata) or any(
            _contains(item, value)
            for item in metadata
            for value in (candidate, physical_parent, physical_final)
        ):
            fail("manifest and lock must be outside Git metadata")
        suffix = _root_suffix(candidate, root)
        lexical_inside = suffix is not False
        physical_inside = any(
            _contains(root, value) for value in (physical_parent, physical_final)
        )
        if lexical_inside != physical_inside:
            fail("manifest and lock must not use repository aliases")
        if not lexical_inside:
            continue
        if suffix is None:
            fail("manifest and lock must not use repository aliases")
        relative = suffix
        if (
            git_optional(
                directory, "check-ignore", "-q", "--", str(relative)
            ).returncode
            != 0
        ):
            fail(
                "manifest and lock inside repository must be gitignored or stored outside it"
            )
        if (
            git_optional(
                directory, "ls-files", "--error-unmatch", "--", str(relative)
            ).returncode
            == 0
        ):
            fail("manifest and lock inside repository must be untracked")
    return path


def precheck_existing_manifest(path):
    """Reject obvious lexical aliases before reading an existing manifest."""
    if ".." in path.parts:
        fail("manifest path must not contain '..'")
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            fail("manifest and lock must not be symlinks")
    except FileNotFoundError:
        return
    except OSError as exc:
        fail(f"cannot inspect manifest: {exc}")


def task(data, name):
    entry = data["tasks"].setdefault(
        name, {"status": "pending", "fix_cycles": 0, "results": {}, "session_roles": {}}
    )
    entry.setdefault("session_roles", {})
    entry.setdefault("fix_sources", {})
    entry.setdefault("decisions", [])
    entry.setdefault("decision_required_for", None)
    entry.setdefault("deferrals", [])
    entry.setdefault("acceptances", [])
    for role, result in entry.get("results", {}).items():
        entry["session_roles"].setdefault(result["session_id"], role)
    return entry


def session_owners(data):
    """Reconstruct immutable run-wide ownership from retained task history."""
    owners = {}
    for name, entry in data["tasks"].items():
        roles = entry.setdefault("session_roles", {})
        results = entry.get("results", {})
        if not isinstance(roles, dict) or not isinstance(results, dict):
            fail("malformed task session history")
        pairs = list(roles.items())
        for role, result in results.items():
            if not isinstance(result, dict) or not isinstance(
                result.get("session_id"), str
            ):
                fail("malformed task result session history")
            # v1 results predate durable run-wide ownership. Preserve their
            # identities before resume invalidates their stale evidence.
            roles.setdefault(result["session_id"], role)
            pairs.append((result["session_id"], role))
        for session_id, role in pairs:
            if not isinstance(session_id, str) or role not in ROLES:
                fail("malformed task session history")
            owner = (name, role)
            previous = owners.setdefault(session_id, owner)
            if previous != owner:
                fail("session_id has conflicting task or role ownership in manifest")
    return owners


def result_digest(result):
    """Identity of one recorded role result: sha256 of its canonical JSON. A
    deferral names the exact result it covers, so a rerun of the same reviewer
    (even on the same head within the same second) needs its own deferral."""
    raw = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bound_record(records, role, result):
    """The record that names exactly this result of `role` (its digest and head),
    or None. Shared by deferrals and acceptances."""
    if role not in DEFER_SOURCES:
        return None
    if not isinstance(result, dict) or result.get("status") != "findings":
        return None
    digest = result_digest(result)
    for record in records if isinstance(records, list) else ():
        if (
            isinstance(record, dict)
            and record.get("source") == role
            and record.get("head") == result.get("head")
            and record.get("result_sha256") == digest
        ):
            return record
    return None


def is_deferred(entry, role, result):
    """A findings result counts as passed only through a deferral of the same
    role bound to exactly this result (its digest) and its head. The single
    rule for state.py and the wave gate."""
    return _bound_record(entry.get("deferrals") if isinstance(entry, dict) else None, role, result) is not None


def accepted_record(entry, role, result):
    """The `fix-loop --accept` record bound to exactly this result, or None."""
    return _bound_record(entry.get("acceptances") if isinstance(entry, dict) else None, role, result)


def is_accepted(entry, role, result):
    """Findings of any severity accepted by the coordinator (`fix-loop --accept`),
    bound to the exact result and head like a deferral."""
    return accepted_record(entry, role, result) is not None


def is_covered(entry, role, result):
    """A findings result counts as passed when it is deferred or accepted: the
    single readiness rule for effective_status, current_verdicts, where and the gate."""
    return is_deferred(entry, role, result) or is_accepted(entry, role, result)


def effective_status(entry, role):
    result = entry["results"].get(role, {})
    if is_covered(entry, role, result):
        return "pass"
    return result.get("status")


def note_review(entry, role, record):
    """Append a task-review result to the task's `review_history`.

    Kept for high-risk tasks of a version 2 manifest only. The history is append-only: neither
    a later result of the role nor `resume` removes an item. It is what makes a verdict stick:
    what a review said about a HEAD cannot be unsaid by overwriting or invalidating the result."""
    entry.setdefault("review_history", []).append(
        {
            "role": role,
            "profile": record.get("profile"),
            "status": record["status"],
            "head": record["head"],
            "tree_fingerprint": record["tree_fingerprint"],
            "packet_hash": record.get("packet_hash"),
            "result_id": record.get("result_id"),
            "result_sha256": result_digest(record),
            "artifact": record.get("artifact"),
            "recorded_at": record.get("recorded_at"),
        }
    )


def history_cover(entry, item, kinds=("deferrals", "acceptances")):
    """The deferral/acceptance bound to exactly this history item (its result digest and head),
    or None. The binding is the same as for a current result, so a cover outlives the result."""
    for kind in kinds:
        for record in entry.get(kind) or ():
            if (
                isinstance(record, dict)
                and record.get("source") == item["role"]
                and record.get("head") == item["head"]
                and record.get("result_sha256") == item["result_sha256"]
            ):
                return record
    return None


def open_review_findings(entry, head, role=None):
    """History items of this HEAD that are findings nobody deferred or accepted. Matched by
    HEAD alone: findings are about the commit, and touching the working tree does not answer them."""
    return [
        item
        for item in (entry or {}).get("review_history") or ()
        if item["status"] == "findings"
        and item["head"] == head
        and (role is None or item["role"] == role)
        and item["role"] in REVIEW_ROLES
        and history_cover(entry, item) is None
    ]


def fable_review_of(entry, head):
    """The first `review_history` item that is a codex-host (Fable) pass or findings of this
    HEAD, or None. Such an item proves Fable was available for this HEAD: the codex-host-opus
    fallback stands in for a Fable that gave NO review, so it neither replaces nor outlives one.
    The single rule behind task-result (verified_review), readiness (high_risk_gaps) and the
    advice of `where`."""
    for item in (entry or {}).get("review_history") or ():
        if (
            item.get("profile") == FABLE_PROFILE
            and item["status"] in ("pass", "findings")
            and item["head"] == head
        ):
            return item
    return None


OPEN_FINDINGS = "open findings"


def open_findings_reason(role):
    return (
        f"{OPEN_FINDINGS}: {role} reported findings on this HEAD that were neither deferred nor "
        f"accepted; record fix-loop --defer or --accept --source {role} for them, or fix the "
        "code (new HEAD) after fix-loop --outcome failed"
    )


def required_roles(risk):
    """Roles whose pass makes a task PR-ready. A high-risk task needs both task reviews."""
    return ("coder", "tester") + (REVIEW_ROLES if risk == "high" else REVIEW_ROLES[:1])


def high_risk_gaps(entry, results, head):
    """Why the passed results of a high-risk task still do not make it ready: {role: reason}.

    `results` are the task's results on the current head and tree, `head` that HEAD. Findings
    of a review recorded for this HEAD and left open block the task whatever the current result
    of the role is (`review_history`). Otherwise only results that read as
    pass (pass, or findings covered by a deferral/acceptance) are judged here; a missing or
    failed role is the ordinary flow's business. The single rule behind update_task_status,
    is_complete, derive_step and where: coder and tester ran on Fable; each review carries a
    report verified at recording time; the pair is one claude-host and one codex-host
    (codex-host-opus only with quota evidence) over one packet."""
    gaps = {}

    def passed(role):
        result = results.get(role)
        if not isinstance(result, dict):
            return None
        if result.get("status") == "pass" or is_covered(entry, role, result):
            return result
        return None

    for role in MODEL_ROLES:
        result = passed(role)
        if result is not None and result.get("model") != FABLE_MODEL:
            gaps[role] = (
                f"{role} result has model {result.get('model') or 'not recorded'}; a high-risk "
                f"task requires {FABLE_MODEL}: re-run {role} on it, no weaker model"
            )
    reviews = {}
    for role in REVIEW_ROLES:
        if open_review_findings(entry, head, role):
            gaps[role] = open_findings_reason(role)
            continue
        result = passed(role)
        if result is None:
            continue
        profile = result.get("profile")
        if (
            profile not in REVIEW_PROFILES
            or not isinstance(result.get("artifact_sha256"), str)
            or not isinstance(result.get("review_session_id"), str)
        ):
            gaps[role] = (
                f"{role} result carries no verified review.py report; a high-risk task requires "
                "task-result --artifact <report> --reviewed-head --packet-hash"
            )
        elif profile == OPUS_PROFILE and not (
            isinstance(result.get("quota_evidence"), dict)
            and result.get("fallback_for") == FABLE_PROFILE
        ):
            gaps[role] = (
                f"{role} is a {OPUS_PROFILE} review without quota evidence; the fallback counts "
                f"only with --quota-evidence <{FABLE_PROFILE} quota error report>"
            )
        elif profile == OPUS_PROFILE and fable_review_of(entry, head) is not None:
            # recorded before Fable answered (Opus -> Fable -> Astra on one HEAD): the history
            # says Fable reviewed this HEAD, so the earlier fallback counts no more
            gaps[role] = (
                f"{role} is a {OPUS_PROFILE} fallback, but {FABLE_PROFILE} reviewed this HEAD "
                f"({fable_review_of(entry, head)['status']} in review_history): the fallback "
                f"counts only when {FABLE_PROFILE} gave no review; record the {FABLE_PROFILE} "
                f"review as {role}"
            )
        else:
            reviews[role] = result
    if len(reviews) == len(REVIEW_ROLES) and not gaps.keys() & set(REVIEW_ROLES):
        last = REVIEW_ROLES[-1]
        profiles = sorted(result["profile"] for result in reviews.values())
        if any(
            len({result[key] for result in reviews.values()}) != len(reviews)
            for key in ("artifact_sha256", "review_session_id")
        ):
            gaps[last] = (
                "the two results are the same review (one report or one review session); "
                "a high-risk task requires two separate reviews"
            )
        elif profiles not in ([ASTRA_PROFILE, FABLE_PROFILE], [ASTRA_PROFILE, OPUS_PROFILE]):
            gaps[last] = (
                f"the two reviews are {' + '.join(profiles)}; a high-risk task requires one "
                f"{ASTRA_PROFILE} and one {FABLE_PROFILE} review "
                f"({OPUS_PROFILE} only with quota evidence)"
            )
        elif len({result.get("packet_hash") for result in reviews.values()}) != 1:
            gaps[last] = (
                "the two reviews cover different packets; a high-risk task requires both "
                "reviews of one packet_hash"
            )
    return gaps


NEW_RUN_REQUIRED = "new run required"
INTERNAL_COUNTED = ("pass", "findings")


def internal_review_gap(entry, head):
    """Why the internal Fable review of a high-risk task is not counted, or None (policy 1.2.4).

    The credit is the durable task record `internal_review`: the LAST task-result of
    internal_reviewer recorded before the first external packet of the task in this run
    (external_review_started: the marker `external_review`, behind it the session history);
    `resume` keeps both. Counted: status pass or findings, model Fable, and its HEAD is the
    HEAD of the first external packet (`external_review.head`), so the internal review read
    exactly the diff that went out. Before the packet the requirement is open: the record must be of the current `head`,
    the one the packet will be built from. After the packet a missing credit cannot be earned in
    this run: the reason starts with NEW_RUN_REQUIRED."""
    record = entry.get("internal_review")
    record = record if isinstance(record, dict) else {}
    status = record.get("status")
    model = record.get("model")
    recorded = f"task-result --role {INTERNAL_SOURCE} --status <pass|findings> --model fable"
    # one rule of «the packet already went out» (external_review_started): the marker, and
    # behind it the session history of the task — a removed marker reopens nothing
    started = external_review_started(entry)
    if started is None:
        if status in INTERNAL_COUNTED and record.get("head") == head and model == FABLE_MODEL:
            return None
        if status is None:
            what = f"no {INTERNAL_SOURCE} result is recorded"
        elif record.get("head") != head:
            what = f"the {INTERNAL_SOURCE} result is of another HEAD ({str(record.get('head'))[:12]})"
        elif status not in INTERNAL_COUNTED:
            what = f"the last {INTERNAL_SOURCE} result is {status}"
        else:
            what = f"the {INTERNAL_SOURCE} record carries model {model or 'not recorded'}"
        return (
            f"{what}; a high-risk task requires the internal Fable review ({FABLE_MODEL}) of this "
            f"HEAD before the first external review packet: record {recorded} (Fable unavailable: "
            "retry or escalate to the owner, no weaker model, and do not start the external review)"
        )
    marker = entry.get("external_review")
    packet = marker.get("head") if isinstance(marker, dict) else None
    if not isinstance(packet, str):
        # the packet went out (session history) but the marker that binds the credit to its HEAD
        # is gone or has no HEAD: nothing to count the credit against
        what = (
            f"the first external packet of the task is known ({started}), but the marker "
            "external_review that names its HEAD is missing or has no head"
        )
    elif status in INTERNAL_COUNTED and record.get("head") == packet and model == FABLE_MODEL:
        return None
    elif status is None:
        what = f"no {INTERNAL_SOURCE} result was recorded before the first external packet"
    elif status not in INTERNAL_COUNTED:
        what = f"the last {INTERNAL_SOURCE} result before the first external packet is {status}"
    elif record.get("head") != packet:
        what = (
            f"the {INTERNAL_SOURCE} result is of HEAD {str(record.get('head'))[:12]}, the first "
            f"external packet of HEAD {packet[:12]}"
        )
    else:
        what = (
            f"the {INTERNAL_SOURCE} record carries model {model or 'not recorded'}, "
            f"required {FABLE_MODEL}"
        )
    return (
        f"{NEW_RUN_REQUIRED}: {what} ({started}); the internal review counts only "
        "before the first external packet of the task and on its HEAD, so this run cannot make "
        "the task ready: start a new run of the task (state.py init on a new manifest path; in "
        "a wave: state.py init --from-plan, a new run of the wave)"
    )


def final_check_gap(entry, results):
    """Why the final check of a high-risk task is not counted, or None (policy 1.2.4).

    `results` are the task's results on the current head and tree. Counted: a current
    final_check with model Fable and status pass recorded after the current results of both
    task reviews:
    `after_reviews` of the record names the result_id of each review it was recorded after, so
    a review recorded later (or again) asks for a new final check. The PR reviews
    (github_codex_review, coderabbit) are no part of the comparison."""
    recorded = "task-result --role final_check --status pass --model fable"
    result = results.get("final_check")
    if not isinstance(result, dict):
        return (
            "no final_check result on this HEAD; a high-risk task requires the final Fable "
            f"check after the last task review: record {recorded}"
        )
    status = result.get("status")
    if result.get("model") != FABLE_MODEL:
        return (
            f"final_check result has model {result.get('model') or 'not recorded'}; a high-risk "
            f"task requires the final check by {FABLE_MODEL}: record {recorded}"
        )
    if status != "pass":
        if status == "findings":
            advice = (
                "return the gaps to work (fix-loop --outcome failed under an ordinary source, "
                f"new HEAD), then record {recorded}"
            )
        elif status == "incomplete":
            advice = f"re-run the final check with the missing context and record {recorded}"
        else:
            advice = (
                "retry once explicitly or escalate to the owner, no substitution by a weaker "
                f"model; then record {recorded}"
            )
        return f"final_check is {status} on this HEAD, a high-risk task requires pass: {advice}"
    seen = result.get("after_reviews")
    seen = seen if isinstance(seen, dict) else {}
    late = [
        role
        for role in REVIEW_ROLES
        if not isinstance(results.get(role), dict)
        or results[role].get("result_id") is None
        or seen.get(role) != results[role].get("result_id")
    ]
    if late:
        return (
            f"final_check was recorded before the current {' and '.join(late)} result of this "
            "HEAD; it counts only after the last task review: run the final check again and "
            f"record {recorded}"
        )
    return None


def fable_role_gaps(entry, results, head, policy):
    """{role: reason} for the mandatory Fable roles of a HIGH-risk task that are not counted.

    Empty under a policy that does not make them mandatory (1.2.1) and for manifest version 1
    (policy None). The single rule behind task_ready, derive_step, where and the merge gate;
    the caller decides that the task is judged as high."""
    if policy not in MANDATORY_ROLES_POLICIES:
        return {}
    gaps = {}
    reason = internal_review_gap(entry, head)
    if reason is not None:
        gaps["internal_reviewer"] = reason
    reason = final_check_gap(entry, results)
    if reason is not None:
        gaps["final_check"] = reason
    return gaps


def task_ready(entry, risk, head, policy):
    """The single readiness rule of a task for its effective risk (None: manifest version 1).
    `risk` and `policy` have no default here or in any caller that recomputes a status: a
    forgotten argument must be an error, never a silent return to older rules. Callers take them
    from `task_risk(data, entry)` and `policy_version(data)`; `head` is the manifest HEAD the
    entry's results belong to."""
    if not all(effective_status(entry, role) == "pass" for role in required_roles(risk)):
        return False
    if risk != "high":
        return True
    return not high_risk_gaps(entry, entry["results"], head) and not fable_role_gaps(
        entry, entry["results"], head, policy
    )


def update_task_status(entry, risk, head, policy):
    """Only coder, tester and the task review(s) can make a task PR-ready.
    Coder and tester need pass; a reviewer may also be findings + deferral/acceptance.
    A high-risk task (manifest version 2) also needs Fable models and the second review, and
    under the policy 1.2.4 the internal review and the final check (fable_role_gaps)."""
    if entry["status"] == "blocked":
        return
    if task_ready(entry, risk, head, policy):
        entry["status"] = "ready_for_pr_review"
    elif entry["results"]:
        entry["status"] = "in_progress"


def settle_mandatory_role(entry, risk, head, policy):
    """After a result of internal_reviewer or final_check under the policy 1.2.4: it may complete
    the task or take its readiness away; any other status (needs_fix, needs_verification,
    pending) is not its business."""
    if entry["status"] in ("blocked", "needs_decision"):
        return
    if task_ready(entry, risk, head, policy):
        entry["status"] = "ready_for_pr_review"
    elif entry["status"] == "ready_for_pr_review":
        entry["status"] = "in_progress"


def invalidate(data, head, tree):
    if data["head"] == head and data["tree_fingerprint"] == tree:
        return []
    invalidated = []
    for name, entry in data["tasks"].items():
        stale = [
            role
            for role, result in entry["results"].items()
            if result["head"] != head or result["tree_fingerprint"] != tree
        ]
        for role in stale:
            del entry["results"][role]
        if stale and entry["status"] not in ("blocked", "needs_decision"):
            entry["status"] = "pending"
            invalidated.append(name)
    data["head"] = head
    data["tree_fingerprint"] = tree
    data["updated_at"] = now()
    return invalidated


class PlanError(Exception):
    """waves.json is unreadable or violates the v1 schema."""


class PlanMissing(PlanError):
    """waves.json does not exist."""


def plan_pairs(pairs):
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise PlanError("duplicate JSON key in plan")
    return dict(pairs)


def valid_name(value):
    return (
        isinstance(value, str)
        and NAME_PATTERN.fullmatch(value) is not None
        and value not in (".", "..")
    )


def plan_path(value):
    """Absolute path with parent directories resolved; the final name is kept."""
    path = Path(value)
    if not path.name or path.name in (".", ".."):
        raise PlanError("plan path must name a file")
    return path.parent.resolve() / path.name


def read_plan_bytes(path):
    """Read a plan without following a final symlink; regular file, <= 1 MiB."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise PlanError("platform does not support safe plan opening")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        try:
            os.lstat(path)
        except FileNotFoundError:
            raise PlanMissing(f"plan does not exist: {path}")
        except OSError:
            pass
        raise PlanError(f"cannot open plan: {path}")
    except OSError as exc:
        raise PlanError(f"cannot open plan (symlink or unreadable): {exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PlanError("plan must be a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as file:
        raw = file.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanError("plan exceeds 1 MiB")
    return raw


def is_text(value):
    return isinstance(value, str) and value != ""


def validate_wave(item, index, seen):
    where = f"waves[{index}]"
    if not isinstance(item, dict) or set(item) != WAVE_KEYS:
        raise PlanError(f"{where} must be an object with exactly {sorted(WAVE_KEYS)}")
    if not valid_name(item["id"]):
        raise PlanError(f"{where}.id is invalid")
    if item["id"] in seen:
        raise PlanError(f"{where}.id is duplicated")
    for key in ("title", "goal", "requirements"):
        if not is_text(item[key]):
            raise PlanError(f"{where}.{key} must be a non-empty string")
    acceptance = item["acceptance"]
    if (
        not isinstance(acceptance, list)
        or not acceptance
        or not all(is_text(entry) for entry in acceptance)
    ):
        raise PlanError(f"{where}.acceptance must be a non-empty list of strings")
    if not isinstance(item["risk"], str) or item["risk"] not in RISKS:
        raise PlanError(f"{where}.risk must be one of {sorted(RISKS)}")
    checks = item["checks"]
    if not isinstance(checks, list):
        raise PlanError(f"{where}.checks must be a list")
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"name", "cmd"}
            or not is_text(check["name"])
            or not is_text(check["cmd"])
        ):
            raise PlanError(f"{where}.checks entries need exactly name and cmd strings")
    depends = item["depends_on"]
    if not isinstance(depends, list) or not all(is_text(d) for d in depends):
        raise PlanError(f"{where}.depends_on must be a list of wave ids")
    for dependency in depends:
        if dependency not in seen:
            raise PlanError(
                f"{where}.depends_on {dependency!r} must reference an earlier wave"
            )


def check_encodable(value):
    """Every string, including object keys, must encode as UTF-8."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise PlanError("plan contains a string that is not valid UTF-8")
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())


def parse_plan(raw):
    try:
        text = raw.decode("utf-8")
        doc = json.loads(text, object_pairs_hook=plan_pairs)
    except PlanError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PlanError(f"plan is not valid UTF-8 JSON: {exc}")
    check_encodable(doc)
    if not isinstance(doc, dict) or set(doc) != PLAN_KEYS:
        raise PlanError(f"plan must be an object with exactly {sorted(PLAN_KEYS)}")
    if type(doc["version"]) is not int or doc["version"] != 1:
        raise PlanError("plan version must be 1")
    for key in ("chain", "repo", "base_branch"):
        if not is_text(doc[key]):
            raise PlanError(f"plan {key} must be a non-empty string")
    if REPO_PATTERN.fullmatch(doc["repo"]) is None:
        raise PlanError("plan repo must be owner/name")
    if not isinstance(doc["waves"], list) or not doc["waves"]:
        raise PlanError("plan waves must be a non-empty list")
    seen = set()
    for index, item in enumerate(doc["waves"]):
        validate_wave(item, index, seen)
        seen.add(item["id"])
    return doc


def canonical_sha256(value):
    blob = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_plan(selector):
    """Return (plan metadata, wave copy) for `<path>#<wave-id>` or fail."""
    location, separator, wave_id = selector.rpartition("#")
    if not separator or not location or not wave_id:
        fail("--from-plan must be <path>#<wave-id>")
    try:
        path = plan_path(location)
        raw = read_plan_bytes(path)
        doc = parse_plan(raw)
    except PlanError as exc:
        fail(f"invalid plan: {exc}")
    selected = next((item for item in doc["waves"] if item["id"] == wave_id), None)
    if selected is None:
        fail(f"wave {wave_id!r} not found in plan")
    digest = hashlib.sha256(raw).hexdigest()
    return (
        {
            "path": str(path),
            "sha256": digest,
            "wave": wave_id,
            "approved_by": f"plan@{digest}",
            "wave_sha256": canonical_sha256(selected),
        },
        selected,
    )


def plan_check(data):
    plan = data.get("plan")
    if plan is None:
        return None
    try:
        doc = parse_plan(read_plan_bytes(plan["path"]))
        for item in doc["waves"]:
            if item["id"] == plan["wave"]:
                same = canonical_sha256(item) == plan["wave_sha256"]
                return "match" if same else "changed"
        return "changed"
    except PlanMissing:
        return "missing"
    except Exception:
        # Whatever is wrong with the plan file, where must report it, not crash.
        return "changed"


def parse_max_runs(value, origin):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        fail(f"{origin} must be a positive integer, got {value!r}")
    # length first: int() of a string past sys.int_info.str_digits_check_threshold raises ValueError
    if len(value) > len(str(MAX_RUNS_LIMIT)):
        fail(f"{origin} must be at most {MAX_RUNS_LIMIT}")
    number = int(value)
    if number > MAX_RUNS_LIMIT:
        fail(f"{origin} must be at most {MAX_RUNS_LIMIT}")
    return number


def run_counter_settings(args):
    """(runs_file, max_runs) when the wave run counter applies, else None. The cap comes from
    --max-runs, else $WAB_DIR/max-runs (the live value), else $WAB_MAX_RUNS, else 2. It applies
    only to `init --from-plan` and only when --runs-file or $WAB_DIR names where the
    counter lives; an ordinary /superarmanda run is never counted."""
    explicit = args.max_runs is not None or args.runs_file is not None
    if args.from_plan is None:
        if explicit:
            fail("--max-runs and --runs-file require --from-plan")
        return None
    directory = os.environ.get("WAB_DIR")
    if args.runs_file is not None:
        runs_file = Path(args.runs_file)
    elif directory:
        runs_file = Path(directory) / "runs.json"
    else:
        if args.max_runs is not None:
            fail("--max-runs requires --runs-file or WAB_DIR")
        return None
    limit_file = Path(directory) / "max-runs" if directory else None
    if args.max_runs is not None:
        limit = parse_max_runs(args.max_runs, "--max-runs")
    elif limit_file is not None and limit_file.exists():
        # The dispatcher keeps the live cap here (chain.json max_runs can be raised while the
        # wave runs; the env of an already started session cannot follow it).
        try:
            raw = limit_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"{limit_file} is unreadable ({exc}); fix or remove it")
        limit = parse_max_runs(raw.rstrip("\n"), str(limit_file))
    elif "WAB_MAX_RUNS" in os.environ:
        limit = parse_max_runs(os.environ["WAB_MAX_RUNS"], "WAB_MAX_RUNS")
    else:
        limit = DEFAULT_MAX_RUNS
    return runs_file, limit


def live_run_cap(manifest_cap):
    """The cap `where` judges the run by. The dispatcher can change $WAB_DIR/max-runs while
    the wave runs (the same source `init --from-plan` reads), so the manifest's frozen
    run.max would call the last run wrongly after a raise or a cut. `where` is read-only and
    must keep answering: a missing, unreadable or invalid file (not a positive integer within
    MAX_RUNS_LIMIT) falls back to the manifest value, it is not an error."""
    directory = os.environ.get("WAB_DIR")
    if not directory:
        return manifest_cap
    try:
        raw = (Path(directory) / "max-runs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return manifest_cap
    raw = raw.rstrip("\n")
    if (re.fullmatch(r"[1-9][0-9]*", raw) is None or len(raw) > len(str(MAX_RUNS_LIMIT))
            or int(raw) > MAX_RUNS_LIMIT):
        return manifest_cap
    return int(raw)


def read_runs(path, wave_id):
    """The wave's recorded runs; a missing file is an empty record, a damaged one
    or one of another wave is a closed refusal."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"cannot read runs file {path}: {exc}")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        fail(f"runs file {path} is not valid JSON: {exc}")
    if (
        not isinstance(value, dict)
        or value.get("version") != RUNS_FILE_VERSION
        or not isinstance(value.get("wave"), str)
        or not isinstance(value.get("runs"), list)
        or not all(isinstance(item, dict) for item in value["runs"])
    ):
        fail(f"runs file {path} has an unsupported format")
    if value["wave"] != wave_id:
        fail(f"runs file {path} belongs to wave {value['wave']}, not {wave_id}")
    return value["runs"]


def init(args):
    path = safe_manifest_path(Path(args.manifest), repo(args.repo))
    if path.exists():
        fail("manifest already exists")
    location = repo(args.repo)
    if args.expect_sha256 is not None:
        if args.from_plan is None:
            fail("--expect-sha256 requires --from-plan")
        if not re.fullmatch(r"[0-9a-f]{64}", args.expect_sha256):
            fail("--expect-sha256 must be 64 lowercase hex characters")
    if args.risk is not None and args.from_plan is not None:
        fail("--risk is not allowed with --from-plan: the wave's risk is the policy level")
    counter = run_counter_settings(args)
    base, head = validate_revision_pair(location, args.base, args.head)
    plan = wave_copy = None
    level = args.risk or "low"
    if args.from_plan is not None:
        plan, wave_copy = load_plan(args.from_plan)
        # the wave's risk is the floor for every task of the run, the model of its roles included
        level = wave_copy["risk"]
        if args.expect_sha256 is not None and plan["sha256"] != args.expect_sha256:
            fail(
                f"plan sha256 {plan['sha256']} != expected {args.expect_sha256}: "
                "plan changed since approval"
            )
    data = {
        "version": 2,
        "review_policy": {"version": POLICY_VERSION, "level": level},
        "run_id": args.run_id,
        "repo": location,
        "base": base,
        "head": head,
        "tree_fingerprint": fingerprint(location),
        "tasks": {},
        "created_at": now(),
        "updated_at": now(),
    }
    if plan is not None:
        data["plan"] = plan
        data["wave"] = wave_copy
    if counter is None:
        write(path, data)
        print(json.dumps(data, sort_keys=True))
        return
    runs_file, limit = counter
    with lock(runs_file):
        runs = read_runs(runs_file, plan["wave"])
        if len(runs) >= limit:
            listed = ", ".join(str(item.get("manifest")) for item in runs)
            fail(
                f"run cap reached for wave {plan['wave']}: {len(runs)}/{limit} runs "
                f"(manifests: {listed}); write BLOCKED: [class=blocked_cap rec=owner "
                "red=no] the wave used all its permitted runs; the owner decides "
                "whether to raise the cap or accept the result"
            )
        data["run"] = {"index": len(runs) + 1, "max": limit}
        record = {
            "index": data["run"]["index"],
            "manifest": str(path),
            "created_at": now(),
            "plan_sha256": plan["sha256"],
        }
        # Fail-safe order: the counter is written FIRST (a reservation), the manifest second.
        # A kill (SIGKILL, power loss) between the writes leaves a counted run without a
        # manifest, which only lowers the remaining budget; the reverse order would leave an
        # uncounted manifest and let the next init exceed the cap.
        write(
            runs_file,
            {
                "version": RUNS_FILE_VERSION,
                "wave": plan["wave"],
                "runs": runs + [record],
            },
        )
        try:
            write(path, data)
        except BaseException:
            # an ordinary failure: give the reservation back (atomically) and re-raise
            if runs:
                write(
                    runs_file,
                    {"version": RUNS_FILE_VERSION, "wave": plan["wave"], "runs": runs},
                )
            else:
                runs_file.unlink(missing_ok=True)
            raise
    print(json.dumps(data, sort_keys=True))


def status(args):
    data = read(Path(args.manifest))
    location = repo(data["repo"])
    current_head = commit(location, "HEAD")
    data["tree_matches"] = (
        data["repo"] == location
        and current_head == data["head"]
        and fingerprint(location) == data["tree_fingerprint"]
    )
    if not data["tree_matches"]:
        for entry in data["tasks"].values():
            if entry.get("status") not in ("blocked", "needs_decision"):
                entry["status"] = "pending"
    print(json.dumps(data, sort_keys=True))


def resume(args):
    path = Path(args.manifest)
    data = read(path)
    location = repo(args.repo)
    base, head = validate_revision_pair(location, args.base, args.head)
    if repo(data["repo"]) != location or data["base"] != base:
        fail("repo or base does not match manifest")
    # Preserve old session_roles while evidence is invalidated, but make all
    # future state operations use the worktree root rather than a subdirectory.
    data["repo"] = location
    invalidated = invalidate(data, head, fingerprint(location))
    write(path, data)
    print(json.dumps({"head": head, "invalidated_tasks": invalidated}, sort_keys=True))


def result(args):
    if args.role not in ROLES or args.status not in STATUSES:
        fail("invalid role or status")
    path = Path(args.manifest)
    data = read(path)
    location = repo(data["repo"])
    if data["repo"] != location:
        fail("manifest repository is not canonical; run resume first")
    actual_head = commit(location, "HEAD")
    if args.head != data["head"] or actual_head != data["head"]:
        fail("result head differs from manifest; run resume first")
    if fingerprint(location) != data["tree_fingerprint"]:
        fail("working tree changed; run resume before recording a result")
    if args.role in V2_ONLY_ROLES:
        require_v2(data, f"role {args.role}")
    if args.model is not None:
        require_v2(data, "--model")
    if args.quota_evidence is not None:
        require_v2(data, "--quota-evidence")
    entry = task(data, args.task)
    risk = task_risk(data, entry)
    policy = policy_version(data)
    if entry["status"] == "blocked":
        fail("task is blocked after three fix cycles; explicit human reset is required")
    if entry["status"] == "needs_decision":
        fail(
            "decision required for source "
            f"{entry['decision_required_for']}: run fix-loop --decision ..."
        )
    model = result_model(args, risk)
    if risk == "high" and args.role in REVIEW_ROLES and open_review_findings(
        entry, data["head"], args.role
    ):
        # Nothing may be recorded over findings nobody disposed of: not a pass of another
        # report, not `unavailable`, and not after `resume` has dropped the result itself.
        # After a fix the HEAD is new, and this HEAD's history no longer applies.
        fail(open_findings_reason(args.role))
    owner = session_owners(data).get(args.session_id)
    if owner is not None and owner != (args.task, args.role):
        fail("session_id is already used by another task or role for this run")
    if args.reviewed_head is not None and args.reviewed_head != args.head:
        fail("reviewed_head differs from result head")
    packet_hash = args.packet_hash
    if packet_hash is not None:
        # review.py пишет голый 64-hex, в манифесте хранится только sha256:<hex>
        digest = packet_hash.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            fail("packet_hash must be sha256:<64 lowercase hex characters>")
        packet_hash = "sha256:" + digest
    if args.role == "cross_provider_reviewer" and args.status == "pass":
        if args.reviewed_head is None or packet_hash is None:
            fail("cross-provider pass requires reviewed_head and packet_hash")
    if args.role == "github_codex_review" and args.status == "pass":
        if args.reviewed_head is None or not (args.artifact or "").startswith(
            "https://"
        ):
            fail("GitHub Codex pass requires reviewed_head and HTTPS artifact URL")
        if packet_hash is not None:
            fail("GitHub Codex review does not accept packet_hash")
    verified = verified_review(args, risk, packet_hash, entry)
    if args.role == INTERNAL_SOURCE:
        refuse_external_report(args.artifact, "--artifact")
    entry["session_roles"][args.session_id] = args.role
    record = {
        "status": args.status,
        "head": args.head,
        "session_id": args.session_id,
        "artifact": args.artifact,
        "reviewed_head": args.reviewed_head,
        "packet_hash": packet_hash,
        "tree_fingerprint": data["tree_fingerprint"],
        "recorded_at": now(),
        # unique per recorded result: a byte-identical rerun (same session, same second)
        # is still another result, and a deferral covers exactly one (#36 п.3)
        "result_id": uuid.uuid4().hex,
    }
    # Policy fields exist only where the policy produced them: a version 1 result and an
    # unverified version 2 result keep the shape of 1.2.0.
    if model is not None:
        record["model"] = model
    record.update(verified)
    mandatory = policy in MANDATORY_ROLES_POLICIES and args.role in MANDATORY_FABLE_ROLES
    if mandatory and args.role == "final_check":
        # which results of the task reviews this check was recorded after (final_check_gap)
        record["after_reviews"] = {
            role: entry["results"][role]["result_id"]
            for role in REVIEW_ROLES
            if isinstance(entry["results"].get(role), dict)
            and entry["results"][role].get("result_id") is not None
        }
    entry["results"][args.role] = record
    if mandatory and args.role == INTERNAL_SOURCE and external_review_started(entry) is None:
        # the credit of the internal review (internal_review_gap): the last record before the
        # first external packet; kept through resume, at any risk (a task may be raised to high)
        entry["internal_review"] = {
            "status": args.status,
            "head": args.head,
            "model": model,
            "recorded_at": record["recorded_at"],
        }
    if risk == "high" and args.role in REVIEW_ROLES:
        note_review(entry, args.role, record)
    if data["version"] == 2 and args.role in EXTERNAL_REVIEW_ROLES:
        # the first external packet of the task in this run: kept through resume and new HEADs
        entry.setdefault(
            "external_review",
            {"role": args.role, "head": args.head, "recorded_at": record["recorded_at"]},
        )
    if args.role not in FABLE_ROLES:
        update_task_status(entry, risk, data["head"], policy)
    elif mandatory:
        settle_mandatory_role(entry, risk, data["head"], policy)
    # else: a result of a Fable subagent role is a record: the task status is not its business
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(entry, sort_keys=True))


def result_model(args, risk):
    """Canonical model ID for the `model` field of a result, or None.
    A high-risk task requires Fable for coder and tester at ANY status: `unavailable` with
    `--model fable` is the record "Fable was requested and is unavailable"; nothing weaker may
    take the role. The Fable subagent roles require it at any status and any risk."""
    if args.model is not None and args.role not in MODEL_ROLES + FABLE_ROLES:
        fail(f"--model is only allowed for roles {', '.join(MODEL_ROLES + FABLE_ROLES)}")
    model = MODEL_ALIASES.get(args.model) if args.model is not None else None
    if args.role in FABLE_ROLES and model != FABLE_MODEL:
        given = "no --model" if args.model is None else repr(args.model)
        fail(
            f"role {args.role} runs only on Fable: --model {FABLE_MODEL} (or fable) is required "
            f"at any status and any risk (got {given}); no substitution by a weaker or another "
            f"model. If Fable is unavailable, record task-result --role {args.role} --status "
            "unavailable --model fable: the field `model` then says Fable was requested and "
            "did not answer"
        )
    if risk == "high" and args.role in MODEL_ROLES and model != FABLE_MODEL:
        given = "no --model" if args.model is None else repr(args.model)
        fail(
            f"high-risk task requires --model {FABLE_MODEL} for {args.role} (got {given}); "
            "no substitution by a weaker or another model. If this host cannot run "
            f"{args.role} on Fable, record --status unavailable --model fable: the task is "
            "then not pass, as the policy intends"
        )
    if args.model is not None and model is None:
        fail(
            f"unknown --model {args.model!r}: must be exactly one of "
            f"{', '.join(sorted(MODEL_ALIASES))}"
        )
    return model


def read_report(location, label):
    """(parsed object, sha256 of the bytes) of a local review.py report. A regular file, not a
    symlink, at most 1 MiB, one JSON object without duplicate keys; anything else is refused."""
    if not isinstance(location, str) or not location:
        fail(f"{label} must be a path to a review.py report")
    if not hasattr(os, "O_NOFOLLOW"):
        fail("platform does not support safe report opening")
    try:
        fd = os.open(location, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError) as exc:
        fail(f"cannot open {label} (missing, symlink or unreadable): {exc}")
    raw = read_regular(fd, label)
    if raw is None:
        fail(f"{label} must be a regular file")
    if len(raw) > MAX_REPORT_BYTES:
        fail(f"{label} exceeds {MAX_REPORT_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=plan_pairs)
    except (PlanError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        fail(f"{label} is not a valid JSON report: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def read_regular(fd, label):
    """At most MAX_REPORT_BYTES + 1 bytes of the regular file behind `fd`, or None when it is
    not a regular file (a directory, a FIFO). Always closes `fd`; a read error is a refusal,
    never a traceback (a directory opens with O_RDONLY and fails only when read)."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as file:
            return file.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        fail(f"cannot read {label}: {exc}")


def refuse_external_report(location, label):
    """Refuse an artifact of the internal review that is a review.py report of an external
    profile: an external finding cannot be recorded under the internal source, which spends no
    fix cycle. Symlinks are followed (the question is what the path gives, not what it is).
    Anything that is not a readable local JSON object (notes in Markdown, a URL, a missing
    path, a directory) is not a report and is left alone; a file too large to check is refused.
    A guard against a careless or convenient relabelling, not a proof of origin: like the
    manifest, the artifact is not signed."""
    if not isinstance(location, str) or not location:
        return
    try:
        fd = os.open(location, os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError):
        return
    raw = read_regular(fd, label)
    if raw is None:
        return
    if len(raw) > MAX_REPORT_BYTES:
        fail(f"{label} exceeds {MAX_REPORT_BYTES} bytes: it cannot be checked for an external review report")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return
    profile = value.get("profile") if isinstance(value, dict) else None
    if isinstance(profile, str) and profile in REVIEW_PROFILES:
        fail(
            f"{label} is an external review report (review.py profile {profile}): its findings "
            f"are not findings of {INTERNAL_SOURCE}; record them as task-result of the external "
            "review role and fix-loop --outcome failed --source <that role>"
        )


def review_profile_models():
    """`PROFILE_MODELS` of the sibling review.py: profile -> (CLI, requested model, observed
    model). Read from the runner itself, so the two scripts cannot disagree; loaded only when a
    report is verified. An unloadable runner is a refusal, not a skipped check."""
    path = Path(__file__).resolve().parent / "review.py"
    try:
        spec = importlib.util.spec_from_file_location("superarmanda_review_profiles", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        table = module.PROFILE_MODELS
    except Exception as exc:
        fail(f"cannot read review profiles from {path.name}: {exc}")
    if not isinstance(table, dict) or set(table) != REVIEW_PROFILES:
        fail("review.py profiles differ from the profiles state.py knows")
    return table


def is_quota_report(evidence):
    """A review.py error report of codex-host (Fable) whose failure is the provider quota."""
    return (
        isinstance(evidence, dict)
        and evidence.get("profile") == FABLE_PROFILE
        and evidence.get("status") == "error"
        and evidence.get("error_category") == "quota"
        and evidence.get("gate_ready") is False
    )


def quota_evidence_problem(evidence, head, packet_hash):
    """Why `evidence` does not open the codex-host-opus fallback for this HEAD and packet
    (None: it does). `head` None skips the HEAD (a plan review has no manifest HEAD: the packet
    binds it). The single rule for task-result --quota-evidence and the plan review of
    `wab.py launch`."""
    if not is_quota_report(evidence):
        return (
            f"must be a {FABLE_PROFILE} review.py report with status error, "
            "error_category quota and gate_ready false"
        )
    if (head is not None and evidence.get("reviewed_head") != head) or evidence.get(
        "state_packet_hash"
    ) != packet_hash:
        return "is not for this HEAD and packet (or predates review.py 1.2.1)"
    return None


def report_models_problem(report, profile):
    """Why a review.py report does not agree with the profile it names (None: it does): the
    model review.py requests for the profile and the model it observed running. One edited
    `profile` field does not make another review.

    review.py writes `observed_models` as the adapter's list for Astra (exactly the one model)
    and as the CLI's modelUsage object for the Claude profiles, where entries of ancillary
    CLI calls beside the primary model are legitimate. So the object is not compared as a
    whole; the primary model must be there (and `primary_model_verified` is checked by the
    caller)."""
    cli, requested, observed = review_profile_models()[profile]
    models = report.get("observed_models")
    if cli == "claude":
        models_fit = isinstance(models, dict) and isinstance(models.get(observed), dict)
    else:
        models_fit = models == [observed]
    if report.get("requested_model") != requested or not models_fit:
        return f"review report models do not belong to profile {profile}"
    return None


def report_verified(report):
    """The report says its primary model was verified and its tools were isolated."""
    capabilities = report.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    isolation = capabilities.get("tool_isolation")
    return (
        capabilities.get("primary_model_verified") is True
        and isinstance(isolation, str)
        and isolation != "unverified"
    )


def verified_review(args, risk, packet_hash, entry):
    """Policy fields of a task-review result, taken from its review.py report.

    The report is checked for shape, for agreement with its own profile and for its binding to
    this HEAD and packet. That stops a wrong or carelessly relabelled file; a report is not
    signed, so its origin is not proven (the same trust as the manifest itself).

    For a high-risk task a `pass` or `findings` of either task review is recorded only with a
    report that state.py reads and checks itself: the coordinator's word is not enough. Other
    statuses (error, unavailable, incomplete) need no report and never pass. Below high the
    report is not dereferenced, as in 1.2.0."""
    checked = risk == "high" and args.role in REVIEW_ROLES and args.status in ("pass", "findings")
    if not checked:
        if args.quota_evidence is not None:
            fail(
                "--quota-evidence is only allowed with a pass or findings of a high-risk task's "
                f"review recorded from a {OPUS_PROFILE} report"
            )
        return {}
    if args.artifact is None or args.reviewed_head is None or packet_hash is None:
        fail(
            f"high-risk {args.role} {args.status} requires --artifact <review.py report>, "
            "--reviewed-head and --packet-hash"
        )
    report, digest = read_report(args.artifact, "--artifact")
    profile = report.get("profile")
    if not isinstance(profile, str) or profile not in REVIEW_PROFILES:
        fail(f"review report profile must be one of {sorted(REVIEW_PROFILES)}")
    if report.get("status") != args.status:
        fail(f"review report status {report.get('status')!r} differs from --status {args.status}")
    # The report must agree with itself (report_models_problem).
    problem = report_models_problem(report, profile)
    if problem:
        fail(problem)
    session = report.get("session_id")
    if not isinstance(session, str) or not session:
        fail("review report has no review session_id")
    for other in REVIEW_ROLES:
        previous = entry["results"].get(other)
        if other != args.role and isinstance(previous, dict) and (
            previous.get("artifact_sha256") == digest
            or previous.get("review_session_id") == session
        ):
            fail(
                f"this report is the review already recorded as {other}: one review cannot "
                "fill both review roles"
            )
    if args.status == "pass" and report.get("gate_ready") is not True:
        fail("review report is not gate_ready: it cannot be recorded as pass")
    if not report_verified(report):
        fail(
            f"review report has no verified model and tool isolation: {args.status} is not recorded"
        )
    if report.get("state_packet_hash") != packet_hash:
        fail("review report state_packet_hash differs from --packet-hash")
    response = report.get("response")
    if not isinstance(response, dict) or response.get("reviewed_head") != args.head:
        fail("review report reviewed_head differs from the result head")
    fields = {"profile": profile, "artifact_sha256": digest, "review_session_id": session}
    if profile != OPUS_PROFILE:
        if args.quota_evidence is not None:
            fail(f"--quota-evidence is only allowed with a {OPUS_PROFILE} report, got {profile}")
        return fields
    # The fallback stands in for a Fable that gave NO review of this HEAD. A Fable pass or
    # findings in the history (either role, covered or not, overwritten since or not) proves
    # Fable was available: an older quota report must not let Opus replace or outvote it.
    item = fable_review_of(entry, args.head)
    if item is not None:
        fail(
            f"{item['role']} recorded a {FABLE_PROFILE} review ({item['status']}) of this "
            f"HEAD: {OPUS_PROFILE} is a fallback only when {FABLE_PROFILE} gave no review"
        )
    if args.quota_evidence is None:
        fail(
            f"a {OPUS_PROFILE} review is accepted only with --quota-evidence "
            f"<{FABLE_PROFILE} quota error report for this HEAD and packet>"
        )
    evidence, evidence_digest = read_report(args.quota_evidence, "--quota-evidence")
    problem = quota_evidence_problem(evidence, args.head, packet_hash)
    if problem:
        fail(f"--quota-evidence {problem}")
    fields["fallback_for"] = FABLE_PROFILE
    fields["quota_evidence"] = {"artifact": args.quota_evidence, "sha256": evidence_digest}
    return fields


def set_risk(args):
    """task-risk: raise (or repeat) the task's own risk; lowering it is refused."""
    if not valid_name(args.task):
        fail("invalid task id")
    path = Path(args.manifest)
    data = read(path)
    require_v2(data, "task-risk")
    entry = task(data, args.task)
    own = entry.get("risk")
    if own is not None and RISK_ORDER.index(args.risk) < RISK_ORDER.index(own):
        fail(f"task risk can only be raised: refusing to lower {own} to {args.risk}")
    entry["risk"] = args.risk
    risk = task_risk(data, entry)
    if risk == "high":
        # the review results recorded below high enter the history now: from here on they
        # cannot be wiped either
        known = {item.get("result_id") for item in entry.get("review_history") or ()}
        for role in REVIEW_ROLES:
            current = entry["results"].get(role)
            if isinstance(current, dict) and current.get("result_id") not in known:
                note_review(entry, role, current)
    # A task that was ready by the old rules is ready no more once the new ones ask for more.
    if entry["status"] == "ready_for_pr_review" and not task_ready(
        entry, risk, data["head"], policy_version(data)
    ):
        entry["status"] = "in_progress"
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(entry, sort_keys=True))


def show_role_model(args):
    """role-model: the model a coder/tester session of this task must run on. Read-only."""
    if not valid_name(args.task):
        fail("invalid task id")
    data = read(Path(args.manifest))
    require_v2(data, "role-model")
    risk = task_risk(data, data["tasks"].get(args.task))
    print(
        json.dumps(
            {
                "task": args.task,
                "role": args.role,
                "risk": risk,
                "model": role_model(risk),
                "policy_version": data["review_policy"]["version"],
            },
            sort_keys=True,
        )
    )


def fix_loop(args):
    path = Path(args.manifest)
    data = read(path)
    location = repo(data["repo"])
    if args.source in V2_ONLY_ROLES:
        require_v2(data, f"--source {args.source}")
    entry = task(data, args.task)
    risk = task_risk(data, entry)
    if entry["status"] == "blocked":
        fail("task is blocked after three fix cycles; explicit human reset is required")
    if args.severity is not None and not args.accept:
        fail("--severity is only allowed with --accept")
    policy = policy_version(data)
    if args.accept:
        record_acceptance(args, entry, data, location, risk, policy)
    elif args.defer:
        record_deferral(args, entry, data, location, risk, policy)
    elif args.decision is not None:
        record_decision(args, entry)
    else:
        record_fix_outcome(args, entry, data, location, risk, policy)
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(entry, sort_keys=True))


def check_note(note, flag):
    if note is None or not note.strip():
        fail(f"{flag} requires a non-empty --note")
    if len(note) > MAX_DECISION_NOTE_LENGTH:
        fail(f"--note must be at most {MAX_DECISION_NOTE_LENGTH} characters")
    if "\n" in note or "\r" in note:
        fail("--note must not contain line breaks")


def coverable_result(args, entry, data, location, flag, risk):
    """Preconditions shared by --defer and --accept: a reviewer's findings result on
    the current head and tree, in a task that is not waiting for a decision."""
    if entry["status"] == "needs_decision":
        fail(
            "decision required for source "
            f"{entry['decision_required_for']}: run fix-loop --decision ..."
        )
    if args.source is None:
        fail(f"{flag} requires --source")
    if args.source not in DEFER_SOURCES:
        fail(
            f"{flag} is only allowed for {', '.join(sorted(DEFER_SOURCES))}; "
            f"{args.source} findings go through --outcome failed"
        )
    check_note(args.note, flag)
    if (
        data["repo"] != location
        or commit(location, "HEAD") != data["head"]
        or fingerprint(location) != data["tree_fingerprint"]
    ):
        fail(f"working tree changed; run resume before recording {flag[2:]}")
    result_entry = entry["results"].get(args.source)
    current = isinstance(result_entry, dict) and (
        result_entry.get("head") == data["head"]
        and result_entry.get("tree_fingerprint") == data["tree_fingerprint"]
    )
    if risk == "high" and not (current and result_entry.get("status") == "findings"):
        # The findings may have left the results (`resume` there and back) but not the
        # history: the disposition is bound to that very result, as if it were still current.
        left_open = open_review_findings(entry, data["head"], args.source)
        if left_open:
            item = left_open[-1]
            return {
                "head": item["head"],
                "recorded_at": item["recorded_at"],
                "result_sha256": item["result_sha256"],
            }
    if not current:
        fail(f"{flag} requires a {args.source} findings result on the current head")
    if result_entry.get("status") != "findings":
        fail(
            f"{flag} requires {args.source} status findings, "
            f"got {result_entry.get('status')}"
        )
    return result_entry


def cover_digest(target):
    """Digest a deferral/acceptance binds to: of a current result, or the one the history kept."""
    return target.get("result_sha256") or result_digest(target)


def settle_after_cover(entry, risk, head, policy):
    """A deferral/acceptance may complete the task. From needs_fix it may only promote to
    ready_for_pr_review (every role pass or covered): update_task_status would otherwise
    demote a still-open needs_fix to in_progress. blocked/needs_decision never get here."""
    if entry["status"] != "needs_fix":
        update_task_status(entry, risk, head, policy)
    elif task_ready(entry, risk, head, policy):
        entry["status"] = "ready_for_pr_review"


def record_deferral(args, entry, data, location, risk, policy):
    """Defer low/P3-only findings of a reviewer to the next wave/task remainder.
    Spends no fix cycle, touches no per-source counter and is not a decision."""
    result_entry = coverable_result(args, entry, data, location, "--defer", risk)
    entry["deferrals"].append(
        {
            "source": args.source,
            "note": args.note,
            "head": result_entry["head"],
            "result_sha256": cover_digest(result_entry),
            "result_recorded_at": result_entry["recorded_at"],
            "recorded_at": max(now(), result_entry["recorded_at"]),
        }
    )
    settle_after_cover(entry, risk, data["head"], policy)


def record_acceptance(args, entry, data, location, risk, policy):
    """Accept a reviewer's findings of any severity as a known limitation (#54).
    Like a deferral it spends no fix cycle and is bound to the exact result; unlike
    a deferral it records the severity so medium/high ones reach the PR body."""
    if args.severity is None:
        fail("--accept requires --severity <low|medium|high>")
    result_entry = coverable_result(args, entry, data, location, "--accept", risk)
    entry.setdefault("acceptances", []).append(
        {
            "source": args.source,
            "severity": args.severity,
            "note": args.note,
            "head": result_entry["head"],
            "result_sha256": cover_digest(result_entry),
            "result_recorded_at": result_entry["recorded_at"],
            "recorded_at": max(now(), result_entry["recorded_at"]),
        }
    )
    settle_after_cover(entry, risk, data["head"], policy)


def record_decision(args, entry):
    """A recorded decision buys exactly one more round for its source."""
    if entry["status"] != "needs_decision":
        fail("--decision is only allowed while the task is needs_decision")
    if args.source is not None and args.source != entry["decision_required_for"]:
        fail(
            "--source "
            f"{args.source} does not match the pending decision_required_for "
            f"{entry['decision_required_for']}"
        )
    note = args.note
    check_note(note, "--decision")
    entry["decisions"].append(
        {
            "source": entry["decision_required_for"],
            "decision": args.decision,
            "note": note,
            "recorded_at": now(),
        }
    )
    entry["decision_required_for"] = None
    entry["status"] = "needs_fix"


def record_fix_outcome(args, entry, data, location, risk, policy):
    if entry["status"] == "needs_decision":
        fail(
            "decision required for source "
            f"{entry['decision_required_for']}: run fix-loop --decision ..."
        )
    if args.outcome == "pass":
        if args.source is not None:
            fail("--source is only allowed with --outcome failed")
        if (
            data["repo"] != location
            or commit(location, "HEAD") != data["head"]
            or fingerprint(location) != data["tree_fingerprint"]
        ):
            fail("working tree changed; run resume before recording a pass")
        update_task_status(entry, risk, data["head"], policy)
        if entry["status"] != "ready_for_pr_review":
            entry["status"] = "needs_verification"
        return
    if args.source is None:
        fail("--source is required with --outcome failed")
    if args.source == INTERNAL_SOURCE:
        record_internal_round(entry, data, location)
        return
    entry["fix_cycles"] += 1
    sources = entry["fix_sources"]
    sources[args.source] = sources.get(args.source, 0) + 1
    entry["status"] = "blocked" if entry["fix_cycles"] >= 3 else "needs_fix"
    if entry["status"] == "blocked":
        entry["blocked_reason"] = "three unsuccessful fix cycles"
        return
    decided = sum(1 for d in entry["decisions"] if d["source"] == args.source)
    if sources[args.source] - decided >= 2:
        entry["status"] = "needs_decision"
        entry["decision_required_for"] = args.source


def external_review_started(entry):
    """The role of the first external review recorded for the task in this run, or None.
    The marker `external_review` is written by task-result since 1.2.3; a manifest of 1.2.2 has
    none, so every record that survives `resume` is read as a witness as well: the session
    ownership of the task, its per-source fix counters, the current results and the review
    history. The rule is the same for state.py and the merge gate, which reads the raw manifest
    (without the session_roles that `read` restores from the results), so a marker and sessions
    removed by hand reopen nothing while any witness is left. Remainder: a first packet of
    github_codex_review/coderabbit leaves no review_history, so after `resume` the marker and
    the sessions are the only witnesses of it."""
    marker = entry.get("external_review")
    if isinstance(marker, dict):
        return marker.get("role") or "an external review"
    used = [role for role in (entry.get("session_roles") or {}).values() if isinstance(role, str)]
    used += list(entry.get("fix_sources") or {})
    used += [role for role in (entry.get("results") or {}) if isinstance(role, str)]
    used += [
        item.get("role")
        for item in (entry.get("review_history") or ())
        if isinstance(item, dict) and isinstance(item.get("role"), str)
    ]
    return next((role for role in sorted(used) if role in EXTERNAL_REVIEW_ROLES), None)


def record_internal_round(entry, data, location):
    """`fix-loop --outcome failed --source internal_reviewer`: a round of the internal Fable
    review. It spends no fix cycle, touches no per-source counter and never asks for a
    decision; it is counted in `internal_rounds` of the task (kept through `resume`), at most
    MAX_INTERNAL_ROUNDS, and only before the first external review of the task in this run.
    Each round is bound to one current `findings` result of the role."""
    started = external_review_started(entry)
    if started is not None:
        fail(
            f"--source {INTERNAL_SOURCE} is closed: the first external review of this task was "
            f"already recorded ({started}); until the end of the run findings go under the "
            "external source that raised them or under tester, which spends a fix cycle"
        )
    rounds = entry.setdefault("internal_rounds", [])
    if len(rounds) >= MAX_INTERNAL_ROUNDS:
        fail(
            f"{MAX_INTERNAL_ROUNDS} internal rounds of this task are already recorded: no "
            f"more rounds from {INTERNAL_SOURCE}; record this one under an ordinary source, "
            "for example fix-loop --outcome failed --source tester (it spends a fix cycle)"
        )
    if (
        data["repo"] != location
        or commit(location, "HEAD") != data["head"]
        or fingerprint(location) != data["tree_fingerprint"]
    ):
        fail("working tree changed; run resume before recording an internal round")
    current = entry["results"].get(INTERNAL_SOURCE)
    if not (
        isinstance(current, dict)
        and current.get("head") == data["head"]
        and current.get("tree_fingerprint") == data["tree_fingerprint"]
    ):
        fail(
            f"--source {INTERNAL_SOURCE} requires an {INTERNAL_SOURCE} findings result on the "
            f"current head: record task-result --role {INTERNAL_SOURCE} --status findings "
            "--model fable first"
        )
    if current.get("status") != "findings":
        fail(
            f"--source {INTERNAL_SOURCE} requires {INTERNAL_SOURCE} status findings, "
            f"got {current.get('status')}"
        )
    refuse_external_report(current.get("artifact"), f"the artifact of the {INTERNAL_SOURCE} result")
    digest = result_digest(current)
    if any(item.get("result_sha256") == digest for item in rounds):
        fail(
            f"this {INTERNAL_SOURCE} result already has its round recorded; a new round needs "
            f"a new {INTERNAL_SOURCE} findings result"
        )
    rounds.append(
        {
            "head": current["head"],
            "result_sha256": digest,
            "result_recorded_at": current.get("recorded_at"),
            "recorded_at": now(),
        }
    )
    entry["status"] = "needs_fix"


def mark(args):
    if not valid_name(args.task):
        fail("invalid task id")
    path = Path(args.manifest)
    data = read(path)
    location = repo(data["repo"])
    if data["repo"] != location:
        fail("manifest repository is not canonical; run resume first")
    if commit(location, "HEAD") != data["head"]:
        fail("repository HEAD differs from manifest; run resume first")
    if fingerprint(location) != data["tree_fingerprint"]:
        fail("working tree changed; run resume before mark")
    task(data, args.task)
    data["position"] = {
        "task": args.task,
        "step": args.step,
        "safe_point": args.safe_point == "true",
        "head": data["head"],
        "tree_fingerprint": data["tree_fingerprint"],
        "recorded_at": now(),
    }
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(data["position"], sort_keys=True))


def current_verdicts(entry, head, tree):
    """Verdicts on the current tree; deferred or accepted findings read as pass."""
    return {
        role: "pass" if is_covered(entry, role, item) else item["status"]
        for role, item in sorted((entry or {}).get("results", {}).items())
        if item.get("head") == head and item.get("tree_fingerprint") == tree
    }


def current_results(entry, head, tree):
    """The task's results recorded on the current head and tree."""
    return {
        role: item
        for role, item in ((entry or {}).get("results") or {}).items()
        if item.get("head") == head and item.get("tree_fingerprint") == tree
    }


def current_gaps(entry, head, tree, risk, policy):
    """Everything that keeps the passed results of a high-risk task from making it ready:
    high_risk_gaps plus, under the policy 1.2.4, the gaps of the mandatory Fable roles."""
    if risk != "high" or not entry:
        return {}
    results = current_results(entry, head, tree)
    return {**high_risk_gaps(entry, results, head), **fable_role_gaps(entry, results, head, policy)}


def is_complete(entry, head, tree, risk, policy):
    step, role, _note = derive_step(
        entry,
        current_verdicts(entry, head, tree),
        risk=risk,
        gaps=current_gaps(entry, head, tree, risk, policy),
    )
    return step == 7 and role is None


def pick_task(data, head, tree):
    tasks = data["tasks"]
    names = sorted(tasks)
    position = data.get("position")
    marked = position["task"] if position is not None else None

    def done(entry):
        return is_complete(entry, head, tree, task_risk(data, entry), policy_version(data))

    if marked in tasks and not done(tasks[marked]):
        return marked
    for name in names:
        if not done(tasks[name]):
            return name
    if marked in tasks:
        return marked
    return names[0] if names else None


ONE_LINE_UNSAFE = re.compile("[\x00-\x1f\x7f\x85\u2028\u2029]")


def one_line(text):
    """Single formatting point: escape control characters, then verify."""
    escaped = ONE_LINE_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
    if ONE_LINE_UNSAFE.search(escaped):
        fail("internal error: next_action is not one line")
    return escaped


REVIEW_ORDER = (
    ("tester", 5),
    ("cross_provider_reviewer", 5),
    ("second_reviewer", 5),
    ("github_codex_review", 7),
)
NO_WEAKER_MODEL = (
    f"a high-risk task requires {FABLE_MODEL}: retry once explicitly or escalate to the owner, "
    "no substitution by a weaker model (not a fix-loop)"
)
SECOND_REVIEW_FALLBACK = (
    f"; the only fallback is profile {OPUS_PROFILE} recorded with --quota-evidence "
    f"<{FABLE_PROFILE} quota error report for this HEAD and packet>, no other model"
)


NO_SECOND_REVIEW_FALLBACK = (
    f"; no fallback: {OPUS_PROFILE} stands in only for a {FABLE_PROFILE} that gave no review of "
    "this HEAD because of the provider quota"
)


def second_review_fallback(entry, head):
    """The tail of the `BLOCKED: second_reviewer error|unavailable` advice of a high-risk task:
    the codex-host-opus fallback is offered only where task-result would accept it. Not when
    Fable already reviewed this HEAD (fable_review_of), and not when the report behind the
    recorded result fails the acceptance rule of the quota evidence (quota_evidence_problem:
    this HEAD and the packet of the other review): the advice must not send the coordinator
    into a refusal. A report that cannot be read is said to be unchecked, not absent."""
    reviewed = fable_review_of(entry, head)
    if reviewed is not None:
        return (
            f"{NO_SECOND_REVIEW_FALLBACK} ({FABLE_PROFILE} already reviewed it: "
            f"{reviewed['status']} recorded as {reviewed['role']})"
        )
    results = (entry or {}).get("results") or {}
    result = results.get("second_reviewer")
    artifact = result.get("artifact") if isinstance(result, dict) else None
    try:
        evidence, _digest = read_report(artifact, "artifact")
    except SystemExit:
        # nothing readable behind the result (no --artifact, a relative path from another directory):
        # the quota is neither proven nor disproven here, task-result will check the report it is given
        return (
            "; the quota report could not be checked (no readable review.py report behind the "
            f"recorded result): {OPUS_PROFILE} counts only with --quota-evidence <{FABLE_PROFILE} "
            "quota error report for this HEAD and packet>, no other model"
        )
    # The acceptance rule itself (quota_evidence_problem), against the packet the other review of this
    # HEAD was recorded for; without one the report's own packet is all there is to compare.
    other = results.get("cross_provider_reviewer")
    packet_hash = other.get("packet_hash") if isinstance(other, dict) and other.get("head") == head else None
    problem = quota_evidence_problem(
        evidence, head, packet_hash if packet_hash is not None else evidence.get("state_packet_hash")
    )
    if problem is None:
        return SECOND_REVIEW_FALLBACK
    return f"{NO_SECOND_REVIEW_FALLBACK} (the recorded {FABLE_PROFILE} report {problem})"


def derive_step(entry, verdicts, last_run=False, risk=None, gaps=None, fallback=SECOND_REVIEW_FALLBACK):
    """Return (step, role, note); note overrides the default next_action.
    `risk` is the task's effective risk (None for manifest version 1) and `gaps` the
    current_gaps of its current results: with risk high the second review is required and a
    passed role named in `gaps` is not a pass; under the policy 1.2.4 `gaps` also names the
    mandatory Fable roles (internal_reviewer after tester, final_check after the two reviews).
    Below high the second review is optional, like CodeRabbit: only its findings need a disposition; error/unavailable/incomplete are
    recorded and ignored. `fallback` is the tail of the advice for a failed second review of a
    high-risk task (second_review_fallback; the default offers codex-host-opus).
    On the last permitted run of a wave there is no new `init --from-plan`, but fix rounds
    of the current run go on: findings that break the acceptance are a normal fix-loop
    --outcome failed; only findings outside the acceptance are deferred/accepted."""
    status = entry.get("status") if entry else None
    if status == "blocked":
        return None, None, None
    gaps = gaps or {}
    if gaps.get(INTERNAL_SOURCE, "").startswith(NEW_RUN_REQUIRED):
        # the internal review was missed before the first external packet: nothing done in this
        # run makes the task ready, so say it before any more work is spent on it
        return 5, INTERNAL_SOURCE, f"BLOCKED: {INTERNAL_SOURCE}: {gaps[INTERNAL_SOURCE]}"
    if status == "needs_decision":
        return 6, "coordinator", None
    if status == "needs_fix":
        return 6, "coder", None
    disposition = [
        role
        for role, _step in REVIEW_ORDER
        if verdicts.get(role) == "findings"
    ]
    # CodeRabbit is optional: only its findings need disposition; its
    # error/unavailable/incomplete are recorded and ignored.
    if verdicts.get("coderabbit") == "findings":
        disposition.append("coderabbit")
    if disposition and last_run and disposition[0] != "tester":
        return (
            6,
            "coordinator",
            "last run of the wave (no new init --from-plan): findings that break the "
            f"wave acceptance go through fix-loop --outcome failed --source {disposition[0]}; "
            "findings outside the acceptance: fix-loop --defer (low/P3) or --accept "
            f"--source {disposition[0]} --severity <low|medium|high> --note <text>",
        )
    if disposition:
        return (
            6,
            "coordinator",
            f"record fix-loop --outcome failed --source {disposition[0]} "
            "after disposing the findings",
        )
    high = risk == "high"
    for role in REVIEW_ROLES:
        # findings of this HEAD that left the results but were never disposed of
        if gaps.get(role, "").startswith(OPEN_FINDINGS):
            return 6, "coordinator", gaps[role]
    if high and verdicts.get("coder") in ("error", "unavailable"):
        return 4, "coder", f"BLOCKED: coder {verdicts['coder']}; {NO_WEAKER_MODEL}"
    # the roles whose failure to give a verdict stops the task
    order = [(role, step) for role, step in REVIEW_ORDER if high or role != "second_reviewer"]
    for role, step in order:
        if verdicts.get(role) in ("error", "unavailable"):
            if high and role == "tester":
                return step, role, f"BLOCKED: tester {verdicts[role]}; {NO_WEAKER_MODEL}"
            return (
                step,
                role,
                f"BLOCKED: {role} {verdicts[role]}; retry once explicitly "
                "or escalate to the owner (not a fix-loop)"
                + (fallback if role == "second_reviewer" else ""),
            )
    for role, step in order:
        if verdicts.get(role) == "incomplete":
            # incomplete = missing context, not a finding: no fix cycle
            return (
                step,
                role,
                f"re-run {role} with the missing context "
                "(incomplete is not a finding; do not record fix-loop)",
            )
    if verdicts.get("coder") != "pass":
        if verdicts.get("coder") is not None:
            return 4, "coder", "step 4 coder: coder must finish or fix before tester"
        return 4, "coder", None
    if "coder" in gaps:
        return 4, "coder", f"step 4 coder: {gaps['coder']}"
    if verdicts.get("tester") != "pass":
        return 5, "tester", None
    if "tester" in gaps:
        return 5, "tester", f"step 5 tester: {gaps['tester']}"
    if INTERNAL_SOURCE in gaps:
        # policy 1.2.4, high: the internal Fable review of this HEAD before the external packet
        if verdicts.get(INTERNAL_SOURCE) in ("error", "unavailable"):
            return 5, INTERNAL_SOURCE, f"BLOCKED: {INTERNAL_SOURCE}: {gaps[INTERNAL_SOURCE]}"
        return 5, INTERNAL_SOURCE, f"step 5 {INTERNAL_SOURCE}: {gaps[INTERNAL_SOURCE]}"
    for role in required_roles(risk)[2:]:
        if verdicts.get(role) != "pass":
            return 5, role, None
        if role in gaps:
            return 5, role, f"step 5 {role}: {gaps[role]}"
    if "final_check" in gaps:
        # policy 1.2.4, high: the final Fable check after the last task review
        if verdicts.get("final_check") in ("error", "unavailable"):
            return 7, "final_check", f"BLOCKED: final_check: {gaps['final_check']}"
        return 7, "final_check", f"step 7 final_check: {gaps['final_check']}"
    if verdicts.get("github_codex_review") != "pass":
        return 7, "github_codex_review", None
    return 7, None, None


def results_any(entry, head, tree):
    """Has the pipeline of the task produced a current result? The Fable subagent roles do not
    count: an architect's result belongs to step 1 and must not end the pre-code steps."""
    return any(role not in FABLE_ROLES for role in current_verdicts(entry, head, tree))


def where(args):
    data = read(Path(args.manifest))
    location = repo(data["repo"])
    current_head = commit(location, "HEAD")
    current_tree = fingerprint(location)
    tree_matches = (
        data["repo"] == location
        and current_head == data["head"]
        and current_tree == data["tree_fingerprint"]
    )
    check = plan_check(data)
    name = pick_task(data, current_head, current_tree)
    entry = data["tasks"].get(name) if name is not None else None
    risk = task_risk(data, entry)
    current = current_results(entry, current_head, current_tree)
    verdicts = {role: item["status"] for role, item in sorted(current.items())}
    deferred_roles = {
        role for role, item in current.items() if is_covered(entry, role, item)
    }
    open_findings = [
        {"role": role, "status": item["status"], "artifact": item.get("artifact")}
        for role, item in sorted(current.items())
        if item["status"] != "pass" and role not in deferred_roles
    ]
    left_open = open_review_findings(entry, current_head) if risk == "high" else []
    current_ids = {item.get("result_id") for item in current.values()}
    open_findings += [
        {"role": item["role"], "status": item["status"], "artifact": item.get("artifact")}
        for item in left_open
        if item.get("result_id") not in current_ids
    ]
    # links to the CURRENT results only (head and tree match): data for the next coordinator, not instructions
    artifacts = [
        {
            "role": role,
            "status": item["status"],
            "artifact": item.get("artifact"),
            "reviewed_head": item.get("reviewed_head"),
            "packet_hash": item.get("packet_hash"),
            "session_id": item.get("session_id"),
            "model": item.get("model"),
            "profile": item.get("profile"),
            "fallback_for": item.get("fallback_for"),
            "quota_evidence": item.get("quota_evidence"),
        }
        for role, item in sorted(current.items())
    ]
    status_value = entry.get("status", "pending") if entry else None
    effective = {
        role: "pass" if role in deferred_roles else verdict
        for role, verdict in verdicts.items()
    }
    run = data.get("run") if isinstance(data.get("run"), dict) else None
    run_max = live_run_cap(run["max"]) if run else None
    run_label = f"{run['index']}/{run_max}" if run else None
    last_run = bool(run) and run["index"] >= run_max
    step, role, note = derive_step(
        entry,
        effective,
        last_run,
        risk=risk,
        gaps=current_gaps(entry, current_head, current_tree, risk, policy_version(data)),
        fallback=second_review_fallback(entry, current_head) if risk == "high" else "",
    )
    position = data.get("position")
    precode = (
        entry is not None
        and not results_any(entry, current_head, current_tree)
        and status_value in ("pending", "in_progress")
        and position is not None
        and position["task"] == name
        and position["head"] == current_head
        and position["tree_fingerprint"] == current_tree
        and position["step"] in (1, 2, 3)
    )
    if precode:
        step, role = position["step"], "coordinator"
    marked_step = (
        position["step"]
        if position is not None and position["task"] == name
        else None
    )
    fix_round = {}
    if entry:
        decisions = entry.get("decisions", [])
        for source, count in sorted(entry.get("fix_sources", {}).items()):
            decided = sum(1 for item in decisions if item["source"] == source)
            fix_round[source] = f"{count - decided}/2"
        if entry.get("internal_rounds"):
            # rounds of the internal Fable review: their own counter, outside `total`
            fix_round[INTERNAL_SOURCE] = f"{len(entry['internal_rounds'])}/{MAX_INTERNAL_ROUNDS}"
    fix_round["total"] = f"{(entry or {}).get('fix_cycles', 0)}/3"
    required = (entry or {}).get("decision_required_for")
    if check == "changed":
        action = (
            "BLOCKED: plan changed since init (selected wave differs from "
            "plan.wave_sha256); stop and ask the owner"
        )
    elif not tree_matches:
        action = (
            f"run resume --manifest {args.manifest} --repo {data['repo']} "
            f"--base {data['base']} --head {current_head} before anything else"
        )
    elif name is None:
        action = "step 4 coder: no task yet; record one with mark and start the coder"
    elif status_value == "blocked":
        action = (
            f"BLOCKED: task {name} blocked after three fix cycles; "
            "explicit human reset is required"
        )
    elif status_value == "needs_decision":
        action = (
            f"run fix-loop --decision <{'|'.join(sorted(DECISIONS))}> "
            f"--note <text> --task {name} (decision required for source {required})"
        )
    elif precode:
        action = f"continue step {step} (requirements/plan) for task {name}"
    elif role is None:
        action = f"done: task {name} has all required results; continue to merge gate"
    elif note is not None:
        action = f"{note} (task {name})" if not note.startswith("BLOCKED") else note
    else:
        action = f"step {step} {role}: continue task {name}"
    action = one_line(action)
    # Only acceptances that cover a result of the current head/tree are active: after a
    # resume onto a new head the old ones are history, not limitations of this result.
    acceptances = [
        record
        for record in (accepted_record(entry, role, item) for role, item in sorted(current.items()))
        if record is not None
    ]
    if risk == "high":
        # an acceptance made on this HEAD stays a limitation of this HEAD after its result was replaced
        for item in (entry or {}).get("review_history") or ():
            record = history_cover(entry, item, ("acceptances",)) if item["head"] == current_head else None
            if record is not None and not any(record is seen for seen in acceptances):
                acceptances.append(record)
    print(
        json.dumps(
            {
                "tree_matches": tree_matches,
                "plan_check": check,
                "task": name,
                "task_status": status_value,
                "risk": risk,
                "review_policy": data.get("review_policy"),
                "step": step,
                "marked_step": marked_step,
                "role": role,
                "fix_round": fix_round,
                "verdicts": verdicts,
                "open_findings": open_findings,
                "artifacts": artifacts,
                "decision_required_for": required,
                "deferred": len((entry or {}).get("deferrals") or []),
                "accepted": len(acceptances),
                "accepted_limitations": [
                    {key: item.get(key) for key in ("source", "severity", "note", "head")}
                    for item in acceptances
                    if isinstance(item, dict) and item.get("severity") in ("medium", "high")
                ],
                "run": run_label,
                "last_run": last_run,
                "safe_point": None
                if position is None or position["task"] != name
                else position["safe_point"] is True
                and position["head"] == current_head
                and position["tree_fingerprint"] == current_tree,
                "next_action": action,
            },
            sort_keys=True,
        )
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", required=True)
    q = sub.add_parser("init", parents=[common])
    q.add_argument("--repo", required=True)
    q.add_argument("--base", required=True)
    q.add_argument("--head", required=True)
    q.add_argument("--run-id", default=None)
    q.add_argument("--from-plan", default=None)
    q.add_argument("--expect-sha256", default=None)
    q.add_argument("--max-runs", default=None)
    q.add_argument("--runs-file", default=None)
    q.add_argument("--risk", choices=RISK_ORDER, default=None)
    q.set_defaults(func=init)
    q = sub.add_parser("status", parents=[common])
    q.set_defaults(func=status)
    q = sub.add_parser("resume", parents=[common])
    q.add_argument("--repo", required=True)
    q.add_argument("--base", required=True)
    q.add_argument("--head", required=True)
    q.set_defaults(func=resume)
    q = sub.add_parser("task-result", parents=[common])
    q.add_argument("--task", required=True)
    q.add_argument("--role", required=True)
    q.add_argument("--status", required=True)
    q.add_argument("--session-id", required=True)
    q.add_argument("--head", required=True)
    q.add_argument("--artifact")
    q.add_argument("--reviewed-head")
    q.add_argument("--packet-hash")
    q.add_argument("--model")
    q.add_argument("--quota-evidence")
    q.set_defaults(func=result)
    q = sub.add_parser("task-risk", parents=[common])
    q.add_argument("--task", required=True)
    q.add_argument("--risk", required=True, choices=RISK_ORDER)
    q.set_defaults(func=set_risk)
    q = sub.add_parser("role-model", parents=[common])
    q.add_argument("--task", required=True)
    q.add_argument("--role", required=True, choices=MODEL_ROLES)
    q.set_defaults(func=show_role_model)
    q = sub.add_parser("fix-loop", parents=[common])
    q.add_argument("--task", required=True)
    outcome_or_decision = q.add_mutually_exclusive_group(required=True)
    outcome_or_decision.add_argument("--outcome", choices=["pass", "failed"])
    outcome_or_decision.add_argument("--decision", choices=sorted(DECISIONS))
    outcome_or_decision.add_argument("--defer", action="store_true")
    outcome_or_decision.add_argument("--accept", action="store_true")
    q.add_argument("--severity", choices=sorted(RISKS))
    q.add_argument("--source", choices=sorted(FIX_SOURCES))
    q.add_argument("--note")
    q.set_defaults(func=fix_loop)
    q = sub.add_parser("mark", parents=[common])
    q.add_argument("--task", required=True)
    q.add_argument("--step", required=True, type=int, choices=range(1, 8))
    q.add_argument("--safe-point", required=True, choices=["true", "false"])
    q.set_defaults(func=mark)
    q = sub.add_parser("where", parents=[common])
    q.set_defaults(func=where)
    return p


if __name__ == "__main__":
    try:
        arguments = parser().parse_args()
        manifest_path = Path(arguments.manifest)
        if arguments.command == "init":
            manifest_path = safe_manifest_path(manifest_path, repo(arguments.repo))
        else:
            precheck_existing_manifest(manifest_path)
            manifest_path = safe_manifest_path(
                manifest_path, repo(read(manifest_path)["repo"])
            )
        arguments.manifest = str(manifest_path)
        with lock(manifest_path):
            arguments.func(arguments)
    except SystemExit:
        raise
    except Exception as exc:
        fail(str(exc))
