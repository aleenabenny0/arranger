#!/usr/bin/env bash
# One-time developer setup: virtual environment, dependencies, notation engine.
#
#   scripts/setup.sh            # the API and the test tooling
#   scripts/setup.sh --audio    # also the audio transcription packages
#   scripts/setup.sh --e2e      # also Playwright, its Chromium and the accessibility checker
#
# Runs in Git Bash on Windows and in bash on Linux and macOS; the PowerShell
# equivalent is in the README. Safe to run again: it only installs what is missing.
set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

with_audio=0
with_e2e=0
for argument in "$@"; do
  case "$argument" in
    --audio) with_audio=1 ;;
    --e2e) with_e2e=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $argument" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# The interpreter that creates the environment. PYTHON=python3.13 overrides it.
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 > /dev/null 2>&1; then PYTHON=python3; else PYTHON=python; fi
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv with $("$PYTHON" --version)"
  "$PYTHON" -m venv .venv
fi

# Windows venvs keep the interpreter under Scripts/, POSIX venvs under bin/.
if [[ -x .venv/Scripts/python.exe ]]; then
  VENV_PY=.venv/Scripts/python.exe
else
  VENV_PY=.venv/bin/python
fi

"$VENV_PY" -m pip install --quiet --upgrade pip

extras="api,dev"
if (( with_e2e )); then extras="$extras,e2e"; fi
echo "Installing arranger with extras: $extras"
"$VENV_PY" -m pip install --quiet -e ".[$extras]"

if (( with_audio )); then
  echo "Installing the audio transcription packages"
  "$VENV_PY" -m pip install --quiet numpy onnxruntime soundfile soxr
  # basic-pitch declares a TensorFlow dependency with no wheels on current
  # Pythons; only its bundled ONNX model file is used, so skip its dependencies.
  "$VENV_PY" -m pip install --quiet --no-deps basic-pitch
fi

echo "Fetching the pinned notation engine (checksum-verified)"
if (( with_e2e )); then
  "$VENV_PY" fetch_vendor.py --dev
  echo "Installing Chromium for Playwright"
  "$VENV_PY" -m playwright install chromium
else
  "$VENV_PY" fetch_vendor.py
fi

echo
echo "Ready. Next:"
echo "  scripts/test.sh                 # the test suite, as CI runs it"
echo "  $VENV_PY -m arranger_api.main   # the API at http://127.0.0.1:8000"
