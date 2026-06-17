"""
Builds the LangGraph agent graph: connects the nodes from nodes.py
into the flow generate -> run -> (fix -> run)* -> finalize.
"""

from langgraph.graph import StateGraph, END

from app.agent.state import AgentState
from app.agent.nodes import (
    generate_test_node,
    run_test_node,
    fix_test_node,
    finalize_node,
    should_retry,
)


def build_agent_graph():
    """
    Compiles and returns the agent graph.

    Flow:
        generate_test -> run_test -> [decision]
                                         |-- passed or max attempts reached --> finalize
                                         |-- failed, attempts remain --> fix_test -> run_test (loop)
    """
    graph = StateGraph(AgentState)

    graph.add_node("generate_test", generate_test_node)
    graph.add_node("run_test", run_test_node)
    graph.add_node("fix_test", fix_test_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("generate_test")

    graph.add_edge("generate_test", "run_test")

    graph.add_conditional_edges(
        "run_test",
        should_retry,
        {
            "fix_test": "fix_test",
            "finalize": "finalize",
        },
    )

    graph.add_edge("fix_test", "run_test")
    graph.add_edge("finalize", END)

    return graph.compile()
