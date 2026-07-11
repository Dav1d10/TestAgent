import app.config  # noqa: F401 — triggers load_dotenv() before OpenAI() is instantiated
from fastapi import FastAPI
from app.webhooks.github_webhook import router as webhook_router

app = FastAPI(title="TestAgent")
app.include_router(webhook_router, prefix="/webhook")
