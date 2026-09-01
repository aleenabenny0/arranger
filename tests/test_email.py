"""Email adapter tests."""

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.email import ResendEmailSender, build_email_sender, build_password_reset_link  # noqa: E402
from arranger_api.settings import load_settings  # noqa: E402


def test_password_reset_link_uses_public_url_and_encoded_token():
    settings = replace(load_settings(), app_public_url="https://arranger.example")

    link = build_password_reset_link(settings, "token with spaces")

    assert link == "https://arranger.example/?reset_token=token+with+spaces"


def test_console_email_sender_is_default():
    sender = build_email_sender(load_settings())

    assert sender.__class__.__name__ == "ConsoleEmailSender"


def test_resend_sender_includes_user_agent_header():
    sender = ResendEmailSender("re_test", "onboarding@resend.dev", "Reset")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.status = 200

        sender.send_password_reset("user@example.com", "https://example.com/?reset_token=t", 30)

    request = urlopen.call_args.args[0]
    assert request.headers["User-agent"] == "arranger-api/0.1"


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
