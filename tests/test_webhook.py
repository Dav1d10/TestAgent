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
                                merged_test_code="MERGED_FILE"),
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_called_once()
        # The PR carries the module test file with the merged content — one file per module.
        assert create_pr_mock.call_args.kwargs["files"] == [("tests/test_utils.py", "MERGED_FILE")]
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
                _final("success", "def test_add_ok(): assert add() is None", merged_test_code="MERGED_ADD"),
                _final("failed_max_attempts", broken),
            ],
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        # PR is opened with only the green function's merged file...
        create_pr_mock.assert_called_once()
        assert create_pr_mock.call_args.kwargs["files"] == [("tests/test_utils.py", "MERGED_ADD")]
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
