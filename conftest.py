import os

# Prevent `client = OpenAI()` in nodes.py from raising at import time during tests.
# The real key (if present in .env) takes precedence; this only fires when unset.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
