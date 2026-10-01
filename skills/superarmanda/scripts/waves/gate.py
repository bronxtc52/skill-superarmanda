"""Merge gate and alarm of the wave dispatcher: pure functions over facts from GitHub and the manifest.

The module never touches the network or git: facts are collected through two injected functions,
`api(path) -> parsed JSON` (one `gh api` GET) and `graphql(query, variables) -> parsed JSON`, and
every failure of them is a CollectError. The gate is fail-closed: a fact that is missing, stale
or unreadable never gives `pass`, at most `wait`. wab.py wraps `gh` into the two functions and
decides what to do with the verdict.

Verdicts of `evaluate`: `pass` (merge may start), `wait` (not yet: checks running, Codex has not
finished, facts could not be collected), `fail` (a reason that needs a human or a fix).
"""
import datetime
import importlib.util
import json
import pathlib
import re
import shlex

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
MAX_PAGES = 20           # 100 items per page: more than that is an error of collection, not a verdict
PAGE = 100
OK_REVIEW_STATES = ("COMMENTED", "APPROVED", "CHANGES_REQUESTED")
P01 = re.compile(r"!\[P[01] Badge\]")
SHA = re.compile(r"[0-9a-f]{40,64}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
NODE_ID = re.compile(r"[A-Za-z0-9_=+/-]+")
SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
COMMIT_CELL = re.compile(r"`([0-9a-f]{7,40})`")
WAVE = re.compile(r"[A-Za-z0-9_-]+")
RUN_ID = re.compile(r"[A-Za-z0-9._-]+")
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage}"
    "nodes{id isResolved comments(first:1){nodes{author{login} body}}}}}}}")
RESOLVE_MUTATION = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"


class CollectError(Exception):
    """A fact could not be collected (gh failed, not JSON, too many pages)."""


def _load(name):
    """A sibling module of scripts/ by file path, under a private name: importing it neither
    shadows nor depends on sys.path (state.py and pr_review.py have no import side effects)."""
    spec = importlib.util.spec_from_file_location(f"superarmanda_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pr_review = _load("pr_review")
state = _load("state")
CODEX = pr_review.CODEX


def fingerprint(directory):
    return state.fingerprint(directory)


# ---------- facts ----------

def _ts(value):
    """An ISO-8601 time as an aware datetime, None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _paged(api, path, key=None):
    """Every page of a list endpoint (`gh` 2.45 has no --slurp): until a short page."""
    sep = "&" if "?" in path else "?"
    out = []
    for page in range(1, MAX_PAGES + 1):
        data = api(f"{path}{sep}per_page={PAGE}&page={page}")
        items = data.get(key) if key and isinstance(data, dict) else data
        if not isinstance(items, list):
            raise CollectError(f"{path}: unexpected response shape")
        out.extend(items)
        if len(items) < PAGE:
            return out
    raise CollectError(f"{path}: more than {MAX_PAGES} pages")


def _object(api, path):
    data = api(path)
    if not isinstance(data, dict):
        raise CollectError(f"{path}: unexpected response shape")
    return data


def _threads(graphql, repo, number):
    owner, name = repo.split("/", 1)
    data = graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number})
    try:
        if data.get("errors"):
            raise CollectError(f"review threads: {str(data['errors'])[:200]}")
        box = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes, more = box["nodes"], box["pageInfo"]["hasNextPage"]
    except (AttributeError, KeyError, TypeError):
        raise CollectError("review threads: unexpected response shape")
    if more or not isinstance(nodes, list) or len(nodes) > PAGE:
        raise CollectError("review threads: more than 100, not all of them seen")
    out = []
    for node in nodes:
        first = ((node.get("comments") or {}).get("nodes") or [None])[0] or {}
        out.append({"id": node.get("id"), "isResolved": bool(node.get("isResolved")),
                    "author": (first.get("author") or {}).get("login"), "body": first.get("body")})
    return out


def summary_commits(comment):
    """Short commits of the COMPLETED rows of a Codex review summary comment (the comment carries
    the summary marker; a table row counts when its status cell says **Completed** and a cell is
    exactly one `short sha`). In progress and other rows are no evidence."""
    body = pr_review.text(comment)
    if SUMMARY_MARKER not in body:
        return []
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not any("**Completed**" in c for c in cells):
            continue
        out += [m.group(1) for c in cells for m in [COMMIT_CELL.fullmatch(c)] if m]
    return out


def collect(repo, number, head, api, graphql):
    """The facts of PR `number` at `head`. A PR that is not open, or whose head is not `head`,
    returns early with the `pr` fact only: nothing else is worth collecting then (evaluate
    answers `wait`/`fail` from it)."""
    pr = _object(api, f"repos/{repo}/pulls/{number}")
    facts = {"pr": {"state": pr.get("state"), "merged": bool(pr.get("merged")),
                    "draft": bool(pr.get("draft")), "head": (pr.get("head") or {}).get("sha")}}
    if facts["pr"]["state"] != "open" or facts["pr"]["head"] != head:
        return facts
    facts["check_runs"] = _paged(api, f"repos/{repo}/commits/{head}/check-runs", "check_runs")
    facts["reviews"] = _paged(api, f"repos/{repo}/pulls/{number}/reviews")
    facts["review_comments"] = _paged(api, f"repos/{repo}/pulls/{number}/comments")
    facts["issue_comments"] = _paged(api, f"repos/{repo}/issues/{number}/comments")
    resolved = {}
    for comment in facts["issue_comments"]:
        for short in (summary_commits(comment) if pr_review.trusted(comment, CODEX) else []):
            if short in resolved:
                continue
            try:
                resolved[short] = _object(api, f"repos/{repo}/commits/{short}").get("sha")
            except CollectError:
                resolved[short] = None  # an unknown or ambiguous short sha is not the head
    facts["resolved"] = resolved
    facts["threads"] = _threads(graphql, repo, number)
    return facts


def critical(facts):
    """What the verdict depends on, reduced to comparable values: two collections of the same PR
    state must give equal results, whatever changed in between (a rerun, a new finding) must not."""
    if facts.get("error") or "check_runs" not in facts:
        return ("raw", json.dumps(facts, sort_keys=True, default=str))
    pick = lambda items, keys: [tuple(i.get(k) for k in keys) for i in items or []]  # noqa: E731
    who = lambda i: (i.get("user") or i.get("author") or {}).get("login")  # noqa: E731
    return json.dumps({
        "pr": facts["pr"],
        "runs": pick(facts["check_runs"], ("id", "name", "status", "conclusion")),
        "reviews": [(r.get("id"), r.get("state"), r.get("commit_id"), who(r)) for r in facts["reviews"]],
        "comments": [(c.get("id"), c.get("commit_id"), c.get("original_commit_id"), c.get("body"), who(c)) for c in facts["review_comments"]],
        "issue": [(c.get("id"), c.get("body"), who(c)) for c in facts["issue_comments"]],
        "resolved": sorted((facts["resolved"] or {}).items()),
        "threads": [(t.get("id"), t.get("isResolved")) for t in facts["threads"]],
    }, sort_keys=True, default=str)


def gather(repo, number, head, api, graphql):
    """collect() that never raises: a failed collection is {"error": reason}."""
    try:
        return collect(repo, number, head, api, graphql)
    except CollectError as e:
        return {"error": str(e)}


# ---------- Codex and checks ----------

def codex_on_head(facts, head):
    """Has the Codex bot finished its review of exactly `head`? -> {done, how, p01: [url], findings}."""
    how = []
    for review in facts.get("reviews") or []:
        if (pr_review.trusted(review, CODEX) and review.get("commit_id") == head
                and (review.get("state") or "").upper() in OK_REVIEW_STATES):
            how.append("review")
    for comment in facts.get("issue_comments") or []:  # only the review summary, never a "clean" comment or a thumb
        if pr_review.trusted(comment, CODEX) and any(
                (facts.get("resolved") or {}).get(short) == head for short in summary_commits(comment)):
            how.append("review summary")
    inline = [c for c in facts.get("review_comments") or []
              if pr_review.trusted(c, CODEX) and c.get("original_commit_id") == head and pr_review.text(c)]
    # `commit_id` of an open inline comment is moved by GitHub to the latest commit of the PR; only
    # `original_commit_id` says which commit the review was written on
    p01 = [pr_review.url(c) or "" for c in inline if P01.search(c.get("body") or "")]
    return {"done": bool(how), "how": how[0] if how else "", "p01": p01, "findings": len(inline)}


def old_p01(facts):
    """Open review threads whose first comment carries a P0/P1 badge. At the gate the ones written on
    HEAD have already failed it, so what is left is a finding from an earlier commit: not a blocker,
    but the owner who closes the threads should see it."""
    return sum(1 for t in facts.get("threads") or []
               if not t.get("isResolved") and P01.search(t.get("body") or ""))


def checks(check_runs):
    """-> {total, pending: [names], failed: [(name, conclusion)]}. Only a completed run with the
    conclusion `success` is a pass (the status string alone says nothing: grabla 10.4)."""
    pending, failed = [], []
    for run in check_runs or []:
        name = run.get("name") or "?"
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") != "success":
            failed.append((name, run.get("conclusion")))
    return {"total": len(check_runs or []), "pending": pending, "failed": failed}


# ---------- manifest ----------

def _bound(result, head, fingerprint_now):
    return (isinstance(result, dict) and result.get("head") == head
            and result.get("tree_fingerprint") == fingerprint_now)


def manifest_problems(manifest, head, cwd_fingerprint):
    """Why the manifest does not vouch for `head` (empty list: it does)."""
    if not isinstance(manifest, dict):
        return ["manifest отсутствует или нечитаем"]
    problems = []
    if manifest.get("head") != head:
        problems.append(f"manifest.head {str(manifest.get('head'))[:12]} ≠ HEAD PR {head[:12]}")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        return problems + ["в manifest нет задач"]
    if not cwd_fingerprint:
        problems.append("отпечаток дерева рабочей копии не получен")
    for name, entry in sorted(tasks.items()):
        entry = entry if isinstance(entry, dict) else {}
        results = entry.get("results") if isinstance(entry.get("results"), dict) else {}
        status = entry.get("status")
        if status in ("needs_decision", "blocked"):
            problems.append(f"задача {name}: статус {status}")
        tester = results.get("tester")
        if not (isinstance(tester, dict) and tester.get("status") == "pass" and tester.get("head") == head):
            problems.append(f"задача {name}: нет tester pass на HEAD PR")
        elif not _bound(tester, head, cwd_fingerprint):
            problems.append(f"задача {name}: tester pass получен на другом дереве, чем рабочая копия")
        review = results.get("cross_provider_reviewer")
        if not (isinstance(review, dict) and review.get("head") == head):
            problems.append(f"задача {name}: нет cross_provider_reviewer на HEAD PR")
        elif not _bound(review, head, cwd_fingerprint):
            problems.append(f"задача {name}: cross_provider_reviewer получен на другом дереве, чем рабочая копия")
        elif review.get("status") != "pass" and not _accepted(entry, review):
            problems.append(f"задача {name}: cross_provider_reviewer {review.get('status')} без решения "
                            f"владельца accept_limitation по этому результату")
    return problems


def _accepted(entry, review):
    """A decision made on THIS result: source cross_provider_reviewer, accept_limitation, recorded
    no earlier than the result. Older decisions, other sources, invariant/cut_surface: no."""
    if review.get("status") != "findings":
        return False
    made = _ts(review.get("recorded_at"))
    for decision in entry.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        when = _ts(decision.get("recorded_at"))
        if (decision.get("source") == "cross_provider_reviewer" and decision.get("decision") == "accept_limitation"
                and made and when and when >= made):
            return True
    return False


# ---------- verdict ----------

def _verdict(verdict, reasons, head, facts, **extra):
    pr = facts.get("pr") or {}
    threads = facts.get("threads") or []
    out = {"verdict": verdict, "reasons": reasons, "draft": bool(pr.get("draft")), "old_p01": old_p01(facts),
           "unresolved": [t["id"] for t in threads if not t.get("isResolved")], "pr": pr, "head": head}
    out.update(extra)
    return out


def evaluate(facts, head, manifest, workdir_state):
    """The merge gate. `workdir_state`: {clean, head, fingerprint} of the wave's working copy."""
    if facts.get("error"):
        return _verdict("wait", [f"сбор фактов: {facts['error']}"], head, facts)
    pr = facts.get("pr") or {}
    if pr.get("merged"):
        return _verdict("fail", ["PR уже смержен вне гейта"], head, facts)
    if pr.get("state") != "open":
        return _verdict("fail", [f"PR закрыт без мерджа (state={pr.get('state')})"], head, facts)
    if pr.get("head") != head:
        return _verdict("wait", [f"HEAD PR {str(pr.get('head'))[:12]} ≠ {head[:12]}"], head, facts)
    runs = checks(facts.get("check_runs"))
    if not runs["total"]:
        return _verdict("wait", ["на HEAD нет check-runs"], head, facts)
    if runs["pending"]:
        return _verdict("wait", [f"проверки не завершены: {', '.join(sorted(runs['pending']))}"], head, facts)
    if runs["failed"]:
        return _verdict("fail", [f"проверки неуспешны: " + ", ".join(f"{n} ({c})" for n, c in sorted(runs["failed"]))],
                        head, facts)
    codex = codex_on_head(facts, head)
    if not codex["done"]:
        return _verdict("wait", ["Codex не завершил ревью HEAD"], head, facts)
    if codex["p01"]:
        return _verdict("fail", [f"Codex: {len(codex['p01'])} замечаний P0/P1 на HEAD"], head, facts)
    problems = []
    work = workdir_state if isinstance(workdir_state, dict) else {}
    if not work.get("clean"):
        problems.append("рабочая копия волны не чистая или не прочитана")
    if work.get("head") != head:
        problems.append(f"HEAD рабочей копии {str(work.get('head'))[:12]} ≠ HEAD PR {head[:12]}")
    problems += manifest_problems(manifest, head, work.get("fingerprint"))
    if problems:
        return _verdict("fail", problems, head, facts)
    return _verdict("pass", [], head, facts)


# ---------- merge and owner script ----------

def _check(repo, number, sha):
    if not (isinstance(repo, str) and REPOSITORY.fullmatch(repo)):
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"PR number must be a positive integer, got {number!r}")
    if not (isinstance(sha, str) and SHA.fullmatch(sha)):
        raise ValueError(f"sha must be 40-64 lowercase hex characters, got {sha!r}")


def merge_argv(repo, number, sha):
    _check(repo, number, sha)
    return ["gh", "pr", "merge", str(number), "--repo", repo, "--squash", "--match-head-commit", sha]


def merge_command(repo, number, sha):
    return shlex.join(merge_argv(repo, number, sha))


def ready_command(repo, number):
    return shlex.join(["gh", "pr", "ready", str(number), "--repo", repo])


def owner_script(wab_py, chain_file, wave, run_id, sha):
    """The script the owner runs when the gate passed but review threads are open. It only starts
    `wab.py owner-merge <chain.json> <wave> <run_id> <sha>`, which refuses another run or another
    gated sha, gates the PR AGAIN and only then resolves the threads, un-drafts and merges."""
    for name, value, rx in (("wave", wave, WAVE), ("run_id", run_id, RUN_ID), ("sha", sha, SHA)):
        if not (isinstance(value, str) and rx.fullmatch(value)):
            raise ValueError(f"{name} is not valid: {value!r}")
    q = shlex.quote
    return ("#!/usr/bin/env bash\n"
            f"# wab: gated merge of the PR of wave {wave} (run {run_id}, gated {sha[:12]}); generated, run by the owner\n"
            "set -euo pipefail\n"
            f"exec python3 {q(str(wab_py))} owner-merge {q(str(chain_file))} {q(wave)} {q(run_id)} {q(sha)}\n")


def threads_note(old):
    return f" (из них P0/P1 из прошлых коммитов: {old})" if old else ""


def alarm_ready(facts, head):
    """Checks all completed (at least one) and Codex finished on `head`: time to wake the wave."""
    if facts.get("error") or (facts.get("pr") or {}).get("head") != head or "check_runs" not in facts:
        return False
    runs = checks(facts["check_runs"])
    return bool(runs["total"]) and not runs["pending"] and codex_on_head(facts, head)["done"]


def alarm_text(number, head, facts):
    runs = checks(facts.get("check_runs"))
    codex = codex_on_head(facts, head)
    bad = ", ".join(f"{n} ({c})" for n, c in runs["failed"]) or "нет"
    verdict = "чисто" if not codex["findings"] else f"{codex['findings']} замечаний, P0/P1: {len(codex['p01'])}"
    open_threads = sum(1 for t in facts.get("threads") or [] if not t.get("isResolved"))
    return (f"[wab] Будильник: PR #{number} (HEAD {head[:12]}) — проверки завершены: "
            f"{runs['total'] - len(runs['failed'])} ok, неуспешные: {bad}; Codex: {verdict}; "
            f"незакрытых тредов: {open_threads}{threads_note(old_p01(facts))}. Разбери и продолжай по протоколу.")
