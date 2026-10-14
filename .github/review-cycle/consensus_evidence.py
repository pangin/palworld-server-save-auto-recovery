"""Strict adapters for explicit, same-commit document-based review verdicts.

Empty priority-finding lists are not verdicts. Native bot identity and review
metadata come from GitHub API objects, never from model JSON. This module checks
schemas, immutable evidence references, and proof bindings; the controller must
independently obtain real test/CI results and invoke ``verify_evidence`` for both
reviewers before building a proof. It does not execute model-proposed commands.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

import review_documents as documents


MARKER = "<!-- review-cycle-verdict:v1 -->"
CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CRITERION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
REPOSITORY = re.compile(r"pangin/[A-Za-z0-9_.-]+\Z")
FENCE = re.compile(r"^```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?$", re.MULTILINE | re.DOTALL)
VERDICT_KEYS = {"schema_version", "reviewer", "repository", "pr_number", "head_sha", "base_sha",
                "document_fingerprint", "manifest_sha256", "verdict", "uncertain", "criteria",
                "unresolved_findings"}
DOCUMENT_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc"}
MAX_VERDICT_BYTES = 16 * 1024
MAX_REASON_CHARACTERS = 1200
MAX_EVIDENCE_REFERENCES = 8
MAX_UNRESOLVED_FINDINGS = 100

# The controller can pass this schema to the Claude Action. Runtime validation
# below additionally binds every identity, source hash, criterion and reference.
VERDICT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": sorted(VERDICT_KEYS),
    "properties": {
        "schema_version": {"const": 1},
        "reviewer": {"enum": ["claude", "codex"]},
        "repository": {"type": "string"}, "pr_number": {"type": "integer", "minimum": 1},
        "head_sha": {"type": "string"}, "base_sha": {"type": "string"},
        "document_fingerprint": {"type": "string"}, "manifest_sha256": {"type": "string"},
        "verdict": {"enum": ["pass", "fail", "needs_review"]},
        "uncertain": {"type": "boolean"},
        "criteria": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "status", "reason", "evidence"],
            "properties": {
                "id": {"type": "string"},
                "status": {"enum": ["satisfied", "failed", "unverified", "needs_review"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": MAX_REASON_CHARACTERS},
                "evidence": {"type": "array", "maxItems": MAX_EVIDENCE_REFERENCES, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["path", "revision", "start_line", "end_line"],
                    "properties": {"path": {"type": "string"}, "revision": {"type": "string"},
                                   "start_line": {"type": "integer", "minimum": 1},
                                   "end_line": {"type": "integer", "minimum": 1}},
                }},
            },
        }},
        "unresolved_findings": {"type": "array", "items": {"type": "string", "minLength": 1},
                                "uniqueItems": True, "maxItems": MAX_UNRESOLVED_FINDINGS},
    },
}


class EvidenceError(ValueError):
    """Explicit review evidence is missing, contradictory or cannot be verified."""


def _integer(value, minimum=1):
    return type(value) is int and value >= minimum


def _text(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise EvidenceError("Review timestamps require an ISO-8601 timezone") from None


def _path(value):
    try:
        return documents._path(value)
    except documents.DocumentError:
        raise EvidenceError("Evidence paths must be canonical relative UTF-8 paths") from None


def _snapshot(snapshot):
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"schema_version", "revision", "sha256", "documents"}
            or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1
            or not isinstance(snapshot.get("revision"), str) or not SHA.fullmatch(snapshot["revision"])
            or not isinstance(snapshot.get("sha256"), str) or not DIGEST.fullmatch(snapshot["sha256"])
            or not isinstance(snapshot.get("documents"), list) or not snapshot["documents"]):
        raise EvidenceError("A complete pinned review document snapshot is required")
    found = {}
    for item in snapshot["documents"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256", "content"}
                or not isinstance(item.get("sha256"), str) or not DIGEST.fullmatch(item["sha256"])
                or not isinstance(item.get("content"), str)):
            raise EvidenceError("Malformed pinned review document")
        path = _path(item["path"])
        if path in found:
            raise EvidenceError("Duplicate pinned review document path")
        try:
            content = item["content"].encode("utf-8", errors="strict")
        except UnicodeError:
            raise EvidenceError("Pinned review documents must contain valid UTF-8") from None
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise EvidenceError("Pinned review document hash mismatch")
        found[path] = item
    body = {key: value for key, value in snapshot.items() if key != "sha256"}
    if hashlib.sha256(_canonical(body)).hexdigest() != snapshot["sha256"]:
        raise EvidenceError("Pinned document snapshot fingerprint mismatch")
    return found


def validate_manifest(criteria: list, snapshot: dict) -> dict:
    """Bind a small, trusted acceptance-criterion list to pinned source paths."""
    sources = _snapshot(snapshot)
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
        raise EvidenceError("Expected between 1 and 20 approved criteria")
    checked, seen = [], set()
    for criterion in criteria:
        if (not isinstance(criterion, dict) or set(criterion) != {"id", "source", "section", "requirement"}
                or not isinstance(criterion.get("id"), str) or not CRITERION_ID.fullmatch(criterion["id"])
                or criterion["id"] in seen or not _text(criterion.get("section"))
                or not _text(criterion.get("requirement"))):
            raise EvidenceError("Criteria require unique canonical IDs and explicit source sections/requirements")
        source = _path(criterion["source"])
        if source not in sources:
            raise EvidenceError("Each approved criterion must cite a pinned review document")
        checked.append(deepcopy(criterion)); seen.add(criterion["id"])
    result = {"schema_version": 1, "criteria": sorted(checked, key=lambda item: item["id"])}
    result["sha256"] = hashlib.sha256(_canonical(result)).hexdigest()
    return result


def _manifest(manifest, snapshot):
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "criteria", "sha256"}
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1):
        raise EvidenceError("An approved criterion manifest is required")
    checked = validate_manifest(manifest["criteria"], snapshot)
    if checked != manifest:
        raise EvidenceError("Approved criterion manifest hash/order mismatch")
    return {item["id"]: item for item in checked["criteria"]}


def _feedback(feedback):
    if (not isinstance(feedback, dict) or not isinstance(feedback.get("repository"), str)
            or not REPOSITORY.fullmatch(feedback["repository"])
            or not _integer(feedback.get("pr_number"))
            or any(not isinstance(feedback.get(key), str) or not SHA.fullmatch(feedback[key])
                   for key in ("head_sha", "base_sha"))):
        raise EvidenceError("Expected verified repository, PR and exact head/base identities")


def validate_verdict(value: dict, reviewer: str, feedback: dict, snapshot: dict, manifest: dict) -> dict:
    """Validate exact identity/criterion coverage without assuming tests passed."""
    _feedback(feedback)
    expected = _manifest(manifest, snapshot)
    if (reviewer not in {"claude", "codex"} or not isinstance(value, dict) or set(value) != VERDICT_KEYS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("reviewer") != reviewer
            or value.get("repository") != feedback["repository"]
            or not _integer(value.get("pr_number")) or value["pr_number"] != feedback["pr_number"]
            or value.get("head_sha") != feedback["head_sha"]
            or value.get("base_sha") != feedback["base_sha"]
            or value.get("document_fingerprint") != snapshot["sha256"]
            or value.get("manifest_sha256") != manifest["sha256"]
            or value.get("verdict") not in {"pass", "fail", "needs_review"}
            or type(value.get("uncertain")) is not bool
            or not isinstance(value.get("criteria"), list) or len(value["criteria"]) != len(expected)
            or not isinstance(value.get("unresolved_findings"), list)):
        raise EvidenceError("Reviewer verdict does not match the exact bound schema")
    seen = set()
    for item in value["criteria"]:
        if (not isinstance(item, dict) or set(item) != {"id", "status", "reason", "evidence"}
                or not isinstance(item.get("id"), str) or item["id"] not in expected or item["id"] in seen
                or item.get("status") not in {"satisfied", "failed", "unverified", "needs_review"}
                or not _text(item.get("reason")) or len(item["reason"]) > MAX_REASON_CHARACTERS
                or not isinstance(item.get("evidence"), list)
                or len(item["evidence"]) > MAX_EVIDENCE_REFERENCES):
            raise EvidenceError("Every approved criterion needs one explicit status and evidence list")
        seen.add(item["id"])
        refs = set()
        for ref in item["evidence"]:
            if (not isinstance(ref, dict) or set(ref) != {"path", "revision", "start_line", "end_line"}
                    or not isinstance(ref.get("revision"), str) or not SHA.fullmatch(ref["revision"])
                    or not _integer(ref.get("start_line")) or not _integer(ref.get("end_line"))
                    or ref["end_line"] < ref["start_line"]):
                raise EvidenceError("Evidence needs an immutable file path and positive ordered line range")
            path = _path(ref["path"])
            key = (path, ref["revision"], ref["start_line"], ref["end_line"])
            if key in refs:
                raise EvidenceError("Duplicate criterion evidence reference")
            refs.add(key)
    unresolved = value["unresolved_findings"]
    if (len(unresolved) > MAX_UNRESOLVED_FINDINGS or any(not _text(item) for item in unresolved)
            or len(unresolved) != len(set(unresolved))):
        raise EvidenceError("Unresolved finding IDs/reasons must be unique nonempty strings")
    if value["verdict"] == "pass" and any(item["status"] != "satisfied" for item in value["criteria"]):
        raise EvidenceError("A passing verdict cannot contain failed or unverified criteria")
    if len(_canonical(value)) > MAX_VERDICT_BYTES:
        raise EvidenceError("Explicit review verdict exceeds the complete 16 KiB limit")
    return deepcopy(value)


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError("Duplicate JSON verdict key")
            result[key] = value
        return result

    def constant(_):
        raise EvidenceError("Nonfinite JSON numbers are not valid review evidence")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError):
        raise EvidenceError("Malformed explicit JSON review verdict") from None


def parse_codex_review(reviews: list, feedback: dict, identity: dict, snapshot: dict,
                       manifest: dict, requested_at: str) -> dict | None:
    """Return one native attestation plus API metadata, or None if none exists.

    The wrapper is ``{verdict, review_ids, submitted_at}``. Model JSON cannot
    supply review IDs. More than one relevant marked attestation is ambiguous,
    even if their claims agree; the controller must reconcile that explicitly.
    """
    _feedback(feedback)
    _manifest(manifest, snapshot)
    cutoff = _timestamp(requested_at)
    if (not isinstance(identity, dict) or identity.get("login") != CODEX_BOT_LOGIN
            or identity.get("type") != "Bot" or not _integer(identity.get("id"))
            or not isinstance(reviews, list)):
        raise EvidenceError("Canonical native Codex identity and review API objects are required")
    marked = []
    for review in reviews:
        if not isinstance(review, dict):
            raise EvidenceError("Malformed GitHub review object")
        user = review.get("user") or {}
        if (not isinstance(user, dict) or any(user.get(key) != identity[key] for key in ("login", "id", "type"))
                or type(user.get("id")) is not int or review.get("commit_id") != feedback["head_sha"]
                or review.get("state") not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}):
            continue
        submitted = review.get("submitted_at")
        if _timestamp(submitted) < cutoff:
            continue
        body = review.get("body")
        if not isinstance(body, str) or MARKER not in body:
            continue
        if not _integer(review.get("id")) or body.count(MARKER) != 1:
            raise EvidenceError("Ambiguous or malformed native Codex verdict metadata")
        tail = body.split(MARKER, 1)[1]
        fences = FENCE.findall(tail)
        if len(fences) != 1:
            raise EvidenceError("A marked verdict requires exactly one JSON fence")
        verdict = validate_verdict(_json(fences[0]), "codex", feedback, snapshot, manifest)
        marked.append({"verdict": verdict, "review_ids": [review["id"]], "submitted_at": submitted})
    if len(marked) > 1:
        raise EvidenceError("Multiple or conflicting native Codex attestations require reconciliation")
    return marked[0] if marked else None


def _code_file(path):
    parts = PurePosixPath(path).parts
    return (PurePosixPath(path).suffix.lower() not in DOCUMENT_SUFFIXES
            and PurePosixPath(path).name.casefold() not in {"agents.md", "claude.md", ".mcp.json"}
            and PurePosixPath(path).name.casefold() not in {"readme", "license", "changelog", "notice", "authors"}
            and not any(part.casefold() in {".git", ".github", ".claude", ".codex"} for part in parts))


def verify_evidence(checkout: Path, verdict: dict, snapshot: dict, manifest: dict) -> dict:
    """Verify cited files/ranges from immutable objects, never working-tree files.

    This confirms existence and source bindings, not semantic correctness. The
    independent reviewers still establish whether the referenced code satisfies
    the criterion. It cannot substitute for real configured test/CI execution.
    """
    sources = _snapshot(snapshot)
    expected = _manifest(manifest, snapshot)
    if not isinstance(verdict, dict):
        raise EvidenceError("A strict reviewer verdict is required for evidence verification")
    feedback = {key: verdict.get(key) for key in ("repository", "pr_number", "head_sha", "base_sha")}
    checked = validate_verdict(verdict, verdict.get("reviewer"), feedback, snapshot, manifest)
    try:
        checkout = Path(checkout).resolve(strict=True)
        if not checkout.is_dir():
            raise ValueError
        trees, blobs = {}, {}
        for criterion in checked["criteria"]:
            source_seen, code_seen = False, False
            for ref in criterion["evidence"]:
                path, revision = ref["path"], ref["revision"]
                wanted = snapshot["revision"] if path in sources else checked["head_sha"]
                if revision != wanted:
                    raise EvidenceError("Evidence cites a stale code commit or unpinned document revision")
                if revision not in trees:
                    trees[revision] = documents._tree(checkout, revision)
                tree = trees[revision]
                if path not in tree:
                    raise EvidenceError("Evidence file is absent from its immutable Git commit")
                mode, kind, oid = tree[path]
                if mode not in {b"100644", b"100755"} or kind != b"blob":
                    raise EvidenceError("Evidence files must be regular Git blobs, not links/submodules")
                key = (revision, path)
                if key not in blobs:
                    size = int(documents._git(checkout, "cat-file", "-s", oid.decode("ascii")).strip())
                    if not 0 <= size <= 2 * 1024 * 1024:
                        raise EvidenceError("Evidence file exceeds the complete 2 MiB verification limit")
                    raw = documents._git(checkout, "cat-file", "blob", oid.decode("ascii"))
                    if len(raw) != size:
                        raise EvidenceError("Evidence blob size mismatch")
                    text = raw.decode("utf-8", errors="strict")
                    if path in sources and hashlib.sha256(raw).hexdigest() != sources[path]["sha256"]:
                        raise EvidenceError("Evidence document does not match its pinned content hash")
                    # Git/GitHub ranges are LF-delimited file lines. Python's
                    # splitlines() also counts Unicode paragraph separators and
                    # vertical tabs, incorrectly validating nonexistent lines.
                    lines = text.split("\n") if text else []
                    if text.endswith("\n"):
                        lines.pop()
                    blobs[key] = lines
                if ref["end_line"] > len(blobs[key]):
                    raise EvidenceError("Evidence line range is outside the cited immutable file")
                source_seen |= path == expected[criterion["id"]]["source"]
                code_seen |= path not in sources and _code_file(path)
            if criterion["status"] == "satisfied" and not (source_seen and code_seen):
                raise EvidenceError("Satisfied criteria require their own pinned source and current code/test evidence")
    except EvidenceError:
        raise
    except (documents.DocumentError, OSError, TypeError, ValueError, UnicodeError):
        raise EvidenceError("Immutable evidence could not be read completely as UTF-8") from None
    return checked


def build_proof(feedback: dict, snapshot: dict, manifest: dict, claude: dict,
                codex: dict, ci_proof: dict) -> dict:
    """Build the state-gate payload from verified verdicts and actual CI evidence.

    ``codex`` is the native parser wrapper, not raw model JSON. Callers must first
    run ``verify_evidence`` for both verdicts and independently fetch/validate the
    trusted test/CI checks that produced ``ci_proof``. A model cannot grant this
    module proof of CI merely by mentioning passing tests in a review reason.
    """
    _feedback(feedback)
    _manifest(manifest, snapshot)
    if feedback.get("findings") != []:
        raise EvidenceError("Current review findings remain unresolved")
    if (not isinstance(codex, dict) or set(codex) != {"verdict", "review_ids", "submitted_at"}
            or not isinstance(codex.get("review_ids"), list) or not codex["review_ids"]
            or any(not _integer(item) for item in codex["review_ids"])
            or len(codex["review_ids"]) != len(set(codex["review_ids"]))):
        raise EvidenceError("Native canonical review metadata is required for Codex proof")
    _timestamp(codex["submitted_at"])
    reviewers = {"claude": validate_verdict(claude, "claude", feedback, snapshot, manifest),
                 "codex": validate_verdict(codex["verdict"], "codex", feedback, snapshot, manifest)}
    for reviewer in reviewers.values():
        if (reviewer["verdict"] != "pass" or reviewer["uncertain"] is not False
                or reviewer["unresolved_findings"]
                or any(item["status"] != "satisfied" for item in reviewer["criteria"])):
            raise EvidenceError("Both reviewers must explicitly pass every criterion without uncertainty or disputes")
    if (not isinstance(ci_proof, dict)
            or ci_proof.get("repository") != feedback["repository"]
            or ci_proof.get("head_sha") != feedback["head_sha"]
            or ci_proof.get("base_sha") != feedback["base_sha"]
            or ci_proof.get("tests_passed") is not True or ci_proof.get("required_ci_passed") is not True
            or not isinstance(ci_proof.get("check_run_ids"), list) or not ci_proof["check_run_ids"]
            or any(not _integer(item) for item in ci_proof["check_run_ids"])
            or len(ci_proof["check_run_ids"]) != len(set(ci_proof["check_run_ids"]))):
        raise EvidenceError("Actual configured test/CI evidence must pass on the exact repository and head/base")
    proof = {"head_sha": feedback["head_sha"], "base_sha": feedback["base_sha"],
             "document_revision": snapshot["revision"], "document_fingerprint": snapshot["sha256"],
             "manifest_sha256": manifest["sha256"], "tests_passed": True, "required_ci_passed": True,
             "check_run_ids": sorted(ci_proof["check_run_ids"]), "remaining_disputes": [],
             "criterion_evidence": {}, "review_verdicts": deepcopy(reviewers)}
    for name in ("claude", "codex"):
        proof[name] = {"verified": True, "uncertain": False, "head_sha": feedback["head_sha"],
                       "base_sha": feedback["base_sha"], "document_fingerprint": snapshot["sha256"],
                       "manifest_sha256": manifest["sha256"]}
    proof["codex"]["review_ids"] = sorted(codex["review_ids"])
    for approved in manifest["criteria"]:
        refs = []
        for name, verdict in reviewers.items():
            criterion = next(item for item in verdict["criteria"] if item["id"] == approved["id"])
            refs.extend(f"{name}:{ref['path']}@{ref['revision']}:{ref['start_line']}-{ref['end_line']}"
                        for ref in criterion["evidence"])
        if not refs:
            raise EvidenceError("Passing criteria need explicit evidence references")
        proof["criterion_evidence"][approved["id"]] = {
            "claude": "satisfied", "codex": "satisfied", "evidence": sorted(set(refs)),
        }
    return proof
