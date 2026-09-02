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
    """换个输出目录续跑，不会重复抽到同一条样本。

    `limit` 是总量而非每轮增量，所以第二轮要拿到新数据得把配额调大——
    仍是 4 的话账本会正确判定配额已满而一条不写。
    """

    registry = make_zip_dataset(tmp_path, rows=10)
    first, _ = write_config(tmp_path, registry, mysql_settings, name="first.json", limit_per_dataset=4)
    same_quota, _ = write_config(tmp_path, registry, mysql_settings, name="same.json", limit_per_dataset=4)
    raised, _ = write_config(tmp_path, registry, mysql_settings, name="second.json", limit_per_dataset=8)

    first_stats = run_export_zips(first)
    same_stats = run_export_zips(same_quota)
    second_stats = run_export_zips(raised)

    first_ids = _ids(first.output_jsonl)
    second_ids = _ids(raised.output_jsonl)
    assert first_stats["written"] == 4
    assert same_stats["written"] == 0
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


def test_scan_without_images_defers_the_files_but_keeps_the_paths(
    tmp_path, clean_database, mysql_settings
):
    registry = make_zip_dataset(tmp_path, rows=6)
    scan_config, scan_path = write_config(tmp_path, registry, mysql_settings, name="scan.json")
    use_config, _ = write_config(
        tmp_path, registry, mysql_settings, name="use.json", limit_per_dataset=2
    )

    run_scan_zips(scan_config, write_images=False)
    scanned_images = sorted(path.name for path in tmp_path.rglob("*.jpg"))
    recorded = json.loads(
        _query_one(mysql_settings, "SELECT image_paths FROM sample_source ORDER BY row_index LIMIT 1")
    )

    assert run_db_status(scan_path)["totals"] == {"pending": 6}
    assert scanned_images == []

    run_export_zips(use_config)
    written = sorted(path.name for path in use_config.images_root.rglob("*.jpg"))

    # Only the consumed samples materialise, and at the path the scan recorded.
    assert len(written) == 2
    assert (use_config.images_root.parent / recorded[0]).exists() == (recorded[0].split("/")[-1] in written)


def _query_one(mysql_settings, sql):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    ledger = MySQLLedger(load_mysql_config(mysql_settings))
    try:
        def query(cursor):
            cursor.execute(sql)
            return cursor.fetchone()[0]

        return ledger.pool.run(query)
    finally:
        ledger.close()


def test_db_scan_resumes_instead_of_rereading(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=8)
    scan_config, scan_path = write_config(tmp_path, registry, mysql_settings, name="scan.json")

    first = run_scan_zips(scan_config, write_images=False)
    second = run_scan_zips(scan_config, write_images=False)

    assert first["written"] == 8
    # A bulk load that dies partway must not redo the parquet reads on restart.
    assert second["written"] == 0
    assert second["processed"] == 0
    assert run_db_status(scan_path)["totals"] == {"pending": 8}


def test_pending_rows_are_still_claimable_after_a_scan(tmp_path, clean_database, mysql_settings):
    registry = make_zip_dataset(tmp_path, rows=8)
    scan_config, scan_path = write_config(tmp_path, registry, mysql_settings, name="scan.json")
    use_config, _ = write_config(
        tmp_path, registry, mysql_settings, name="use.json", limit_per_dataset=3
    )

    run_scan_zips(scan_config, write_images=False)
    run_export_zips(use_config)

    # The ingest watermark must not hide pending rows from the consume path.
    assert run_db_status(scan_path)["totals"] == {"pending": 5, "done": 3}


def test_concurrent_shards_ingest_disjoint_datasets(tmp_path, clean_database, mysql_settings):
    """Sharding a bulk load by dataset needs no coordination between processes."""

    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "zips"
    root.mkdir(exist_ok=True)
    for name in ("alpha", "beta", "gamma"):
        make_zip_dataset(tmp_path, rows=6, dataset_name=name)
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps(
            {
                "data_root": str(root),
                "datasets": {name: {"zip": f"{name}.zip"} for name in ("alpha", "beta", "gamma")},
            }
        ),
        encoding="utf-8",
    )

    def shard(name):
        config, _ = write_config(
            tmp_path, registry, mysql_settings, name=f"{name}.json", datasets=[name]
        )
        return run_scan_zips(config, write_images=False)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(shard, ("alpha", "beta", "gamma")))

    assert [item["written"] for item in results] == [6, 6, 6]
    assert _query_one(mysql_settings, "SELECT COUNT(*) FROM sample_source") == 18
    assert _query_one(
        mysql_settings, "SELECT COUNT(DISTINCT version_id, sample_id) FROM sample_source"
    ) == 18


def test_lean_ledger_skips_source_text_but_still_dedupes(tmp_path, clean_database, mysql_settings):
    """store_conversations=false turns the table into a pure mapping."""

    lean = {**mysql_settings, "store_conversations": False}
    registry = make_zip_dataset(tmp_path, rows=6)
    scan_config, scan_path = write_config(tmp_path, registry, lean, name="scan.json")
    use_config, _ = write_config(tmp_path, registry, lean, name="use.json", limit_per_dataset=2)

    run_scan_zips(scan_config, write_images=False)
    stored = _query_one(lean, "SELECT COUNT(*) FROM sample_source WHERE conversations IS NOT NULL")

    assert run_db_status(scan_path)["totals"] == {"pending": 6}
    assert stored == 0

    # Consuming is unaffected: the text comes from the parquet, not the ledger.
    run_export_zips(use_config)
    records = [
        json.loads(line)
        for line in use_config.output_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert all(record["conversations"][1]["value"] for record in records)

    # Untranslated English cannot be rebuilt from the ledger, and it says so.
    export = run_db_export(scan_path, tmp_path / "exported.jsonl")
    assert export["written"] == 0
    assert "no source text in the ledger" in export["note"]


def test_lean_ledger_still_exports_translated_samples(tmp_path, clean_database, mysql_settings):
    """The translation table is unaffected, so translated rows export fine."""

    lean = {**mysql_settings, "store_conversations": False}
    registry = make_zip_dataset(tmp_path, rows=3)
    config, config_path = write_config(tmp_path, registry, lean, chinese_ratio=1.0)

    run_translate_zips(config, _SuccessfulPool(), _handler)
    export = run_db_export(config_path, tmp_path / "exported.jsonl")
    records = [
        json.loads(line)
        for line in (tmp_path / "exported.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert export["written"] == 3
    assert all(record["conversations"][1]["value"] == "答" for record in records)


def test_a_backfill_scan_does_not_undo_finished_translations(tmp_path, clean_database, mysql_settings):
    """The case that makes scanning and translating safe to run at the same time.

    ``db-scan`` writes every row it reads as ``pending``. Re-scanning to fill in
    source text a first pass skipped must not walk back over rows the translator
    has already finished, or a multi-day run quietly loses its results.
    """

    registry = make_zip_dataset(tmp_path, rows=4)
    config, config_path = write_config(tmp_path, registry, mysql_settings, chinese_ratio=1.0)
    scan_config, scan_path = write_config(tmp_path, registry, mysql_settings, name="scan.json")

    run_translate_zips(config, _SuccessfulPool(), _handler)
    assert run_db_status(config_path)["totals"] == {"done": 4}

    # A backfill re-reads from row 0: the cursor is what a re-scan would clear.
    _execute(mysql_settings, "DELETE FROM dataset_cursor")
    run_scan_zips(scan_config, write_images=False)

    assert run_db_status(scan_path)["totals"] == {"done": 4}
    assert _query_one(mysql_settings, "SELECT COUNT(*) FROM sample_translation") == 4
    # The point of the backfill still happens: source text lands on done rows.
    assert _query_one(
        mysql_settings, "SELECT COUNT(*) FROM sample_source WHERE conversations IS NOT NULL"
    ) == 4
    assert run_db_export(config_path, tmp_path / "exported.jsonl")["written"] == 4


def _execute(mysql_settings, sql):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    ledger = MySQLLedger(load_mysql_config(mysql_settings))
    try:
        ledger.pool.run(lambda cursor: cursor.execute(sql))
    finally:
        ledger.close()


def test_db_restore_makes_a_lost_ledger_skip_already_translated_rows(
    tmp_path, clean_database, mysql_settings
):
    """账本随卷丢失、JSONL 尚在时，恢复后续跑必须一条都不重做。"""

    from finevision_to_sharegpt.db_commands import run_db_restore

    registry = make_zip_dataset(tmp_path, rows=8)
    config, config_path = write_config(
        tmp_path, registry, mysql_settings, chinese_ratio=1.0, limit_per_dataset=5
    )

    first = run_translate_zips(config, _SuccessfulPool(), _handler)
    assert first["written"] == 5

    # 模拟整个卷被清空：库没了，产出还在。
    _drop_and_recreate(mysql_settings)

    restored = run_db_restore(config_path)
    assert restored["source_rows"] == 5
    assert restored["translations"] == 5

    # 关键断言：恢复之后再跑同一份配置，配额已经被占满，不该再翻任何一条。
    second = run_translate_zips(config, _SuccessfulPool(), _handler)
    assert second["written"] == 0
    assert second["skipped"] == 5


def test_db_restore_without_the_cursor_would_redo_everything(
    tmp_path, clean_database, mysql_settings
):
    """水位线是恢复的必要部分，不是可选项——去掉它恢复就等于没做。"""

    from finevision_to_sharegpt.db_commands import run_db_restore

    registry = make_zip_dataset(tmp_path, rows=8)
    config, config_path = write_config(
        tmp_path, registry, mysql_settings, chinese_ratio=1.0, limit_per_dataset=5
    )
    run_translate_zips(config, _SuccessfulPool(), _handler)
    _drop_and_recreate(mysql_settings)
    run_db_restore(config_path)

    # 只抹掉水位线，行仍然是 done。
    _execute(mysql_settings, "DELETE FROM dataset_cursor")
    again = run_translate_zips(config, _SuccessfulPool(), _handler)

    assert again["written"] == 5


def test_db_restore_dry_run_touches_nothing(tmp_path, clean_database, mysql_settings):
    from finevision_to_sharegpt.db_commands import run_db_restore

    registry = make_zip_dataset(tmp_path, rows=4)
    config, config_path = write_config(tmp_path, registry, mysql_settings, chinese_ratio=1.0)
    run_translate_zips(config, _SuccessfulPool(), _handler)
    _drop_and_recreate(mysql_settings)

    stats = run_db_restore(config_path, dry_run=True)

    assert stats["dry_run"] is True
    assert stats["source_rows"] == 4
    assert _query_one(mysql_settings, "SELECT COUNT(*) FROM sample_source") == 0


def _execute(mysql_settings, sql):
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    ledger = MySQLLedger(load_mysql_config(mysql_settings))
    try:
        ledger.pool.run(lambda cursor: cursor.execute(sql))
    finally:
        ledger.close()


def _drop_and_recreate(mysql_settings):
    for table in ("sample_translation", "sample_source", "dataset_cursor", "dataset_version"):
        _execute(mysql_settings, f"DROP TABLE IF EXISTS {table}")
    from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger

    ledger = MySQLLedger(load_mysql_config(mysql_settings))
    ledger.close()


def test_resume_does_not_exceed_the_quota_in_mysql_mode(tmp_path, clean_database, mysql_settings):
    """水位线让已完成的行读都不读，配额必须由账本补报，否则每次续跑再翻一份。"""

    registry = make_zip_dataset(tmp_path, rows=8)
    config, _ = write_config(
        tmp_path, registry, mysql_settings, chinese_ratio=1.0, limit_per_dataset=5
    )

    first = run_translate_zips(config, _SuccessfulPool(), _handler)
    second = run_translate_zips(config, _SuccessfulPool(), _handler)

    assert first["written"] == 5
    assert second["written"] == 0
    assert second["skipped"] == 5
    # 配额是「这个数据集最终贡献多少条」，不是「每轮新翻多少条」。
    assert first["written"] + second["written"] == 5


def test_raising_the_quota_lets_a_resume_continue(tmp_path, clean_database, mysql_settings):
    """补报不能把数据集锁死：调大 limit 之后应当接着翻。"""

    registry = make_zip_dataset(tmp_path, rows=8)
    first_config, _ = write_config(
        tmp_path, registry, mysql_settings, name="a.json", chinese_ratio=1.0, limit_per_dataset=5
    )
    run_translate_zips(first_config, _SuccessfulPool(), _handler)

    bigger, _ = write_config(
        tmp_path, registry, mysql_settings, name="b.json", chinese_ratio=1.0, limit_per_dataset=8
    )
    second = run_translate_zips(bigger, _SuccessfulPool(), _handler)

    assert second["written"] == 3
    assert second["skipped"] == 5
