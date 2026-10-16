"""Regression contract for the reviewed, literal workflow concurrency block.

This bounded check is not a general YAML parser or a substitute for actionlint.
It separately covers GitHub's documented queue property, unsupported by local
actionlint 1.7.12. GitHub's actual scheduling remains an activation-time check.
"""
from pathlib import Path
import unittest
from workflow_validation import check_concurrency, check_lint_result, QUEUE_DIAGNOSTIC


def workflow_text():
    directory = Path(__file__).resolve().parent
    path = directory / "cloud-workflow.yml"
    if not path.is_file():
        path = directory.parent / "workflows/codex-claude-cycle.yml"
    return path.read_text()


class WorkflowConcurrencyTests(unittest.TestCase):
    def test_review_ci_and_owner_response_share_pr_group(self):
        check_concurrency(workflow_text())
        # All three event shapes supply the same PR number in the exact paths
        # represented by EXPECTED_GROUP, not an empty/repository-wide group.
        events = [
            {"pull_request": {"number": 7}},
            {"issue": {"number": 7}},
            {"workflow_run": {"pull_requests": [{"number": 7}]}},
        ]
        numbers = [
            event.get("pull_request", {}).get("number")
            or event.get("issue", {}).get("number")
            or event.get("workflow_run", {}).get("pull_requests", [{}])[0].get("number")
            for event in events
        ]
        self.assertEqual(numbers, [7, 7, 7])

    def test_missing_owner_response_path_is_rejected(self):
        with self.assertRaises(ValueError):
            check_concurrency(workflow_text().replace("github.event.issue.number || ", ""))

    def test_replacing_pending_events_is_rejected(self):
        for replacement in ["  queue: single", "  queue: typo", "", "    queue: max"]:
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                check_concurrency(workflow_text().replace("  queue: max", replacement))

    def test_canceling_active_runs_is_rejected(self):
        with self.assertRaises(ValueError):
            check_concurrency(workflow_text().replace("cancel-in-progress: false", "cancel-in-progress: true"))

    def test_duplicate_or_unknown_fields_are_rejected(self):
        for extra in ["  queue: max", "  unexpected: max"]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                check_concurrency(workflow_text().replace("  queue: max", "  queue: max\n" + extra))

    def queue_diagnostic(self):
        return {"message": QUEUE_DIAGNOSTIC, "filepath": "cycle.yml",
                "line": workflow_text().splitlines().index("  queue: max") + 1,
                "column": 3, "kind": "syntax-check", "snippet": "  queue: max\n  ^~~~~~",
                "end_column": 8}

    def test_only_exact_single_queue_diagnostic_is_classified(self):
        self.assertEqual(check_lint_result(workflow_text(), "cycle.yml", 1, [self.queue_diagnostic()]),
                         "queue_tool_compatibility_only")
        self.assertEqual(check_lint_result(workflow_text(), "cycle.yml", 0, []),
                         "full_actionlint_pass")

    def test_other_lint_errors_or_diagnostic_locations_fail(self):
        for changes in [{"line": 1}, {"column": 4}, {"filepath": "other.yml"},
                        {"message": "other error"}, {"kind": "expression"},
                        {"snippet": "  queue: typo"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                check_lint_result(workflow_text(), "cycle.yml", 1, [{**self.queue_diagnostic(), **changes}])
        for diagnostics in [[], [self.queue_diagnostic(), self.queue_diagnostic()]]:
            with self.subTest(diagnostics=diagnostics), self.assertRaises(ValueError):
                check_lint_result(workflow_text(), "cycle.yml", 1, diagnostics)

    def test_unexpected_return_codes_or_success_with_errors_fail(self):
        for code in [0, 2, -1]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                check_lint_result(workflow_text(), "cycle.yml", code, [self.queue_diagnostic()])

    def test_tool_gap_cannot_excuse_invalid_queue_contract(self):
        text = workflow_text().replace("queue: max", "queue: single")
        with self.assertRaises(ValueError):
            check_lint_result(text, "cycle.yml", 1, [self.queue_diagnostic()])


if __name__ == "__main__":
    unittest.main()
