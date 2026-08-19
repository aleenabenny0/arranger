#!/usr/bin/env bash
# PostToolUse hook: whenever a score is written, verify it.
#
# The point of a hook is that it fires whether or not the agent cooperates.
# An instruction in CLAUDE.md is a request; this is enforcement.
set -euo pipefail
FILE="${1:-}"
case "$FILE" in
  *.json|*.musicxml|*.xml) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0
if ! PYTHONPATH=src python3 -m arranger.verify.cli "$FILE" --profile profiles/me.json >/dev/null 2>&1; then
  echo "BLOCKED: $FILE has hard playability violations. Run:" >&2
  echo "  PYTHONPATH=src python3 -m arranger.verify.cli $FILE --profile profiles/me.json" >&2
  exit 2
fi
