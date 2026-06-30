import sys

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    db_path: str = "dance.sqlite3"
    timezone: str = "Europe/Moscow"
    default_lead_hours: int = 18
    log_level: str = "INFO"
    developer_id: int = 0  # Telegram id allowed to grant admin status via /grant
    # Самостоятельный онбординг препода на пилоте: если задан, любой может стать
    # преподавателем командой `/claim <secret>` — без ручного /grant разработчиком.
    # Пусто = фича выключена. Меняй/чисти секрет между пилотами.
    owner_claim_secret: str = ""
    # Куда отсылать нового препода за доступом (напр. "@username" или ссылка).
    # Подставляется в приветствие нераспознанному пользователю. Пусто = дефолт.
    developer_contact: str = ""

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    def validate_runtime(self) -> None:
        """Stop early on missing/obviously-wrong values — лучше упасть с понятным
        сообщением при старте, чем загадочно молчать в polling-loop."""
        problems: list[str] = []
        if not self.bot_token or len(self.bot_token) < 20 or ":" not in self.bot_token:
            problems.append(
                "BOT_TOKEN пуст или некорректен — возьми у @BotFather и пропиши в .env"
            )
        if self.developer_id <= 0:
            problems.append(
                "DEVELOPER_ID не задан — без него никто не сможет получить роль "
                "админа через /grant. Пропиши свой Telegram user_id в .env"
            )
        if self.default_lead_hours <= 0:
            problems.append("DEFAULT_LEAD_HOURS должен быть > 0")
        if problems:
            raise RuntimeError("Конфиг невалиден:\n  - " + "\n  - ".join(problems))


try:
    settings = Settings()
except ValidationError as e:
    print(f"Не удалось прочитать .env: {e}", file=sys.stderr)
    raise
