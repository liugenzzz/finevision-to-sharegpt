"""End-to-end db mode against a real server.

Skipped unless ``FV_TEST_MYSQL`` is set to a JSON object with connection
settings, e.g.::

    FV_TEST_MYSQL='{"host":"127.0.0.1","port":3307,"user":"fv",
                    "password":"fv","database":"finevision",
                    "collation":"utf8mb4_general_ci"}' pytest tests/test_db_integration.py
"""

import json
import os
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finevision_to_sharegpt.config_loader import load_zip_task_config
from finevision_to_sharegpt.db import load_mysql_config
from finevision_to_sharegpt.db_commands import run_db_export, run_db_status
from finevision_to_sharegpt.zip_pipeline import run_export_zips, run_scan_zips, run_translate_zips

pytest.importorskip("pymysql")

_SETTINGS = os.getenv("FV_TEST_MYSQL")
pytestmark = pytest.mark.skipif(not _SETTINGS, reason="FV_TEST_MYSQL is not configured")


@pytest.fixture()
def mysql_settings():
    return json.loads(_SETTINGS)


@pytest.fixture()
def clean_database(mysql_settings):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    config = load_mysql_config(mysql_settings)
    ledger = MySQLLedger(config)
    ledger.pool.run(lambda cursor: cursor.execute("DROP TABLE IF EXISTS sample_translation"))
    ledger.pool.run(lambda cursor: cursor.execute("DROP TABLE IF EXISTS sample_source"))
    ledger.pool.run(lambda cursor: cursor.execute("DROP TABLE IF EXISTS dataset_cursor"))
    ledger.pool.run(lambda cursor: cursor.execute("DROP TABLE IF EXISTS dataset_version"))
    ledger.ensure_schema()
    ledger.close()
    return config


def make_zip_dataset(tmp_path, rows=10, dataset_name="okvqa", salt=b""):
    data_root = tmp_path / "zips"
    data_root.mkdir(exist_ok=True)
    parquet_path = tmp_path / f"{dataset_name}.parquet"
    pq.write_table(
        pa.table(
            {
                "images": [[b"\xff\xd8\xff" + salt + str(i).encode()] for i in range(rows)],
                "texts": [[{"user": f"Q{i}", "assistant": f"A{i}"}] for i in range(rows)],
            }
        ),
        parquet_path,
        row_group_size=2,
    )
    zip_path = data_root / f"{dataset_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(parquet_path, arcname="nested/part.parquet")
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps({"data_root": str(data_root), "datasets": {dataset_name: {"zip": f"{dataset_name}.zip"}}}),
        encoding="utf-8",
    )
    return registry


def write_config(tmp_path, registry, mysql_settings, name="task.json", **overrides):
    data = {
        "dataset_registry": str(registry),
        "datasets": ["okvqa"],
        "output_jsonl": str(tmp_path / name.replace(".json", "") / "train.jsonl"),
        "chinese_ratio": 1.0,
        "seed": 42,
        "resume": False,
        "mysql": mysql_settings,
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_zip_task_config(path), path


class _SuccessfulPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
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


def _handler(task, client, timeout):
    return {
        "id": task.id,
        "images": task.image_paths,
        "conversations": [{"from": "human", "value": "问"}, {"from": "gpt", "value": "答"}],
    }


def _ids(path):
    return [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_schema_creation_is_idempotent(clean_database):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    ledger = MySQLLedger(clean_database)
    ledger.ensure_schema()
    ledger.ensure_view("okvqa")
    ledger.ensure_view("okvqa")
    ledger.close()


def test_two_runs_with_separate_outputs_never_repeat_a_sample(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=10)
    first, _ = write_config(tmp_path, registry, mysql_settings, name="first.json", limit_per_dataset=4)
    second, _ = write_config(tmp_path, registry, mysql_settings, name="second.json", limit_per_dataset=4)

    first_stats = run_export_zips(first)
    second_stats = run_export_zips(second)

    first_ids = _ids(first.output_jsonl)
    second_ids = _ids(second.output_jsonl)
    assert first_stats["written"] == 4
    assert second_stats["written"] == 4
    assert set(first_ids).isdisjoint(second_ids)
    assert second_ids == [f"okvqa:nested/part.parquet:{i}" for i in range(4, 8)]


def test_rebuilt_archive_starts_a_fresh_version(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=4)
    config, _ = write_config(tmp_path, registry, mysql_settings, name="first.json")
    run_export_zips(config)

    # Same dataset name, different content: a new source_hash, so a clean slate.
    make_zip_dataset(tmp_path, rows=4, salt=b"v2")
    second, second_path = write_config(tmp_path, registry, mysql_settings, name="second.json")
    stats = run_export_zips(second)

    assert stats["written"] == 4
    assert stats["skipped"] == 0
    status = run_db_status(second_path)
    assert len({row["source_hash"] for row in status["rows"]}) == 2
    assert status["totals"]["done"] == 8


def test_translation_rows_land_with_model_attribution(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=3)
    config, config_path = write_config(tmp_path, registry, mysql_settings)

    run_translate_zips(
        config,
        _SuccessfulPool(),
        _handler,
        translation_meta={"models_by_backend": {"gpu0": "Qwen3-VL"}, "prompt_version": "abc123"},
    )

    export = run_db_export(config_path, tmp_path / "exported.jsonl")
    records = [json.loads(line) for line in (tmp_path / "exported.jsonl").read_text(encoding="utf-8").splitlines()]
    assert export["written"] == 3
    assert all(record["conversations"][1]["value"] == "答" for record in records)
    assert all(record["images"] for record in records)


def test_english_share_exports_the_untranslated_text(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=3)
    config, config_path = write_config(tmp_path, registry, mysql_settings, chinese_ratio=0.0)

    run_translate_zips(config, _SuccessfulPool(), _handler)
    run_db_export(config_path, tmp_path / "exported.jsonl")

    records = [json.loads(line) for line in (tmp_path / "exported.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["conversations"][1]["value"] for record in records] == ["A0", "A1", "A2"]


def test_db_scan_ingests_pending_rows_that_a_later_run_consumes(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=6)
    scan_config, scan_path = write_config(tmp_path, registry, mysql_settings, name="scan.json")

    scan_stats = run_scan_zips(scan_config)
    status = run_db_status(scan_path)

    assert scan_stats["written"] == 6
    assert status["totals"] == {"pending": 6}

    export_config, export_path = write_config(
        tmp_path, registry, mysql_settings, name="export.json", limit_per_dataset=2
    )
    export_stats = run_export_zips(export_config)

    assert export_stats["written"] == 2
    assert run_db_status(export_path)["totals"] == {"pending": 4, "done": 2}


def test_export_filters_by_dataset_and_batch(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=4)
    config, config_path = write_config(tmp_path, registry, mysql_settings, batch_id="batch-a")
    run_export_zips(config)

    matching = run_db_export(config_path, tmp_path / "a.jsonl", batch_id="batch-a")
    other = run_db_export(config_path, tmp_path / "b.jsonl", batch_id="batch-b")
    wrong_dataset = run_db_export(config_path, tmp_path / "c.jsonl", dataset="missing")

    assert matching["written"] == 4
    assert other["written"] == 0
    assert wrong_dataset["written"] == 0


def test_expired_claims_are_recovered_on_the_next_run(tmp_path, clean_database, mysql_settings):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    registry = make_zip_dataset(tmp_path, rows=4)
    config, config_path = write_config(tmp_path, registry, mysql_settings, name="first.json")
    run_export_zips(config)

    # Simulate a crash between claim and completion on two rows.
    ledger = MySQLLedger(load_mysql_config(mysql_settings))
    ledger.pool.run(
        lambda cursor: cursor.execute(
            "UPDATE sample_source SET status='claimed', done_at=NULL, "
            "claim_expires_at=DATE_SUB(NOW(), INTERVAL 1 HOUR) WHERE row_index IN (1, 2)"
        )
    )
    ledger.close()

    second, second_path = write_config(tmp_path, registry, mysql_settings, name="second.json")
    stats = run_export_zips(second)

    assert stats["written"] == 2
    assert _ids(second.output_jsonl) == [
        "okvqa:nested/part.parquet:1",
        "okvqa:nested/part.parquet:2",
    ]
    assert run_db_status(second_path)["totals"] == {"done": 4}
