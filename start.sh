#!/usr/bin/env bash
# Linux / macOS (for development or a headless server PC): creates a virtual environment once, then starts AstroFlow.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python run.py "$@"
