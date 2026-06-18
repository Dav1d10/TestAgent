"""
Agent prompts. Kept separate from graph logic because these are the most
frequently iterated piece — tuning prompts should never require touching node code.
"""

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

FIX_TEST_SYSTEM_PROMPT = """You are a senior software engineer debugging a failing test.
You are given the original code, the test you generated previously, and the error it produced.

Strict rules:
- Respond ONLY with the complete corrected test code, no markdown.
- Do NOT modify the source code under any circumstances.

Determine the cause of the failure before making any change:

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
than no test suite.
"""

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


def build_generate_test_prompt(source_code: str, module_name: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for initial test generation."""
    system = GENERATE_TEST_SYSTEM_PROMPT.format(module_name=module_name)
    user = GENERATE_TEST_USER_TEMPLATE.format(
        module_name=module_name, source_code=source_code
    )
    return system, user


def build_fix_test_prompt(
    source_code: str, module_name: str, test_code: str, error_output: str
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for a self-correction attempt."""
    system = FIX_TEST_SYSTEM_PROMPT
    user = FIX_TEST_USER_TEMPLATE.format(
        module_name=module_name,
        source_code=source_code,
        test_code=test_code,
        error_output=error_output,
    )
    return system, user
