"""
GitHub webhook receiver.

Validates the HMAC-SHA256 signature, filters push events for modified/added
Python files, and dispatches the agent cycle as a background task.
"""

import hashlib
import hmac
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
    file_exists,
    get_file_content,
    get_push_diff,
    post_commit_comment,
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


def _process_push_event(payload: Dict[str, Any], files: List[Dict[str, str]]) -> None:
    """
    Background task: for each touched file, diff-parse the modified functions and
    run one agent cycle per function. Successes are batched into a single PR;
    if nothing succeeded but something failed, a comment is posted on the commit.
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

    successes: List[Tuple[str, str]] = []  # (path in new branch, test code)
    failures: List[str] = []  # "module::function" labels

    for patched_file in patch_set:
        path = patched_file.path
        if path not in wanted_paths:
            continue

        try:
            source_code = get_file_content(repo=repo, path=path, ref=head, token=GITHUB_TOKEN)
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", path, exc)
            continue

        module_name = Path(path).stem
        functions = extract_modified_functions(patched_file, source_code)

        if functions and file_exists(repo, _test_file_path(module_name), head, GITHUB_TOKEN):
            logger.info("Skipping %s — %s already exists", path, _test_file_path(module_name))
            continue

        for fn in functions:
            initial_state = create_initial_state(
                source_code, module_name=module_name, target_function=fn.name
            )
            try:
                final_state = _agent.invoke(initial_state)
            except Exception as exc:
                logger.error("Agent failed for %s::%s: %s", path, fn.name, exc)
                failures.append(f"{module_name}::{fn.name}")
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
                successes.append((f"tests/test_{module_name}_{fn.name}.py", final_state["test_code"]))
            else:
                failures.append(f"{module_name}::{fn.name}")

    if successes:
        branch = f"test-agent/push/{head[:7]}"
        tested = ", ".join(path.split("/")[-1] for path, _ in successes)
        body = f"Auto-generated tests for: {tested}"
        if failures:
            body += "\n\nFunctions that did not converge within max_attempts: " + ", ".join(failures)
        try:
            pr_url = create_pr(
                repo=repo,
                branch=branch,
                title=f"TestAgent: generated tests for push {head[:7]}",
                body=body,
                files=successes,
                token=GITHUB_TOKEN,
            )
            logger.info("Opened PR: %s", pr_url)
        except Exception as exc:
            logger.error("Failed to create PR for %s: %s", repo, exc)
    elif failures:
        try:
            post_commit_comment(
                repo=repo,
                sha=head,
                body="TestAgent could not generate a passing test within max_attempts for: "
                + ", ".join(failures),
                token=GITHUB_TOKEN,
            )
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
