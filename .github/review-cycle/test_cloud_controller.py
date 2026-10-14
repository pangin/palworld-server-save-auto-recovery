import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import cloud_controller as c
import cloud_state as s
import cloud_consensus as consensus
import human_input
from test_cloud_state import feedback, repo, pr, review, comment, config, BOT, NOW


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.run_git("init", "-q")
        (self.workspace / "app.js").write_text("value = 1;\n")
        (self.workspace / "README.md").write_text("approved baseline\n")
        (self.workspace / "AGENTS.md").write_text("root criteria\n")
        (self.workspace / "src").mkdir()
        (self.workspace / "src/AGENTS.md").write_text("scoped criteria\n")
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
        self.head = self.run_git("rev-parse", "HEAD").strip()
        self.api = Mock()

    def execution_config(self, **changes):
        return config(**{"review_documents": ["README.md"],
                        "review_criteria": [{"id":"C1","source":"README.md","section":"Scope",
                                             "requirement":"Preserve approved behavior"}],
                        "required_ci": [{"name":"application-container-smoke",
                                         "workflow":".github/workflows/review-cycle-checks.yml",
                                         "app_id":15368,"app_slug":"github-actions"}], **changes})

    def prepare_fixture(self, *, findings=True, documents=None):
        path = self.root / "config.json"
        c.save(path, self.execution_config(review_documents=documents or ["README.md"]))
        candidate = pr(head=self.head)
        candidate["head"]["ref"] = "fix/example"
        candidate["base"]["ref"] = "main"
        c.save(self.root / "event.json", {"action": "submitted", "sender": BOT,
                                        "pull_request": {"number": 7}, "review": {"id": 10}})
        def request(route, method="GET", payload=None):
            if route.startswith("users/"): return BOT
            if route == "repos/pangin/example": return repo()
            if route == "repos/pangin/example/pulls/7": return candidate
            if method == "POST" and route.endswith("/issues/7/comments"): return {"id": 99}
            if method == "POST" and route.endswith("/issues/7/labels"): return {"labels": payload.get("labels", [])}
            if method == "PATCH" and route.endswith("/issues/comments/99"): return {"id": 99}
            raise AssertionError((route, method))
        def pages(route):
            if route.endswith("/reviews"): return [review(head=self.head)]
            if route.endswith("/pulls/7/comments"):
                return [comment(head=self.head, path="app.js")] if findings else []
            if route.endswith("/issues/7/comments"): return []
            if route.endswith("/files"): return [{"filename": "app.js"}]
            raise AssertionError(route)
        self.api.request.side_effect = request
        self.api.pages.side_effect = pages
        return path, {"GITHUB_REPOSITORY": "pangin/example", "GITHUB_RUN_ID": "123",
                      "GITHUB_WORKSPACE": str(self.workspace), "HAS_CLAUDE_TOKEN": "true",
                      "HAS_APP_ID": "true", "HAS_APP_KEY": "true"}

    def run_git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.workspace,
                              check=True, text=True, capture_output=True).stdout

    def protected_prepare_fixture(self, finding_path=".github/workflows/build.yml"):
        path, env = self.prepare_fixture()
        original_request = self.api.request.side_effect
        original_pages = self.api.pages.side_effect
        conversation = []

        def request(route, method="GET", payload=None):
            if method == "POST" and route.endswith("/issues/7/comments"):
                item = {"id": 99 + len(conversation), "user": c.LEDGER_BOT, "body": payload["body"]}
                conversation.append(item)
                return {"id": item["id"]}
            if method == "PATCH" and route.endswith("/issues/comments/99"):
                conversation[0]["body"] = payload["body"]
                return {"id": 99}
            return original_request(route, method, payload)

        def pages(route):
            if route.endswith("/pulls/7/comments"):
                return [comment(head=self.head, path=finding_path, body="[P1] untrusted private prose")]
            if route.endswith("/issues/7/comments"):
                return conversation
            return original_pages(route)

        self.api.request.side_effect = request
        self.api.pages.side_effect = pages
        return path, env, conversation

    def test_canonical_protected_or_invalid_finding_notifies_owner_without_model_or_push(self):
        for finding_path in [".github/workflows/build.yml", "AGENTS.md", "../untrusted-private-path"]:
            with self.subTest(finding_path=finding_path):
                self.api.reset_mock()
                path, env, conversation = self.protected_prepare_fixture(finding_path)
                with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW), \
                     patch.object(c, "output") as outputs, patch.object(c, "test_container") as tests, \
                     patch.object(c, "publish") as publish:
                    c.prepare(self.root, self.root / "event.json", path, self.api)
                questions = [item for item in conversation if item["body"].startswith(human_input.QUESTION_MARKER)]
                self.assertEqual(len(questions), 1)
                self.assertIn("@pangin", questions[0]["body"])
                self.assertNotIn(finding_path, questions[0]["body"])
                self.assertNotIn("untrusted private prose", str(conversation))
                _, ledger = c.ledger_read(self.api, "pangin/example", 7)
                record = ledger["pulls"]["pangin/example#7"]
                self.assertEqual(record["state"], "needs_user_input")
                self.assertEqual(record["attempts"], 0)
                self.assertEqual(record["human_request"]["source_review_ids"], [10])
                self.assertEqual(record["human_request"]["status"], "posted")
                self.assertEqual(outputs.call_args_list, [unittest.mock.call("run_claude", "false")])
                self.assertFalse((self.root / "job.json").exists())
                tests.assert_not_called()
                publish.assert_not_called()

    def test_repeated_protected_preflight_uses_existing_posted_owner_question(self):
        path, env, conversation = self.protected_prepare_fixture()
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW):
            c.prepare(self.root, self.root / "event.json", path, self.api)
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(sum(item["body"].startswith(human_input.QUESTION_MARKER) for item in conversation), 1)
        _, ledger = c.ledger_read(self.api, "pangin/example", 7)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 0)
        self.assertFalse((self.root / "job.json").exists())

    def test_stale_triggering_review_never_notifies_for_protected_finding(self):
        path, env, conversation = self.protected_prepare_fixture()
        event = c.load(self.root / "event.json")
        event["review"]["id"] = 999
        c.save(self.root / "event.json", event)
        with patch.dict(os.environ, env, clear=True):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(conversation, [])
        self.assertFalse((self.root / "job.json").exists())

    def test_noncanonical_event_never_notifies_for_protected_finding(self):
        path, env, conversation = self.protected_prepare_fixture()
        event = c.load(self.root / "event.json")
        event["sender"] = {**BOT, "id": 999}
        c.save(self.root / "event.json", event)
        with patch.dict(os.environ, env, clear=True), self.assertRaises(s.StateError):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(conversation, [])
        self.api.pages.assert_not_called()

    def test_stale_or_out_of_scope_live_pr_never_notifies_for_protected_finding(self):
        for changes in [{"head": {"sha": "d" * 40, "repo": repo()}},
                        {"user": {"login": "other"}}, {"draft": True},
                        {"created_at": "2026-09-29T23:00:00Z"}]:
            with self.subTest(changes=changes):
                self.api.reset_mock()
                path, env, conversation = self.protected_prepare_fixture()
                original_request = self.api.request.side_effect

                def request(route, method="GET", payload=None):
                    value = original_request(route, method, payload)
                    return {**value, **changes} if route.endswith("/pulls/7") else value

                self.api.request.side_effect = request
                with patch.dict(os.environ, env, clear=True):
                    c.prepare(self.root, self.root / "event.json", path, self.api)
                self.assertEqual(conversation, [])

    def test_live_pr_race_is_checked_before_protected_finding_notification(self):
        path, env, conversation = self.protected_prepare_fixture()
        original_request = self.api.request.side_effect
        checks = 0

        def request(route, method="GET", payload=None):
            nonlocal checks
            value = original_request(route, method, payload)
            if route.endswith("/pulls/7"):
                checks += 1
                if checks == 2:
                    value = {**value, "head": {**value["head"], "sha": "d" * 40}}
            return value

        self.api.request.side_effect = request
        with patch.dict(os.environ, env, clear=True), self.assertRaises(s.StateError):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(checks, 2)
        self.assertEqual(conversation, [])

    def test_untyped_preflight_failure_is_not_owner_notification_authority(self):
        path, env, conversation = self.protected_prepare_fixture()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(s, "collect_feedback", side_effect=s.StateError("untrusted private error")), \
             self.assertRaises(s.StateError):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(conversation, [])
        self.assertFalse((self.root / "job.json").exists())

    def test_pending_question_refreshes_changed_head_without_releasing_writer(self):
        fb = feedback(findings=False)
        question = human_input.build_request(
            fb, kind="review_clarification", summary="Original pending decision.",
            sources=[], created_at=NOW)
        ledger = s.request_user_input(s.new_ledger(), fb, question, now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=99)
        current = {**fb, "head_sha": "d" * 40, "source_review_ids": [11]}
        self.api.request.return_value = {"id": 100}
        with patch.object(c, "now", return_value=NOW):
            self.assertIsNone(c.human_review_gate(self.root, self.api, current, ledger, 99, [], BOT))
        calls = self.api.request.call_args_list
        questions = [call.args[2]["body"] for call in calls
                     if call.args[1] == "POST" and "body" in call.args[2]]
        self.assertEqual(len(questions), 1)
        self.assertIn("@pangin", questions[0])
        bodies = [call.args[2]["body"] for call in calls if "body" in call.args[2]]
        self.assertIn('"head_sha": "' + current["head_sha"] + '"', bodies[-1])
        self.assertIn('"state": "needs_user_input"', bodies[-1])
        self.assertFalse((self.root / "job.json").exists())

    def test_pending_current_question_never_resumes_or_reposts(self):
        fb = feedback(findings=False)
        question = human_input.build_request(fb, kind="review_clarification", summary="Pending.",
                                             sources=[], created_at=NOW)
        ledger = s.request_user_input(s.new_ledger(), fb, question, now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=99)
        self.assertIsNone(c.human_review_gate(self.root, self.api, fb, ledger, 99, [], BOT))
        self.api.request.assert_not_called()

    def answered_ledger(self, option="clarify"):
        fb = feedback(findings=False)
        question = human_input.build_request(fb, kind="review_clarification", summary="Decision.",
                                             sources=[], created_at=NOW)
        ledger = s.request_user_input(s.new_ledger(), fb, question, now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=99)
        response = {"schema_version": 1, "request_id": question["request_id"], "option_id": option,
                    "message": "Keep the documented behavior.", "actor_login": "pangin",
                    "actor_id": 1, "body_sha256": "f" * 64}
        return fb, s.record_user_response(ledger, fb, response, now=NOW)

    def test_owner_stop_blocks_even_a_new_commit_and_review(self):
        fb, ledger = self.answered_ledger("stop")
        current = {**fb, "head_sha": "d" * 40, "source_review_ids": [11]}
        self.assertIsNone(c.human_review_gate(self.root, self.api, current, ledger, 99,
                                             [review(head=current["head_sha"], rid=11)], BOT))
        self.api.request.assert_not_called()

    def test_post_answer_gate_rejects_replays_and_pre_answer_reviews(self):
        fb, ledger = self.answered_ledger()
        fb = {**fb, "source_review_ids": [10, 11]}
        for candidate in [review(rid=10), review(rid=11),
                          {**review(rid=11), "submitted_at": "2026-09-29T23:00:00Z"}]:
            with self.subTest(review_id=candidate["id"]):
                self.assertIsNone(c.human_review_gate(self.root, self.api, fb, ledger, 99,
                                                     [candidate], BOT, trigger_id=candidate["id"]))
        self.api.request.assert_not_called()

    def test_post_answer_gate_filters_old_verdicts_and_checks_event_id(self):
        fb, ledger = self.answered_ledger()
        fb = {**fb, "source_review_ids": [10, 11]}
        fresh = {**review(rid=11), "submitted_at": "2026-09-30T03:00:01Z"}
        # NOW in these fixtures is 2026-09-30T03:00:00Z (12:00 KST).
        retained = c.human_review_gate(self.root, self.api, fb, ledger, 99,
                                      [review(rid=10), fresh], BOT, trigger_id=11)
        self.assertEqual(retained, [fresh])
        self.assertIsNone(c.human_review_gate(self.root, self.api, fb, ledger, 99,
                                             [fresh], BOT, trigger_id=10))

    def test_answered_same_question_does_not_discard_answer_or_repost(self):
        fb, ledger = self.answered_ledger()
        with patch.object(c, "now", return_value=NOW):
            cid, unchanged = c.request_human_input(self.root, self.api, fb, ledger, 99,
                                                   "Manual resolution is still required.")
        self.assertEqual(cid, 99)
        self.assertEqual(unchanged, ledger)
        self.api.request.assert_not_called()

    def test_post_answer_clean_review_does_not_reuse_old_protected_comment(self):
        path, env, conversation = self.protected_prepare_fixture()
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        cid, ledger = c.ledger_read(self.api, "pangin/example", 7)
        question = ledger["pulls"]["pangin/example#7"]["human_request"]
        fb = feedback(head=self.head, findings=False)
        response = {"schema_version": 1, "request_id": question["request_id"], "option_id": "clarify",
                    "message": "Inspect the unchanged candidate independently.", "actor_login": "pangin",
                    "actor_id": 1, "body_sha256": "f" * 64}
        answered = s.record_user_response(ledger, fb, response, now=NOW)
        c.ledger_write(self.api, "pangin/example", 7, cid, answered, "Owner direction recorded.")
        old_pages = self.api.pages.side_effect
        fresh = review(head=self.head, rid=11, submitted="2026-09-30T03:00:01Z")

        def pages(route):
            if route.endswith("/reviews"):
                return [review(head=self.head, rid=10), fresh]
            return old_pages(route)

        self.api.pages.side_effect = pages
        event = c.load(self.root / "event.json"); event["review"]["id"] = 11
        c.save(self.root / "event.json", event)
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW), \
             patch.object(consensus, "prepare") as final:
            c.prepare(self.root, self.root / "event.json", path, self.api)
        final.assert_called_once()
        fresh_feedback = final.call_args.args[4]
        self.assertEqual(fresh_feedback["source_review_ids"], [11])
        self.assertEqual(fresh_feedback["findings"], [])
        self.assertEqual(final.call_args.args[6], [fresh])
        self.assertEqual(sum(item["body"].startswith(human_input.QUESTION_MARKER) for item in conversation), 1)
        self.assertFalse((self.root / "job.json").exists())

    def test_disabled_never_fetches_or_reserves(self):
        path = self.root / "config.json"; c.save(path, {**config(), "enabled": False, "activated_at": None})
        c.prepare(self.root, self.root / "missing-event.json", path, self.api)
        self.api.request.assert_not_called(); self.api.pages.assert_not_called()

    def test_missing_cloud_credentials_stop_before_reservation(self):
        path, env, conversation = self.protected_prepare_fixture("app.js")
        env["HAS_CLAUDE_TOKEN"] = "false"
        with patch.dict(os.environ, env, clear=True):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        questions = [item for item in conversation if item["body"].startswith(human_input.QUESTION_MARKER)]
        self.assertEqual(len(questions), 1)
        self.assertIn("@pangin", questions[0]["body"])
        self.assertIn("without posting credential values", questions[0]["body"])
        self.assertFalse((self.root / "job.json").exists())
        _, ledger = c.ledger_read(self.api, "pangin/example", 7)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 0)

    def test_pending_missing_or_malformed_profiles_ask_once_without_execution(self):
        for changes in [
            {"readiness": "pending", "test_image": None, "test_commands": [],
             "review_documents": [], "review_criteria": [], "required_ci": []},
            {"readiness": "ready", "test_image": "untrusted-secret-value"},
            {"test_commands": [["bash", "-c", "untrusted-secret-value"]]},
            {"required_ci": []}, {"review_documents": []}, {"review_criteria": []},
            {"review_criteria": [{"id": "malformed", "private": "untrusted-secret-value"}]},
        ]:
            with self.subTest(changes=changes):
                self.api.reset_mock()
                path, env, conversation = self.protected_prepare_fixture("app.js")
                c.save(path, {**c.load(path), **changes, "max_rounds": None,
                              "readiness_reason": "untrusted-secret-value"})
                with patch.dict(os.environ, env, clear=True), patch.object(c, "output") as outputs, \
                     patch.object(c, "test_container") as tests, patch.object(c, "publish") as publisher:
                    c.prepare(self.root, self.root / "event.json", path, self.api)
                    c.prepare(self.root, self.root / "event.json", path, self.api)
                questions = [item for item in conversation if item["body"].startswith(human_input.QUESTION_MARKER)]
                self.assertEqual(len(questions), 1)
                self.assertIn("@pangin", questions[0]["body"])
                self.assertNotIn("untrusted-secret-value", str(conversation))
                self.assertNotIn("untrusted private prose", str(conversation))
                self.assertFalse((self.root / "job.json").exists())
                self.assertNotIn(unittest.mock.call("run_claude", "true"), outputs.call_args_list)
                _, ledger = c.ledger_read(self.api, "pangin/example", 7)
                record = ledger["pulls"]["pangin/example#7"]
                self.assertEqual(record["state"], "needs_user_input")
                self.assertEqual(record["attempts"], 0)
                tests.assert_not_called(); publisher.assert_not_called()

    def test_pending_without_execution_fields_is_enrolled_not_a_fake_test_profile(self):
        path, env, conversation = self.protected_prepare_fixture("app.js")
        original = c.load(path)
        pending = {key: value for key, value in original.items() if key not in
                   {"test_image", "test_commands", "review_documents", "review_criteria", "required_ci"}}
        c.save(path, {**pending, "readiness": "pending", "max_rounds": None})
        with patch.dict(os.environ, env, clear=True):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(sum(item["body"].startswith(human_input.QUESTION_MARKER) for item in conversation), 1)
        self.assertFalse((self.root / "job.json").exists())

    def test_pending_and_missing_credentials_never_notify_ineligible_or_stale_prs(self):
        for changes in [{"draft": True}, {"user": {"login": "other"}},
                        {"created_at": "2026-09-29T23:00:00Z"},
                        {"head": {"sha": "d" * 40, "repo": repo()}}]:
            with self.subTest(changes=changes):
                self.api.reset_mock()
                path, env, conversation = self.protected_prepare_fixture("app.js")
                c.save(path, {**c.load(path), "readiness": "pending", "test_commands": []})
                env["HAS_CLAUDE_TOKEN"] = "false"
                old_request = self.api.request.side_effect
                def request(route, method="GET", payload=None):
                    value = old_request(route, method, payload)
                    return {**value, **changes} if route.endswith("/pulls/7") else value
                self.api.request.side_effect = request
                with patch.dict(os.environ, env, clear=True):
                    c.prepare(self.root, self.root / "event.json", path, self.api)
                self.assertEqual(conversation, [])

    def test_noncanonical_and_stale_review_never_authorize_readiness_questions(self):
        for changes in [{"sender": {**BOT, "id": 999}}, {"review": {"id": 999}}]:
            with self.subTest(changes=changes):
                self.api.reset_mock()
                path, env, conversation = self.protected_prepare_fixture("app.js")
                c.save(path, {**c.load(path), "readiness": "pending"})
                c.save(self.root / "event.json", {**c.load(self.root / "event.json"), **changes})
                with patch.dict(os.environ, env, clear=True):
                    if "sender" in changes:
                        with self.assertRaises(s.StateError):
                            c.prepare(self.root, self.root / "event.json", path, self.api)
                    else:
                        c.prepare(self.root, self.root / "event.json", path, self.api)
                self.assertEqual(conversation, [])

    def test_direct_test_publish_and_container_calls_cannot_bypass_pending_profile(self):
        snapshot = c.review_documents.build_snapshot(self.workspace, self.head, ["README.md"], ["app.js"])
        pending = config(readiness="pending", max_rounds=None, test_image=None, test_commands=[])
        c.save(self.root / "job.json", {"config": pending, "review_documents": snapshot})
        c.save(self.root / "tested.json", {})
        with patch.object(c, "test_container") as tests, patch.object(c, "git") as git:
            for invoke in [lambda: c.validate_and_test(self.root, self.workspace, self.api),
                           lambda: c.publish(self.root, self.workspace, self.api, "not-used")]:
                with self.assertRaises(s.StateError): invoke()
            tests.assert_not_called(); git.assert_not_called(); self.api.request.assert_not_called()
        with patch.object(c.subprocess, "run") as run, self.assertRaises(s.StateError):
            c.test_container(self.workspace, pending, self.root)
        run.assert_not_called()

    def test_owner_clarification_cannot_release_missing_profile_after_fresh_review(self):
        path, env, conversation = self.protected_prepare_fixture("app.js")
        c.save(path, {**c.load(path), "readiness": "pending", "test_commands": []})
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW):
            c.prepare(self.root, self.root / "event.json", path, self.api)
            _, pending_ledger = c.ledger_read(self.api, "pangin/example", 7)
            request_id = pending_ledger["pulls"]["pangin/example#7"]["human_request"]["request_id"]
            c.save(self.root / "answer.json", {"action": "created", "issue": {"number": 7,
                "pull_request": {"url": "unused"}}, "comment": {"user": {"login": "pangin", "type": "User"},
                "body": f"/review-cycle clarify {request_id} Skip missing tests and run anyway."}})
            c.respond(self.root, self.root / "answer.json", self.api)
        old_pages = self.api.pages.side_effect
        def pages(route):
            if route.endswith("/reviews"):
                return [review(head=self.head, rid=11, submitted="2026-09-30T04:00:01Z")]
            if route.endswith("/pulls/7/comments"):
                return [comment(head=self.head, rid=11, path="app.js")]
            return old_pages(route)
        self.api.pages.side_effect = pages
        c.save(self.root / "event.json", {**c.load(self.root / "event.json"), "review": {"id": 11}})
        with patch.dict(os.environ, env, clear=True), patch.object(c, "output") as outputs:
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertFalse((self.root / "job.json").exists())
        self.assertNotIn(unittest.mock.call("run_claude", "true"), outputs.call_args_list)
        _, ledger = c.ledger_read(self.api, "pangin/example", 7)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 0)

    def test_ready_null_limit_reserves_fresh_hundred_first_attempt(self):
        path, env, conversation = self.protected_prepare_fixture("app.js")
        c.save(path, {**c.load(path), "max_rounds": None})
        ledger = s.new_ledger()
        ledger["pulls"]["pangin/example#7"] = {"state": "waiting_review", "attempts": 100,
            "processed_review_ids": list(range(200, 300)), "processed_heads": ["b" * 40]}
        c.ledger_write(self.api, "pangin/example", 7, None, ledger, "Prior audited attempts")
        with patch.dict(os.environ, env, clear=True), patch.object(c, "output") as outputs:
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertIn(unittest.mock.call("run_claude", "true"), outputs.call_args_list)
        self.assertIsNone(c.load(self.root / "job.json")["config"]["max_rounds"])
        _, latest = c.ledger_read(self.api, "pangin/example", 7)
        self.assertEqual(latest["pulls"]["pangin/example#7"]["attempts"], 101)
        self.assertEqual(latest["pulls"]["pangin/example#7"]["state"], "processing")

    def test_pending_ci_completion_can_only_ask_readiness_after_current_native_review(self):
        path, env, conversation = self.protected_prepare_fixture("app.js")
        c.save(path, {**c.load(path), "readiness": "pending", "required_ci": []})
        env["GITHUB_EVENT_NAME"] = "workflow_run"
        c.save(self.root / "event.json", {"action": "completed", "workflow_run": {"id": 42}})
        original_request = self.api.request.side_effect
        def request(route, method="GET", payload=None):
            if route == "repos/pangin/example/actions/runs/42":
                return {"id": 42, "event": "pull_request", "status": "completed", "conclusion": "success",
                        "repository": repo(), "head_repository": repo(), "head_sha": self.head,
                        "path": ".github/workflows/review-cycle-checks.yml", "pull_requests": [
                            {"number": 7, "head": {"sha": self.head}, "base": {"sha": "b" * 40}}]}
            return original_request(route, method, payload)
        self.api.request.side_effect = request
        with patch.dict(os.environ, env, clear=True), patch.object(c, "output") as outputs:
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertEqual(sum(item["body"].startswith(human_input.QUESTION_MARKER) for item in conversation), 1)
        self.assertNotIn(unittest.mock.call("run_claude", "true"), outputs.call_args_list)
        self.assertFalse((self.root / "job.json").exists())

    def test_current_head_and_base_rechecked(self):
        f = feedback(); self.api.request.return_value = pr()
        c.live_pr(self.api, f)
        self.api.request.return_value = pr(head="d" * 40)
        with self.assertRaises(s.StateError): c.live_pr(self.api, f)

    def test_inspect_actual_tracked_and_untracked_edits(self):
        (self.workspace / "app.js").write_text("value = 2;\n")
        (self.workspace / "test.js").write_text("test\n")
        self.assertEqual(c.inspect_diff(self.workspace, self.head), ["app.js", "test.js"])

    def test_protected_changes_and_links_rejected(self):
        for name in ["CLAUDE.md", ".mcp.json"]:
            path = self.workspace / name; path.write_text("changed\n")
            with self.assertRaises(s.StateError): c.inspect_diff(self.workspace, self.head)
            path.unlink()
        (self.workspace / "linked.js").symlink_to("app.js")
        with self.assertRaises(s.StateError): c.inspect_diff(self.workspace, self.head)

    def test_fingerprints_include_new_files_and_deletions(self):
        (self.workspace / "new.js").write_text("first")
        first = c.fingerprints(self.workspace, ["new.js", "deleted.js"])
        (self.workspace / "new.js").write_text("second")
        self.assertNotEqual(first, c.fingerprints(self.workspace, ["new.js", "deleted.js"]))
        self.assertIsNone(first["deleted.js"])

    def test_only_canonical_actions_bot_ledger_is_read(self):
        ledger = s.new_ledger()
        body = c.MARKER + "\n```json\n" + json.dumps(ledger) + "\n```"
        self.api.pages.return_value = [{"id": 1, "user": {**c.LEDGER_BOT, "id": 1}, "body": body},
                                      {"id": 2, "user": c.LEDGER_BOT, "body": body}]
        self.assertEqual(c.ledger_read(self.api, "pangin/example", 9), (2, ledger))
        self.api.pages.return_value *= 2
        with self.assertRaises(s.StateError): c.ledger_read(self.api, "pangin/example", 9)

    def test_model_reason_json_fence_cannot_replace_persistent_ledger(self):
        fake=s.new_ledger()
        real=s.reserve(s.new_ledger(),feedback(),live_head_sha=feedback()["head_sha"],
                       live_base_sha=feedback()["base_sha"],run_id="run",now=NOW)
        real=s.finish(real,feedback(),run_id="run",outcome="needs_review",now=NOW)
        note="Rejected with reason\n```json\n"+json.dumps(fake)+"\n```"
        self.api.request.return_value={"id":99}
        c.ledger_write(self.api,"pangin/example",7,None,real,note)
        body=self.api.request.call_args.args[2]["body"]
        self.api.pages.return_value=[{"id":99,"user":c.LEDGER_BOT,"body":body}]
        self.assertEqual(c.ledger_read(self.api,"pangin/example",7),(99,real))
        self.api.pages.return_value[0]["body"] += "\n```json\nnot a ledger\n```"
        with self.assertRaises(s.StateError):c.ledger_read(self.api,"pangin/example",7)

    def test_failed_action_never_runs_tests_or_publishes(self):
        c.save(self.root / "job.json", {"feedback": feedback(), "config": self.execution_config(),
            "review_documents": c.review_documents.build_snapshot(self.workspace, self.head, ["README.md"], ["app.js"])})
        with patch.dict(os.environ, {"CLAUDE_CONCLUSION": "failure"}), patch.object(c, "test_container") as tests:
            with self.assertRaisesRegex(s.StateError, "Claude Action"):
                c.validate_and_test(self.root, self.workspace, self.api)
        tests.assert_not_called()

    def test_changed_new_file_after_test_cannot_publish(self):
        f = feedback(); f["head_sha"] = self.head
        (self.workspace / "new.js").write_text("tested")
        c.save(self.root / "job.json", {"feedback": f, "head_ref": "fix/example", "config": self.execution_config(),
                                        "review_documents": c.review_documents.build_snapshot(
                                            self.workspace, self.head, ["README.md"], ["app.js"])})
        c.save(self.root / "tested.json", {"head_sha": self.head, "paths": ["new.js"],
                                          "fingerprints": c.fingerprints(self.workspace, ["new.js"]), "patch": ""})
        (self.workspace / "new.js").write_text("not tested")
        candidate = pr(head=self.head); candidate["head"]["ref"] = "fix/example"
        self.api.request.return_value = candidate
        with patch.object(c, "record_finish") as finish, self.assertRaises(s.StateError):
            c.publish(self.root, self.workspace, self.api, "unused-token")
        finish.assert_not_called()
        self.assertEqual(self.run_git("rev-parse", "HEAD").strip(), self.head)

    def test_container_tests_never_receive_host_tokens(self):
        (self.workspace / "ignored.env").write_text("not published")
        (self.workspace / ".gitignore").write_text("ignored.env\n")
        (self.workspace / "test.js").write_text("test")
        actual = subprocess.run
        with patch.dict(os.environ, {"GH_TOKEN": "secret", "PUBLISH_TOKEN": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret"}), \
             patch.object(c.subprocess, "run", wraps=subprocess.run) as run:
            # Keep real Git listing; mock only Docker calls.
            def execute(args, **kwargs):
                if args[0] == "docker": return Mock(returncode=0)
                return actual(args, **kwargs)
            run.side_effect = execute
            c.test_container(self.workspace, config(), self.root)
            calls = [call for call in run.call_args_list if call.args[0][0] == "docker"]
        self.assertGreaterEqual(len(calls), 4)
        for call in calls:
            self.assertNotIn("GH_TOKEN", call.kwargs["env"])
            self.assertNotIn("PUBLISH_TOKEN", call.kwargs["env"])
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", call.kwargs["env"])
        self.assertFalse((self.root / "test-source/ignored.env").exists())
        self.assertTrue((self.root / "test-source/test.js").exists())

    def test_prepare_explicitly_supplies_pinned_criteria_before_claude(self):
        path, env = self.prepare_fixture()
        (self.workspace / "README.md").write_text("candidate weakens criteria\n")
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW), \
             patch.object(c, "output") as output:
            c.prepare(self.root, self.root / "event.json", path, self.api)
        job = c.load(self.root / "job.json")
        snapshot = job["review_documents"]
        self.assertEqual(snapshot["revision"], self.head)
        text = dict((call.args[0], call.args[1]) for call in output.call_args_list)["claude_prompt"]
        self.assertIn("approved baseline", text)
        self.assertNotIn("candidate weakens criteria", text)
        self.assertIn(snapshot["sha256"], text)
        self.assertIn("A rejection is a dispute", text)
        written = self.api.request.call_args_list[-1].args[2]["body"]
        self.assertIn('"document_fingerprint": "' + snapshot["sha256"] + '"', written)

    def test_missing_criteria_stops_before_model_or_reservation(self):
        path, env = self.prepare_fixture(documents=["missing.md"])
        with patch.dict(os.environ, env, clear=True):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        self.assertFalse((self.root / "job.json").exists())
        bodies = [call.args[2]["body"] for call in self.api.request.call_args_list
                  if len(call.args) > 2 and isinstance(call.args[2], dict) and "body" in call.args[2]]
        self.assertTrue(any("review-cycle-human-question:v1" in body for body in bodies))

    def test_owner_comment_records_direction_and_keeps_fresh_review_required(self):
        fb = feedback(findings=False)
        request = human_input.build_request(
            fb, kind="review_clarification", summary="The reviewers need a product decision.",
            sources=["docs/review-contract.md"], created_at=NOW)
        ledger = s.request_user_input(s.new_ledger(), fb, request, now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=101)
        body = c.MARKER + "\n```json\n" + json.dumps(ledger) + "\n```"
        event = {"action": "created", "issue": {"number": 7, "pull_request": {"url": "x"}},
                 "comment": {"body": f"/review-cycle clarify {request['request_id']} Keep the documented behavior.",
                             "user": {"login": "pangin", "type": "User", "id": 1}}}
        event_path = self.root / "comment-event.json"; c.save(event_path, event)
        candidate = pr(head=fb["head_sha"], base=fb["base_sha"])
        posted = []
        def request(route, method="GET", payload=None):
            if route == "repos/pangin/example/issues/7": return {"pull_request": {"url": "x"}}
            if route == "repos/pangin/example/pulls/7": return candidate
            if method == "PATCH" and route.endswith("/issues/comments/101"):
                posted.append(payload["body"]); return {"id": 101}
            if method == "POST" and route.endswith("/issues/7/comments"):
                posted.append(payload["body"]); return {"id": 102}
            raise AssertionError((route, method))
        api = Mock(); api.request.side_effect = request; api.pages.return_value = [
            {"id": 101, "user": c.LEDGER_BOT, "body": body}]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "pangin/example"}, clear=True):
            c.respond(self.root, event_path, api)
        self.assertTrue(any('"state": "ready"' in value for value in posted))
        self.assertTrue(any(c.HUMAN_ACK_MARKER in value for value in posted))

    def test_clean_review_waits_for_verification_not_completion(self):
        path, env = self.prepare_fixture(findings=False)
        with patch.dict(os.environ, env, clear=True), patch.object(c, "now", return_value=NOW):
            c.prepare(self.root, self.root / "event.json", path, self.api)
        body = self.api.request.call_args.args[2]["body"]
        self.assertIn('"state": "awaiting_verification"', body)
        self.assertIn("Missing output is not agreement", body)
        self.assertFalse((self.root / "job.json").exists())

    def test_baseline_survives_default_branch_changes(self):
        ledger = s.new_ledger()
        original = c.pin_documents(self.workspace, config(review_documents=["README.md"]),
                                   ledger, feedback(), ["app.js"])
        (self.workspace / "README.md").write_text("new default branch criteria\n")
        self.run_git("add", "README.md")
        self.run_git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "new docs")
        later = c.pin_documents(self.workspace, config(review_documents=["missing.md"]),
                                ledger, feedback(), ["app.js"])
        self.assertEqual(original, later)

    def test_new_scoped_criteria_require_explicit_new_baseline(self):
        ledger = s.new_ledger()
        c.pin_documents(self.workspace, config(review_documents=["README.md"]), ledger, feedback(), ["app.js"])
        with self.assertRaises(s.StateError):
            c.pin_documents(self.workspace, config(review_documents=["README.md"]),
                            ledger, feedback(), ["src/new.js"])

    def test_snapshot_documents_cannot_be_changed_before_tests_or_push(self):
        f = feedback(); f["head_sha"] = self.head
        job = {"feedback": f, "config": self.execution_config(),
               "review_documents": c.review_documents.build_snapshot(
                   self.workspace, self.head, ["README.md"], ["app.js"])}
        c.save(self.root / "job.json", job)
        (self.workspace / "README.md").write_text("weaken criteria\n")
        self.api.request.return_value = pr(head=self.head)
        with patch.dict(os.environ, {"CLAUDE_CONCLUSION": "success"}), \
             patch.object(c, "test_container") as tests, self.assertRaises(s.StateError):
            c.validate_and_test(self.root, self.workspace, self.api)
        tests.assert_not_called()
        with self.assertRaises(s.StateError): c.protect_documents(["README.md"], job, self.workspace)
        c.protect_documents(["app.js"], job, self.workspace)

    def test_writer_cannot_expand_scope_without_new_criteria(self):
        snapshot = c.review_documents.build_snapshot(self.workspace, self.head, ["README.md"], ["app.js"])
        with self.assertRaises(s.StateError):
            c.protect_documents(["src/new.js"], {"review_documents": snapshot}, self.workspace)

    def test_criteria_prompt_is_never_silently_truncated(self):
        with self.assertRaises(s.StateError):
            c.claude_prompt(feedback(), {"documents": [{"content": "x" * (400 * 1024)}]})


class ModelWorkflowContractTests(unittest.TestCase):
    def test_standalone_model_action_has_no_github_credentials(self):
        # Read the canonical template locally or the installed workflow in CI.
        directory = Path(__file__).resolve().parent
        workflow = directory / "cloud-workflow.yml"
        if not workflow.is_file():
            workflow = directory.parent / "workflows/codex-claude-cycle.yml"
        text = workflow.read_text()
        # actionlint validates YAML; this bounded scan checks our step contract
        # without adding a YAML dependency to the credential-free CI suite.
        steps = text.split("\n      - name:")
        models = [step for step in steps if "\n        id: claude\n" in step]
        self.assertEqual(len(models), 1)
        model = models[0]
        self.assertIn(
            "uses: anthropics/claude-code-action/base-action@fd1c128679612beff4ca259c78021c506e8aa7a7",
            model,
        )
        self.assertNotIn("\n        env:", model)
        self.assertNotIn("github.token", model)
        self.assertNotIn("GITHUB_TOKEN", model)
        self.assertNotIn("GH_TOKEN", model)
        self.assertNotIn("PUBLISH_TOKEN", model)
        self.assertNotIn("REVIEW_CYCLE_APP", model)
        inputs = {
            line.strip().split(":", 1)[0]
            for line in model.splitlines()
            if line.startswith("          ") and not line.lstrip().startswith("#")
        }
        self.assertEqual(inputs, {
            "claude_code_oauth_token", "prompt", "claude_args", "show_full_output",
        })
        self.assertIn("prompt: ${{ steps.prepare.outputs.claude_prompt }}", model)
        self.assertIn("claude_args: ${{ steps.prepare.outputs.claude_args }}", model)
        self.assertIn("show_full_output: 'false'", model)
        tests = [step for step in steps if "\n        id: tests\n" in step]
        self.assertEqual(len(tests), 1)
        self.assertIn("steps.claude.outputs.conclusion", tests[0])
        self.assertIn("steps.claude.outputs.structured_output", tests[0])


if __name__ == "__main__":
    unittest.main()
