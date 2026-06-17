"""
Centralized environment variable loading. (Stage 3)
All other modules should import settings from here instead of calling os.getenv directly.
"""

import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
