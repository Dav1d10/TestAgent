import base64

import httpx
import pytest

from app.tools import github_client


class _FakeResponse:
    def __init__(self, json_data=None, text="", status_code=200):
        self._json = json_data or {}
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.github.com")
            raise httpx.HTTPStatusError(
                "error", request=request, response=httpx.Response(self.status_code, request=request)
            )


class TestFileExists:
    def test_returns_true_when_content_fetched(self, mocker):
        mocker.patch("app.tools.github_client.get_file_content", return_value="content")
        assert github_client.file_exists("owner/repo", "tests/test_x.py", "sha", "tok") is True

    def test_returns_false_on_404(self, mocker):
        request = httpx.Request("GET", "https://api.github.com")
        response = httpx.Response(404, request=request)
        mocker.patch(
            "app.tools.github_client.get_file_content",
            side_effect=httpx.HTTPStatusError("not found", request=request, response=response),
        )
        assert github_client.file_exists("owner/repo", "tests/test_x.py", "sha", "tok") is False

    def test_reraises_non_404_errors(self, mocker):
        request = httpx.Request("GET", "https://api.github.com")
        response = httpx.Response(500, request=request)
        mocker.patch(
            "app.tools.github_client.get_file_content",
            side_effect=httpx.HTTPStatusError("server error", request=request, response=response),
        )
        with pytest.raises(httpx.HTTPStatusError):
            github_client.file_exists("owner/repo", "tests/test_x.py", "sha", "tok")


class TestGetPushDiff:
    def test_uses_compare_endpoint_with_diff_media_type(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.get.return_value = _FakeResponse(text="diff --git a/x.py b/x.py")
        mocker.patch("httpx.Client").return_value.__enter__.return_value = mock_client

        result = github_client.get_push_diff("owner/repo", "base_sha", "head_sha", "tok")

        assert result == "diff --git a/x.py b/x.py"
        called_url = mock_client.get.call_args[0][0]
        assert "compare/base_sha...head_sha" in called_url
        called_headers = mock_client.get.call_args[1]["headers"]
        assert called_headers["Accept"] == "application/vnd.github.v3.diff"


class TestPostCommitComment:
    def test_posts_to_commit_comments_endpoint(self, mocker):
        mock_client = mocker.MagicMock()
        mock_client.post.return_value = _FakeResponse()
        mocker.patch("httpx.Client").return_value.__enter__.return_value = mock_client

        github_client.post_commit_comment("owner/repo", "sha123", "it failed", "tok")

        called_url = mock_client.post.call_args[0][0]
        called_json = mock_client.post.call_args[1]["json"]
        assert "commits/sha123/comments" in called_url
        assert called_json == {"body": "it failed"}


class TestCreatePr:
    def test_full_flow_creates_branch_commits_files_and_opens_pr(self, mocker):
        mock_client = mocker.MagicMock()

        def get_side_effect(url, **kwargs):
            if url.endswith("/repos/owner/repo"):
                return _FakeResponse(json_data={"default_branch": "main"})
            if "git/ref/heads/main" in url:
                return _FakeResponse(json_data={"object": {"sha": "base_sha"}})
            raise AssertionError(f"unexpected GET {url}")

        mock_client.get.side_effect = get_side_effect
        mock_client.post.side_effect = [
            _FakeResponse(),  # create ref
            _FakeResponse(json_data={"html_url": "https://github.com/owner/repo/pull/1"}),  # open PR
        ]
        mock_client.put.return_value = _FakeResponse()
        mocker.patch("httpx.Client").return_value.__enter__.return_value = mock_client

        pr_url = github_client.create_pr(
            repo="owner/repo",
            branch="test-agent/push/abc1234",
            title="TestAgent: generated tests",
            body="body",
            files=[("tests/test_x_add.py", "def test_add(): pass")],
            token="tok",
        )

        assert pr_url == "https://github.com/owner/repo/pull/1"

        create_ref_call = mock_client.post.call_args_list[0]
        assert create_ref_call.kwargs["json"] == {
            "ref": "refs/heads/test-agent/push/abc1234",
            "sha": "base_sha",
        }

        put_call = mock_client.put.call_args
        assert "contents/tests/test_x_add.py" in put_call[0][0]
        sent_content = base64.b64decode(put_call.kwargs["json"]["content"]).decode()
        assert sent_content == "def test_add(): pass"

        open_pr_call = mock_client.post.call_args_list[1]
        assert open_pr_call.kwargs["json"]["head"] == "test-agent/push/abc1234"
        assert open_pr_call.kwargs["json"]["base"] == "main"
