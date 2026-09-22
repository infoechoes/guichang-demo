#!/bin/sh
set -eu
cd "$(dirname "$0")"
: "${CENTER_ORIGIN:?Set the existing HTTPS account-service origin}"
: "${CENTER_CERT:?Set the existing account-service public certificate path}"
exec "${PYTHON:-python3}" app/preview_center_login.py --data-root "${DATA_ROOT:-.local-runtime}" --center "$CENTER_ORIGIN" --center-cert "$CENTER_CERT" --port "${PORT:-42880}"
