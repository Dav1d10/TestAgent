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
- Fix the test so it passes, without changing the expected behavior of the original function.
- If the error shows your test had an incorrect expectation, adjust the test accordingly.
- If the error suggests a real bug in the source code, do NOT modify the source — adjust the
  test to reflect the actual (correct) behavior and flag the potential bug in a comment.
- Respond ONLY with the complete corrected test code, no markdown.
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
