# Intermediate Files and Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-dataset pre-translation `raw.jsonl` artifacts to `translate-zips` and a stable, ID-deduplicating `merge` CLI command that emits JSONL plus JSON.

**Architecture:** Keep raw emission as a translate-only side path immediately after parsing and image persistence, with one resume set per dataset. Keep merge logic in `json_io.py` as a streaming operation; the CLI only parses flags, calls it, and prints statistics. Existing export, translation routing, image storage, and record formats remain unchanged.

**Tech Stack:** Python 3.11+, argparse, pathlib, JSON/JSONL helpers, pytest, pyarrow fixtures.

---

### Task 1: Add the `emit_raw` configuration contract

**Files:**
- Modify: `tests/test_config_loader.py`
- Modify: `src/finevision_to_sharegpt/config_loader.py`
- Modify: `configs/translate_zips.json`
- Modify: `configs/translate_zips.example.json`

- [ ] **Step 1: Write failing configuration tests**

Add assertions proving omission defaults to `True` and an explicit JSON `false` loads as `False`:

```python
def test_load_zip_task_config_defaults_emit_raw_to_true(tmp_path):
    path = tmp_path / "zip.json"
    path.write_text(json.dumps({
        "dataset_registry": "datasets.json",
        "output_jsonl": "output/train.jsonl",
    }), encoding="utf-8")

    assert load_zip_task_config(path).emit_raw is True


def test_load_zip_task_config_can_disable_raw_output(tmp_path):
    path = tmp_path / "zip.json"
    path.write_text(json.dumps({
        "dataset_registry": "datasets.json",
        "output_jsonl": "output/train.jsonl",
        "emit_raw": False,
    }), encoding="utf-8")

    assert load_zip_task_config(path).emit_raw is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_config_loader.py --basetemp=.tmp/pytest-config -p no:cacheprovider -q`

Expected: both new tests fail because `ZipTaskConfig` has no `emit_raw` attribute.

- [ ] **Step 3: Implement the minimal configuration field**

Add to `ZipTaskConfig`:

```python
emit_raw: bool = True
```

Add to `load_zip_task_config`:

```python
emit_raw=bool(data.get("emit_raw", True)),
```

Add `"emit_raw": true` to both translate-zips configuration files, immediately before `"resume"`. Do not add it to export-zips configuration files.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_config_loader.py --basetemp=.tmp/pytest-config-green -p no:cacheprovider -q`

Expected: all config-loader tests pass.

### Task 2: Emit and resume-deduplicate per-dataset raw records

**Files:**
- Modify: `tests/test_zip_pipeline.py`
- Modify: `src/finevision_to_sharegpt/zip_pipeline.py`

- [ ] **Step 1: Write a failing test for the default raw artifact**

Extend the successful translate test to read `output/okvqa/raw.jsonl` and assert:

```python
raw_records = [
    json.loads(line)
    for line in (tmp_path / "output" / "okvqa" / "raw.jsonl").read_text(encoding="utf-8").splitlines()
]
assert [record["id"] for record in raw_records] == [
    "okvqa:nested/part.parquet:0",
    "okvqa:nested/part.parquet:1",
]
assert raw_records[0]["conversations"][0]["value"].endswith("Compare.")
assert raw_records[0]["images"] == records[0]["images"]
assert not (tmp_path / "output" / "okvqa" / "raw.json").exists()
```

- [ ] **Step 2: Write failing tests for disable, truncate, retry dedupe, and export isolation**

Add these focused helpers and cases:

```python
def write_translate_zip_config(tmp_path, registry, **overrides):
    data = {
        "dataset_registry": str(registry),
        "datasets": ["okvqa"],
        "output_jsonl": str(tmp_path / "output" / "train.jsonl"),
        "chinese_ratio": 1.0,
        "seed": 42,
        "resume": False,
    }
    data.update(overrides)
    path = tmp_path / "translate.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ConsumeOnlyPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            raise AssertionError(f"unexpected translation task: {task.id}")
        if False:
            yield None


class SuccessfulPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            yield type("Result", (), {
                "ok": True,
                "item": task,
                "value": handler(task, object(), 120),
                "error": None,
                "backend_name": "gpu0",
            })()


class FailingPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            yield type("Result", (), {
                "ok": False,
                "item": task,
                "value": None,
                "error": "temporary failure",
                "backend_name": "gpu0",
            })()


def test_run_translate_zips_emit_raw_false_does_not_create_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = write_translate_zip_config(
        tmp_path, registry, chinese_ratio=0.0, emit_raw=False
    )

    stats = run_translate_zips(
        load_zip_task_config(config_path), ConsumeOnlyPool(), lambda *_args: {}
    )

    assert stats["written"] == 2
    assert (tmp_path / "output" / "train.jsonl").exists()
    assert not (tmp_path / "output" / "okvqa" / "raw.jsonl").exists()


def test_run_translate_zips_resume_false_truncates_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    raw_path = tmp_path / "output" / "okvqa" / "raw.jsonl"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"id":"stale"}\n', encoding="utf-8")
    config_path = write_translate_zip_config(tmp_path, registry, chinese_ratio=0.0)

    run_translate_zips(
        load_zip_task_config(config_path), ConsumeOnlyPool(), lambda *_args: {}
    )

    raw_ids = [json.loads(line)["id"] for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert raw_ids == [
        "okvqa:nested/part.parquet:0",
        "okvqa:nested/part.parquet:1",
    ]


def test_run_translate_zips_resume_does_not_duplicate_raw_after_translation_failure(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = write_translate_zip_config(tmp_path, registry)
    config = load_zip_task_config(config_path)

    first_stats = run_translate_zips(config, FailingPool(), lambda *_args: {})

    raw_path = tmp_path / "output" / "okvqa" / "raw.jsonl"
    assert first_stats["failed"] == 2
    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 2

    write_translate_zip_config(tmp_path, registry, resume=True)

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    second_stats = run_translate_zips(
        load_zip_task_config(config_path), SuccessfulPool(), handler
    )

    raw_ids = [json.loads(line)["id"] for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert second_stats["written"] == 2
    assert len(raw_ids) == 2
    assert len(set(raw_ids)) == 2


def test_run_export_zips_does_not_create_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "export.json"
    config_path.write_text(json.dumps({
        "dataset_registry": str(registry),
        "datasets": ["okvqa"],
        "output_jsonl": str(tmp_path / "output" / "train_en.jsonl"),
        "resume": False,
    }), encoding="utf-8")

    run_export_zips(load_zip_task_config(config_path))

    assert not (tmp_path / "output" / "okvqa" / "raw.jsonl").exists()
```

- [ ] **Step 3: Run the raw tests and verify RED**

Run: `python -m pytest tests/test_zip_pipeline.py --basetemp=.tmp/pytest-raw-red -p no:cacheprovider -q`

Expected: raw-specific assertions fail because no raw output is implemented.

- [ ] **Step 4: Add raw path and resume helpers**

Implement focused helpers:

```python
def _raw_output_path(config: ZipTaskConfig, dataset_name: str) -> Path:
    safe = _safe_path_part(dataset_name)
    return config.output_jsonl.parent / safe / "raw.jsonl"


def _prepare_raw_outputs(
    config: ZipTaskConfig,
    datasets: list[tuple[RegisteredDataset, DatasetRequest]],
) -> dict[str, set[str]]:
    raw_done: dict[str, set[str]] = {}
    for dataset, _request in datasets:
        path = _raw_output_path(config, dataset.name)
        if not config.resume:
            truncate_file(path)
            raw_done[dataset.name] = set()
        else:
            raw_done[dataset.name] = load_completed_ids([path])
    return raw_done
```

Call `_prepare_raw_outputs` only from `run_translate_zips` and only when `config.emit_raw` is true. This keeps `run_export_zips` unchanged and ensures disabled raw output does not create a file.

- [ ] **Step 5: Write raw records before language routing**

Immediately after each `ParsedZipRow` is yielded to `chinese_tasks`, before calculating the ratio:

```python
if config.emit_raw and item.sample_id not in raw_done[item.dataset.name]:
    append_jsonl(
        _raw_output_path(config, item.dataset.name),
        build_sharegpt_record(item.sample, item.image_paths),
    )
    raw_done[item.dataset.name].add(item.sample_id)
```

Do not call `ImageStore.save` here; reuse `item.image_paths`. Do not create `raw.json` in finalization.

- [ ] **Step 6: Run raw tests and verify GREEN**

Run: `python -m pytest tests/test_zip_pipeline.py --basetemp=.tmp/pytest-raw-green -p no:cacheprovider -q`

Expected: all zip-pipeline tests pass, including failed-translation resume deduplication and export isolation.

### Task 3: Add stable merge I/O and CLI command

**Files:**
- Modify: `tests/test_json_io.py`
- Modify: `tests/test_cli.py`
- Modify: `src/finevision_to_sharegpt/json_io.py`
- Modify: `src/finevision_to_sharegpt/cli.py`

- [ ] **Step 1: Write the failing merge I/O test**

Add a test with two inputs containing one duplicate ID and two ID-less records:

```python
def test_merge_jsonl_files_stably_deduplicates_ids_and_writes_json(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text('{"id":"one","value":"first"}\n{"value":"no-id-a"}\n', encoding="utf-8")
    second.write_text('{"id":"one","value":"later"}\n{"id":"two"}\n{"value":"no-id-b"}\n', encoding="utf-8")

    stats = merge_jsonl_files([first, second], output)

    expected = [
        {"id": "one", "value": "first"},
        {"value": "no-id-a"},
        {"id": "two"},
        {"value": "no-id-b"},
    ]
    assert list(iter_json_records(output)) == expected
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == expected
    assert stats == {"read": 5, "written": 4, "duplicates": 1}
```

- [ ] **Step 2: Run the merge I/O test and verify RED**

Run: `python -m pytest tests/test_json_io.py --basetemp=.tmp/pytest-merge-io-red -p no:cacheprovider -q`

Expected: collection fails because `merge_jsonl_files` does not exist.

- [ ] **Step 3: Implement streaming merge in `json_io.py`**

```python
def merge_jsonl_files(inputs: list[Path], output: Path) -> dict[str, int]:
    output = Path(output)
    truncate_file(output)
    seen_ids: set[str] = set()
    stats = {"read": 0, "written": 0, "duplicates": 0}
    for input_path in inputs:
        for record in iter_json_records(input_path):
            stats["read"] += 1
            record_id = record.get("id")
            if record_id is not None:
                normalized_id = str(record_id)
                if normalized_id in seen_ids:
                    stats["duplicates"] += 1
                    continue
                seen_ids.add(normalized_id)
            append_jsonl(output, record)
            stats["written"] += 1
    jsonl_to_json_array(output, output.with_suffix(".json"))
    return stats
```

This deliberately has no ratio, count, sorting, shuffling, or content mutation.

- [ ] **Step 4: Run the merge I/O test and verify GREEN**

Run: `python -m pytest tests/test_json_io.py --basetemp=.tmp/pytest-merge-io-green -p no:cacheprovider -q`

Expected: all JSON I/O tests pass.

- [ ] **Step 5: Write failing CLI tests**

Update the exact command set to include `merge`, assert help contains it, and add:

```python
def test_cli_merge_writes_outputs_and_prints_stats(tmp_path, capsys):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text('{"id":"one"}\n', encoding="utf-8")
    second.write_text('{"id":"one"}\n{"id":"two"}\n', encoding="utf-8")

    code = main(["merge", "--inputs", str(first), str(second), "--output", str(output)])

    assert code == 0
    assert capsys.readouterr().out.strip() == (
        f"read=3 written=2 duplicates=1 output={output}"
    )
    assert [record["id"] for record in iter_json_records(output)] == ["one", "two"]
```

- [ ] **Step 6: Run CLI tests and verify RED**

Run: `python -m pytest tests/test_cli.py --basetemp=.tmp/pytest-merge-cli-red -p no:cacheprovider -q`

Expected: command-set and merge tests fail because the parser has no `merge` command.

- [ ] **Step 7: Add the CLI parser and dispatch branch**

Import `merge_jsonl_files`; add:

```python
merge = subparsers.add_parser("merge", help="merge JSONL files with stable id deduplication")
merge.add_argument("--inputs", nargs="+", required=True)
merge.add_argument("--output", required=True)
```

Dispatch before config-driven commands:

```python
if args.command == "merge":
    stats = merge_jsonl_files([Path(item) for item in args.inputs], Path(args.output))
    print(
        f"read={stats['read']} written={stats['written']} "
        f"duplicates={stats['duplicates']} output={args.output}"
    )
    return 0
```

- [ ] **Step 8: Run CLI tests and verify GREEN**

Run: `python -m pytest tests/test_cli.py --basetemp=.tmp/pytest-merge-cli-green -p no:cacheprovider -q`

Expected: all CLI tests pass and exactly five commands are exposed.

### Task 4: Document the three layers and merge workflow

**Files:**
- Modify: `README.md`
- Modify: `docs/运行说明.md`

- [ ] **Step 1: Update the README**

Document `emit_raw` in the translate-zips config example and field notes. Replace the output tree with:

```text
output/train.jsonl                    # translated layers combined
output/train.json
output/okvqa/raw.jsonl                # pre-translation English records
output/okvqa/train.jsonl              # translated/mixed records
output/okvqa/train.json
output/images/okvqa/<hash>.<ext>
```

State that raw and train reuse identical image paths, raw is not backfilled when enabled midway through a resumed job, and `export-zips` does not emit raw. Add the exact merge example and stable deduplication semantics.

- [ ] **Step 2: Update `docs/运行说明.md`**

Change the overview to four user workflows (three configured pipelines plus flag-driven merge), add `emit_raw` to translate-zips configuration and output explanations, and add a merge section containing:

```bash
python -m finevision_to_sharegpt merge \
  --inputs output/okvqa/train.jsonl output/chartqa/train.jsonl \
  --output output/merged.jsonl
```

Document that `merged.jsonl` and `merged.json` are overwritten outputs, input order is stable, first ID wins, ID-less records are preserved, and no sampling/shuffling occurs.

- [ ] **Step 3: Check documentation against actual CLI help**

Run: `python -m finevision_to_sharegpt merge --help`

Expected: help lists required `--inputs INPUTS [INPUTS ...]` and `--output OUTPUT` flags matching both documents.

### Task 5: Regression and acceptance verification

**Files:**
- Verify only; no planned production changes.

- [ ] **Step 1: Run focused changed-area tests**

Run: `python -m pytest tests/test_config_loader.py tests/test_zip_pipeline.py tests/test_json_io.py tests/test_cli.py --basetemp=.tmp/pytest-focused-final -p no:cacheprovider -q`

Expected: all focused tests pass.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest --basetemp=.tmp/pytest-full-final -p no:cacheprovider -q`

Expected: all tests pass with no failures or errors.

- [ ] **Step 3: Confirm legacy sampling code was not reintroduced**

Run: `rg -n "mixer|SampleRecipe|language_mix" src tests configs scripts`

Expected: no matches.

- [ ] **Step 4: Review acceptance scope**

Confirm source changes are limited to configuration loading, translate-only raw output, JSONL merge, and CLI dispatch; confirm no changes to `should_translate_to_chinese`, `ImageStore`, ShareGPT record shape, `run_export_zips`, or `translate-json` behavior.
