"""Explicit-verdict adapters: immutable Git fixtures, no API/model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import consensus_evidence as e
import review_documents as d


BOT = {"login": e.CODEX_BOT_LOGIN, "id": 199175422, "type": "Bot"}
REQUESTED = "2026-10-01T00:00:00Z"
SUBMITTED = "2026-10-01T00:01:00Z"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.write("README.md", "# Product\n## Contract\nReturn the correct value.\n")
        self.write("AGENTS.md", "# Instructions\nFollow the documented contract.\n")
        self.write("src/main.py", "def value():\n    return 1\n")
        self.write("tests/test_main.py", "from src.main import value\n\ndef test_value():\n    assert value() == 2\n")
        self.base = self.commit()
        self.snapshot = d.build_snapshot(self.root, self.base, ["README.md"], ["src/main.py"])
        self.write("src/main.py", "def value():\n    return 2\n")
        self.head = self.commit()
        self.feedback = {"schema_version": 1, "repository": "pangin/example", "pr_number": 7,
                         "head_sha": self.head, "base_sha": self.base, "findings": [],
                         "source_review_ids": [42]}
        self.criteria = [{"id": "behavior:correct-result", "source": "README.md",
                          "section": "Contract", "requirement": "Return the correct value."}]
        self.manifest = e.validate_manifest(self.criteria, self.snapshot)

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.root,
                              check=True, capture_output=True).stdout

    def write(self, path, content):
        item = self.root / path; item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode().strip()

    def current(self):
        self.head = self.commit(); self.feedback["head_sha"] = self.head

    def ref(self, path="src/main.py", start=1, end=2, revision=None):
        return {"path": path, "revision": revision or (self.base if path in
                {item["path"] for item in self.snapshot["documents"]} else self.head),
                "start_line": start, "end_line": end}

    def verdict(self, reviewer="codex", **changes):
        value = {"schema_version": 1, "reviewer": reviewer, "repository": self.feedback["repository"],
                 "pr_number": self.feedback["pr_number"], "head_sha": self.head, "base_sha": self.base,
                 "document_fingerprint": self.snapshot["sha256"], "manifest_sha256": self.manifest["sha256"],
                 "verdict": "pass", "uncertain": False,
                 "criteria": [{"id": self.criteria[0]["id"], "status": "satisfied", "reason": "Contract and regression evidence agree.",
                               "evidence": [self.ref("README.md", 2, 3), self.ref(), self.ref("tests/test_main.py", 3, 4)]}],
                 "unresolved_findings": []}
        return {**value, **changes}

    def review(self, value=None, **changes):
        body = e.MARKER + "\n```json\n" + json.dumps(value or self.verdict()) + "\n```"
        return {"id": 42, "user": deepcopy(BOT), "commit_id": self.head,
                "state": "COMMENTED", "submitted_at": SUBMITTED, "body": body, **changes}

    def parse(self, reviews):
        return e.parse_codex_review(reviews, self.feedback, BOT, self.snapshot, self.manifest, REQUESTED)

    def validate(self, verdict):
        return e.validate_verdict(verdict, verdict.get("reviewer", "codex"),
                                  self.feedback, self.snapshot, self.manifest)

    def verify(self, verdict):
        return e.verify_evidence(self.root, verdict, self.snapshot, self.manifest)

    def ci(self, **changes):
        return {"repository": self.feedback["repository"], "head_sha": self.head, "base_sha": self.base,
                "tests_passed": True, "required_ci_passed": True, "check_run_ids": [501, 502], **changes}

    def proof(self, claude=None, codex=None, ci=None, feedback=None):
        return e.build_proof(feedback or self.feedback, self.snapshot, self.manifest,
                             claude or self.verdict("claude"), codex or self.parse([self.review()]),
                             ci or self.ci())

    def test_manifest_is_canonical_copied_and_source_bound(self):
        added = {**self.criteria[0], "id": "a-first"}
        criteria = [self.criteria[0], added]
        manifest = e.validate_manifest(criteria, self.snapshot)
        reverse = e.validate_manifest(list(reversed(criteria)), self.snapshot)
        self.assertEqual(manifest, reverse)
        self.assertEqual([item["id"] for item in manifest["criteria"]], ["a-first", self.criteria[0]["id"]])
        body = {key: value for key, value in manifest.items() if key != "sha256"}
        self.assertEqual(manifest["sha256"], hashlib.sha256(json.dumps(body, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        manifest["criteria"][0]["requirement"] = "changed"
        self.assertNotEqual(criteria[1]["requirement"], "changed")

    def test_invalid_manifest_count_keys_ids_and_sources_rejected(self):
        patches = [{"id": "space invalid"}, {"source": "missing.md"}, {"source": "../README.md"},
                   {"section": " "}, {"requirement": ""}, {"id": "../escape"}, {"command": ["rm", "-rf"]}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError):
                e.validate_manifest([{**self.criteria[0], **patch}], self.snapshot)
        for criteria in [[], self.criteria * 2, [{**self.criteria[0], "id": f"c{n}"} for n in range(21)]]:
            with self.subTest(count=len(criteria)), self.assertRaises(e.EvidenceError):
                e.validate_manifest(criteria, self.snapshot)

    def test_snapshot_and_manifest_tampering_rejected(self):
        snapshot = deepcopy(self.snapshot); snapshot["documents"][0]["content"] += "weaker"
        with self.assertRaises(e.EvidenceError): e.validate_manifest(self.criteria, snapshot)
        manifest = deepcopy(self.manifest); manifest["sha256"] = "f" * 64
        with self.assertRaises(e.EvidenceError):
            e.validate_verdict(self.verdict(), "codex", self.feedback, self.snapshot, manifest)

    def test_exact_verdict_is_copied_and_no_model_ci_claims_allowed(self):
        value = self.verdict(); checked = self.validate(value)
        checked["criteria"][0]["reason"] = "changed"
        self.assertNotEqual(value["criteria"][0]["reason"], "changed")
        for patch in [{"tests_passed": True}, {"tests_run": True}, {"review_ids": [42]}, {"run_id": "42"}]:
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError):
                self.validate({**value, **patch})

    def test_repository_pr_commit_reviewer_fingerprint_bindings(self):
        patches = [{"repository": "pangin/other"}, {"pr_number": True}, {"pr_number": 8},
                   {"head_sha": self.base}, {"base_sha": self.head}, {"schema_version": True},
                   {"document_fingerprint": "f" * 64}, {"manifest_sha256": "f" * 64},
                   {"uncertain": "false"}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError): self.validate(self.verdict(**patch))
        with self.assertRaises(e.EvidenceError):
            e.validate_verdict(self.verdict("claude"), "codex", self.feedback, self.snapshot, self.manifest)

    def test_exact_criterion_coverage_and_pass_consistency(self):
        item = self.verdict()["criteria"][0]
        for criteria in [[], [item, item], [{**item, "id": "other"}], [{**item, "status": "unverified"}],
                         [{**item, "reason": ""}], [{**item, "tests_passed": True}]]:
            with self.subTest(criteria=criteria), self.assertRaises(e.EvidenceError):
                self.validate(self.verdict(criteria=criteria))

    def test_nonpass_and_uncertain_verdicts_are_valid_but_cannot_be_proof(self):
        item = self.verdict("claude")["criteria"][0]
        for patch in [{"verdict": "needs_review", "criteria": [{**item, "status": "unverified", "evidence": []}]},
                      {"verdict": "fail", "criteria": [{**item, "status": "failed", "evidence": []}]},
                      {"uncertain": True}, {"unresolved_findings": ["comment:20"]}]:
            value = self.verdict("claude", **patch)
            self.validate(value)
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError): self.proof(claude=value)

    def test_reference_schema_paths_ranges_and_duplicates(self):
        item = self.verdict()["criteria"][0]
        invalid = [self.ref("../src/main.py"), self.ref("/src/main.py"), self.ref("src\\main.py"),
                   self.ref(start=True), self.ref(start=0), self.ref(start=3, end=2),
                   self.ref(revision="HEAD"), {**self.ref(), "result": "passed"}]
        for ref in invalid:
            with self.subTest(ref=ref), self.assertRaises(e.EvidenceError):
                self.validate(self.verdict(criteria=[{**item, "evidence": [ref]}]))
        with self.assertRaises(e.EvidenceError):
            self.validate(self.verdict(criteria=[{**item, "evidence": [self.ref(), self.ref()]}]))

    def test_verdict_bound_reason_references_unresolved_and_total_size(self):
        item = self.verdict()["criteria"][0]
        for changes in [{"reason": "x" * 1201}, {"evidence": [self.ref(start=n, end=n) for n in range(1, 10)]}]:
            with self.subTest(changes=changes), self.assertRaises(e.EvidenceError):
                self.validate(self.verdict(criteria=[{**item, **changes}]))
        with self.assertRaises(e.EvidenceError):
            self.validate(self.verdict(unresolved_findings=[f"comment:{n}" for n in range(101)]))
        criteria = [{**self.criteria[0], "id": f"criterion:{n}"} for n in range(20)]
        manifest = e.validate_manifest(criteria, self.snapshot)
        value = self.verdict(manifest_sha256=manifest["sha256"], criteria=[
            {**item, "id": criterion["id"], "reason": "x" * 1100} for criterion in criteria])
        with self.assertRaises(e.EvidenceError):
            e.validate_verdict(value, "codex", self.feedback, self.snapshot, manifest)

    def test_parse_explicit_attestation_and_api_only_metadata(self):
        parsed = self.parse([self.review()])
        self.assertEqual(parsed, {"verdict": self.verdict(), "review_ids": [42], "submitted_at": SUBMITTED})
        parsed["verdict"]["criteria"][0]["reason"] = "changed"
        self.assertNotEqual(self.verdict()["criteria"][0]["reason"], "changed")

    def test_unmarked_silence_praise_and_plain_markdown_are_not_verdicts(self):
        for body in ["Looks good", "", None, "Reviewer: Codex\nVerdict: pass\nAll good."]:
            with self.subTest(body=body): self.assertIsNone(self.parse([self.review(body=body)]))

    def test_spoofed_dismissed_pending_stale_and_pre_request_reviews_are_ignored(self):
        patches = [{"user": {**BOT, "login": "another-bot"}}, {"user": {**BOT, "id": 9}},
                   {"user": {**BOT, "type": "User"}}, {"user": {**BOT, "id": True}},
                   {"state": "DISMISSED"}, {"state": "PENDING"}, {"commit_id": self.base},
                   {"submitted_at": "2026-09-30T23:59:59Z"}]
        for patch in patches:
            with self.subTest(patch=patch):
                self.assertIsNone(self.parse([self.review(**patch)]))
                self.assertIsNotNone(self.parse([self.review(**patch), self.review()]))

    def test_marked_malformed_duplicate_json_and_missing_fences_fail_closed(self):
        payload = json.dumps(self.verdict())
        invalid = [e.MARKER + " no JSON", e.MARKER + "\n```json\nnot-json\n```",
                   e.MARKER + "\n```JSON\n" + payload + "\n```",
                   self.review()["body"] + "\n" + e.MARKER,
                   self.review()["body"] + "\n```json\n" + payload + "\n```",
                   e.MARKER + "\n```json\n{\"schema_version\":1," + payload[1:] + "\n```"]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(e.EvidenceError): self.parse([self.review(body=body)])

    def test_multiple_duplicate_or_latest_conflicting_attestations_are_not_cherry_picked(self):
        changed = self.verdict(verdict="needs_review", uncertain=True)
        for reviews in [[self.review(), self.review()],
                        [self.review(), self.review(id=43)],
                        [self.review(), self.review(changed, id=43, submitted_at="2026-10-01T00:02:00Z")]]:
            with self.subTest(reviews=reviews), self.assertRaises(e.EvidenceError): self.parse(reviews)

    def test_parser_rejects_native_malformed_identity_time_or_model_metadata(self):
        with self.assertRaises(e.EvidenceError):
            e.parse_codex_review([], self.feedback, {**BOT, "type": "User"}, self.snapshot, self.manifest, REQUESTED)
        for patch in [{"id": True}, {"submitted_at": "2026-10-01"}]:
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError): self.parse([self.review(**patch)])
        with self.assertRaises(e.EvidenceError): self.parse([self.review(self.verdict(review_ids=[42]))])

    def test_immutable_git_evidence_ignores_working_tree_changes(self):
        value = self.verdict()
        self.write("README.md", "weakened")
        self.write("src/main.py", "")
        self.assertEqual(self.verify(value), value)

    def test_satisfied_criterion_needs_own_source_and_code_not_just_documents(self):
        self.write("extra.md", "Not executable implementation.\n")
        self.current()
        item = self.verdict()["criteria"][0]
        possibilities = [[], [self.ref()], [self.ref("README.md", 2, 3)],
                         [self.ref("AGENTS.md", 1, 2), self.ref()],
                         [self.ref("README.md", 2, 3), self.ref("extra.md", 1, 1)]]
        for refs in possibilities:
            with self.subTest(refs=refs), self.assertRaises(e.EvidenceError):
                self.verify(self.verdict(criteria=[{**item, "evidence": refs}]))

    def test_code_and_document_refs_must_use_their_exact_bound_revisions(self):
        item = self.verdict()["criteria"][0]
        for ref in [self.ref(revision=self.base), self.ref("README.md", 2, 3, revision=self.head)]:
            with self.subTest(ref=ref), self.assertRaises(e.EvidenceError):
                self.verify(self.verdict(criteria=[{**item, "evidence": [ref]}]))

    def test_absent_files_and_invalid_line_ranges_rejected(self):
        item = self.verdict()["criteria"][0]
        for ref in [self.ref("missing.py"), self.ref(end=3), self.ref("README.md", 2, 1000)]:
            with self.subTest(ref=ref), self.assertRaises(e.EvidenceError):
                self.verify(self.verdict(criteria=[{**item, "evidence": [ref]}]))

    def test_line_ranges_use_git_lf_lines_not_unicode_paragraph_separators(self):
        item = self.verdict()["criteria"][0]
        self.write("src/main.py", "value = 'one\u2028two'\n")
        self.current()
        with self.assertRaises(e.EvidenceError):
            self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref(end=2)]}]))
        self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref(end=1)]}]))
        self.write("src/main.py", "")
        self.current()
        with self.assertRaises(e.EvidenceError):
            self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref(end=1)]}]))

    def test_symlink_and_gitlink_evidence_rejected(self):
        (self.root / "linked.py").symlink_to("src/main.py")
        self.current()
        item = self.verdict()["criteria"][0]
        with self.assertRaises(e.EvidenceError):
            self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref("linked.py")]}]))
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.head},module.py")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "gitlink")
        self.head = self.git("rev-parse", "HEAD").decode().strip(); self.feedback["head_sha"] = self.head
        with self.assertRaises(e.EvidenceError):
            self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref("module.py")]}]))

    def test_non_utf8_and_oversized_evidence_rejected(self):
        item = self.verdict()["criteria"][0]
        for content in [b"bad\xff\n", b"x" * (2 * 1024 * 1024 + 1)]:
            (self.root / "src/main.py").write_bytes(content); self.current()
            with self.subTest(size=len(content)), self.assertRaises(e.EvidenceError):
                self.verify(self.verdict(criteria=[{**item, "evidence": [self.ref("README.md", 2, 3), self.ref()]}]))

    def test_nonpass_without_claimed_evidence_remains_controlled(self):
        item = self.verdict()["criteria"][0]
        value = self.verdict(verdict="needs_review", uncertain=True,
                             criteria=[{**item, "status": "unverified", "evidence": []}])
        self.assertEqual(self.verify(value), value)

    def test_proof_contains_same_bindings_real_ci_ids_and_per_reviewer_evidence(self):
        self.verify(self.verdict("claude")); self.verify(self.verdict())
        proof = self.proof()
        self.assertEqual(proof["head_sha"], self.head)
        self.assertEqual(proof["document_fingerprint"], self.snapshot["sha256"])
        self.assertEqual(proof["manifest_sha256"], self.manifest["sha256"])
        self.assertEqual(proof["codex"]["review_ids"], [42])
        self.assertEqual(proof["check_run_ids"], [501, 502])
        self.assertEqual(proof["remaining_disputes"], [])
        refs = proof["criterion_evidence"][self.criteria[0]["id"]]["evidence"]
        self.assertTrue(any(ref.startswith("claude:") for ref in refs))
        self.assertTrue(any(ref.startswith("codex:") for ref in refs))

    def test_ci_claims_must_be_positive_unique_and_exact_commit_identity(self):
        patches = [{"repository": "pangin/other"}, {"head_sha": self.base}, {"base_sha": self.head},
                   {"tests_passed": False}, {"tests_passed": 1}, {"required_ci_passed": False},
                   {"check_run_ids": []}, {"check_run_ids": [True]}, {"check_run_ids": [0]},
                   {"check_run_ids": [1, 1]}]
        for patch in patches:
            with self.subTest(patch=patch), self.assertRaises(e.EvidenceError): self.proof(ci=self.ci(**patch))

    def test_proof_requires_parser_metadata_and_no_open_priority_findings(self):
        with self.assertRaises(e.EvidenceError): self.proof(codex=self.verdict())
        parsed = self.parse([self.review()]); parsed["review_ids"] = [True]
        with self.assertRaises(e.EvidenceError): self.proof(codex=parsed)
        with self.assertRaises(e.EvidenceError):
            self.proof(feedback={**self.feedback, "findings": [{"id": "comment:20"}]})

    def test_schema_matches_exact_runtime_fields_and_bounds(self):
        self.assertEqual(set(e.VERDICT_SCHEMA["properties"]), e.VERDICT_KEYS)
        self.assertEqual(set(e.VERDICT_SCHEMA["required"]), e.VERDICT_KEYS)
        self.assertFalse(e.VERDICT_SCHEMA["additionalProperties"])
        self.assertEqual(e.VERDICT_SCHEMA["properties"]["criteria"]["maxItems"], 20)
        props = e.VERDICT_SCHEMA["properties"]["criteria"]["items"]["properties"]
        self.assertEqual(props["reason"]["maxLength"], 1200)
        self.assertEqual(props["evidence"]["maxItems"], 8)


if __name__ == "__main__":
    unittest.main()
