#!/usr/bin/env bash
# Run the tests the way CI does.
#
#   scripts/test.sh                 # everything but the browser tests, with coverage and reports/
#   scripts/test.sh --quick         # no coverage, no reports, no lint: fastest feedback
#   scripts/test.sh --browser       # include the Playwright browser journey
#   scripts/test.sh -- -k solver    # pass extra arguments to pytest after --
#
# The coverage gate (fail_under in pyproject.toml), the JUnit file and the
# quality table are the same ones the CI job produces, so a green run here is
# a green run there.
set -euo pipefail

cd "$(dirname "$0")/.."

quick=0
browser=0
extra=()
while (( $# > 0 )); do
  case "$1" in
    --quick) quick=1 ;;
    --browser) browser=1 ;;
    --)
      shift
      extra=("$@")
      break
      ;;
    -h | --help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1 (use -- before pytest arguments)" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -x .venv/Scripts/python.exe ]]; then
  VENV_PY=.venv/Scripts/python.exe
elif [[ -x .venv/bin/python ]]; then
  VENV_PY=.venv/bin/python
else
  echo "no .venv found; run scripts/setup.sh first" >&2
  exit 2
fi

# The verifier must keep working on nothing but the standard library.
"$VENV_PY" tests/test_constraints.py

args=(--durations=10 --reruns 2 -rRs)
if (( ! quick )); then
  rm -rf reports
  args+=(--cov=src --cov-report=term --cov-report=xml:reports/coverage.xml --junitxml=reports/junit.xml)
fi
if (( ! browser )); then
  args+=(--ignore=tests/test_browser_e2e.py)
fi

"$VENV_PY" -m pytest "${args[@]}" "${extra[@]}"

if (( ! quick )); then
  "$VENV_PY" scripts/test_report.py --junit reports/junit.xml --coverage reports/coverage.xml --title "Local run"
  "$VENV_PY" -m ruff check .
fi
