"""Email adapter tests."""

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.email import (  # noqa: E402
    EmailSendError,
    ResendEmailSender,
    build_email_sender,
    build_password_reset_link,
    email_diagnostics,
)
from arranger_api.settings import load_settings  # noqa: E402


def test_password_reset_link_uses_public_url_and_encoded_token():
    settings = replace(load_settings(), app_public_url="https://arranger.example")

    link = build_password_reset_link(settings, "token with spaces")

    assert link == "https://arranger.example/?reset_token=token+with+spaces"


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
        app_public_url="https://arranger.example",
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


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
