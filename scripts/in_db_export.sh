#!/usr/bin/env bash
set -euo pipefail

# Run from anywhere: resolve the project root from this script's location so
# relative paths inside the config file resolve against the project root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Default config lives in the repo; override by passing a path as $1.
# Any further arguments are forwarded (e.g. --dataset okvqa --lang zh).
CONFIG_PATH="${1:-configs/mysql.json}"
shift || true
OUTPUT_PATH="${FV_DB_EXPORT_OUTPUT:-output/db_export.jsonl}"
PYTHON="${PYTHON:-python}"

# Make the package importable without installing it (works in a container or
# straight from a checkout on a server).
export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON}" -m finevision_to_sharegpt db-export \
  --config "${CONFIG_PATH}" --output "${OUTPUT_PATH}" "$@"
