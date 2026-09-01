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


def test_resend_sender_uses_http2_and_includes_user_agent_header():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")

    with patch("httpx.Client") as client_class:
        response = client_class.return_value.__enter__.return_value.post.return_value
        response.status_code = 200

        sender.send_password_reset("user@example.com", "https://example.com/?reset_token=t", 30)

    assert client_class.call_args.kwargs["http2"] is True
    post = client_class.return_value.__enter__.return_value.post
    assert post.call_args.args[0] == "https://api.resend.com/emails"
    assert post.call_args.kwargs["headers"]["User-Agent"] == "arranger-api/0.1"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer re_test"
    assert '"to": ["user@example.com"]' in post.call_args.kwargs["content"]


def test_resend_sender_raises_email_send_error_with_status_and_body():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")

    with patch("httpx.Client") as client_class:
        response = client_class.return_value.__enter__.return_value.post.return_value
        response.status_code = 403
        response.text = "error code: 1010"

        try:
            sender.send_password_reset("user@example.com", "https://example.com/?reset_token=t", 30)
            raise AssertionError("expected EmailSendError")
        except EmailSendError as exc:
            assert exc.status_code == 403
            assert exc.body == "error code: 1010"


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
