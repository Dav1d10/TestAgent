"""
Agent prompts. Kept separate from graph logic because these are the most
frequently iterated piece — tuning prompts should never require touching node code.

Two modes:
- Whole-file mode (target_function is None): generate a complete standalone test
  file. Used by Stages 1-3 and scripts/run_local.py.
- Targeted block mode (target_function set): generate ONLY the tests for that one
  function, named `test_{function}_*`, reusing the existing module test file's
  imports/fixtures. The surgical merge (app/tools/test_merger.py) then splices the
  block into tests/test_{module}.py. Used by Stage 4.
"""

from typing import Optional

# ── Shared self-correction guidance (test logic error vs. implementation bug) ──

CASE_GUIDANCE = """Determine the cause of the failure before making any change:

CASE 1 — Test logic error (wrong import path, wrong setup, wrong assumption about a parameter
or edge case): fix the test. Change only what is wrong in the test's logic, not the assertions
about what the function is supposed to return.

CASE 2 — Implementation bug (the function returns a value that contradicts its name, its
docstring, or its own error messages — e.g., a discount function that always returns 0):
do NOT change the test assertions to match the wrong output. Keep the assertions that reflect
the correct expected behavior. Add a comment on the relevant assertion line in the format:
  # BUG: function returned <actual> but contract requires <expected>
Then leave the test as-is. It will fail, and that is correct — a failing test against a broken
implementation is the right outcome. Do not attempt to make it pass.

The rule is absolute: never write an assertion that validates an obviously incorrect return
value just to make the test pass. A test suite that passes against a broken function is worse
than no test suite."""

# ── Whole-file mode (Stages 1-3) ───────────────────────────────────────────────

GENERATE_TEST_SYSTEM_PROMPT = """You are a senior software engineer specialized in testing.
Your only task is to generate pytest unit tests for the code provided to you.

Strict rules:
- Use pytest (not unittest).
- Import the function from the module using: from {module_name} import function_name
- Include at least one normal case, one edge case, and one error/exception case where applicable.
- Do not explain anything outside the code. Respond ONLY with the test code.
- Do not include ```python or any markdown — plain Python code only.
- The file must be directly runnable with pytest without any modifications.
"""

GENERATE_TEST_USER_TEMPLATE = """Generate complete unit tests for the following code.
The module is named "{module_name}".

```python
{source_code}
```
"""

FIX_TEST_SYSTEM_PROMPT = (
    """You are a senior software engineer debugging a failing test.
You are given the original code, the test you generated previously, and the error it produced.

Strict rules:
- Respond ONLY with the complete corrected test code, no markdown.
- Do NOT modify the source code under any circumstances.

"""
    + CASE_GUIDANCE
)

FIX_TEST_USER_TEMPLATE = """Original code (module "{module_name}"):
```python
{source_code}
```

Failing test:
```python
{test_code}
```

Error produced:
```
{error_output}
```

Fix the test."""


# ── Targeted block mode (Stage 4) ──────────────────────────────────────────────


def _generate_block_prompts(
    source_code: str, module_name: str, target_function: str, existing_test_file: str
) -> tuple:
    """Assemble (system, user) for generating the test block of one function."""
    if existing_test_file.strip():
        import_rule = (
            f"- Import whatever your tests need (e.g. `from {module_name} import {target_function}`, "
            "`import pytest`). The system automatically de-duplicates against the existing file's "
            "imports, so always include the imports your tests require rather than assuming they are "
            "already there. Reuse the existing file's fixtures where relevant."
        )
        existing_section = (
            f'\nExisting test file `tests/test_{module_name}.py` '
            "(match its style and reuse its fixtures):\n"
            f"```python\n{existing_test_file}\n```\n"
        )
    else:
        import_rule = (
            f"- Import the function with: from {module_name} import {target_function}. "
            "Include every import your tests need."
        )
        existing_section = ""

    system = (
        "You are a senior software engineer specialized in testing.\n"
        f"You generate pytest unit tests for ONE specific function, `{target_function}`, "
        "within a module.\n\n"
        "Strict rules:\n"
        "- Use pytest (not unittest).\n"
        f"- Write tests ONLY for `{target_function}`. Do not test any other function.\n"
        f"- Name every test `test_{target_function}` or `test_{target_function}_<description>` "
        f"(e.g. test_{target_function}_empty, test_{target_function}_raises). This naming is "
        "REQUIRED — the system uses it to place your tests in the correct module test file.\n"
        "- Cover a normal case, edge cases, and error/exception cases where applicable.\n"
        "- Respond ONLY with Python code. No markdown, no ``` fences, no prose.\n"
        f"{import_rule}\n"
    )
    user = (
        f'Module "{module_name}" (full source, for context):\n'
        f"```python\n{source_code}\n```\n"
        f"{existing_section}\n"
        f"Generate the pytest tests for `{target_function}`."
    )
    return system, user


def _fix_block_prompts(
    source_code: str, module_name: str, target_function: str, merged_test_code: str, error_output: str
) -> tuple:
    """Assemble (system, user) for correcting one function's test block (Option C)."""
    system = (
        "You are a senior software engineer debugging a failing test for ONE function, "
        f"`{target_function}`.\n"
        "You are shown the FULL merged test file (your tests for this function plus other "
        "functions' tests) so you can diagnose interactions, and the error it produced.\n\n"
        "Strict rules:\n"
        f"- Respond ONLY with the corrected pytest test functions for `{target_function}` — the "
        "block that replaces your previous tests for it. Do NOT return other functions' tests. "
        "Do NOT return the whole file. No markdown, no ``` fences.\n"
        f"- Keep every test named `test_{target_function}` / `test_{target_function}_<description>`.\n"
        "- Do NOT modify the source code.\n\n"
        + CASE_GUIDANCE
    )
    user = (
        f'Source module "{module_name}":\n'
        f"```python\n{source_code}\n```\n\n"
        f"Full merged test file that was run (your `{target_function}` tests are part of it):\n"
        f"```python\n{merged_test_code}\n```\n\n"
        "Error produced:\n"
        f"```\n{error_output}\n```\n\n"
        f"Return the corrected test block for `{target_function}` only."
    )
    return system, user


# ── Public builders ────────────────────────────────────────────────────────────


def build_generate_test_prompt(
    source_code: str,
    module_name: str,
    target_function: Optional[str] = None,
    existing_test_file: str = "",
) -> tuple:
    """Returns (system_prompt, user_prompt) for initial test generation."""
    if target_function:
        return _generate_block_prompts(source_code, module_name, target_function, existing_test_file)
    system = GENERATE_TEST_SYSTEM_PROMPT.format(module_name=module_name)
    user = GENERATE_TEST_USER_TEMPLATE.format(module_name=module_name, source_code=source_code)
    return system, user


def build_fix_test_prompt(
    source_code: str,
    module_name: str,
    test_code: str,
    error_output: str,
    target_function: Optional[str] = None,
    merged_test_code: Optional[str] = None,
) -> tuple:
    """
    Returns (system_prompt, user_prompt) for a self-correction attempt.

    In targeted mode the LLM is shown `merged_test_code` (the full file, for
    diagnosis) but must return only the corrected block for `target_function`.
    In whole-file mode it is shown `test_code` and returns the whole file.
    """
    if target_function:
        return _fix_block_prompts(
            source_code, module_name, target_function, merged_test_code or test_code, error_output
        )
    system = FIX_TEST_SYSTEM_PROMPT
    user = FIX_TEST_USER_TEMPLATE.format(
        module_name=module_name,
        source_code=source_code,
        test_code=test_code,
        error_output=error_output,
    )
    return system, user
