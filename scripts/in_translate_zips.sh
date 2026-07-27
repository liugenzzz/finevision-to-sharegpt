#!/usr/bin/env bash
set -euo pipefail

# Run from anywhere: resolve the project root from this script's location so
# relative paths inside the config file resolve against the project root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Default config lives in the repo; override by passing a path as $1.
CONFIG_PATH="${1:-configs/translate_zips.json}"
PYTHON="${PYTHON:-python}"

# Make the package importable without installing it (works in a container or
# straight from a checkout on a server).
export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON}" -m finevision_to_sharegpt translate-zips --config "${CONFIG_PATH}"
