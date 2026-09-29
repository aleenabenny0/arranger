"""Email adapter tests. Nothing here sends mail: the Resend SDK call is patched."""

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.email import (  # noqa: E402
    ConsoleEmailSender,
    EmailHealth,
    EmailSendError,
    ResendEmailSender,
    build_email_sender,
    build_email_verification_link,
    build_password_reset_link,
    deliver_email,
    email_diagnostics,
)
from arranger_api.metrics import AppMetrics  # noqa: E402
from arranger_api.settings import ConfigurationError, load_settings  # noqa: E402


def test_password_reset_link_uses_public_url_and_encoded_token():
    settings = replace(load_settings(), public_base_url="https://arranger.example")

    link = build_password_reset_link(settings, "token with spaces")

    # Fragment, not query string: the token never reaches a server or proxy log.
    assert link == "https://arranger.example/#/reset-password?token=token%20with%20spaces"


def test_verification_link_points_at_the_spa_verify_route():
    settings = replace(load_settings(), public_base_url="https://arranger.example")

    link = build_email_verification_link(settings, "a/b+c=")

    assert link == "https://arranger.example/#/verify-email?token=a%2Fb%2Bc%3D"
    assert "?" not in link.split("#", 1)[0]


def test_console_email_sender_is_default():
    sender = build_email_sender(load_settings())

    assert sender.__class__.__name__ == "ConsoleEmailSender"


def test_resend_sender_uses_official_sdk_payload():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")

    with patch("resend.Emails.send") as send:
        sender.send_password_reset("user@example.com", "https://example.com/?reset_token=t", 30)

    payload = send.call_args.args[0]
    assert payload["from"] == "onboarding@resend.dev"
    assert payload["to"] == ["user@example.com"]
    assert payload["subject"] == "Reset"
    assert "https://example.com/?reset_token=t" in payload["html"]
    assert "https://example.com/?reset_token=t" in payload["text"]


def test_resend_sender_sends_verification_email_through_the_same_port():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset", "Verify")
    link = "https://example.com/#/verify-email?token=t"

    with patch("resend.Emails.send") as send:
        sender.send_email_verification("user@example.com", link, 1440)

    payload = send.call_args.args[0]
    assert payload["subject"] == "Verify"
    assert payload["to"] == ["user@example.com"]
    assert link in payload["text"] and link in payload["html"]


def test_resend_sender_escapes_the_link_in_html():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")
    with patch("resend.Emails.send") as send:
        sender.send_password_reset("user@example.com", 'https://x/"><script>1</script>', 30)
    assert "<script>" not in send.call_args.args[0]["html"]


def test_resend_sender_raises_email_send_error_with_status_and_body():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")

    with patch("resend.Emails.send") as send:
        error = Exception("sandbox recipient is not allowed")
        error.status_code = 403
        send.side_effect = error

        try:
            sender.send_password_reset("user@example.com", "https://example.com/?reset_token=t", 30)
            raise AssertionError("expected EmailSendError")
        except EmailSendError as exc:
            assert exc.status_code is None
            assert "sandbox recipient is not allowed" in exc.body


def test_email_diagnostics_reports_non_secret_fields_only():
    settings = replace(
        load_settings(),
        email_provider="resend",
        resend_api_key="re_super_secret",
        public_base_url="https://arranger.example",
        password_reset_from="Arranger <onboarding@resend.dev>",
    )

    diagnostics = email_diagnostics(settings)

    assert diagnostics == {
        "provider": "resend",
        "has_resend_key": True,
        "app_public_url": "https://arranger.example",
        "password_reset_from": "Arranger <onboarding@resend.dev>",
    }
    assert "re_super_secret" not in str(diagnostics)


# --- 7: reset links and the console sender ------------------------------------


def test_console_sender_prints_to_stderr_and_never_through_the_logger(capsys, caplog):
    # Audit 7: the full reset link was attached to a log record's `extra`.
    link = "http://127.0.0.1:8000/#/reset-password?token=CONSOLE-SECRET"
    with caplog.at_level(logging.DEBUG):
        ConsoleEmailSender("development").send_password_reset("dev@example.com", link, 30)
        ConsoleEmailSender("test").send_email_verification("dev@example.com", link, 30)

    assert capsys.readouterr().err.count(link) == 2
    assert caplog.records == []


@pytest.mark.parametrize("app_env", ["production", "staging", "prod", ""])
def test_console_sender_is_unusable_outside_development_and_test(app_env, capsys):
    sender = ConsoleEmailSender(app_env)
    with pytest.raises(EmailSendError):
        sender.send_password_reset("user@example.com", "https://x/#/reset-password?token=T", 30)
    with pytest.raises(EmailSendError):
        sender.send_email_verification("user@example.com", "https://x/#/verify-email?token=T", 30)
    assert "token=T" not in capsys.readouterr().err


# --- 8: no silent fallback ----------------------------------------------------


def production(**overrides):
    values = {
        "app_env": "production",
        "email_provider": "resend",
        "resend_api_key": "re_placeholder",
        **overrides,
    }
    return replace(load_settings(), **values)


def test_build_email_sender_never_substitutes_the_console_provider():
    with pytest.raises(ConfigurationError, match="RESEND_API_KEY"):
        build_email_sender(production(resend_api_key=""))
    with pytest.raises(ConfigurationError, match="console"):
        build_email_sender(replace(production(), email_provider="console"))
    with pytest.raises(ConfigurationError, match="not supported"):
        build_email_sender(replace(load_settings(), email_provider="smtp"))

    assert isinstance(build_email_sender(production()), ResendEmailSender)


def test_deliver_email_records_failure_as_error_log_metric_and_health():
    health, metrics = EmailHealth(), AppMetrics()
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("arranger_api")
    logger.addHandler(handler)

    def failing_send():
        raise EmailSendError("rejected", status_code=403, body="error code: 1010")

    try:
        delivered = deliver_email(
            "password_reset",
            failing_send,
            provider="resend",
            recipient="user@example.com",
            health=health,
            metrics=metrics,
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert health.status() == "degraded"
    assert metrics.email_sends.value(kind="password_reset", outcome="failed") == 1
    failure = [r for r in records if r.levelno == logging.ERROR]
    assert len(failure) == 1
    event = json.loads(failure[0].getMessage())
    assert event["event"] == "password_reset_email_failed"
    assert event["status_code"] == 403 and event["response_body"] == "error code: 1010"
    assert event["email_domain"] == "example.com" and "user@" not in failure[0].getMessage()


def test_deliver_email_success_is_counted_and_healthy():
    health, metrics = EmailHealth(), AppMetrics()
    sent = []
    assert deliver_email(
        "email_verification",
        lambda: sent.append(1),
        provider="resend",
        recipient="user@example.com",
        health=health,
        metrics=metrics,
    )
    assert sent == [1] and health.status() == "ok"
    assert metrics.email_sends.value(kind="email_verification", outcome="sent") == 1


def test_email_health_reflects_recent_outcomes():
    health = EmailHealth(window=4)
    assert health.status() == "ok"  # nothing sent yet
    health.record(True)
    health.record(False)
    assert health.status() == "degraded"  # the latest send failed
    health.record(True)
    health.record(True)
    assert health.status() == "ok"  # one old failure in four is not an outage
    health.record(True)
    health.record(True)
    assert health.snapshot() == {"status": "ok", "recent_sends": 4, "recent_failures": 0}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
