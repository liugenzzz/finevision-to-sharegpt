# Docker Image Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and export a usable Docker image for the `finevision-to-sharegpt` CLI.

**Architecture:** The Dockerfile installs the local package into a Python 3.11 slim image and exposes the installed CLI as the entrypoint. A `.dockerignore` file controls build context size. PowerShell and Bash scripts automate build plus `docker save`, with `linux/amd64` as the default target platform.

**Tech Stack:** Docker, Python 3.11, setuptools, PowerShell, Bash.

---

### Task 1: Docker Build Context

**Files:**
- Create: `.dockerignore`

- [ ] **Step 1: Create `.dockerignore`**

```text
.git
.pytest_cache
.mypy_cache
.ruff_cache
__pycache__
*.py[cod]
.venv
venv
env
build
dist
*.egg-info
*.tar
*.tar.gz
*.zip
output
outputs
data
datasets
*.jsonl
*.parquet
```

- [ ] **Step 2: Review build context exclusions**

Run: `Get-Content -Raw .dockerignore`

Expected: the file excludes caches, environments, archives, generated output, and dataset files while allowing project source, prompts, configs, README, and packaging metadata.

### Task 2: Dockerfile Package Install

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Replace the Dockerfile with a package-installing image**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY prompts ./prompts
COPY configs ./configs

RUN python -m pip install --upgrade pip \
    && python -m pip install .

ENTRYPOINT ["finevision-to-sharegpt"]
CMD ["--help"]
```

- [ ] **Step 2: Verify Dockerfile content**

Run: `Get-Content -Raw Dockerfile`

Expected: the entrypoint is `finevision-to-sharegpt`, and the image installs the local package with `python -m pip install .`.

### Task 3: Export Scripts

**Files:**
- Create: `scripts/build_docker_image.ps1`
- Create: `scripts/build_docker_image.sh`

- [ ] **Step 1: Add PowerShell build and export helper**

```powershell
param(
    [string]$ImageTag = "finevision-to-sharegpt:latest",
    [string]$OutputTar = "finevision-to-sharegpt_latest.tar",
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $ProjectRoot
try {
    docker build --platform $Platform -t $ImageTag .
    docker save $ImageTag -o $OutputTar

    $Saved = Get-Item -LiteralPath $OutputTar
    Write-Host "Saved Docker image $ImageTag to $($Saved.FullName)"
    Write-Host "Size: $([Math]::Round($Saved.Length / 1MB, 2)) MB"
}
finally {
    Pop-Location
}
```

- [ ] **Step 2: Add Linux Bash build and export helper**

```bash
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
```

- [ ] **Step 3: Verify script content**

Run: `Get-Content -Raw scripts/build_docker_image.ps1`

Expected: the script builds `finevision-to-sharegpt:latest` for `linux/amd64`, saves `finevision-to-sharegpt_latest.tar`, and prints the saved path and size.

Run: `Get-Content -Raw scripts/build_docker_image.sh`

Expected: the script builds `finevision-to-sharegpt:latest` for `linux/amd64`, saves `finevision-to-sharegpt_latest.tar`, and prints the saved path and size.

### Task 4: Build, Test, and Export

**Files:**
- Generated: `finevision-to-sharegpt_latest.tar`

- [ ] **Step 1: Run Python tests**

Run: `python -m pytest -q`

Expected: tests pass in the local environment.

- [ ] **Step 2: Build and export the image**

Run: `powershell -ExecutionPolicy Bypass -File scripts/build_docker_image.ps1`

Expected: Docker builds `finevision-to-sharegpt:latest` and creates `finevision-to-sharegpt_latest.tar`.

Linux alternative: `bash scripts/build_docker_image.sh`

- [ ] **Step 3: Verify container CLI**

Run: `docker run --rm finevision-to-sharegpt:latest --help`

Expected: command exits with code 0 and prints CLI help.

- [ ] **Step 4: Verify archive**

Run: `Get-Item finevision-to-sharegpt_latest.tar | Select-Object FullName,Length`

Expected: the tar file exists and has a length greater than zero.
