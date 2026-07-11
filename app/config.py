"""
Centralized environment variable loading.
All modules import settings from here instead of calling os.getenv directly.
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))
