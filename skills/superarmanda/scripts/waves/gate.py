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
MANIFEST_MISSING = "manifest отсутствует или нечитаем"
MERGED_OUTSIDE = "PR уже смержен вне гейта"  # wab.py names `owner-handover` in the BLOCKED line of this reason
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


DEPTH_MARK = "[скрыто: глубина]"
HIDDEN = "[скрыто]"


def key_hides_value(key):
    """Whether the value under a dict key is hidden. wab.py replaces this with its predicate (_key_hides_value: the
    invisible characters and the secret words of redact live there) when it is imported; used alone, gate.py hides
    EVERY value (keys only): the safe side."""
    return True


def _leaves(obj, depth=0):
    """Every str leaf (and dict key) of a nested answer, AS IT IS: str()/repr() of a list writes an invisible
    character as a literal `\\u200b`, which wab's mask cannot find; wab masks the whole message where it shows it."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, (dict, list, tuple, set, frozenset)) and depth >= 20:
        yield DEPTH_MARK  # never str()/repr() of what is left: it would write an invisible character as `\\u200b`
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(k, depth + 1)
            if key_hides_value(k):  # the value under a secret or invisible key is not shown at all
                yield HIDDEN
            else:
                yield from _leaves(v, depth + 1)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from _leaves(v, depth + 1)
    elif obj is not None:
        yield str(obj)


def _exc_str(e):
    """An exception as text WITHOUT str(e) (str(KeyError("password<ZWSP>=x")) is the repr of the string): the leaves
    of its args as they are, joined; wab masks the whole message where it shows it."""
    return " ".join(x for a in e.args for x in _leaves(a))


def _threads(graphql, repo, number):
    owner, name = repo.split("/", 1)
    data = graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number})
    try:
        if data.get("errors"):
            raise CollectError(f"review threads: {' '.join(_leaves(data['errors']))}")
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
                    "draft": bool(pr.get("draft")), "head": (pr.get("head") or {}).get("sha"),
                    "base": (pr.get("base") or {}).get("ref")}}
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
        "reviews": [(r.get("id"), r.get("state"), r.get("commit_id"), r.get("body"), who(r)) for r in facts["reviews"]],
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
        return {"error": _exc_str(e)}


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
    # a P0/P1 badge may also sit in the body of a review written on HEAD, not only in its inline comments
    bodies = [r for r in facts.get("reviews") or []
              if pr_review.trusted(r, CODEX) and r.get("commit_id") == head and P01.search(r.get("body") or "")]
    p01 = ([pr_review.url(c) or "" for c in inline if P01.search(c.get("body") or "")]
           + [pr_review.url(r) or "" for r in bodies])
    return {"done": bool(how), "how": how[0] if how else "", "p01": p01, "findings": len(inline) + len(bodies)}


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


# Field shapes the gate reads. A manifest that breaks one is refused with a reason:
# the gate never guesses what a malformed record meant, and never raises on it.
_TASK_FIELD_TYPES = {
    "results": dict, "decisions": list, "deferrals": list, "acceptances": list,
    "fix_sources": dict, "session_roles": dict,
}


def _shape_problems(manifest, tasks):
    out = []
    if "run" in manifest and not isinstance(manifest["run"], dict):
        out.append("manifest: поле run не объект")
    for name, entry in sorted(tasks.items()):
        if not isinstance(entry, dict):
            out.append(f"manifest: задача {name} не объект")
            continue
        for field, kind in _TASK_FIELD_TYPES.items():
            if field in entry and not isinstance(entry[field], kind):
                out.append(f"manifest: задача {name}: поле {field} не {'список' if kind is list else 'объект'}")
        cycles = entry.get("fix_cycles")
        if "fix_cycles" in entry and (isinstance(cycles, bool) or not isinstance(cycles, int)):
            out.append(f"manifest: задача {name}: поле fix_cycles не число")
    return out


# What the reasons of the gate tell the wave to do (1.2.2, #86). The gate names the action, not only the fact.
NEW_RUN = "новый прогон волны с `state.py init` на текущей версии скилла"
TWO_REVIEWS = ("нужны два gate_ready-ревью текущего HEAD: claude-host (Astra) и codex-host (Fable; codex-host-opus "
               "только с quota evidence) — запусти недостающее ревью review.py и запиши его "
               "`state.py task-result --role second_reviewer`")
# Fields of a result that only the policy of manifest version 2 writes (state.py result/verified_review).
V2_RESULT_KEYS = ("model", "profile", "artifact_sha256", "review_session_id", "fallback_for", "quota_evidence")
# Fields state.py writes only into a task review of a HIGH-risk task (verified_review); `model` is not one of them.
HIGH_RESULT_KEYS = ("profile", "artifact_sha256", "review_session_id", "fallback_for", "quota_evidence")


def _high_traces(name, entry, results):
    """What a task carries of the high policy: state.py keeps `review_history` and the fields of a verified
    report only for a task whose risk is high, and a risk is never lowered by it. On a task judged below high
    they mean the risk (the task's own or the level of the run) was lowered by hand. The role `second_reviewer`
    alone is no trace: below high it is an optional review."""
    traces = [f"{name}.review_history"] if entry.get("review_history") else []
    for role in state.REVIEW_ROLES:
        res = results.get(role)
        if isinstance(res, dict):
            traces += [f"{name}.{role}.{key}" for key in HIGH_RESULT_KEYS if key in res]
    return traces


def _wave_copy_problem(manifest):
    """A version 2 manifest of `init --from-plan` carries the wave (`wave`) and its digest (`plan.wave_sha256`):
    a copy that no longer matches the digest was edited. Not a signature (both are in one unsigned file); it
    stops the careless edit of `wave.risk` alone."""
    if "wave" not in manifest and "plan" not in manifest:
        return None
    wave, plan = manifest.get("wave"), manifest.get("plan")
    try:
        same = isinstance(wave, dict) and isinstance(plan, dict) and state.canonical_sha256(wave) == plan.get("wave_sha256")
    except Exception:  # noqa: BLE001 - a value json cannot encode: not the copy state.py wrote
        same = False
    return None if same else (f"manifest: копия волны (`wave`) не совпадает с `plan.wave_sha256` этого manifest: её "
                              f"правили вручную; нужен {NEW_RUN}")


def _v2_traces(manifest, tasks):
    """What a manifest that calls itself version 1 carries of version 2: a run created as version 2 and
    lowered by hand keeps such traces (state.py never writes them into version 1)."""
    traces = ["review_policy"] if "review_policy" in manifest else []
    for name, entry in sorted(tasks.items()):
        if not isinstance(entry, dict):
            continue
        traces += [f"{name}.{key}" for key in state.V2_TASK_KEYS if key in entry]
        results = entry.get("results") if isinstance(entry.get("results"), dict) else {}
        roles = entry.get("session_roles") if isinstance(entry.get("session_roles"), dict) else {}
        used = set(results) | {r for r in roles.values() if isinstance(r, str)}
        traces += [f"{name}.{role}" for role in sorted(state.V2_ONLY_ROLES & used)]
        for role, res in sorted(results.items()):
            if isinstance(res, dict):
                traces += [f"{name}.{role}.{key}" for key in V2_RESULT_KEYS if key in res]
    return traces


def _policy_problems(manifest, tasks, plan_risk):
    """(problems, version, wave_risk) of the review policy of the manifest against the risk of the wave.
    version None: the manifest cannot be judged by any rules (the tasks are not looked at for readiness).

    The risk of the wave is the HIGHEST of what is known about it: the approved plan under the pin
    (`plan_risk`), and for a manifest version 2 also the copy of the wave that `init --from-plan` put into it
    (`wave.risk`) and the level of the run (`review_policy.level`). So neither a pin removed from chain.json of
    a running chain nor a lowered level makes a high wave judged as a lower one. A manifest version 1 has no
    policy: a live 1.2.0 manifest carries `wave.risk: high` too, so there only the plan raises the rules."""
    version = manifest.get("version", 1)  # no field: the shape of before the policy, judged as version 1
    if isinstance(version, bool) or version not in state.MANIFEST_VERSIONS:
        return ["manifest: неизвестная версия: этот гейт её не судит"], None, plan_risk
    if version == 2:
        try:
            state.check_schema(manifest)  # the one schema check of state.py, not a copy
        except SystemExit as e:
            return [f"manifest: {_exc_str(e).removeprefix('state: ')}"], None, plan_risk
        level = manifest["review_policy"]["level"]
        wave = manifest.get("wave")
        copied = wave.get("risk") if isinstance(wave, dict) else None
        problems = [p for p in [_wave_copy_problem(manifest)] if p]
        if not (isinstance(copied, str) and copied in state.RISKS):  # a list or an object is not hashable: no `in` first
            if isinstance(wave, dict):
                problems.append(f"manifest: `wave.risk` не строка из {'|'.join(state.RISK_ORDER)}: копию волны "
                                f"правили вручную или manifest повреждён; нужен {NEW_RUN}")
            copied = None
        known = {"одобренному waves.json": plan_risk, "копии волны в manifest (`wave.risk`)": copied}
        wave_risk = max((r for r in (plan_risk, copied, level) if r), key=state.RISK_ORDER.index)
        if wave_risk == "high" and level != "high":
            source = next(text for text, risk in known.items() if risk == "high")
            problems.append(f"волна high по {source}, а review_policy.level в manifest — {level}: уровень "
                            f"занижен; нужен {NEW_RUN} (`--from-plan` берёт риск волны)")
        problems += _old_policy_problems(manifest, tasks, wave_risk)
        return problems, 2, wave_risk
    problems = []
    traces = _v2_traces(manifest, tasks)
    if traces:
        problems.append(f"manifest version 1 несёт записи версии 2 ({', '.join(traces)}): правила ревью "
                        f"понижены вручную или manifest повреждён; нужен {NEW_RUN}")
    if plan_risk == "high":
        problems.append(f"волна high по одобренному waves.json, а manifest version 1: два ревью по нему не "
                        f"проверить; нужен {NEW_RUN}")
    return problems, 1, plan_risk


def _old_policy_problems(manifest, tasks, wave_risk):
    """A manifest of a review policy that does not ask a high-risk task for `internal_reviewer` and
    `final_check` (1.2.1, written by 1.2.1-1.2.3) where this gate does: the wave is high, or a task has the
    high policy level in any wave. The version of the policy in the manifest never weakens the gate: such a
    run was led by the old rules, so the answer is a new run, not the records added afterwards. Below high
    without high tasks the old policy asks for the same as the new one: judged as before."""
    version = state.policy_version(manifest)
    if version in state.MANDATORY_ROLES_POLICIES:
        return []
    high = [name for name, entry in sorted(tasks.items())
            if isinstance(entry, dict) and state.task_risk(manifest, entry) == "high"]
    if wave_risk != "high" and not high:
        return []
    why = "волна high" if wave_risk == "high" else f"задача high ({', '.join(high)})"
    return [f"manifest: политика ревью {version} (`review_policy.version`) не требует `internal_reviewer` и "
            f"`final_check`, а {why}: прогон шёл по старым правилам; нужен {NEW_RUN}"]


def _high_problems(name, entry, results, head, cwd_fingerprint, wave_risk, policy):
    """A task judged as high (the wave is high by the plan, or the task's own policy is): both task reviews on
    HEAD and the rules of state.py over them (high_risk_gaps: models, verified reports, the pair, the quota
    evidence of Opus, findings and a Fable review kept by review_history; fable_role_gaps of the policy 1.2.4:
    the internal review before the first external packet and the final check after the last task review).
    Never the saved status."""
    why = "волна high" if wave_risk == "high" else "задача high"
    problems = []
    second = results.get("second_reviewer")
    if not (isinstance(second, dict) and second.get("head") == head):
        problems.append(f"задача {name}: нет second_reviewer на HEAD PR ({why}): {TWO_REVIEWS}")
    elif not _bound(second, head, cwd_fingerprint):
        problems.append(f"задача {name}: second_reviewer получен на другом дереве, чем рабочая копия")
    elif second.get("status") != "pass" and not _covered(entry, "second_reviewer", second):
        problems.append(f"задача {name}: second_reviewer {second.get('status')} без fix-loop --defer/--accept по "
                        f"этому результату ({why}): {TWO_REVIEWS}")
    try:
        current = state.current_results(dict(entry, results={r: v for r, v in results.items() if isinstance(v, dict)}),
                                        head, cwd_fingerprint)
        for role, gap in sorted(state.high_risk_gaps(entry, current, head).items()):
            problems.append(f"задача {name}: {role} ({why}): {gap}")
        for role, gap in sorted(state.fable_role_gaps(entry, current, head, policy).items()):
            action = f"; нужен {NEW_RUN}" if gap.startswith(state.NEW_RUN_REQUIRED) else ""
            problems.append(f"задача {name}: {role} ({why}): {gap}{action}")
        history = entry.get("review_history")
        known = {item.get("result_sha256") for item in history if isinstance(item, dict) and item.get("head") == head} \
            if isinstance(history, list) else set()
        for role in state.REVIEW_ROLES:
            if role in current and state.result_digest(current[role]) not in known:
                problems.append(f"задача {name}: {role}: текущего результата нет в review_history этого HEAD "
                                f"(manifest правили вручную?); запиши ревью заново через `state.py task-result`")
    except Exception:  # noqa: BLE001 - fail closed: records the rules cannot read are a reason, never a pass
        problems.append(f"manifest: задача {name}: записи ревью не разбираются правилами state.py")
    return problems


def manifest_problems(manifest, head, cwd_fingerprint, plan=None):
    """Why the manifest does not vouch for `head` (empty list: it does).

    `plan`: what the approved waves.json (pinned by chain.json `plan_sha256`) says about this wave:
    {"risk": ...}, {"error": reason} (the pinned plan could not be read: a closed refusal, never «not high»)
    or None (a chain without the pin: the rules of 1.2.0 plus the manifest's own policy of version 2).
    The risk of the WAVE comes only from the plan; the manifest gives the review artifacts. In a high wave
    every task is judged as high whatever its own risk; in any wave a task whose own policy (manifest
    version 2) is high is. The saved task status is necessary, never sufficient: readiness is recomputed
    by the functions of state.py."""
    if not isinstance(manifest, dict):
        return [MANIFEST_MISSING]
    problems = []
    plan = plan if isinstance(plan, dict) else {} if plan is None else {"error": "риск волны не получен"}
    if plan.get("error") or (plan and plan.get("risk") not in state.RISKS):
        problems.append(f"план волн: {plan.get('error') or 'риск волны не получен'}")
    plan_risk = plan.get("risk") if not plan.get("error") and plan.get("risk") in state.RISKS else None
    for key in sorted(k for k in manifest if k not in state.MANIFEST_KEYS):
        problems.append(f"manifest: unknown record type {key!r}: this gate cannot judge it")
    if manifest.get("head") != head:
        problems.append(f"manifest.head {str(manifest.get('head'))[:12]} ≠ HEAD PR {head[:12]}")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        return problems + ["в manifest нет задач"]
    problems += _shape_problems(manifest, tasks)
    policy, version, wave_risk = _policy_problems(manifest, tasks, plan_risk)
    problems += policy
    if version is None:
        return problems
    review_policy = state.policy_version(manifest) if version == 2 else None  # the rules the manifest is judged by
    if not cwd_fingerprint:
        problems.append("отпечаток дерева рабочей копии не получен")
    for name, entry in sorted(tasks.items()):
        entry = entry if isinstance(entry, dict) else {}
        for key in sorted(k for k in entry if k not in state.TASK_KEYS):
            problems.append(f"manifest: unknown record type {key!r} in task {name}: this gate cannot judge it")
        results = entry.get("results") if isinstance(entry.get("results"), dict) else {}
        status = entry.get("status")
        review = results.get("cross_provider_reviewer")
        # the risk the task is judged by: high for every task of a high wave; else the task's own (version 2)
        risk = None if version == 1 else "high" if wave_risk == "high" else state.task_risk(manifest, entry)
        if status != "ready_for_pr_review" and not _needs_fix_explained(entry, review, head, cwd_fingerprint):
            problems.append(f"задача {name}: статус {status} (цикл исправлений не завершён)")
        elif version == 2 and status == "ready_for_pr_review" and not _ready_by_state(entry, results, risk, head, review_policy):
            problems.append(f"задача {name}: статус ready_for_pr_review не подтверждён пересчётом по правилам "
                            f"state.py (риск {risk}); гейт не верит сохранённому статусу")
        for role, other in sorted(results.items()):  # github_codex_review, coderabbit, ...
            if (role not in ("coder", "tester", "cross_provider_reviewer") and isinstance(other, dict)
                    # the Fable subagent roles are records here. `internal_reviewer` and `final_check` of a task
                    # judged as high are judged below by state.fable_role_gaps (policy 1.2.4), with a reason
                    # that names the action; a manifest of the policy 1.2.1 there is refused as a whole
                    and role not in state.FABLE_ROLES
                    and other.get("head") == head and other.get("status") != "pass"
                    and not (role == "coderabbit" and other.get("status") == "unavailable")
                    # high: judged below with its own reasons; below high the second review is optional, like in
                    # state.py derive_step: only its findings need a disposition
                    and not (role == "second_reviewer" and (risk == "high" or other.get("status") != "findings"))
                    and not _covered(entry, role, other)):
                problems.append(f"задача {name}: {role} {other.get('status')}")
        if risk == "high":
            problems += _high_problems(name, entry, results, head, cwd_fingerprint, wave_risk, review_policy)
        elif version == 2 and _high_traces(name, entry, results):
            problems.append(f"задача {name}: несёт записи политики high ({', '.join(_high_traces(name, entry, results))}), "
                            f"а её риск в manifest — {risk}: риск задачи или review_policy.level понижен вручную; "
                            f"нужен {NEW_RUN}")
        coder = results.get("coder")
        if not (isinstance(coder, dict) and coder.get("status") == "pass" and coder.get("head") == head):
            problems.append(f"задача {name}: нет coder pass на HEAD")
        elif not _bound(coder, head, cwd_fingerprint):
            problems.append(f"задача {name}: coder pass получен на другом дереве, чем рабочая копия")
        tester = results.get("tester")
        if not (isinstance(tester, dict) and tester.get("status") == "pass" and tester.get("head") == head):
            problems.append(f"задача {name}: нет tester pass на HEAD PR")
        elif not _bound(tester, head, cwd_fingerprint):
            problems.append(f"задача {name}: tester pass получен на другом дереве, чем рабочая копия")
        if not (isinstance(review, dict) and review.get("head") == head):
            problems.append(f"задача {name}: нет cross_provider_reviewer на HEAD PR")
        elif not _bound(review, head, cwd_fingerprint):
            problems.append(f"задача {name}: cross_provider_reviewer получен на другом дереве, чем рабочая копия")
        elif review.get("status") != "pass" and not _accepted(entry, review):
            problems.append(f"задача {name}: cross_provider_reviewer {review.get('status')} без решения "
                            f"владельца accept_limitation или --defer по этому результату")
    return problems


def _as_list(value):
    return value if isinstance(value, list) else []


def _ready_by_state(entry, results, risk, head, policy):
    """state.task_ready over the manifest's records: the readiness rule of state.py itself, not a copy.
    Records it cannot read are «not ready»."""
    try:
        return state.task_ready(dict(entry, results={r: v for r, v in results.items() if isinstance(v, dict)}),
                                risk, head, policy)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _needs_fix_explained(entry, review, head, fingerprint_now):
    """`needs_fix` is allowed only as the accepted-limitation path: the reviewer's findings on HEAD are
    accepted and the LAST decision is accept_limitation of the reviewer. A later failed fix-loop leaves
    no timestamp in the manifest, so it is told apart by its traces only: another source in
    `fix_sources`, `fix_cycles` beyond the reviewer's count, or status needs_decision/blocked.
    Limit: one more failed round on the reviewer itself right after the decision stays needs_fix
    with the same counters shape and is not distinguishable here."""
    if entry.get("status") != "needs_fix" or not (_bound(review, head, fingerprint_now) and _accepted(entry, review)):
        return False
    decisions = [d for d in _as_list(entry.get("decisions")) if isinstance(d, dict) and _ts(d.get("recorded_at"))]
    if not decisions:
        return False
    last = max(decisions, key=lambda d: _ts(d.get("recorded_at")))
    if last.get("source") != "cross_provider_reviewer" or last.get("decision") != "accept_limitation":
        return False
    sources = entry.get("fix_sources") if isinstance(entry.get("fix_sources"), dict) else {}
    own = sources.get("cross_provider_reviewer") or 0
    cycles = entry.get("fix_cycles") or 0
    if not (isinstance(own, int) and isinstance(cycles, int)):
        return False
    return (all(n in (0, None) for k, n in sources.items() if k != "cross_provider_reviewer")
            and cycles <= own)


def _deferred(entry, role, result):
    """`fix-loop --defer` on THIS result: low/P3-only findings of `role` moved to the next wave's
    remainder. The rule is state.is_deferred (bound to the exact result and its head), not a copy."""
    return state.is_deferred(entry, role, result)


def _covered(entry, role, result):
    """Deferred or accepted (`fix-loop --accept`) on THIS result: state.is_covered, not a copy."""
    return state.is_covered(entry, role, result)


def accepted_notes(manifest, head):
    """`accepted <severity> <source>: <note>` for each medium/high acceptance that covers a current
    findings result of the manifest, so the verdict reports what the merge carries as a limitation."""
    notes = []
    tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
    for name, entry in sorted(tasks.items() if isinstance(tasks, dict) else ()):
        if not isinstance(entry, dict) or not isinstance(entry.get("results"), dict):
            continue
        for role, res in sorted(entry["results"].items()):
            if not (isinstance(res, dict) and res.get("head") == head):
                continue
            record = state.accepted_record(entry, role, res)
            if record and record.get("severity") in ("medium", "high"):
                notes.append(f"accepted {record['severity']} {role}: {record.get('note')}")
    return notes


def _accepted(entry, review):
    """A decision made on THIS result: source cross_provider_reviewer, accept_limitation, recorded
    no earlier than the result. Older decisions, other sources, invariant/cut_surface: no.
    A deferral (`fix-loop --defer`) or an acceptance (`fix-loop --accept`) of the reviewer on this
    result counts the same way."""
    if review.get("status") != "findings":
        return False
    if _covered(entry, "cross_provider_reviewer", review):
        return True
    made = _ts(review.get("recorded_at"))
    for decision in _as_list(entry.get("decisions")):
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


def evaluate(facts, head, manifest, workdir_state, base, plan=None):
    """The merge gate. `workdir_state`: {clean, head, fingerprint} of the wave's working copy;
    `base`: the branch the chain expects the PR to merge into (unknown: wait, never pass);
    `plan`: the wave's risk from the approved waves.json (see manifest_problems)."""
    if facts.get("error"):
        return _verdict("wait", [f"сбор фактов: {facts['error']}"], head, facts)
    pr = facts.get("pr") or {}
    if pr.get("merged"):
        return _verdict("fail", [MERGED_OUTSIDE], head, facts)
    if pr.get("state") != "open":
        return _verdict("fail", [f"PR закрыт без мерджа (state={pr.get('state')})"], head, facts)
    if not base:
        return _verdict("wait", ["базовая ветка цепочки неизвестна"], head, facts)
    if not pr.get("base"):
        return _verdict("wait", ["в фактах PR нет базовой ветки"], head, facts)
    if pr.get("base") != base:
        return _verdict("fail", [f"PR перенаправлен на {pr.get('base')}, цепочка ждёт {base}"], head, facts)
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
    problems += manifest_problems(manifest, head, work.get("fingerprint"), plan)
    accepted = accepted_notes(manifest, head)
    if problems:
        return _verdict("fail", problems, head, facts, accepted=accepted)
    return _verdict("pass", list(accepted), head, facts, accepted=accepted)


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
    gated sha, gates the PR AGAIN and only then resolves the threads and, for a draft, makes it ready and stops (the next run gates again and merges)."""
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


def alarm_p01_note(n):
    """The alarm is sent before any gate, so an open P0/P1 thread may be one written on HEAD: count them
    without claiming they come from earlier commits."""
    return f" (из них с P0/P1: {n})" if n else ""


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
            f"незакрытых тредов: {open_threads}{alarm_p01_note(old_p01(facts))}. Разбери и продолжай по протоколу.")
