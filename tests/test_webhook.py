import hashlib
import hmac
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from unidiff import PatchSet

from app.main import app
import app.webhooks.github_webhook as webhook_module
from app.webhooks.github_webhook import _extract_python_files, _process_push_event, _safe_module_name
from app.tools.code_parser import FunctionInfo
from app.tools.sandbox import SandboxResult

TEST_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")

client = TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post(payload: dict, event: str = "push", secret: str = TEST_SECRET, bad_sig: bool = False):
    """Send a signed webhook request. Pass bad_sig=True to send an invalid signature."""
    body = json.dumps(payload).encode()
    sig = "sha256=invalidsignature" if bad_sig else (
        "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    )
    return client.post(
        "/webhook/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": event,
        },
    )


PUSH_PAYLOAD = {
    "repository": {"full_name": "user/repo"},
    "before": "def456",
    "after": "abc123",
    "commits": [
        {
            "modified": ["app/utils.py"],
            "added": ["app/new_feature.py"],
        }
    ],
}


# ── Signature validation ──────────────────────────────────────────────────────

def test_invalid_signature_returns_403():
    response = _post(PUSH_PAYLOAD, bad_sig=True)
    assert response.status_code == 403


def test_missing_signature_returns_403():
    body = json.dumps(PUSH_PAYLOAD).encode()
    response = client.post(
        "/webhook/github",
        content=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
    )
    assert response.status_code == 403


def test_valid_signature_is_accepted():
    with patch("app.webhooks.github_webhook._process_push_event"):
        response = _post(PUSH_PAYLOAD)
    assert response.status_code == 200


# ── Event filtering ───────────────────────────────────────────────────────────

def test_non_push_event_is_ignored():
    payload = {"action": "opened"}
    response = _post(payload, event="pull_request")
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["event"] == "pull_request"


def test_ping_event_is_ignored():
    response = _post({"zen": "Keep it logically awesome."}, event="ping")
    assert response.json()["status"] == "ignored"


# ── Push event handling ───────────────────────────────────────────────────────

def test_push_with_python_files_is_accepted():
    with patch("app.webhooks.github_webhook._process_push_event"):
        response = _post(PUSH_PAYLOAD)
    assert response.json()["status"] == "accepted"
    assert response.json()["files_queued"] == 2


def test_push_without_python_files_returns_no_files():
    payload = {
        "repository": {"full_name": "user/repo"},
        "after": "abc123",
        "commits": [{"modified": ["README.md", "docs/guide.rst"], "added": []}],
    }
    response = _post(payload)
    assert response.json()["status"] == "no_python_files"



# ── _extract_python_files ─────────────────────────────────────────────────────

class TestExtractPythonFiles:
    def test_extracts_modified_and_added(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [{"modified": ["app/utils.py"], "added": ["app/new.py"]}],
        }
        files = _extract_python_files(payload)
        assert len(files) == 2
        paths = {f["path"] for f in files}
        assert paths == {"app/utils.py", "app/new.py"}

    def test_uses_after_sha_as_ref(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "deadbeef",
            "commits": [{"modified": ["app/utils.py"], "added": []}],
        }
        files = _extract_python_files(payload)
        assert all(f["ref"] == "deadbeef" for f in files)

    def test_skips_test_files(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [{"modified": ["tests/test_utils.py", "app/utils.py"], "added": []}],
        }
        files = _extract_python_files(payload)
        assert len(files) == 1
        assert files[0]["path"] == "app/utils.py"

    def test_skips_non_python_files(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [{"modified": ["README.md", "app/utils.py"], "added": []}],
        }
        files = _extract_python_files(payload)
        assert len(files) == 1

    def test_deduplicates_across_commits(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [
                {"modified": ["app/utils.py"], "added": []},
                {"modified": ["app/utils.py"], "added": []},
            ],
        }
        files = _extract_python_files(payload)
        assert len(files) == 1

    def test_includes_removed_python_file_with_final_status(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [
                {"modified": ["app/removed.py"], "added": [], "removed": []},
                {"modified": [], "added": [], "removed": ["app/removed.py"]},
            ],
        }
        files = _extract_python_files(payload)
        assert files == [
            {
                "repo": "user/repo",
                "path": "app/removed.py",
                "ref": "abc123",
                "deleted": True,
            }
        ]

    def test_empty_commits_returns_empty(self):
        payload = {
            "repository": {"full_name": "user/repo"},
            "after": "abc123",
            "commits": [],
        }
        assert _extract_python_files(payload) == []


# ── _safe_module_name ───────────────────────────────────────────────────────────

class TestSafeModuleName:
    def test_renames_stdlib_collision(self):
        assert _safe_module_name("collections") == "collections_module"

    def test_renames_another_stdlib_collision(self):
        assert _safe_module_name("string") == "string_module"

    def test_leaves_non_colliding_name_untouched(self):
        assert _safe_module_name("math_utils") == "math_utils"

    def test_does_not_collide_on_near_miss(self):
        # "strings" (plural) is not a stdlib module — only "string" (singular) is.
        assert _safe_module_name("strings") == "strings"


# ── _process_push_event (Stage 4 orchestration) ────────────────────────────────

UTILS_DIFF = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    # comment
     return a + b
"""

FILES = [{"repo": "user/repo", "path": "app/utils.py", "ref": "abc123"}]


def _fn(name):
    return FunctionInfo(
        name=name, source=f"def {name}(): pass", file_path="app/utils.py", line_start=1, line_end=2
    )


def _final(status, test_code, merged_test_code=None, max_attempts=3):
    return {
        "final_status": status,
        "test_code": test_code,
        "merged_test_code": merged_test_code if merged_test_code is not None else test_code,
        "attempt_count": 3,
        "max_attempts": max_attempts,
    }


class TestProcessPushEvent:
    def _mocks(self, mocker, functions):
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=UTILS_DIFF)
        mocker.patch("app.webhooks.github_webhook.get_file_content", return_value="def add(): pass\n")
        # No existing module test file (baseline is empty).
        mocker.patch("app.webhooks.github_webhook.get_file_content_or_empty", return_value="")
        mocker.patch("app.webhooks.github_webhook.extract_modified_functions", return_value=functions)
        return (
            mocker.patch("app.webhooks.github_webhook.create_pr"),
            mocker.patch("app.webhooks.github_webhook.post_commit_comment"),
        )

    def test_success_opens_a_pr_with_merged_file(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, [_fn("add")])
        mocker.patch.object(
            webhook_module._agent,
            "invoke",
            return_value=_final("success", "def test_add_ok(): assert add() is None",
                                merged_test_code="def test_add_ok():\n    assert add() is None\n"),
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_called_once()
        # The PR carries the module test file with the merged content — one file per module.
        assert create_pr_mock.call_args.kwargs["files"] == [
            ("tests/test_utils.py", "def test_add_ok():\n    assert add() is None\n")
        ]
        post_comment_mock.assert_not_called()

    def test_broken_function_posts_bug_comment_not_pr(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, [_fn("add")])
        failing = "def test_add_ok():\n    # BUG: add() returned 3 but contract requires None\n    assert add() is None"
        mocker.patch.object(webhook_module._agent, "invoke",
                            return_value=_final("failed_max_attempts", failing))

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        post_comment_mock.assert_called_once()
        body = post_comment_mock.call_args.kwargs["body"]
        assert "appears to have a bug" in body
        assert "# BUG" in body  # the failing test (with its BUG line) is included

    def test_unconverged_function_posts_plain_comment(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, [_fn("add")])
        failing = "def test_add_ok():\n    assert add() == 42"  # no # BUG marker
        mocker.patch.object(webhook_module._agent, "invoke",
                            return_value=_final("failed_max_attempts", failing))

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        post_comment_mock.assert_called_once()
        body = post_comment_mock.call_args.kwargs["body"]
        assert "could not generate a passing test" in body
        assert "appears to have a bug" not in body

    def test_mixed_push_opens_pr_and_posts_comment(self, mocker):
        # add succeeds, is_prime is broken — both in the same module.
        create_pr_mock, post_comment_mock = self._mocks(mocker, [_fn("add"), _fn("is_prime")])
        broken = "def test_is_prime_sq():\n    # BUG: is_prime(9) returned True\n    assert is_prime(9) is False"
        mocker.patch.object(
            webhook_module._agent,
            "invoke",
            side_effect=[
                _final(
                    "success",
                    "def test_add_ok(): assert add() is None",
                    merged_test_code="def test_add_ok():\n    assert add() is None\n",
                ),
                _final("failed_max_attempts", broken),
            ],
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        # PR is opened with only the green function's merged file...
        create_pr_mock.assert_called_once()
        assert create_pr_mock.call_args.kwargs["files"] == [
            ("tests/test_utils.py", "def test_add_ok():\n    assert add() is None\n")
        ]
        # ...and the broken function gets its own commit comment.
        post_comment_mock.assert_called_once()
        assert "is_prime" in post_comment_mock.call_args.kwargs["body"]

    def test_no_modified_functions_does_nothing(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, [])
        agent_invoke = mocker.patch.object(webhook_module._agent, "invoke")

        _process_push_event(PUSH_PAYLOAD, FILES)

        agent_invoke.assert_not_called()
        create_pr_mock.assert_not_called()
        post_comment_mock.assert_not_called()

    def _deletion_mocks(self, mocker, old_source, new_source, baseline, diff_text):
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=diff_text)
        mocker.patch("app.webhooks.github_webhook.get_file_content", return_value=new_source)
        mocker.patch(
            "app.webhooks.github_webhook.get_file_content_or_empty",
            side_effect=[old_source, baseline],
        )
        mocker.patch("app.webhooks.github_webhook.extract_modified_functions", return_value=[])
        sandbox = mocker.patch(
            "app.webhooks.github_webhook.run_test_in_sandbox",
            return_value=SandboxResult(True, "1 passed", "", 0),
        )
        return (
            mocker.patch("app.webhooks.github_webhook.create_pr"),
            mocker.patch("app.webhooks.github_webhook.post_commit_comment"),
            sandbox,
        )

    def test_deleted_function_removes_owned_tests_and_import(self, mocker):
        old_source = "def removed():\n    return 1\n\ndef current():\n    return 2\n"
        new_source = "def current():\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,5 +1,2 @@
-def removed():
-    return 1
-
 def current():
     return 2
"""
        baseline = (
            "from utils import removed\n"
            "from utils import current\n\n"
            "def test_removed_basic():\n    assert removed() == 1\n\n"
            "def test_current_basic():\n    assert current() == 2\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )
        agent_invoke = mocker.patch.object(webhook_module._agent, "invoke")

        _process_push_event(PUSH_PAYLOAD, FILES)

        agent_invoke.assert_not_called()
        sandbox.assert_called_once()
        cleaned = create_pr_mock.call_args.kwargs["files"][0][1]
        assert "removed" not in cleaned
        assert "test_current_basic" in cleaned
        assert create_pr_mock.call_args.kwargs["deleted_files"] == []
        post_comment_mock.assert_not_called()

    def test_reconciles_orphan_left_by_missed_deletion_webhook(self, mocker):
        source = "def current():\n    # touched later\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +1,3 @@
 def current():
+    # touched later
     return 2
"""
        baseline = (
            "from utils import removed\n"
            "from utils import current\n\n"
            "def test_removed_basic():\n    assert removed() == 1\n\n"
            "def test_current_basic():\n    assert current() == 2\n"
        )
        create_pr_mock, _, _ = self._deletion_mocks(
            mocker, source, source, baseline, diff_text
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        cleaned = create_pr_mock.call_args.kwargs["files"][0][1]
        assert "removed" not in cleaned
        assert "test_current_basic" in cleaned

    def test_deleting_last_owned_tests_deletes_test_file(self, mocker):
        old_source = "def removed():\n    return 1\n"
        new_source = ""
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +0,0 @@
-def removed():
-    return 1
"""
        baseline = "from utils import removed\n\ndef test_removed_basic():\n    assert removed() == 1\n"
        create_pr_mock, _, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        sandbox.assert_not_called()
        assert create_pr_mock.call_args.kwargs["files"] == []
        assert create_pr_mock.call_args.kwargs["deleted_files"] == ["tests/test_utils.py"]

    def test_deleted_source_file_deletes_module_test_file(self, mocker):
        diff_text = """--- a/app/utils.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def add():
-    return 1
"""
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=diff_text)
        get_content = mocker.patch("app.webhooks.github_webhook.get_file_content")
        mocker.patch(
            "app.webhooks.github_webhook.get_file_content_or_empty",
            side_effect=[
                "from utils import add\n\ndef test_add():\n    assert add() == 1\n",
                "def add():\n    return 1\n",
            ],
        )
        create_pr_mock = mocker.patch("app.webhooks.github_webhook.create_pr")
        mocker.patch("app.webhooks.github_webhook.post_commit_comment")
        deleted_files = [
            {
                "repo": "user/repo",
                "path": "app/utils.py",
                "ref": "abc123",
                "deleted": True,
            }
        ]

        _process_push_event(PUSH_PAYLOAD, deleted_files)

        get_content.assert_not_called()
        assert create_pr_mock.call_args.kwargs["files"] == []
        assert create_pr_mock.call_args.kwargs["deleted_files"] == ["tests/test_utils.py"]

    def test_cleanup_validation_failure_posts_specific_comment(self, mocker):
        old_source = "def removed():\n    return 1\n\ndef current():\n    return 2\n"
        new_source = "def current():\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,5 +1,2 @@
-def removed():
-    return 1
-
 def current():
     return 2
"""
        baseline = (
            "from utils import removed\n"
            "def test_removed():\n    assert removed() == 1\n\n"
            "def test_unrelated():\n    assert False\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )
        sandbox.return_value = SandboxResult(False, "1 failed", "", 1)

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        assert "could not safely remove obsolete tests" in post_comment_mock.call_args.kwargs["body"]

    def test_human_alias_dependency_preserves_file_and_posts_comment(self, mocker):
        old_source = "def removed():\n    return 1\n\ndef current():\n    return 2\n"
        new_source = "def current():\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,5 +1,2 @@
-def removed():
-    return 1
-
 def current():
     return 2
"""
        baseline = (
            "from utils import removed as operation\n\n"
            "def test_custom_contract():\n    assert operation() == 1\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )
        sandbox.return_value = SandboxResult(
            False,
            "ImportError: cannot import name 'removed'",
            "",
            2,
        )
        agent_invoke = mocker.patch.object(webhook_module._agent, "invoke")

        _process_push_event(PUSH_PAYLOAD, FILES)

        agent_invoke.assert_not_called()
        create_pr_mock.assert_not_called()
        sandbox.assert_called_once_with(
            source_code=new_source,
            test_code=baseline,
            module_name="utils",
        )
        body = post_comment_mock.call_args.kwargs["body"]
        assert "human/unknown tests still depend on deleted code" in body
        assert "`removed`" in body
        assert "`test_custom_contract`" in body
        assert "ImportError" in body
        assert "No cleanup PR was opened" in body

    def test_one_human_dependency_blocks_mixed_owned_cleanup(self, mocker):
        old_source = "def removed():\n    return 1\n\ndef current():\n    return 2\n"
        new_source = "def current():\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,5 +1,2 @@
-def removed():
-    return 1
-
 def current():
     return 2
"""
        baseline = (
            "from utils import removed as operation\n\n"
            "def test_removed_basic():\n    assert operation() == 1\n\n"
            "def test_custom_contract():\n    assert operation() == 1\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )
        sandbox.return_value = SandboxResult(False, "collection failed", "", 2)

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        # The convention-owned block is also preserved: partial cleanup would
        # still leave a broken file and would obscure the manual intervention.
        assert sandbox.call_args.kwargs["test_code"] == baseline
        body = post_comment_mock.call_args.kwargs["body"]
        assert "`test_custom_contract`" in body
        assert "`test_removed_basic`" not in body

    def test_missed_webhook_human_dependency_is_reconciled_on_later_touch(self, mocker):
        source = "def current():\n    # touched later\n    return 2\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +1,3 @@
 def current():
+    # touched later
     return 2
"""
        baseline = (
            "from utils import removed as operation\n\n"
            "def test_custom_contract():\n    assert operation() == 1\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, source, source, baseline, diff_text
        )
        sandbox.return_value = SandboxResult(False, "stale import", "", 2)

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        sandbox.assert_called_once()
        body = post_comment_mock.call_args.kwargs["body"]
        assert "`removed`" in body
        assert "`test_custom_contract`" in body

    def test_preserved_dependency_warns_even_if_unchanged_file_passes(self, mocker):
        old_source = "def removed():\n    return 1\n"
        new_source = ""
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +0,0 @@
-def removed():
-    return 1
"""
        baseline = (
            "import utils\n\n"
            "def test_custom_contract():\n"
            "    if False:\n"
            "        utils.removed()\n"
            "    assert True\n"
        )
        create_pr_mock, post_comment_mock, sandbox = self._deletion_mocks(
            mocker, old_source, new_source, baseline, diff_text
        )
        sandbox.return_value = SandboxResult(True, "1 passed", "", 0)

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        sandbox.assert_called_once()
        body = post_comment_mock.call_args.kwargs["body"]
        assert "Unchanged-file sandbox validation: PASSED" in body
        assert "Manual review is required" in body

    def test_deleted_source_file_with_human_dependency_is_not_deleted(self, mocker):
        old_source = "def removed():\n    return 1\n"
        baseline = (
            "from utils import removed as operation\n\n"
            "def test_custom_contract():\n    assert operation() == 1\n"
        )
        diff_text = """--- a/app/utils.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def removed():
-    return 1
"""
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=diff_text)
        mocker.patch(
            "app.webhooks.github_webhook.get_file_content_or_empty",
            side_effect=[baseline, old_source],
        )
        create_pr_mock = mocker.patch("app.webhooks.github_webhook.create_pr")
        post_comment_mock = mocker.patch("app.webhooks.github_webhook.post_commit_comment")
        sandbox = mocker.patch(
            "app.webhooks.github_webhook.run_test_in_sandbox",
            return_value=SandboxResult(False, "ImportError", "", 2),
        )
        deleted_files = [
            {
                "repo": "user/repo",
                "path": "app/utils.py",
                "ref": "abc123",
                "deleted": True,
            }
        ]

        _process_push_event(PUSH_PAYLOAD, deleted_files)

        create_pr_mock.assert_not_called()
        sandbox.assert_called_once_with(
            source_code="",
            test_code=baseline,
            module_name="utils",
        )
        body = post_comment_mock.call_args.kwargs["body"]
        assert "`test_custom_contract`" in body
        assert "human/unknown" in body

    def test_rename_removes_old_tests_and_generates_new_block_in_one_pr(self, mocker):
        old_source = "def old_name():\n    return 1\n"
        new_source = "def new_name():\n    return 1\n"
        diff_text = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +1,2 @@
-def old_name():
+def new_name():
     return 1
"""
        baseline = (
            "from utils import old_name\n\n"
            "def test_old_name_basic():\n    assert old_name() == 1\n"
        )
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=diff_text)
        mocker.patch("app.webhooks.github_webhook.get_file_content", return_value=new_source)
        mocker.patch(
            "app.webhooks.github_webhook.get_file_content_or_empty",
            side_effect=[old_source, baseline],
        )
        mocker.patch(
            "app.webhooks.github_webhook.extract_modified_functions",
            return_value=[_fn("new_name")],
        )
        create_pr_mock = mocker.patch("app.webhooks.github_webhook.create_pr")
        post_comment_mock = mocker.patch("app.webhooks.github_webhook.post_commit_comment")
        mocker.patch.object(
            webhook_module._agent,
            "invoke",
            return_value=_final(
                "success",
                "from utils import new_name\n\ndef test_new_name_basic():\n    assert new_name() == 1\n",
                merged_test_code=(
                    "from utils import new_name\n\n"
                    "def test_new_name_basic():\n    assert new_name() == 1\n"
                ),
            ),
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        content = create_pr_mock.call_args.kwargs["files"][0][1]
        assert "old_name" not in content
        assert "test_new_name_basic" in content
        assert create_pr_mock.call_args.kwargs["deleted_files"] == []
        post_comment_mock.assert_not_called()
