"""Email adapter tests."""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger_api.email import build_email_sender, build_password_reset_link  # noqa: E402
from arranger_api.settings import load_settings  # noqa: E402


def test_password_reset_link_uses_public_url_and_encoded_token():
    settings = replace(load_settings(), app_public_url="https://arranger.example")

    link = build_password_reset_link(settings, "token with spaces")

    assert link == "https://arranger.example/?reset_token=token+with+spaces"


def test_console_email_sender_is_default():
    sender = build_email_sender(load_settings())

    assert sender.__class__.__name__ == "ConsoleEmailSender"


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
