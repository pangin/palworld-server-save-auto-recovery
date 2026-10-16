"""Offline CI identity, completion resumption and explicit failure-stop tests."""

from copy import deepcopy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import cloud_consensus as cc
import cloud_controller as c
import cloud_state as state
import test_cloud_consensus as consensus_fixtures
import test_cloud_controller as controller_fixtures
from test_cloud_state import repo, NOW


class CIIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = consensus_fixtures.AdapterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_normalize_only_strict_canonical_workflow_and_optional_ref(self):
        path = ".github/workflows/build.yaml"
        for suffix in ("", "@main", "@refs/heads/topic/ci", "@refs/pull/7/merge", "@" + "a" * 40):
            with self.subTest(suffix=suffix):
                self.assertEqual(cc.workflow_path(path + suffix), path)
        for value in (None, [], "https://github.com/a/b/build.yml@main", "../build.yml@main",
                      ".github/workflows/../build.yml@main", path + "@", path + "@main@other",
                      path + "@../main", path + "@refs//heads/main", path + "@refs/heads/.hidden",
                      path + "@refs/heads/topic.lock", path + "@https://evil.invalid/x",
                      path + "@main\n", path + "@refs/heads/main/", path + "@main%2Fother"):
            with self.subTest(value=value), self.assertRaises(state.StateError):
                cc.workflow_path(value)

    def test_required_ci_accepts_ref_suffix_without_weakening_identity(self):
        fixture = self.fixture
        fixture.run["path"] += "@refs/heads/topic/ci"
        proof = cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE)
        self.assertEqual(proof["check_run_ids"], [fixture.check["id"]])
        for changes in ({"path": ".github/workflows/wrong.yml@main"},
                        {"head_repository": {"full_name": "other/example"}},
                        {"head_sha": "d" * 40}, {"event": "push"}, {"id": 43}):
            before = deepcopy(fixture.run)
            fixture.run.update(changes)
            with self.subTest(changes=changes), self.assertRaises(state.StateError):
                cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE)
            fixture.run = before

    def test_completed_failure_is_not_pending_and_does_not_trust_check_names_alone(self):
        fixture = self.fixture
        fixture.check["conclusion"] = fixture.run["conclusion"] = "failure"
        with self.assertRaises(cc.RequiredCIFailed) as caught:
            cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE)
        self.assertEqual(caught.exception.failures, [{"check_run_id": 101, "workflow_run_id": 42,
            "workflow": consensus_fixtures.PROFILE[0]["workflow"], "conclusions": ["failure"]}])
        fixture.run["path"] = ".github/workflows/unapproved.yml@main"
        with self.assertRaises(state.StateError) as caught:
            cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE)
        self.assertNotIsInstance(caught.exception, cc.RequiredCIFailed)

    def test_pending_check_does_not_hide_later_required_failure(self):
        fixture = self.fixture
        fixture.check["conclusion"] = fixture.run["conclusion"] = "timed_out"
        earlier_pending = {**consensus_fixtures.PROFILE[0], "name": "earlier-pending"}
        with self.assertRaises(cc.RequiredCIFailed):
            cc.required_ci(fixture.api, fixture.feedback, [earlier_pending] + consensus_fixtures.PROFILE)
        fixture.check.update(status="in_progress", conclusion=None)
        self.assertIsNone(cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE))

    def test_old_base_failure_is_not_a_current_candidate_failure(self):
        fixture = self.fixture
        fixture.check["conclusion"] = fixture.run["conclusion"] = "failure"
        fixture.run["pull_requests"][0]["base"]["sha"] = "c" * 40
        self.assertIsNone(cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE))

    def test_in_progress_overall_workflow_is_still_pending(self):
        fixture = self.fixture
        fixture.run.update(status="in_progress", conclusion=None)
        self.assertIsNone(cc.required_ci(fixture.api, fixture.feedback, consensus_fixtures.PROFILE))

    def test_failed_native_final_ci_notifies_once_without_model_attempt(self):
        fixture = self.fixture
        fixture.check["conclusion"] = fixture.run["conclusion"] = "failure"
        self.assertNotIn("run_claude", fixture.prepare())
        self.assertEqual(fixture.record["state"], "needs_review")
        self.assertEqual(fixture.record["ci_failure"]["notification_status"], "posted")
        self.assertNotIn("verification_attempts", fixture.record)
        self.assertFalse((fixture.root / "job.json").exists())
        self.assertEqual(len(fixture.native_requests), 1)
        self.assertIn("@pangin", fixture.native_requests[0])
        self.assertTrue(fixture.native_requests[0].startswith(cc.CI_FAILURE_MARKER))
        self.assertNotIn("run_claude", fixture.prepare())
        self.assertEqual(len(fixture.native_requests), 1)

    def test_failure_notification_never_replaces_owner_stop_or_active_lease(self):
        fixture = self.fixture
        failure = cc.RequiredCIFailed([])
        for current_state in ("processing", "verifying", "needs_user_input", "failed", "complete"):
            ledger = deepcopy(fixture.ledger)
            record = ledger["pulls"]["pangin/example#7"]
            record["state"] = current_state
            with patch.object(c, "ledger_write") as write:
                cc.notify_ci_failure(c, fixture.root, fixture.api, fixture.feedback, ledger, 99, failure)
            write.assert_not_called()
            self.assertEqual(record["state"], current_state)
        ledger = deepcopy(fixture.ledger)
        ledger["pulls"]["pangin/example#7"]["human_response"] = {"option_id": "stop"}
        with patch.object(c, "ledger_write") as write:
            cc.notify_ci_failure(c, fixture.root, fixture.api, fixture.feedback, ledger, 99, failure)
        write.assert_not_called()

    def test_interrupted_notification_is_not_blindly_retried(self):
        fixture = self.fixture
        fixture.record["ci_failure"] = {"head_sha": fixture.head, "base_sha": fixture.head,
                                         "notification_status": "posting"}
        with patch.object(c, "ledger_write") as write, self.assertRaises(state.StateError):
            cc.notify_ci_failure(c, fixture.root, fixture.api, fixture.feedback,
                                 fixture.ledger, 99, cc.RequiredCIFailed([]))
        write.assert_not_called()
        self.assertEqual(fixture.native_requests, [])


class CIEventTests(unittest.TestCase):
    def setUp(self):
        self.fixture = controller_fixtures.ControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.path, self.env = fixture.prepare_fixture(findings=False)
        self.env["GITHUB_EVENT_NAME"] = "workflow_run"
        config = c.load(self.path)
        config["required_ci"][0]["workflow"] = ".github/workflows/custom-ci.yml"
        c.save(self.path, config)
        self.run = {"id": 42, "event": "pull_request", "status": "completed", "conclusion": "success",
                    "repository": repo(), "head_repository": repo(), "head_sha": fixture.head,
                    "path": ".github/workflows/custom-ci.yml@refs/heads/fix/example",
                    "pull_requests": [{"number": 7, "head": {"sha": fixture.head}, "base": {"sha": "b" * 40}}]}
        self.check = {"id": 101, "name": "application-container-smoke", "head_sha": fixture.head,
                      "app": {"id": 15368, "slug": "github-actions"}, "status": "completed", "conclusion": "success",
                      "details_url": "https://github.com/pangin/example/actions/runs/42/job/101"}
        self.conversation = []
        old_request, old_pages = fixture.api.request.side_effect, fixture.api.pages.side_effect

        def request(route, method="GET", payload=None):
            if route == "repos/pangin/example/actions/runs/42":
                return deepcopy(self.run)
            if "/check-runs?" in route:
                return {"check_runs": [deepcopy(self.check)]}
            if method == "POST" and route.endswith("/issues/7/comments"):
                item = {"id": 99 + len(self.conversation), "user": c.LEDGER_BOT, "body": payload["body"]}
                self.conversation.append(item)
                return {"id": item["id"]}
            if method == "PATCH" and route.endswith("/issues/comments/99"):
                self.conversation[0]["body"] = payload["body"]
                return {"id": 99}
            return old_request(route, method, payload)

        def pages(route):
            if route.endswith("/issues/7/comments"):
                return self.conversation
            return old_pages(route)

        fixture.api.request.side_effect, fixture.api.pages.side_effect = request, pages
        c.save(fixture.root / "event.json", {"action": "completed", "workflow_run": {"id": 42}})

    def prepare(self):
        fixture = self.fixture
        with patch.dict(os.environ, self.env, clear=True), patch.object(c, "now", return_value=NOW), \
                patch.object(c, "output") as output, patch.object(cc, "prepare") as final, \
                patch.object(c, "test_container") as tests, patch.object(c, "publish") as publisher:
            c.prepare(fixture.root, fixture.root / "event.json", self.path, fixture.api)
        tests.assert_not_called()
        publisher.assert_not_called()
        return output, final

    def test_other_configured_workflow_completion_can_resume_final_assessment(self):
        output, final = self.prepare()
        final.assert_called_once()
        self.assertNotIn(unittest.mock.call("run_claude", "true"), output.call_args_list)

    def test_completed_failure_notifies_owner_and_stops_without_any_model_reservation(self):
        self.check["conclusion"] = self.run["conclusion"] = "failure"
        output, final = self.prepare()
        final.assert_not_called()
        self.assertNotIn(unittest.mock.call("run_claude", "true"), output.call_args_list)
        _, ledger = c.ledger_read(self.fixture.api, "pangin/example", 7)
        record = ledger["pulls"]["pangin/example#7"]
        self.assertEqual(record["state"], "needs_review")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["ci_failure"]["notification_status"], "posted")
        self.assertTrue(any(item["body"].startswith(cc.CI_FAILURE_MARKER) and "@pangin" in item["body"]
                            for item in self.conversation))
        self.assertFalse((self.fixture.root / "job.json").exists())

    def test_unapproved_cross_repository_and_malformed_paths_cannot_resume_or_notify(self):
        for change in ({"path": ".github/workflows/unapproved.yml@main"},
                       {"path": ".github/workflows/custom-ci.yml@main@other"},
                       {"head_repository": {"full_name": "other/example"}},
                       {"event": "push"}):
            original = deepcopy(self.run)
            self.run.update(change)
            with self.subTest(change=change), self.assertRaises(state.StateError):
                self.prepare()
            self.assertEqual(self.conversation, [])
            self.run = original

    def test_stale_head_or_base_completion_cannot_resume_or_notify(self):
        original = deepcopy(self.run)
        self.run["head_sha"] = "c" * 40
        _, final = self.prepare()
        final.assert_not_called()
        self.run = original
        self.run["pull_requests"][0]["base"]["sha"] = "c" * 40
        _, final = self.prepare()
        final.assert_not_called()
        self.assertEqual(self.conversation, [])

    def test_workflow_subscribes_to_all_completions_and_does_not_filter_failure(self):
        from test_workflow_concurrency import workflow_text
        text = workflow_text()
        self.assertIn("workflows: ['*']", text)
        self.assertIn("types: [completed]", text)
        self.assertNotIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", text)


if __name__ == "__main__":
    unittest.main()
