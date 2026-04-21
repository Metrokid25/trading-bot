"""환경변수(.env) 기반 설정 로더.

실제 API 키는 절대 코드에 하드코딩하지 말고 .env 파일을 사용합니다.
.env 파일은 .gitignore 로 제외되며, 템플릿은 .env.example 참고.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- KIS API ---
    KIS_ENV: Literal["REAL", "PAPER"] = "PAPER"

    KIS_REAL_APP_KEY: str = ""
    KIS_REAL_APP_SECRET: str = ""
    KIS_REAL_ACCOUNT_NO: str = ""

    KIS_PAPER_APP_KEY: str = ""
    KIS_PAPER_APP_SECRET: str = ""
    KIS_PAPER_ACCOUNT_NO: str = ""

    KIS_REAL_BASE_URL: str = "https://openapi.koreainvestment.com:9443"
    KIS_PAPER_BASE_URL: str = "https://openapivts.koreainvestment.com:29443"
    KIS_REAL_WS_URL: str = "ws://ops.koreainvestment.com:21000"
    KIS_PAPER_WS_URL: str = "ws://ops.koreainvestment.com:31000"

    KIS_HTS_ID: str = ""

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_ALERT_CHAT_ID: str = ""
    # 허용된 사용자 ID (CSV: "12345,67890"). 빈 리스트면 모든 사용자 거부(안전 기본값).
    ALLOWED_TELEGRAM_USERS: Annotated[list[int], NoDecode] = []

    @field_validator("ALLOWED_TELEGRAM_USERS", mode="before")
    @classmethod
    def _parse_allowed_users(cls, v):
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    # --- Trading params ---
    TOTAL_SEED: int = 10_000_000
    MAX_CONCURRENT_POSITIONS: int = 3
    DAILY_LOSS_LIMIT_PCT: float = -10.0
    STOP_LOSS_PCT: float = -5.0

    # --- Infra ---
    LOG_LEVEL: str = "INFO"
    LOG_DIR: Path = PROJECT_ROOT / "logs"
    DB_PATH: Path = PROJECT_ROOT / "db" / "trading.db"

    # --- Computed: 현재 모드에 맞는 키 자동 선택 ---
    @computed_field  # type: ignore[misc]
    @property
    def app_key(self) -> str:
        return self.KIS_REAL_APP_KEY if self.KIS_ENV == "REAL" else self.KIS_PAPER_APP_KEY

    @computed_field  # type: ignore[misc]
    @property
    def app_secret(self) -> str:
        return self.KIS_REAL_APP_SECRET if self.KIS_ENV == "REAL" else self.KIS_PAPER_APP_SECRET

    @computed_field  # type: ignore[misc]
    @property
    def account_no(self) -> str:
        return self.KIS_REAL_ACCOUNT_NO if self.KIS_ENV == "REAL" else self.KIS_PAPER_ACCOUNT_NO

    @computed_field  # type: ignore[misc]
    @property
    def base_url(self) -> str:
        return self.KIS_REAL_BASE_URL if self.KIS_ENV == "REAL" else self.KIS_PAPER_BASE_URL

    @computed_field  # type: ignore[misc]
    @property
    def ws_url(self) -> str:
        return self.KIS_REAL_WS_URL if self.KIS_ENV == "REAL" else self.KIS_PAPER_WS_URL

    def ensure_dirs(self) -> None:
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
