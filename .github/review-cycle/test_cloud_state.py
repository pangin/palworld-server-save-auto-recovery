"""State, race and evidence tests for the cloud review cycle; no network calls."""
from copy import deepcopy
import unittest

import cloud_state as state
import human_input


NOW = "2026-09-30T12:00:00+09:00"
HEAD = "a" * 40
BASE = "b" * 40
NEXT = "c" * 40
DOC_REVISION = "d" * 40
DOC_FINGERPRINT = "e" * 64
BOT = {"login": state.CODEX_BOT_LOGIN, "type": "Bot", "id": 12345}


def config(**changes):
    value = {"owner": "pangin", "enabled": True, "activated_at": "2026-09-30T00:00:00Z",
             "max_rounds": 3, "model": "opus[1m]",
             "test_image": "ghcr.io/pangin/test-runtime@sha256:" + "f" * 64,
             "test_commands": [["python3", "-m", "unittest", "discover"]]}
    return {**value, **changes}


def repo():
    return {"full_name": "pangin/example", "owner": {"login": "pangin"},
            "fork": False, "archived": False}


def pr(head=HEAD, base=BASE):
    return {"number": 7, "state": "open", "draft": False, "user": {"login": "pangin"},
            "created_at": "2026-09-30T00:01:00Z", "changed_files": 1,
            "head": {"sha": head, "repo": repo()}, "base": {"sha": base, "repo": repo()}}


def review(rid=10, head=HEAD, body="Review complete", submitted="2026-09-30T00:02:00Z"):
    return {"id": rid, "user": deepcopy(BOT), "commit_id": head,
            "state": "COMMENTED", "submitted_at": submitted, "body": body}


def comment(cid=20, rid=10, head=HEAD, path="src/main.py", body="[P1 Badge] Fix incorrect result"):
    return {"id": cid, "pull_request_review_id": rid, "user": deepcopy(BOT),
            "commit_id": head, "original_commit_id": head, "line": 12, "path": path,
            "created_at": "2026-09-30T00:02:01Z", "body": body}


def feedback(head=HEAD, base=BASE, rid=10, findings=True):
    return {"schema_version": 1, "repository": "pangin/example", "pr_number": 7,
            "head_sha": head, "base_sha": base, "source_review_ids": [rid],
            "findings": ([{"id": f"comment:{rid + 10}", "body": "[P1] Fix result", "path": "src/main.py"}]
                         if findings else [])}


def structured(fb=None, decision="accepted", paths=None):
    fb = fb or feedback()
    return {"decisions": [{"id": item["id"], "decision": decision, "reason": "Independent code assessment"}
                          for item in fb["findings"]],
            "changed_files": (["src/main.py"] if paths is None else paths), "tests_run": False}


def reserve(ledger=None, fb=None, run="run-1"):
    fb = fb or feedback()
    return state.reserve(ledger or state.new_ledger(), fb, live_head_sha=fb["head_sha"],
                         live_base_sha=fb["base_sha"], run_id=run, now=NOW)


def baseline(ledger):
    result = deepcopy(ledger)
    record = result["pulls"].setdefault("pangin/example#7", {
        "state": "ready", "attempts": 0, "processed_review_ids": [], "processed_heads": [],
    })
    record.update(document_revision=DOC_REVISION, document_fingerprint=DOC_FINGERPRINT,
                  review_criteria=["requirements:correct-result"])
    return result


def proof(fb):
    reviewer = {"verified": True, "uncertain": False, "head_sha": fb["head_sha"],
                "base_sha": fb["base_sha"], "document_fingerprint": DOC_FINGERPRINT}
    return {"run_id": "verify-1", "head_sha": fb["head_sha"], "base_sha": fb["base_sha"],
            "document_fingerprint": DOC_FINGERPRINT, "tests_passed": True,
            "required_ci_passed": True, "remaining_disputes": [],
            "criterion_evidence": {"requirements:correct-result": {
                "claude": "satisfied", "codex": "satisfied",
                "evidence": ["src/main.py:12", "tests/test_main.py:42"],
            }},
            "claude": deepcopy(reviewer),
            "codex": {**reviewer, "review_ids": sorted(fb["source_review_ids"])}}


class ConfigTests(unittest.TestCase):
    def test_valid_config_is_copied_and_commands_keep_argv(self):
        source = config()
        result = state.validate_config(source)
        result["test_commands"][0].append("changed")
        self.assertEqual(source["test_commands"][0], ["python3", "-m", "unittest", "discover"])

    def test_wrong_owner_provider_limit_and_types_fail(self):
        for patch in [{"owner": "other"}, {"enabled": 1}, {"max_rounds": 0}, {"max_rounds": -1},
                      {"max_rounds": True}, {"model": "local-qwen"}, {"activated_at": "2026-09-30"}]:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.validate_config(config(**patch))

    def test_tests_require_pinned_image_nonempty_argv_and_no_shell(self):
        for patch in [{"test_image": "ubuntu:latest"}, {"test_commands": []},
                      {"test_commands": ["pytest"]}, {"test_commands": [["bash", "-c", "pytest"]]},
                      {"test_commands": [["/bin/sh", "-c", "pytest"]]},
                      {"test_commands": [["python3", ""]]}]:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.validate_test_profile(config(**patch))

    def test_disabled_config_allows_unset_activation(self):
        self.assertFalse(state.validate_config(config(enabled=False, activated_at=None))["enabled"])

    def test_pending_enrollment_accepts_omitted_or_empty_execution_fields(self):
        pending = {key: value for key, value in config(readiness="pending", max_rounds=None).items()
                   if key not in {"test_image", "test_commands"}}
        for value in [pending, {**pending, "test_image": None, "test_commands": [],
                               "review_documents": [], "review_criteria": [], "required_ci": []}]:
            with self.subTest(value=value):
                self.assertEqual(state.validate_config(value), value)
                self.assertIsNotNone(state.collect_feedback(value, repo(), pr(), [review()], [comment()], BOT))
                with self.assertRaises(state.StateError):
                    state.validate_execution_config(value)

    def test_ready_execution_cannot_omit_tests_or_run_when_disabled(self):
        for changes in [{"test_image": None}, {"test_commands": []}, {"enabled": False},
                        {"readiness": "pending"}]:
            with self.subTest(changes=changes), self.assertRaises(state.StateError):
                state.validate_execution_config(config(**changes))
        self.assertEqual(state.validate_execution_config(config()), config())

    def test_invalid_readiness_is_not_an_execution_opt_in(self):
        for value in [None, [], {}, True, "", "READY", "auto"]:
            with self.subTest(value=value), self.assertRaises(state.StateError):
                state.validate_config(config(readiness=value))

    def test_null_limit_is_explicit_and_positive_finite_legacy_limits_remain_valid(self):
        for limit in [None, 1, 3, 4, 100]:
            self.assertEqual(state.validate_config(config(max_rounds=limit))["max_rounds"], limit)
        missing = config(); del missing["max_rounds"]
        with self.assertRaises(state.StateError):
            state.validate_config(missing)


class FeedbackTests(unittest.TestCase):
    def collect(self, **changes):
        values = {"config": config(), "repository": repo(), "pr": pr(),
                  "reviews": [review()], "comments": [comment()], "identity": BOT}
        return state.collect_feedback(**{**values, **changes})

    def test_current_native_badge_and_separate_summary_are_collected(self):
        result = self.collect(reviews=[review(), review(rid=11, body="[P2] Summary issue")])
        self.assertEqual([item["id"] for item in result["findings"]], ["comment:20", "review:11"])
        self.assertEqual(result["findings"][0]["priority"], "P1")

    def test_summary_from_same_inline_review_is_not_duplicated(self):
        result = self.collect(reviews=[review(body="[P2] Summary issue")])
        self.assertEqual([item["id"] for item in result["findings"]], ["comment:20"])

    def test_final_verdict_prior_priority_reference_is_not_a_new_finding(self):
        body = (state.VERDICT_MARKER + '\n```json\n'
                '{"verdict":"pass","reason":"Previously reported [P1] is now fixed"}\n```')
        result = self.collect(reviews=[review(body=body)], comments=[])
        self.assertEqual(result["findings"], [])

    def test_final_verdict_fence_preserves_priority_prose_before_and_after(self):
        fenced = (state.VERDICT_MARKER + '\n```json\n'
                  '{"verdict":"pass","reason":"Prior [P1] fixed"}\n```\n')
        for body, priority in [("[P0] Actual remaining problem\n" + fenced, "P0"),
                               (fenced + "[P2] Another actual problem", "P2")]:
            with self.subTest(body=body):
                result = self.collect(reviews=[review(body=body)], comments=[])
                self.assertEqual(result["findings"][0]["id"], "review:10")
                self.assertEqual(result["findings"][0]["priority"], priority)

    def test_final_verdict_fence_never_hides_inline_priority_findings(self):
        body = (state.VERDICT_MARKER + '\n```json\n'
                '{"verdict":"pass","reason":"Previously reported [P1] fixed"}\n```')
        result = self.collect(reviews=[review(body=body)], comments=[comment()])
        self.assertEqual([item["id"] for item in result["findings"]], ["comment:20"])

    def test_malformed_marked_fenced_json_never_implies_completion(self):
        body = state.VERDICT_MARKER + '\n```json\n{"reason":"[P1]", malformed}\n```'
        result = self.collect(reviews=[review(body=body)], comments=[])
        self.assertEqual(result["findings"], [])
        ledger = state.acknowledge_clean(baseline(state.new_ledger()), result, now=NOW)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["state"], "awaiting_verification")
        with self.assertRaises(state.StateError):
            state.acknowledge_consensus(ledger, result, proof(result), now=NOW)

    def test_unmarked_json_priority_remains_a_review_summary(self):
        body = '```json\n{"reason":"[P1] actual finding"}\n```'
        result = self.collect(reviews=[review(body=body)], comments=[])
        self.assertEqual(result["findings"][0]["priority"], "P1")

    def test_sender_identity_requires_live_id_type_and_login(self):
        for patch in [{"id": 555}, {"type": "User"}, {"login": "codex-helper[bot]"}]:
            wrong = review()
            wrong["user"].update(patch)
            with self.subTest(patch=patch):
                self.assertIsNone(self.collect(reviews=[wrong]))
        with self.assertRaises(state.StateError):
            self.collect(identity={"login": state.CODEX_BOT_LOGIN, "type": "User", "id": 12345})

    def test_bogus_inline_author_is_not_an_actionable_finding(self):
        wrong = comment()
        wrong["user"]["id"] = 99
        self.assertEqual(self.collect(comments=[wrong])["findings"], [])

    def test_fork_archived_and_foreign_repositories_are_excluded(self):
        for patch in [{"fork": True}, {"archived": True}, {"full_name": "someone/example"}]:
            with self.subTest(patch=patch):
                self.assertIsNone(self.collect(repository={**repo(), **patch}))

    def test_closed_draft_foreign_author_and_old_pr_are_excluded(self):
        for patch in [{"state": "closed"}, {"draft": True}, {"user": {"login": "other"}},
                      {"created_at": "2026-09-29T23:59:59Z"}]:
            with self.subTest(patch=patch):
                self.assertIsNone(self.collect(pr={**pr(), **patch}))

    def test_fork_pull_request_and_different_head_repo_are_excluded(self):
        for patch in [{"fork": True}, {"full_name": "pangin/other"}]:
            candidate = pr()
            candidate["head"]["repo"].update(patch)
            with self.subTest(patch=patch):
                self.assertIsNone(self.collect(pr=candidate))

    def test_stale_review_and_pre_activation_review_are_excluded(self):
        self.assertIsNone(self.collect(reviews=[review(head=NEXT)]))
        old = review()
        old["submitted_at"] = "2026-09-29T23:00:00Z"
        self.assertIsNone(self.collect(reviews=[old]))

    def test_outdated_comment_and_unstructured_praise_do_not_become_findings(self):
        old = comment()
        old["original_commit_id"] = NEXT
        self.assertEqual(self.collect(comments=[old])["findings"], [])
        self.assertEqual(self.collect(comments=[comment(body="Looks good, thanks")])["findings"], [])

    def test_protected_path_finding_stops_for_human_resolution(self):
        with self.assertRaises(state.ProtectedFindingError) as caught:
            self.collect(comments=[comment(path=".github/workflows/build.yml")])
        self.assertEqual(caught.exception.feedback, feedback(findings=False))
        self.assertNotIn(".github", str(caught.exception))

    def test_invalid_current_path_is_a_sanitized_human_review_signal(self):
        for path in [None, "../outside", "/tmp/private", "src/\x00private"]:
            with self.subTest(path=path), self.assertRaises(state.ProtectedFindingError) as caught:
                self.collect(comments=[comment(path=path, body="[P1] untrusted private content")])
            self.assertEqual(caught.exception.feedback["findings"], [])
            self.assertNotIn("untrusted private content", str(caught.exception.feedback))

    def test_ineligible_protected_comments_never_request_human_review(self):
        for changes in [
            {"id": 0}, {"pull_request_review_id": 999},
            {"user": {**BOT, "id": 99}}, {"commit_id": NEXT},
            {"original_commit_id": NEXT}, {"created_at": "2026-09-29T23:00:00Z"},
            {"line": None}, {"body": "unstructured praise"},
        ]:
            with self.subTest(changes=changes):
                value = {**comment(path=".github/workflows/build.yml"), **changes}
                self.assertEqual(self.collect(comments=[value])["findings"], [])

    def test_duplicate_review_and_comment_ids_fail(self):
        for patch in [{"reviews": [review(), review()]}, {"comments": [comment(), comment()]}]:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                self.collect(**patch)

    def test_disabled_scope_never_collects_work(self):
        self.assertIsNone(self.collect(config=config(enabled=False)))


class LedgerTests(unittest.TestCase):
    def unlimited_reserve(self, ledger, fb, run):
        return state.reserve(ledger, fb, live_head_sha=fb["head_sha"], live_base_sha=fb["base_sha"],
                             run_id=run, now=NOW, max_rounds=None)

    def test_unlimited_fix_limit_audits_fourth_and_hundredth_fresh_attempts(self):
        ledger = state.new_ledger()
        for index in range(1, 101):
            fb = feedback(head=f"{index:040x}", rid=1000 + index)
            ledger = self.unlimited_reserve(ledger, fb, str(index))
            self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], index)
            ledger = state.finish(ledger, fb, run_id=str(index), outcome="pushed", now=NOW,
                                  pushed_head_sha=f"{index + 1:040x}")
        self.assertEqual(state._ledger(ledger)["pulls"]["pangin/example#7"]["attempts"], 100)
        self.assertNotEqual(ledger["pulls"]["pangin/example#7"]["state"], "complete")

    def test_unlimited_does_not_retry_duplicates_processing_failure_or_human_stops(self):
        first = feedback()
        processing = self.unlimited_reserve(state.new_ledger(), first, "first")
        with self.assertRaises(state.StateError):
            self.unlimited_reserve(processing, feedback(head=NEXT, rid=11), "concurrent")
        pushed = state.finish(processing, first, run_id="first", outcome="pushed", now=NOW,
                              pushed_head_sha=NEXT)
        for fb in [first, feedback(rid=11), feedback(head=NEXT, rid=10)]:
            with self.subTest(feedback=fb), self.assertRaises(state.StateError):
                self.unlimited_reserve(pushed, fb, "duplicate")
        for outcome in ["failed", "needs_review"]:
            halted = state.finish(processing, first, run_id="first", outcome=outcome, now=NOW)
            with self.subTest(outcome=outcome), self.assertRaises(state.StateError):
                self.unlimited_reserve(halted, feedback(head=NEXT, rid=11), "retry")
        fresh = feedback(head=NEXT, rid=11)
        request = human_input.build_request(fresh, kind="criteria_conflict", summary="Approve criteria",
                                           sources=[], created_at=NOW)
        request.update(status="posted", comment_id=101)
        blocked = state.request_user_input(pushed, fresh, request, now=NOW)
        with self.assertRaises(state.StateError):
            self.unlimited_reserve(blocked, feedback(head=NEXT, rid=12), "waiting-human")
        response = human_input.parse_response(f"/review-cycle stop {request['request_id']}", request,
                                              {"login": "pangin", "type": "User"})
        stopped = state.record_user_response(blocked, fresh, response, now=NOW)
        with self.assertRaises(state.StateError):
            self.unlimited_reserve(stopped, feedback(head="d" * 40, rid=12), "owner-stopped")

    def test_unlimited_does_not_repeat_completed_same_sha_consensus(self):
        clean = feedback(findings=False)
        ledger = baseline(state.new_ledger())
        ledger["pulls"]["pangin/example#7"]["attempts"] = 100
        ledger = state.acknowledge_clean(ledger, clean, now=NOW)
        ledger = state.reserve_verification(ledger, clean, run_id="verify-1", now=NOW)
        complete = state.acknowledge_consensus(ledger, clean, proof(clean), now=NOW)
        with self.assertRaises(state.StateError):
            self.unlimited_reserve(complete, feedback(rid=11), "after-consensus")
        self.assertEqual(complete["pulls"]["pangin/example#7"]["attempts"], 100)

    def test_reservation_precedes_work_and_preserves_input(self):
        original = state.new_ledger()
        current = reserve(original)
        self.assertEqual(original["pulls"], {})
        record = current["pulls"]["pangin/example#7"]
        self.assertEqual(record["state"], "processing")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["processed_review_ids"], [10])
        self.assertEqual(record["processed_heads"], [HEAD])

    def test_stale_head_and_base_are_rejected_before_work(self):
        for head, base in [(NEXT, BASE), (HEAD, NEXT)]:
            with self.subTest(head=head, base=base), self.assertRaises(state.StateError):
                state.reserve(state.new_ledger(), feedback(), live_head_sha=head, live_base_sha=base,
                              run_id="race", now=NOW)

    def test_processing_cannot_be_duplicated_or_implicitly_recovered(self):
        current = reserve()
        with self.assertRaises(state.StateError):
            reserve(current, feedback(head=NEXT, rid=11), run="another-run")

    def test_duplicate_review_and_same_head_are_not_reprocessed(self):
        current = state.finish(reserve(), feedback(), run_id="run-1", outcome="pushed", now=NOW,
                               pushed_head_sha=NEXT)
        for fb in [feedback(), feedback(rid=11), feedback(head=NEXT, rid=10)]:
            with self.subTest(feedback=fb), self.assertRaises(state.StateError):
                reserve(current, fb, run="repeat")

    def test_cap_applies_across_new_commits(self):
        ledger = state.new_ledger()
        for index, char in enumerate("acde", 1):
            fb = feedback(head=char * 40, rid=index + 10)
            if index == 4:
                with self.assertRaises(state.StateError):
                    reserve(ledger, fb, run=str(index))
                break
            ledger = reserve(ledger, fb, run=str(index))
            ledger = state.finish(ledger, fb, run_id=str(index), outcome="pushed", now=NOW,
                                  pushed_head_sha="f" * 40)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 3)
        self.assertNotEqual(ledger["pulls"]["pangin/example#7"]["state"], "complete")

    def test_failed_and_uncertain_attempts_stop_pr_without_retry(self):
        for outcome in ("failed", "needs_review"):
            ledger = state.finish(reserve(), feedback(), run_id="run-1", outcome=outcome, now=NOW)
            with self.subTest(outcome=outcome), self.assertRaises(state.StateError):
                reserve(ledger, feedback(head=NEXT, rid=11), run="retry")

    def test_finish_matches_run_head_base_and_review_ids(self):
        ledger = reserve()
        for fb, run in [(feedback(), "wrong"), (feedback(head=NEXT), "run-1"),
                        (feedback(base=NEXT), "run-1"), (feedback(rid=11), "run-1")]:
            with self.subTest(feedback=fb, run=run), self.assertRaises(state.StateError):
                state.finish(ledger, fb, run_id=run, outcome="failed", now=NOW)

    def test_direct_complete_bypasses_no_consensus_gate(self):
        with self.assertRaises(state.StateError):
            state.finish(reserve(), feedback(), run_id="run-1", outcome="complete", now=NOW)

    def test_pushed_requires_a_different_commit(self):
        for sha in (HEAD, "not-a-sha", None):
            with self.subTest(sha=sha), self.assertRaises(state.StateError):
                state.finish(reserve(), feedback(), run_id="run-1", outcome="pushed", now=NOW,
                             pushed_head_sha=sha)


class ResultTests(unittest.TestCase):
    def test_accepted_fix_moves_to_testing(self):
        result = state.validate_result(structured(), feedback(), ["src/main.py"])
        self.assertEqual(result["outcome"], "ready_to_test")

    def test_all_rejected_is_dissent_never_complete(self):
        result = state.validate_result(structured(decision="rejected", paths=[]), feedback(), [])
        self.assertEqual(result["outcome"], "needs_review")

    def test_uncertain_pauses_even_if_another_issue_was_fixed(self):
        fb = feedback()
        fb["findings"].append({"id": "review:11", "body": "[P2] Unclear requirement"})
        result = structured(fb)
        result["decisions"][1]["decision"] = "needs_review"
        self.assertEqual(state.validate_result(result, fb, ["src/main.py"])["outcome"], "needs_review")

    def test_mixed_accepted_and_rejected_pauses_without_discarding_fix_or_dispute(self):
        fb = feedback()
        fb["findings"].append({"id": "review:11", "body": "[P2] Disputed requirement"})
        result = structured(fb)
        result["decisions"][1]["decision"] = "rejected"
        checked = state.validate_result(result, fb, ["src/main.py"])
        self.assertEqual(checked["outcome"], "needs_review")
        self.assertEqual(checked["changed_files"], ["src/main.py"])
        self.assertEqual(checked["decisions"], result["decisions"])

    def test_rejected_findings_cannot_authorize_edits(self):
        with self.assertRaises(state.StateError):
            state.validate_result(structured(decision="rejected"), feedback(), ["src/main.py"])

    def test_accepted_without_actual_fix_stops(self):
        with self.assertRaises(state.StateError):
            state.validate_result(structured(paths=[]), feedback(), [])

    def test_actual_and_claimed_files_must_match(self):
        for claimed, actual in [(["src/main.py"], []), ([], ["src/main.py"]),
                                (["src/main.py", "src/main.py"], ["src/main.py"])]:
            with self.subTest(claimed=claimed, actual=actual), self.assertRaises(state.StateError):
                state.validate_result(structured(paths=claimed), feedback(), actual)

    def test_protected_traversal_symlink_and_mode_changes_stop(self):
        for path in [".git/config", ".github/workflows/main.yml", "src/.claude/settings.json",
                     "nested/.codex/config.toml", "docs/AGENTS.md", "src/CLAUDE.md", ".mcp.json",
                     "../outside", "/outside", "src\\outside"]:
            with self.subTest(path=path), self.assertRaises(state.StateError):
                state.validate_result(structured(paths=[path]), feedback(), [path])
        for flags in [{"symlink_changed": True}, {"mode_changed": True}]:
            with self.subTest(flags=flags), self.assertRaises(state.StateError):
                state.validate_result(structured(), feedback(), ["src/main.py"], **flags)

    def test_unstructured_error_missing_success_and_claimed_tests_stop(self):
        values = ["fixed", {"result": "fixed"}, {"is_error": True, "structured_output": structured()},
                  {"structured_output": structured()}, {**structured(), "tests_run": True}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(state.StateError):
                state.validate_result(value, feedback(), ["src/main.py"])
        envelope = {"is_error": False, "structured_output": structured(), "result": "tool output"}
        self.assertEqual(state.validate_result(envelope, feedback(), ["src/main.py"])["outcome"], "ready_to_test")

    def test_each_finding_requires_exactly_one_independent_decision(self):
        fb = feedback()
        fb["findings"].append({"id": "review:11", "body": "[P2] Another finding"})
        result = structured(fb)
        for patch in [result["decisions"][:1], [result["decisions"][0]] * 2,
                      [{**result["decisions"][0], "id": "unknown"}, result["decisions"][1]],
                      [{**result["decisions"][0], "reason": " "}, result["decisions"][1]]]:
            with self.subTest(decisions=patch), self.assertRaises(state.StateError):
                state.validate_result({**result, "decisions": patch}, fb, ["src/main.py"])


class ConsensusTests(unittest.TestCase):
    def awaiting(self):
        fb = feedback(findings=False)
        ledger = baseline(state.new_ledger())
        return state.acknowledge_clean(ledger, fb, now=NOW), fb

    def verifying(self):
        ledger, fb = self.awaiting()
        return state.reserve_verification(ledger, fb, run_id="verify-1", now=NOW), fb

    def test_no_priority_findings_only_await_verification(self):
        ledger, fb = self.awaiting()
        record = ledger["pulls"]["pangin/example#7"]
        self.assertEqual(record["state"], "awaiting_verification")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["verified_codex_review"], {"head_sha": HEAD, "base_sha": BASE, "review_ids": [10]})

    def test_acknowledgement_preserves_attempts_and_requires_pushed_commit(self):
        ledger = state.finish(reserve(), feedback(), run_id="run-1", outcome="pushed", now=NOW,
                               pushed_head_sha=NEXT)
        with self.assertRaises(state.StateError):
            state.acknowledge_clean(ledger, feedback(findings=False), now=NOW)
        result = state.acknowledge_clean(ledger, feedback(head=NEXT, rid=11, findings=False), now=NOW)
        self.assertEqual(result["pulls"]["pangin/example#7"]["attempts"], 1)
        self.assertEqual(result["pulls"]["pangin/example#7"]["state"], "awaiting_verification")

    def test_both_reviewers_documents_tests_and_ci_allow_completion(self):
        ledger, fb = self.verifying()
        result = state.acknowledge_consensus(ledger, fb, proof(fb), now=NOW)
        self.assertEqual(result["pulls"]["pangin/example#7"]["state"], "complete")
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["state"], "verifying")
        self.assertNotIn("verification_reservation", result["pulls"]["pangin/example#7"])
        self.assertEqual(result["pulls"]["pangin/example#7"]["verification_attempts"], {HEAD: 1})

    def test_new_finding_while_awaiting_verification_can_reserve_same_head(self):
        ledger, _ = self.awaiting()
        new = feedback(rid=11)
        new["source_review_ids"] = [10, 11]
        result = reserve(ledger, new)
        self.assertEqual(result["pulls"]["pangin/example#7"]["state"], "processing")
        self.assertEqual(result["pulls"]["pangin/example#7"]["attempts"], 1)
        self.assertEqual(result["pulls"]["pangin/example#7"]["processed_review_ids"], [10, 11])

    def test_absence_of_findings_cannot_complete_without_explicit_proof(self):
        ledger, fb = self.verifying()
        for incomplete in [{}, {"tests_passed": True}, {**proof(fb), "claude": {}},
                           {**proof(fb), "codex": {}}]:
            with self.subTest(proof=incomplete), self.assertRaises(state.StateError):
                state.acknowledge_consensus(ledger, fb, incomplete, now=NOW)

    def test_document_baseline_must_exist_and_match(self):
        ledger, fb = self.verifying()
        for field in ("document_revision", "document_fingerprint"):
            modified = deepcopy(ledger)
            del modified["pulls"]["pangin/example#7"][field]
            with self.subTest(field=field), self.assertRaises(state.StateError):
                state.acknowledge_consensus(modified, fb, proof(fb), now=NOW)
        changed = proof(fb)
        changed["document_fingerprint"] = "f" * 64
        with self.assertRaises(state.StateError):
            state.acknowledge_consensus(ledger, fb, changed, now=NOW)

    def test_stale_commit_base_review_or_reviewer_document_stops(self):
        ledger, fb = self.verifying()
        changes = [{"head_sha": NEXT}, {"base_sha": NEXT},
                   {"codex": {**proof(fb)["codex"], "review_ids": [999]}},
                   {"claude": {**proof(fb)["claude"], "head_sha": NEXT}},
                   {"codex": {**proof(fb)["codex"], "base_sha": NEXT}},
                   {"claude": {**proof(fb)["claude"], "document_fingerprint": "f" * 64}}]
        for patch in changes:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.acknowledge_consensus(ledger, fb, {**proof(fb), **patch}, now=NOW)

    def test_failed_tests_ci_uncertain_review_and_disputes_stop(self):
        ledger, fb = self.verifying()
        for patch in [{"tests_passed": False}, {"required_ci_passed": False}, {"remaining_disputes": ["finding"]},
                      {"remaining_disputes": None},
                      {"claude": {**proof(fb)["claude"], "uncertain": True}},
                      {"codex": {**proof(fb)["codex"], "verified": False}}]:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.acknowledge_consensus(ledger, fb, {**proof(fb), **patch}, now=NOW)

    def test_missing_or_incomplete_criterion_matrix_cannot_complete(self):
        ledger, fb = self.verifying()
        for matrix in (None, {}, {"extra": {"claude": "satisfied", "codex": "satisfied", "evidence": ["ref"]}},
                       {"requirements:correct-result": {"claude": "satisfied", "codex": "uncertain", "evidence": ["ref"]}},
                       {"requirements:correct-result": {"claude": "satisfied", "codex": "satisfied", "evidence": []}}):
            with self.subTest(matrix=matrix), self.assertRaises(state.StateError):
                state.acknowledge_consensus(ledger, fb, {**proof(fb), "criterion_evidence": matrix}, now=NOW)
        for criteria in (None, [], ["requirements:correct-result", "requirements:correct-result"]):
            changed = deepcopy(ledger)
            changed["pulls"]["pangin/example#7"]["review_criteria"] = criteria
            with self.subTest(criteria=criteria), self.assertRaises(state.StateError):
                state.acknowledge_consensus(changed, fb, proof(fb), now=NOW)

    def test_findings_and_dissent_cannot_be_cleared_by_clean_review(self):
        ledger, fb = self.verifying()
        with self.assertRaises(state.StateError):
            state.acknowledge_consensus(ledger, feedback(), proof(fb), now=NOW)
        for outcome in ("needs_review", "failed"):
            paused = state.finish(reserve(), feedback(), run_id="run-1", outcome=outcome, now=NOW)
            with self.subTest(outcome=outcome), self.assertRaises(state.StateError):
                state.acknowledge_clean(paused, fb, now=NOW)


class HumanInputStateTests(unittest.TestCase):
    def request(self, fb=None):
        fb = fb or feedback(findings=False)
        return human_input.build_request(
            fb, kind="review_clarification", summary="Review criteria are ambiguous.",
            sources=["docs/review-contract.md"], created_at=NOW)

    def posted(self, fb=None, ledger=None):
        fb = fb or feedback(findings=False)
        result = state.request_user_input(ledger or state.new_ledger(), fb, self.request(fb), now=NOW)
        result["pulls"]["pangin/example#7"]["human_request"].update(status="posted", comment_id=101)
        return result

    def answer(self, ledger, fb=None, option="clarify"):
        fb = fb or feedback(findings=False)
        question = ledger["pulls"]["pangin/example#7"]["human_request"]
        response = {"schema_version": 1, "request_id": question["request_id"],
                    "option_id": option, "message": "Keep the documented behavior.",
                    "actor_login": "pangin", "actor_id": 1, "body_sha256": "f" * 64}
        return state.record_user_response(ledger, fb, response, now=NOW)

    def test_question_blocks_model_and_response_reopens_fresh_review(self):
        fb = feedback(findings=False)
        request = self.request(fb)
        ledger = state.request_user_input(state.new_ledger(), fb, request, now=NOW)
        record = ledger["pulls"]["pangin/example#7"]
        # The controller changes posting -> posted only after the PR comment exists.
        record["human_request"]["status"] = "posted"
        self.assertEqual(state._ledger(ledger)["pulls"]["pangin/example#7"]["state"], "needs_user_input")
        response = {"schema_version": 1, "request_id": request["request_id"],
                    "option_id": "clarify", "message": "Keep the documented API contract.",
                    "actor_login": "pangin", "actor_id": 1, "body_sha256": "f" * 64}
        resumed = state.record_user_response(ledger, fb, response, now=NOW)
        self.assertEqual(resumed["pulls"]["pangin/example#7"]["state"], "ready")
        self.assertEqual(resumed["pulls"]["pangin/example#7"]["human_response"]["option_id"], "clarify")
        self.assertEqual(resumed["pulls"]["pangin/example#7"]["human_answered_at"], NOW)

    def test_answered_question_can_be_superseded_without_losing_audit_or_budgets(self):
        fb = feedback(findings=False)
        original = baseline(self.answer(self.posted(fb), fb))
        record = original["pulls"]["pangin/example#7"]
        record.update(attempts=2, processed_review_ids=[8, 9], processed_heads=[HEAD],
                      verification_attempts={HEAD: 1})
        before = deepcopy(original)
        next_fb = feedback(head=NEXT, rid=11, findings=False)
        result = state.request_user_input(original, next_fb, self.request(next_fb), now=NOW)
        changed = result["pulls"]["pangin/example#7"]
        self.assertEqual(changed["human_request_history"], [{
            "request": before["pulls"]["pangin/example#7"]["human_request"],
            "response": before["pulls"]["pangin/example#7"]["human_response"],
            "answered_at": NOW,
        }])
        for name in ("attempts", "processed_review_ids", "processed_heads", "verification_attempts",
                     "document_revision", "document_fingerprint", "review_criteria"):
            self.assertEqual(changed[name], record[name])
        self.assertNotIn("human_response", changed)
        self.assertNotIn("human_answered_at", changed)
        self.assertEqual(changed["state"], "needs_user_input")
        self.assertEqual(original, before)
        changed["human_request"].update(status="posted", comment_id=102)
        state._ledger(result)

    def test_stale_pending_head_or_base_can_get_a_current_question(self):
        original = self.posted()
        for next_fb in [feedback(head=NEXT, rid=11, findings=False),
                        feedback(base=NEXT, rid=11, findings=False)]:
            with self.subTest(feedback=next_fb):
                result = state.request_user_input(original, next_fb, self.request(next_fb), now=NOW)
                record = result["pulls"]["pangin/example#7"]
                self.assertEqual(record["human_request_history"], [{
                    "request": original["pulls"]["pangin/example#7"]["human_request"], "response": None}])
                self.assertEqual(record["human_request"]["head_sha"], next_fb["head_sha"])
                self.assertEqual(record["human_request"]["base_sha"], next_fb["base_sha"])
                self.assertEqual(record["state"], "needs_user_input")

    def test_same_head_pending_question_is_deduplicated_not_replaced(self):
        original = self.posted()
        self.assertEqual(state.request_user_input(original, feedback(findings=False),
                                                 self.request(), now=NOW), original)
        other = human_input.build_request(feedback(findings=False), kind="criteria_conflict",
                                           summary="Another question.", sources=[], created_at=NOW)
        with self.assertRaises(state.StateError):
            state.request_user_input(original, feedback(findings=False), other, now=NOW)

    def test_identical_answered_question_is_not_automatically_reopened(self):
        original = self.answer(self.posted())
        with self.assertRaises(state.StateError):
            state.request_user_input(original, feedback(findings=False), self.request(), now=NOW)

    def test_answered_different_kind_can_get_a_new_question_on_same_head(self):
        original = self.answer(self.posted())
        other = human_input.build_request(feedback(findings=False), kind="criteria_conflict",
                                           summary="A distinct unresolved criterion.", sources=[], created_at=NOW)
        result = state.request_user_input(original, feedback(findings=False), other, now=NOW)
        self.assertEqual(len(result["pulls"]["pangin/example#7"]["human_request_history"]), 1)

    def test_active_fix_or_verification_reservation_cannot_be_abandoned(self):
        clean = feedback(findings=False)
        verifying = state.reserve_verification(
            state.acknowledge_clean(baseline(state.new_ledger()), clean, now=NOW),
            clean, run_id="verify-1", now=NOW)
        for ledger in [reserve(), verifying]:
            before = deepcopy(ledger)
            with self.subTest(state=ledger["pulls"]["pangin/example#7"]["state"]), self.assertRaises(state.StateError):
                state.request_user_input(ledger, clean, self.request(clean), now=NOW)
            self.assertEqual(ledger, before)

    def test_owner_stop_is_preserved_even_for_a_new_head(self):
        original = self.answer(self.posted(), option="stop")
        next_fb = feedback(head=NEXT, rid=11, findings=False)
        with self.assertRaises(state.StateError):
            state.request_user_input(original, next_fb, self.request(next_fb), now=NOW)
        self.assertEqual(original["pulls"]["pangin/example#7"]["state"], "needs_review")

    def test_question_history_is_bounded_without_silent_truncation(self):
        ledger = self.posted()
        for number in range(1, state.MAX_HUMAN_REQUEST_HISTORY + 1):
            fb = feedback(head=f"{number:040x}", rid=10 + number, findings=False)
            ledger = self.posted(fb, ledger)
        before = deepcopy(ledger)
        self.assertEqual(len(ledger["pulls"]["pangin/example#7"]["human_request_history"]),
                         state.MAX_HUMAN_REQUEST_HISTORY)
        fb = feedback(head="f" * 40, rid=100, findings=False)
        with self.assertRaises(state.StateError):
            state.request_user_input(ledger, fb, self.request(fb), now=NOW)
        self.assertEqual(ledger, before)

    def test_malformed_history_and_answer_time_fail_closed(self):
        answered = self.answer(self.posted())
        entry = {"request": answered["pulls"]["pangin/example#7"]["human_request"],
                 "response": answered["pulls"]["pangin/example#7"]["human_response"], "answered_at": NOW}
        for history in [None, [entry] * (state.MAX_HUMAN_REQUEST_HISTORY + 1),
                        [{}], [{**entry, "unexpected": True}],
                        [{**entry, "request": {}}], [{**entry, "response": {}}],
                        [{**entry, "answered_at": None}],
                        [{**entry, "request": {**entry["request"], "repository": "pangin/other"}}]]:
            with self.subTest(history=history), self.assertRaises(state.StateError):
                broken = deepcopy(answered)
                broken["pulls"]["pangin/example#7"]["human_request_history"] = history
                state._ledger(broken)
        for value in [None, "not-a-time", "2026-10-03T01:00:00"]:
            with self.subTest(answered_at=value), self.assertRaises(state.StateError):
                broken = deepcopy(answered)
                broken["pulls"]["pangin/example#7"]["human_answered_at"] = value
                state._ledger(broken)
        broken = self.posted()
        broken["pulls"]["pangin/example#7"]["human_answered_at"] = NOW
        with self.assertRaises(state.StateError):
            state._ledger(broken)

    def test_stop_keeps_pr_blocked(self):
        fb = feedback(findings=False)
        request = self.request(fb)
        ledger = state.request_user_input(state.new_ledger(), fb, request, now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"]["status"] = "posted"
        response = {"schema_version": 1, "request_id": request["request_id"],
                    "option_id": "stop", "message": "", "actor_login": "pangin",
                    "actor_id": 1, "body_sha256": "f" * 64}
        result = state.record_user_response(ledger, fb, response, now=NOW)
        self.assertEqual(result["pulls"]["pangin/example#7"]["state"], "needs_review")

    def test_pending_question_requires_posted_notification(self):
        fb = feedback(findings=False)
        ledger = state.request_user_input(state.new_ledger(), fb, self.request(fb), now=NOW)
        with self.assertRaises(state.StateError):
            state._ledger(ledger)

    def test_pending_question_blocks_pure_fix_and_clean_review_transitions(self):
        fb = feedback(findings=False)
        ledger = state.request_user_input(state.new_ledger(), fb, self.request(fb), now=NOW)
        ledger["pulls"]["pangin/example#7"]["human_request"]["status"] = "posted"
        with self.assertRaises(state.StateError):
            state.reserve(ledger, feedback(), live_head_sha=HEAD, live_base_sha=BASE,
                          run_id="must-not-start", now=NOW)
        with self.assertRaises(state.StateError):
            state.acknowledge_clean(ledger, fb, now=NOW)

    def test_repeated_unanswered_question_restores_blocked_state(self):
        fb = feedback(findings=False)
        request = self.request(fb)
        ledger = state.request_user_input(state.new_ledger(), fb, request, now=NOW)
        record = ledger["pulls"]["pangin/example#7"]
        record["human_request"]["status"] = "posted"
        record["state"] = "awaiting_verification"
        restored = state.request_user_input(ledger, fb, request, now=NOW)
        self.assertEqual(restored["pulls"]["pangin/example#7"]["state"], "needs_user_input")
        self.assertEqual(restored["pulls"]["pangin/example#7"]["attempts"], 0)


class VerificationLeaseTests(unittest.TestCase):
    def awaiting(self):
        fb = feedback(findings=False)
        ledger = baseline(state.new_ledger())
        return state.acknowledge_clean(ledger, fb, now=NOW), fb

    def verifying(self):
        ledger, fb = self.awaiting()
        return state.reserve_verification(ledger, fb, run_id="verify-1", now=NOW), fb

    def test_lease_spends_per_head_budget_without_spending_fix_budget(self):
        original, fb = self.awaiting()
        ledger = state.reserve_verification(original, fb, run_id="verify-1", now=NOW)
        record = ledger["pulls"]["pangin/example#7"]
        self.assertEqual(record["state"], "verifying")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["verification_attempts"], {HEAD: 1})
        self.assertEqual(record["verification_reservation"], {
            "run_id": "verify-1", "head_sha": HEAD, "base_sha": BASE, "source_review_ids": [10]})
        self.assertEqual(original["pulls"]["pangin/example#7"]["state"], "awaiting_verification")

    def test_cannot_bypass_verification_lease_to_complete(self):
        ledger, fb = self.awaiting()
        with self.assertRaises(state.StateError):
            state.acknowledge_consensus(ledger, fb, proof(fb), now=NOW)
        ledger, fb = self.verifying()
        for patch in [{"run_id": "another"}, {"run_id": None}]:
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.acknowledge_consensus(ledger, fb, {**proof(fb), **patch}, now=NOW)

    def test_crashed_lease_cannot_retry_or_be_overwritten_by_clean_review(self):
        ledger, fb = self.verifying()
        with self.assertRaises(state.StateError):
            state.reserve_verification(ledger, fb, run_id="retry", now=NOW)
        with self.assertRaises(state.StateError):
            state.acknowledge_clean(ledger, fb, now=NOW)
        with self.assertRaises(state.StateError):
            reserve(ledger, feedback(rid=11), run="fix-race")

    def test_verification_reservation_refuses_stale_head_base_or_review_ids(self):
        ledger, _ = self.awaiting()
        for fb in [feedback(head=NEXT, findings=False), feedback(base=NEXT, findings=False),
                   feedback(rid=11, findings=False), feedback()]:
            with self.subTest(feedback=fb), self.assertRaises(state.StateError):
                state.reserve_verification(ledger, fb, run_id="verify-1", now=NOW)

    def test_verification_requires_pinned_document_baseline_and_criteria(self):
        original, fb = self.awaiting()
        for field in ("document_revision", "document_fingerprint", "review_criteria"):
            ledger = deepcopy(original)
            del ledger["pulls"]["pangin/example#7"][field]
            with self.subTest(field=field), self.assertRaises(state.StateError):
                state.reserve_verification(ledger, fb, run_id="verify-1", now=NOW)
        for maximum in (0, 2, True):
            with self.subTest(maximum=maximum), self.assertRaises(state.StateError):
                state.reserve_verification(original, fb, run_id="verify-1", now=NOW, max_attempts=maximum)

    def test_finish_requires_matching_run_head_base_and_reviews(self):
        ledger, fb = self.verifying()
        for candidate, run in [(fb, "wrong"), (feedback(head=NEXT, findings=False), "verify-1"),
                               (feedback(base=NEXT, findings=False), "verify-1"),
                               (feedback(rid=11, findings=False), "verify-1")]:
            with self.subTest(feedback=candidate, run=run), self.assertRaises(state.StateError):
                state.record_verification(ledger, candidate, run_id=run, outcome="failed", now=NOW)

    def test_each_finish_outcome_preserves_consumed_budget_and_removes_lease(self):
        original, fb = self.verifying()
        for outcome in ("failed", "needs_review", "awaiting_verification"):
            ledger = state.record_verification(original, fb, run_id="verify-1", outcome=outcome, now=NOW)
            record = ledger["pulls"]["pangin/example#7"]
            self.assertEqual(record["state"], outcome)
            self.assertEqual(record["verification_attempts"], {HEAD: 1})
            self.assertNotIn("verification_reservation", record)
            with self.subTest(outcome=outcome), self.assertRaises(state.StateError):
                state.reserve_verification(ledger, fb, run_id="again", now=NOW)
            with self.assertRaises(state.StateError):
                state.record_verification(ledger, fb, run_id="verify-1", outcome=outcome, now=NOW)

    def test_new_commit_has_one_separate_verification_attempt(self):
        ledger, fb = self.verifying()
        ledger = state.acknowledge_consensus(ledger, fb, proof(fb), now=NOW)
        current = feedback(head=NEXT, rid=11, findings=False)
        ledger = state.acknowledge_clean(ledger, current, now=NOW)
        ledger = state.reserve_verification(ledger, current, run_id="verify-2", now=NOW)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["verification_attempts"], {HEAD: 1, NEXT: 1})
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 0)

    def test_corrupt_attempts_or_missing_lease_fail_closed(self):
        original, fb = self.verifying()
        for patch in [{"verification_attempts": {HEAD: 2}}, {"verification_attempts": {HEAD: True}},
                      {"verification_attempts": {}}, {"verification_reservation": None}]:
            ledger = deepcopy(original)
            ledger["pulls"]["pangin/example#7"].update(patch)
            with self.subTest(patch=patch), self.assertRaises(state.StateError):
                state.record_verification(ledger, fb, run_id="verify-1", outcome="failed", now=NOW)


if __name__ == "__main__":
    unittest.main()
