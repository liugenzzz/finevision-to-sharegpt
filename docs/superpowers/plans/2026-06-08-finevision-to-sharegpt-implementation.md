# finevision-to-sharegpt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a container-first Python CLI that converts local FineVision-style zip/parquet datasets into LLaMA-Factory-compatible ShareGPT JSONL with Chinese text translated by an internal Qwen3-VL endpoint.

**Architecture:** Keep pure data handling modules independent from model I/O so parsing, validation, image storage, and mixing are testable offline. The CLI orchestrates archive scanning, parquet row streaming, image hashing, translation, validation, and output writing. Docker packages the runtime and keeps all runtime data access local except the configured internal model API.

**Tech Stack:** Python 3.11, pytest, requests/httpx, pyarrow, Pillow, tqdm, Docker.

---

## File Structure

- `requirements.txt`: Runtime and test dependencies.
- `Dockerfile`: Container image for offline runtime execution after build.
- `README.md`: Docker-first usage, offline constraints, commands.
- `prompts/translate_sample_zh.txt`: Full-sample translation prompt.
- `prompts/translate_utterance_zh.txt`: Per-turn fallback prompt.
- `src/finevision_to_sharegpt/__init__.py`: Package marker and version.
- `src/finevision_to_sharegpt/models.py`: Dataclasses for source turns, parsed samples, translation results, and validation results.
- `src/finevision_to_sharegpt/image_store.py`: Image hashing, extension detection, and shared image directory writes.
- `src/finevision_to_sharegpt/sample_parser.py`: Row-to-single-image ShareGPT source parsing and rejection reasons.
- `src/finevision_to_sharegpt/validator.py`: ShareGPT/messages multimodal validation.
- `src/finevision_to_sharegpt/mixer.py`: Ratio/count JSONL mixing.
- `src/finevision_to_sharegpt/qwen_client.py`: OpenAI-compatible Qwen3-VL client with base64 image payloads.
- `src/finevision_to_sharegpt/translator.py`: Translation orchestration, prompt loading, retries, fallback, and quality checks.
- `src/finevision_to_sharegpt/parquet_reader.py`: Parquet row streaming.
- `src/finevision_to_sharegpt/archive.py`: Zip input discovery and parquet extraction to temp dirs.
- `src/finevision_to_sharegpt/cli.py`: `translate`, `validate`, and `mix` commands.
- `tests/`: Offline pytest coverage for core behavior.

## Tasks

### Task 1: Project Skeleton and Test Harness

**Files:**
- Create: `requirements.txt`
- Create: `src/finevision_to_sharegpt/__init__.py`
- Create: `tests/test_import.py`

- [ ] Write failing import test for package availability.
- [ ] Run `python -m pytest tests/test_import.py -v`; expect failure before package file exists.
- [ ] Add package marker and requirements.
- [ ] Run `python -m pytest tests/test_import.py -v`; expect pass.
- [ ] Commit with `git commit -m "chore: add python project skeleton"`.

### Task 2: Image Store

**Files:**
- Create: `src/finevision_to_sharegpt/image_store.py`
- Create: `tests/test_image_store.py`

- [ ] Write tests that JPEG/PNG bytes are saved under `images/<sha256>.<ext>` and repeated bytes reuse the same path.
- [ ] Run `python -m pytest tests/test_image_store.py -v`; expect import/function failure.
- [ ] Implement `detect_image_extension()` and `ImageStore.save()`.
- [ ] Run image store tests; expect pass.
- [ ] Commit with `git commit -m "feat: add hashed image store"`.

### Task 3: Sample Parser

**Files:**
- Create: `src/finevision_to_sharegpt/models.py`
- Create: `src/finevision_to_sharegpt/sample_parser.py`
- Create: `tests/test_sample_parser.py`

- [ ] Write tests for single-image conversation parsing, caption fallback, multi-image rejection, no-image rejection, and role normalization.
- [ ] Run `python -m pytest tests/test_sample_parser.py -v`; expect missing implementation failure.
- [ ] Implement dataclasses and `parse_row()`.
- [ ] Run parser tests; expect pass.
- [ ] Commit with `git commit -m "feat: parse finevision rows"`.

### Task 4: Validator

**Files:**
- Create: `src/finevision_to_sharegpt/validator.py`
- Create: `tests/test_validator.py`

- [ ] Write tests based on the user's prior scripts: image token count mismatch rejects, valid ShareGPT passes, messages format passes, empty conversations reject.
- [ ] Run `python -m pytest tests/test_validator.py -v`; expect failure.
- [ ] Implement `validate_record()`, `load_records()`, `dump_records()`, and `validate_file()`.
- [ ] Run validator tests; expect pass.
- [ ] Commit with `git commit -m "feat: validate multimodal sharegpt records"`.

### Task 5: Mixer

**Files:**
- Create: `src/finevision_to_sharegpt/mixer.py`
- Create: `tests/test_mixer.py`

- [ ] Write tests for count-based mixing, ratio-based mixing with total, deterministic seed, and unchanged image paths.
- [ ] Run `python -m pytest tests/test_mixer.py -v`; expect failure.
- [ ] Implement JSONL reading, sampling, shuffling, and writing.
- [ ] Run mixer tests; expect pass.
- [ ] Commit with `git commit -m "feat: add jsonl dataset mixer"`.

### Task 6: Qwen Client

**Files:**
- Create: `src/finevision_to_sharegpt/qwen_client.py`
- Create: `tests/test_qwen_client.py`

- [ ] Write tests using a fake transport/session to verify request URL, auth header, model field, base64 data URL image block, timeout handling, and response text extraction.
- [ ] Run `python -m pytest tests/test_qwen_client.py -v`; expect failure.
- [ ] Implement `QwenClient.chat()`.
- [ ] Run qwen client tests; expect pass.
- [ ] Commit with `git commit -m "feat: add qwen vision client"`.

### Task 7: Translator

**Files:**
- Create: `src/finevision_to_sharegpt/translator.py`
- Create: `prompts/translate_sample_zh.txt`
- Create: `prompts/translate_utterance_zh.txt`
- Create: `tests/test_translator.py`

- [ ] Write tests using a fake client: full-sample JSON translation succeeds, malformed JSON falls back per turn, `<image>` is inserted by program only, failed fallback returns failure metadata.
- [ ] Run `python -m pytest tests/test_translator.py -v`; expect failure.
- [ ] Implement prompt loading, strict JSON parsing, fallback, and output assembly.
- [ ] Run translator tests; expect pass.
- [ ] Commit with `git commit -m "feat: translate samples to sharegpt"`.

### Task 8: Archive and Parquet Streaming

**Files:**
- Create: `src/finevision_to_sharegpt/parquet_reader.py`
- Create: `src/finevision_to_sharegpt/archive.py`
- Create: `tests/test_archive_parquet.py`

- [ ] Write tests that create a tiny parquet inside a zip, discover it, extract it, and stream rows.
- [ ] Run `python -m pytest tests/test_archive_parquet.py -v`; expect failure.
- [ ] Implement zip discovery, parquet extraction, and row streaming with pyarrow.
- [ ] Run archive/parquet tests; expect pass.
- [ ] Commit with `git commit -m "feat: read parquet rows from zip inputs"`.

### Task 9: CLI

**Files:**
- Create: `src/finevision_to_sharegpt/cli.py`
- Create: `tests/test_cli.py`

- [ ] Write CLI tests for `validate` and `mix`; keep `translate` orchestration covered with fake components and no network.
- [ ] Run `python -m pytest tests/test_cli.py -v`; expect failure.
- [ ] Implement argparse commands and console entry via `python -m finevision_to_sharegpt.cli`.
- [ ] Run CLI tests; expect pass.
- [ ] Commit with `git commit -m "feat: add command line interface"`.

### Task 10: Docker and Docs

**Files:**
- Create: `Dockerfile`
- Create: `README.md`
- Modify: `requirements.txt`

- [ ] Write Dockerfile with Python 3.11, install requirements, copy source/prompts, and set entrypoint.
- [ ] Write README with offline runtime guarantee, Docker examples, translate/validate/mix examples, and remote build notes.
- [ ] Run `python -m pytest -v`; expect pass.
- [ ] Run `python -m finevision_to_sharegpt.cli --help`; expect help output.
- [ ] If Docker is available, run `docker build -t finevision-to-sharegpt:latest .`; expect successful image build.
- [ ] Commit with `git commit -m "docs: add docker usage and project documentation"`.

## Self-Review

- Spec coverage: plan covers container runtime, local zip/parquet input, one zip to one JSONL, shared hashed images, ShareGPT output, single-image filtering, Qwen base64 calls, no runtime public internet, resume via JSONL, validation, mixing, Docker, and docs.
- Placeholder scan: no task relies on unspecified implementation placeholders.
- Type consistency: modules use shared dataclasses from `models.py`; CLI delegates to parser, validator, mixer, translator, archive, and parquet modules.
