# TestAgent

Autonomous agent that generates and self-corrects unit tests in response to git events — no human intervention required in the loop.

## How it works

1. A git event (push / pull request) triggers the agent via a webhook.
2. The agent analyzes the diff and decides which functions need tests.
3. It generates pytest unit tests using an LLM (GPT-4o).
4. Tests run inside an isolated Docker sandbox — never on the host.
5. If a test fails, the agent enters a self-correction loop: it reads the error, fixes the test, and retries (up to `max_attempts`).
6. Once tests pass, the agent opens a Pull Request automatically.

## Architecture

```
app/
├── agent/          # LangGraph graph: state, nodes, prompts
├── tools/          # Sandbox runner, GitHub client, diff parser
└── webhooks/       # GitHub webhook receiver (Stage 3)

sandbox/            # Isolated Docker image for running generated tests
scripts/            # Local runner for Stage 1 validation
tests/              # Tests for the agent itself
```

## Quick start (Stage 1 — local validation)

**Prerequisites:** Python 3.11+, Docker Desktop running.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
cp .env.example .env
# edit .env and add your OPENAI_API_KEY

# 3. Build the sandbox image (once)
docker build -t test-agent-sandbox:latest sandbox/

# 4. Run the agent against the example function
python scripts/run_local.py
```

## Stages

| Stage | Description | Status |
|-------|-------------|--------|
| 1 | Core: LangGraph graph + Docker sandbox | Code complete, pending end-to-end validation |
| 2 | Robustness: agent's own test suite | Not started |
| 3 | Autonomy: FastAPI webhook receiver | Not started |
| 4 | GitHub integration: diff parser + PR opener | Not started |

## Key design decisions

- **LangGraph** as orchestrator to model the retry loop as a conditional graph edge.
- **Docker sandbox** for every test run — LLM-generated code is untrusted by definition.
- **GPT-4o** for reasoning quality; the task (code analysis, edge-case generation, error correction) needs the full model.
- **Agent never modifies source code** — only the test is adjusted during self-correction.
- **`max_attempts` default: 3** — prevents infinite correction loops.
