"""Pure validation and PR-wide state for the Codex → Claude cloud review cycle.

No function performs I/O. The controller must read trusted configuration from the
default branch, fetch the bot identity independently, hold a PR lock, and persist
``reserve`` before invoking Claude. A crashed ``processing`` reservation is never
retried implicitly. State limits apply to the whole PR, including new commits.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re

import human_input


OWNER = "pangin"
CODEX_BOT_LOGIN = "chatgpt-codex-connector[bot]"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"pangin/[A-Za-z0-9_.-]+\Z")
PRIORITY = re.compile(r"\[(P[0-3])(?:\s+Badge)?\]", re.IGNORECASE)
VERDICT_MARKER = "<!-- review-cycle-verdict:v1 -->"
VERDICT_JSON_FENCE = re.compile(r"^```json[ \t]*\r?\n[\s\S]*?^```[ \t]*\r?$", re.MULTILINE)
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
STATES = {"ready", "processing", "waiting_review", "awaiting_verification", "verifying", "complete", "needs_user_input", "needs_review", "failed"}
PROTECTED_PARTS = {".git", ".github", ".claude", ".codex"}
PROTECTED_FILES = {"agents.md", "claude.md", ".mcp.json"}
MAX_HUMAN_REQUEST_HISTORY = 10
MAX_BASELINE_HISTORY = 10
BASELINE_EVIDENCE_FIELDS = {
    "document_revision", "document_fingerprint", "document_paths", "review_manifest",
    "review_criteria", "review_required_ci", "review_task", "review_requests",
    "native_final_verdict", "verified_codex_review", "consensus_proof",
    "reservation", "verification_reservation",
}

RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decisions", "changed_files", "tests_run"],
    "properties": {
        "decisions": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "decision", "reason"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "decision": {"enum": ["accepted", "rejected", "needs_review"]},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
        "changed_files": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "tests_run": {"const": False},
    },
}


class StateError(ValueError):
    """Fail closed: caller should report the error and refrain from publishing."""


class ProtectedFindingError(StateError):
    """A verified current native finding needs a visible owner decision.

    Carry only validated PR/review identities, never comment prose or a path.
    The controller must still recheck the triggering review and live PR before
    using these identities to notify the owner.
    """

    def __init__(self, feedback):
        self.feedback = _feedback({**feedback, "findings": []})
        super().__init__("A current native finding targets a protected or invalid path")


def timestamp(value: str) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise StateError("Expected an ISO-8601 timestamp with timezone") from exc


def _integer(value, minimum=1, maximum=None) -> bool:
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def _sha(value) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _login(value, expected=OWNER) -> bool:
    return isinstance(value, str) and value.casefold() == expected.casefold()


def validate_config(config: dict) -> dict:
    """Validate enrollment/scope without granting authority to execute.

    A pending enrollment may omit its execution profile. Legacy configs without
    ``readiness`` remain ready, but must pass the separate execution validator.
    Only an explicit null round limit opts into unlimited audited fix attempts.
    """
    if not isinstance(config, dict) or config.get("owner") != OWNER:
        raise StateError("Configuration must target owner pangin")
    if type(config.get("enabled")) is not bool:
        raise StateError("enabled must be a boolean")
    if "max_rounds" not in config or (config["max_rounds"] is not None and not _integer(config["max_rounds"])):
        raise StateError("max_rounds must be a positive integer or null")
    if config.get("model") != "opus[1m]":
        raise StateError("Expected official Claude Opus model opus[1m]")
    activation = config.get("activated_at")
    if config["enabled"] or activation is not None:
        timestamp(activation)
    if (not isinstance(config.get("readiness", "ready"), str)
            or config.get("readiness", "ready") not in {"pending", "ready"}):
        raise StateError("readiness must be pending or ready")
    if "readiness_reason" in config and (not isinstance(config["readiness_reason"], str)
            or "\x00" in config["readiness_reason"] or len(config["readiness_reason"].encode()) > 8192):
        raise StateError("readiness_reason must be bounded text")
    return deepcopy(config)


def validate_test_profile(config: dict) -> dict:
    """Validate trusted test execution; enrollment is not a passing profile."""
    config = validate_config(config)
    if config.get("readiness", "ready") != "ready":
        raise StateError("Repository execution readiness is pending")
    if not isinstance(config.get("test_image"), str) or not IMAGE.fullmatch(config["test_image"]):
        raise StateError("test_image must be a digest-pinned OCI image reference")
    commands = config.get("test_commands")
    if not isinstance(commands, list) or not commands:
        raise StateError("test_commands must contain at least one argv array")
    for command in commands:
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)):
            raise StateError("Each test command must be a nonempty array of string arguments")
        if PurePosixPath(command[0]).name in {"sh", "bash", "zsh", "dash", "fish", "cmd", "powershell", "pwsh"}:
            raise StateError("Shell commands are not permitted in test_commands")
    return deepcopy(config)


def validate_execution_config(config: dict) -> dict:
    """Model/test/publish jobs require enabled, ready, trusted test settings."""
    config = validate_test_profile(config)
    if not config["enabled"]:
        raise StateError("Repository execution is disabled")
    return config


def validate_bot_identity(identity: dict) -> dict:
    if (not isinstance(identity, dict) or not _login(identity.get("login"), CODEX_BOT_LOGIN)
            or identity.get("type") != "Bot" or not _integer(identity.get("id"))):
        raise StateError("Canonical Codex bot identity must be fetched and verified")
    return {"login": CODEX_BOT_LOGIN, "id": identity["id"], "type": "Bot"}


def _is_bot(user: dict, identity: dict) -> bool:
    return (isinstance(user, dict) and _login(user.get("login"), CODEX_BOT_LOGIN)
            and user.get("type") == "Bot" and type(user.get("id")) is int
            and user["id"] == identity["id"])


def _after(value, activation: datetime) -> bool:
    try:
        return timestamp(value) >= activation
    except StateError:
        return False


def _relative_path(path: str) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return False
    parts = path.split("/")
    return not path.startswith("/") and not any(part in {"", ".", ".."} for part in parts)


def path_allowed(path: str) -> bool:
    """Only canonical relative paths; control/configuration files are protected."""
    if not _relative_path(path):
        return False
    parts = path.split("/")
    if (any(part.casefold() in PROTECTED_PARTS for part in parts)
            or any(part.casefold() in PROTECTED_FILES for part in parts)):
        return False
    return True


def _summary_text(body: str) -> str:
    """Keep priority prose, excluding exact final-attestation JSON fences.

    A final verdict can cite a previous [P1] finding without raising a new one.
    Only closed JSON fences following the marker are removed. Their validity,
    uniqueness and bound verdict schema are the explicit adapter's job; even
    malformed fenced JSON must not become instructions to the fix worker.
    Priority prose outside these fences and all inline comments remain intact.
    """
    if VERDICT_MARKER not in body:
        return body
    prefix, tail = body.split(VERDICT_MARKER, 1)
    return prefix + VERDICT_MARKER + VERDICT_JSON_FENCE.sub("", tail)


def collect_feedback(config: dict, repository: dict, pr: dict, reviews: list,
                     comments: list, identity: dict) -> dict | None:
    """Build feedback only from canonical bot reviews of the live current commit.

    Inputs are GitHub REST objects. ``None`` means ineligible/no current review.
    A feedback payload with no findings is a clean current review; never invoke
    Claude for it. Review content remains untrusted data, regardless of author.
    """
    config = validate_config(config)
    identity = validate_bot_identity(identity)
    if not config["enabled"]:
        return None
    if not isinstance(repository, dict) or not isinstance(pr, dict):
        raise StateError("Repository and PR must be GitHub REST objects")
    if not isinstance(reviews, list) or not isinstance(comments, list):
        raise StateError("Reviews and comments must be arrays")
    activation = timestamp(config["activated_at"])
    name = repository.get("full_name")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    head_repo, base_repo = head.get("repo") or {}, base.get("repo") or {}
    if (not isinstance(name, str) or not REPOSITORY.fullmatch(name)
            or not _login((repository.get("owner") or {}).get("login"))
            or repository.get("fork") is not False or repository.get("archived") is not False
            or pr.get("state") != "open" or pr.get("draft") is not False
            or not _integer(pr.get("number")) or not _login((pr.get("user") or {}).get("login"))
            or not _after(pr.get("created_at"), activation)
            or not _login(head_repo.get("full_name"), name)
            or not _login(base_repo.get("full_name"), name)
            or head_repo.get("fork") is not False or base_repo.get("fork") is not False
            or not _sha(head.get("sha")) or not _sha(base.get("sha"))):
        return None
    eligible = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise StateError("Malformed review object")
        rid = review.get("id")
        if (not _integer(rid) or not _is_bot(review.get("user"), identity)
                or review.get("commit_id") != head["sha"]
                or review.get("state") not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}
                or not _after(review.get("submitted_at"), activation)):
            continue
        if rid in eligible:
            raise StateError("Duplicate review ID in fetched data")
        eligible[rid] = review
    if not eligible:
        return None
    findings, seen_comments = [], set()
    feedback = {"schema_version": 1, "repository": name, "pr_number": pr["number"],
                "head_sha": head["sha"], "base_sha": base["sha"],
                "source_review_ids": sorted(eligible), "findings": findings}
    for comment in comments:
        if not isinstance(comment, dict):
            raise StateError("Malformed review comment object")
        cid, rid = comment.get("id"), comment.get("pull_request_review_id")
        body = comment.get("body")
        priority = PRIORITY.search(body) if isinstance(body, str) else None
        if (not _integer(cid) or rid not in eligible or not _is_bot(comment.get("user"), identity)
                or comment.get("commit_id") != head["sha"]
                or comment.get("original_commit_id", head["sha"]) != head["sha"]
                or not _after(comment.get("created_at"), activation) or not priority
                or (comment.get("subject_type") != "file" and not _integer(comment.get("line")))):
            continue
        if not path_allowed(comment.get("path")):
            raise ProtectedFindingError(feedback)
        if cid in seen_comments:
            raise StateError("Duplicate review comment ID in fetched data")
        seen_comments.add(cid)
        item = {"id": f"comment:{cid}", "body": body, "priority": priority.group(1).upper(),
                "path": comment["path"], "review_id": rid}
        if comment.get("line") is not None:
            item["line"] = comment["line"]
        findings.append(item)
    for rid, review in sorted(eligible.items()):
        body = review.get("body")
        priority = PRIORITY.search(_summary_text(body)) if isinstance(body, str) else None
        if priority and not any(finding["review_id"] == rid for finding in findings):
            findings.append({"id": f"review:{rid}", "body": body,
                             "priority": priority.group(1).upper(), "review_id": rid})
    return feedback


def _feedback(value: dict, require_findings=False) -> dict:
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("repository"), str)
            or not REPOSITORY.fullmatch(value["repository"])
            or not _integer(value.get("pr_number"))
            or not _sha(value.get("head_sha")) or not _sha(value.get("base_sha"))):
        raise StateError("Malformed feedback identity")
    ids = value.get("source_review_ids")
    if (not isinstance(ids, list) or not ids or any(not _integer(rid) for rid in ids)
            or len(ids) != len(set(ids))):
        raise StateError("Feedback requires unique source review IDs")
    findings = value.get("findings")
    if not isinstance(findings, list) or (require_findings and not findings) or len(findings) > 100:
        raise StateError("Expected between 1 and 100 actionable findings" if require_findings else "Invalid findings")
    finding_ids = set()
    for finding in findings:
        if (not isinstance(finding, dict) or not isinstance(finding.get("id"), str)
                or not finding["id"] or finding["id"] in finding_ids
                or not isinstance(finding.get("body"), str) or not finding["body"].strip()
                or ("path" in finding and not _relative_path(finding["path"]))):
            raise StateError("Malformed or duplicate finding")
        finding_ids.add(finding["id"])
    return value


def new_ledger() -> dict:
    return {"schema_version": 1, "pulls": {}}


def validate_baseline_approval(value):
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("request_id"), str)
            or not re.fullmatch(r"human-[0-9a-f]{20}", value["request_id"])
            or any(not _sha(value.get(name)) for name in ("head_sha", "original_base_sha", "target_revision"))
            or value["head_sha"] == value["target_revision"]
            or not isinstance(value.get("source_review_ids"), list) or not value["source_review_ids"]
            or any(not _integer(rid) for rid in value["source_review_ids"])
            or len(value["source_review_ids"]) != len(set(value["source_review_ids"]))
            or value.get("status") not in {"pending", "consumed", "superseded"}):
        raise StateError("Malformed trusted document baseline approval")
    timestamp(value.get("approved_at"))
    if value["status"] != "pending":
        timestamp(value.get(value["status"] + "_at"))
    return deepcopy(value)


def _ledger(value: dict) -> dict:
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("pulls"), dict)):
        raise StateError("Malformed persistent ledger")
    for key, record in value["pulls"].items():
        if (not isinstance(key, str) or not re.fullmatch(r"pangin/[A-Za-z0-9_.-]+#[1-9][0-9]*", key)
                or not isinstance(record, dict) or record.get("state") not in STATES
                or not _integer(record.get("attempts"), minimum=0)
                or not isinstance(record.get("processed_review_ids"), list)
                or any(not _integer(rid) for rid in record["processed_review_ids"])
                or not isinstance(record.get("processed_heads"), list)
                or any(not _sha(sha) for sha in record["processed_heads"])):
            raise StateError("Malformed PR ledger record")
        verification_attempts = record.get("verification_attempts", {})
        if (not isinstance(verification_attempts, dict)
                or any(not _sha(head) or not _integer(count, minimum=0, maximum=1)
                       for head, count in verification_attempts.items())):
            raise StateError("Malformed per-head verification attempts")
        lease = record.get("verification_reservation")
        if record["state"] == "verifying":
            if (not isinstance(lease, dict)
                    or set(lease) != {"run_id", "head_sha", "base_sha", "source_review_ids"}
                    or not isinstance(lease.get("run_id"), str) or not lease["run_id"].strip()
                    or not _sha(lease.get("head_sha")) or not _sha(lease.get("base_sha"))
                    or not isinstance(lease.get("source_review_ids"), list) or not lease["source_review_ids"]
                    or any(not _integer(rid) for rid in lease["source_review_ids"])
                    or len(lease["source_review_ids"]) != len(set(lease["source_review_ids"]))
                    or verification_attempts.get(lease["head_sha"]) != 1):
                raise StateError("Malformed or unbudgeted verification reservation")
        elif lease is not None:
            raise StateError("Verification reservation exists outside verifying state")
        question = record.get("human_request")
        response = record.get("human_response")
        if question is not None:
            try:
                human_input.validate_request(question)
            except human_input.HumanInputError as exc:
                raise StateError(str(exc)) from exc
        if response is not None:
            if question is None:
                raise StateError("Human response exists without a question")
            try:
                human_input.validate_response(response, question)
            except human_input.HumanInputError as exc:
                raise StateError(str(exc)) from exc
        answered_at = record.get("human_answered_at")
        if "human_answered_at" in record:
            timestamp(answered_at)
            if response is None or question.get("status") != "answered":
                raise StateError("Human answer time requires an answered question and response")
        history = record.get("human_request_history", [])
        if not isinstance(history, list) or len(history) > MAX_HUMAN_REQUEST_HISTORY:
            raise StateError("Human question history exceeds its bounded audit limit")
        for entry in history:
            if (not isinstance(entry, dict)
                    or not {"request", "response"}.issubset(entry)
                    or set(entry) - {"request", "response", "answered_at"}):
                raise StateError("Malformed human question history entry")
            try:
                archived = human_input.validate_request(entry["request"])
                if f"{archived['repository']}#{archived['pr_number']}" != key:
                    raise StateError("Archived human question targets another PR")
                if entry["response"] is not None:
                    human_input.validate_response(entry["response"], archived)
            except human_input.HumanInputError as exc:
                raise StateError(str(exc)) from exc
            if "answered_at" in entry:
                timestamp(entry["answered_at"])
                if entry["response"] is None or archived["status"] != "answered":
                    raise StateError("Archived answer time requires an answered question and response")
        if record["state"] == "needs_user_input" and (question is None or question.get("status") != "posted"):
            raise StateError("Human-input state requires a posted question")
        approval = record.get("baseline_refresh_approval")
        if approval is not None:
            validate_baseline_approval(approval)
            if approval["status"] == "pending" and (
                    response is None or response.get("option_id") != "update-docs"
                    or response.get("request_id") != approval["request_id"]
                    or response.get("approved_revision") != approval["target_revision"]
                    or question.get("head_sha") != approval["head_sha"]
                    or question.get("base_sha") != approval["original_base_sha"]):
                raise StateError("Pending document approval has no matching owner response")
        baselines = record.get("baseline_history", [])
        if not isinstance(baselines, list) or len(baselines) > MAX_BASELINE_HISTORY:
            raise StateError("Document baseline history exceeds its bounded audit limit")
        for entry in baselines:
            if (not isinstance(entry, dict) or set(entry) != {"baseline", "approval", "refreshed_at"}
                    or not isinstance(entry["baseline"], dict)
                    or set(entry["baseline"]) - BASELINE_EVIDENCE_FIELDS):
                raise StateError("Malformed document baseline audit history")
            validate_baseline_approval(entry["approval"])
            if entry["approval"]["status"] != "consumed":
                raise StateError("Baseline audit entry requires a consumed approval")
            timestamp(entry["refreshed_at"])
    return deepcopy(value)


def _key(feedback: dict) -> str:
    return f"{feedback['repository']}#{feedback['pr_number']}"


def reserve(ledger: dict, feedback: dict, *, live_head_sha: str, live_base_sha: str,
            run_id: str, now: str, max_rounds: int | None = 3) -> dict:
    """Reserve one attempt; caller atomically persists returned ledger under lock.

    Rejects stale head/base, seen reviews, a previously handled head, in-progress
    work and failed/uncertain PRs. Null removes only the count cap, not those gates;
    finite limits remain PR-wide and are never reset by a new head SHA.
    """
    feedback = _feedback(feedback, require_findings=True)
    result = _ledger(ledger)
    timestamp(now)
    if ((max_rounds is not None and not _integer(max_rounds))
            or not isinstance(run_id, str) or not run_id.strip()):
        raise StateError("Invalid reservation parameters")
    if live_head_sha != feedback["head_sha"] or live_base_sha != feedback["base_sha"]:
        raise StateError("PR head or base changed before reservation")
    key = _key(feedback)
    record = result["pulls"].setdefault(key, {
        "state": "ready", "attempts": 0, "processed_review_ids": [], "processed_heads": [],
    })
    if record["state"] in {"processing", "verifying", "needs_user_input", "needs_review", "failed"}:
        raise StateError("PR is already processing or requires human attention")
    if max_rounds is not None and record["attempts"] >= max_rounds:
        raise StateError("PR-wide automatic fix limit reached")
    new_reviews = set(feedback["source_review_ids"]) - set(record["processed_review_ids"])
    if ((feedback["head_sha"] in record["processed_heads"]
         and record["state"] != "awaiting_verification") or not new_reviews):
        raise StateError("Review or current head has already been handled")
    record.update({"state": "processing", "attempts": record["attempts"] + 1,
                   "reservation": {"run_id": run_id, "head_sha": feedback["head_sha"],
                                   "base_sha": feedback["base_sha"],
                                   "source_review_ids": sorted(feedback["source_review_ids"])},
                   "updated_at": now})
    record["processed_review_ids"] = sorted(set(record["processed_review_ids"] + feedback["source_review_ids"]))
    record["processed_heads"] = sorted(set(record["processed_heads"] + [feedback["head_sha"]]))
    return result


def finish(ledger: dict, feedback: dict, *, run_id: str, outcome: str,
           now: str, pushed_head_sha: str | None = None) -> dict:
    """Finish the matching reservation. ``pushed`` transitions to waiting_review.

    A failure consumes its reserved attempt and stops the PR rather than silently
    retrying. The publisher must check current head/base again before pushing.
    """
    feedback = _feedback(feedback, require_findings=True)
    result = _ledger(ledger)
    timestamp(now)
    if outcome not in {"awaiting_verification", "needs_review", "failed", "pushed"}:
        raise StateError("Invalid finish outcome")
    record = result["pulls"].get(_key(feedback))
    expected = {"run_id": run_id, "head_sha": feedback["head_sha"],
                "base_sha": feedback["base_sha"], "source_review_ids": sorted(feedback["source_review_ids"])}
    if not record or record["state"] != "processing" or record.get("reservation") != expected:
        raise StateError("Reservation mismatch or already finished")
    if outcome == "pushed":
        if not _sha(pushed_head_sha) or pushed_head_sha == feedback["head_sha"]:
            raise StateError("A pushed result requires a different valid commit SHA")
        record["pushed_head_sha"] = pushed_head_sha
    elif pushed_head_sha is not None:
        raise StateError("Commit SHA is only valid for a pushed result")
    record.update({"state": "waiting_review" if outcome == "pushed" else outcome,
                   "updated_at": now, "last_run_id": run_id})
    record.pop("reservation")
    return result


def acknowledge_clean(ledger: dict, feedback: dict, *, now: str) -> dict:
    """Store a current Codex review for verification, never infer completion.

    Absence of priority findings is not positive evidence of document compliance
    or agreement between the two reviewers. The controller attaches a trusted
    document revision/fingerprint baseline before calling this function.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    if feedback["findings"]:
        raise StateError("Review is not clean")
    record = result["pulls"].setdefault(_key(feedback), {
        "state": "ready", "attempts": 0, "processed_review_ids": [], "processed_heads": [],
    })
    if record["state"] in {"processing", "verifying", "needs_user_input", "failed", "needs_review"}:
        raise StateError("PR requires attention before acknowledging a clean review")
    if record["state"] == "waiting_review" and record.get("pushed_head_sha") != feedback["head_sha"]:
        raise StateError("Clean review does not match the pushed commit")
    record.update({"state": "awaiting_verification", "updated_at": now,
                   "verified_codex_review": {
                       "head_sha": feedback["head_sha"], "base_sha": feedback["base_sha"],
                       "review_ids": sorted(feedback["source_review_ids"]),
                   }})
    record["processed_review_ids"] = sorted(set(record["processed_review_ids"] + feedback["source_review_ids"]))
    record["processed_heads"] = sorted(set(record["processed_heads"] + [feedback["head_sha"]]))
    return result


def request_user_input(ledger: dict, feedback: dict, request: dict, *, now: str) -> dict:
    """Persist a visible question and block all automatic work for this head.

    The controller posts the question after this intent is persisted.  A posted
    request is required for the ``needs_user_input`` state, so an interrupted
    notification remains inspectable instead of being silently retried. A stale
    question or answered different question may be superseded with a bounded
    audit record, without resetting budgets or abandoning active reservations.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    try:
        request = human_input.validate_request(request)
    except human_input.HumanInputError as exc:
        raise StateError(str(exc)) from exc
    if (request["repository"] != feedback["repository"]
            or request["pr_number"] != feedback["pr_number"]
            or request["head_sha"] != feedback["head_sha"]
            or request["base_sha"] != feedback["base_sha"]):
        raise StateError("Human question does not match the current PR head/base")
    record = result["pulls"].setdefault(_key(feedback), {
        "state": "ready", "attempts": 0, "processed_review_ids": [], "processed_heads": [],
    })
    if record["state"] in {"processing", "verifying"}:
        raise StateError("An active reservation cannot be replaced by a human question")
    if record["state"] == "complete":
        raise StateError("Completed PR cannot be redirected to human input")
    if (record.get("human_response") or {}).get("option_id") == "stop":
        raise StateError("An owner stop cannot be reopened by an automatic question")
    previous = record.get("human_request")
    if previous is not None:
        same_head = all(previous[name] == request[name] for name in ("head_sha", "base_sha"))
        same_request = same_head and previous["request_id"] == request["request_id"]
        if previous["status"] == "answered" and same_request:
            raise StateError("This human question was already answered; a fresh question identity is required")
        if previous["status"] != "answered" and same_head:
            if not same_request:
                raise StateError("A different human question is already pending for this head/base")
            record.update({"state": "needs_user_input", "updated_at": now})
            return result
        history = record.setdefault("human_request_history", [])
        if len(history) >= MAX_HUMAN_REQUEST_HISTORY:
            raise StateError("Human question history is full; manual attention required without truncation")
        archived = {"request": deepcopy(previous), "response": deepcopy(record.get("human_response"))}
        if record.get("human_answered_at") is not None:
            archived["answered_at"] = record["human_answered_at"]
        history.append(archived)
    record.update({"state": "needs_user_input", "updated_at": now,
                   "human_request": request})
    approval = record.get("baseline_refresh_approval")
    if approval and approval["status"] == "pending":
        approval.update(status="superseded", superseded_at=now)
    record.pop("human_response", None)
    record.pop("human_answered_at", None)
    return result


def record_user_response(ledger: dict, feedback: dict, response: dict, *, now: str,
                         trusted_default_revision: str | None = None) -> dict:
    """Record an owner response and make a fresh review eligible.

    A response never completes a PR.  It only changes the blocked state back to
    ``ready`` (or keeps it ``needs_review`` for ``stop``); a new canonical review
    is still required before any model or publisher step can run.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    record = result["pulls"].get(_key(feedback))
    request = record.get("human_request") if record else None
    try:
        response = human_input.validate_response(response, request)
    except human_input.HumanInputError as exc:
        raise StateError(str(exc)) from exc
    updating = response.get("option_id") == "update-docs"
    if (not record or record["state"] != "needs_user_input"
            or request["head_sha"] != feedback["head_sha"]
            or (not updating and request["base_sha"] != feedback["base_sha"])):
        raise StateError("No matching human question is awaiting a response")
    if updating:
        revision = response["approved_revision"]
        if (not _sha(trusted_default_revision) or revision != trusted_default_revision
                or revision != feedback["base_sha"] or revision == feedback["head_sha"]):
            raise StateError("Document approval is not the live trusted default commit")
        record["baseline_refresh_approval"] = {
            "schema_version": 1, "request_id": response["request_id"],
            "head_sha": request["head_sha"], "original_base_sha": request["base_sha"],
            "target_revision": revision, "source_review_ids": list(request["source_review_ids"]),
            "approved_at": now, "status": "pending",
        }
    request = deepcopy(request)
    request["status"] = "answered"
    record.update({"state": "needs_review" if response["option_id"] == "stop" else "ready",
                   "updated_at": now, "human_request": request,
                   "human_response": response, "human_answered_at": now})
    return result


def consume_baseline_refresh(record: dict, feedback: dict, *, trusted_revision: str, now: str) -> dict:
    """Consume one explicit default-document approval without resetting budgets.

    The controller must first verify the snapshot/config at trusted_revision and
    pass its genuinely post-answer review gate. This function also rejects old
    question review IDs, active work, stale head/base and PR-candidate baselines.
    """
    feedback = _feedback(feedback)
    timestamp(now)
    result = deepcopy(record)
    approval = validate_baseline_approval(result.get("baseline_refresh_approval"))
    response = result.get("human_response") or {}
    if (approval["status"] != "pending" or result.get("state") != "ready"
            or response.get("option_id") != "update-docs"
            or response.get("request_id") != approval["request_id"]
            or response.get("approved_revision") != approval["target_revision"]
            or approval["target_revision"] != trusted_revision
            or approval["target_revision"] != feedback["base_sha"]
            or approval["head_sha"] != feedback["head_sha"]
            or approval["head_sha"] == trusted_revision
            or set(approval["source_review_ids"]) & set(feedback["source_review_ids"])):
        raise StateError("Document approval is stale, already consumed or lacks a fresh current review")
    history = result.setdefault("baseline_history", [])
    if not isinstance(history, list) or len(history) >= MAX_BASELINE_HISTORY:
        raise StateError("Document baseline history is full; manual attention required without truncation")
    approval.update(status="consumed", consumed_at=now)
    history.append({"baseline": {key: deepcopy(result[key]) for key in BASELINE_EVIDENCE_FIELDS if key in result},
                    "approval": deepcopy(approval), "refreshed_at": now})
    for key in BASELINE_EVIDENCE_FIELDS:
        result.pop(key, None)
    result["baseline_refresh_approval"] = approval
    result["updated_at"] = now
    return result


def _document_baseline(record: dict) -> tuple[str, list[str]]:
    fingerprint, criteria = record.get("document_fingerprint"), record.get("review_criteria")
    if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not _sha(record.get("document_revision"))):
        raise StateError("Trusted document baseline is missing")
    if (not isinstance(criteria, list) or not criteria
            or any(not isinstance(item, str) or not item.strip() or "\x00" in item for item in criteria)
            or len(criteria) != len(set(criteria))):
        raise StateError("Trusted document review criteria are missing or invalid")
    return fingerprint, criteria


def _matching_verification(record: dict | None, feedback: dict, run_id: str) -> None:
    expected = {"run_id": run_id, "head_sha": feedback["head_sha"],
                "base_sha": feedback["base_sha"], "source_review_ids": sorted(feedback["source_review_ids"])}
    if (not isinstance(run_id, str) or not run_id.strip() or not record
            or record["state"] != "verifying" or record.get("verification_reservation") != expected):
        raise StateError("Verification reservation mismatch or already finished")


def reserve_verification(ledger: dict, feedback: dict, *, run_id: str,
                         now: str, max_attempts: int = 1) -> dict:
    """Persist a single read-only Claude verification attempt for this head.

    The caller must hold the PR lock, confirm current head/base immediately before
    this call and persist the returned lease before invoking the model. A crash
    consumes the attempt and leaves verifying state; it never causes an implicit
    retry. This per-head budget is independent of the PR-wide fix budget.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    if not _integer(max_attempts, maximum=1) or not isinstance(run_id, str) or not run_id.strip():
        raise StateError("Verification allows exactly one attempt per head")
    record = result["pulls"].get(_key(feedback))
    expected_review = {"head_sha": feedback["head_sha"], "base_sha": feedback["base_sha"],
                       "review_ids": sorted(feedback["source_review_ids"])}
    if (not record or record["state"] != "awaiting_verification" or feedback["findings"]
            or record.get("verified_codex_review") != expected_review):
        raise StateError("Verification requires the stored current canonical Codex review")
    _document_baseline(record)
    attempts = record.setdefault("verification_attempts", {})
    if attempts.get(feedback["head_sha"], 0) >= max_attempts:
        raise StateError("Current head verification attempt already consumed")
    attempts[feedback["head_sha"]] = attempts.get(feedback["head_sha"], 0) + 1
    record.update({"state": "verifying", "updated_at": now,
                   "verification_reservation": {
                       "run_id": run_id, "head_sha": feedback["head_sha"],
                       "base_sha": feedback["base_sha"],
                       "source_review_ids": sorted(feedback["source_review_ids"]),
                   }})
    return result


def record_verification(ledger: dict, feedback: dict, *, run_id: str,
                        outcome: str, now: str) -> dict:
    """Finish a verification lease without resetting its consumed attempt.

    Returning to awaiting_verification may retain pending evidence but does not
    permit another model invocation on the same commit. Successful completion is
    only available through acknowledge_consensus while its lease remains active.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    if outcome not in {"failed", "needs_review", "awaiting_verification"}:
        raise StateError("Invalid verification outcome")
    record = result["pulls"].get(_key(feedback))
    _matching_verification(record, feedback, run_id)
    record.update({"state": outcome, "updated_at": now, "last_verification_run_id": run_id})
    record.pop("verification_reservation")
    return result


def acknowledge_consensus(ledger: dict, feedback: dict, proof: dict, *, now: str) -> dict:
    """Complete only with explicit same-commit, same-document agreement and tests.

    ``proof`` is trusted controller evidence, never a parsed review-text claim. It
    contains run_id matching an active verification lease, head_sha, base_sha,
    document_fingerprint, tests_passed,
    required_ci_passed, remaining_disputes, and claude/codex objects. Each reviewer
    object must assert verified=True, uncertain=False and the same head/base/doc
    fingerprint. Codex additionally carries the recorded canonical review_ids.
    A trusted document_revision, document_fingerprint and review_criteria ID list
    must already exist in the PR ledger. The proof's criterion_evidence must
    cover each trusted criterion with claude/codex "satisfied" decisions and
    nonempty evidence reference strings. This is a low-level assertion gate;
    semantic document review and trustworthy evidence production remain the
    controller's responsibility. Any change requires explicit re-verification.
    """
    feedback = _feedback(feedback)
    result = _ledger(ledger)
    timestamp(now)
    record = result["pulls"].get(_key(feedback))
    if (not record or record["state"] != "verifying" or feedback["findings"]
            or not isinstance(proof, dict)):
        raise StateError("Completion requires an active verification lease and explicit consensus")
    _matching_verification(record, feedback, proof.get("run_id"))
    fingerprint, criteria = _document_baseline(record)
    if (proof.get("head_sha") != feedback["head_sha"]
            or proof.get("base_sha") != feedback["base_sha"]
            or proof.get("document_fingerprint") != fingerprint
            or proof.get("tests_passed") is not True
            or proof.get("required_ci_passed") is not True
            or proof.get("remaining_disputes") != []):
        raise StateError("Missing, stale, untested or disputed completion evidence")
    if ("document_revision" in proof
            and proof["document_revision"] != record["document_revision"]):
        raise StateError("Document revision changed during verification")
    expected_review = {"head_sha": feedback["head_sha"], "base_sha": feedback["base_sha"],
                       "review_ids": sorted(feedback["source_review_ids"])}
    if record.get("verified_codex_review") != expected_review:
        raise StateError("Canonical Codex review evidence is missing or stale")
    for name in ("claude", "codex"):
        reviewer = proof.get(name)
        if (not isinstance(reviewer, dict) or reviewer.get("verified") is not True
                or reviewer.get("uncertain") is not False
                or reviewer.get("head_sha") != feedback["head_sha"]
                or reviewer.get("base_sha") != feedback["base_sha"]
                or reviewer.get("document_fingerprint") != fingerprint):
            raise StateError("Both reviewers must explicitly verify the same commit and documents")
    if proof["codex"].get("review_ids") != expected_review["review_ids"]:
        raise StateError("Codex consensus does not match the recorded canonical reviews")
    matrix = proof.get("criterion_evidence")
    if not isinstance(matrix, dict) or set(matrix) != set(criteria):
        raise StateError("Criterion evidence must exactly cover the trusted criteria")
    for item in matrix.values():
        if (not isinstance(item, dict) or item.get("claude") != "satisfied"
                or item.get("codex") != "satisfied" or not isinstance(item.get("evidence"), list)
                or not item["evidence"]
                or any(not isinstance(ref, str) or not ref.strip() or "\x00" in ref for ref in item["evidence"])):
            raise StateError("Every document criterion needs explicit agreement and evidence")
    record.update({"state": "complete", "updated_at": now, "consensus_proof": deepcopy(proof),
                   "last_verification_run_id": proof["run_id"]})
    record.pop("verification_reservation")
    return result


def validate_result(result: dict, feedback: dict, actual_changed_paths: list[str], *,
                    symlink_changed: bool = False, mode_changed: bool = False) -> dict:
    """Validate Claude's structured output against findings and the actual diff.

    Accept either a bare structured object or the Claude CLI JSON envelope with
    ``is_error: false`` and ``structured_output``. Return normalized decisions,
    paths and ``outcome`` (needs_review or ready_to_test). Test execution
    is the controller's job, using trusted commands rather than model suggestions.
    Any rejected finding requires independent reconciliation before automatic
    continuation, including a mixed result that fixes other accepted findings.
    """
    feedback = _feedback(feedback, require_findings=True)
    if not isinstance(result, dict):
        raise StateError("Claude result must be structured JSON")
    if "structured_output" in result:
        if result.get("is_error") is not False:
            raise StateError("Claude reported an error or missing success status")
        result = result["structured_output"]
    if (not isinstance(result, dict) or set(result) != {"decisions", "changed_files", "tests_run"}
            or result.get("tests_run") is not False):
        raise StateError("Claude result does not match the expected schema")
    decisions, claimed = result["decisions"], result["changed_files"]
    if (not isinstance(decisions, list) or len(decisions) != len(feedback["findings"])
            or not isinstance(claimed, list) or not isinstance(actual_changed_paths, list)):
        raise StateError("Expected exactly one decision per finding and a changed-file array")
    expected = {finding["id"] for finding in feedback["findings"]}
    ids, selected = set(), set()
    for decision in decisions:
        if (not isinstance(decision, dict) or set(decision) != {"id", "decision", "reason"}
                or not isinstance(decision.get("id"), str) or decision["id"] not in expected
                or decision["id"] in ids
                or decision.get("decision") not in {"accepted", "rejected", "needs_review"}
                or not isinstance(decision.get("reason"), str) or not decision["reason"].strip()):
            raise StateError("Missing, duplicate or invalid finding decision")
        ids.add(decision["id"])
        selected.add(decision["decision"])
    if ids != expected:
        raise StateError("Decisions do not cover all findings")
    if (any(not path_allowed(path) for path in claimed + actual_changed_paths)
            or len(claimed) != len(set(claimed))
            or len(actual_changed_paths) != len(set(actual_changed_paths))
            or set(claimed) != set(actual_changed_paths)):
        raise StateError("Claimed changes differ from actual changes or include protected paths")
    if type(symlink_changed) is not bool or type(mode_changed) is not bool:
        raise StateError("Diff safety flags must be booleans")
    if symlink_changed or mode_changed:
        raise StateError("Symlink or file-mode changes require human review")
    if actual_changed_paths and "accepted" not in selected:
        raise StateError("Claude changed files without accepting a finding")
    if "needs_review" in selected or "rejected" in selected:
        outcome = "needs_review"
    elif "accepted" in selected:
        if not actual_changed_paths:
            raise StateError("Accepted findings have no actual fix")
        outcome = "ready_to_test"
    else:
        # Defensive fallback: absence of an accepted decision cannot demonstrate
        # agreement or authorize automatic completion.
        outcome = "needs_review"
    return {**deepcopy(result), "changed_files": sorted(actual_changed_paths), "outcome": outcome}
