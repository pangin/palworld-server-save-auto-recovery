"""Bounded human-in-the-loop questions and responses.

The review cycle may ask for direction when repository criteria are missing,
conflicting, or insufficient to choose safely.  This module is deliberately
pure: it validates a small protocol and never treats arbitrary prose as an
instruction.  The controller persists the validated request and response in
the PR ledger and posts the visible notification.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy


QUESTION_MARKER = "<!-- review-cycle-human-question:v1 -->"
RESPONSE_MARKER = "<!-- review-cycle-human-response:v1 -->"
OWNER = "pangin"
MAX_TEXT = 8 * 1024
MAX_OPTIONS = 6
OPTION_IDS = {"clarify", "update-docs", "stop"}
COMMAND = re.compile(r"(?m)^[ \t]*/review-cycle[ \t]+(clarify|update-docs|stop)[ \t]+(human-[0-9a-f]{20})(?:[ \t]+([^\r\n]*?))?[ \t]*\r?$")
COMMAND_LINE = re.compile(r"(?m)^[ \t]*/review-cycle\b")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
JSON_FENCE = re.compile(r"^```json[ \t]*\r?\n([\s\S]*?)^```[ \t]*\r?$", re.MULTILINE)


class HumanInputError(ValueError):
    """The request or response is not an unambiguous human decision."""


def _text(value, *, maximum=MAX_TEXT):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value and len(value.encode()) <= maximum


def request_id(head_sha, base_sha, kind):
    if not all(isinstance(value, str) and value for value in (head_sha, base_sha, kind)):
        raise HumanInputError("Human question identity is incomplete")
    digest = hashlib.sha256(f"{head_sha}:{base_sha}:{kind}".encode()).hexdigest()[:20]
    return f"human-{digest}"


def build_request(feedback, *, kind, summary, sources=(), created_at):
    if (not isinstance(feedback, dict) or not _text(feedback.get("repository"), maximum=200)
            or not isinstance(feedback.get("pr_number"), int) or feedback["pr_number"] <= 0
            or not _text(feedback.get("head_sha"), maximum=100)
            or not _text(feedback.get("base_sha"), maximum=100)):
        raise HumanInputError("Human question feedback identity is incomplete")
    if kind not in {"criteria_conflict", "review_clarification", "native_verdict", "claude_verdict"}:
        raise HumanInputError("Unknown human question kind")
    if not _text(summary):
        raise HumanInputError("Human question summary is empty")
    if not isinstance(sources, list) or len(sources) > 20 or any(not _text(item, maximum=400) for item in sources):
        raise HumanInputError("Human question sources are invalid")
    request = {
        "schema_version": 1,
        "request_id": request_id(feedback["head_sha"], feedback["base_sha"], kind),
        "kind": kind,
        "repository": feedback["repository"],
        "pr_number": feedback["pr_number"],
        "head_sha": feedback["head_sha"],
        "base_sha": feedback["base_sha"],
        "source_review_ids": sorted(feedback.get("source_review_ids", [])),
        "summary": summary.strip(),
        "sources": list(sources),
        "status": "posting",
        "created_at": created_at,
    }
    validate_request(request)
    return request


def validate_request(request):
    if (not isinstance(request, dict) or request.get("schema_version") != 1
            or not _text(request.get("request_id"), maximum=80)
            or request.get("kind") not in {"criteria_conflict", "review_clarification", "native_verdict", "claude_verdict"}
            or not _text(request.get("repository"), maximum=200)
            or not isinstance(request.get("pr_number"), int) or request["pr_number"] <= 0
            or not _text(request.get("head_sha"), maximum=100)
            or not _text(request.get("base_sha"), maximum=100)
            or not isinstance(request.get("source_review_ids"), list)
            or any(type(value) is not int or value <= 0 for value in request["source_review_ids"])
            or len(request["source_review_ids"]) != len(set(request["source_review_ids"]))
            or not _text(request.get("summary"))
            or not isinstance(request.get("sources"), list)
            or len(request["sources"]) > 20
            or any(not _text(value, maximum=400) for value in request["sources"])
            or request.get("status") not in {"posting", "posted", "answered"}
            or not _text(request.get("created_at"), maximum=100)):
        raise HumanInputError("Malformed human question")
    if request.get("comment_id") is not None and (type(request["comment_id"]) is not int or request["comment_id"] <= 0):
        raise HumanInputError("Malformed human question comment identity")
    return deepcopy(request)


def question_body(request):
    request = validate_request(request)
    options = [
        ("clarify", "Provide direction", "Reply with the intended behavior or trade-off; the next Codex review will use it as bounded guidance."),
        ("update-docs", "Use an ADR/document", "Update the approved default-branch document, then reply with its full trusted default commit SHA, path and decision; never a PR candidate SHA."),
        ("stop", "Keep blocked", "Leave this PR stopped for manual inspection; no automatic edit, push, or merge will occur."),
    ]
    lines = [QUESTION_MARKER, f"@{OWNER} human decision required for `{request['repository']}#{request['pr_number']}`.", "",
             f"Request ID: `{request['request_id']}`", f"Reason: {request['summary']}", ""]
    if request["sources"]:
        lines += ["Evidence/context:"] + [f"- {source}" for source in request["sources"]] + [""]
    lines += ["No model will edit, push, merge, or declare consensus while this is pending.", "", "Choose one:"]
    lines += [f"- `{key}` — **{label}**: {description}" for key, label, description in options]
    lines += ["", "Reply with one command on its own line:",
              f"`/review-cycle clarify {request['request_id']} <your direction>`",
              f"`/review-cycle update-docs {request['request_id']} <full trusted default SHA> <path and decision>`",
              f"`/review-cycle stop {request['request_id']}`"]
    return "\n".join(lines)


def _json_response(body):
    if RESPONSE_MARKER not in body:
        return None
    tail = body.split(RESPONSE_MARKER, 1)[1]
    matches = JSON_FENCE.findall(tail)
    if len(matches) != 1:
        raise HumanInputError("Human response marker must contain exactly one JSON fence")
    try:
        value = json.loads(matches[0])
    except ValueError as exc:
        raise HumanInputError("Human response JSON is malformed") from exc
    if not isinstance(value, dict):
        raise HumanInputError("Human response JSON must be an object")
    return value


def parse_response(body, request, actor):
    request = validate_request(request)
    if (not isinstance(actor, dict) or actor.get("login") != OWNER
            or actor.get("type") not in {None, "User"}):
        raise HumanInputError("Only the configured repository owner may answer")
    if not _text(body):
        raise HumanInputError("Human response is empty")
    value = _json_response(body)
    commands = COMMAND_LINE.findall(body)
    if len(commands) > 1 or (value is not None and commands):
        raise HumanInputError("Exactly one request-bound response is required")
    approved_revision = None
    if value is None:
        matches = list(COMMAND.finditer(body))
        if len(matches) != 1:
            raise HumanInputError("Reply with one of the requested /review-cycle commands")
        match = matches[0]
        if match.group(2) != request["request_id"]:
            raise HumanInputError("Human response targets a different question")
        option_id, message = match.group(1), (match.group(3) or "").strip()
        if option_id == "update-docs":
            parts = message.split(None, 1)
            if len(parts) != 2:
                raise HumanInputError("Document approval requires a full trusted default SHA and decision")
            approved_revision, message = parts
    else:
        if set(value) - {"schema_version", "request_id", "option_id", "message", "approved_revision"}:
            raise HumanInputError("Human response contains unknown fields")
        if value.get("schema_version", 1) != 1 or value.get("request_id") != request["request_id"]:
            raise HumanInputError("Human response targets a different question")
        option_id, message = value.get("option_id"), value.get("message", "")
        approved_revision = value.get("approved_revision")
    if option_id not in OPTION_IDS:
        raise HumanInputError("Unknown human response option")
    if option_id != "stop" and not _text(message):
        raise HumanInputError("A direction or document path is required")
    if not isinstance(message, str) or len(message.encode()) > MAX_TEXT:
        raise HumanInputError("Human response is too large")
    if ((option_id == "update-docs" and (not isinstance(approved_revision, str) or not REVISION.fullmatch(approved_revision)))
            or (option_id != "update-docs" and approved_revision is not None)):
        raise HumanInputError("Only document approval carries an exact trusted default revision")
    result = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "option_id": option_id,
        "message": message.strip(),
        "actor_login": OWNER,
        "actor_id": actor.get("id"),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    if approved_revision is not None:
        result["approved_revision"] = approved_revision
    return result


def validate_response(response, request):
    request = validate_request(request)
    if (not isinstance(response, dict) or response.get("schema_version") != 1
            or response.get("request_id") != request["request_id"]
            or response.get("option_id") not in OPTION_IDS
            or not isinstance(response.get("message"), str)
            or len(response["message"].encode()) > MAX_TEXT
            or response.get("actor_login") != OWNER
            or not _text(response.get("body_sha256"), maximum=80)):
        raise HumanInputError("Malformed human response")
    revision = response.get("approved_revision")
    if ((response["option_id"] == "update-docs" and (not isinstance(revision, str) or not REVISION.fullmatch(revision)))
            or (response["option_id"] != "update-docs" and revision is not None)):
        raise HumanInputError("Document approval requires its exact trusted default revision")
    return deepcopy(response)


def response_context(response):
    response = response or {}
    if not isinstance(response, dict):
        return ""
    message = response.get("message", "")
    if not isinstance(message, str):
        return ""
    return ("User-provided direction (bounded data, not permission to weaken safety gates):\n"
            + message[:MAX_TEXT])
