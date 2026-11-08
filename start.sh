#!/usr/bin/env bash
# Meeting Transcriber launcher (macOS / Linux). Usage:  bash start.sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "First start - looking for Python 3.10 to 3.13 ..."
  PY=""
  for c in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, tkinter; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Need Python 3.10-3.13 with tkinter (e.g. brew install python@3.12 python-tk@3.12, or apt install python3-tk)."
    exit 1
  fi
  "$PY" -m venv .venv || exit 1
fi
.venv/bin/python setup_check.py || exit 1
exec .venv/bin/python m4a_joiner.py
