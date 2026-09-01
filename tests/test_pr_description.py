import pytest

from app.tools.pr_description import (
    PullRequestAction,
    PullRequestChange,
    build_pull_request_body,
    build_pull_request_title,
)


def _change(
    action: PullRequestAction,
    path: str = "tests/test_utils.py",
    functions: tuple[str, ...] = ("calculate",),
    reason: str = "Source function was modified",
) -> PullRequestChange:
    return PullRequestChange(action, path, functions, reason)


@pytest.mark.parametrize(
    ("action", "operation"),
    [
        (PullRequestAction.CREATED, "add tests"),
        (PullRequestAction.UPDATED, "update tests"),
        (PullRequestAction.CLEANED, "remove obsolete tests"),
    ],
)
def test_title_describes_a_single_action(action, operation):
    assert build_pull_request_title([_change(action)], "abcdef123") == (
        f"TestAgent: {operation} for push abcdef1"
    )


def test_deleted_title_uses_singular_or_plural_file_count():
    one = [_change(PullRequestAction.DELETED)]
    two = one + [
        _change(PullRequestAction.DELETED, path="tests/test_other.py")
    ]

    assert build_pull_request_title(one, "abcdef123") == (
        "TestAgent: delete obsolete test file for push abcdef1"
    )
    assert build_pull_request_title(two, "abcdef123") == (
        "TestAgent: delete obsolete test files for push abcdef1"
    )


def test_mixed_actions_use_reconcile_title():
    changes = [
        _change(PullRequestAction.CLEANED, functions=("old_name",)),
        _change(PullRequestAction.UPDATED, functions=("new_name",)),
    ]

    assert build_pull_request_title(changes, "abcdef123") == (
        "TestAgent: reconcile tests for push abcdef1"
    )


def test_body_contains_summary_grouped_changes_and_trigger_without_validation():
    changes = [
        _change(
            PullRequestAction.DELETED,
            functions=("prefix_value",),
            reason="All remaining tests were orphaned",
        ),
        _change(
            PullRequestAction.DELETED,
            functions=("another_function",),
            reason="All remaining tests were orphaned",
        ),
    ]

    body = build_pull_request_body(changes, "368dfc5abcdef")

    assert "## Summary" in body
    assert "## Changes" in body
    assert "## Trigger" in body
    assert "## Validation" not in body
    assert "Removed an obsolete generated test file" in body
    assert body.count("| Deleted | `tests/test_utils.py`") == 1
    assert "`another_function`, `prefix_value`" in body
    assert "All remaining tests were orphaned" in body
    assert "Commit: `368dfc5`" in body


def test_empty_change_list_is_rejected():
    with pytest.raises(ValueError, match="At least one"):
        build_pull_request_title([], "abcdef123")
    with pytest.raises(ValueError, match="At least one"):
        build_pull_request_body([], "abcdef123")
