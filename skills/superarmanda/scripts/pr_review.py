#!/usr/bin/env python3
"""Read-only evidence checker for the GitHub PR-review gate."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CODEX = "chatgpt-codex-connector[bot]"
RABBIT = "coderabbitai[bot]"
REPOSITORY = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MARKER = "<!-- codex-pull-request-review-summary -->"
CLEAN_LEGACY = re.compile(
    r"\ACodex Review: Didn't find any major issues\. :rocket:\n\n\*\*Reviewed commit:\*\* `([0-9a-fA-F]+)`(?:\n\n<details><summary>About Codex</summary>Automated review\.</details>)?\Z",
    re.DOTALL,
)
CLEAN_OBSERVED = re.compile(
    r"\ACodex Review: Didn't find any major issues\. (?:Keep them coming!|Chef's kiss\.)\n\n"
    r"\*\*Reviewed commit:\*\* `([0-9a-fA-F]+)`\n\n"
    r"<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n"
    r"\[Your team has set up Codex to review pull requests in this repo\]"
    r"\(https://chatgpt\.com/codex/cloud/settings/general\)\. Reviews are triggered when you\n"
    r"- Open a pull request for review\n- Mark a draft as ready\n- Comment \"@codex review\"\.\n\n"
    r"If Codex has suggestions, it will comment; otherwise it will react with 👍\.\n\n\n\n\n"
    r"Codex can also answer questions or update the PR\. Try commenting \"@codex address that feedback\"\.\n"
    r"            \n"
    r"</details>\Z",
    re.DOTALL,
)
RABBIT_BOILERPLATE = re.compile(
    r"\A<!-- coderabbitai-review-status -->\nReview skipped: no credits available\.\Z",
    re.DOTALL,
)
RABBIT_DRAFT_SKIP = re.compile(
    r"\A<!-- This is an auto-generated comment: summarize by coderabbit\.ai -->\n"
    r"<!-- This is an auto-generated comment: skip review by coderabbit\.ai -->\n\n"
    r"> \[!IMPORTANT\]\n> ## Draft PR not reviewed\n> \n"
    r"> Draft PRs are not automatically reviewed by default\.\n> \n"
    r'> - \[ \] <!-- \{"checkboxId":"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"\} --> Trigger a manual review\n'
    r"> \n> To automatically review draft PRs, update your CodeRabbit configuration:\n> \n"
    r"> ```yaml\n> reviews:\n>   auto_review:\n>     drafts: true\n> ```\n\n"
    r"<!-- end of auto-generated comment: skip review by coderabbit\.ai -->\n\n"
    r"<!-- tips_start -->\n\n---\n\n\n\n\n<sub>Comment `@coderabbitai help` to get the list of available commands\.</sub>\n\n<!-- tips_end -->\Z",
    re.DOTALL,
)


def login(item):
    return (item.get("user") or item.get("author") or {}).get("login")


def trusted(item, bot):
    actor = item.get("user") or item.get("author") or {}
    return actor.get("login") == bot and actor.get("type") == "Bot"


def url(item):
    return item.get("html_url") or item.get("url")


def text(item):
    return (item.get("body") or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def rabbit_boilerplate(body):
    return bool(RABBIT_BOILERPLATE.fullmatch(body) or RABBIT_DRAFT_SKIP.fullmatch(body))


def clean_commit(body):
    for pattern in (CLEAN_LEGACY, CLEAN_OBSERVED):
        match = pattern.fullmatch(body)
        if match:
            return match.group(1)
    return None


def finding(source, item, reason):
    value = {
        "source": source,
        "author": login(item),
        "url": url(item),
        "reason": reason,
    }
    if source == "review_comment":
        value["commit_id"] = item.get("commit_id")
        value["original_commit_id"] = item.get("original_commit_id")
    return value


def evaluate(
    pr,
    reviews,
    review_comments,
    issue_comments,
    expected_head,
    resolve_commit=lambda ref: None,
):
    """Classify fetched GitHub evidence without I/O; suitable for fixture tests."""
    head = (pr.get("head") or {}).get("sha")
    result = {
        "status": "incomplete",
        "current_head": head,
        "draft": bool(pr.get("draft")),
        "evidence_urls": [],
        "findings": [],
        "limitations": [],
    }
    if head != expected_head:
        result["limitations"].append("PR HEAD differs from expected SHA")
        return result
    codex_approved = False
    codex_clean_comment = False
    codex_seen = False
    blocking_incomplete = False
    comments_by_review = {}
    for comment in review_comments:
        who, commit, body = login(comment), comment.get("commit_id"), text(comment)
        if not (trusted(comment, CODEX) or trusted(comment, RABBIT)) or not body:
            continue
        result["evidence_urls"].append(url(comment))
        if commit != expected_head:
            result["limitations"].append(
                "historical bot inline comment is not current-HEAD evidence"
            )
            continue
        comments_by_review.setdefault(comment.get("pull_request_review_id"), []).append(
            comment
        )
        # Inline findings are independently current-HEAD evidence. GitHub can
        # omit, stale, or reassign their parent review, so parent aggregation
        # must never decide whether this evidence is emitted.
        if who != RABBIT or not rabbit_boilerplate(body):
            result["findings"].append(
                finding("review_comment", comment, "applies to current HEAD")
            )
    for review in reviews:
        who, state, commit = (
            login(review),
            (review.get("state") or "").upper(),
            review.get("commit_id"),
        )
        if not (trusted(review, CODEX) or trusted(review, RABBIT)):
            continue
        result["evidence_urls"].append(url(review))
        if who == CODEX:
            codex_seen = True
        if commit != expected_head:
            if who == CODEX:
                result["limitations"].append(
                    "Codex review is stale or lacks the full current commit_id"
                )
            continue
        attached = comments_by_review.get(review.get("id"), [])
        body = text(review)
        clean_sha = clean_commit(body) if body else None
        clean_review = (
            who == CODEX
            and state in ("APPROVED", "COMMENTED")
            and clean_sha is not None
            and resolve_commit(clean_sha) == expected_head
            and not attached
        )
        if clean_review:
            codex_approved = True
        elif state == "APPROVED" and who == CODEX and not body and not attached:
            codex_approved = True
        elif state == "COMMENTED":
            if body or attached:
                if who == RABBIT and rabbit_boilerplate(body) and not attached:
                    result["limitations"].append(
                        "CodeRabbit walkthrough/status metadata is not a finding"
                    )
                else:
                    result["findings"].append(
                        finding(
                            "review",
                            review,
                            "COMMENTED review requires coordinator disposition",
                        )
                    )
            elif who == RABBIT:
                result["limitations"].append(
                    "empty CodeRabbit COMMENTED review is incomplete"
                )
                blocking_incomplete = True
            elif who == CODEX:
                result["limitations"].append(
                    "Codex COMMENTED review is not an explicit clean result"
                )
                blocking_incomplete = True
        elif state in ("CHANGES_REQUESTED", "DISMISSED") or body or attached:
            result["findings"].append(
                finding("review", review, "review requires coordinator disposition")
            )
        elif who == CODEX:
            result["limitations"].append(
                "Codex review is pending or has an unknown format"
            )
            blocking_incomplete = True
    for comment in issue_comments:
        who, body = login(comment), text(comment)
        if not (trusted(comment, CODEX) or trusted(comment, RABBIT)) or not body:
            continue
        result["evidence_urls"].append(url(comment))
        if who == CODEX:
            codex_seen = True
            clean_sha = clean_commit(body)
            if clean_sha:
                full = resolve_commit(clean_sha)
                if full == expected_head:
                    codex_clean_comment = True
                    continue
                result["limitations"].append(
                    "Codex clean comment does not resolve to current full SHA"
                )
                continue
            if MARKER in body:
                result["limitations"].append(
                    "Codex summary marks completion only; it is not a clean result"
                )
                continue
            result["findings"].append(
                finding(
                    "issue_comment",
                    comment,
                    "unknown current-HEAD Codex comment requires disposition",
                )
            )
            continue
        if rabbit_boilerplate(body):
            result["limitations"].append(
                "CodeRabbit walkthrough/status metadata is not a finding"
            )
            continue
        result["findings"].append(
            finding(
                "issue_comment",
                comment,
                "bot-provided comment requires coordinator disposition",
            )
        )
    result["evidence_urls"] = sorted({item for item in result["evidence_urls"] if item})
    if result["findings"]:
        result["status"] = "findings"
    elif blocking_incomplete:
        result["status"] = "incomplete"
    elif codex_approved or codex_clean_comment:
        result["status"] = "pass"
    else:
        result["limitations"].append(
            "no completed, clean GitHub Codex review bound to current HEAD"
        )
        if not codex_seen:
            result["limitations"].append(
                "trusted GitHub Codex bot evidence was not found"
            )
    return result


def gh(*args):
    try:
        return json.loads(
            subprocess.check_output(
                ["gh", "api", "--hostname", "github.com", "--method", "GET", *args],
                text=True,
                stderr=subprocess.PIPE,
                timeout=20,
            )
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("gh api read failed") from exc


def pages(endpoint):
    result, page = [], 1
    while True:
        part = gh(f"{endpoint}?per_page=100&page={page}")
        if not isinstance(part, list):
            raise RuntimeError("gh api pagination response is not a list")
        result.extend(part)
        if len(part) < 100:
            return result
        page += 1


def contains(parent, child):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def root_suffix(path, root):
    inside = False
    for ancestor in (path, *path.parents):
        resolved = ancestor.resolve(strict=False)
        if resolved == root:
            return path.relative_to(ancestor)
        if contains(root, resolved):
            inside = True
    return None if inside else False


def local_git_environment():
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def safe_output(value, worktree):
    path = Path(os.fspath(value))
    if ".." in path.parts:
        raise ValueError("--output must not contain '..'")
    path = path if path.is_absolute() else Path.cwd() / path
    roots = [worktree.resolve()]
    for argument in ("--git-dir", "--git-common-dir"):
        try:
            raw = subprocess.check_output(
                ["git", "-C", str(worktree), "rev-parse", argument],
                text=True,
                stderr=subprocess.PIPE,
                timeout=10,
                env=local_git_environment(),
            ).strip()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise ValueError("cannot resolve local Git metadata") from exc
        location = Path(raw)
        roots.append(
            (worktree / location).resolve()
            if not location.is_absolute()
            else location.resolve()
        )
    parent, final = path.parent.resolve(), path.resolve(strict=False)
    if any(
        root_suffix(path, root) is not False
        or any(contains(root, item) for item in (path, parent, final))
        for root in roots
    ):
        raise ValueError("--output must be outside --repo, worktree and Git metadata")
    return path


def local_worktree(path):
    try:
        return Path(
            subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.PIPE,
                timeout=10,
                env=local_git_environment(),
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("--worktree must be a local Git worktree") from exc


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def check(args):
    if not REPOSITORY.fullmatch(args.repo) or any(
        part in (".", "..") for part in args.repo.split("/")
    ):
        raise ValueError("--repo must be OWNER/NAME")
    output = safe_output(args.output, local_worktree(args.worktree))
    base = f"repos/{args.repo}/pulls/{args.pr}"
    first = gh(base)
    reviews, review_comments, issue_comments = (
        pages(base + "/reviews"),
        pages(base + "/comments"),
        pages(f"repos/{args.repo}/issues/{args.pr}/comments"),
    )
    cache = {}

    def resolve(ref):
        if ref not in cache:
            try:
                cache[ref] = gh(f"repos/{args.repo}/commits/{ref}").get("sha")
            except RuntimeError:
                cache[ref] = None
        return cache[ref]

    result = evaluate(
        first, reviews, review_comments, issue_comments, args.head, resolve
    )
    second = (
        pages(base + "/reviews"),
        pages(base + "/comments"),
        pages(f"repos/{args.repo}/issues/{args.pr}/comments"),
    )
    last = gh(base)  # Evidence is invalid if the PR moved during collection.
    if ((last.get("head") or {}).get("sha")) != ((first.get("head") or {}).get("sha")):
        result["status"] = "incomplete"
        result["current_head"] = (last.get("head") or {}).get("sha")
        result["limitations"].append("PR HEAD changed while collecting evidence")
    elif second != (reviews, review_comments, issue_comments):
        result["status"] = "incomplete"
        result["limitations"].append(
            "review evidence changed while collecting evidence"
        )
    write(output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("pass", "findings", "incomplete") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    cmd = sub.add_parser("check")
    cmd.add_argument("--repo", required=True, metavar="OWNER/NAME")
    cmd.add_argument("--pr", required=True, type=int)
    cmd.add_argument("--head", required=True)
    cmd.add_argument("--output", required=True)
    cmd.add_argument(
        "--worktree",
        default=Path.cwd(),
        type=Path,
        help="local checkout used only to reject an in-tree output",
    )
    cmd.set_defaults(func=check)
    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except (ValueError, RuntimeError) as exc:
        print(f"pr_review: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
