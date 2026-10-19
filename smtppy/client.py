import asyncio
from collections.abc import Callable

from .config import clear_overrides, set_overrides, validate_smtp_settings
from .relay import dispatch_bulk, normalize_recipients


def configure(**overrides) -> None:
    settings_fields = {
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password",
        "smtp_from_email",
        "smtp_from_name",
        "smtp_use_tls",
        "smtp_use_ssl",
        "smtp_timeout_seconds",
        "rate_limit_per_second",
        "rate_limit_burst",
        "max_retries",
        "retry_base_delay_seconds",
        "database_path",
    }
    unknown = set(overrides) - settings_fields
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    set_overrides(overrides)


def reset_configuration() -> None:
    clear_overrides()


def _prepare(to: str | list[str], subject: str | None, body: str | None, html: str | None) -> tuple[list[str], str]:
    recipients = normalize_recipients(to)
    clean_subject = (subject or "").strip()
    if not clean_subject:
        raise ValueError("subject must not be empty")
    if not body and not html:
        raise ValueError("provide at least one of body or html")
    validate_smtp_settings()
    return recipients, clean_subject


def send_email(
    to: str | list[str],
    subject: str,
    body: str | None = None,
    html: str | None = None,
) -> dict:
    recipients, clean_subject = _prepare(to, subject, body, html)
    result = asyncio.run(dispatch_bulk(recipients, clean_subject, body, html))
    return {
        "ok": result["failed"] == 0 and result["sent"] > 0,
        "sent": result["sent"],
        "failed": result["failed"],
        "total": result["total"],
    }


async def send_email_async(
    to: str | list[str],
    subject: str,
    body: str | None = None,
    html: str | None = None,
) -> dict:
    recipients, clean_subject = _prepare(to, subject, body, html)
    result = await dispatch_bulk(recipients, clean_subject, body, html)
    return {
        "ok": result["failed"] == 0 and result["sent"] > 0,
        "sent": result["sent"],
        "failed": result["failed"],
        "total": result["total"],
    }


def send_bulk(
    recipients: str | list[str],
    subject: str,
    body: str | None = None,
    html: str | None = None,
    on_progress: Callable[[int, int, str, bool], None] | None = None,
) -> dict:
    to_list, clean_subject = _prepare(recipients, subject, body, html)
    return asyncio.run(dispatch_bulk(to_list, clean_subject, body, html, on_progress=on_progress))
