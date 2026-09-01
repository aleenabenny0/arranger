"""Security helper tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import HTTPException  # noqa: E402

from arranger_api.security import RateLimiter  # noqa: E402


def test_rate_limiter_rejects_over_limit():
    limiter = RateLimiter(requests=2, window_seconds=60)
    limiter.check("client")
    limiter.check("client")
    try:
        limiter.check("client")
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("expected rate limit exception")


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
