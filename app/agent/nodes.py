"""
Graph nodes. Each function receives the full AgentState, does its work,
and returns a partial dict with only the fields it updated.
"""

from openai import OpenAI

from app.agent.state import AgentState
from app.agent.prompts import build_generate_test_prompt, build_fix_test_prompt
from app.tools.sandbox import run_test_in_sandbox

client = OpenAI()

# GPT-4o over GPT-4o mini: reasoning about code, generating edge cases,
# and self-correcting from error messages needs the higher-capacity model.
MODEL_NAME = "gpt-4o"


def _call_llm(system_prompt: str, user_prompt: str) -> str:
    """Calls the LLM and returns the raw text of the response."""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content.strip()


def _strip_markdown_fences(code: str) -> str:
    """
    Removes markdown code fences the LLM may include despite instructions.
    Handles ```python ... ``` and plain ``` ... ``` wrappers.
    """
    lines = code.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def generate_test_node(state: AgentState) -> dict:
    """First node: generates the initial test from the source code."""
    system, user = build_generate_test_prompt(
        source_code=state["source_code"],
        module_name=state["module_name"],
    )
    raw = _call_llm(system, user)
    test_code = _strip_markdown_fences(raw)

    return {
        "test_code": test_code,
        "attempt_count": state["attempt_count"] + 1,
    }


def run_test_node(state: AgentState) -> dict:
    """Runs the current test (initial or corrected) inside the sandbox."""
    result = run_test_in_sandbox(
        source_code=state["source_code"],
        test_code=state["test_code"],
        module_name=state["module_name"],
    )

    return {
        "test_passed": result.passed,
        "test_stdout": result.stdout,
        "test_stderr": result.stderr,
    }


def fix_test_node(state: AgentState) -> dict:
    """
    Runs when the previous test failed and attempts remain.
    Passes the real error output to the LLM so it can correct the test,
    not the source code.
    """
    error_output = state["test_stderr"] or state["test_stdout"] or ""
    system, user = build_fix_test_prompt(
        source_code=state["source_code"],
        module_name=state["module_name"],
        test_code=state["test_code"],
        error_output=error_output,
    )
    raw = _call_llm(system, user)
    fixed_test_code = _strip_markdown_fences(raw)

    return {
        "test_code": fixed_test_code,
        "attempt_count": state["attempt_count"] + 1,
    }


def finalize_node(state: AgentState) -> dict:
    """Terminal node: records the final outcome of the cycle."""
    if state["test_passed"]:
        return {"final_status": "success"}
    return {"final_status": "failed_max_attempts"}


def should_retry(state: AgentState) -> str:
    """
    Conditional edge function used by LangGraph to decide the next node
    after a test run.

    Returns the name of the next node: "fix_test" or "finalize".
    """
    if state["test_passed"]:
        return "finalize"
    if state["attempt_count"] >= state["max_attempts"]:
        return "finalize"
    return "fix_test"
