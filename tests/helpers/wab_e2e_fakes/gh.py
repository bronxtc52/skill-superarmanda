"""Stateful `gh` stand-in for tests/helpers/superarmanda_waves_e2e_test.py.

World: $FAKE_GH_STATE/state.json (repo, path of the bare "origin", PRs) and a call log. The PRs'
branches live in a REAL local bare git repository, so `gh pr merge --squash` really squashes the
branch into the base of that repository, and the next wave's fetch really sees the merge commit.

Per PR the test sets what GitHub would show for the head: "ci" (none|in_progress|failure|success),
"codex" (none|stale|head: the Codex review is absent / written on an older commit / on the head),
"draft". Nothing is checked by the stand-in at merge time (GitHub without branch protection merges
a red PR too): it RECORDS in merges.jsonl what was true at that moment, and the test's oracle judges.
Exit 2 and a log entry for any verb the dispatcher is not expected to use.

Bodies of the answers are LIVE GitHub answers (tests/fixtures/github/, README there): the stand-in
only fills in the values it owns (sha, number, state, draft, check-run status/conclusion, which Codex
evidence is present). The only answers not taken from GitHub: `gh pr merge` (a mutation, prints
nothing), `gh pr create` (prints the URL of the new PR), `gh pr ready`.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

STATE = os.environ["FAKE_GH_STATE"]
FIXTURES = pathlib.Path(os.environ["FAKE_GH_FIXTURES"])
CODEX_BOT = ("chatgpt-codex-connector[bot]", "chatgpt-codex-connector")
ZERO = "0" * 40


def jlog(name, obj):
    with open(os.path.join(STATE, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load():
    with open(os.path.join(STATE, "state.json"), encoding="utf-8") as f:
        return json.load(f)


def save(st):
    tmp = os.path.join(STATE, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, os.path.join(STATE, "state.json"))


def fail(msg, code=1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def git(origin, *args, cwd=None, check=True):
    cmd = ["git", "--git-dir", origin, *args] if cwd is None else ["git", *args]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        fail(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else None


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def head_of(st, pr):
    return git(st["origin"], "rev-parse", "--verify", "-q", f"refs/heads/{pr['branch']}", check=False)


def fixture(name, st, pr=None, head=None, **extra):
    """A live answer from tests/fixtures/github with the placeholders filled in (README there)."""
    pr = pr or {}
    head = head or (head_of(st, pr) if pr else None) or ZERO
    stale = (git(st["origin"], "rev-parse", f"{head}~1", check=False) if head != ZERO else None) or ZERO
    owner, _, repo = st["repo"].partition("/")
    base = (git(st["origin"], "rev-parse", "--verify", "-q", f"refs/heads/{pr['base']}", check=False) if pr else None) or ZERO
    subs = {"{REPO}": st["repo"], "{OWNER}": owner, "{NAME}": repo, "{NODE}": "N", "{BASE}": base,
            "{HEAD}": head, "{HEAD7}": head[:7], "{HEAD10}": head[:10],
            "{STALE}": stale, "{STALE7}": stale[:7], "{STALE10}": stale[:10],
            "{MERGE}": pr.get("merge_commit") or ZERO, "{NUMBER}": str(pr.get("number", 0)), **extra}
    text = (FIXTURES / name).read_text(encoding="utf-8").replace('"{NUMBER}"', subs["{NUMBER}"])
    for key, value in subs.items():
        text = text.replace(key, value)
    return json.loads(text)


def as_json(st, pr, name="pr-view.json"):
    """`gh pr view|list --json …`: the live answer, the values of the PR the stand-in owns overwritten."""
    obj = fixture(name, st, pr)
    items = obj if isinstance(obj, list) else [obj]
    for item in items:
        item.update({"isDraft": bool(pr.get("draft")), "state": pr["state"], "baseRefName": pr["base"]})
        if "mergeCommit" in item:
            mc = pr.get("merge_commit")
            item["mergeCommit"] = {"oid": mc} if mc else None
    return obj


def find(st, ref):
    if str(ref).isdigit():
        return st["prs"].get(str(ref))
    found = [p for p in st["prs"].values() if p["branch"] == ref]
    return max(found, key=lambda p: p["number"]) if found else None


def pick(obj, fields):
    return {k: obj[k] for k in fields.split(",")}


def pr_cmd(st, args):
    sub, rest = args[0], args[1:]
    repo = opt(rest, "--repo")
    if repo and repo != st["repo"]:
        fail(f"unexpected --repo {repo}")
    if sub == "create":  # played by the wave session
        n = len(st["prs"]) + 1
        st["prs"][str(n)] = {"number": n, "branch": opt(rest, "--head"), "base": opt(rest, "--base"),
                             "draft": "--draft" in rest, "state": "OPEN", "ci": "none", "codex": "none"}
        save(st)
        print(f"https://github.com/{st['repo']}/pull/{n}")
    elif sub == "list":
        want = [p for p in st["prs"].values() if p["branch"] == opt(rest, "--head")
                and p["base"] == opt(rest, "--base") and head_of(st, p)]
        print(json.dumps([pick(as_json(st, p, "pr-list.json")[0], opt(rest, "--json")) for p in want]))
    elif sub == "view":
        pr = find(st, rest[0])
        if pr is None:
            fail(f"no pull requests found for {rest[0]}")
        print(json.dumps(pick(as_json(st, pr), opt(rest, "--json"))))
    elif sub == "ready":
        pr = find(st, rest[0])
        pr["draft"] = False
        save(st)
    elif sub == "merge":
        merge(st, find(st, rest[0]), rest)
    else:
        unexpected(args)


def merge(st, pr, rest):
    if pr is None or pr["state"] != "OPEN":
        fail("pull request is not open")
    head = head_of(st, pr)
    if "--squash" not in rest:
        fail("only --squash is expected")
    if opt(rest, "--match-head-commit") != head:
        fail("Head branch was modified. Review and try the merge again.")
    record = {"pr": pr["number"], "sha": head, "ci": pr["ci"], "codex": pr["codex"], "draft": bool(pr["draft"]),
              "argv": rest}
    work = tempfile.mkdtemp(dir=STATE, prefix="merge-")
    try:
        git(None, "clone", "-q", st["origin"], work, cwd=STATE)
        base = pr["base"]
        git(None, "checkout", "-q", base, cwd=work)
        git(None, "merge", "--squash", "-q", f"origin/{pr['branch']}", cwd=work)
        git(None, "commit", "-q", "-m", f"{pr['branch']} (#{pr['number']})", cwd=work)
        record["merge_commit"] = git(None, "rev-parse", "HEAD", cwd=work)
        git(None, "push", "-q", "origin", f"HEAD:refs/heads/{base}", cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    pr["state"], pr["merge_commit"] = "MERGED", record["merge_commit"]
    save(st)
    jlog("merges.jsonl", record)


def api_cmd(st, args):
    path = args[0].split("?")[0]
    if path == "graphql":
        number = int(next(a.split("=", 1)[1] for a in args if a.startswith("number=")))
        pr = st["prs"].get(str(number))
        out = fixture("review-threads.json", st, pr)
        if pr is None or pr["codex"] == "none":  # no Codex review, so no thread of it
            out["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] = []
        print(json.dumps(out))
        return
    parts = path.split("/")  # repos/<owner>/<name>/...
    if parts[:3] != ["repos", *st["repo"].split("/")]:
        fail(f"unexpected api path {path}")
    tail = parts[3:]
    if tail[0] == "commits" and len(tail) == 2:  # `commits/<short>`: how the gate resolves a short sha
        full = git(st["origin"], "rev-parse", "--verify", "-q", f"{tail[1]}^{{commit}}", check=False)
        if full is None:
            fail("HTTP 404 Not Found")
        print(json.dumps(fixture("commit.json", st, head=full)))
        return
    if tail[0] == "commits" and len(tail) == 3 and tail[2] == "check-runs":
        pr = next((p for p in st["prs"].values() if head_of(st, p) == tail[1]), None)
        if pr is None:
            fail("HTTP 404 Not Found")
        out = fixture("check-runs.json", st, pr)
        runs = out["check_runs"]
        if pr["ci"] == "none":
            runs = []
        for run in runs:  # the live runs are green; the other states change only the status fields
            if pr["ci"] == "in_progress":
                run.update({"status": "in_progress", "conclusion": None, "completed_at": None})
            elif pr["ci"] == "failure":
                run["conclusion"] = "failure"
        out.update({"total_count": len(runs), "check_runs": runs})
        print(json.dumps(out))
        return
    pr = st["prs"].get(tail[1]) if tail[0] in ("pulls", "issues") and len(tail) > 1 else None
    if pr is None:
        fail("HTTP 404 Not Found")
    head = head_of(st, pr)
    codex = pr["codex"]
    if tail[0] == "pulls" and len(tail) == 2:
        out = fixture("pull.json", st, pr)
        out.update({"state": "open" if pr["state"] == "OPEN" else "closed", "merged": pr["state"] == "MERGED",
                    "draft": bool(pr.get("draft"))})
        out["head"]["sha"], out["base"]["ref"] = head, pr["base"]
        print(json.dumps(out))
    elif tail[0] == "pulls" and tail[2] == "reviews":
        items = fixture("reviews.json", st, pr)  # the Codex review and the author's reply, as on the live PR
        print(json.dumps([] if codex == "none" else items))
    elif tail[0] == "pulls" and tail[2] == "comments":
        items = fixture("review-comments.json", st, pr)
        print(json.dumps([] if codex == "none" else items))
    elif tail[0] == "issues" and tail[2] == "comments":
        items = fixture("issue-comments.json", st, pr)
        if codex == "none":  # Codex has not written anything yet
            items = [i for i in items if i["user"]["login"] not in CODEX_BOT]
        elif codex == "stale":  # everything Codex wrote is about the commit before the head
            older = fixture("issue-comments.json", st, pr, head=git(st["origin"], "rev-parse", f"{head}~1", check=False) or ZERO)
            items = [o if o["user"]["login"] in CODEX_BOT else i for i, o in zip(items, older)]
        print(json.dumps(items))
    else:
        unexpected(args)


def unexpected(args):
    jlog("calls.jsonl", {"unexpected": args})
    fail(f"gh {' '.join(args[:2])}: not expected in this test", 2)


def main(argv):
    jlog("calls.jsonl", {"argv": argv, "cwd": os.getcwd()})
    st = load()
    if argv[0] == "pr":
        pr_cmd(st, argv[1:])
    elif argv[0] == "api":
        api_cmd(st, argv[1:])
    else:
        unexpected(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
