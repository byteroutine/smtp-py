import asyncio
import re
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr

from .config import get_settings, validate_smtp_settings
from .database import claim_transmission, init_db, queue_email, update_status
from .limiter import TokenBucketRateLimiter

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TRANSIENT_SMTP_CODES = {421, 450, 451, 452}


def is_valid_email(address: str) -> bool:
    return bool(EMAIL_PATTERN.match(address.strip()))


def normalize_recipients(raw: str | list[str]) -> list[str]:
    if isinstance(raw, str):
        candidates = re.split(r"[,\n;]+", raw)
    else:
        candidates = []
        for item in raw:
            candidates.extend(re.split(r"[,\n;]+", str(item)))
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        email = candidate.strip().lower()
        if not email or email in seen:
            continue
        if not is_valid_email(email):
            raise ValueError(f"Invalid email address: {candidate.strip()}")
        seen.add(email)
        result.append(email)
    if not result:
        raise ValueError("No valid recipients provided")
    return result


def parse_recipients_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()
    return normalize_recipients(content)


def parse_compose_file(path: str) -> dict:
    recipients: list[str] = []
    subject = ""
    body_lines: list[str] = []
    html_lines: list[str] = []
    section = "head"
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            marker = stripped.upper()
            if section == "head":
                if marker.startswith("TO:"):
                    recipients.append(stripped[3:].strip())
                elif marker.startswith("SUBJECT:"):
                    subject = stripped[8:].strip()
                elif marker == "BODY:":
                    section = "body"
                elif stripped == "":
                    continue
                else:
                    raise ValueError(f"Unrecognized line in compose file: {stripped}")
            elif section == "body":
                if marker == "HTML:":
                    section = "html"
                else:
                    body_lines.append(line)
            else:
                html_lines.append(line)
    normalized = normalize_recipients(recipients) if recipients else []
    if not subject:
        raise ValueError("Compose file is missing a SUBJECT: line")
    body_text = "\n".join(body_lines).strip() or None
    body_html = "\n".join(html_lines).strip() or None
    if not body_text and not body_html:
        raise ValueError("Compose file has no BODY: content")
    return {
        "recipients": normalized,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
    }


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code in TRANSIENT_SMTP_CODES
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    message = str(exc).lower()
    transient_phrases = ("temporary failure", "try again", "service not available", "throttled")
    return any(phrase in message for phrase in transient_phrases)


def build_message(
    sender: str,
    recipient: str,
    subject: str,
    body_text: str | None,
    body_html: str | None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    if body_text:
        message.set_content(body_text)
    if body_html:
        if body_text:
            message.add_alternative(body_html, subtype="html")
        else:
            message.set_content(body_html, subtype="html")
    if not body_text and not body_html:
        message.set_content("")
    return message


def _send_sync(
    recipient: str,
    subject: str,
    body_text: str | None,
    body_html: str | None,
) -> None:
    settings = get_settings()
    sender = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message = build_message(sender, recipient, subject, body_text, body_html)
    if settings.smtp_use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        )
    else:
        server = smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        )
    try:
        server.ehlo()
        if settings.smtp_use_tls and not settings.smtp_use_ssl:
            server.starttls()
            server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass


def _smtp_error_text(exc: BaseException) -> str:
    code = getattr(exc, "smtp_code", "")
    raw = getattr(exc, "smtp_error", "")
    if isinstance(raw, bytes):
        detail = raw.decode("utf-8", errors="replace")
    else:
        detail = str(raw)
    combined = f"{code} {detail}".strip()
    return combined or str(exc)


def test_smtp_connection() -> dict:
    validate_smtp_settings()
    settings = get_settings()
    result: dict = {"connected": False, "authenticated": False, "error": None, "guidance": None}
    server = None
    try:
        if settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
        else:
            server = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            )
            server.ehlo()
            if settings.smtp_use_tls:
                server.starttls()
                server.ehlo()
        result["connected"] = True
        try:
            server.login(settings.smtp_username, settings.smtp_password)
            result["authenticated"] = True
        except smtplib.SMTPAuthenticationError as exc:
            result["error"] = f"Authentication failed ({_smtp_error_text(exc)})"
            result["guidance"] = (
                "The server rejected the username or password. "
                "Make sure SMTP_USERNAME is the full mailbox address and SMTP_PASSWORD "
                "is the mailbox password in your .env file. "
                "In Spacemail webmail, open Settings and enable IMAP/SMTP connections "
                "for this mailbox. If the account uses 2-step verification, "
                "create an app-specific password and use that instead. "
                "If the password contains special characters, keep it in the .env file "
                "rather than typing it into CMD."
            )
    except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        result["error"] = f"Connection failed: {type(exc).__name__}: {exc}"
        result["guidance"] = (
            "Could not reach the SMTP server at all. Check that SMTP_HOST is "
            "mail.spacemail.com, your firewall allows outbound traffic on port 465, "
            "and that you have a working internet connection."
        )
    except smtplib.SMTPException as exc:
        result["error"] = f"SMTP error: {type(exc).__name__}: {exc}"
        result["guidance"] = (
            "The server refused the connection. Double-check the host, port, "
            "and SSL settings in your .env file."
        )
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
    return result


async def send_single(
    recipient: str,
    subject: str,
    body_text: str | None,
    body_html: str | None,
    transmission_id: int,
    limiter: TokenBucketRateLimiter,
    database_path: str | None = None,
) -> bool:
    settings = get_settings()
    delays = settings.retry_delays()
    await limiter.acquire()
    if not claim_transmission(transmission_id, database_path):
        return False
    total_attempts = len(delays) + 1
    for attempt in range(total_attempts):
        if attempt > 0:
            await limiter.acquire()
            if not claim_transmission(transmission_id, database_path):
                return False
        try:
            await asyncio.to_thread(_send_sync, recipient, subject, body_text, body_html)
            update_status(
                transmission_id,
                "SENT",
                increment_attempts=True,
                database_path=database_path,
            )
            return True
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            last_attempt = attempt == total_attempts - 1
            should_retry = is_transient_error(exc) and not last_attempt
            if should_retry:
                update_status(
                    transmission_id,
                    "RETRYING",
                    last_error=error_text,
                    increment_attempts=True,
                    database_path=database_path,
                )
                await asyncio.sleep(delays[attempt])
                continue
            update_status(
                transmission_id,
                "FAILED",
                last_error=error_text,
                increment_attempts=True,
                database_path=database_path,
            )
            return False
    return False


async def dispatch_bulk(
    recipients: list[str],
    subject: str,
    body_text: str | None = None,
    body_html: str | None = None,
    database_path: str | None = None,
    on_progress=None,
) -> dict:
    settings = get_settings()
    resolved_path = database_path or settings.database_path
    init_db(resolved_path)
    limiter = TokenBucketRateLimiter(
        rate_per_second=settings.rate_limit_per_second,
        capacity=float(settings.rate_limit_burst),
    )
    queued: list[tuple[int, str]] = []
    for recipient in recipients:
        transmission_id = queue_email(
            recipient,
            subject,
            body_text,
            body_html,
            database_path=resolved_path,
        )
        queued.append((transmission_id, recipient))
    sent_count = 0
    failed_count = 0
    for index, (transmission_id, recipient) in enumerate(queued):
        ok = await send_single(
            recipient,
            subject,
            body_text,
            body_html,
            transmission_id,
            limiter,
            database_path=resolved_path,
        )
        if ok:
            sent_count += 1
        else:
            failed_count += 1
        if on_progress is not None:
            on_progress(index + 1, len(queued), recipient, ok)
    return {
        "total": len(queued),
        "sent": sent_count,
        "failed": failed_count,
        "database_path": resolved_path,
    }
