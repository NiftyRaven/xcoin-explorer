#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install -q -r requirements.txt
echo
echo "Starting XFER Explorer at http://127.0.0.1:8080"
echo "Keep any X Coin 1.0.13+ Light or Heavy wallet running with server=1 (see START HERE.txt)"
echo "Guide: docs/SETUP.md"
echo
exec python -m explorer "$@"
