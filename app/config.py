from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_local_env(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    hfd_base_url: str = "https://dash.hfd.fund"
    database_url: str = "sqlite+aiosqlite:///./data/hfd.db"
    redis_url: str = ""
    raw_payload_dir: str = "./data/raw_payloads"
    externalize_raw_payloads: bool = False
    http_timeout_seconds: float = 20.0
    collector_user_agent: str = "HFDResearchBot/0.1"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    live_trading_enabled: bool = False
    trading_gateway: str = "disabled"


def get_settings() -> Settings:
    load_local_env()
    return Settings(
        hfd_base_url=os.getenv("HFD_BASE_URL", Settings.hfd_base_url).rstrip("/"),
        database_url=os.getenv("DATABASE_URL", Settings.database_url),
        redis_url=os.getenv("REDIS_URL", Settings.redis_url),
        raw_payload_dir=os.getenv("RAW_PAYLOAD_DIR", Settings.raw_payload_dir),
        externalize_raw_payloads=os.getenv("EXTERNALIZE_RAW_PAYLOADS", "false").lower() in {"1", "true", "yes", "on"},
        http_timeout_seconds=float(
            os.getenv("HTTP_TIMEOUT_SECONDS", str(Settings.http_timeout_seconds))
        ),
        collector_user_agent=os.getenv(
            "COLLECTOR_USER_AGENT", Settings.collector_user_agent
        ),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        live_trading_enabled=os.getenv("LIVE_TRADING_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
        trading_gateway=os.getenv("TRADING_GATEWAY", Settings.trading_gateway),
    )
