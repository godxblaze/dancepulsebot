from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    db_path: str = "dance.sqlite3"
    timezone: str = "Europe/Moscow"
    default_lead_hours: int = 18
    log_level: str = "INFO"
    developer_id: int = 0  # Telegram id allowed to grant teacher status via /grant

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()
