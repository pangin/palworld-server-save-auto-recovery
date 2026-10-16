"""Snapshot review criteria from an immutable, trusted Git commit.

The caller chooses the trusted default-branch commit and mandatory documents.
Nothing here reads a document from the working tree, invokes a shell, fetches a
remote, or treats document contents as executable instructions. A snapshot is
complete or raises ``DocumentError``; oversized criteria are never truncated.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess


MAX_DOCUMENT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 200 * 1024
REVISION = re.compile(r"[0-9a-f]{40}\Z")
OBJECT_ID = re.compile(rb"[0-9a-f]{40}\Z")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


class DocumentError(ValueError):
    """Criteria could not be snapshotted completely; do not claim compliance."""


def _path(value: str, *, markdown: bool = False) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or "\\" in value or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise DocumentError("Expected a canonical relative document or changed-file path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise DocumentError("Paths must be valid UTF-8") from None
    if markdown and PurePosixPath(value).suffix.lower() != ".md":
        raise DocumentError("Configured review documents must be Markdown files")
    return value


def _git(checkout: Path, *args: str) -> bytes:
    """Read Git objects without inherited credentials or executable Git helpers."""
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise DocumentError("Git is unavailable")
    env = {
        "PATH": os.defpath,
        "HOME": str(checkout),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    command = [executable, "-c", f"core.hooksPath={os.devnull}",
               "-c", "core.fsmonitor=false", "-c", "core.pager=false",
               "-c", f"core.attributesFile={os.devnull}", *args]
    try:
        result = subprocess.run(command, cwd=checkout, env=env, capture_output=True,
                                timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise DocumentError("Unable to read the trusted Git commit") from None
    if result.returncode != 0:
        # Git diagnostics can include paths, URLs, or configuration values. Do
        # not surface raw stderr in workflow comments or model prompts.
        raise DocumentError("Unable to read the trusted Git commit")
    return result.stdout


def _tree(checkout: Path, revision: str) -> dict[str, tuple[bytes, bytes, bytes]]:
    if _git(checkout, "cat-file", "-t", revision).strip() != b"commit":
        raise DocumentError("Review document revision must identify a Git commit")
    entries = {}
    for record in _git(checkout, "ls-tree", "-r", "-z", "--full-tree", revision).split(b"\x00"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ")
            if not OBJECT_ID.fullmatch(object_id):
                raise ValueError
            # Unrelated files may have arbitrary Git filenames. Decode them
            # losslessly; only selected, valid UTF-8 paths are ever included.
            path = raw_path.decode("utf-8", errors="surrogateescape")
        except ValueError:
            raise DocumentError("Malformed trusted Git tree") from None
        if path in entries:
            raise DocumentError("Duplicate path in trusted Git tree")
        entries[path] = (mode, kind, object_id)
    return entries


def build_snapshot(checkout: Path, revision: str, document_paths: list[str],
                   changed_paths: list[str]) -> dict:
    """Return pinned criteria plus instruction files applicable to changed files.

    ``document_paths`` is a nonempty, unique list of mandatory Markdown files.
    Root AGENTS.md/CLAUDE.md are optional, as are instruction files in ancestor
    directories of ``changed_paths``. Instructions in unrelated directories are
    excluded. All selected files must be regular UTF-8 blobs in ``revision``.
    The fingerprint includes the commit, exact contents, paths, and file hashes.
    """
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise DocumentError("Expected an exact 40-character lowercase Git commit SHA")
    if not isinstance(document_paths, list) or not document_paths:
        raise DocumentError("At least one mandatory review document is required")
    configured = [_path(path, markdown=True) for path in document_paths]
    if len(set(configured)) != len(configured):
        raise DocumentError("Configured review document paths must be unique")
    if not isinstance(changed_paths, list):
        raise DocumentError("Changed-file paths must be an array")
    changed = [_path(path) for path in changed_paths]
    try:
        checkout = Path(checkout).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise DocumentError("Expected an existing Git checkout directory") from None
    if not checkout.is_dir():
        raise DocumentError("Expected an existing Git checkout directory")

    tree = _tree(checkout, revision)
    if any(path not in tree for path in configured):
        raise DocumentError("A mandatory review document is missing from the trusted commit")
    selected = set(configured)
    directories = {""}
    for path in changed:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    for directory in directories:
        for name in INSTRUCTION_FILES:
            candidate = f"{directory}/{name}" if directory else name
            if candidate in tree:
                selected.add(candidate)

    documents, total_bytes = [], 0
    for path in sorted(selected):
        mode, kind, object_id = tree[path]
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise DocumentError("Selected review documents must be regular Git files")
        oid = object_id.decode("ascii")
        try:
            size = int(_git(checkout, "cat-file", "-s", oid).strip())
        except ValueError:
            raise DocumentError("Invalid review document size") from None
        if size < 0 or size > MAX_DOCUMENT_BYTES:
            raise DocumentError("A review document exceeds the 64 KiB limit")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise DocumentError("Review documents exceed the combined 200 KiB limit")
        raw = _git(checkout, "cat-file", "blob", oid)
        if len(raw) != size:
            raise DocumentError("Review document size does not match its Git blob")
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise DocumentError("Selected review documents must contain valid UTF-8") from None
        documents.append({"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                          "content": content})

    snapshot = {"schema_version": 1, "revision": revision, "documents": documents}
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    snapshot["sha256"] = hashlib.sha256(canonical).hexdigest()
    return snapshot
