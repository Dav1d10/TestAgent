"""
Defines the state object that flows through the agent graph.

In LangGraph, every node receives the full state, updates it, and returns
the modified fields. This TypedDict is the contract for what exists at each
point in the flow.
"""

from typing import TypedDict, Optional


class AgentState(TypedDict):
    # Input: what the agent receives at startup
    source_code: str        # the function/code that needs tests
    module_name: str        # filename stem given to the module (no .py extension)
    target_function: Optional[str]  # name of the specific function to test within source_code
                                     # (None means "test the file as a whole", used by Stages 1-3)
    existing_test_file: str  # current contents of tests/test_{module}.py in the repo, or ""
                             # if none yet. The baseline the target function's tests merge into.

    # Generated during the flow
    test_code: Optional[str]        # the LLM's raw output: in targeted mode, only the target
                                     # function's test block; in whole-file mode, the full file
    merged_test_code: Optional[str]  # test_code merged into existing_test_file — this is what
                                     # runs in the sandbox and, on success, goes into the PR
    test_passed: Optional[bool]     # result of the last sandbox run
    test_stdout: Optional[str]      # pytest stdout
    test_stderr: Optional[str]      # pytest stderr / error details

    # Self-correction loop control
    attempt_count: int      # how many generate/fix cycles have run so far
    max_attempts: int       # ceiling before giving up (default: 3)

    # Final outcome
    final_status: Optional[str]     # "success" | "failed_max_attempts" | "error"
    error_message: Optional[str]    # detail when something fails outside normal flow


def create_initial_state(
    source_code: str,
    module_name: str = "target_module",
    max_attempts: int = 3,
    target_function: Optional[str] = None,
    existing_test_file: str = "",
) -> AgentState:
    """Returns the initial state to start the graph with a new function."""
    return AgentState(
        source_code=source_code,
        module_name=module_name,
        target_function=target_function,
        existing_test_file=existing_test_file,
        test_code=None,
        merged_test_code=None,
        test_passed=None,
        test_stdout=None,
        test_stderr=None,
        attempt_count=0,
        max_attempts=max_attempts,
        final_status=None,
        error_message=None,
    )
