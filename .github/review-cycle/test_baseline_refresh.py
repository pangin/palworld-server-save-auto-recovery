"""Explicit trusted-default baseline refresh; Git fixtures only, no app/network."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import cloud_controller as c
import cloud_consensus as consensus
import cloud_state as s
import human_input as h
from test_cloud_state import BOT, NOW, config, feedback, pr, repo, review


class BaselineRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.git("init", "-q")
        (self.workspace / "README.md").write_text("## Scope\nOriginal approved behavior.\n")
        (self.workspace / "AGENTS.md").write_text("Preserve safety boundaries.\n")
        (self.workspace / "app.js").write_text("value = 1;\n")
        self.old_revision = self.commit("original documents")
        self.old_feedback = feedback(base=self.old_revision, findings=False)
        self.config = config(review_documents=["README.md"], review_criteria=[{
            "id": "C1", "source": "README.md", "section": "Scope", "requirement": "Preserve approved behavior"}],
            required_ci=[{"name": "application-container-smoke", "workflow": ".github/workflows/review-cycle-checks.yml",
                          "app_id": 15368, "app_slug": "github-actions"}])
        self.ledger = s.new_ledger()
        self.old_snapshot = c.pin_documents(self.workspace, self.config, self.ledger, self.old_feedback, ["app.js"])
        record = self.ledger["pulls"]["pangin/example#7"]
        consensus.pin_manifest(self.config, self.old_snapshot, record, pr())
        record.update(attempts=4, processed_review_ids=[10], processed_heads=[self.old_feedback["head_sha"]],
                      verification_attempts={self.old_feedback["head_sha"]: 1},
                      review_requests={"old": {"status": "posted"}},
                      native_final_verdict={"old": True}, consensus_proof={"old": True},
                      verified_codex_review={"old": True}, last_run_id="failed-run-audit")
        self.question = h.build_request(self.old_feedback, kind="criteria_conflict", summary="Approve a document update.",
                                        sources=["README.md"], created_at=NOW)
        self.ledger = s.request_user_input(self.ledger, self.old_feedback, self.question, now=NOW)
        self.ledger["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=101)
        (self.workspace / "README.md").write_text("## Scope\nNew explicitly approved behavior.\n")
        self.new_revision = self.commit("approved document update")
        self.current = feedback(base=self.new_revision, rid=11, findings=False)
        self.response = h.parse_response(
            f"/review-cycle update-docs {self.question['request_id']} {self.new_revision} README.md changed behavior",
            self.question, {"login": "pangin", "type": "User", "id": 1})

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.workspace,
                              check=True, text=True, capture_output=True).stdout.strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def approve(self, **changes):
        return s.record_user_response(self.ledger, changes.get("feedback", self.current),
                                      changes.get("response", self.response), now=NOW,
                                      trusted_default_revision=changes.get("trusted", self.new_revision))

    def test_explicit_update_pins_new_snapshot_and_rebuilds_manifest_not_old_evidence(self):
        ledger = self.approve()
        old_verdict = consensus.verdict_example(self.old_feedback, self.old_snapshot,
                                                ledger["pulls"]["pangin/example#7"]["review_manifest"])
        with patch.object(c, "now", return_value=NOW):
            snapshot = c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"])
        record = ledger["pulls"]["pangin/example#7"]
        self.assertEqual(snapshot["revision"], self.new_revision)
        self.assertNotEqual(snapshot["sha256"], self.old_snapshot["sha256"])
        self.assertIn("New explicitly approved", str(snapshot))
        for field in ["review_manifest", "review_requests", "native_final_verdict", "consensus_proof", "verified_codex_review"]:
            self.assertNotIn(field, record)
        self.assertEqual(record["attempts"], 4)
        self.assertEqual(record["processed_review_ids"], [10])
        self.assertEqual(record["processed_heads"], [self.old_feedback["head_sha"]])
        self.assertEqual(record["verification_attempts"], {self.old_feedback["head_sha"]: 1})
        self.assertEqual(record["last_run_id"], "failed-run-audit")
        self.assertEqual(record["baseline_refresh_approval"]["status"], "consumed")
        self.assertEqual(record["baseline_history"][0]["baseline"]["document_revision"], self.old_revision)
        self.assertEqual(record["baseline_history"][0]["baseline"]["consensus_proof"], {"old": True})
        consensus.pin_manifest(self.config, snapshot, record, pr(base=self.new_revision))
        with self.assertRaises(consensus.evidence.EvidenceError):
            consensus.evidence.validate_verdict(old_verdict, "codex", self.current, snapshot, record["review_manifest"])
        s._ledger(ledger)

    def test_refresh_does_not_reset_failure_or_duplicate_execution_gates(self):
        ledger = self.approve()
        with patch.object(c, "now", return_value=NOW):
            c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"])
        # Approval of new documents is not authority to retry an already handled
        # candidate or replenish its independently consumed verification budget.
        finding = {**self.current, "findings": [{"id": "comment:22", "body": "[P1] Reconsider result", "path": "app.js"}]}
        with self.assertRaises(s.StateError):
            s.reserve(ledger, finding, live_head_sha=finding["head_sha"], live_base_sha=finding["base_sha"],
                      run_id="new-attempt", now=NOW, max_rounds=None)
        record = ledger["pulls"]["pangin/example#7"]
        record["state"] = "awaiting_verification"
        record["verified_codex_review"] = {"head_sha": self.current["head_sha"], "base_sha": self.current["base_sha"], "review_ids": [11]}
        record["review_criteria"] = ["C1"]
        with self.assertRaises(s.StateError):
            s.reserve_verification(ledger, self.current, run_id="new-verification", now=NOW)
        for blocked in ["processing", "verifying", "failed", "needs_review"]:
            pending = self.approve()["pulls"]["pangin/example#7"]
            pending["state"] = blocked
            with self.subTest(state=blocked), self.assertRaises(s.StateError):
                s.consume_baseline_refresh(pending, self.current, trusted_revision=self.new_revision, now=NOW)
        self.assertEqual(record["attempts"], 4)

    def test_new_question_supersedes_unused_approval_without_erasing_human_audit(self):
        ledger = self.approve()
        changed = {**self.current, "head_sha": "f" * 40}
        question = h.build_request(changed, kind="criteria_conflict", summary="Changed candidate needs approval.", sources=[], created_at=NOW)
        ledger = s.request_user_input(ledger, changed, question, now=NOW)
        record = ledger["pulls"]["pangin/example#7"]
        record["human_request"].update(status="posted", comment_id=102)
        self.assertEqual(record["baseline_refresh_approval"]["status"], "superseded")
        self.assertEqual(record["human_request_history"][0]["request"]["request_id"], self.question["request_id"])
        self.assertEqual(record["attempts"], 4)
        s._ledger(ledger)

    def test_generic_default_change_preserves_old_baseline(self):
        ledger = deepcopy(self.ledger)
        record = ledger["pulls"]["pangin/example#7"]
        record["state"] = "ready"
        snapshot = c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"])
        self.assertEqual(snapshot, self.old_snapshot)
        self.assertNotIn("baseline_history", record)

    def test_update_approval_requires_exact_live_default_not_candidate_or_stale_head(self):
        cases = [{"trusted": self.old_revision}, {"trusted": None},
                 {"feedback": {**self.current, "head_sha": "f" * 40}},
                 {"feedback": {**self.current, "base_sha": self.old_revision}},
                 {"response": {**self.response, "approved_revision": self.current["head_sha"]},
                  "feedback": {**self.current, "base_sha": self.current["head_sha"]},
                  "trusted": self.current["head_sha"]}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(s.StateError):
                self.approve(**changes)

    def test_clarification_cannot_approve_a_changed_base(self):
        response = {key: value for key, value in self.response.items() if key != "approved_revision"}
        response["option_id"] = "clarify"
        with self.assertRaises(s.StateError):
            self.approve(response=response)

    def test_consume_denies_stale_head_base_revision_or_old_review(self):
        ledger = self.approve()
        original = deepcopy(ledger)
        cases = [({**self.current, "head_sha": "f" * 40}, self.new_revision),
                 ({**self.current, "base_sha": self.old_revision}, self.new_revision),
                 ({**self.current, "source_review_ids": [10]}, self.new_revision),
                 (self.current, self.old_revision)]
        for fb, revision in cases:
            with self.subTest(feedback=fb, revision=revision), self.assertRaises(s.StateError):
                s.consume_baseline_refresh(ledger["pulls"]["pangin/example#7"], fb,
                                           trusted_revision=revision, now=NOW)
        self.assertEqual(ledger, original)

    def test_fresh_review_gate_required_after_answer(self):
        ledger = self.approve()
        api = Mock()
        stale = review(rid=11, submitted="2026-09-30T02:59:59Z")
        fresh = review(rid=11, submitted="2026-09-30T03:00:01Z")
        self.assertIsNone(c.human_review_gate(self.root, api, self.current, ledger, 101, [stale], BOT, trigger_id=11))
        self.assertEqual(c.human_review_gate(self.root, api, self.current, ledger, 101, [fresh], BOT, trigger_id=11), [fresh])
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["baseline_refresh_approval"]["status"], "pending")

    def test_approval_consumed_once_without_automatic_repin_on_later_default_change(self):
        ledger = self.approve()
        with patch.object(c, "now", return_value=NOW):
            snapshot = c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"])
        (self.workspace / "README.md").write_text("## Scope\nUnapproved later behavior.\n")
        self.commit("later unrelated docs")
        self.assertEqual(c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"]), snapshot)
        self.assertEqual(len(ledger["pulls"]["pangin/example#7"]["baseline_history"]), 1)
        with self.assertRaises(s.StateError):
            s.consume_baseline_refresh(ledger["pulls"]["pangin/example#7"], self.current,
                                       trusted_revision=self.new_revision, now=NOW)

    def test_history_limit_stops_without_truncation_or_mutation(self):
        ledger = self.approve()
        record = ledger["pulls"]["pangin/example#7"]
        consumed = {**record["baseline_refresh_approval"], "status": "consumed", "consumed_at": NOW}
        record["baseline_history"] = [{"baseline": {}, "approval": deepcopy(consumed), "refreshed_at": NOW}
                                      for _ in range(s.MAX_BASELINE_HISTORY)]
        before = deepcopy(ledger)
        with patch.object(c, "now", return_value=NOW), self.assertRaises(s.StateError):
            c.pin_documents(self.workspace, self.config, ledger, self.current, ["app.js"])
        self.assertEqual(ledger, before)

    def test_pending_approval_does_not_mutate_baseline_if_new_config_is_invalid(self):
        ledger = self.approve()
        before = deepcopy(ledger)
        with self.assertRaises(s.StateError):
            c.pin_documents(self.workspace, {**self.config, "test_commands": []}, ledger, self.current, ["app.js"])
        self.assertEqual(ledger, before)

    def respond_fixture(self, *, head=None, base=None, ref="main", revision=None):
        body = f"/review-cycle update-docs {self.question['request_id']} {revision or self.new_revision} README.md changed behavior"
        event = {"action": "created", "issue": {"number": 7, "pull_request": {"url": "unused"}},
                 "comment": {"body": body, "user": {"login": "pangin", "type": "User", "id": 1}}}
        path = self.root / "event.json"; c.save(path, event)
        candidate = pr(head=head or self.current["head_sha"], base=base or self.new_revision)
        candidate["base"]["ref"] = ref
        api = Mock()
        def request(route, method="GET", payload=None):
            if route.endswith("/pulls/7") and method == "GET": return candidate
            if route == "repos/pangin/example" and method == "GET": return {**repo(), "default_branch": "main"}
            if route.endswith("/issues/7/comments") and method == "POST": return {"id": 102}
            raise AssertionError((route, method))
        api.request.side_effect = request
        return path, api

    def test_respond_accepts_changed_base_only_explicit_exact_trusted_default_update(self):
        path, api = self.respond_fixture()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "pangin/example", "GITHUB_WORKSPACE": str(self.workspace)}, clear=True), \
             patch.object(c, "ledger_read", return_value=(101, self.ledger)), patch.object(c, "ledger_write") as write, \
             patch.object(c, "now", return_value=NOW):
            c.respond(self.root, path, api)
        record = write.call_args.args[4]["pulls"]["pangin/example#7"]
        self.assertEqual(record["baseline_refresh_approval"]["target_revision"], self.new_revision)
        self.assertEqual(record["human_request"]["base_sha"], self.old_revision)
        self.assertEqual(record["state"], "ready")

    def test_respond_rejects_stale_head_revision_or_nondefault_branch_without_ledger_write(self):
        for changes in [{"head": "f" * 40}, {"base": self.old_revision}, {"ref": "feature"},
                        {"revision": self.old_revision}, {"revision": self.current["head_sha"]}]:
            with self.subTest(changes=changes):
                path, api = self.respond_fixture(**changes)
                with patch.dict(os.environ, {"GITHUB_REPOSITORY": "pangin/example", "GITHUB_WORKSPACE": str(self.workspace)}, clear=True), \
                     patch.object(c, "ledger_read", return_value=(101, self.ledger)), patch.object(c, "ledger_write") as write, \
                     self.assertRaises(s.StateError):
                    c.respond(self.root, path, api)
                write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
