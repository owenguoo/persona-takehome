"""Runtime settings, read from .env at the repo root.

Re-read on every call so a key pasted into .env works without a restart.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
ENV_FILE = ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    text_model: str
    realtime_model: str
    voice: str


def settings() -> Settings:
    load_dotenv(ENV_FILE, override=True)
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        text_model=os.getenv("OPENAI_TEXT_MODEL", "gpt-4.1-mini").strip(),
        realtime_model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1").strip(),
        voice=os.getenv("OPENAI_VOICE", "marin").strip(),
    )
