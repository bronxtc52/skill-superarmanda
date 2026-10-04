#!/usr/bin/env python3
"""Read-only evidence checker for the GitHub PR-review gate (GraphQL only)."""

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
    r"\ACodex Review: Didn't find any major issues\."
    r"(?: [^\s<>\[\]`#][^\n<>\[\]`#]{0,47})?\n\n"
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
RABBIT_DONE = re.compile(
    r"No actionable comments were generated|Actionable comments posted: \d+"
)
RABBIT_RANGE = re.compile(
    r"between ([0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?)(?![0-9a-fA-F])"
    r" and ([0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?)(?![0-9a-fA-F])"
)
RABBIT_REFUSAL = re.compile(
    r"review limit reached|rate limited|no credits available"
    r"|reviews? (?:are |is )?disabled",
    re.IGNORECASE,
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


def outside_quotes(body):
    """Строки тела вне блок-цитат: первая непробельная литера `>` — цитата."""
    return "\n".join(
        line for line in body.split("\n") if not line.lstrip().startswith(">")
    )


def range_ends(body):
    return [match.group(2).lower() for match in RABBIT_RANGE.finditer(body)]


def coderabbit_verdict(expected_head, reviews, review_comments, issue_comments):
    """Результат CodeRabbit на HEAD по факту ревью; чистая функция без I/O.

    Два канала доказательства: (а) review на HEAD; (б) сводный issue comment с
    маркером завершения и диапазоном, кончающимся ровно на HEAD (тела review не
    считаются). Явный отказ засчитывается, только если в его теле есть диапазон
    с концом на HEAD. Остальное — pending: ждёт координатор.
    """
    head = (expected_head or "").lower()
    verdict = lambda status, reason, evidence=None: {  # noqa: E731
        "status": status,
        "reason": reason,
        "evidence_url": evidence,
    }
    attached = {}
    found = None  # url первой HEAD-находки
    for comment in review_comments:
        body = text(comment)
        if not trusted(comment, RABBIT) or not body:
            continue
        if (comment.get("commit_id") or "").lower() != head:
            continue
        attached.setdefault(comment.get("pull_request_review_id"), []).append(comment)
        if not rabbit_boilerplate(body):
            found = found or url(comment) or "inline comment"
    proof = None  # url первого доказательства ревью HEAD
    for review in reviews:
        if not trusted(review, RABBIT):
            continue
        body, state = text(review), (review.get("state") or "").upper()
        if (review.get("commit_id") or "").lower() == head:
            if state in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED"):
                proof = proof or url(review)
            has_text = bool(body) and not rabbit_boilerplate(body)
            if has_text or attached.get(review.get("id")) or state in (
                "CHANGES_REQUESTED",
                "DISMISSED",
            ):
                found = found or url(review) or "review"
    bodies = [
        (url(item), text(item))
        for item in (*reviews, *issue_comments)
        if trusted(item, RABBIT) and text(item)
    ]
    summaries = [
        (url(item), text(item))
        for item in issue_comments
        if trusted(item, RABBIT) and text(item)
    ]
    for where, body in summaries:
        quoted_free = outside_quotes(body)
        if RABBIT_DONE.search(quoted_free) and head in range_ends(quoted_free):
            proof = proof or where
    if found:
        return verdict("findings", "CodeRabbit left findings on current HEAD", found)
    if proof:
        return verdict("pass", "CodeRabbit review of current HEAD is proven", proof)
    for where, body in bodies:
        match = RABBIT_REFUSAL.search(body)
        if match and head in range_ends(body):
            return verdict(
                "unavailable", f"CodeRabbit refused: {match.group(0).lower()}", where
            )
    if any(
        trusted(item, RABBIT) and RABBIT_DRAFT_SKIP.fullmatch(text(item))
        for item in issue_comments
    ):
        return verdict("pending", "CodeRabbit skipped the draft PR")
    return verdict("pending", "no CodeRabbit review bound to current HEAD yet")


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
        "coderabbit": {
            "status": "pending",
            "reason": "PR HEAD differs from expected SHA",
            "evidence_url": None,
        },
    }
    if head != expected_head:
        result["limitations"].append("PR HEAD differs from expected SHA")
        return result
    result["coderabbit"] = coderabbit_verdict(
        expected_head, reviews, review_comments, issue_comments
    )
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
                    "empty CodeRabbit COMMENTED review on HEAD proves completion"
                )
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


GRAPHQL_FAILED = "gh api graphql failed"
MAX_PAGES = 50  # страниц одной коннекции; больше — fail-closed

HEADER_QUERY = """query Header($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { headRefOid isDraft state }
  }
}"""
REVIEWS_QUERY = """query Reviews($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          databaseId url state body
          commit { oid }
          author { __typename login }
        }
      }
    }
  }
}"""
THREADS_QUERY = """query Threads($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {
              databaseId url body
              commit { oid }
              originalCommit { oid }
              author { __typename login }
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}"""
THREAD_COMMENTS_QUERY = """query ThreadComments($thread: ID!, $cursor: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          databaseId url body
          commit { oid }
          originalCommit { oid }
          author { __typename login }
          pullRequestReview { databaseId }
        }
      }
    }
  }
}"""
ISSUE_COMMENTS_QUERY = """query IssueComments($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId url body author { __typename login } }
      }
    }
  }
}"""
CHECKS_QUERY = """query Checks($owner: String!, $name: String!, $head: GitObjectID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    object(oid: $head) {
      ... on Commit {
        checkSuites(first: 100, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            app { slug }
            checkRuns(first: 100) {
              pageInfo { hasNextPage endCursor }
              nodes { name status conclusion detailsUrl }
            }
          }
        }
      }
    }
  }
}"""
SUITE_RUNS_QUERY = """query SuiteRuns($suite: ID!, $cursor: String) {
  node(id: $suite) {
    ... on CheckSuite {
      checkRuns(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { name status conclusion detailsUrl }
      }
    }
  }
}"""
RESOLVE_QUERY = """query Resolve($owner: String!, $name: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $ref) { ... on Commit { oid } }
  }
}"""


def fail():
    raise RuntimeError(GRAPHQL_FAILED)


def graphql(query, variables=None):
    """Одна read-only GraphQL-операция; любой сбой — одна фиксированная ошибка."""
    cmd = ["gh", "api", "graphql", "--hostname", "github.com", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        if isinstance(value, bool):
            fail()
        elif isinstance(value, int):
            cmd += ["-F", f"{key}={value}"]
        elif isinstance(value, str):
            cmd += ["-f", f"{key}={value}"]
        else:
            fail()
    try:
        reply = json.loads(
            subprocess.check_output(
                cmd, text=True, stderr=subprocess.PIPE, timeout=20
            )
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        raise RuntimeError(GRAPHQL_FAILED) from exc
    if not isinstance(reply, dict) or "errors" in reply:
        fail()
    data = reply.get("data")
    if not isinstance(data, dict):
        fail()
    return data


def dig(value, *path):
    """Вложенный объект по пути; пропуск, null или не-объект — fail-closed."""
    for key in path:
        if not isinstance(value, dict) or not isinstance(value.get(key), dict):
            fail()
        value = value[key]
    return value


def drain(fetch, first=None):
    """Все узлы коннекции. fetch(cursor) -> коннекция; first — уже прочитанная первая страница."""
    nodes, seen, cursor, page = [], set(), None, first
    for _ in range(MAX_PAGES):
        if page is None:
            page = fetch(cursor)
        info, part = page.get("pageInfo"), page.get("nodes")
        if not isinstance(info, dict) or not isinstance(part, list):
            fail()
        if not all(isinstance(item, dict) for item in part):
            fail()
        more = info.get("hasNextPage")
        if not isinstance(more, bool):
            fail()
        nodes.extend(part)
        if not more:
            return nodes
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            fail()
        seen.add(cursor)
        page = None
    fail()  # MAX_PAGES исчерпан, а страницы не кончились


def actor(author):
    """GraphQL-автор -> REST-форма: у бота логин с `[bot]`, тип Bot."""
    if not isinstance(author, dict):
        return {}
    name, kind = author.get("login"), author.get("__typename")
    if not (isinstance(name, str) and name and isinstance(kind, str)):
        return {}
    if kind == "Bot":
        return {
            "login": name if name.endswith("[bot]") else name + "[bot]",
            "type": "Bot",
        }
    return {"login": name, "type": kind}


def oid(node, key):
    value = node.get(key)
    value = value.get("oid") if isinstance(value, dict) else None
    return value if isinstance(value, str) else None


def fetch_header(owner, name, number):
    pull = dig(
        graphql(HEADER_QUERY, {"owner": owner, "name": name, "number": number}),
        "repository",
        "pullRequest",
    )
    head = pull.get("headRefOid")
    if not isinstance(head, str) or not head:
        fail()
    return {"head": {"sha": head}, "draft": pull.get("isDraft"), "state": pull.get("state")}


def pull_connection(query, key, owner, name, number):
    base = {"owner": owner, "name": name, "number": number}

    def fetch(cursor):
        variables = dict(base, cursor=cursor) if cursor else base
        return dig(graphql(query, variables), "repository", "pullRequest", key)

    return drain(fetch)


def fetch_reviews(owner, name, number):
    return [
        {
            "id": node.get("databaseId"),
            "state": node.get("state"),
            "body": node.get("body"),
            "commit_id": oid(node, "commit"),
            "html_url": node.get("url"),
            "user": actor(node.get("author")),
        }
        for node in pull_connection(REVIEWS_QUERY, "reviews", owner, name, number)
    ]


def review_comment(node):
    review = node.get("pullRequestReview")
    return {
        "id": node.get("databaseId"),
        "body": node.get("body"),
        "commit_id": oid(node, "commit"),
        "original_commit_id": oid(node, "originalCommit"),
        "pull_request_review_id": review.get("databaseId")
        if isinstance(review, dict)
        else None,
        "html_url": node.get("url"),
        "user": actor(node.get("author")),
    }


def fetch_review_comments(owner, name, number):
    comments = []
    for thread in pull_connection(THREADS_QUERY, "reviewThreads", owner, name, number):
        tid, first = thread.get("id"), thread.get("comments")
        if not isinstance(tid, str) or not isinstance(first, dict):
            fail()

        def more(cursor, tid=tid):
            return dig(
                graphql(THREAD_COMMENTS_QUERY, {"thread": tid, "cursor": cursor}),
                "node",
                "comments",
            )

        comments.extend(review_comment(node) for node in drain(more, first))
    return comments


def fetch_issue_comments(owner, name, number):
    return [
        {
            "id": node.get("databaseId"),
            "body": node.get("body"),
            "html_url": node.get("url"),
            "user": actor(node.get("author")),
        }
        for node in pull_connection(
            ISSUE_COMMENTS_QUERY, "comments", owner, name, number
        )
    ]


def fetch_checks(owner, name, head):
    """Check runs коммита HEAD: информационно, на статус гейта не влияют."""
    base = {"owner": owner, "name": name, "head": head}

    def suites(cursor):
        variables = dict(base, cursor=cursor) if cursor else base
        found = dig(graphql(CHECKS_QUERY, variables), "repository", "object")
        return dig(found, "checkSuites")

    runs = []
    for suite in drain(suites):
        sid, first = suite.get("id"), suite.get("checkRuns")
        if not isinstance(sid, str) or not isinstance(first, dict):
            fail()
        app = suite.get("app")
        slug = app.get("slug") if isinstance(app, dict) else None

        def more(cursor, sid=sid):
            return dig(
                graphql(SUITE_RUNS_QUERY, {"suite": sid, "cursor": cursor}),
                "node",
                "checkRuns",
            )

        for node in drain(more, first):
            runs.append(
                {
                    "name": node.get("name"),
                    "status": node.get("status"),
                    "conclusion": node.get("conclusion"),
                    "app": slug,
                }
            )
    pending = [r["name"] for r in runs if r["status"] != "COMPLETED"]
    failed = [
        [r["name"], r["conclusion"]]
        for r in runs
        if r["status"] == "COMPLETED" and r["conclusion"] != "SUCCESS"
    ]
    return runs, {"total": len(runs), "pending": pending, "failed": failed}


def resolve_ref(owner, name, ref):
    """Полный SHA для сокращённого; null/не-Commit — None. Сбой gh/GraphQL — RuntimeError."""
    found = dig(
        graphql(RESOLVE_QUERY, {"owner": owner, "name": name, "ref": ref}),
        "repository",
    ).get("object")
    value = found.get("oid") if isinstance(found, dict) else None
    return value if isinstance(value, str) and value else None


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
    owner, name = args.repo.split("/")

    def snapshot():
        return (
            fetch_reviews(owner, name, args.pr),
            fetch_review_comments(owner, name, args.pr),
            fetch_issue_comments(owner, name, args.pr),
        )

    def moved(reason):
        # Результат CodeRabbit посчитан по первому снимку: данные сдвинулись, он недействителен.
        return {"status": "pending", "reason": reason, "evidence_url": None}

    first = fetch_header(owner, name, args.pr)
    reviews, review_comments, issue_comments = snapshot()
    check_runs, checks = fetch_checks(owner, name, first["head"]["sha"])
    cache = {}

    def resolve(ref):
        if ref not in cache:
            cache[ref] = resolve_ref(owner, name, ref)
        return cache[ref]

    result = evaluate(
        first, reviews, review_comments, issue_comments, args.head, resolve
    )
    # Информационные поля: статус гейта они не меняют.
    result["check_runs"], result["checks"] = check_runs, checks
    second = snapshot()
    last = fetch_header(owner, name, args.pr)  # Evidence is invalid if the PR moved.
    if last["head"]["sha"] != first["head"]["sha"]:
        result["status"] = "incomplete"
        result["current_head"] = last["head"]["sha"]
        result["limitations"].append("PR HEAD changed while collecting evidence")
        result["coderabbit"] = moved("PR HEAD changed while collecting evidence")
    elif second != (reviews, review_comments, issue_comments):
        result["status"] = "incomplete"
        result["limitations"].append(
            "review evidence changed while collecting evidence"
        )
        result["coderabbit"] = moved("review evidence changed while collecting evidence")
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
