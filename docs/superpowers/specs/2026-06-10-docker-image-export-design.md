# Docker Image Export Design

## Goal

Package `finevision-to-sharegpt` as a reproducible Docker image and export it as a tar archive that can be copied to another host and loaded with `docker load`.

## Scope

- Build image tag: `finevision-to-sharegpt:latest`.
- Target platform: `linux/amd64` by default, which matches typical x86_64 Linux servers.
- Export archive: `finevision-to-sharegpt_latest.tar`.
- Keep runtime data outside the image. Users mount input, output, and optional config paths at `docker run` time.
- Do not include local caches, test caches, virtual environments, archives, generated outputs, or large dataset files in the Docker build context.

## Architecture

The image uses `python:3.11-slim` and installs the project as a Python package from the local source tree. The installed console script `finevision-to-sharegpt` is the container entrypoint, so users can pass subcommands such as `translate`, `sample`, `translate-json`, `validate`, and `mix`.

The build copies only packaging metadata, dependencies, source code, prompts, configs, and README content required by the CLI. A `.dockerignore` file keeps the build context small and prevents accidental inclusion of data or existing image tar files.

## Export Workflow

A PowerShell helper script builds the image on Windows and saves it to `finevision-to-sharegpt_latest.tar` by default. A Bash helper script provides the same workflow for Linux hosts. Both scripts accept optional image tag, output path, and platform parameters for later reuse.

## Verification

- Run unit tests with `python -m pytest -q` if dependencies are available locally.
- Build the Docker image with `docker build --platform linux/amd64`.
- Verify the container CLI starts with `docker run --rm finevision-to-sharegpt:latest --help`.
- Export the image with `docker save`.
- Confirm the tar file exists and has non-zero size.
