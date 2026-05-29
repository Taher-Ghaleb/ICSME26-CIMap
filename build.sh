#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m pip install -r requirements.txt
"$PYTHON" scripts/parse_docs.py --force
"$PYTHON" scripts/validate_docs.py
"$PYTHON" scripts/repair_docs.py
"$PYTHON" scripts/build_database.py --fresh
