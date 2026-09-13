#!/usr/bin/env python3
"""Local, atomic state for one Superarmanda run; no service or global ledger."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROLES = {
    "coder",
    "tester",
    "cross_provider_reviewer",
    "github_codex_review",
    "coderabbit",
}
STATUSES = {"pass", "findings", "incomplete", "error", "unavailable"}


def fail(message):
    raise SystemExit(f"state: {message}")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    if value.get("version") != 1 or not isinstance(value.get("tasks"), dict):
        fail("unsupported or malformed manifest")
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


def update_task_status(entry):
    """Only coder, tester and cross-provider review can make a task PR-ready."""
    if entry["status"] == "blocked":
        return
    required = ("coder", "tester", "cross_provider_reviewer")
    if all(entry["results"].get(role, {}).get("status") == "pass" for role in required):
        entry["status"] = "ready_for_pr_review"
    elif entry["results"]:
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
        if stale and entry["status"] != "blocked":
            entry["status"] = "pending"
            invalidated.append(name)
    data["head"] = head
    data["tree_fingerprint"] = tree
    data["updated_at"] = now()
    return invalidated


def init(args):
    path = safe_manifest_path(Path(args.manifest), repo(args.repo))
    if path.exists():
        fail("manifest already exists")
    location = repo(args.repo)
    base, head = validate_revision_pair(location, args.base, args.head)
    data = {
        "version": 1,
        "run_id": args.run_id,
        "repo": location,
        "base": base,
        "head": head,
        "tree_fingerprint": fingerprint(location),
        "tasks": {},
        "created_at": now(),
        "updated_at": now(),
    }
    write(path, data)
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
            if entry.get("status") != "blocked":
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
    entry = task(data, args.task)
    if entry["status"] == "blocked":
        fail("task is blocked after three fix cycles; explicit human reset is required")
    owner = session_owners(data).get(args.session_id)
    if owner is not None and owner != (args.task, args.role):
        fail("session_id is already used by another task or role for this run")
    if args.reviewed_head is not None and args.reviewed_head != args.head:
        fail("reviewed_head differs from result head")
    packet_is_sha256 = (
        args.packet_hash is not None
        and len(args.packet_hash) == 71
        and args.packet_hash.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in args.packet_hash[7:])
    )
    if args.packet_hash is not None and not packet_is_sha256:
        fail("packet_hash must be sha256:<64 lowercase hex characters>")
    if args.role == "cross_provider_reviewer" and args.status == "pass":
        if args.reviewed_head is None or args.packet_hash is None:
            fail("cross-provider pass requires reviewed_head and packet_hash")
    if args.role == "github_codex_review" and args.status == "pass":
        if args.reviewed_head is None or not (args.artifact or "").startswith(
            "https://"
        ):
            fail("GitHub Codex pass requires reviewed_head and HTTPS artifact URL")
        if args.packet_hash is not None:
            fail("GitHub Codex review does not accept packet_hash")
    entry["session_roles"][args.session_id] = args.role
    entry["results"][args.role] = {
        "status": args.status,
        "head": args.head,
        "session_id": args.session_id,
        "artifact": args.artifact,
        "reviewed_head": args.reviewed_head,
        "packet_hash": args.packet_hash,
        "tree_fingerprint": data["tree_fingerprint"],
        "recorded_at": now(),
    }
    update_task_status(entry)
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(entry, sort_keys=True))


def fix_loop(args):
    path = Path(args.manifest)
    data = read(path)
    location = repo(data["repo"])
    entry = task(data, args.task)
    if entry["status"] == "blocked":
        fail("task is blocked after three fix cycles; explicit human reset is required")
    if args.outcome == "pass":
        if (
            data["repo"] != location
            or commit(location, "HEAD") != data["head"]
            or fingerprint(location) != data["tree_fingerprint"]
        ):
            fail("working tree changed; run resume before recording a pass")
        update_task_status(entry)
        if entry["status"] != "ready_for_pr_review":
            entry["status"] = "needs_verification"
    else:
        entry["fix_cycles"] += 1
        entry["status"] = "blocked" if entry["fix_cycles"] >= 3 else "needs_fix"
        if entry["status"] == "blocked":
            entry["blocked_reason"] = "three unsuccessful fix cycles"
    data["updated_at"] = now()
    write(path, data)
    print(json.dumps(entry, sort_keys=True))


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
    q.set_defaults(func=result)
    q = sub.add_parser("fix-loop", parents=[common])
    q.add_argument("--task", required=True)
    q.add_argument("--outcome", choices=["pass", "failed"], required=True)
    q.set_defaults(func=fix_loop)
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
