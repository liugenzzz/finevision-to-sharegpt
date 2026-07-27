# Multibackend Multimage Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build three container-oriented data commands: translate existing JSON, translate registered zip datasets by language ratio, and export registered zip datasets without translation.

**Architecture:** Keep `cli.py` as a thin compatibility entrypoint and move reusable behavior into focused modules. Add multi-image sample data, streaming JSONL/JSON utilities, dataset registry loading, fixed-concurrency backend pools, and zip pipelines that feed the same translation executor.

**Tech Stack:** Python 3.11, argparse, pyarrow, httpx, pytest, existing package layout under `src/finevision_to_sharegpt`.

---

## Task List

### Task 1: Multi-Image Core Model And Record Formatting

**Files:**
- Modify: `src/finevision_to_sharegpt/models.py`
- Modify: `src/finevision_to_sharegpt/translator.py`
- Modify: `src/finevision_to_sharegpt/sample_parser.py`
- Modify: `src/finevision_to_sharegpt/qwen_client.py`
- Test: `tests/test_translator.py`
- Test: `tests/test_sample_parser.py`
- Test: `tests/test_qwen_client.py`

- [ ] Write failing tests proving `SourceSample` accepts multiple images, parser does not reject multi-image rows, translator inserts one `<image>` token per image, and Qwen payload sends multiple `image_url` entries.
- [ ] Run targeted tests and verify they fail because production code is still single-image.
- [ ] Change `SourceSample` to store `image_bytes_list: list[bytes]` with an `image_bytes` compatibility property for old tests.
- [ ] Update `parse_row()` to accept multiple images and preserve all image bytes.
- [ ] Update `translate_sample()` and `build_sharegpt_record()` to insert repeated image tokens into the first human turn.
- [ ] Update `QwenClient.chat()` to accept `image_bytes` as either bytes or list of bytes and emit all images in one message content list.
- [ ] Run targeted tests until green.

### Task 2: Streaming JSON IO And Resume Utilities

**Files:**
- Create: `src/finevision_to_sharegpt/json_io.py`
- Modify: `src/finevision_to_sharegpt/validator.py`
- Test: `tests/test_json_io.py`
- Test: `tests/test_validator.py`

- [ ] Write failing tests for streaming JSONL append, JSONL-to-JSON array conversion, completed-id scanning from JSONL and JSON arrays, and multi-image validator token counts.
- [ ] Run targeted tests and verify expected failures due to missing module or old validator behavior.
- [ ] Implement `append_jsonl()`, `iter_json_records()`, `load_records_by_id()`, `load_completed_ids()`, `jsonl_to_json_array()`, and `truncate_file()`.
- [ ] Update validator so `len(images)` must equal total `<image>` token count, allowing multi-image records.
- [ ] Run targeted tests until green.

### Task 3: Dataset Registry, Config Loading, And Image Layout

**Files:**
- Create: `src/finevision_to_sharegpt/config_loader.py`
- Create: `src/finevision_to_sharegpt/dataset_registry.py`
- Modify: `src/finevision_to_sharegpt/image_store.py`
- Create: `configs/datasets.example.json`
- Create: `configs/backend_config.example.json`
- Create: `configs/translate_json.example.json`
- Create: `configs/translate_zips.example.json`
- Create: `configs/export_zips.example.json`
- Test: `tests/test_config_loader.py`
- Test: `tests/test_dataset_registry.py`
- Test: `tests/test_image_store.py`

- [ ] Write failing tests for loading config JSON, resolving dataset names to zip paths, wildcard dataset selection, per-dataset ratio override, and saving images under `images/<dataset>/<hash>.<ext>`.
- [ ] Run targeted tests and verify failures.
- [ ] Implement dataclasses and loader helpers for backend, JSON translation, zip translation, and zip export configs.
- [ ] Implement dataset registry resolution using `data_root`.
- [ ] Extend `ImageStore.save()` with optional `dataset_name`.
- [ ] Add example config files.
- [ ] Run targeted tests until green.

### Task 4: Fixed-Concurrency Backend Pool

**Files:**
- Create: `src/finevision_to_sharegpt/backend_pool.py`
- Test: `tests/test_backend_pool.py`

- [ ] Write failing tests for fixed backend concurrency, retry behavior, failed result emission, and disabling a backend after consecutive failures.
- [ ] Run targeted tests and verify failures.
- [ ] Implement backend config normalization, worker pool startup, bounded queue submission, retry loop, and backend disable logic.
- [ ] Keep failure handling per-record: failed tasks return error metadata instead of raising out of the whole job.
- [ ] Run targeted tests until green.

### Task 5: Translation Job Executor

**Files:**
- Create: `src/finevision_to_sharegpt/translation_job.py`
- Test: `tests/test_translation_job.py`

- [ ] Write failing tests for translating records into JSONL, writing done and failed JSONL, skipping completed ids, preserving image paths for `translate-json`, and writing final JSON from JSONL.
- [ ] Run targeted tests and verify failures.
- [ ] Implement a streaming translation executor that accepts `TranslationTask` items and a backend pool or single-client fallback.
- [ ] Implement completed-id checks from done/output files.
- [ ] Ensure successful records are appended to output JSONL and done JSONL immediately.
- [ ] Ensure final JSON is generated from output JSONL with `jsonl_to_json_array()`.
- [ ] Run targeted tests until green.

### Task 6: Zip Pipeline And Ratio Routing

**Files:**
- Create: `src/finevision_to_sharegpt/zip_pipeline.py`
- Test: `tests/test_zip_pipeline.py`

- [ ] Write failing tests for registered zip streaming, multi-image export, dataset-name image folders, stable hash language routing, per-dataset ratio application, rejected records, resume skip, and report counts.
- [ ] Run targeted tests and verify failures.
- [ ] Implement zip/parquet sample iteration over registered datasets.
- [ ] Implement stable language routing using `seed + sample_id`.
- [ ] Implement English export records without model calls.
- [ ] Implement Chinese translation task generation for translation executor.
- [ ] Write report JSON with per-dataset processed/english/chinese/rejected/failed/skipped counts.
- [ ] Run targeted tests until green.

### Task 7: CLI Commands And Container-Internal Scripts

**Files:**
- Modify: `src/finevision_to_sharegpt/cli.py`
- Create: `scripts/in_translate_json.sh`
- Create: `scripts/in_translate_zips.sh`
- Create: `scripts/in_export_zips.sh`
- Test: `tests/test_cli.py`

- [ ] Write failing tests that `translate-json --config`, `translate-zips --config`, and `export-zips --config` parse and invoke the correct runners.
- [ ] Write failing CLI integration tests for each new command using temporary configs.
- [ ] Run targeted tests and verify failures.
- [ ] Wire new command handlers to config loaders and runner functions.
- [ ] Keep old `translate`, old positional `translate-json`, and `sample` compatibility where practical.
- [ ] Add three container-internal scripts that accept exactly one config path and call the CLI.
- [ ] Run targeted tests until green.

### Task 8: Full Regression And Cleanup

**Files:**
- Modify as needed based on failures.
- Test: all test files.

- [ ] Run `pytest`.
- [ ] Fix regressions with failing tests first when behavior is missing.
- [ ] Run `pytest` again until all tests pass.
- [ ] Scan for old duplicated JSON writer and ad hoc append helpers in `cli.py`; move callers to `json_io.py`.
- [ ] Confirm no production path keeps full output in memory except small id sets for resume.
- [ ] Confirm final config examples match CLI behavior.

## Execution Notes

- This workspace is not a git repository, so commit steps are intentionally omitted.
- TDD is required for each behavior change: write the test, verify it fails, implement, verify it passes.
- Existing scripts that wrap external `docker run` are not the main path. The new scripts are container-internal and accept config files.
