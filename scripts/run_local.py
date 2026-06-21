"""
Runs the agent against an example function directly from the terminal.
Useful for validating the core cycle before connecting GitHub or FastAPI.

Usage:
    python scripts/run_local.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.config  # noqa: F401 — triggers load_dotenv() before OpenAI() is instantiated

from app.agent.graph import build_agent_graph
from app.agent.state import create_initial_state


EXAMPLE_FUNCTION = '''
def calculate_discount(price, percentage):
    if percentage < 0 or percentage > 100:
        raise ValueError("Percentage must be between 0 and 100")
    return price - (price * percentage / 100)
'''


def main():
    agent = build_agent_graph()
    initial_state = create_initial_state(
        source_code=EXAMPLE_FUNCTION,
        module_name="target_module",
        max_attempts=3,
    )

    print("=" * 60)
    print("Starting agent with example function...")
    print("=" * 60)

    final_state = agent.invoke(initial_state)

    print("\n" + "=" * 60)
    print(f"Final status:    {final_state['final_status']}")
    print(f"Attempts used:   {final_state['attempt_count']}/{final_state['max_attempts']}")
    print("=" * 60)
    print("\nGenerated test:\n")
    print(final_state["test_code"])
    print("\nPytest output:\n")
    print(final_state["test_stdout"])

    if final_state["test_stderr"]:
        print("\nErrors:\n")
        print(final_state["test_stderr"])


if __name__ == "__main__":
    main()
