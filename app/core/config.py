import sys
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    # Больше не строит DSN — Postgres обязателен (см. database_url ниже). Имя
    # оставлено для файлов состояния, не привязанных к БД: FSM-json (до Фазы 6)
    # и basename instance-lock файла.
    db_path: str = "dance.sqlite3"
    database_url: str
    timezone: str = "Europe/Moscow"
    default_lead_hours: int = 18
    log_level: str = "INFO"
    # Telegram id'ы, которым доступны /grant, /status, /devstats и dev-режим
    # (Phase 10) — CSV в .env (DEVELOPER_TG_IDS=111,222,...). Раньше был
    # единственный developer_id: int — несколько разработчиков не могли делить
    # доступ без расшаривания одного аккаунта.
    developer_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, validation_alias="DEVELOPER_TG_IDS"
    )

    @field_validator("developer_ids", mode="before")
    @classmethod
    def _parse_developer_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.split(",") if x.strip()]
        return v

    # Самостоятельный онбординг препода на пилоте: если задан, любой может стать
    # преподавателем командой `/claim <secret>` — без ручного /grant разработчиком.
    # Пусто = фича выключена. Меняй/чисти секрет между пилотами.
    owner_claim_secret: str = ""
    # Куда отсылать нового препода за доступом (напр. "@username" или ссылка).
    # Подставляется в приветствие нераспознанному пользователю. Пусто = дефолт.
    developer_contact: str = ""

    def validate_runtime(self) -> None:
        """Stop early on missing/obviously-wrong values — лучше упасть с понятным
        сообщением при старте, чем загадочно молчать в polling-loop."""
        problems: list[str] = []
        if not self.bot_token or len(self.bot_token) < 20 or ":" not in self.bot_token:
            problems.append(
                "BOT_TOKEN пуст или некорректен — возьми у @BotFather и пропиши в .env"
            )
        if not self.developer_ids:
            problems.append(
                "DEVELOPER_TG_IDS не задан — без него никто не сможет получить роль "
                "админа через /grant. Пропиши свой Telegram user_id в .env"
            )
        if self.default_lead_hours <= 0:
            problems.append("DEFAULT_LEAD_HOURS должен быть > 0")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            problems.append(f"TIMEZONE {self.timezone!r} — не существующая IANA-зона")
        if self.log_level.upper() not in _VALID_LOG_LEVELS:
            problems.append(
                f"LOG_LEVEL {self.log_level!r} — допустимо: {', '.join(sorted(_VALID_LOG_LEVELS))}"
            )
        if problems:
            raise RuntimeError("Конфиг невалиден:\n  - " + "\n  - ".join(problems))


try:
    settings = Settings()
except ValidationError as e:
    print(f"Не удалось прочитать .env: {e}", file=sys.stderr)
    raise
