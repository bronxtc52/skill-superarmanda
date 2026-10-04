"""Stateful `gh` stand-in for tests/helpers/superarmanda_waves_e2e_test.py.

World: $FAKE_GH_STATE/state.json (repo, path of the bare "origin", PRs) and a call log. The PRs'
branches live in a REAL local bare git repository, so `gh pr merge --squash` really squashes the
branch into the base of that repository, and the next wave's fetch really sees the merge commit.

Per PR the test sets what GitHub would show for the head: "ci" (none|in_progress|failure|success),
"codex" (none|stale|head: the Codex review is absent / written on an older commit / on the head),
"draft". Nothing is checked by the stand-in at merge time (GitHub without branch protection merges
a red PR too): it RECORDS in merges.jsonl what was true at that moment, and the test's oracle judges.
Exit 2 and a log entry for any verb the dispatcher is not expected to use.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

STATE = os.environ["FAKE_GH_STATE"]
BOT = {"login": "chatgpt-codex-connector[bot]", "type": "Bot"}


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


def as_json(st, pr):
    owner, _, name = st["repo"].partition("/")
    mc = pr.get("merge_commit")
    return {"number": pr["number"], "headRefOid": head_of(st, pr), "isDraft": bool(pr.get("draft")),
            "state": pr["state"], "baseRefName": pr["base"],
            "headRepositoryOwner": {"login": owner}, "headRepository": {"name": name},
            "mergeCommit": {"oid": mc} if mc else None}


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
        print(json.dumps([pick(as_json(st, p), opt(rest, "--json")) for p in want]))
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
        print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False}, "nodes": []}}}}}))
        return
    parts = path.split("/")  # repos/<owner>/<name>/...
    if parts[:3] != ["repos", *st["repo"].split("/")]:
        fail(f"unexpected api path {path}")
    tail = parts[3:]
    if tail[0] == "commits" and len(tail) == 3 and tail[2] == "check-runs":
        pr = next((p for p in st["prs"].values() if head_of(st, p) == tail[1]), None)
        if pr is None:
            fail("HTTP 404 Not Found")
        runs = {"none": [], "in_progress": [{"id": 1, "name": "ci", "status": "in_progress", "conclusion": None}],
                "failure": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "failure"}],
                "success": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success"}]}[pr["ci"]]
        print(json.dumps({"total_count": len(runs), "check_runs": runs}))
        return
    pr = st["prs"].get(tail[1]) if tail[0] in ("pulls", "issues") and len(tail) > 1 else None
    if pr is None:
        fail("HTTP 404 Not Found")
    head = head_of(st, pr)
    if tail[0] == "pulls" and len(tail) == 2:
        print(json.dumps({"state": "open" if pr["state"] == "OPEN" else "closed", "merged": pr["state"] == "MERGED",
                          "draft": bool(pr.get("draft")), "head": {"sha": head}, "base": {"ref": pr["base"]}}))
    elif tail[0] == "pulls" and tail[2] == "reviews":
        commit = {"head": head, "stale": git(st["origin"], "rev-parse", f"{head}~1", check=False)}.get(pr["codex"])
        print(json.dumps([] if not commit else [{"id": 11, "user": BOT, "commit_id": commit, "state": "COMMENTED",
                                                  "body": "Codex Review: Didn't find any major issues."}]))
    elif tail[2] == "comments":
        print("[]")
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
