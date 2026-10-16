"""Owner response binding regressions; pure parsing, no external operations."""
import json
import unittest

import human_input as h


class RequestBindingTests(unittest.TestCase):
    def setUp(self):
        self.request = h.build_request(
            {"repository": "pangin/example", "pr_number": 7, "head_sha": "a" * 40,
             "base_sha": "b" * 40, "source_review_ids": [10]},
            kind="criteria_conflict", summary="Choose the documented behavior.",
            sources=[], created_at="2026-10-16T00:00:00Z")
        self.actor = {"login": "pangin", "type": "User", "id": 1}
        self.rid = self.request["request_id"]

    def parse(self, body):
        return h.parse_response(body, self.request, self.actor)

    def test_all_plain_commands_require_exact_request_id(self):
        for option, tail in [("clarify", " Keep compatibility"), ("stop", ""),
                             ("update-docs", " " + "c" * 40 + " README.md clarified behavior")]:
            with self.subTest(option=option):
                result = self.parse(f"/review-cycle {option} {self.rid}{tail}")
                self.assertEqual(result["request_id"], self.rid)
                self.assertEqual(result["option_id"], option)
                for invalid in [f"/review-cycle {option}{tail}",
                                f"/review-cycle {option} human-{'0' * 20}{tail}"]:
                    with self.assertRaises(h.HumanInputError):
                        self.parse(invalid)

    def test_multiple_commands_are_never_first_match_wins(self):
        first = f"/review-cycle clarify {self.rid} Keep compatibility"
        for second in [f"/review-cycle stop {self.rid}", "/review-cycle unknown", "/review-cycle stop"]:
            with self.subTest(second=second), self.assertRaises(h.HumanInputError):
                self.parse(first + "\n" + second)

    def test_json_and_command_combination_is_ambiguous(self):
        body = h.RESPONSE_MARKER + "\n```json\n" + json.dumps({
            "request_id": self.rid, "option_id": "clarify", "message": "Keep compatibility"
        }) + "\n```\n" + f"/review-cycle stop {self.rid}"
        with self.assertRaises(h.HumanInputError):
            self.parse(body)

    def test_update_docs_requires_full_revision_and_decision(self):
        for tail in ["README.md", "c" * 39 + " README.md", "main README.md", "c" * 40]:
            with self.subTest(tail=tail), self.assertRaises(h.HumanInputError):
                self.parse(f"/review-cycle update-docs {self.rid} {tail}")
        result = self.parse(f"/review-cycle update-docs {self.rid} {'c' * 40} README.md clarified behavior")
        self.assertEqual(result["approved_revision"], "c" * 40)
        self.assertEqual(result["message"], "README.md clarified behavior")
        h.validate_response(result, self.request)

    def test_marked_json_requires_revision_only_for_update_docs(self):
        value = {"schema_version": 1, "request_id": self.rid, "option_id": "update-docs",
                 "message": "README.md clarified behavior"}
        def body(v):
            return h.RESPONSE_MARKER + "\n```json\n" + json.dumps(v) + "\n```"
        with self.assertRaises(h.HumanInputError):
            self.parse(body(value))
        self.assertEqual(self.parse(body({**value, "approved_revision": "c" * 40}))["approved_revision"], "c" * 40)
        with self.assertRaises(h.HumanInputError):
            self.parse(body({**value, "option_id": "clarify", "approved_revision": "c" * 40}))

    def test_visible_examples_bind_question_and_explain_default_revision(self):
        text = h.question_body(self.request)
        for option in ["clarify", "update-docs", "stop"]:
            self.assertIn(f"/review-cycle {option} {self.rid}", text)
        self.assertIn("full trusted default SHA", text)
        self.assertIn("never a PR candidate SHA", text)


if __name__ == "__main__":
    unittest.main()
