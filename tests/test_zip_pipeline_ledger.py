"""db-mode wiring: the pipeline drives the ledger, the ledger drives the scan."""

import json
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finevision_to_sharegpt import zip_pipeline
from finevision_to_sharegpt.config_loader import load_zip_task_config
from finevision_to_sharegpt.db import ConsumptionLedger, DatasetVersion, ScanPlan
from finevision_to_sharegpt.zip_pipeline import run_export_zips, run_scan_zips, run_translate_zips


class RecordingLedger(ConsumptionLedger):
    """A ledger that behaves like MySQL mode and records every call."""

    def __init__(self, consumed=None, plan=None):
        self.consumed = set(consumed or ())
        self.plan = plan or ScanPlan()
        self.claims = []
        self.done = []
        self.failed = []
        self.rejected = []
        self.translations = []
        self.scanned = []
        self.finished = []
        self.ingest_plans = []
        self.flushes = 0
        self.closed = False

    def open_dataset(self, dataset, zip_path, images_root):
        return DatasetVersion(dataset=dataset, version_id=7, source_hash="deadbeef")

    def scan_plan(self, version, parquet_name, for_ingest=False):
        self.ingest_plans.append(for_ingest)
        return self.plan

    def is_consumed(self, version, sample_id, row_index, plan):
        return sample_id in self.consumed

    def claim(self, version, parquet_name, row_index, sample_id, conversations, image_paths, status="claimed"):
        self.claims.append((sample_id, row_index, status, conversations, image_paths))

    def mark_rejected(self, version, parquet_name, row_index, sample_id, reason):
        self.rejected.append((sample_id, reason))

    def mark_done(self, version, sample_id, lang):
        self.done.append((sample_id, lang))

    def mark_failed(self, version, sample_id, error):
        self.failed.append((sample_id, error))

    def record_translation(
        self, version, sample_id, conversations, backend_name, model_name, prompt_version, latency_ms
    ):
        self.translations.append((sample_id, backend_name, model_name, prompt_version, latency_ms))

    def note_scanned(self, version, parquet_name, row_index):
        self.scanned.append(row_index)

    def finish_parquet(self, version, parquet_name):
        self.finished.append(parquet_name)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


def make_zip_dataset(tmp_path, rows=4, dataset_name="okvqa", include_bad_row=False):
    data_root = tmp_path / "zips"
    data_root.mkdir(exist_ok=True)
    images = [[b"\xff\xd8\xff%d" % index] for index in range(rows)]
    texts = [[{"user": f"Q{index}", "assistant": f"A{index}"}] for index in range(rows)]
    if include_bad_row:
        images.append([])
        texts.append([{"user": "Q", "assistant": "A"}])
    parquet_path = tmp_path / "part.parquet"
    pq.write_table(pa.table({"images": images, "texts": texts}), parquet_path, row_group_size=2)
    zip_path = data_root / f"{dataset_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(parquet_path, arcname="nested/part.parquet")
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps({"data_root": str(data_root), "datasets": {dataset_name: {"zip": f"{dataset_name}.zip"}}}),
        encoding="utf-8",
    )
    return registry


def write_config(tmp_path, registry, name="task.json", **overrides):
    data = {
        "dataset_registry": str(registry),
        "datasets": ["okvqa"],
        "output_jsonl": str(tmp_path / "output" / "train.jsonl"),
        "chinese_ratio": 1.0,
        "seed": 42,
        "resume": False,
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def use_ledger(monkeypatch, ledger):
    monkeypatch.setattr(zip_pipeline, "open_ledger", lambda *args, **kwargs: ledger)
    return ledger


class _SuccessfulPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            if task.metadata is not None:
                task.metadata["latency_ms"] = 42
            yield type(
                "Result",
                (),
                {
                    "ok": True,
                    "item": task,
                    "value": handler(task, object(), 120),
                    "error": None,
                    "backend_name": "gpu0",
                },
            )()


class _FailingPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            yield type(
                "Result",
                (),
                {"ok": False, "item": task, "value": None, "error": "boom", "backend_name": "gpu0"},
            )()


def _identity_handler(task, client, timeout):
    return {
        "id": task.id,
        "images": task.image_paths,
        "conversations": [{"from": "human", "value": "问"}, {"from": "gpt", "value": "答"}],
    }


# -- export ----------------------------------------------------------------


def test_export_claims_then_marks_each_row_done(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    stats = run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    assert stats["written"] == 4
    assert [claim[0] for claim in ledger.claims] == [f"okvqa:nested/part.parquet:{i}" for i in range(4)]
    assert all(claim[2] == "claimed" for claim in ledger.claims)
    assert ledger.done == [(f"okvqa:nested/part.parquet:{i}", "en") for i in range(4)]
    assert ledger.scanned == [0, 1, 2, 3]
    assert ledger.finished == ["nested/part.parquet"]
    assert ledger.closed


def test_claimed_rows_carry_conversations_and_image_paths(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=1)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    _sample_id, _row, _status, conversations, image_paths = ledger.claims[0]
    assert conversations == [{"from": "human", "value": "Q0"}, {"from": "gpt", "value": "A0"}]
    assert image_paths == [path for path in image_paths if path.startswith("images/okvqa/")]
    assert len(image_paths) == 1


def test_rows_the_ledger_reports_consumed_are_skipped(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path)
    already = {f"okvqa:nested/part.parquet:{i}" for i in (0, 2)}
    ledger = use_ledger(monkeypatch, RecordingLedger(consumed=already))

    stats = run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    assert stats["written"] == 2
    assert stats["skipped"] == 2
    assert [claim[0] for claim in ledger.claims] == [
        "okvqa:nested/part.parquet:1",
        "okvqa:nested/part.parquet:3",
    ]
    # Skipped rows still advance the watermark.
    assert ledger.scanned == [0, 1, 2, 3]


def test_scan_plan_start_row_skips_the_head_of_the_parquet(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path)
    ledger = use_ledger(monkeypatch, RecordingLedger(plan=ScanPlan(start_row=2, gap_end=2)))

    stats = run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    assert stats["written"] == 2
    assert stats["skipped"] == 0
    assert [claim[1] for claim in ledger.claims] == [2, 3]


def test_rejected_rows_are_recorded_in_the_ledger(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=2, include_bad_row=True)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    stats = run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    assert stats["rejected"] == 1
    assert ledger.rejected == [("okvqa:nested/part.parquet:2", "missing_image")]
    assert len(ledger.claims) == 2


def test_limit_stops_the_scan_without_marking_the_parquet_finished(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    stats = run_export_zips(
        load_zip_task_config(write_config(tmp_path, registry, limit_per_dataset=2))
    )

    assert stats["written"] == 2
    assert ledger.finished == []


# -- translate --------------------------------------------------------------


def test_translation_success_records_translation_and_marks_done(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=2)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_translate_zips(
        load_zip_task_config(write_config(tmp_path, registry)),
        _SuccessfulPool(),
        _identity_handler,
        translation_meta={"models_by_backend": {"gpu0": "Qwen3-VL"}, "prompt_version": "abc123"},
    )

    assert [entry[1:] for entry in ledger.translations] == [("gpu0", "Qwen3-VL", "abc123", 42)] * 2
    assert sorted(ledger.done) == sorted(
        (f"okvqa:nested/part.parquet:{i}", "zh") for i in range(2)
    )


def test_translation_failure_marks_failed_not_done(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=2)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_translate_zips(
        load_zip_task_config(write_config(tmp_path, registry)),
        _FailingPool(),
        _identity_handler,
    )

    assert [entry[0] for entry in ledger.failed] == [
        f"okvqa:nested/part.parquet:{i}" for i in range(2)
    ]
    assert ledger.done == []
    assert ledger.translations == []


def test_english_share_is_marked_done_without_a_translation_row(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=2)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_translate_zips(
        load_zip_task_config(write_config(tmp_path, registry, chinese_ratio=0.0)),
        _SuccessfulPool(),
        _identity_handler,
    )

    assert ledger.done == [(f"okvqa:nested/part.parquet:{i}", "en") for i in range(2)]
    assert ledger.translations == []


def test_unknown_backend_records_an_empty_model_name(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=1)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_translate_zips(
        load_zip_task_config(write_config(tmp_path, registry)),
        _SuccessfulPool(),
        _identity_handler,
        translation_meta={"models_by_backend": {}, "prompt_version": "v1"},
    )

    assert ledger.translations[0][2] == ""


# -- scan-only --------------------------------------------------------------


def test_run_scan_zips_requires_a_mysql_section(tmp_path):
    registry = make_zip_dataset(tmp_path)

    with pytest.raises(ValueError, match="mysql"):
        run_scan_zips(load_zip_task_config(write_config(tmp_path, registry)))


def test_run_scan_zips_ingests_as_pending_and_writes_no_output(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=3)
    ledger = use_ledger(monkeypatch, RecordingLedger())
    config_path = write_config(
        tmp_path,
        registry,
        mysql={"host": "db", "user": "fv", "database": "finevision"},
    )

    stats = run_scan_zips(load_zip_task_config(config_path))

    assert stats["written"] == 3
    assert all(claim[2] == "pending" for claim in ledger.claims)
    assert ledger.done == []
    assert not (tmp_path / "output" / "train.jsonl").exists()
    # Bulk load resumes at the watermark, so a rerun re-reads nothing.
    assert ledger.ingest_plans == [True]


def test_consume_paths_do_not_use_the_ingest_plan(tmp_path, monkeypatch):
    registry = make_zip_dataset(tmp_path, rows=2)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    run_export_zips(load_zip_task_config(write_config(tmp_path, registry)))

    # A pending row below the watermark is exactly what consuming looks for.
    assert ledger.ingest_plans == [False]


def test_the_ledger_is_closed_even_if_setup_fails_afterwards(tmp_path, monkeypatch):
    """A leaked connection lives on for wait_timeout, so failures must close it."""

    registry = make_zip_dataset(tmp_path, rows=2)
    ledger = use_ledger(monkeypatch, RecordingLedger())

    def boom(*args, **kwargs):
        raise RuntimeError("setup blew up after the ledger was opened")

    monkeypatch.setattr(zip_pipeline, "_prepare_raw_outputs", boom)

    with pytest.raises(RuntimeError, match="blew up"):
        run_translate_zips(
            load_zip_task_config(write_config(tmp_path, registry, emit_raw=True)),
            _SuccessfulPool(),
            _identity_handler,
        )

    assert ledger.closed
