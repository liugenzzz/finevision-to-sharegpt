#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-finevision-to-sharegpt:latest}"
OUTPUT_TAR="${OUTPUT_TAR:-finevision-to-sharegpt_latest.tar}"
PLATFORM="${PLATFORM:-linux/amd64}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

docker build --platform "${PLATFORM}" -t "${IMAGE_TAG}" .
docker save "${IMAGE_TAG}" -o "${OUTPUT_TAR}"

SAVED_PATH="$(cd "$(dirname "${OUTPUT_TAR}")" && pwd)/$(basename "${OUTPUT_TAR}")"
SIZE_MB="$(python3 -c 'import os, sys; print(round(os.path.getsize(sys.argv[1]) / 1024 / 1024, 2))' "${OUTPUT_TAR}")"
echo "Saved Docker image ${IMAGE_TAG} to ${SAVED_PATH}"
echo "Size: ${SIZE_MB} MB"
