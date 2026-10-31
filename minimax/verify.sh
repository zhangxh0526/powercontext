#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONPATH=.
PYTHON=${PYTHON:-python3}

if SDD_STATUS_MODULE=samples.broken_status "$PYTHON" -m unittest tests.test_status; then
  echo "broken implementation unexpectedly passed" >&2
  exit 1
else
  echo "EXPECTED: target tests fail before the implementation"
fi

SDD_STATUS_MODULE=src.status "$PYTHON" -m unittest tests.test_status
"$PYTHON" -m unittest tests.test_context_manager
