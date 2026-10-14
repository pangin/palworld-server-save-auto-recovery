"""Narrow local validation for the cycle's documented GitHub concurrency queue.

No diagnostics are globally ignored. Current official actionlint cannot parse
the queue property; accept only its exact single diagnostic after independently
checking our literal block. This is not proof of GitHub's runtime scheduling.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


EXPECTED_GROUP = (
    "codex-claude-cycle-${{ github.repository }}-"
    "${{ github.event.pull_request.number || github.event.issue.number || "
    "github.event.workflow_run.pull_requests[0].number }}"
)
QUEUE_DIAGNOSTIC = (
    'unexpected key "queue" for "concurrency" section. '
    'expected one of "cancel-in-progress", "group"'
)


def check_concurrency(text):
    # Accept only the finite static syntax we deploy; reject duplicate/unknown
    # keys and nested/folded values instead of guessing general YAML semantics.
    if text.count("\nconcurrency:\n") != 1:
        raise ValueError("Expected one workflow-level concurrency block")
    block = text.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or line.startswith("  #"):
            continue
        if not line.startswith("  ") or line.startswith("   ") or ": " not in line:
            raise ValueError("Unexpected concurrency syntax")
        name, value = line[2:].split(": ", 1)
        if name in fields:
            raise ValueError("Duplicate concurrency field")
        fields[name] = value
    if fields != {"group": EXPECTED_GROUP, "queue": "max", "cancel-in-progress": "false"}:
        raise ValueError("PR events must share a noncanceling bounded queue")


def check_lint_result(text, path, returncode, diagnostics):
    check_concurrency(text)
    if returncode == 0 and diagnostics == []:
        return "full_actionlint_pass"
    lines = text.splitlines()
    if lines.count("  queue: max") != 1:
        raise ValueError("Expected exactly one literal queue key")
    expected = {
        "message": QUEUE_DIAGNOSTIC,
        "filepath": str(path),
        "line": lines.index("  queue: max") + 1,
        "column": 3,
        "kind": "syntax-check",
        "snippet": "  queue: max\n  ^~~~~~",
        "end_column": 8,
    }
    if returncode != 1 or diagnostics != [expected]:
        raise ValueError("Unclassified actionlint failure; do not suppress it")
    return "queue_tool_compatibility_only"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    args = parser.parse_args()
    try:
        text = args.workflow.read_text()
        check_concurrency(text)
        result = subprocess.run(
            ["actionlint", "-format", "{{json .}}", str(args.workflow)],
            text=True, capture_output=True, timeout=30,
        )
        # Other stderr, missing output or malformed JSON must not become a pass.
        if result.stderr.strip():
            raise ValueError("Unexpected actionlint stderr")
        status = check_lint_result(text, args.workflow, result.returncode,
                                   json.loads(result.stdout))
        if status == "full_actionlint_pass":
            print("Literal concurrency contract and full actionlint passed.")
        else:
            print("Known actionlint queue-key compatibility gap only; all other diagnostics absent.")
            print("Literal queue contract passed; raw actionlint exited 1, GitHub runtime unverified.")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Workflow validation failed; inspect unfiltered actionlint diagnostics.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
