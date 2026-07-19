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
from app.tools.code_parser import extract_modified_functions
from app.tools.github_client import (
    create_pr,
    get_file_content,
    get_file_content_or_empty,
    get_push_diff,
    post_commit_comment,
)
from app.tools.test_merger import has_bug_annotation

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


def _extract_python_files(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Return deduplicated list of modified/added .py files (outside tests/) from a
    push event payload. Uses payload["after"] as the ref for all files so every
    file is fetched at the same HEAD commit.
    """
    repo = payload["repository"]["full_name"]
    ref = payload["after"]
    seen: set = set()
    results = []
    for commit in payload.get("commits", []):
        changed = commit.get("modified", []) + commit.get("added", [])
        for path in changed:
            if path.endswith(".py") and not path.startswith("tests/") and path not in seen:
                seen.add(path)
                results.append({"repo": repo, "path": path, "ref": ref})
    return results


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


def _process_push_event(payload: Dict[str, Any], files: List[Dict[str, str]]) -> None:
    """
    Background task: for each touched module, diff-parse the modified functions and
    run one agent cycle per function. Each function is handled independently:

    - Passing functions' tests are merged into that module's single test file
      (tests/test_{module}.py), accumulating across functions, and all such files
      go into ONE pull request. The PR only ever contains tests that pass.
    - A function whose test fails all attempts never enters the PR; it produces a
      commit comment instead — differentiated between "the function looks buggy"
      (a `# BUG:` annotation is present) and "the agent could not converge".

    PR and commit comments are independent: a single push can produce both.
    Runs synchronously in a thread pool (Starlette's BackgroundTasks calls
    run_in_threadpool for sync functions, so this does not block the event loop).
    """
    repo = payload["repository"]["full_name"]
    base = payload["before"]
    head = payload["after"]
    wanted_paths = {f["path"] for f in files}

    try:
        diff_text = get_push_diff(repo=repo, base=base, head=head, token=GITHUB_TOKEN)
        patch_set = PatchSet(diff_text)
    except Exception as exc:
        logger.error("Failed to fetch/parse push diff for %s (%s...%s): %s", repo, base, head, exc)
        return

    pr_files: List[Tuple[str, str]] = []   # (test_path, merged content) — only green tests
    broken_comments: List[str] = []        # one commit-comment body per failed function

    for patched_file in patch_set:
        path = patched_file.path
        if path not in wanted_paths:
            continue

        try:
            source_code = get_file_content(repo=repo, path=path, ref=head, token=GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", path, exc)
            continue

        module_name = _safe_module_name(Path(path).stem)
        test_path = _test_file_path(module_name)
        functions = extract_modified_functions(patched_file, source_code)
        if not functions:
            continue

        # Baseline the module's existing test file so a function's tests merge into
        # it rather than clobbering the tests of untouched functions.
        try:
            accumulated = get_file_content_or_empty(repo, test_path, head, GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch baseline %s: %s", test_path, exc)
            continue

        module_has_success = False

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
                module_has_success = True
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

        if module_has_success:
            pr_files.append((test_path, accumulated))

    if pr_files:
        branch = f"test-agent/push/{head[:7]}"
        modules = ", ".join(p.split("/")[-1] for p, _ in pr_files)
        try:
            pr_url = create_pr(
                repo=repo,
                branch=branch,
                title=f"TestAgent: tests for push {head[:7]}",
                body=f"Auto-generated/updated tests for: {modules}",
                files=pr_files,
                token=GITHUB_TOKEN,
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
