"""Turn pytest's JUnit and coverage XML into a Markdown quality table.

    python scripts/test_report.py --junit reports/junit.xml --coverage reports/coverage.xml

In GitHub Actions the table is appended to the job summary ($GITHUB_STEP_SUMMARY);
anywhere else it is printed. Standard library only, so it runs on the CI image
before any project dependency is installed.

What it reports, and where each number comes from:

- pass / fail / error / skip counts and the pass rate: one entry per test from
  the JUnit file. `pytest-rerunfailures` writes every attempt as its own
  `<testcase>` with the same classname and name, and only the final attempt
  carries a `<failure>` or `<error>`; so tests are grouped by name and the
  last attempt decides the outcome.
- flaky tests: a test with more than one attempt whose last attempt passed.
  It counts as passed, and it is listed so it can be fixed rather than hidden.
- the 5 slowest tests: by the `time` attribute of the final attempt.
- coverage: the `line-rate` on the root of the Cobertura XML that pytest-cov
  writes, alongside the `fail_under` gate read from pyproject.toml.

The exit code is 0 whenever the files could be read. The build is failed by
pytest itself (test failures) and by pytest-cov (the coverage gate), not by
this reporter: a report should never be the reason the numbers went missing.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SLOWEST = 5


@dataclass
class TestResult:
    name: str            # "classname::name"
    status: str          # passed | failed | error | skipped
    attempts: int
    seconds: float

    @property
    def flaky(self) -> bool:
        return self.attempts > 1 and self.status == "passed"


@dataclass
class Report:
    title: str
    tests: list[TestResult] = field(default_factory=list)
    coverage_percent: float | None = None
    lines_covered: int | None = None
    lines_valid: int | None = None
    gate_percent: float | None = None

    def count(self, status: str) -> int:
        return sum(1 for t in self.tests if t.status == status)

    @property
    def total(self) -> int:
        return len(self.tests)

    @property
    def pass_rate(self) -> float | None:
        """Passed over everything that ran; skipped tests neither pass nor fail."""
        ran = self.total - self.count("skipped")
        return None if ran == 0 else 100.0 * self.count("passed") / ran

    @property
    def flaky(self) -> list[TestResult]:
        return [t for t in self.tests if t.flaky]

    @property
    def slowest(self) -> list[TestResult]:
        return sorted(self.tests, key=lambda t: -t.seconds)[:SLOWEST]

    @property
    def coverage_ok(self) -> bool | None:
        if self.coverage_percent is None or self.gate_percent is None:
            return None
        return self.coverage_percent >= self.gate_percent


# --- parsing ---------------------------------------------------------------


def _status_of(testcase: ET.Element) -> str:
    tags = {child.tag for child in testcase}
    if "error" in tags:
        return "error"
    if "failure" in tags:
        return "failed"
    if "skipped" in tags:
        return "skipped"
    return "passed"


def parse_junit(paths: list[Path]) -> list[TestResult]:
    """Every test once, with its final outcome and how many attempts it took."""
    grouped: dict[str, list[ET.Element]] = {}
    for path in paths:
        root = ET.parse(path).getroot()
        for testcase in root.iter("testcase"):
            key = f"{testcase.get('classname', '')}::{testcase.get('name', '')}"
            grouped.setdefault(key, []).append(testcase)
    results: list[TestResult] = []
    for name, attempts in grouped.items():
        final = attempts[-1]
        # `<rerun>` children are how older junit families mark an attempt;
        # they never describe the final outcome, so they are not a status.
        results.append(TestResult(
            name=name,
            status=_status_of(final),
            attempts=len(attempts),
            seconds=float(final.get("time") or 0.0),
        ))
    return results


def parse_coverage(path: Path) -> tuple[float, int | None, int | None]:
    """(percent, lines covered, lines valid) from a Cobertura file."""
    root = ET.parse(path).getroot()
    rate = float(root.get("line-rate") or 0.0)
    covered = root.get("lines-covered")
    valid = root.get("lines-valid")
    return 100.0 * rate, (int(covered) if covered else None), (int(valid) if valid else None)


def coverage_gate(pyproject: Path) -> float | None:
    """`[tool.coverage.report] fail_under`, the gate pytest-cov enforces."""
    if not pyproject.exists():
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always has tomllib
        return None
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    value = data.get("tool", {}).get("coverage", {}).get("report", {}).get("fail_under")
    return None if value is None else float(value)


def build_report(
    junit_paths: list[Path],
    coverage_path: Path | None,
    *,
    title: str = "Test quality",
    gate: float | None = None,
) -> Report:
    report = Report(title=title, tests=parse_junit(junit_paths), gate_percent=gate)
    if coverage_path is not None and coverage_path.exists():
        report.coverage_percent, report.lines_covered, report.lines_valid = parse_coverage(coverage_path)
    return report


# --- rendering ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def render_markdown(report: Report) -> str:
    lines = [f"## {report.title}", ""]
    rate = report.pass_rate
    coverage = _pct(report.coverage_percent)
    if report.lines_covered is not None and report.lines_valid is not None:
        coverage += f" ({report.lines_covered} of {report.lines_valid} lines)"
    gate = "n/a" if report.gate_percent is None else f"{report.gate_percent:.0f}%"
    if report.coverage_ok is True:
        gate += " (met)"
    elif report.coverage_ok is False:
        gate += " (NOT met)"
    lines += [
        "| Metric | Value |",
        "|---|---|",
        f"| Tests | {report.total} |",
        f"| Passed | {report.count('passed')} |",
        f"| Failed | {report.count('failed')} |",
        f"| Errors | {report.count('error')} |",
        f"| Skipped | {report.count('skipped')} |",
        f"| Pass rate | {_pct(rate)} |",
        f"| Flaky (passed after a rerun) | {len(report.flaky)} |",
        f"| Line coverage | {coverage} |",
        f"| Coverage gate | {gate} |",
        "",
    ]
    if report.slowest:
        lines += [f"### {len(report.slowest)} slowest tests", "", "| Test | Seconds |", "|---|---|"]
        lines += [f"| `{t.name}` | {t.seconds:.2f} |" for t in report.slowest]
        lines.append("")
    if report.flaky:
        lines += ["### Flaky tests (passed only after a rerun)", "", "| Test | Attempts |", "|---|---|"]
        lines += [f"| `{t.name}` | {t.attempts} |" for t in report.flaky]
        lines.append("")
    failed = [t for t in report.tests if t.status in ("failed", "error")]
    if failed:
        lines += ["### Failed", "", "| Test | Outcome | Attempts |", "|---|---|---|"]
        lines += [f"| `{t.name}` | {t.status} | {t.attempts} |" for t in failed]
        lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, out: Path | None = None) -> Path | None:
    """Append to $GITHUB_STEP_SUMMARY (or `out`); print when neither is set."""
    target = out or (Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    if target is None:
        print(markdown)
        return None
    with target.open("a", encoding="utf-8") as handle:
        handle.write(markdown)
        handle.write("\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--junit", type=Path, nargs="+", required=True, help="one or more JUnit XML files")
    parser.add_argument("--coverage", type=Path, default=None, help="Cobertura XML from pytest-cov")
    parser.add_argument("--title", default="Test quality")
    parser.add_argument("--gate", type=float, default=None, help="coverage gate; default: pyproject fail_under")
    parser.add_argument("--pyproject", type=Path, default=Path(__file__).resolve().parents[1] / "pyproject.toml")
    parser.add_argument("--out", type=Path, default=None, help="write here instead of the step summary")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.junit if not p.exists()]
    if missing:
        print(f"JUnit file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    gate = args.gate if args.gate is not None else coverage_gate(args.pyproject)
    report = build_report(args.junit, args.coverage, title=args.title, gate=gate)
    markdown = render_markdown(report)
    target = write_summary(markdown, args.out)
    if target is not None:
        print(markdown)
        print(f"summary appended to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
