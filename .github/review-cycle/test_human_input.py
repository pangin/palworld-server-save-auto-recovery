import json
import unittest

import human_input as h


HEAD = "a" * 40
BASE = "b" * 40


def feedback(**changes):
    value = {"repository": "pangin/example", "pr_number": 7,
             "head_sha": HEAD, "base_sha": BASE, "source_review_ids": [11]}
    value.update(changes)
    return value


class HumanInputTests(unittest.TestCase):
    def setUp(self):
        self.request = h.build_request(
            feedback(), kind="review_clarification", summary="The reviewers disagree about the API compatibility requirement.",
            sources=["docs/review-contract.md#Compatibility"], created_at="2026-10-01T00:00:00Z")
        self.actor = {"login": "pangin", "type": "User", "id": 1}

    def test_question_is_bounded_and_visible(self):
        body = h.question_body(self.request)
        self.assertTrue(body.startswith(h.QUESTION_MARKER))
        self.assertIn("@pangin", body)
        self.assertIn("/review-cycle clarify", body)
        self.assertIn(self.request["request_id"], body)

    def test_owner_command_response_is_structured(self):
        result = h.parse_response(f"/review-cycle clarify {self.request['request_id']} Keep the compatibility behavior.", self.request, self.actor)
        self.assertEqual(result["option_id"], "clarify")
        self.assertEqual(result["message"], "Keep the compatibility behavior.")
        self.assertEqual(h.validate_response(result, self.request), result)

    def test_marked_json_response_requires_exact_request(self):
        body = h.RESPONSE_MARKER + "\n```json\n" + json.dumps({
            "schema_version": 1, "request_id": self.request["request_id"],
            "option_id": "update-docs", "message": "Use ADR-12.", "approved_revision": "c" * 40
        }) + "\n```"
        result = h.parse_response(body, self.request, self.actor)
        self.assertEqual(result["option_id"], "update-docs")
        with self.assertRaises(h.HumanInputError):
            h.parse_response(body.replace(self.request["request_id"], "human-other"), self.request, self.actor)

    def test_non_owner_and_ambiguous_prose_are_rejected(self):
        with self.assertRaises(h.HumanInputError):
            h.parse_response("Looks good to me", self.request, self.actor)
        with self.assertRaises(h.HumanInputError):
            h.parse_response("/review-cycle clarify", self.request, {"login": "someone-else", "type": "User"})

    def test_stop_needs_no_message(self):
        result = h.parse_response(f"/review-cycle stop {self.request['request_id']}", self.request, self.actor)
        self.assertEqual(result["option_id"], "stop")
        self.assertEqual(result["message"], "")


if __name__ == "__main__":
    unittest.main()
