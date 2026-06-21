import hashlib
import hmac
import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.webhooks.github_webhook import _extract_python_files

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
