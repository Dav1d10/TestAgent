"""
GitHub webhook receiver.

Validates the HMAC-SHA256 signature, filters push events for modified/added
Python files, and dispatches the agent cycle as a background task.
"""

import hashlib
import hmac
import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from unidiff import PatchSet

from app.agent.graph import build_agent_graph
from app.agent.state import create_initial_state
from app.config import GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
from app.tools.code_parser import (
    extract_deleted_function_names,
    extract_modified_functions,
    top_level_function_names,
)
from app.tools.github_client import (
    create_pr,
    get_file_content,
    get_file_content_or_empty,
    get_push_diff,
    post_commit_comment,
)
from app.tools.pr_description import (
    PullRequestAction,
    PullRequestChange,
    build_pull_request_body,
    build_pull_request_title,
)
from app.tools.sandbox import SandboxOutcome, run_test_in_sandbox
from app.tools.test_merger import (
    PreservedTestDependency,
    contains_test_cases,
    find_orphaned_test_functions,
    find_preserved_test_dependencies,
    has_bug_annotation,
    remove_owned_test_blocks,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Compiled once at import time — graph compilation is cheap but not free.
_agent = build_agent_graph()


def _verify_signature(payload_bytes: bytes, signature_header: Optional[str]) -> None:
    """Raise HTTP 403 if the HMAC-SHA256 signature does not match."""
    if not signature_header:
        raise HTTPException(status_code=403, detail="Missing X-Hub-Signature-256 header")
    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET not configured")
    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=403, detail="Invalid signature")


def _extract_python_files(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return deduplicated modified/added/removed .py files outside tests/.

    The last status seen across the push's commits wins, so a file modified in
    one commit and removed in a later commit is correctly classified as deleted.
    Uses payload["after"] as the ref for all files that still exist.
    """
    repo = payload["repository"]["full_name"]
    ref = payload["after"]
    by_path: Dict[str, Dict[str, Any]] = {}
    for commit in payload.get("commits", []):
        for path in commit.get("modified", []) + commit.get("added", []):
            if path.endswith(".py") and not path.startswith("tests/"):
                by_path[path] = {"repo": repo, "path": path, "ref": ref, "deleted": False}
        for path in commit.get("removed", []):
            if path.endswith(".py") and not path.startswith("tests/"):
                by_path[path] = {"repo": repo, "path": path, "ref": ref, "deleted": True}
    return list(by_path.values())


def _test_file_path(module_name: str) -> str:
    return f"tests/test_{module_name}.py"


def _safe_module_name(stem: str) -> str:
    """
    Disambiguate a filename stem that collides with an already-importable module
    (stdlib or otherwise installed). The sandbox writes source as {module_name}.py
    and the generated test does `from {module_name} import ...` — if module_name
    is e.g. "collections", that import silently resolves to the real stdlib
    module instead of our file, and every generated test fails on import with no
    connection to the actual function's correctness.
    """
    try:
        collides = importlib.util.find_spec(stem) is not None
    except (ImportError, ValueError):
        collides = False
    return f"{stem}_module" if collides else stem


def _build_broken_comment(module_name: str, function_name: str, failing_test: str) -> str:
    """
    Comment for a function whose test failed because the function itself appears
    wrong (a `# BUG:` annotation is present). Verdict first, then the failing test
    as reproducible evidence.
    """
    return (
        f"⚠️ **TestAgent: `{function_name}` appears to have a bug**\n\n"
        f"I generated a unit test for `{function_name}` in `{module_name}` based on its contract "
        "(name, docstring, and error messages), but the test does not pass against the current "
        "implementation. Per policy, TestAgent never adapts assertions to match an incorrect "
        "result — a test that passes against a broken function is worse than none — so this points "
        "to a bug in the implementation, not the test. No PR was opened for this function.\n\n"
        "**Failing test (kept as-is, expected to fail):**\n"
        f"```python\n{failing_test.strip()}\n```"
    )


def _build_unconverged_comment(
    module_name: str, function_name: str, failing_test: str, max_attempts: int
) -> str:
    """
    Comment for a function the agent simply could not cover within max_attempts,
    with no implementation bug detected — an agent limitation, not a code bug.
    """
    body = (
        f"ℹ️ **TestAgent: could not generate a passing test for `{function_name}`**\n\n"
        f"After {max_attempts} attempts the generated test for `{function_name}` in "
        f"`{module_name}` still did not pass, and no implementation bug was detected. This is "
        "usually a limitation of the agent on a tricky contract rather than a bug in your code. "
        "No PR was opened for this function."
    )
    if failing_test.strip():
        body += "\n\n**Last generated test (for reference):**\n" f"```python\n{failing_test.strip()}\n```"
    return body


def _build_cleanup_failed_comment(module_name: str, function_names: List[str], output: str) -> str:
    """Report a deterministic orphan-cleanup failure without blaming a healthy function."""
    names = ", ".join(f"`{name}`" for name in function_names)
    body = (
        f"🧹 **TestAgent: could not safely remove obsolete tests in `{module_name}`**\n\n"
        f"The source functions {names} no longer exist, but the module's test file still did "
        "not pass after their convention-owned tests/imports were removed. No cleanup PR was "
        "opened because TestAgent only publishes a test-file update after validating the full "
        "remaining file in the sandbox."
    )
    if output.strip():
        body += f"\n\n**Sandbox output:**\n```text\n{output.strip()}\n```"
    return body


def _build_infrastructure_error_comment(
    module_name: str, subject: str, output: str
) -> str:
    """Report an inconclusive run caused by the sandbox infrastructure."""
    body = (
        f"🛠️ **TestAgent: sandbox infrastructure error for {subject}**\n\n"
        f"The tests in `{module_name}` were not evaluated because the isolated execution "
        "environment could not start or complete normally. This is an operational failure, "
        "not evidence that the generated tests or source implementation are incorrect. "
        "TestAgent stopped without asking the model to rewrite the tests, and no PR was "
        "opened for this operation."
    )
    if output.strip():
        body += f"\n\n**Infrastructure diagnostic:**\n```text\n{output.strip()}\n```"
    return body


def _build_preserved_dependency_comment(
    module_name: str,
    dependencies: List[PreservedTestDependency],
    output: str,
    sandbox_outcome: SandboxOutcome,
) -> str:
    """Report deleted symbols still used by human/ownership-unknown test content."""
    details = "\n".join(
        f"- `{dependency.function_name}`: "
        + ", ".join(f"`{node}`" for node in dependency.affected_nodes)
        for dependency in dependencies
    )
    verdict = {
        SandboxOutcome.PASSED: "PASSED",
        SandboxOutcome.TEST_FAILED: "FAILED",
        SandboxOutcome.TIMED_OUT: "TIMED OUT",
        SandboxOutcome.INFRASTRUCTURE_ERROR: "INCONCLUSIVE — INFRASTRUCTURE ERROR",
    }[sandbox_outcome]
    body = (
        f"🛑 **TestAgent: human/unknown tests still depend on deleted code in `{module_name}`**\n\n"
        "TestAgent found preserved test content that statically references source functions "
        "which no longer exist. These blocks do not follow TestAgent's ownership convention, "
        "so they may be human-authored and were left byte-for-byte unchanged.\n\n"
        f"**Affected preserved content:**\n{details}\n\n"
        "No cleanup PR was opened for this module, and generation for its other modified "
        "functions was skipped. Manual review is required before this test file can be safely "
        "reconciled.\n\n"
        f"**Unchanged-file sandbox validation: {verdict}**"
    )
    if output.strip():
        body += f"\n```text\n{output.strip()}\n```"
    return body


def _process_push_event(payload: Dict[str, Any], files: List[Dict[str, Any]]) -> None:
    """
    Background task: for each touched module, diff-parse the modified functions and
    run one agent cycle per function. Each function is handled independently:

    - Passing functions' tests are merged into that module's single test file
      (tests/test_{module}.py), accumulating across functions, and all such files
      go into ONE pull request. The PR only ever contains tests that pass.
    - A function whose test fails all attempts never enters the PR; it produces a
      commit comment instead — differentiated between "the function looks buggy"
      (a `# BUG:` annotation is present) and "the agent could not converge".
    - A sandbox infrastructure error stops that function immediately, never enters
      the LLM correction loop, and produces an explicitly inconclusive comment.

    PR and commit comments are independent: a single push can produce both.
    Runs synchronously in a thread pool (Starlette's BackgroundTasks calls
    run_in_threadpool for sync functions, so this does not block the event loop).
    """
    repo = payload["repository"]["full_name"]
    base = payload["before"]
    head = payload["after"]
    files_by_path = {f["path"]: f for f in files}
    wanted_paths = set(files_by_path)

    try:
        diff_text = get_push_diff(repo=repo, base=base, head=head, token=GITHUB_TOKEN)
        patch_set = PatchSet(diff_text)
    except Exception as exc:
        logger.error("Failed to fetch/parse push diff for %s (%s...%s): %s", repo, base, head, exc)
        return

    pr_files: List[Tuple[str, str]] = []   # (test_path, merged content) — only green tests
    deleted_test_files: List[str] = []     # module test files removed when no tests can remain
    pr_changes: List[PullRequestChange] = []  # factual metadata for the published PR
    broken_comments: List[str] = []        # one commit-comment body per failed function

    for patched_file in patch_set:
        path = patched_file.path
        if path not in wanted_paths:
            continue

        module_name = _safe_module_name(Path(path).stem)
        test_path = _test_file_path(module_name)

        # A fully deleted source module normally makes its convention-owned test
        # file obsolete. Preserve it instead when human/unknown blocks still
        # reference any of the deleted functions: ownership is not sufficient to
        # delete those blocks safely, even though the unchanged file will fail.
        if files_by_path[path].get("deleted", False):
            try:
                existing_test = get_file_content_or_empty(repo, test_path, head, GITHUB_TOKEN)
            except Exception as exc:
                logger.error("Failed to fetch deleted module baseline %s: %s", test_path, exc)
                continue
            if not existing_test.strip():
                continue
            try:
                old_source_code = get_file_content_or_empty(repo, path, base, GITHUB_TOKEN)
            except Exception as exc:
                logger.error("Failed to fetch deleted source %s@%s: %s", path, base, exc)
                continue

            deleted_names = top_level_function_names(old_source_code)
            preserved_dependencies = find_preserved_test_dependencies(
                existing_content=existing_test,
                module_name=module_name,
                deleted_function_names=deleted_names,
                current_function_names=[],
                current_source_code="",
            )
            if preserved_dependencies:
                sandbox_result = run_test_in_sandbox(
                    source_code="",
                    test_code=existing_test,
                    module_name=module_name,
                )
                output = "\n".join(
                    part.strip()
                    for part in (sandbox_result.stdout, sandbox_result.stderr)
                    if part.strip()
                )
                broken_comments.append(
                    _build_preserved_dependency_comment(
                        module_name,
                        preserved_dependencies,
                        output,
                        sandbox_result.outcome,
                    )
                )
                continue

            if test_path not in deleted_test_files:
                deleted_test_files.append(test_path)
                pr_changes.append(
                    PullRequestChange(
                        action=PullRequestAction.DELETED,
                        test_path=test_path,
                        functions=tuple(deleted_names),
                        reason="Source module was deleted",
                    )
                )
            continue

        try:
            source_code = get_file_content(repo=repo, path=path, ref=head, token=GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", path, exc)
            continue

        try:
            old_source_code = get_file_content_or_empty(repo, path, base, GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch previous source %s@%s: %s", path, base, exc)
            continue

        functions = extract_modified_functions(patched_file, source_code)
        current_function_names = top_level_function_names(source_code)
        deleted_function_names = extract_deleted_function_names(old_source_code, source_code)

        # Baseline the module's existing test file so a function's tests merge into
        # it rather than clobbering the tests of untouched functions.
        try:
            accumulated = get_file_content_or_empty(repo, test_path, head, GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch baseline %s: %s", test_path, exc)
            continue

        # Hybrid deletion handling:
        # 1. before/after AST comparison catches this push's deletions;
        # 2. source/test reconciliation heals stale blocks left by a previously
        #    missed webhook (the failure reproduced by Stage 4 edge case #9).
        orphaned_function_names = find_orphaned_test_functions(
            accumulated,
            module_name,
            current_function_names,
        )

        preserved_dependencies = find_preserved_test_dependencies(
            existing_content=accumulated,
            module_name=module_name,
            deleted_function_names=deleted_function_names,
            current_function_names=current_function_names,
            current_source_code=source_code,
        )
        if preserved_dependencies:
            # Any affected human/unknown block makes partial cleanup unsafe. Keep
            # the entire baseline unchanged, validate that exact state for useful
            # evidence, warn on the triggering commit, and skip this module.
            sandbox_result = run_test_in_sandbox(
                source_code=source_code,
                test_code=accumulated,
                module_name=module_name,
            )
            output = "\n".join(
                part.strip()
                for part in (sandbox_result.stdout, sandbox_result.stderr)
                if part.strip()
            )
            logger.warning(
                "Preserved test dependencies block cleanup for %s: %s",
                path,
                ", ".join(
                    dependency.function_name for dependency in preserved_dependencies
                ),
            )
            broken_comments.append(
                _build_preserved_dependency_comment(
                    module_name,
                    preserved_dependencies,
                    output,
                    sandbox_result.outcome,
                )
            )
            continue

        cleanup_names = list(
            dict.fromkeys(deleted_function_names + orphaned_function_names)
        )
        old_function_names = top_level_function_names(old_source_code)
        all_known_names = list(
            dict.fromkeys(old_function_names + current_function_names + cleanup_names)
        )

        original_test_file = accumulated
        if cleanup_names:
            accumulated = remove_owned_test_blocks(
                existing_content=accumulated,
                function_names=cleanup_names,
                module_name=module_name,
                all_function_names=all_known_names,
            )

        cleanup_changed = accumulated != original_test_file
        module_has_pr_change = False
        delete_test_file_if_no_success = False
        successful_generation_changes: List[PullRequestChange] = []

        if cleanup_changed:
            if contains_test_cases(accumulated):
                cleanup_result = run_test_in_sandbox(
                    source_code=source_code,
                    test_code=accumulated,
                    module_name=module_name,
                )
                if not cleanup_result.passed:
                    output = "\n".join(
                        part.strip()
                        for part in (cleanup_result.stdout, cleanup_result.stderr)
                        if part.strip()
                    )
                    logger.error(
                        "Cleanup validation failed for %s (%s): %s",
                        path,
                        ", ".join(cleanup_names),
                        output,
                    )
                    if cleanup_result.outcome == SandboxOutcome.INFRASTRUCTURE_ERROR:
                        names = ", ".join(f"`{name}`" for name in cleanup_names)
                        broken_comments.append(
                            _build_infrastructure_error_comment(
                                module_name,
                                f"obsolete-test cleanup ({names})",
                                output,
                            )
                        )
                    else:
                        broken_comments.append(
                            _build_cleanup_failed_comment(module_name, cleanup_names, output)
                        )
                    continue
                module_has_pr_change = True
            else:
                # A module test file with no tests is obsolete. Start any newly
                # generated block from a clean baseline; if none succeeds, the
                # existing file will be deleted in the PR.
                accumulated = ""
                module_has_pr_change = True
                delete_test_file_if_no_success = bool(original_test_file.strip())

        for fn in functions:
            state = create_initial_state(
                source_code,
                module_name=module_name,
                target_function=fn.name,
                existing_test_file=accumulated,
            )
            try:
                final_state = _agent.invoke(state)
            except Exception as exc:
                logger.error("Agent failed for %s::%s: %s", path, fn.name, exc)
                broken_comments.append(
                    _build_unconverged_comment(module_name, fn.name, "", 0)
                )
                continue

            logger.info(
                "Agent completed %s::%s — status: %s, attempts: %d/%d",
                path,
                fn.name,
                final_state["final_status"],
                final_state["attempt_count"],
                final_state["max_attempts"],
            )

            if final_state["final_status"] == "success":
                # Advance the accumulator so the next function in this module merges
                # on top of this function's green tests.
                accumulated = final_state["merged_test_code"]
                module_has_pr_change = True
                delete_test_file_if_no_success = False
                successful_generation_changes.append(
                    PullRequestChange(
                        action=(
                            PullRequestAction.UPDATED
                            if original_test_file.strip()
                            else PullRequestAction.CREATED
                        ),
                        test_path=test_path,
                        functions=(fn.name,),
                        reason=(
                            "Source function was modified"
                            if fn.name in old_function_names
                            else "New source function was added"
                        ),
                    )
                )
            elif final_state["final_status"] == "infrastructure_error":
                output = "\n".join(
                    part.strip()
                    for part in (
                        final_state.get("test_stdout") or "",
                        final_state.get("test_stderr") or "",
                    )
                    if part.strip()
                )
                broken_comments.append(
                    _build_infrastructure_error_comment(
                        module_name,
                        f"`{fn.name}`",
                        output,
                    )
                )
            else:
                failing = final_state.get("test_code") or ""
                if has_bug_annotation(failing):
                    broken_comments.append(_build_broken_comment(module_name, fn.name, failing))
                else:
                    broken_comments.append(
                        _build_unconverged_comment(
                            module_name, fn.name, failing, final_state["max_attempts"]
                        )
                    )

        if module_has_pr_change:
            if contains_test_cases(accumulated):
                pr_files.append((test_path, accumulated))
                if cleanup_changed:
                    directly_deleted = [
                        name for name in cleanup_names if name in deleted_function_names
                    ]
                    reconciled_orphans = [
                        name for name in cleanup_names if name not in deleted_function_names
                    ]
                    if directly_deleted:
                        pr_changes.append(
                            PullRequestChange(
                                action=PullRequestAction.CLEANED,
                                test_path=test_path,
                                functions=tuple(directly_deleted),
                                reason="Source function no longer exists",
                            )
                        )
                    if reconciled_orphans:
                        pr_changes.append(
                            PullRequestChange(
                                action=PullRequestAction.CLEANED,
                                test_path=test_path,
                                functions=tuple(reconciled_orphans),
                                reason="Orphaned tests found during reconciliation",
                            )
                        )
                pr_changes.extend(successful_generation_changes)
            elif delete_test_file_if_no_success and test_path not in deleted_test_files:
                deleted_test_files.append(test_path)
                directly_deleted = [
                    name for name in cleanup_names if name in deleted_function_names
                ]
                reconciled_orphans = [
                    name for name in cleanup_names if name not in deleted_function_names
                ]
                if directly_deleted:
                    pr_changes.append(
                        PullRequestChange(
                            action=PullRequestAction.DELETED,
                            test_path=test_path,
                            functions=tuple(directly_deleted),
                            reason="No testable source functions remain",
                        )
                    )
                if reconciled_orphans:
                    pr_changes.append(
                        PullRequestChange(
                            action=PullRequestAction.DELETED,
                            test_path=test_path,
                            functions=tuple(reconciled_orphans),
                            reason="All remaining tests were orphaned",
                        )
                    )

    if pr_files or deleted_test_files:
        branch = f"test-agent/push/{head[:7]}"
        try:
            pr_url = create_pr(
                repo=repo,
                branch=branch,
                title=build_pull_request_title(pr_changes, head),
                body=build_pull_request_body(pr_changes, head),
                files=pr_files,
                token=GITHUB_TOKEN,
                deleted_files=deleted_test_files,
            )
            logger.info("Opened PR: %s", pr_url)
        except Exception as exc:
            logger.error("Failed to create PR for %s: %s", repo, exc)

    for body in broken_comments:
        try:
            post_commit_comment(repo=repo, sha=head, body=body, token=GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to post commit comment on %s@%s: %s", repo, head, exc)


@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    payload_bytes = await request.body()
    _verify_signature(payload_bytes, x_hub_signature_256)

    if x_github_event != "push":
        return {"status": "ignored", "event": x_github_event}

    payload = json.loads(payload_bytes)
    files = _extract_python_files(payload)

    if not files:
        return {"status": "no_python_files"}

    background_tasks.add_task(_process_push_event, payload, files)
    return {"status": "accepted", "files_queued": len(files)}
