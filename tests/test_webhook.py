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
from app.webhooks.github_webhook import _extract_python_files, _process_push_event
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


# ── _process_push_event (Stage 4 orchestration) ────────────────────────────────

UTILS_DIFF = """--- a/app/utils.py
+++ b/app/utils.py
@@ -1,2 +1,3 @@
 def add(a, b):
+    # comment
     return a + b
"""

FILES = [{"repo": "user/repo", "path": "app/utils.py", "ref": "abc123"}]
ONE_FUNCTION = [FunctionInfo(name="add", source="def add(a, b):\n    return a + b", file_path="app/utils.py", line_start=1, line_end=2)]


class TestProcessPushEvent:
    def _mocks(self, mocker, functions=ONE_FUNCTION, file_exists=False):
        mocker.patch("app.webhooks.github_webhook.get_push_diff", return_value=UTILS_DIFF)
        mocker.patch("app.webhooks.github_webhook.get_file_content", return_value="def add(a, b):\n    return a + b\n")
        mocker.patch("app.webhooks.github_webhook.extract_modified_functions", return_value=functions)
        mocker.patch("app.webhooks.github_webhook.file_exists", return_value=file_exists)
        return (
            mocker.patch("app.webhooks.github_webhook.create_pr"),
            mocker.patch("app.webhooks.github_webhook.post_commit_comment"),
        )

    def test_success_opens_a_pr_with_generated_test(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker)
        mocker.patch.object(
            webhook_module._agent,
            "invoke",
            return_value={
                "final_status": "success",
                "test_code": "def test_add(): assert add(1, 2) == 3",
                "attempt_count": 1,
                "max_attempts": 3,
            },
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_called_once()
        assert create_pr_mock.call_args.kwargs["files"] == [
            ("tests/test_utils_add.py", "def test_add(): assert add(1, 2) == 3")
        ]
        post_comment_mock.assert_not_called()

    def test_failure_posts_a_commit_comment_not_a_pr(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker)
        mocker.patch.object(
            webhook_module._agent,
            "invoke",
            return_value={
                "final_status": "failed_max_attempts",
                "test_code": "def test_add(): assert add(1, 2) == 99",
                "attempt_count": 3,
                "max_attempts": 3,
            },
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        create_pr_mock.assert_not_called()
        post_comment_mock.assert_called_once()
        assert "utils::add" in post_comment_mock.call_args.kwargs["body"]

    def test_skips_function_when_test_file_already_exists(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, file_exists=True)
        agent_invoke = mocker.patch.object(
            webhook_module._agent, "invoke"
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        agent_invoke.assert_not_called()
        create_pr_mock.assert_not_called()
        post_comment_mock.assert_not_called()

    def test_no_modified_functions_does_nothing(self, mocker):
        create_pr_mock, post_comment_mock = self._mocks(mocker, functions=[])
        agent_invoke = mocker.patch.object(
            webhook_module._agent, "invoke"
        )

        _process_push_event(PUSH_PAYLOAD, FILES)

        agent_invoke.assert_not_called()
        create_pr_mock.assert_not_called()
        post_comment_mock.assert_not_called()
