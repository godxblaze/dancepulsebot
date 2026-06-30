"""Config for the live end-to-end harness — reads E2E_* from the same .env.

Kept separate from app.core.config so the bot never depends on test creds.
The app's Settings has extra="ignore", so these E2E_* keys don't affect it.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class E2ESettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="e2e_", extra="ignore"
    )

    api_id: int = 0  # с https://my.telegram.org → API development tools
    api_hash: str = ""
    session: str = ""  # StringSession, заполняется через e2e.live.login
    bot_username: str = "dancepulsebot"

    def require(self) -> None:
        """Падать с понятным текстом, если креды не заданы."""
        missing = [
            name
            for name, val in (
                ("E2E_API_ID", self.api_id),
                ("E2E_API_HASH", self.api_hash),
                ("E2E_SESSION", self.session),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "Не заданы " + ", ".join(missing) + " в .env. "
                "Сделай одноразовый логин: python -m e2e.live.login"
            )


e2e_settings = E2ESettings()
