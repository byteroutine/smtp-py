import asyncio
import os
import smtplib
import time

import pytest

from smtppy import config
from smtppy import database
from smtppy import limiter
from smtppy import cli as main
from smtppy import menu
from smtppy import relay
from smtppy.config import Settings, get_settings, require_api_token, validate_smtp_settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("SMTP_HOST", "mail.spacemail.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USERNAME", "team@cvbinder.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-pw")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "team@cvbinder.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "CVBinder Team")
    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.setenv("SMTP_USE_SSL", "true")
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("API_BEARER_TOKEN", "t" * 32)
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "8000")
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def fast_settings(db, **overrides):
    base = dict(
        retry_base_delay_seconds=0.01,
        max_retries=2,
        rate_limit_per_second=1000.0,
        rate_limit_burst=1000,
        database_path=db,
    )
    base.update(overrides)
    return Settings(**base)


class TestConfigValidation:
    def test_spacemail_defaults_load(self, env):
        settings = get_settings()
        assert settings.smtp_host == "mail.spacemail.com"
        assert settings.smtp_port == 465
        assert settings.smtp_use_ssl is True
        assert settings.smtp_use_tls is False
        assert settings.smtp_timeout_seconds == 30
        assert settings.app_host == "127.0.0.1"
        assert settings.app_port == 8000
        validate_smtp_settings(settings)

    def test_missing_password_rejected(self, env):
        settings = Settings(smtp_password="")
        with pytest.raises(ValueError, match="SMTP_PASSWORD"):
            validate_smtp_settings(settings)

    def test_missing_username_rejected(self, env):
        settings = Settings(smtp_username="  ")
        with pytest.raises(ValueError, match="SMTP_USERNAME"):
            validate_smtp_settings(settings)

    def test_port_465_requires_ssl(self, env):
        settings = Settings(smtp_port=465, smtp_use_ssl=False, smtp_use_tls=False)
        with pytest.raises(ValueError, match="Port 465 requires implicit SSL"):
            validate_smtp_settings(settings)

    def test_tls_and_ssl_conflict(self, env):
        settings = Settings(smtp_use_tls=True, smtp_use_ssl=True)
        with pytest.raises(ValueError, match="cannot both be true"):
            validate_smtp_settings(settings)

    def test_bad_from_address_rejected(self, env):
        settings = Settings(smtp_from_email="not-an-email")
        with pytest.raises(ValueError, match="SMTP_FROM_EMAIL"):
            validate_smtp_settings(settings)

    def test_no_default_api_token(self, monkeypatch):
        monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
        get_settings.cache_clear()
        try:
            settings = Settings()
            assert settings.api_bearer_token is None
            with pytest.raises(ValueError, match="API_BEARER_TOKEN is not set"):
                require_api_token(settings)
        finally:
            get_settings.cache_clear()

    def test_short_token_rejected(self, env):
        settings = Settings(api_bearer_token="short")
        with pytest.raises(ValueError, match="too short"):
            require_api_token(settings)

    def test_strong_token_accepted(self, env):
        assert require_api_token() == "t" * 32

    def test_retry_schedule(self, env):
        assert get_settings().retry_delays() == [1.0, 2.0, 4.0, 8.0]


class TestSmtpSslWiring:
    def test_port_465_uses_implicit_ssl(self, env, monkeypatch):
        seen = {}

        class FakeSSL:
            def __init__(self, host, port, timeout=None):
                seen["host"] = host
                seen["port"] = port
                seen["timeout"] = timeout

            def ehlo(self):
                pass

            def login(self, user, password):
                seen["login"] = (user, password)

            def send_message(self, message):
                seen["sent"] = True

            def quit(self):
                pass

        def boom(*args, **kwargs):
            raise AssertionError("plain SMTP must not be used on port 465")

        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings("x", smtp_password="pw"))
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
        monkeypatch.setattr(smtplib, "SMTP", boom)
        relay._send_sync("a@example.com", "Sub", "body", None)
        assert seen["host"] == "mail.spacemail.com"
        assert seen["port"] == 465
        assert seen["login"] == ("team@cvbinder.com", "pw")
        assert seen["sent"] is True


class TestSmtpConnectionCheck:
    def test_reports_success(self, env, monkeypatch):
        class FakeSSL:
            def __init__(self, *args, **kwargs):
                pass

            def login(self, user, password):
                pass

            def quit(self):
                pass

        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
        result = relay.test_smtp_connection()
        assert result["connected"] is True
        assert result["authenticated"] is True
        assert result["error"] is None

    def test_reports_auth_failure_with_guidance(self, env, monkeypatch):
        class FakeSSL:
            def __init__(self, *args, **kwargs):
                pass

            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

            def quit(self):
                pass

        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
        result = relay.test_smtp_connection()
        assert result["connected"] is True
        assert result["authenticated"] is False
        assert "535" in result["error"]
        assert "password" in result["guidance"].lower()

    def test_reports_unreachable_with_guidance(self, env, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("network unreachable")

        monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
        result = relay.test_smtp_connection()
        assert result["connected"] is False
        assert "firewall" in result["guidance"].lower()

    def test_refuses_without_valid_config(self, env, monkeypatch):
        def bad_config():
            raise ValueError("SMTP_PASSWORD is empty")

        monkeypatch.setattr(relay, "validate_smtp_settings", bad_config)
        with pytest.raises(ValueError, match="SMTP_PASSWORD"):
            relay.test_smtp_connection()


class TestRelayLogic:
    def test_normalize_recipients(self):
        assert relay.normalize_recipients("A@x.com, b@y.com\nA@x.com") == ["a@x.com", "b@y.com"]
        with pytest.raises(ValueError):
            relay.normalize_recipients("not-an-email")

    def test_parse_compose_file(self, tmp_path):
        path = tmp_path / "writemail.txt"
        path.write_text(
            "TO: alice@example.com\n"
            "TO: bob@example.com\n"
            "SUBJECT: Hello\n"
            "BODY:\n"
            "First line\n"
            "Second line\n"
            "HTML:\n"
            "<p>Hi</p>\n",
            encoding="utf-8",
        )
        data = relay.parse_compose_file(str(path))
        assert data["recipients"] == ["alice@example.com", "bob@example.com"]
        assert data["subject"] == "Hello"
        assert "First line" in data["body_text"]
        assert data["body_html"] == "<p>Hi</p>"

    def test_parse_compose_file_missing_subject(self, tmp_path):
        path = tmp_path / "bad.txt"
        path.write_text("TO: a@example.com\nBODY:\nhi\n", encoding="utf-8")
        with pytest.raises(ValueError, match="SUBJECT"):
            relay.parse_compose_file(str(path))

    def test_parse_compose_file_without_recipients(self, tmp_path):
        path = tmp_path / "writemail.txt"
        path.write_text(
            "SUBJECT: Hello\nBODY:\nFirst line\nHTML:\n<p>Hi</p>\n",
            encoding="utf-8",
        )
        data = relay.parse_compose_file(str(path))
        assert data["recipients"] == []
        assert data["subject"] == "Hello"
        assert "First line" in data["body_text"]
        assert data["body_html"] == "<p>Hi</p>"

    def test_parse_compose_file_recipients_still_supported(self, tmp_path):
        path = tmp_path / "legacy.txt"
        path.write_text(
            "TO: alice@example.com\nSUBJECT: Hi\nBODY:\nhello\n",
            encoding="utf-8",
        )
        data = relay.parse_compose_file(str(path))
        assert data["recipients"] == ["alice@example.com"]

    def test_transient_classification(self):
        assert relay.is_transient_error(smtplib.SMTPResponseException(421, b"temp")) is True
        assert relay.is_transient_error(smtplib.SMTPResponseException(451, b"temp")) is True
        assert relay.is_transient_error(smtplib.SMTPResponseException(550, b"nope")) is False
        assert relay.is_transient_error(smtplib.SMTPServerDisconnected()) is True

    def test_send_success(self, env, monkeypatch):
        database.init_db(env)
        calls = []
        monkeypatch.setattr(relay, "_send_sync", lambda *a: calls.append(a))
        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings(env))

        async def run():
            tid = database.queue_email("a@example.com", "S", "b", database_path=env)
            lim = limiter.TokenBucketRateLimiter(1000, 1000)
            return await relay.send_single("a@example.com", "S", "b", None, tid, lim, env)

        assert asyncio.run(run()) is True
        assert len(calls) == 1
        assert database.get_stats(env)["SENT"] == 1

    def test_transient_then_success(self, env, monkeypatch):
        database.init_db(env)
        calls = []

        def flaky(*args):
            calls.append(1)
            if len(calls) < 3:
                raise smtplib.SMTPResponseException(421, b"temporary failure")

        monkeypatch.setattr(relay, "_send_sync", flaky)
        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings(env))

        async def run():
            tid = database.queue_email("a@example.com", "S", "b", database_path=env)
            lim = limiter.TokenBucketRateLimiter(1000, 1000)
            return await relay.send_single("a@example.com", "S", "b", None, tid, lim, env)

        assert asyncio.run(run()) is True
        assert len(calls) == 3

    def test_permanent_failure_no_retry(self, env, monkeypatch):
        database.init_db(env)
        calls = []

        def dead(*args):
            calls.append(1)
            raise smtplib.SMTPResponseException(550, b"mailbox unavailable")

        monkeypatch.setattr(relay, "_send_sync", dead)
        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings(env))

        async def run():
            tid = database.queue_email("a@example.com", "S", "b", database_path=env)
            lim = limiter.TokenBucketRateLimiter(1000, 1000)
            return await relay.send_single("a@example.com", "S", "b", None, tid, lim, env)

        assert asyncio.run(run()) is False
        assert len(calls) == 1
        row = database.fetch_by_status("FAILED", database_path=env)[0]
        assert "550" in (row["last_error"] or "")

    def test_rate_limiter_throttles(self):
        bucket = limiter.TokenBucketRateLimiter(rate_per_second=5.0, capacity=5.0)

        async def drain():
            for _ in range(6):
                await bucket.acquire()

        start = time.monotonic()
        asyncio.run(drain())
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15
        assert elapsed < 3.0

    def test_claim_prevents_double_send(self, env, monkeypatch):
        database.init_db(env)
        calls = []
        monkeypatch.setattr(relay, "_send_sync", lambda *a: calls.append(a))
        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings(env))
        tid = database.queue_email("a@example.com", "S", "b", database_path=env)
        assert database.claim_transmission(tid, env) is True
        assert database.claim_transmission(tid, env) is False

        async def run():
            lim = limiter.TokenBucketRateLimiter(1000, 1000)
            return await relay.send_single("a@example.com", "S", "b", None, tid, lim, env)

        assert asyncio.run(run()) is False
        assert calls == []

    def test_dispatch_bulk_summary(self, env, monkeypatch):
        def fake_send(recipient, subject, body_text, body_html):
            if recipient == "bad@example.com":
                raise smtplib.SMTPResponseException(550, b"nope")

        monkeypatch.setattr(relay, "_send_sync", fake_send)
        monkeypatch.setattr(relay, "get_settings", lambda: fast_settings(env))
        result = asyncio.run(
            relay.dispatch_bulk(
                ["a@example.com", "bad@example.com"],
                "Sub",
                "body",
                database_path=env,
            )
        )
        assert result == {"total": 2, "sent": 1, "failed": 1, "database_path": env}


class TestDatabase:
    def test_stats_and_recent(self, env):
        database.init_db(env)
        first = database.queue_email("a@example.com", "One", "b", database_path=env)
        database.queue_email("b@example.com", "Two", "b", database_path=env)
        database.update_status(first, "SENT", increment_attempts=True, database_path=env)
        stats = database.get_stats(env)
        assert stats["SENT"] == 1
        assert stats["QUEUED"] == 1
        assert stats["TOTAL"] == 2
        recent = database.fetch_recent(10, database_path=env)
        assert recent[0]["recipient"] == "b@example.com"

    def test_retry_requeue(self, env):
        database.init_db(env)
        tid = database.queue_email("a@example.com", "S", "b", database_path=env)
        database.update_status(tid, "FAILED", last_error="boom", increment_attempts=True, database_path=env)
        assert database.reset_retryable(env) == 1
        assert database.get_stats(env)["QUEUED"] == 1


class _FakePrompt:
    answers = []

    @classmethod
    def ask(cls, *args, **kwargs):
        return cls.answers.pop(0)


class _FakeConfirm:
    value = False

    @classmethod
    def ask(cls, *args, **kwargs):
        return cls.value


class TestMenu:
    def test_invalid_selection_then_exit(self, monkeypatch, capsys):
        _FakePrompt.answers = ["9", "", "0"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        menu.run_menu()
        out = capsys.readouterr().out
        assert "Invalid selection" in out
        assert "0 to 8" in out
        assert "Goodbye" in out

    def test_exit_directly(self, monkeypatch, capsys):
        _FakePrompt.answers = ["0"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        menu.run_menu()
        assert "Goodbye" in capsys.readouterr().out

    def test_stats_action(self, env, capsys):
        database.init_db(env)
        database.queue_email("a@example.com", "S", "b", database_path=env)
        menu.action_stats()
        out = capsys.readouterr().out
        assert "QUEUED" in out

    def test_recent_action(self, env, monkeypatch, capsys):
        database.init_db(env)
        database.queue_email("zed@example.com", "Hello subject", "b", database_path=env)
        _FakePrompt.answers = ["5"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        menu.action_recent()
        out = capsys.readouterr().out
        assert "zed@example.com" in out
        assert "Hello subject" in out

    def test_retry_action_confirmed(self, env, monkeypatch, capsys):
        database.init_db(env)
        tid = database.queue_email("a@example.com", "S", "b", database_path=env)
        database.update_status(tid, "FAILED", database_path=env)
        _FakeConfirm.value = True
        monkeypatch.setattr(menu, "Confirm", _FakeConfirm)
        menu.action_retry()
        assert "Requeued 1" in capsys.readouterr().out
        assert database.get_stats(env)["QUEUED"] == 1

    def test_retry_action_declined(self, env, monkeypatch, capsys):
        database.init_db(env)
        tid = database.queue_email("a@example.com", "S", "b", database_path=env)
        database.update_status(tid, "FAILED", database_path=env)
        _FakeConfirm.value = False
        monkeypatch.setattr(menu, "Confirm", _FakeConfirm)
        menu.action_retry()
        assert "Cancelled" in capsys.readouterr().out
        assert database.get_stats(env)["FAILED"] == 1

    def test_send_action_compose_file(self, env, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "writemail.txt").write_text(
            "SUBJECT: Hi\nBODY:\nhello there\nHTML:\n<p>hi</p>\n",
            encoding="utf-8",
        )
        _FakePrompt.answers = ["1", "alice@example.com", "3"]
        _FakeConfirm.value = True
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        monkeypatch.setattr(menu, "Confirm", _FakeConfirm)
        recorded = {}

        def fake_send_batch(r, s, t, h=None):
            recorded.update({"recipients": r, "subject": s, "text": t, "html": h})
            return {"total": len(r), "sent": len(r), "failed": 0}

        monkeypatch.setattr(menu, "send_batch", fake_send_batch)
        menu.action_send()
        assert recorded["recipients"] == ["alice@example.com"]
        assert recorded["subject"] == "Hi"
        assert recorded["text"] == "hello there"
        assert recorded["html"] == "<p>hi</p>"

    def test_send_action_emails_txt_autoload(self, env, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "emails.txt").write_text("bob@example.com\n", encoding="utf-8")
        _FakePrompt.answers = ["2", "1", "TestSub", "", ""]
        _FakeConfirm.value = True
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        monkeypatch.setattr(menu, "Confirm", _FakeConfirm)
        recorded = {}

        def fake_send_batch(r, s, t, h=None):
            recorded.update({"recipients": r, "subject": s, "text": t, "html": h})
            return {"total": len(r), "sent": len(r), "failed": 0}

        monkeypatch.setattr(menu, "send_batch", fake_send_batch)
        menu.action_send()
        assert recorded["recipients"] == ["bob@example.com"]
        assert recorded["subject"] == "TestSub"

    def test_send_action_missing_emails_txt(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        _FakePrompt.answers = ["2"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        menu.action_send()
        assert "emails.txt not found" in capsys.readouterr().out

    def test_menu_option_eight_shows_about(self, monkeypatch, capsys):
        _FakePrompt.answers = ["8", "", "0"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        menu.run_menu()
        out = capsys.readouterr().out
        assert "github.com/byteroutine/smtp-py" in out
        assert "Developed by ByteRoutine" in out
        assert "Get the source:" in out
        assert "MIT License" in out

    def test_menu_option_six_runs_smtp_test(self, monkeypatch, capsys):
        _FakePrompt.answers = ["6", "", "0"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        called = []
        monkeypatch.setattr(menu, "action_test_smtp", lambda: called.append(True))
        menu.run_menu()
        assert called == [True]
        assert "Goodbye" in capsys.readouterr().out

    def test_menu_option_seven_runs_diagnose(self, monkeypatch, capsys):
        _FakePrompt.answers = ["7", "", "0"]
        monkeypatch.setattr(menu, "Prompt", _FakePrompt)
        called = []
        monkeypatch.setattr(menu, "action_diagnose", lambda: called.append(True))
        menu.run_menu()
        assert called == [True]
        assert "Goodbye" in capsys.readouterr().out

    def test_clear_screen_runs(self):
        assert menu.clear_screen() is None

    def test_error_guidance_auth(self):
        guidance = menu._error_guidance("SMTPAuthenticationError: (535, b'Authentication failed')")
        assert "password" in guidance.lower()
        assert "Spacemail" in guidance

    def test_error_guidance_timeout(self):
        guidance = menu._error_guidance("Connection failed: TimeoutError: timed out")
        assert "firewall" in guidance.lower()

    def test_smtp_test_action_success(self, monkeypatch, capsys):
        monkeypatch.setattr(
            menu,
            "test_smtp_connection",
            lambda: {"connected": True, "authenticated": True, "error": None, "guidance": None},
        )
        menu.action_test_smtp()
        out = capsys.readouterr().out
        assert "OK" in out
        assert "Connection test passed" in out

    def test_smtp_test_action_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(
            menu,
            "test_smtp_connection",
            lambda: {
                "connected": True,
                "authenticated": False,
                "error": "Authentication failed (535)",
                "guidance": "Check the password.",
            },
        )
        menu.action_test_smtp()
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "Check the password." in out


class TestTyperCommands:

    def test_stats_command(self, env):
        from typer.testing import CliRunner

        result = CliRunner().invoke(main.cli, ["stats"])
        assert result.exit_code == 0
        assert "QUEUED" in result.output

    def test_recent_command(self, env):
        from typer.testing import CliRunner

        database.init_db(env)
        result = CliRunner().invoke(main.cli, ["recent", "--limit", "20"])
        assert result.exit_code == 0

    def test_retry_failed_command(self, env):
        from typer.testing import CliRunner

        database.init_db(env)
        tid = database.queue_email("a@example.com", "S", "b", database_path=env)
        database.update_status(tid, "FAILED", database_path=env)
        result = CliRunner().invoke(main.cli, ["retry-failed"])
        assert result.exit_code == 0
        assert "Re-queued 1" in result.output

    def test_send_help(self):
        from typer.testing import CliRunner

        result = CliRunner().invoke(main.cli, ["send", "--help"])
        assert result.exit_code == 0
        assert "--compose" in result.output

    def test_bare_run_opens_menu_and_exits(self, env):
        from typer.testing import CliRunner

        result = CliRunner().invoke(main.cli, [], input="0\n")
        assert result.exit_code == 0
        assert "Goodbye" in result.output


class TestApi:
    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(main.app)

    def test_health_open(self, env):
        assert self._client().get("/health").status_code == 200

    def test_send_requires_auth(self, env):
        client = self._client()
        assert client.post("/api/send", json={"to": "a@x.com", "subject": "s", "text": "b"}).status_code == 401
        bad = {"Authorization": "Bearer wrong"}
        assert client.post("/api/send", headers=bad, json={"to": "a@x.com", "subject": "s", "text": "b"}).status_code == 401

    def test_send_rejects_bad_payload(self, env):
        client = self._client()
        headers = {"Authorization": "Bearer " + "t" * 32}
        assert client.post("/api/send", headers=headers, json={"to": "nope", "subject": "s", "text": "b"}).status_code == 422
        assert client.post("/api/send", headers=headers, json={"to": "a@x.com", "subject": "s"}).status_code == 422

    def test_send_queues(self, env, monkeypatch):
        monkeypatch.setattr(main, "_run_dispatch", lambda *a: None)
        client = self._client()
        headers = {"Authorization": "Bearer " + "t" * 32}
        response = client.post(
            "/api/send",
            headers=headers,
            json={"to": ["a@x.com", "b@x.com"], "subject": "s", "text": "b"},
        )
        assert response.status_code == 202
        assert response.json()["queued"] == 2

    def test_stats_requires_auth(self, env):
        client = self._client()
        assert client.get("/api/stats").status_code == 401
        headers = {"Authorization": "Bearer " + "t" * 32}
        response = client.get("/api/stats", headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == 0


class TestDiagnose:
    def test_env_file_info(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        info = config.env_file_info()
        assert info["path"].endswith(".env")
        assert info["exists"] is False
        target = tmp_path / ".env"
        target.write_text("SMTP_PASSWORD=abc\n", encoding="utf-8")
        info = config.env_file_info()
        assert info["exists"] is True
        assert info["size"] > 0

    def test_error_names_env_path(self, env, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        get_settings.cache_clear()
        try:
            with pytest.raises(ValueError, match="SMTP_PASSWORD"):
                validate_smtp_settings(Settings(smtp_password=""))
        finally:
            get_settings.cache_clear()

    def test_diagnose_masks_secrets(self, env):
        from typer.testing import CliRunner

        result = CliRunner().invoke(main.cli, ["diagnose"])
        assert result.exit_code == 0
        assert "secret-pw" not in result.output
        assert "t" * 32 not in result.output
        assert "(9 chars)" in result.output
        assert "(32 chars)" in result.output
        assert "Working directory" in result.output

    def test_diagnose_reports_missing_env_file(self, env, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main.cli, ["diagnose"])
        assert result.exit_code == 0
        assert "NOT FOUND" in result.output

    def test_diagnose_action_via_menu(self, monkeypatch, capsys):
        called = []
        monkeypatch.setattr(menu, "diagnose_report", lambda: called.append(True))
        menu.action_diagnose()
        assert called == [True]
