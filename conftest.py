import os

# Set dummy values before any app module is imported during test collection.
# load_dotenv() (called inside app/config.py) does not override vars already in os.environ,
# so these dummies take effect in tests even if a real .env file exists.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
