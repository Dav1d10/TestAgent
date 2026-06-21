from fastapi import FastAPI
from app.webhooks.github_webhook import router as webhook_router

app = FastAPI(title="TestAgent")
app.include_router(webhook_router, prefix="/webhook")
