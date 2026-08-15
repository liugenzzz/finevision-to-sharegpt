# Docker Image Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Rebuild the project from a source-derived, explicit runtime dependency set and deliver a verified linux/amd64 Docker image tar that can be imported offline.

**Architecture:** Keep the existing CLI entry point and python:3.11-slim runtime image. Make requirements.txt and pyproject.toml declare the same three direct runtime dependencies, install those dependencies before installing the local package with --no-deps, and keep data outside the image through runtime mounts. The export helpers build the image, save it, and write a SHA-256 sidecar.

**Tech Stack:** Python 3.11, setuptools, Docker BuildKit, PowerShell, Bash, pytest.

---

## File map

- Create: tests/test_packaging.py — static packaging contract tests that run without Docker.
- Modify: requirements.txt — explicit runtime pins: httpx==0.28.1, pyarrow==25.0.1, tqdm==4.70.0.
- Modify: pyproject.toml — keep the project metadata dependency list identical to requirements.txt; retain the existing test extra.
- Modify: Dockerfile — install the explicit requirements, then install the local package without dependency resolution.
- Modify: .dockerignore — keep generated checksum files and local build artifacts out of the build context.
- Modify: scripts/build_docker_image.ps1 — default to the platform-specific tar name and write its checksum.
- Modify: scripts/build_docker_image.sh — provide the same behavior on Linux.
- Modify: README.md — document the helper scripts, tar name, checksum, and offline import.
- Create: finevision-to-sharegpt_linux-amd64.tar — generated Docker image export (not committed).
- Create: finevision-to-sharegpt_linux-amd64.tar.sha256 — generated checksum sidecar (not committed).

## Task 1: Add packaging contract tests first

**Files:**
- Create: tests/test_packaging.py

- [ ] Step 1: Write the failing static contract tests

Create tests/test_packaging.py with this content:

~~~python
from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REQUIREMENTS = [
    "httpx==0.28.1",
    "pyarrow==25.0.1",
    "tqdm==4.70.0",
]


def _requirements() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_runtime_requirements_are_explicit_and_source_derived() -> None:
    assert _requirements() == EXPECTED_REQUIREMENTS


def test_pyproject_runtime_dependencies_match_requirements() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["dependencies"] == EXPECTED_REQUIREMENTS
    assert "pytest>=8,<9" in metadata["project"]["optional-dependencies"]["test"]


def test_dockerfile_installs_requirements_before_local_package() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements_install = "python -m pip install --no-cache-dir -r requirements.txt"
    package_install = "python -m pip install --no-cache-dir --no-deps ."
    assert "COPY requirements.txt pyproject.toml README.md ./" in dockerfile
    assert requirements_install in dockerfile
    assert package_install in dockerfile
    assert dockerfile.index(requirements_install) < dockerfile.index(package_install)
    assert 'ENTRYPOINT ["finevision-to-sharegpt"]' in dockerfile


def test_export_helpers_use_platform_specific_tar_and_checksum() -> None:
    powershell = (ROOT / "scripts" / "build_docker_image.ps1").read_text(encoding="utf-8")
    bash = (ROOT / "scripts" / "build_docker_image.sh").read_text(encoding="utf-8")
    expected_tar = "finevision-to-sharegpt_linux-amd64.tar"
    assert expected_tar in powershell
    assert expected_tar in bash
    assert "Get-FileHash" in powershell
    assert "sha256sum" in bash


def test_dockerignore_excludes_generated_archives_and_data() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "*.tar" in dockerignore
    assert "*.sha256" in dockerignore
    assert "data" in dockerignore
    assert "output" in dockerignore
~~~

- [ ] Step 2: Run only the new tests and confirm they fail against the current packaging

Run:

~~~powershell
python -m pytest tests/test_packaging.py -q
~~~

Expected: failures report the old dependency list, the missing Docker requirements install, the old export filename/checksum behavior, and the missing checksum ignore rule. This confirms the tests exercise the requested change rather than merely validating the existing files.

- [ ] Step 3: Commit the red tests

~~~powershell
git add tests/test_packaging.py
git commit -m "test: define Docker packaging contracts"
~~~

## Task 2: Replace the dependency declarations

**Files:**
- Modify: requirements.txt
- Modify: pyproject.toml

- [ ] Step 1: Replace requirements.txt with the three explicit runtime pins

The complete file must be:

~~~text
httpx==0.28.1
pyarrow==25.0.1
tqdm==4.70.0
~~~

Do not add test-only packages or packages not imported by the runtime source.

- [ ] Step 2: Synchronize the project metadata dependency list

In pyproject.toml, replace only the project dependencies array with:

~~~toml
dependencies = [
  "httpx==0.28.1",
  "pyarrow==25.0.1",
  "tqdm==4.70.0",
]
~~~

Leave requires-python, the project script, package discovery, and the test optional extra unchanged.

- [ ] Step 3: Run the dependency contract tests

Run:

~~~powershell
python -m pytest tests/test_packaging.py::test_runtime_requirements_are_explicit_and_source_derived tests/test_packaging.py::test_pyproject_runtime_dependencies_match_requirements -q
~~~

Expected: both tests pass.

- [ ] Step 4: Commit the dependency metadata

~~~powershell
git add requirements.txt pyproject.toml
git commit -m "build: refresh runtime dependency pins"
~~~

## Task 3: Make the Dockerfile consume the new dependency source

**Files:**
- Modify: Dockerfile
- Modify: .dockerignore

- [ ] Step 1: Replace Dockerfile with the package-installing runtime image

Use this complete file:

~~~dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir --no-deps .

COPY prompts ./prompts
COPY configs ./configs

ENTRYPOINT ["finevision-to-sharegpt"]
CMD ["--help"]
~~~

Copy src before pip install . so setuptools can package the local source; prompts and configs remain later layers that do not affect dependency installation.

- [ ] Step 2: Extend .dockerignore for generated checksum files

Keep all existing exclusions and add:

~~~text
*.sha256
~~~

The existing archive, output, data, cache, and virtual-environment exclusions must remain in place.

- [ ] Step 3: Run the Dockerfile and ignore contract tests

Run:

~~~powershell
python -m pytest tests/test_packaging.py::test_dockerfile_installs_requirements_before_local_package tests/test_packaging.py::test_dockerignore_excludes_generated_archives_and_data -q
~~~

Expected: both tests pass.

- [ ] Step 4: Commit the container definition

~~~powershell
git add Dockerfile .dockerignore
git commit -m "build: install explicit dependencies in Docker image"
~~~

## Task 4: Update image build/export helpers and user documentation

**Files:**
- Modify: scripts/build_docker_image.ps1
- Modify: scripts/build_docker_image.sh
- Modify: README.md

- [ ] Step 1: Replace the PowerShell helper

Use this complete script:

~~~powershell
param(
    [string]$ImageTag = "finevision-to-sharegpt:latest",
    [string]$OutputTar = "finevision-to-sharegpt_linux-amd64.tar",
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputPath = if ([System.IO.Path]::IsPathRooted($OutputTar)) {
    [System.IO.Path]::GetFullPath($OutputTar)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputTar))
}
$OutputDirectory = Split-Path -Parent $OutputPath
$ChecksumPath = "$OutputPath.sha256"

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
Push-Location $ProjectRoot
try {
    docker build --platform $Platform -t $ImageTag .
    docker save $ImageTag -o $OutputPath

    $Saved = Get-Item -LiteralPath $OutputPath
    $Hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $(Split-Path -Leaf $OutputPath)" | Out-File -LiteralPath $ChecksumPath -Encoding ascii

    Write-Host "Saved Docker image $ImageTag to $($Saved.FullName)"
    Write-Host "Size: $([Math]::Round($Saved.Length / 1MB, 2)) MB"
    Write-Host "SHA-256: $Hash"
}
finally {
    Pop-Location
}
~~~

- [ ] Step 2: Replace the Bash helper

Use this complete script:

~~~bash
#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-finevision-to-sharegpt:latest}"
OUTPUT_TAR="${OUTPUT_TAR:-finevision-to-sharegpt_linux-amd64.tar}"
PLATFORM="${PLATFORM:-linux/amd64}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

case "${OUTPUT_TAR}" in
    /*) OUTPUT_PATH="${OUTPUT_TAR}" ;;
    *) OUTPUT_PATH="${PROJECT_ROOT}/${OUTPUT_TAR}" ;;
esac

mkdir -p "$(dirname "${OUTPUT_PATH}")"
cd "${PROJECT_ROOT}"

docker build --platform "${PLATFORM}" -t "${IMAGE_TAG}" .
docker save "${IMAGE_TAG}" -o "${OUTPUT_PATH}"

CHECKSUM_PATH="${OUTPUT_PATH}.sha256"
(cd "$(dirname "${OUTPUT_PATH}")" && sha256sum "$(basename "${OUTPUT_PATH}")" > "$(basename "${CHECKSUM_PATH}")")

SIZE_MB="$(du -m "${OUTPUT_PATH}" | cut -f1)"
HASH="$(cut -d' ' -f1 "${CHECKSUM_PATH}")"
echo "Saved Docker image ${IMAGE_TAG} to ${OUTPUT_PATH}"
echo "Size: ${SIZE_MB} MB"
echo "SHA-256: ${HASH}"
~~~

- [ ] Step 3: Update the README image section

Replace the existing direct build/save/load block with:

~~~markdown
## 构建镜像

Windows PowerShell：

~~~powershell
powershell -ExecutionPolicy Bypass -File scripts/build_docker_image.ps1
~~~

Linux/macOS：

~~~bash
bash scripts/build_docker_image.sh
~~~

默认生成：

~~~text
finevision-to-sharegpt_linux-amd64.tar
finevision-to-sharegpt_linux-amd64.tar.sha256
~~~

导入镜像：

~~~bash
docker load -i finevision-to-sharegpt_linux-amd64.tar
docker run --rm finevision-to-sharegpt:latest --help
~~~

数据集和输出目录在运行时挂载，不会打进镜像；翻译任务仍需能访问配置的模型后端。
~~~

- [ ] Step 4: Run helper contract tests and inspect the documentation diff

Run:

~~~powershell
python -m pytest tests/test_packaging.py::test_export_helpers_use_platform_specific_tar_and_checksum -q
git diff --check
~~~

Expected: the test passes and git diff --check emits no output.

- [ ] Step 5: Commit helper and documentation changes

~~~powershell
git add scripts/build_docker_image.ps1 scripts/build_docker_image.sh README.md
git commit -m "docs: document offline Docker image export"
~~~

## Task 5: Run the full test suite and build the image

**Files:**
- Generated: finevision-to-sharegpt_linux-amd64.tar
- Generated: finevision-to-sharegpt_linux-amd64.tar.sha256

- [ ] Step 1: Run all Python tests before invoking Docker

Run:

~~~powershell
python -m pytest -q
~~~

Expected: all tests pass, including the new packaging contract tests.

- [ ] Step 2: Build and export the Linux amd64 image

Run:

~~~powershell
powershell -ExecutionPolicy Bypass -File scripts/build_docker_image.ps1
~~~

Expected: Docker reports a successful build, the image is tagged finevision-to-sharegpt:latest, and both generated files exist in the project root.

- [ ] Step 3: Verify the container CLI and imports

Run:

~~~powershell
docker run --rm finevision-to-sharegpt:latest --help
docker run --rm --entrypoint python finevision-to-sharegpt:latest -c "import httpx, pyarrow, tqdm, finevision_to_sharegpt; print(httpx.__version__, pyarrow.__version__, tqdm.__version__)"
~~~

Expected: both commands exit with code 0; the second prints 0.28.1 25.0.1 4.70.0.

- [ ] Step 4: Verify the exported archive and checksum

Run:

~~~powershell
Get-Item finevision-to-sharegpt_linux-amd64.tar, finevision-to-sharegpt_linux-amd64.tar.sha256 | Select-Object Name,Length
Get-FileHash finevision-to-sharegpt_linux-amd64.tar -Algorithm SHA256
docker load --input finevision-to-sharegpt_linux-amd64.tar
docker image inspect finevision-to-sharegpt:latest --format '{{.Os}}/{{.Architecture}}'
~~~

Expected: both files are non-empty, the printed hash matches the sidecar, docker load succeeds, and image inspection prints linux/amd64.

- [ ] Step 5: Record the final artifact details

Run:

~~~powershell
docker image inspect finevision-to-sharegpt:latest --format '{{.Id}} {{.Size}}'
Get-FileHash finevision-to-sharegpt_linux-amd64.tar -Algorithm SHA256 | Format-List
~~~

Report the image ID, tar size, SHA-256, and exact import/run commands to the user. Do not commit the generated tar or checksum because .gitignore and .dockerignore intentionally exclude them.

- [ ] Step 6: Commit only source changes if any verification fix was needed

~~~powershell
git status --short
git diff --check
~~~

If a verification fix changed a tracked file, commit that file with a focused message; leave generated image artifacts untracked and available for delivery.

## Self-review checklist

- Spec coverage: dependency refresh is Task 2; Docker structure and exclusions are Task 3; offline tar/checksum are Tasks 4–5; CLI behavior and data-outside-image constraints are verified in Task 5; interface advice remains explicitly non-implemented in the spec.
- Placeholder scan: all commands, versions, filenames, and expected results above are concrete; no deferred implementation decisions remain.
- Type/name consistency: every task uses finevision-to-sharegpt_linux-amd64.tar, its .sha256 sidecar, the finevision-to-sharegpt:latest tag, and the same three dependency pins.

