"""The quality reporter in scripts/test_report.py.

Fixture XML below is shaped exactly like what pytest 9 with pytest-rerunfailures
16 writes: each attempt of a rerun test is its own `<testcase>` with the same
classname and name, and only the last attempt carries a `<failure>`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_report.py"
_spec = importlib.util.spec_from_file_location("quality_report", SCRIPT)
quality_report = importlib.util.module_from_spec(_spec)
sys.modules["quality_report"] = quality_report
_spec.loader.exec_module(quality_report)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
<testsuite name="pytest" errors="1" failures="1" skipped="1" tests="6" time="1.0">
<testcase classname="tests.test_a" name="test_flaky" time="0.20" />
<testcase classname="tests.test_a" name="test_flaky" time="0.30" />
<testcase classname="tests.test_a" name="test_fast" time="0.01" />
<testcase classname="tests.test_a" name="test_slow" time="2.50" />
<testcase classname="tests.test_b" name="test_broken" time="0.05" />
<testcase classname="tests.test_b" name="test_broken" time="0.05" />
<testcase classname="tests.test_b" name="test_broken" time="0.05"><failure message="assert False">boom</failure></testcase>
<testcase classname="tests.test_b" name="test_errored" time="0.02"><error message="fixture">setup</error></testcase>
<testcase classname="tests.test_b" name="test_skipped" time="0.00"><skipped message="no postgres">skipped</skipped></testcase>
<testcase classname="tests.test_c" name="test_medium" time="0.90" />
</testsuite>
</testsuites>
"""

COVERAGE = """<?xml version="1.0" ?>
<coverage version="7.6" timestamp="1" lines-valid="1000" lines-covered="874" line-rate="0.874" branches-covered="0" branches-valid="0" branch-rate="0" complexity="0">
<sources><source>src</source></sources>
<packages></packages>
</coverage>
"""


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    junit = tmp_path / "junit.xml"
    coverage = tmp_path / "coverage.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    coverage.write_text(COVERAGE, encoding="utf-8")
    return junit, coverage


def test_each_test_is_counted_once_by_its_final_attempt(files):
    junit, coverage = files
    report = quality_report.build_report([junit], coverage)
    assert report.total == 7
    assert report.count("passed") == 4
    assert report.count("failed") == 1
    assert report.count("error") == 1
    assert report.count("skipped") == 1


def test_pass_rate_ignores_skipped_tests(files):
    junit, coverage = files
    report = quality_report.build_report([junit], coverage)
    assert report.pass_rate == pytest.approx(100.0 * 4 / 6)


def test_a_test_that_passed_after_a_rerun_is_flaky_and_a_failure_after_reruns_is_not(files):
    junit, coverage = files
    report = quality_report.build_report([junit], coverage)
    assert [t.name for t in report.flaky] == ["tests.test_a::test_flaky"]
    broken = next(t for t in report.tests if t.name.endswith("test_broken"))
    assert broken.status == "failed" and broken.attempts == 3 and not broken.flaky


def test_slowest_tests_are_the_five_longest_by_final_attempt(files):
    junit, coverage = files
    report = quality_report.build_report([junit], coverage)
    names = [t.name for t in report.slowest]
    assert len(names) == 5
    assert names[:3] == ["tests.test_a::test_slow", "tests.test_c::test_medium", "tests.test_a::test_flaky"]
    flaky = report.slowest[2]
    assert flaky.seconds == pytest.approx(0.30), "the final attempt's time, not the first"


def test_coverage_comes_from_the_cobertura_line_rate(files):
    junit, coverage = files
    report = quality_report.build_report([junit], coverage, gate=85.0)
    assert report.coverage_percent == pytest.approx(87.4)
    assert (report.lines_covered, report.lines_valid) == (874, 1000)
    assert report.coverage_ok is True
    assert quality_report.build_report([junit], coverage, gate=90.0).coverage_ok is False


def test_missing_coverage_file_reports_not_available(files):
    junit, _ = files
    report = quality_report.build_report([junit], Path("does-not-exist.xml"))
    assert report.coverage_percent is None and report.coverage_ok is None
    assert "| Line coverage | n/a |" in quality_report.render_markdown(report)


def test_markdown_has_the_table_the_slow_list_the_flaky_list_and_the_failures(files):
    junit, coverage = files
    markdown = quality_report.render_markdown(
        quality_report.build_report([junit], coverage, title="Python tests (3.13)", gate=85.0)
    )
    assert markdown.startswith("## Python tests (3.13)")
    assert "| Tests | 7 |" in markdown
    assert "| Pass rate | 66.7% |" in markdown
    assert "| Flaky (passed after a rerun) | 1 |" in markdown
    assert "| Line coverage | 87.4% (874 of 1000 lines) |" in markdown
    assert "| Coverage gate | 85% (met) |" in markdown
    assert "### 5 slowest tests" in markdown and "`tests.test_a::test_slow` | 2.50" in markdown
    assert "### Flaky tests" in markdown and "`tests.test_a::test_flaky` | 2" in markdown
    assert "### Failed" in markdown and "`tests.test_b::test_errored` | error | 1" in markdown


def test_gate_is_read_from_pyproject(tmp_path: Path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.coverage.report]\nfail_under = 80\n", encoding="utf-8")
    assert quality_report.coverage_gate(pyproject) == 80.0
    assert quality_report.coverage_gate(tmp_path / "absent.toml") is None


def test_cli_appends_to_the_github_step_summary(files, tmp_path: Path, monkeypatch, capsys):
    junit, coverage = files
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = quality_report.main(["--junit", str(junit), "--coverage", str(coverage), "--gate", "85"])
    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n") and "| Tests | 7 |" in text
    assert "summary appended to" in capsys.readouterr().out


def test_cli_prints_when_there_is_no_step_summary(files, monkeypatch, capsys):
    junit, coverage = files
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert quality_report.main(["--junit", str(junit), "--coverage", str(coverage), "--gate", "85"]) == 0
    assert "| Tests | 7 |" in capsys.readouterr().out


def test_cli_fails_clearly_when_the_junit_file_is_missing(tmp_path: Path, capsys):
    assert quality_report.main(["--junit", str(tmp_path / "nope.xml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_several_junit_files_are_merged(files, tmp_path: Path):
    junit, coverage = files
    second = tmp_path / "junit-2.xml"
    second.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="tests.test_d" name="test_extra" time="0.1"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    report = quality_report.build_report([junit, second], coverage)
    assert report.total == 8 and report.count("passed") == 5
