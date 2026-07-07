"""
config.py
=========
Central configuration loader.

All sensitive values come from environment variables (loaded from a local
`.env` file in development). Nothing secret is ever hard-coded.

Required env vars:
    AI_API_KEY            - API key for the AI provider (OpenAI or Gemini)
    Plus ONE Discord delivery method:
      * DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID   (bot delivery, preferred), or
      * DISCORD_WEBHOOK_URL                        (real .../api/webhooks/... URL)
    PRIMARY_API_KEY       - IPOGuru key  (GMP aggregator fallback — optional)
    FALLBACK_API_KEY      - IPOAlerts key (facts PRIMARY source + GMP fallback — optional)

FALLBACK_API_KEY doubles as the primary facts source (IPOAlerts aggregates
NSE+BSE) and, when the facts step already needed it, the GMP data comes along
for free on the same call. Both keys may be left blank — NSE/BSE (keyless) are
the fallback facts sources.

Optional env vars (sensible defaults provided):
    AI_PROVIDER           - "openai" (default) or "gemini"
    AI_MODEL              - model id (default depends on provider)
    LOOKAHEAD_DAYS        - how many days ahead to scan for IPOs (default 21)
    SCHEDULE_HOUR         - hour of day to run the daily check (default 8)
    SCHEDULE_MINUTE       - minute of the hour (default 0)
    TIMEZONE              - IANA tz name for the scheduler (default UTC)
    DB_PATH               - path to the SQLite state file (default state.db)
    RUN_ON_START          - "true"/"false"; run once immediately (default true)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load variables from a `.env` file if present. Real environment variables
# always take precedence over the file, which is what we want in production.
load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "claude": "haiku",  # uses local `claude -p` CLI; alias resolved by the CLI
}


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of runtime configuration."""

    # Secrets
    discord_webhook_url: str
    discord_bot_token: str
    discord_channel_id: str
    ai_api_key: str
    primary_api_key: str
    fallback_api_key: str

    # AI settings
    ai_provider: str
    ai_model: str

    # Behaviour
    lookahead_days: int
    schedule_hour: int
    schedule_minute: int
    timezone: str
    db_path: str
    run_on_start: bool


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Env var {name} must be an integer, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    """Read, validate and return the runtime configuration."""
    provider = _get("AI_PROVIDER", "openai").strip().lower()
    if provider not in _DEFAULT_MODELS:
        raise ConfigError(
            f"AI_PROVIDER must be one of {sorted(_DEFAULT_MODELS)}, got {provider!r}"
        )

    # Discord delivery: accept EITHER a bot (token + channel id) OR a real webhook.
    webhook_url = _get("DISCORD_WEBHOOK_URL", default="")
    bot_token = _get("DISCORD_BOT_TOKEN", default="")
    channel_id = _get("DISCORD_CHANNEL_ID", default="")
    has_bot = bool(bot_token and channel_id)
    has_webhook = "/api/webhooks/" in webhook_url
    if not (has_bot or has_webhook):
        raise ConfigError(
            "No valid Discord delivery configured. Provide either "
            "DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID, or a real DISCORD_WEBHOOK_URL "
            "(https://discord.com/api/webhooks/<id>/<token>). A "
            "https://discord.com/channels/... link is the channel URL, not a webhook."
        )

    return Config(
        discord_webhook_url=webhook_url,
        discord_bot_token=bot_token,
        discord_channel_id=channel_id,
        ai_api_key=_get("AI_API_KEY", required=True),
        # Both provider keys are optional now — they are only the GMP fallback.
        primary_api_key=_get("PRIMARY_API_KEY", default=""),
        fallback_api_key=_get("FALLBACK_API_KEY", default=""),
        ai_provider=provider,
        ai_model=_get("AI_MODEL", _DEFAULT_MODELS[provider]),
        lookahead_days=_get_int("LOOKAHEAD_DAYS", 21),
        schedule_hour=_get_int("SCHEDULE_HOUR", 8),
        schedule_minute=_get_int("SCHEDULE_MINUTE", 0),
        timezone=_get("TIMEZONE", "UTC"),
        db_path=_get("DB_PATH", "state.db"),
        run_on_start=_get_bool("RUN_ON_START", True),
    )
