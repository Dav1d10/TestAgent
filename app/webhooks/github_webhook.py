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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.agent.graph import build_agent_graph
from app.agent.state import create_initial_state
from app.config import GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
from app.tools.github_client import get_file_content

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


def _process_push_event(files: List[Dict[str, str]]) -> None:
    """
    Background task: fetch source for each file and run the agent cycle.
    Runs synchronously in a thread pool (Starlette's BackgroundTasks calls
    run_in_threadpool for sync functions, so this does not block the event loop).

    Stage 4 will replace the logger.info call with PR creation on success
    and a PR comment on failed_max_attempts.
    """
    for file_info in files:
        try:
            source_code = get_file_content(
                repo=file_info["repo"],
                path=file_info["path"],
                ref=file_info["ref"],
                token=GITHUB_TOKEN,
            )
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", file_info["path"], exc)
            continue

        module_name = Path(file_info["path"]).stem
        initial_state = create_initial_state(source_code, module_name=module_name)

        try:
            final_state = _agent.invoke(initial_state)
            logger.info(
                "Agent completed %s — status: %s, attempts: %d/%d",
                file_info["path"],
                final_state["final_status"],
                final_state["attempt_count"],
                final_state["max_attempts"],
            )
            # Stage 4: if final_state["final_status"] == "success" → open PR
            # Stage 4: if "failed_max_attempts" → post comment on triggering commit
        except Exception as exc:
            logger.error("Agent failed for %s: %s", file_info["path"], exc)


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

    background_tasks.add_task(_process_push_event, files)
    return {"status": "accepted", "files_queued": len(files)}
