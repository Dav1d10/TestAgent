"""Deterministic titles and Markdown bodies for TestAgent pull requests."""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


class PullRequestAction(str, Enum):
    """A source-driven change that TestAgent actually publishes in a PR."""

    CREATED = "created"
    UPDATED = "updated"
    CLEANED = "cleaned"
    DELETED = "deleted"


@dataclass(frozen=True)
class PullRequestChange:
    """Review-facing metadata for one test-file operation and its cause."""

    action: PullRequestAction
    test_path: str
    functions: tuple[str, ...]
    reason: str


_ACTION_ORDER = {
    PullRequestAction.CREATED: 0,
    PullRequestAction.UPDATED: 1,
    PullRequestAction.CLEANED: 2,
    PullRequestAction.DELETED: 3,
}

_SUMMARIES = {
    PullRequestAction.UPDATED: (
        "Updated generated tests to match source functions changed by this push."
    ),
    PullRequestAction.CLEANED: (
        "Removed obsolete generated tests while preserving the remaining test file content."
    ),
}


def _normalized(changes: Iterable[PullRequestChange]) -> list[PullRequestChange]:
    """Combine equivalent rows and return them in stable review order."""
    grouped: dict[tuple[PullRequestAction, str, str], set[str]] = {}
    for change in changes:
        key = (change.action, change.test_path, change.reason)
        grouped.setdefault(key, set()).update(change.functions)

    return [
        PullRequestChange(
            action=action,
            test_path=test_path,
            functions=tuple(sorted(functions)),
            reason=reason,
        )
        for (action, test_path, reason), functions in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][1],
                _ACTION_ORDER[item[0][0]],
                item[0][2],
            ),
        )
    ]


def _inline_code(value: str) -> str:
    """Keep repository-derived values from breaking a Markdown table row."""
    escaped = value.replace("|", "&#124;").replace("`", "&#96;")
    return f"`{escaped}`"


def build_pull_request_title(
    changes: Sequence[PullRequestChange], head: str
) -> str:
    """Return an action-specific title, falling back to reconciliation for mixed work."""
    normalized = _normalized(changes)
    if not normalized:
        raise ValueError("At least one pull-request change is required")

    actions = {change.action for change in normalized}
    short_sha = head[:7]
    if len(actions) > 1:
        operation = "reconcile tests"
    else:
        action = next(iter(actions))
        if action == PullRequestAction.CREATED:
            operation = "add tests"
        elif action == PullRequestAction.UPDATED:
            operation = "update tests"
        elif action == PullRequestAction.CLEANED:
            operation = "remove obsolete tests"
        else:
            deleted_paths = {
                change.test_path
                for change in normalized
                if change.action == PullRequestAction.DELETED
            }
            noun = "test file" if len(deleted_paths) == 1 else "test files"
            operation = f"delete obsolete {noun}"

    return f"TestAgent: {operation} for push {short_sha}"


def build_pull_request_body(
    changes: Sequence[PullRequestChange], head: str
) -> str:
    """Render a concise factual body without claiming repository-wide validation."""
    normalized = _normalized(changes)
    if not normalized:
        raise ValueError("At least one pull-request change is required")

    actions = {change.action for change in normalized}
    if len(actions) == 1:
        action = next(iter(actions))
        path_count = len({change.test_path for change in normalized})
        if action == PullRequestAction.CREATED:
            summary = (
                "Created a generated test file for source functions changed by this push."
                if path_count == 1
                else "Created generated test files for source functions changed by this push."
            )
        elif action == PullRequestAction.DELETED:
            summary = (
                "Removed an obsolete generated test file after detecting that its source "
                "module no longer contains any testable functions."
                if path_count == 1
                else "Removed obsolete generated test files after detecting that their source "
                "modules no longer contain any testable functions."
            )
        else:
            summary = _SUMMARIES[action]
    else:
        summary = (
            "Reconciled generated tests with the current source code using the changes "
            "detected in this push."
        )

    rows = []
    for change in normalized:
        functions = (
            ", ".join(_inline_code(name) for name in change.functions)
            if change.functions
            else "—"
        )
        rows.append(
            f"| {change.action.value.title()} | {_inline_code(change.test_path)} "
            f"| {functions} | {change.reason} |"
        )

    return "\n".join(
        [
            "## Summary",
            "",
            summary,
            "",
            "## Changes",
            "",
            "| Action | Test file | Functions | Reason |",
            "| --- | --- | --- | --- |",
            *rows,
            "",
            "## Trigger",
            "",
            f"Commit: {_inline_code(head[:7])}",
        ]
    )
