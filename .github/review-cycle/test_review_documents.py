import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import review_documents as d


class ReviewDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.write("README.md", "# Product\nRequired behavior.\n")
        self.write("AGENTS.md", "Root review rules.\n")
        self.write("CLAUDE.md", "Root implementation rules.\n")
        self.write("src/AGENTS.md", "Rules for all source.\n")
        self.write("src/feature/CLAUDE.md", "Feature acceptance criteria.\n")
        self.write("unrelated/AGENTS.md", "Unrelated instructions.\n")
        self.write("src/feature/code.py", "pass\n")
        self.revision = self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=self.root, check=True, capture_output=True).stdout

    def write(self, name, content):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode().strip()

    def snapshot(self, documents=None, changed=None, revision=None):
        return d.build_snapshot(self.root, revision or self.revision,
                                ["README.md"] if documents is None else documents,
                                ["src/feature/code.py"] if changed is None else changed)

    def test_includes_root_and_applicable_ancestor_instructions_only(self):
        snapshot = self.snapshot()
        self.assertEqual([doc["path"] for doc in snapshot["documents"]],
                         ["AGENTS.md", "CLAUDE.md", "README.md", "src/AGENTS.md",
                          "src/feature/CLAUDE.md"])
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["revision"], self.revision)
        self.assertRegex(snapshot["sha256"], r"^[0-9a-f]{64}$")

    def test_root_change_does_not_include_nested_instructions(self):
        self.assertEqual([doc["path"] for doc in self.snapshot(changed=["app.py"])["documents"]],
                         ["AGENTS.md", "CLAUDE.md", "README.md"])

    def test_working_tree_and_new_head_edits_cannot_change_pinned_criteria(self):
        before = self.snapshot()
        self.write("README.md", "Weakened criteria.\n")
        self.write("src/AGENTS.md", "Weaker instructions.\n")
        self.assertEqual(self.snapshot(), before)
        new_head = self.commit()
        self.assertEqual(self.snapshot(), before)
        self.assertNotEqual(self.snapshot(revision=new_head)["sha256"], before["sha256"])

    def test_fingerprint_includes_revision_even_when_documents_are_unchanged(self):
        before = self.snapshot()
        self.write("src/feature/code.py", "value = 2\n")
        after = self.snapshot(revision=self.commit())
        self.assertEqual(after["documents"], before["documents"])
        self.assertNotEqual(after["sha256"], before["sha256"])

    def test_local_replace_refs_cannot_change_pinned_commit_contents(self):
        before = self.snapshot()
        self.write("README.md", "Replacement criteria.\n")
        replacement = self.commit()
        self.git("replace", self.revision, replacement)
        self.assertEqual(self.snapshot(), before)

    def test_sorted_canonical_fingerprint_and_per_document_hashes(self):
        first = self.snapshot(documents=["src/AGENTS.md", "README.md"],
                              changed=["src/feature/code.py", "README.md"])
        second = self.snapshot(documents=["README.md", "src/AGENTS.md"],
                               changed=["README.md", "src/feature/code.py", "README.md"])
        self.assertEqual(first, second)
        canonical = json.dumps({key: value for key, value in first.items() if key != "sha256"},
                               sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(first["sha256"], hashlib.sha256(canonical).hexdigest())
        for document in first["documents"]:
            self.assertEqual(document["sha256"], hashlib.sha256(document["content"].encode()).hexdigest())

    def test_missing_optional_instructions_are_okay(self):
        for name in ["AGENTS.md", "CLAUDE.md", "src/AGENTS.md", "src/feature/CLAUDE.md"]:
            (self.root / name).unlink()
        snapshot = self.snapshot(revision=self.commit())
        self.assertEqual([doc["path"] for doc in snapshot["documents"]], ["README.md"])

    def test_missing_configured_document_and_non_markdown_rejected(self):
        for documents in [["missing.md"], ["src/feature/code.py"], [],
                          ["README.md", "README.md"], "README.md"]:
            with self.subTest(documents=documents), self.assertRaises(d.DocumentError):
                self.snapshot(documents=documents)

    def test_invalid_paths_and_revision_rejected_before_git(self):
        invalid = ["/README.md", "../README.md", "src/../README.md", "./README.md",
                   "src//README.md", "README.md/", "src\\README.md", "bad\x00.md", "\udcff.md", None]
        with patch.object(d, "_git") as git:
            for path in invalid:
                with self.subTest(path=repr(path)), self.assertRaises(d.DocumentError):
                    self.snapshot(documents=[path])
                with self.subTest(changed=repr(path)), self.assertRaises(d.DocumentError):
                    self.snapshot(changed=[path])
            for revision in ["HEAD", "a" * 39, "A" * 40, "g" * 40, None]:
                with self.subTest(revision=revision), self.assertRaises(d.DocumentError):
                    d.build_snapshot(self.root, revision, ["README.md"], [])
            git.assert_not_called()

    def test_changed_paths_must_be_list_but_empty_is_valid(self):
        with self.assertRaises(d.DocumentError): self.snapshot(changed="src/code.py")
        self.assertEqual([doc["path"] for doc in self.snapshot(changed=[])["documents"]],
                         ["AGENTS.md", "CLAUDE.md", "README.md"])

    def test_exact_sha_must_identify_commit_not_blob(self):
        blob = self.git("rev-parse", f"{self.revision}:README.md").decode().strip()
        with self.assertRaises(d.DocumentError): self.snapshot(revision=blob)
        with self.assertRaises(d.DocumentError): self.snapshot(revision="a" * 40)

    def test_selected_symlink_rejected_even_when_optional(self):
        (self.root / "AGENTS.md").unlink()
        (self.root / "AGENTS.md").symlink_to("README.md")
        with self.assertRaises(d.DocumentError): self.snapshot(revision=self.commit())

    def test_configured_symlink_and_gitlink_rejected(self):
        (self.root / "linked.md").symlink_to("README.md")
        revision = self.commit()
        with self.assertRaises(d.DocumentError): self.snapshot(documents=["linked.md"], revision=revision)
        self.git("update-index", "--add", "--cacheinfo", f"160000,{revision},module.md")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "gitlink fixture")
        sha = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaises(d.DocumentError): self.snapshot(documents=["module.md"], revision=sha)

    def test_executable_regular_markdown_allowed(self):
        (self.root / "README.md").chmod(0o755)
        snapshot = self.snapshot(revision=self.commit())
        self.assertIn("README.md", [doc["path"] for doc in snapshot["documents"]])

    def test_invalid_utf8_selected_document_rejected(self):
        (self.root / "README.md").write_bytes(b"bad\xff")
        with self.assertRaises(d.DocumentError): self.snapshot(revision=self.commit())

    def test_per_file_limit_accepts_boundary_and_rejects_overflow(self):
        self.write("README.md", "x" * d.MAX_DOCUMENT_BYTES)
        self.assertEqual(len(next(doc["content"] for doc in self.snapshot(revision=self.commit())["documents"]
                                  if doc["path"] == "README.md")), d.MAX_DOCUMENT_BYTES)
        self.write("README.md", "x" * (d.MAX_DOCUMENT_BYTES + 1))
        with self.assertRaises(d.DocumentError): self.snapshot(revision=self.commit())

    def test_total_limit_uses_utf8_bytes_not_characters(self):
        documents = [f"criteria/{number}.md" for number in range(4)]
        for name in documents: self.write(name, "가" * (d.MAX_DOCUMENT_BYTES // 3))
        with self.assertRaises(d.DocumentError): self.snapshot(documents=documents, revision=self.commit())

    def test_unrelated_invalid_utf8_content_is_not_read(self):
        (self.root / "unrelated/AGENTS.md").write_bytes(b"bad\xff")
        self.snapshot(revision=self.commit())

    def test_git_environment_drops_credentials_and_disables_helpers(self):
        actual = subprocess.run
        secrets = {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret", "PUBLISH_TOKEN": "secret",
                   "CLAUDE_CODE_OAUTH_TOKEN": "secret", "GIT_CONFIG_COUNT": "1",
                   "GIT_SSH_COMMAND": "evil-command", "GIT_DIR": "/bad/repository"}
        with patch.dict(os.environ, secrets), patch.object(d.subprocess, "run", wraps=actual) as run:
            self.snapshot()
        for call in run.call_args_list:
            self.assertFalse(set(secrets) & set(call.kwargs["env"]))
            self.assertIn("core.fsmonitor=false", call.args[0])
            self.assertIn("core.pager=false", call.args[0])
            self.assertIn(f"core.hooksPath={os.devnull}", call.args[0])
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(call.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(call.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_git_errors_and_timeouts_redact_stderr(self):
        for result in [Mock(returncode=1, stderr=b"token=do-not-leak", stdout=b""),
                       subprocess.TimeoutExpired(["git"], 30, stderr=b"token=do-not-leak")]:
            with self.subTest(result=result), patch.object(d.subprocess, "run") as run:
                if isinstance(result, Exception): run.side_effect = result
                else: run.return_value = result
                with self.assertRaises(d.DocumentError) as caught: self.snapshot()
                self.assertNotIn("do-not-leak", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
