import pytest

import smtppy
from smtppy import client, configure, reset_configuration, send_bulk, send_email
from smtppy.config import clear_overrides, get_settings, resolve_env_file
from smtppy import relay as relay_module


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM_EMAIL",
        "SMTP_FROM_NAME",
        "DATABASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_overrides()
    yield tmp_path
    clear_overrides()


def valid_config(db):
    return dict(
        smtp_host="mail.example.com",
        smtp_port=465,
        smtp_username="you@example.com",
        smtp_password="secret",
        smtp_from_email="you@example.com",
        smtp_from_name="Tester",
        smtp_use_tls=False,
        smtp_use_ssl=True,
        rate_limit_per_second=1000.0,
        rate_limit_burst=1000,
        retry_base_delay_seconds=0.01,
        max_retries=1,
        database_path=db,
    )


def test_version():
    assert smtppy.__version__ == "1.0.1"


def test_public_api_exports():
    for name in ("send_email", "send_email_async", "send_bulk", "configure", "get_settings"):
        assert callable(getattr(smtppy, name)), name


def test_send_email_success(clean, monkeypatch):
    db = str(clean / "t.db")
    configure(**valid_config(db))
    calls = []
    monkeypatch.setattr(relay_module, "_send_sync", lambda *a: calls.append(a))
    result = send_email(to="friend@example.com", subject="Hello", body="Hi there")
    assert result == {"ok": True, "sent": 1, "failed": 0, "total": 1}
    assert len(calls) == 1


def test_send_email_accepts_list_and_html(clean, monkeypatch):
    db = str(clean / "t.db")
    configure(**valid_config(db))
    monkeypatch.setattr(relay_module, "_send_sync", lambda *a: None)
    result = send_email(
        to=["a@example.com", "b@example.com"],
        subject="Hi",
        body="text",
        html="<p>html</p>",
    )
    assert result["ok"] is True
    assert result["total"] == 2


def test_send_email_bad_recipient(clean):
    configure(**valid_config(str(clean / "t.db")))
    with pytest.raises(ValueError, match="Invalid email"):
        send_email(to="not-an-email", subject="Hi", body="x")


def test_send_email_empty_subject(clean):
    configure(**valid_config(str(clean / "t.db")))
    with pytest.raises(ValueError, match="subject"):
        send_email(to="a@example.com", subject="  ", body="x")


def test_send_email_no_body(clean):
    configure(**valid_config(str(clean / "t.db")))
    with pytest.raises(ValueError, match="body or html"):
        send_email(to="a@example.com", subject="Hi")


def test_send_email_missing_config_raises_helpful_error(clean):
    with pytest.raises(ValueError, match="SMTP_HOST"):
        send_email(to="a@example.com", subject="Hi", body="x")


def test_configure_rejects_unknown_settings(clean):
    with pytest.raises(ValueError, match="Unknown setting"):
        configure(nonsense_option="x")


def test_send_bulk_report(clean, monkeypatch):
    db = str(clean / "t.db")
    configure(**valid_config(db))
    monkeypatch.setattr(relay_module, "_send_sync", lambda *a: None)
    seen = []
    report = send_bulk(
        ["a@example.com", "b@example.com"],
        subject="News",
        body="hello",
        on_progress=lambda done, total, recipient, ok: seen.append((done, total, ok)),
    )
    assert report["sent"] == 2
    assert report["failed"] == 0
    assert seen == [(1, 2, True), (2, 2, True)]


def test_env_smtpserver_fallback(clean):
    (clean / ".env.smtpserver").write_text("SMTP_HOST=smtp.fallback.test\nSMTP_PASSWORD=secret\n")
    get_settings.cache_clear()
    assert resolve_env_file() == ".env.smtpserver"
    settings = get_settings()
    assert settings.smtp_host == "smtp.fallback.test"


def test_dotenv_wins_over_smtpserver_file(clean):
    (clean / ".env").write_text("SMTP_HOST=smtp.dotenv.test\n")
    (clean / ".env.smtpserver").write_text("SMTP_HOST=smtp.fallback.test\n")
    get_settings.cache_clear()
    assert resolve_env_file() == ".env"
    assert get_settings().smtp_host == "smtp.dotenv.test"


def test_send_email_async_variant(clean, monkeypatch):
    import asyncio

    db = str(clean / "t.db")
    configure(**valid_config(db))
    monkeypatch.setattr(relay_module, "_send_sync", lambda *a: None)
    result = asyncio.run(client.send_email_async(to="a@example.com", subject="Hi", body="x"))
    assert result["ok"] is True
