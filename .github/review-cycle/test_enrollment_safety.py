"""No network tests, complete criteria scope, and safe initial enrollment."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import cloud_controller as controller
import cloud_state as state
from test_cloud_state import config


def workflow_text():
    root = Path(__file__).resolve().parent
    path = root / "cloud-workflow.yml"
    if not path.is_file():
        path = root.parent / "workflows/codex-claude-cycle.yml"
    return path.read_text()


class CompleteChangedPathsTests(unittest.TestCase):
    def test_incomplete_files_ask_visibly_before_baseline_or_reservation(self):
        from test_cloud_controller import ControllerTests
        harness = ControllerTests(); harness.setUp(); self.addCleanup(harness.doCleanups)
        path, env, conversation = harness.protected_prepare_fixture("app.js")
        original_request = harness.api.request.side_effect
        def request(route, method="GET", payload=None):
            value = original_request(route, method, payload)
            return {**value, "changed_files": 3001} if route.endswith("/pulls/7") else value
        harness.api.request.side_effect = request
        with patch.dict(os.environ, env, clear=True), patch.object(controller, "pin_documents") as pin:
            controller.prepare(harness.root, harness.root / "event.json", path, harness.api)
        pin.assert_not_called()
        self.assertEqual(sum(item["body"].startswith(controller.human_input.QUESTION_MARKER) for item in conversation), 1)
        self.assertFalse((harness.root / "job.json").exists())
        _, ledger = controller.ledger_read(harness.api, "pangin/example", 7)
        self.assertEqual(ledger["pulls"]["pangin/example#7"]["attempts"], 0)

    def test_head_movement_during_files_fetch_never_posts_stale_question(self):
        from test_cloud_controller import ControllerTests
        harness = ControllerTests(); harness.setUp(); self.addCleanup(harness.doCleanups)
        path, env, conversation = harness.protected_prepare_fixture("app.js")
        original_pages = harness.api.pages.side_effect
        original_request = harness.api.request.side_effect
        moved = False
        def pages(route):
            nonlocal moved
            value = original_pages(route)
            if route.endswith("/files"):
                moved = True
            return value
        def request(route, method="GET", payload=None):
            value = original_request(route, method, payload)
            if moved and route.endswith("/pulls/7"):
                return {**value, "head": {**value["head"], "sha": "d" * 40}}
            return value
        harness.api.pages.side_effect = pages; harness.api.request.side_effect = request
        with patch.dict(os.environ, env, clear=True), self.assertRaises(state.StateError):
            controller.prepare(harness.root, harness.root / "event.json", path, harness.api)
        self.assertEqual(conversation, [])
        self.assertFalse((harness.root / "job.json").exists())

    def test_declared_count_must_match_and_fit_rest_cap(self):
        files = [{"filename": "app.py"}]
        for count in [None, True, -1, 0, 2, 3001]:
            with self.subTest(count=count), self.assertRaises(state.StateError):
                controller.changed_pr_paths({"changed_files": count}, files)
        self.assertEqual(controller.changed_pr_paths({"changed_files": 1}, files), ["app.py"])
        self.assertEqual(controller.changed_pr_paths({"changed_files": 0}, []), [])

    def test_capped_response_cannot_pin_partial_scoped_documents(self):
        files = [{"filename": f"src/{i}.py"} for i in range(3000)]
        with self.assertRaises(state.StateError):
            controller.changed_pr_paths({"changed_files": 3001}, files)
        self.assertEqual(len(controller.changed_pr_paths({"changed_files": 3000}, files)), 3000)

    def test_duplicate_and_noncanonical_paths_are_rejected(self):
        for files in [[{"filename": "a"}, {"filename": "a"}],
                      [{"filename": "../a"}], [{"filename": None}], [None],
                      [{"filename": "a", "previous_filename": "b"}]]:
            with self.subTest(files=files), self.assertRaises(state.StateError):
                controller.changed_pr_paths({"changed_files": len(files)}, files)

    def test_rename_pins_old_and_new_scoped_instructions(self):
        files = [{"filename": "new/app.py", "status": "renamed", "previous_filename": "old/app.py"}]
        self.assertEqual(controller.changed_pr_paths({"changed_files": 1}, files), ["new/app.py", "old/app.py"])
        for previous in [None, "../old.py", "/old.py"]:
            with self.subTest(previous=previous), self.assertRaises(state.StateError):
                controller.changed_pr_paths({"changed_files": 1}, [{**files[0], "previous_filename": previous}])


class TrustedEnrollmentBootstrapTests(unittest.TestCase):
    def bootstrap(self, *, present=False, complete=False):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            workspace = root / "workspace"; workspace.mkdir()
            runner = root / "runner"; runner.mkdir()
            output = root / "output"
            if present:
                control = workspace / ".github/review-cycle"; control.mkdir(parents=True)
                if complete:
                    for name in ["cloud_controller.py", "config.json", "git_askpass.py"]:
                        (control / name).write_text("fixture\n")
            step = workflow_text().split("      - name: Preserve trusted control code", 1)[1].split("\n      - name:", 1)[0]
            script = "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines() if line)
            result = subprocess.run(["bash", "-eu", "-c", script], cwd=workspace,
                                    env={"PATH": os.defpath, "RUNNER_TEMP": str(runner), "GITHUB_OUTPUT": str(output)},
                                    capture_output=True, text=True, timeout=10)
            return result.returncode, output.read_text() if output.exists() else "", (runner / "review-cycle").exists()

    def test_absent_trusted_enrollment_is_successful_noop(self):
        self.assertEqual(self.bootstrap(), (0, "enrolled=false\n", False))

    def test_partial_enrollment_fails_closed(self):
        code, output, copied = self.bootstrap(present=True)
        self.assertNotEqual(code, 0)
        self.assertNotIn("enrolled=true", output)
        self.assertFalse(copied)

    def test_existing_complete_enrollment_copies_trusted_controller(self):
        self.assertEqual(self.bootstrap(present=True, complete=True), (0, "enrolled=true\n", True))

    def test_all_entry_actions_require_trusted_enrollment(self):
        text = workflow_text()
        self.assertIn("if: steps.trusted.outputs.enrolled == 'true' && github.event_name == 'issue_comment'", text)
        self.assertIn("if: steps.trusted.outputs.enrolled == 'true' && github.event_name != 'issue_comment'", text)


class OfflineContainerTests(unittest.TestCase):
    def test_test_container_explicitly_disables_network_and_credentials(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch); workspace = root / "workspace"; workspace.mkdir()
            with patch.object(controller, "git", return_value=""), \
                 patch.object(controller.subprocess, "run", return_value=Mock(returncode=0)) as run:
                controller.test_container(workspace, config(), root)
            startup = next(call for call in run.call_args_list if call.args[0][:2] == ["docker", "run"])
            self.assertEqual(startup.args[0].count("--network=none"), 1)
            self.assertNotIn("--network=host", startup.args[0])
            self.assertNotIn("GH_TOKEN", startup.kwargs["env"])
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", startup.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
