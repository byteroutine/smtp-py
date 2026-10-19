import os
import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_TOKEN_LENGTH = 32

ENV_FILE_CANDIDATES = (".env", ".env.smtpserver")


def resolve_env_file() -> str:
    for candidate in ENV_FILE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ENV_FILE_CANDIDATES[0]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=465)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from_email: str = Field(default="")
    smtp_from_name: str = Field(default="")
    smtp_use_tls: bool = Field(default=False)
    smtp_use_ssl: bool = Field(default=True)
    smtp_timeout_seconds: int = Field(default=30)

    api_bearer_token: str | None = Field(default=None)

    rate_limit_per_second: float = Field(default=5.0)
    rate_limit_burst: int = Field(default=5)
    max_retries: int = Field(default=4)
    retry_base_delay_seconds: float = Field(default=1.0)

    database_path: str = Field(default="smtppy.db")

    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000)

    @field_validator("smtp_port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        return value

    @field_validator("rate_limit_per_second")
    @classmethod
    def validate_rate(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("rate_limit_per_second must be greater than 0")
        return value

    @field_validator("max_retries")
    @classmethod
    def validate_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_retries cannot be negative")
        return value

    @field_validator("retry_base_delay_seconds")
    @classmethod
    def validate_delay(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("retry_base_delay_seconds must be greater than 0")
        return value

    @property
    def smtp_from_header(self) -> str:
        name = self.smtp_from_name.strip()
        email = self.smtp_from_email.strip()
        if name:
            return f"{name} <{email}>"
        return email

    def retry_delays(self) -> list[float]:
        return [self.retry_base_delay_seconds * (2.0**attempt) for attempt in range(self.max_retries)]


_overrides: dict = {}


def set_overrides(overrides: dict) -> None:
    _overrides.clear()
    _overrides.update(overrides)
    get_settings.cache_clear()


def clear_overrides() -> None:
    _overrides.clear()
    get_settings.cache_clear()


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=resolve_env_file(), **_overrides)


def env_file_info() -> dict:
    resolved = resolve_env_file()
    path = os.path.abspath(resolved)
    exists = os.path.isfile(resolved)
    return {
        "path": path,
        "exists": exists,
        "size": os.path.getsize(path) if exists else 0,
        "candidates": [os.path.abspath(candidate) for candidate in ENV_FILE_CANDIDATES],
    }


def _env_hint() -> str:
    info = env_file_info()
    names = " or ".join(ENV_FILE_CANDIDATES)
    if not info["exists"]:
        return f" No {names} file was found (looked in {info['path']})."
    return f" (checked {info['path']})"


def validate_smtp_settings(settings: Settings | None = None) -> None:
    checked = settings or get_settings()
    problems: list[str] = []
    host = checked.smtp_host.strip()
    if not host or host == "smtp.example.com":
        problems.append(
            "SMTP_HOST is not configured. Set SMTP_HOST to your provider's SMTP server "
            "in your .env or .env.smtpserver file."
        )
    if not checked.smtp_username.strip():
        problems.append(
            "SMTP_USERNAME is empty." + _env_hint() + " Set it to your full mailbox address."
        )
    if not checked.smtp_password:
        problems.append(
            "SMTP_PASSWORD is empty." + _env_hint() + " Put the mailbox password in the env file. It is never printed or committed."
        )
    if not EMAIL_PATTERN.match(checked.smtp_from_email.strip()):
        problems.append(
            f"SMTP_FROM_EMAIL '{checked.smtp_from_email}' is not a valid email address."
        )
    if checked.smtp_use_tls and checked.smtp_use_ssl:
        problems.append(
            "SMTP_USE_TLS and SMTP_USE_SSL cannot both be true. For port 465 use implicit SSL only: "
            "set SMTP_USE_SSL=true and SMTP_USE_TLS=false."
        )
    if checked.smtp_port == 465 and not checked.smtp_use_ssl:
        problems.append(
            "Port 465 requires implicit SSL. Set SMTP_USE_SSL=true and SMTP_USE_TLS=false."
        )
    if checked.smtp_port == 25 and checked.smtp_use_ssl:
        problems.append(
            "Port 25 does not support implicit SSL. Use port 465 with SSL or port 587 with STARTTLS."
        )
    if problems:
        raise ValueError("SMTP configuration error:\n- " + "\n- ".join(problems))


def require_api_token(settings: Settings | None = None) -> str:
    checked = settings or get_settings()
    token = (checked.api_bearer_token or "").strip()
    if not token:
        raise ValueError(
            "API_BEARER_TOKEN is not set." + _env_hint() + " Generate a strong token with "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
            "and add it to your env file as API_BEARER_TOKEN."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"API_BEARER_TOKEN is too short ({len(token)} characters). "
            f"It must be at least {MIN_TOKEN_LENGTH} characters. Generate a new one with "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\"."
        )
    return token
