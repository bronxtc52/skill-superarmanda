#!/usr/bin/env python3
"""Contract tests for the read-only GitHub PR-review evidence gate.

Fixtures deliberately model GitHub API responses rather than implementation
internals: a green answer must be tied to the exact head and a trusted bot.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "superarmanda" / "scripts" / "pr_review.py"
SPEC = importlib.util.spec_from_file_location("superarmanda_pr_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

HEAD = "a" * 40
OLD = "b" * 40
MOVED = "c" * 40
CODEX = "chatgpt-codex-connector[bot]"
CODERABBIT_DRAFT_SKIP = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
    "<!-- This is an auto-generated comment: skip review by coderabbit.ai -->\n\n"
    "> [!IMPORTANT]\n"
    "> ## Draft PR not reviewed\n"
    ">\x20\n"
    "> Draft PRs are not automatically reviewed by default.\n"
    ">\x20\n"
    '> - [ ] <!-- {"checkboxId":"11111111-2222-4333-8444-555555555555"} --> Trigger a manual review\n'
    ">\x20\n"
    "> To automatically review draft PRs, update your CodeRabbit configuration:\n"
    ">\x20\n"
    "> ```yaml\n"
    "> reviews:\n"
    ">   auto_review:\n"
    ">     drafts: true\n"
    "> ```\n\n"
    "<!-- end of auto-generated comment: skip review by coderabbit.ai -->\n\n"
    "<!-- tips_start -->\n\n---\n\n\n\n\n"
    "<sub>Comment `@coderabbitai help` to get the list of available commands.</sub>\n\n"
    "<!-- tips_end -->"
)


def actor(login=CODEX, kind="Bot"):
    return {"login": login, "type": kind}


def pr(sha=HEAD):
    return {"head": {"sha": sha}, "draft": False}


def review(state="APPROVED", sha=HEAD, body="", ident=1, who=None):
    return {
        "id": ident,
        "user": who or actor(),
        "state": state,
        "commit_id": sha,
        "body": body,
        "html_url": f"https://review/{ident}",
    }


def issue(body, who=None, ident=1):
    return {"user": who or actor(), "body": body, "html_url": f"https://issue/{ident}"}


OBSERVED_FOOTER = (
    "<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n"
    "[Your team has set up Codex to review pull requests in this repo]"
    "(https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you\n"
    '- Open a pull request for review\n- Mark a draft as ready\n- Comment "@codex review".\n\n'
    "If Codex has suggestions, it will comment; otherwise it will react with 👍.\n\n\n\n\n"
    'Codex can also answer questions or update the PR. Try commenting "@codex address that feedback".\n'
    "            \n"
    "</details>"
)


def observed_clean(sha=HEAD, header="Keep them coming!"):
    return (
        "Codex Review: Didn't find any major issues. "
        + header
        + "\n\n**Reviewed commit:** `"
        + sha[:10]
        + "`\n\n"
        + OBSERVED_FOOTER
    )


class EvaluateContract(unittest.TestCase):
    def evaluate(
        self, current=HEAD, reviews=(), comments=(), issues=(), resolve=lambda ref: None
    ):
        return MODULE.evaluate(
            pr(current), list(reviews), list(comments), list(issues), HEAD, resolve
        )

    def test_exact_current_codex_bot_approval_is_clean(self):
        result = self.evaluate(reviews=[review()])
        self.assertEqual(result["status"], "pass")

    def test_same_login_with_user_account_is_not_trusted(self):
        result = self.evaluate(reviews=[review(who=actor(CODEX, "User"))])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(
            "trusted GitHub Codex bot evidence was not found", result["limitations"]
        )

    def test_wrong_or_stale_head_never_passes(self):
        for current, reviewed in ((OLD, HEAD), (HEAD, OLD)):
            with self.subTest(current=current, reviewed=reviewed):
                result = self.evaluate(current=current, reviews=[review(sha=reviewed)])
                self.assertEqual(result["status"], "incomplete")

    def test_explicit_clean_comment_can_use_abbreviation_and_trailing_codex_details_only_when_api_resolves_it(
        self,
    ):
        clean = "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `aaaaaaaaaaaa`"
        rendered_clean = (
            clean
            + "\n\n<details><summary>About Codex</summary>Automated review.</details>"
        )
        accepted = self.evaluate(
            issues=[issue(rendered_clean)],
            resolve=lambda ref: HEAD if ref == "a" * 12 else None,
        )
        rejected = self.evaluate(issues=[issue(clean)], resolve=lambda ref: None)
        self.assertEqual(accepted["status"], "pass")
        self.assertEqual(rejected["status"], "incomplete")

    def test_clean_prefix_with_details_is_normalized_from_crlf(self):
        clean = (
            "Codex Review: Didn't find any major issues. :rocket:\r\n\r\n"
            "**Reviewed commit:** `aaaaaaaaaaaa`\r\n\r\n"
            "<details><summary>About Codex</summary>Automated review.</details>"
        )
        result = self.evaluate(issues=[issue(clean)], resolve=lambda ref: HEAD)
        self.assertEqual(result["status"], "pass")

    def test_exact_observed_clean_comment_variant_is_accepted(self):
        original = (
            "Codex Review: Didn't find any major issues. Keep them coming!\n\n**Reviewed commit:** `"
            + HEAD[:10]
            + "`"
        )
        self.assertEqual(
            self.evaluate(issues=[issue(original)], resolve=lambda ref: HEAD)["status"],
            "findings",
        )
        self.assertEqual(
            self.evaluate(issues=[issue(observed_clean())], resolve=lambda ref: HEAD)[
                "status"
            ],
            "pass",
        )
        raw_fetched_body = (
            "Codex Review: Didn't find any major issues. Keep them coming!\n\n"
            "**Reviewed commit:** `039f737951`\n\n" + OBSERVED_FOOTER
        )
        self.assertEqual(MODULE.clean_commit(raw_fetched_body), "039f737951")

    def test_exact_chefs_kiss_clean_comment_requires_the_observed_footer(self):
        exact = observed_clean(header="Chef's kiss.")
        self.assertEqual(
            self.evaluate(issues=[issue(exact)], resolve=lambda ref: HEAD)["status"],
            "pass",
        )
        near_miss = observed_clean(header="Chef's kiss!")
        self.assertEqual(
            self.evaluate(issues=[issue(near_miss)], resolve=lambda ref: HEAD)[
                "status"
            ],
            "findings",
        )
        carried_finding = {
            "user": actor(),
            "body": "This current finding remains visible.",
            "commit_id": HEAD,
            "original_commit_id": OLD,
            "html_url": "https://comment/carried",
        }
        result = self.evaluate(
            issues=[issue(exact)], comments=[carried_finding], resolve=lambda ref: HEAD
        )
        self.assertEqual(result["status"], "findings")
        self.assertEqual(result["findings"][0]["original_commit_id"], OLD)

    def test_observed_clean_format_rejects_mutations_and_untrusted_or_stale_evidence(
        self,
    ):
        cases = (
            (
                "appended",
                observed_clean() + "\nSQL injection remains reachable.",
                actor(),
                HEAD,
            ),
            (
                "inserted",
                observed_clean().replace(
                    "<br/>", "<br/>\nSQL injection remains reachable."
                ),
                actor(),
                HEAD,
            ),
            ("author", observed_clean(), actor(CODEX, "User"), HEAD),
            ("stale", observed_clean(OLD), actor(), HEAD),
            (
                "footer",
                observed_clean().replace(
                    "Try commenting", "Please investigate. Try commenting"
                ),
                actor(),
                HEAD,
            ),
        )
        for name, body, who, current in cases:
            with self.subTest(name=name):
                self.assertNotEqual(
                    self.evaluate(
                        current=current,
                        issues=[issue(body, who)],
                        resolve=lambda ref: HEAD if ref == HEAD[:10] else OLD,
                    )["status"],
                    "pass",
                )

    def test_codex_clean_footer_cannot_contain_a_substantive_finding(self):
        body = (
            "Codex Review: Didn't find any major issues. :rocket:\n\n"
            f"**Reviewed commit:** `{HEAD}`\n\n"
            "<details><summary>About Codex</summary>Automated review. "
            "SQL injection remains reachable.</details>"
        )
        result = self.evaluate(issues=[issue(body)], resolve=lambda ref: HEAD)
        self.assertEqual(result["status"], "findings")

    def test_unknown_current_codex_comment_alongside_clean_comment_is_a_finding(self):
        clean = (
            "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `"
            + HEAD
            + "`"
        )
        result = self.evaluate(
            issues=[
                issue(clean, ident=20),
                issue("Could this leak tenant data?", ident=21),
            ],
            resolve=lambda ref: HEAD,
        )
        self.assertEqual(result["status"], "findings")
        self.assertEqual(len(result["findings"]), 1)

    def test_summary_or_absence_is_not_a_clean_review(self):
        summary = "<!-- codex-pull-request-review-summary -->\nCompleted review."
        for issues in ([], [issue(summary)]):
            with self.subTest(issues=bool(issues)):
                self.assertEqual(self.evaluate(issues=issues)["status"], "incomplete")

    def test_commented_suggestions_are_findings_and_override_old_clean_at_same_head(
        self,
    ):
        clean = (
            "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `"
            + HEAD
            + "`"
        )
        result = self.evaluate(
            reviews=[
                review(
                    "COMMENTED", body="Please validate the authorization path.", ident=7
                )
            ],
            issues=[issue(clean, ident=8)],
            resolve=lambda ref: HEAD,
        )
        self.assertEqual(result["status"], "findings")
        self.assertTrue(result["findings"])

    def test_coderabbit_is_optional_but_substantive_comment_is_a_finding(self):
        no_rabbit = self.evaluate(reviews=[review()])
        rabbit = actor("coderabbitai[bot]", "Bot")
        substantive = self.evaluate(
            reviews=[
                review(),
                review(
                    "COMMENTED", body="SQL injection in search", ident=9, who=rabbit
                ),
            ]
        )
        self.assertEqual(no_rabbit["status"], "pass")
        self.assertEqual(substantive["status"], "findings")

    def test_coderabbit_skipped_boilerplate_does_not_block_a_codex_clean_result(self):
        rabbit = actor("coderabbitai[bot]", "Bot")
        skipped = issue(
            "<!-- coderabbitai-review-status -->\nReview skipped: no credits available.",
            rabbit,
        )
        result = self.evaluate(reviews=[review()], issues=[skipped])
        self.assertEqual(result["status"], "pass")

    def test_exact_coderabbit_draft_skip_template_is_unavailable_but_appended_finding_is_not(
        self,
    ):
        rabbit = actor("coderabbitai[bot]", "Bot")
        with self.subTest(case="known template"):
            result = self.evaluate(
                reviews=[review()], issues=[issue(CODERABBIT_DRAFT_SKIP, rabbit)]
            )
            self.assertEqual(result["status"], "pass")
        with self.subTest(case="appended finding"):
            result = self.evaluate(
                reviews=[review()],
                issues=[
                    issue(
                        CODERABBIT_DRAFT_SKIP
                        + "\n\nrate limit exhausted: SQL injection remains reachable.",
                        rabbit,
                    )
                ],
            )
            self.assertEqual(result["status"], "findings")

    def test_empty_coderabbit_commented_review_is_conservatively_incomplete_with_a_reason(
        self,
    ):
        rabbit = actor("coderabbitai[bot]", "Bot")
        result = self.evaluate(
            reviews=[review(), review("COMMENTED", body="", ident=13, who=rabbit)]
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(
            any(
                "CodeRabbit" in item or "COMMENTED" in item
                for item in result["limitations"]
            )
        )

    def test_historical_inline_comment_from_an_old_commit_is_recorded_as_history_not_a_current_finding(
        self,
    ):
        old_inline = {
            "user": actor(),
            "body": "This was fixed in a later commit.",
            "commit_id": OLD,
            "original_commit_id": OLD,
            "html_url": "https://comment/old",
        }
        result = self.evaluate(reviews=[review()], comments=[old_inline])
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["findings"])

    def test_current_inline_finding_is_never_hidden_by_its_parent_review(self):
        """GitHub may retain an old, absent, or foreign parent review id."""
        inline = {
            "user": actor(),
            "body": "This current line still permits privilege escalation.",
            "commit_id": HEAD,
            "original_commit_id": HEAD,
            "html_url": "https://comment/current",
            "pull_request_review_id": 7,
        }
        for parents in (
            [review("COMMENTED", OLD, ident=7)],
            [],
            [
                review(
                    "COMMENTED",
                    HEAD,
                    ident=7,
                    who=actor("external-reviewer", "User"),
                )
            ],
        ):
            with self.subTest(parents=parents):
                result = self.evaluate(
                    reviews=[*parents, review("APPROVED", HEAD, ident=9)],
                    comments=[inline],
                )
                self.assertEqual(result["status"], "findings")
                self.assertTrue(result["findings"])
                self.assertEqual(result["findings"][0]["commit_id"], HEAD)
                self.assertEqual(result["findings"][0]["original_commit_id"], HEAD)
                self.assertEqual(
                    result["findings"][0]["reason"], "applies to current HEAD"
                )

    def test_current_inline_finding_retains_carried_forward_original_commit(self):
        inline = {
            "user": actor(),
            "body": "This current line still permits privilege escalation.",
            "commit_id": HEAD,
            "original_commit_id": OLD,
            "html_url": "https://comment/carried",
        }
        result = self.evaluate(reviews=[review()], comments=[inline])
        self.assertEqual(result["status"], "findings")
        self.assertEqual(result["findings"][0]["commit_id"], HEAD)
        self.assertEqual(result["findings"][0]["original_commit_id"], OLD)
        self.assertEqual(result["findings"][0]["reason"], "applies to current HEAD")

    def test_coderabbit_metadata_words_do_not_hide_body_or_attached_finding(self):
        rabbit = actor("coderabbitai[bot]", "Bot")
        status = review(
            "COMMENTED",
            HEAD,
            "Review skipped after rate limit: no credits. Walkthrough follows.",
            ident=41,
            who=rabbit,
        )
        attached = {
            "user": rabbit,
            "body": "SQL injection remains reachable from this query.",
            "commit_id": HEAD,
            "html_url": "https://comment/rabbit",
            "pull_request_review_id": 41,
        }
        with self.subTest(status_word="attached"):
            result = self.evaluate(reviews=[review(), status], comments=[attached])
            self.assertEqual(result["status"], "findings")
        for status_word in (
            "rate limit",
            "walkthrough",
            "review skipped",
            "no credits",
        ):
            with self.subTest(status_word=status_word):
                result = self.evaluate(
                    reviews=[
                        review(),
                        review(
                            "COMMENTED",
                            HEAD,
                            f"{status_word}: SQL injection remains reachable.",
                            ident=42,
                            who=rabbit,
                        ),
                    ]
                )
                self.assertEqual(result["status"], "findings")

    def test_exact_clean_codex_summary_in_current_commented_review_is_clean(self):
        clean = (
            "Codex Review: Didn't find any major issues. :rocket:\n\n"
            f"**Reviewed commit:** `{HEAD}`"
        )
        accepted = self.evaluate(
            reviews=[review("COMMENTED", HEAD, clean)], resolve=lambda ref: HEAD
        )
        stale = self.evaluate(
            reviews=[review("COMMENTED", OLD, clean)], resolve=lambda ref: HEAD
        )
        unknown = self.evaluate(
            reviews=[review("COMMENTED", HEAD, "all clear")],
            resolve=lambda ref: HEAD,
        )
        unresolved = self.evaluate(
            reviews=[review("COMMENTED", HEAD, clean)], resolve=lambda ref: None
        )
        self.assertEqual(accepted["status"], "pass")
        for result in (stale, unknown, unresolved):
            self.assertNotEqual(result["status"], "pass")

    def test_clean_text_cannot_override_nonclean_review_state(self):
        clean = (
            "Codex Review: Didn't find any major issues. :rocket:\n\n"
            f"**Reviewed commit:** `{HEAD}`"
        )
        for state in ("CHANGES_REQUESTED", "DISMISSED", "PENDING"):
            with self.subTest(state=state):
                result = self.evaluate(
                    reviews=[review(state, HEAD, clean)], resolve=lambda ref: HEAD
                )
                self.assertNotEqual(result["status"], "pass")

    def test_current_incomplete_codex_review_blocks_another_clean_approval(self):
        clean = review("APPROVED", HEAD, ident=1)
        for state in ("COMMENTED", "PENDING", "UNKNOWN"):
            with self.subTest(state=state):
                result = self.evaluate(reviews=[clean, review(state, HEAD, ident=2)])
                self.assertEqual(result["status"], "incomplete")

        stale = self.evaluate(reviews=[clean, review("PENDING", OLD, ident=2)])
        self.assertEqual(stale["status"], "pass")


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["PR_REVIEW_GH_LOG"])
log.open("a", encoding="utf-8").write(json.dumps(argv) + "\n")
if argv[:5] != ["api", "--hostname", "github.com", "--method", "GET"]:
    print("only read-only GET is permitted", file=sys.stderr)
    raise SystemExit(91)
endpoint = argv[5]
data = json.loads(Path(os.environ["PR_REVIEW_FIXTURE"]).read_text(encoding="utf-8"))
value = data.get(endpoint, [])
if isinstance(value, dict) and "_sequence" in value:
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    occurrence = sum(call[5] == endpoint for call in calls) - 1
    value = value["_sequence"][occurrence]
if isinstance(value, list) and value and isinstance(value[0], list):
    page = int(endpoint.rsplit("page=", 1)[1]) - 1
    value = value[page] if page < len(value) else []
print(json.dumps(value))
"""


class GhBoundaryContract(unittest.TestCase):
    def test_repository_path_rejects_injection_before_github_access(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "owner/name?x=1",
                "owner/name/extra",
                "../name",
                "owner/..",
                "owner/name#fragment",
            ):
                args = MODULE.argparse.Namespace(
                    repo=name,
                    worktree=ROOT,
                    output=Path(directory) / "result.json",
                    pr=7,
                    head=HEAD,
                )
                with self.subTest(repo=name), mock.patch.object(MODULE, "gh") as remote:
                    with self.assertRaisesRegex(
                        ValueError, "--repo must be OWNER/NAME"
                    ):
                        MODULE.check(args)
                    remote.assert_not_called()
        self.assertIsNotNone(MODULE.REPOSITORY.fullmatch("owner/.github"))

    def test_github_failures_do_not_expose_raw_diagnostics(self):
        failure = subprocess.CalledProcessError(
            1, ["gh"], stderr=b"private-diagnostic-canary"
        )
        with mock.patch.object(MODULE.subprocess, "check_output", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "^gh api read failed$"):
                MODULE.gh("repos/owner/name/pulls/7")

    def test_metadata_resolution_failures_are_sanitized_before_gh_access(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            for failure in (
                OSError("sensitive path"),
                subprocess.TimeoutExpired(["git"], 1),
                subprocess.CalledProcessError(1, ["git"], stderr="sensitive path"),
            ):
                with (
                    self.subTest(failure=type(failure).__name__),
                    mock.patch.object(
                        MODULE.subprocess, "check_output", side_effect=failure
                    ),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "cannot resolve local Git metadata"
                    ):
                        MODULE.safe_output(worktree / "result.json", worktree)

    def test_cli_rejects_output_inside_explicit_local_worktree_before_any_gh_request(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "synthetic-git"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "gh"
            fake.write_text(FAKE_GH, encoding="utf-8")
            fake.chmod(0o755)
            log, fixture = root / "calls.jsonl", root / "fixture.json"
            fixture.write_text("{}", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(bindir) + os.pathsep + env["PATH"],
                    "PR_REVIEW_GH_LOG": str(log),
                    "PR_REVIEW_FIXTURE": str(fixture),
                }
            )
            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "check",
                    "--repo",
                    "github-owner/github-name",
                    "--pr",
                    "7",
                    "--head",
                    HEAD,
                    "--worktree",
                    str(worktree),
                    "--output",
                    str(worktree / "evidence.json"),
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--output must be outside --repo", proc.stderr)
            self.assertFalse(
                log.exists(), "the local output guard must run before GitHub access"
            )

    def test_paginated_get_only_collection_and_head_change_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "gh"
            fake.write_text(FAKE_GH, encoding="utf-8")
            fake.chmod(0o755)
            log, fixture, output = (
                root / "calls.jsonl",
                root / "fixture.json",
                root / "evidence.json",
            )
            base = "repos/acme/widget/pulls/42"
            clean = "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `aaaaaaaaaaaa`"
            # A full first page forces the second API page; its clean evidence
            # would otherwise pass, so the changed final PR head must win.
            first_page = [
                {
                    "id": n,
                    "user": actor("someone", "User"),
                    "state": "COMMENTED",
                    "commit_id": HEAD,
                    "body": "",
                }
                for n in range(100)
            ]
            first_page.append(review(ident=201))
            fixture.write_text(
                json.dumps(
                    {
                        base: {"_sequence": [pr(HEAD), pr(MOVED)]},
                        base + "/reviews?per_page=100&page=1": [first_page, first_page],
                        base + "/reviews?per_page=100&page=2": [
                            [review(ident=202)],
                            [review(ident=202)],
                        ],
                        base + "/comments?per_page=100&page=1": [[], []],
                        "repos/acme/widget/issues/42/comments?per_page=100&page=1": [
                            [issue(clean)],
                            [issue(clean)],
                        ],
                        "repos/acme/widget/commits/aaaaaaaaaaaa": {"sha": HEAD},
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(bindir) + os.pathsep + env["PATH"],
                    "PR_REVIEW_GH_LOG": str(log),
                    "PR_REVIEW_FIXTURE": str(fixture),
                }
            )
            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "check",
                    "--repo",
                    "acme/widget",
                    "--pr",
                    "42",
                    "--head",
                    HEAD,
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["current_head"], MOVED)
            self.assertIn(
                "PR HEAD changed while collecting evidence", result["limitations"]
            )
            calls = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(
                calls
                and all(
                    call[:5] == ["api", "--hostname", "github.com", "--method", "GET"]
                    for call in calls
                )
            )
            endpoints = [call[5] for call in calls]
            self.assertIn(base + "/reviews?per_page=100&page=2", endpoints)
            self.assertIn("repos/acme/widget/commits/aaaaaaaaaaaa", endpoints)

    def test_current_finding_added_during_second_snapshot_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "gh"
            fake.write_text(FAKE_GH, encoding="utf-8")
            fake.chmod(0o755)
            log, fixture, output = (
                root / "calls.jsonl",
                root / "fixture.json",
                root / "evidence.json",
            )
            base = "repos/acme/widget/pulls/42"
            fixture.write_text(
                json.dumps(
                    {
                        base: {"_sequence": [pr(HEAD), pr(HEAD)]},
                        base + "/reviews?per_page=100&page=1": {
                            "_sequence": [
                                [review()],
                                [review(), review("COMMENTED", HEAD, "new finding", 2)],
                            ]
                        },
                        base + "/comments?per_page=100&page=1": {"_sequence": [[], []]},
                        "repos/acme/widget/issues/42/comments?per_page=100&page=1": {
                            "_sequence": [[], []]
                        },
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
                "PR_REVIEW_GH_LOG": str(log),
                "PR_REVIEW_FIXTURE": str(fixture),
            }
            proc = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "check",
                    "--repo",
                    "acme/widget",
                    "--pr",
                    "42",
                    "--head",
                    HEAD,
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["status"], "incomplete")
            self.assertIn(
                "review evidence changed while collecting evidence",
                result["limitations"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
