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


# Live Codex comment bodies fetched via `gh api` for PRs #441 and #451 (issue
# #442). Embedded byte-for-byte, including the 12-space line before
# </details>; the trailing newline GitHub returns is stripped by text()'s
# .strip(), same as the real fetch path. NOT built through observed_clean(),
# which composes fixtures from an accepted-header list -- these are the raw
# evidence the property has to accept on its own.
RAW_CODEX_COMMENT_D37B123 = (
    "Codex Review: Didn't find any major issues. :rocket:\n"
    "\n"
    "**Reviewed commit:** `d37b123ebd`\n"
    "\n"
    "<details> <summary>ℹ️ About Codex in GitHub</summary>\n"
    "<br/>\n"
    "\n"
    "[Your team has set up Codex to review pull requests in this repo]"
    "(https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you\n"
    "- Open a pull request for review\n"
    "- Mark a draft as ready\n"
    "- Comment \"@codex review\".\n"
    "\n"
    "If Codex has suggestions, it will comment; otherwise it will react with 👍.\n"
    "\n"
    "\n"
    "\n"
    "\n"
    "Codex can also answer questions or update the PR. Try commenting "
    "\"@codex address that feedback\".\n"
    "            \n"
    "</details>\n"
)

RAW_CODEX_COMMENT_737CE2B = (
    "Codex Review: Didn't find any major issues. You're on a roll.\n"
    "\n"
    "**Reviewed commit:** `737ce2b8bf`\n"
    "\n"
    "<details> <summary>ℹ️ About Codex in GitHub</summary>\n"
    "<br/>\n"
    "\n"
    "[Your team has set up Codex to review pull requests in this repo]"
    "(https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when you\n"
    "- Open a pull request for review\n"
    "- Mark a draft as ready\n"
    "- Comment \"@codex review\".\n"
    "\n"
    "If Codex has suggestions, it will comment; otherwise it will react with 👍.\n"
    "\n"
    "\n"
    "\n"
    "\n"
    "Codex can also answer questions or update the PR. Try commenting "
    "\"@codex address that feedback\".\n"
    "            \n"
    "</details>\n"
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

    def test_raw_fetched_pr441_and_pr451_clean_comments_are_accepted(self):
        """Live bodies from PR #441 (:rocket:) and #451 (You're on a roll.)."""
        self.assertEqual(
            MODULE.clean_commit(MODULE.text({"body": RAW_CODEX_COMMENT_D37B123})),
            "d37b123ebd",
        )
        self.assertEqual(
            MODULE.clean_commit(MODULE.text({"body": RAW_CODEX_COMMENT_737CE2B})),
            "737ce2b8bf",
        )
        for raw, short_sha in (
            (RAW_CODEX_COMMENT_D37B123, "d37b123ebd"),
            (RAW_CODEX_COMMENT_737CE2B, "737ce2b8bf"),
        ):
            with self.subTest(short_sha=short_sha):
                result = self.evaluate(
                    issues=[issue(raw)],
                    resolve=lambda ref, short_sha=short_sha: (
                        HEAD if ref == short_sha else None
                    ),
                )
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["findings"], [])

    def test_short_clean_header_with_punctuation_passes_and_keeps_the_observed_footer(
        self,
    ):
        """Punctuation is not part of the accepted-phrase property (#442)."""
        exact = observed_clean(header="Chef's kiss!")
        self.assertEqual(
            self.evaluate(issues=[issue(exact)], resolve=lambda ref: HEAD)["status"],
            "pass",
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

    def test_short_clean_header_accepts_arbitrary_short_phrases_but_stays_current_and_trusted(
        self,
    ):
        for header in (
            "Bravo.",
            "What shall we delve into next?",
            "Breezy!",
            "Nice work, team!",
        ):
            with self.subTest(header=header):
                exact = observed_clean(header=header)
                self.assertEqual(
                    self.evaluate(
                        issues=[issue(exact)], resolve=lambda ref: HEAD
                    )["status"],
                    "pass",
                )
                for body, who, resolver in (
                    (exact + "\nA finding was appended.", actor(), lambda ref: HEAD),
                    (exact, actor(CODEX, "User"), lambda ref: HEAD),
                    (observed_clean(OLD, header), actor(), lambda ref: OLD),
                    (
                        exact.replace(header, "<script>alert(1)</script>"),
                        actor(),
                        lambda ref: HEAD,
                    ),
                ):
                    self.assertNotEqual(
                        self.evaluate(
                            issues=[issue(body, who)], resolve=resolver
                        )["status"],
                        "pass",
                    )

    def test_short_clean_header_structural_limits_are_enforced(self):
        """Table from the #442 plan review: one subTest per named limitation,
        each broken by exactly one of the mutations recorded in mutations.log.
        """
        cases = (
            ("newline_in_header", "Nice.\nConsider validating tenant id."),
            ("angle_brackets_in_header", "<b>ok</b>"),
            ("square_brackets_in_header", "[x](y)"),
            ("backtick_in_header", "`deadbeef`"),
            ("header_too_long_49_chars", "x" * 49),
            # The forbidden-character class must apply to the FIRST phrase
            # character too, not just the rest: \S alone lets a lone leading
            # #, <, or [ through even though the same char later in the
            # phrase is already excluded (coordinator finding, #442 diff).
            ("header_starts_with_hash", "#123 fixed"),
            ("header_starts_with_angle", "<script src=x"),
            ("header_starts_with_bracket", "[see notes"),
            # Every prior forbidden-character case puts the character in the
            # FIRST position, so the trailing class [^\n<>\[\]`#]{0,47} was
            # never actually exercised: a mutation that widens it to [^\n]
            # (still blocking newlines, nothing else) left the whole suite
            # green (coordinator finding, PR #11 diff review). These put the
            # forbidden character in the middle of the phrase instead.
            ("mid_angle_brackets_in_header", "ok <b>x</b>"),
            ("mid_square_brackets_in_header", "ok [x](y)"),
            ("mid_backtick_in_header", "ok `sha`"),
            ("mid_hash_in_header", "ok #1"),
        )
        for name, header in cases:
            with self.subTest(name=name):
                body = (
                    "Codex Review: Didn't find any major issues. "
                    + header
                    + "\n\n**Reviewed commit:** `"
                    + HEAD[:10]
                    + "`\n\n"
                    + OBSERVED_FOOTER
                )
                self.assertEqual(
                    self.evaluate(issues=[issue(body)], resolve=lambda ref: HEAD)[
                        "status"
                    ],
                    "findings",
                )
        with self.subTest(name="header_blank_after_period"):
            # Two spaces: the mandatory leading space plus a whitespace-only
            # "phrase" candidate. \S rejects it (a space is not \S). A weakened
            # \S->. would accept the second space as the phrase's first char
            # without having to swallow a newline, so this fixture is what
            # that specific mutation needs to flip to pass.
            blank = (
                "Codex Review: Didn't find any major issues.  \n\n"
                "**Reviewed commit:** `" + HEAD[:10] + "`\n\n" + OBSERVED_FOOTER
            )
            self.assertEqual(
                self.evaluate(issues=[issue(blank)], resolve=lambda ref: HEAD)[
                    "status"
                ],
                "findings",
            )
        with self.subTest(name="footer_missing_space"):
            weakened_footer = OBSERVED_FOOTER.replace(
                "<details> <summary>", "<details><summary>", 1
            )
            weakened = (
                "Codex Review: Didn't find any major issues. :rocket:\n\n"
                "**Reviewed commit:** `" + HEAD[:10] + "`\n\n" + weakened_footer
            )
            self.assertEqual(
                self.evaluate(issues=[issue(weakened)], resolve=lambda ref: HEAD)[
                    "status"
                ],
                "findings",
            )

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

    def test_empty_coderabbit_commented_review_on_head_proves_completion_not_incompleteness(
        self,
    ):
        # Изменено в 1.0.2 (#72): пустое ревью CodeRabbit на HEAD — доказательство
        # завершения, а не неполнота; основной статус от него больше не зависит.
        rabbit = actor("coderabbitai[bot]", "Bot")
        result = self.evaluate(
            reviews=[review(), review("COMMENTED", body="", ident=13, who=rabbit)]
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["coderabbit"]["status"], "pass")
        self.assertIn(
            "empty CodeRabbit COMMENTED review on HEAD proves completion",
            result["limitations"],
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


FIXTURES = ROOT / "tests" / "fixtures" / "pr-review"

# Fake `gh`: serves GraphQL read-only from a fixture of
# {"entries": [{"name", "variables", "responses": [...]}]}. The n-th identical
# request gets responses[n] (the last one repeats). Anything that is not
# `api graphql`, not a `query`, or has no entry exits non-zero.
FAKE_GH = r"""#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["PR_REVIEW_GH_LOG"])
log.open("a", encoding="utf-8").write(json.dumps(argv) + "\n")
if argv[:4] != ["api", "graphql", "--hostname", "github.com"]:
    print("only `api graphql` is permitted", file=sys.stderr)
    raise SystemExit(91)
query, variables, rest = None, {}, argv[4:]
while rest:
    flag, pair, rest = rest[0], rest[1], rest[2:]
    key, value = pair.split("=", 1)
    if flag == "-f" and key == "query":
        query = value
    elif flag == "-f":
        variables[key] = value
    elif flag == "-F":
        variables[key] = int(value)
    else:
        raise SystemExit(92)
if query is None or not query.lstrip().startswith("query") or "mutation" in query:
    print("only read-only queries are permitted", file=sys.stderr)
    raise SystemExit(93)
name = re.match(r"\s*query\s+(\w+)", query).group(1)
data = json.loads(Path(os.environ["PR_REVIEW_FIXTURE"]).read_text(encoding="utf-8"))
entries = data["entries"] if isinstance(data, dict) else data
calls = []
for line in log.read_text(encoding="utf-8").splitlines():
    call = json.loads(line)
    seen = {}
    rest2 = call[4:]
    while rest2:
        flag, pair, rest2 = rest2[0], rest2[1], rest2[2:]
        k, v = pair.split("=", 1)
        seen[k] = v if flag == "-f" else int(v)
    calls.append(seen)
match = [e for e in entries if e["name"] == name and e["variables"] == variables]
if not match:
    print("private-diagnostic-canary: no fixture for " + name, file=sys.stderr)
    raise SystemExit(1)
occurrence = sum(
    1 for c in calls
    if {k: v for k, v in c.items() if k != "query"} == variables
    and re.match(r"\s*query\s+(\w+)", c["query"]).group(1) == name
) - 1
responses = match[0]["responses"]
print(json.dumps(responses[min(occurrence, len(responses) - 1)]))
"""


def gactor(login="chatgpt-codex-connector", kind="Bot"):
    return {"__typename": kind, "login": login}


def g_review(state="APPROVED", sha=HEAD, body="", ident=1, who=None):
    return {
        "databaseId": ident,
        "url": f"https://review/{ident}",
        "state": state,
        "body": body,
        "commit": {"oid": sha} if sha else None,
        "author": who or gactor(),
    }


def g_comment(body, sha=HEAD, orig=None, ident=1, review_id=None, who=None):
    return {
        "databaseId": ident,
        "url": f"https://thread-comment/{ident}",
        "body": body,
        "commit": {"oid": sha},
        "originalCommit": {"oid": orig or sha},
        "author": who or gactor(),
        "pullRequestReview": {"databaseId": review_id} if review_id else None,
    }


def g_issue(body, ident=1, who=None):
    return {
        "databaseId": ident,
        "url": f"https://issue/{ident}",
        "body": body,
        "author": who or gactor(),
    }


def g_suite(sid, runs, app="github-actions"):
    return {"id": sid, "app": {"slug": app}, "runs": runs}


def g_run(name="ci", status="COMPLETED", conclusion="SUCCESS"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": f"https://run/{name}",
    }


class Snap:
    """Explicit per-read contents: first read, second read."""

    def __init__(self, first, second):
        self.items = [first, second]


def snaps(value):
    return value.items if isinstance(value, Snap) else [value, value]


def conn(nodes, i, total, tag):
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": i < total - 1, "endCursor": f"{tag}-{i}"},
    }


class Server:
    """Builds fixture entries; every list is served in pages with cursors."""

    def __init__(self, owner="acme", name="widget", number=42, size=100):
        self.base = {"owner": owner, "name": name, "number": number}
        self.size, self.entries = size, []

    def add(self, name, variables, responses):
        self.entries.append(
            {"name": name, "variables": variables, "responses": responses}
        )

    def paged(self, name, variables, snapshots, wrap, tag=None, size=None):
        tag, size = tag or name, size or self.size
        chunks = [
            [s[i : i + size] for i in range(0, len(s), size)] or [[]] for s in snapshots
        ]
        total = len(chunks[0])
        assert all(len(c) == total for c in chunks), "page counts must match"
        for i in range(total):
            vars_ = dict(variables)
            if i:
                vars_["cursor"] = f"{tag}-{i - 1}"
            self.add(name, vars_, [wrap(conn(c[i], i, total, tag)) for c in chunks])

    def header(self, heads=(HEAD, HEAD), draft=False, state="OPEN"):
        self.add(
            "Header",
            self.base,
            [
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": h,
                                "isDraft": draft,
                                "state": state,
                            }
                        }
                    }
                }
                for h in heads
            ],
        )

    def resolve(self, ref, full):
        obj = {"oid": full} if full else None
        self.add(
            "Resolve",
            {"owner": self.base["owner"], "name": self.base["name"], "ref": ref},
            [{"data": {"repository": {"object": obj}}}],
        )

    def threads(self, thread_snaps):
        built = []
        for specs in snaps(thread_snaps):
            nodes = []
            for spec in specs:
                comments = spec["comments"]
                first, more = comments[:100], comments[100:]
                tid = spec["id"]
                if more:
                    pages = [comments[i : i + 100] for i in range(100, len(comments), 100)]
                    total = len(pages) + 1
                    for i, page in enumerate(pages, start=1):
                        vars_ = {"thread": tid, "cursor": f"tc-{tid}-{i - 1}"}
                        self.add(
                            "ThreadComments",
                            vars_,
                            [
                                {
                                    "data": {
                                        "node": {
                                            "comments": conn(page, i, total, f"tc-{tid}")
                                        }
                                    }
                                }
                            ],
                        )
                    cmts = conn(first, 0, total, f"tc-{tid}")
                else:
                    cmts = conn(first, 0, 1, f"tc-{tid}")
                nodes.append(
                    {"id": tid, "isResolved": spec.get("resolved", False), "comments": cmts}
                )
            built.append(nodes)
        self.paged(
            "Threads",
            self.base,
            built,
            lambda c: {"data": {"repository": {"pullRequest": {"reviewThreads": c}}}},
        )

    def checks(self, suites, head=HEAD):
        nodes = []
        for suite in suites:
            runs = suite["runs"]
            first = runs[:100]
            sid = suite["id"]
            total = max(1, -(-len(runs) // 100))
            for i in range(1, total):
                self.add(
                    "SuiteRuns",
                    {"suite": sid, "cursor": f"sr-{sid}-{i - 1}"},
                    [
                        {
                            "data": {
                                "node": {
                                    "checkRuns": conn(
                                        runs[i * 100 : (i + 1) * 100], i, total, f"sr-{sid}"
                                    )
                                }
                            }
                        }
                    ],
                )
            nodes.append(
                {
                    "id": sid,
                    "app": suite["app"],
                    "checkRuns": conn(first, 0, total, f"sr-{sid}"),
                }
            )
        self.paged(
            "Checks",
            {
                "owner": self.base["owner"],
                "name": self.base["name"],
                "head": head,
            },
            [nodes],
            lambda c: {"data": {"repository": {"object": {"checkSuites": c}}}},
        )

    def standard(
        self,
        heads=(HEAD, HEAD),
        reviews=(),
        threads=(),
        issues=(),
        suites=None,
        size=None,
    ):
        if size:
            self.size = size
        self.header(heads)
        self.paged(
            "Reviews",
            self.base,
            snaps(list(reviews) if not isinstance(reviews, Snap) else reviews),
            lambda c: {"data": {"repository": {"pullRequest": {"reviews": c}}}},
        )
        self.threads(threads if isinstance(threads, Snap) else list(threads))
        self.paged(
            "IssueComments",
            self.base,
            snaps(list(issues) if not isinstance(issues, Snap) else issues),
            lambda c: {"data": {"repository": {"pullRequest": {"comments": c}}}},
        )
        self.checks(
            [g_suite("S1", [g_run()])] if suites is None else suites, head=heads[0]
        )
        return self


def run_check(entries, head=HEAD, extra=(), repo="acme/widget", pr_no="42", cwd=None):
    """Run the real CLI against a fake `gh`; returns (proc, result|None, calls)."""
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
        fixture.write_text(json.dumps({"entries": entries}), encoding="utf-8")
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
                repo,
                "--pr",
                pr_no,
                "--head",
                head,
                "--output",
                str(output),
                *extra,
            ],
            text=True,
            capture_output=True,
            env=env,
            cwd=cwd,
        )
        result = (
            json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        )
        calls = (
            [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists()
            else []
        )
        return proc, result, calls


def sent_queries(calls):
    out = []
    for call in calls:
        for flag, pair in zip(call, call[1:]):
            if flag == "-f" and pair.startswith("query="):
                out.append(pair[len("query=") :])
    return out


CLEAN = "Codex Review: Didn't find any major issues. :rocket:\n\n**Reviewed commit:** `aaaaaaaaaaaa`"


def load_variants():
    data = json.loads((FIXTURES / "codex-clean-variants.json").read_text("utf-8"))
    return data["variants"]


class CleanVariantsFixture(unittest.TestCase):
    """#5: 18 live Codex clean-answer variants (captured 2026-10-04)."""

    def test_fixture_is_the_live_capture(self):
        variants = load_variants()
        self.assertEqual(len(variants), 18)
        for v in variants:
            self.assertEqual(
                set(v), {"body", "reviewed_short", "resolved_full", "source_url"}
            )

    def evaluate(self, body, full, resolved, who=None, expected=None):
        return MODULE.evaluate(
            pr(full),
            [],
            [],
            [issue(body, who=who)],
            expected or full,
            lambda ref: resolved.get(ref),
        )

    def test_every_live_variant_is_recognized_and_passes_when_it_resolves_to_head(self):
        for v in load_variants():
            with self.subTest(url=v["source_url"]):
                body = MODULE.text({"body": v["body"]})
                self.assertEqual(MODULE.clean_commit(body), v["reviewed_short"])
                result = self.evaluate(
                    v["body"], v["resolved_full"], {v["reviewed_short"]: v["resolved_full"]}
                )
                self.assertEqual(result["status"], "pass", result)

    def test_mutations_of_every_variant_never_pass(self):
        def footer(body):
            if "Reviews are triggered" in body:
                return body.replace("Reviews are triggered", "Reviews are triggerd")
            return body + "\n<details>x</details>"

        mutations = {
            "extra line": lambda b: b + "\nextra line",
            "markup in phrase": lambda b: b.replace(
                "major issues.", "major issues. <b>x</b>", 1
            ),
            "changed footer": footer,
            "phrase over 48": lambda b: b.replace(
                "major issues.", "major issues. " + "x" * 49, 1
            ),
            "changed marker": lambda b: b.replace("Reviewed commit", "Reviewed  commit"),
            "other line": lambda b: b.replace("Didn't find", "Did find", 1),
        }
        for v in load_variants():
            full, short = v["resolved_full"], v["reviewed_short"]
            for label, mutate in mutations.items():
                with self.subTest(url=v["source_url"], mutation=label):
                    body = mutate(v["body"])
                    self.assertNotEqual(body, v["body"])
                    result = self.evaluate(body, full, {short: full})
                    self.assertNotEqual(result["status"], "pass")
            with self.subTest(url=v["source_url"], mutation="other SHA"):
                other = "d" * 40
                self.assertNotEqual(
                    self.evaluate(v["body"], full, {short: other})["status"], "pass"
                )
            with self.subTest(url=v["source_url"], mutation="User with same login"):
                user = {"login": "chatgpt-codex-connector", "type": "User"}
                self.assertNotEqual(
                    self.evaluate(v["body"], full, {short: full}, who=user)["status"],
                    "pass",
                )

    def test_variants_pass_end_to_end_through_graphql_normalization(self):
        v = load_variants()[0]
        full, short = v["resolved_full"], v["reviewed_short"]
        srv = Server()
        srv.standard(heads=(full, full), issues=[g_issue(v["body"])])
        srv.resolve(short, full)
        proc, result, _ = run_check(srv.entries, head=full)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "pass", result)


class GraphQLNormalization(unittest.TestCase):
    def test_bot_login_gets_suffix_and_user_with_same_login_is_untrusted(self):
        srv = Server().standard(
            reviews=[g_review(who=gactor("chatgpt-codex-connector", "User"))]
        )
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(result["status"], "pass")
        srv = Server().standard(reviews=[g_review()])
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(result["status"], "pass", result)

    def test_missing_author_is_not_trusted(self):
        review_ = g_review()
        review_["author"] = None
        proc, result, _ = run_check(Server().standard(reviews=[review_]).entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(result["status"], "pass")

    def test_stale_codex_review_and_clean_comment_do_not_pass(self):
        srv = Server().standard(reviews=[g_review(sha=OLD)])
        proc, result, _ = run_check(srv.entries)
        self.assertNotEqual(result["status"], "pass")
        self.assertIn(
            "Codex review is stale or lacks the full current commit_id",
            result["limitations"],
        )
        srv = Server().standard(issues=[g_issue(CLEAN)])
        srv.resolve("aaaaaaaaaaaa", OLD)
        proc, result, _ = run_check(srv.entries)
        self.assertNotEqual(result["status"], "pass")
        srv = Server().standard(issues=[g_issue(CLEAN)])
        srv.resolve("aaaaaaaaaaaa", HEAD)
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(result["status"], "pass", result)

    def test_unresolvable_short_sha_is_not_pass(self):
        # Успешный ответ: object null или не Commit (нет oid) -> None, не pass и не отказ.
        for obj in (None, {}, {"__typename": "Tree"}):
            srv = Server().standard(issues=[g_issue(CLEAN)])
            srv.add(
                "Resolve",
                {"owner": "acme", "name": "widget", "ref": "aaaaaaaaaaaa"},
                [{"data": {"repository": {"object": obj}}}],
            )
            proc, result, _ = run_check(srv.entries)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotEqual(result["status"], "pass")

    def test_resolve_failure_is_a_refusal_not_findings(self):
        # Нет записи Resolve: фейковый gh завершается rc 1 -> сбой gh.
        srv = Server().standard(issues=[g_issue(CLEAN)])
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stderr.strip(), "pr_review: gh api graphql failed")
        self.assertNotIn("canary", proc.stderr)
        self.assertIsNone(result)
        # errors / нет repository в ответе Resolve.
        for response in (
            {"errors": [{"message": "private-diagnostic-canary"}]},
            {"data": {"repository": None}},
            {"data": None},
        ):
            srv = Server().standard(issues=[g_issue(CLEAN)])
            srv.add(
                "Resolve",
                {"owner": "acme", "name": "widget", "ref": "aaaaaaaaaaaa"},
                [response],
            )
            proc, result, _ = run_check(srv.entries)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertEqual(proc.stderr.strip(), "pr_review: gh api graphql failed")
            self.assertNotIn("canary", proc.stderr)
            self.assertIsNone(result)

    def test_thread_comment_maps_commit_ids_and_review_id(self):
        thread = {
            "id": "T1",
            "comments": [g_comment("bug", sha=HEAD, orig=OLD, ident=9, review_id=1)],
        }
        srv = Server().standard(reviews=[g_review()], threads=[thread])
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(result["status"], "findings")
        found = result["findings"][0]
        self.assertEqual(found["source"], "review_comment")
        self.assertEqual(found["commit_id"], HEAD)
        self.assertEqual(found["original_commit_id"], OLD)
        self.assertEqual(found["author"], CODEX)
        self.assertEqual(found["url"], "https://thread-comment/9")


class GraphQLPagination(unittest.TestCase):
    def test_all_thread_pages_are_read_and_last_page_finding_is_found(self):
        threads = [
            {"id": f"T{n}", "comments": [g_comment("old", sha=OLD, ident=n)]}
            for n in range(4)
        ]
        threads.append(
            {"id": "T4", "comments": [g_comment("real bug", sha=HEAD, ident=77)]}
        )
        srv = Server().standard(threads=threads, size=2)
        proc, result, calls = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "findings")
        self.assertEqual([f["url"] for f in result["findings"]], ["https://thread-comment/77"])
        thread_calls = [q for q in sent_queries(calls) if q.lstrip().startswith("query Threads")]
        self.assertGreaterEqual(len(thread_calls), 6)  # 3 pages, read twice
        self.assertIn(
            "pageInfo", thread_calls[0]
        )

    def test_thread_with_more_than_100_comments_is_drained(self):
        comments = [g_comment("old", sha=OLD, ident=n) for n in range(1, 250)]
        comments.append(g_comment("late bug", sha=HEAD, ident=999))
        srv = Server().standard(threads=[{"id": "BIG", "comments": comments}])
        proc, result, calls = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "findings")
        self.assertEqual([f["url"] for f in result["findings"]], ["https://thread-comment/999"])
        self.assertTrue(
            any(q.lstrip().startswith("query ThreadComments") for q in sent_queries(calls))
        )

    def test_reviews_and_issue_comments_on_multiple_pages(self):
        noise = [g_review("COMMENTED", HEAD, "", n, gactor("someone", "User")) for n in range(1, 5)]
        srv = Server().standard(reviews=noise + [g_review(ident=50)], size=2)
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(result["status"], "pass", result)
        issues = [g_issue("hi", ident=n, who=gactor("someone", "User")) for n in range(1, 5)]
        srv = Server().standard(issues=issues + [g_issue(CLEAN, ident=60)], size=2)
        srv.resolve("aaaaaaaaaaaa", HEAD)
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(result["status"], "pass", result)

    def test_check_runs_suite_and_nested_run_pagination_with_summary(self):
        runs = [g_run(f"job{n}") for n in range(120)]
        runs.append(g_run("slow", "IN_PROGRESS", None))
        runs.append(g_run("bad", "COMPLETED", "FAILURE"))
        suites = [g_suite(f"S{n}", [g_run(f"s{n}")], app=f"app{n}") for n in range(3)]
        suites.append(g_suite("BIG", runs))
        srv = Server().standard(reviews=[g_review()], suites=suites, size=2)
        proc, result, calls = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "pass")  # checks never change status
        self.assertEqual(result["checks"]["total"], 3 + 122)
        self.assertEqual(result["checks"]["pending"], ["slow"])
        self.assertEqual(result["checks"]["failed"], [["bad", "FAILURE"]])
        self.assertIn(
            {"name": "s1", "status": "COMPLETED", "conclusion": "SUCCESS", "app": "app1"},
            result["check_runs"],
        )
        self.assertTrue(
            any(q.lstrip().startswith("query SuiteRuns") for q in sent_queries(calls))
        )


class LivePr71Fixture(unittest.TestCase):
    """Живые ответы GraphQL по bronxtc52/skill-superarmanda#71 (записаны самим check)."""

    LIVE_HEAD = "98b72fc0900086732249dea82c6014c380500893"
    OLD_HEAD = "9306e856ed0e2927d96e17b4c60103f18ea0f700"
    THREAD_URL = "https://github.com/bronxtc52/skill-superarmanda/pull/71#discussion_r4176410524"

    def entries(self):
        return json.loads((FIXTURES / "pr71-graphql.json").read_text("utf-8"))["entries"]

    def run71(self, head):
        return run_check(self.entries(), head=head, repo="bronxtc52/skill-superarmanda", pr_no="71")

    def test_live_head_is_findings_not_pass_with_the_expected_evidence(self):
        proc, result, calls = self.run71(self.LIVE_HEAD)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Codex оставил чистый комментарий на 98b72fc (он распознан и резолвится в HEAD),
        # но inline-тред с 9306e85 перенесён GitHub на текущий HEAD (commit.oid == HEAD,
        # originalCommit.oid == 9306e85), а три комментария CodeRabbit — не шаблон
        # пропуска. Поэтому честный статус — findings, а не pass.
        self.assertEqual(result["status"], "findings")
        self.assertEqual(result["current_head"], self.LIVE_HEAD)
        self.assertIn(result["coderabbit"]["status"], ("pass", "findings"))
        threads = [f for f in result["findings"] if f["source"] == "review_comment"]
        self.assertEqual([f["url"] for f in threads], [self.THREAD_URL])
        self.assertEqual(threads[0]["commit_id"], self.LIVE_HEAD)
        self.assertEqual(threads[0]["original_commit_id"], self.OLD_HEAD)
        self.assertEqual(
            [f["author"] for f in result["findings"] if f["source"] == "issue_comment"],
            ["coderabbitai[bot]"] * 3,
        )
        self.assertNotIn(
            "https://github.com/bronxtc52/skill-superarmanda/pull/71#issuecomment-5977422763",
            [f["url"] for f in result["findings"]],
        )
        # Чистый комментарий на старом HEAD (9306e85) не резолвится в текущий.
        self.assertIn(
            "Codex clean comment does not resolve to current full SHA",
            result["limitations"],
        )
        self.assertEqual(result["checks"], {"total": 2, "pending": [], "failed": []})
        self.assertEqual(
            sorted(r["name"] for r in result["check_runs"]),
            ["tests (macos-latest)", "tests (ubuntu-latest)"],
        )
        for call in calls:
            self.assertEqual(call[:4], ["api", "graphql", "--hostname", "github.com"])
        for query in sent_queries(calls):
            self.assertTrue(query.lstrip().startswith("query"))
            self.assertNotIn("mutation", query)

    def test_old_head_is_incomplete_head_differs(self):
        proc, result, _ = self.run71(self.OLD_HEAD)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("PR HEAD differs from expected SHA", result["limitations"])
        self.assertEqual(result["current_head"], self.LIVE_HEAD)

    def test_fixture_has_no_leaked_coderabbit_scope_token(self):
        raw = (FIXTURES / "pr71-graphql.json").read_text("utf-8")
        self.assertNotIn("scope=ghh_", raw)


class CodeRabbitResetOnMovingEvidence(unittest.TestCase):
    """Смена HEAD или evidence между снимками сбрасывает и status, и coderabbit."""

    LIVE_HEAD = LivePr71Fixture.LIVE_HEAD

    def entries(self):
        return json.loads((FIXTURES / "pr71-graphql.json").read_text("utf-8"))["entries"]

    def run71(self, entries):
        return run_check(
            entries, head=self.LIVE_HEAD, repo="bronxtc52/skill-superarmanda", pr_no="71"
        )

    def assert_pending(self, result, text):
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(text, result["limitations"])
        cr = result["coderabbit"]
        self.assertEqual(cr["status"], "pending")
        self.assertTrue(isinstance(cr["reason"], str) and cr["reason"])
        self.assertIsNone(cr["evidence_url"])
        self.assertEqual(set(cr), {"status", "reason", "evidence_url"})

    def test_head_change_between_snapshots_makes_coderabbit_pending(self):
        entries = self.entries()
        header = next(e for e in entries if e["name"] == "Header")
        header["responses"][1]["data"]["repository"]["pullRequest"]["headRefOid"] = (
            "f" * 40
        )
        proc, result, _ = self.run71(entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_pending(result, "PR HEAD changed while collecting evidence")

    def test_evidence_change_between_snapshots_makes_coderabbit_pending(self):
        entries = self.entries()
        ic = next(e for e in entries if e["name"] == "IssueComments")
        nodes = ic["responses"][1]["data"]["repository"]["pullRequest"]["comments"]["nodes"]
        nodes[-1]["body"] = nodes[-1]["body"] + "\nedited between snapshots"
        proc, result, _ = self.run71(entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_pending(result, "review evidence changed while collecting evidence")


class GraphQLFailClosed(unittest.TestCase):
    def assert_refused(self, entries, expect_calls=True):
        proc, result, _ = run_check(entries)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stderr.strip(), "pr_review: gh api graphql failed")
        self.assertNotIn("canary", proc.stderr)
        self.assertIsNone(result)

    def tamper(self, name, mutate, **kw):
        srv = Server().standard(**kw)
        for entry in srv.entries:
            if entry["name"] == name:
                entry["responses"] = [mutate(json.loads(json.dumps(r))) for r in entry["responses"]]
                break
        return srv.entries

    def test_errors_key_in_response(self):
        def add_errors(r):
            r["errors"] = [{"message": "private-diagnostic-canary"}]
            return r

        self.assert_refused(self.tamper("Reviews", add_errors))

    def test_has_next_page_without_end_cursor(self):
        def broken(r):
            info = r["data"]["repository"]["pullRequest"]["reviews"]["pageInfo"]
            info.update(hasNextPage=True, endCursor=None)
            return r

        self.assert_refused(self.tamper("Reviews", broken))

    def test_repeated_cursor(self):
        srv = Server().standard(reviews=[g_review(ident=n) for n in range(1, 5)], size=2)
        for entry in srv.entries:
            if entry["name"] == "Reviews" and entry["variables"].get("cursor"):
                for r in entry["responses"]:
                    c = r["data"]["repository"]["pullRequest"]["reviews"]
                    c["pageInfo"] = {"hasNextPage": True, "endCursor": "Reviews-0"}
        self.assert_refused(srv.entries)
        # Отказ сразу на втором повторе курсора, а не по MAX_PAGES.
        _, _, calls = run_check(srv.entries)
        reviews = [q for q in sent_queries(calls) if q.lstrip().startswith("query Reviews")]
        self.assertLessEqual(len(reviews), 3, len(reviews))

    def test_missing_page_info_or_nodes_not_a_list(self):
        def no_info(r):
            del r["data"]["repository"]["pullRequest"]["comments"]["pageInfo"]
            return r

        def bad_nodes(r):
            r["data"]["repository"]["pullRequest"]["comments"]["nodes"] = {}
            return r

        self.assert_refused(self.tamper("IssueComments", no_info))
        self.assert_refused(self.tamper("IssueComments", bad_nodes))

    def test_pull_request_null_and_missing_data(self):
        def null_pr(r):
            r["data"]["repository"]["pullRequest"] = None
            return r

        self.assert_refused(self.tamper("Header", null_pr))
        self.assert_refused(self.tamper("Reviews", lambda r: {"data": None}))

    def test_check_runs_failure_is_fatal_not_an_empty_list(self):
        def no_object(r):
            r["data"]["repository"]["object"] = None
            return r

        self.assert_refused(self.tamper("Checks", no_object))
        entries = [e for e in Server().standard().entries if e["name"] != "Checks"]
        self.assert_refused(entries)

    def test_gh_crash_has_no_raw_diagnostics(self):
        entries = [e for e in Server().standard().entries if e["name"] != "Reviews"]
        self.assert_refused(entries)

    def test_max_pages_exceeded(self):
        def endless(query, variables=None):
            cursor = (variables or {}).get("cursor")
            n = 0 if cursor is None else int(cursor.split("-")[1]) + 1
            return {
                "repository": {
                    "pullRequest": {
                        "headRefOid": HEAD,
                        "isDraft": False,
                        "state": "OPEN",
                        "reviews": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": f"c-{n}"},
                        },
                    }
                }
            }

        calls = []

        def counting(query, variables=None):
            calls.append(query)
            return endless(query, variables)

        with mock.patch.object(MODULE, "graphql", side_effect=counting):
            with self.assertRaisesRegex(RuntimeError, "^gh api graphql failed$"):
                MODULE.fetch_reviews("acme", "widget", 42)
        self.assertEqual(len(calls), MODULE.MAX_PAGES)


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
                with self.subTest(repo=name), mock.patch.object(
                    MODULE, "graphql"
                ) as remote:
                    with self.assertRaisesRegex(
                        ValueError, "--repo must be OWNER/NAME"
                    ):
                        MODULE.check(args)
                    remote.assert_not_called()
        self.assertIsNotNone(MODULE.REPOSITORY.fullmatch("owner/.github"))

    def test_github_failures_do_not_expose_raw_diagnostics(self):
        failures = (
            subprocess.CalledProcessError(1, ["gh"], stderr=b"private-diagnostic-canary"),
            OSError("private-diagnostic-canary"),
            subprocess.TimeoutExpired(["gh"], 20),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                MODULE.subprocess, "check_output", side_effect=failure
            ):
                with self.assertRaisesRegex(RuntimeError, "^gh api graphql failed$"):
                    MODULE.graphql("query Header { viewer { login } }")

    def test_malformed_graphql_responses_are_sanitized(self):
        for raw in (
            "not json private-diagnostic-canary",
            "[]",
            "null",
            '{"errors":[{"message":"private-diagnostic-canary"}]}',
            '{"errors":[],"data":{}}',
            '{"nodata":1}',
            '{"data":null}',
        ):
            with self.subTest(raw=raw), mock.patch.object(
                MODULE.subprocess, "check_output", return_value=raw
            ):
                with self.assertRaisesRegex(RuntimeError, "^gh api graphql failed$"):
                    MODULE.graphql("query Header { viewer { login } }")

    def test_graphql_argv_is_read_only_and_variables_are_typed(self):
        with mock.patch.object(
            MODULE.subprocess, "check_output", return_value='{"data":{"a":1}}'
        ) as call:
            self.assertEqual(
                MODULE.graphql("query X { a }", {"owner": "acme", "number": 7}),
                {"a": 1},
            )
        argv = call.call_args.args[0]
        self.assertEqual(
            argv[:5], ["gh", "api", "graphql", "--hostname", "github.com"]
        )
        self.assertIn("query=query X { a }", argv)
        self.assertEqual(argv[argv.index("owner=acme") - 1], "-f")
        self.assertEqual(argv[argv.index("number=7") - 1], "-F")
        self.assertEqual(call.call_args.kwargs["timeout"], 20)

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
            worktree = Path(directory) / "synthetic-git"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            bindir = Path(directory) / "bin"
            bindir.mkdir()
            fake = bindir / "gh"
            fake.write_text(FAKE_GH, encoding="utf-8")
            fake.chmod(0o755)
            log, fixture = Path(directory) / "calls.jsonl", Path(directory) / "f.json"
            fixture.write_text("[]", encoding="utf-8")
            env = {
                **os.environ,
                "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
                "PR_REVIEW_GH_LOG": str(log),
                "PR_REVIEW_FIXTURE": str(fixture),
            }
            proc = subprocess.run(
                [
                    "python3", str(SCRIPT), "check", "--repo", "github-owner/github-name",
                    "--pr", "7", "--head", HEAD, "--worktree", str(worktree),
                    "--output", str(worktree / "evidence.json"),
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

    def test_all_requests_are_graphql_queries_and_head_change_fails_closed(self):
        # A multi-page review list whose second page would pass on its own;
        # the final PR head differs, so the clean evidence must not win.
        noise = [g_review("COMMENTED", HEAD, "", n, gactor("someone", "User")) for n in range(1, 4)]
        srv = Server().standard(
            heads=(HEAD, MOVED), reviews=noise + [g_review(ident=201)], size=2
        )
        proc, result, calls = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["current_head"], MOVED)
        self.assertIn("PR HEAD changed while collecting evidence", result["limitations"])
        self.assertTrue(calls)
        self.assertTrue(all(call[:4] == ["api", "graphql", "--hostname", "github.com"] for call in calls))
        queries = sent_queries(calls)
        self.assertEqual(len(queries), len(calls))
        for query in queries:
            self.assertTrue(query.lstrip().startswith("query"), query[:40])
            self.assertNotIn("mutation", query)
        rest = [a for call in calls for a in call if a.startswith("repos/")]
        self.assertEqual(rest, [])

    def test_current_finding_added_during_second_snapshot_is_incomplete(self):
        srv = Server().standard(
            reviews=Snap(
                [g_review()],
                [g_review(), g_review("COMMENTED", HEAD, "new finding", 2)],
            )
        )
        proc, result, _ = run_check(srv.entries)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(
            "review evidence changed while collecting evidence", result["limitations"]
        )



class CodeRabbitFact(unittest.TestCase):
    """Результат CodeRabbit по факту ревью HEAD (issue #72); фикстуры сняты с живых PR."""

    H70 = "4930cffa099e1164f6c0777196fc741d915396e8"
    H71 = "98b72fc0900086732249dea82c6014c380500893"
    H592 = "c1fe47b26b91f046c58b02213b9aa20b85b49b70"
    OTHER = "1234567890abcdef1234567890abcdef12345678"
    RABBIT = "coderabbitai[bot]"

    def load(self, n):
        raw = (FIXTURES / "coderabbit" / f"pr{n}-rest.json").read_text("utf-8")
        return json.loads(raw)

    def run_eval(self, data, head=None):
        head = head or data["head"]["sha"]
        return MODULE.evaluate(
            {"head": {"sha": data["head"]["sha"]}, "draft": False},
            data["reviews"],
            data["review_comments"],
            data["issue_comments"],
            head,
            lambda ref: None,
        )

    def rabbit(self, data, key="issue_comments"):
        return [x for x in data[key] if x["user"]["login"] == self.RABBIT]

    def summary71(self, data):
        return next(x for x in self.rabbit(data) if "No actionable comments" in x["body"])

    def only_rabbit_evidence(self, data):
        for key in ("reviews", "review_comments", "issue_comments"):
            data[key] = [x for x in data[key] if x["user"]["login"] == self.RABBIT]
        return data

    def test_pr71_summary_range_is_pass(self):
        result = self.run_eval(self.load(71))
        self.assertEqual(result["coderabbit"]["status"], "pass", result["coderabbit"])
        self.assertTrue(result["coderabbit"]["evidence_url"])

    def test_pr70_empty_review_on_head_is_pass_and_not_incomplete(self):
        result = self.run_eval(self.load(70))
        self.assertEqual(result["coderabbit"]["status"], "pass", result["coderabbit"])
        self.assertNotEqual(result["status"], "incomplete")
        self.assertNotIn(
            "empty CodeRabbit COMMENTED review is incomplete", result["limitations"]
        )

    def test_pr592_limit_reached_is_unavailable(self):
        result = self.run_eval(self.load(592))
        verdict = result["coderabbit"]
        self.assertEqual(verdict["status"], "unavailable", verdict)
        self.assertTrue(verdict["reason"].startswith("CodeRabbit refused: "))
        self.assertTrue(verdict["evidence_url"])

    def test_field_present_when_head_differs(self):
        result = self.run_eval(self.load(71), head=self.OTHER)
        self.assertEqual(result["coderabbit"]["status"], "pending")
        self.assertTrue(result["coderabbit"]["reason"])
        self.assertIsNone(result["coderabbit"]["evidence_url"])

    def test_range_end_replaced_is_not_pass(self):
        data = self.load(71)
        item = self.summary71(data)
        item["body"] = item["body"].replace(self.H71, self.OTHER)
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_range_end_uppercase_is_still_pass(self):
        data = self.load(71)
        item = self.summary71(data)
        item["body"] = item["body"].replace(self.H71, self.H71.upper())
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_abbreviated_range_end_is_not_pass(self):
        data = self.load(71)
        item = self.summary71(data)
        item["body"] = item["body"].replace(self.H71, self.H71[:12])
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def quote_lines(self, body, needles):
        return "\n".join(
            "> " + line if any(n in line for n in needles) else line
            for line in body.split("\n")
        )

    def test_marker_in_blockquote_is_not_pass(self):
        data = self.load(71)
        item = self.summary71(data)
        item["body"] = self.quote_lines(item["body"], ["No actionable comments"])
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_range_in_blockquote_is_not_pass(self):
        data = self.load(71)
        item = self.summary71(data)
        item["body"] = self.quote_lines(item["body"], ["between "])
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_empty_review_on_other_commit_is_not_pass(self):
        data = self.only_rabbit_evidence(self.load(70))
        data["issue_comments"] = []
        for item in data["reviews"]:
            item["commit_id"] = self.OTHER
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_same_login_but_user_type_is_ignored(self):
        data = self.only_rabbit_evidence(self.load(70))
        data["issue_comments"] = []
        for item in data["reviews"]:
            item["user"]["type"] = "User"
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "pending")
        data = self.load(71)
        for item in data["issue_comments"]:
            if item["user"]["login"] == self.RABBIT:
                item["user"]["type"] = "User"
        self.assertNotEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def rabbit_inline(self, ident, commit):
        return {
            "id": ident,
            "user": {"login": self.RABBIT, "type": "Bot"},
            "body": "Потенциальная проблема: проверьте границы.",
            "commit_id": commit,
            "html_url": f"https://github.com/x/y/pull/1#discussion_r{ident}",
        }

    def test_inline_comment_on_head_is_findings(self):
        data = self.load(71)
        data["review_comments"].append(self.rabbit_inline(1, self.H71))
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "findings")

    def test_inline_comment_on_old_commit_is_not_findings(self):
        data = self.load(71)
        data["review_comments"].append(self.rabbit_inline(1, self.OTHER))
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "pass")

    def test_review_with_body_on_head_is_findings(self):
        data = self.load(70)
        data["reviews"].append(
            {
                "id": 77,
                "user": {"login": self.RABBIT, "type": "Bot"},
                "state": "COMMENTED",
                "body": "Замечание по коду.",
                "commit_id": self.H70,
                "html_url": "https://github.com/x/y/pull/1#pullrequestreview-77",
            }
        )
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "findings")

    def test_changes_requested_on_head_is_findings(self):
        data = self.load(70)
        data["reviews"].append(
            {
                "id": 78,
                "user": {"login": self.RABBIT, "type": "Bot"},
                "state": "CHANGES_REQUESTED",
                "body": "",
                "commit_id": self.H70,
                "html_url": "https://github.com/x/y/pull/1#pullrequestreview-78",
            }
        )
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "findings")

    def test_refusal_with_range_not_ending_at_head_is_not_unavailable(self):
        data = self.load(592)
        for item in data["issue_comments"]:
            item["body"] = item["body"].replace(self.H592, self.OTHER)
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "pending")

    def test_refusal_without_range_counts(self):
        data = self.load(592)
        data["issue_comments"] = [
            {
                "id": 5,
                "user": {"login": self.RABBIT, "type": "Bot"},
                "body": "Review rate limited. Try later.",
                "html_url": "https://github.com/x/y/pull/1#issuecomment-5",
            }
        ]
        verdict = self.run_eval(data)["coderabbit"]
        self.assertEqual(verdict["status"], "unavailable", verdict)
        self.assertEqual(
            verdict["evidence_url"], "https://github.com/x/y/pull/1#issuecomment-5"
        )

    def test_no_evidence_is_pending(self):
        data = self.load(592)
        data["issue_comments"] = []
        verdict = self.run_eval(data)["coderabbit"]
        self.assertEqual(verdict["status"], "pending")
        self.assertIn("no CodeRabbit review bound to current HEAD yet", verdict["reason"])

    def test_draft_skip_is_pending_with_specific_reason(self):
        data = self.load(592)
        data["issue_comments"] = [
            {
                "id": 6,
                "user": {"login": self.RABBIT, "type": "Bot"},
                "body": CODERABBIT_DRAFT_SKIP,
                "html_url": "https://x/6",
            }
        ]
        verdict = self.run_eval(data)["coderabbit"]
        self.assertEqual(verdict["status"], "pending")
        self.assertIn("draft", verdict["reason"])

    def test_findings_with_proof_stay_findings(self):
        data = self.load(71)
        data["review_comments"].append(self.rabbit_inline(2, self.H71))
        self.assertEqual(self.run_eval(data)["coderabbit"]["status"], "findings")

    def test_main_status_unchanged_for_pr71(self):
        # Issue-комментарии CodeRabbit по-прежнему находки: слова внутри текста их не гасят.
        result = self.run_eval(self.load(71))
        self.assertEqual(result["status"], "findings")
        self.assertTrue([f for f in result["findings"] if f["source"] == "issue_comment"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
