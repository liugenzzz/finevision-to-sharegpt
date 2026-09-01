import pytest

from finevision_to_sharegpt.db import JsonlLedger, ScanPlan, load_mysql_config, open_ledger, zip_fingerprint
from finevision_to_sharegpt.db.config import expand_env
from finevision_to_sharegpt.db.ledger import DatasetVersion
from finevision_to_sharegpt.db.mysql_ledger import MySQLLedger
from finevision_to_sharegpt.db.pool import BatchWriter, MySQLUnavailable
from finevision_to_sharegpt.db.schema import create_view, view_name


# -- config ---------------------------------------------------------------


def test_load_mysql_config_returns_none_without_section():
    assert load_mysql_config(None) is None
    assert load_mysql_config({}) is None


def test_load_mysql_config_expands_environment_references(monkeypatch):
    monkeypatch.setenv("FV_TEST_PASSWORD", "s3cret")
    config = load_mysql_config(
        {"host": "db", "user": "fv", "database": "finevision", "password": "${FV_TEST_PASSWORD}"}
    )

    assert config.password == "s3cret"
    assert config.batch_size == 200
    assert config.on_connect_error == "fallback"
    assert config.fail_fast is False


def test_load_mysql_config_rejects_unknown_on_connect_error():
    with pytest.raises(ValueError, match="on_connect_error"):
        load_mysql_config({"host": "db", "user": "fv", "database": "d", "on_connect_error": "retry"})


def test_load_mysql_config_requires_core_fields():
    with pytest.raises(ValueError, match="database"):
        load_mysql_config({"host": "db", "user": "fv"})


def test_expand_env_reports_missing_variable_with_the_fix(monkeypatch):
    monkeypatch.delenv("FV_ABSENT", raising=False)

    with pytest.raises(ValueError) as caught:
        expand_env("${FV_ABSENT}")

    message = str(caught.value)
    assert "FV_ABSENT" in message
    # A fresh shell is the usual cause, so the message names the actual fix
    # instead of reading like a database failure.
    assert "export FV_ABSENT=" in message
    assert "~/.bashrc" in message


# -- fingerprint -----------------------------------------------------------


def test_zip_fingerprint_is_stable_and_content_sensitive(tmp_path):
    first = tmp_path / "a.zip"
    first.write_bytes(b"payload" * 100)
    second = tmp_path / "b.zip"
    second.write_bytes(b"payload" * 100)
    changed = tmp_path / "c.zip"
    changed.write_bytes(b"payload" * 99 + b"other!!")

    assert zip_fingerprint(first).source_hash == zip_fingerprint(first).source_hash
    assert zip_fingerprint(first).source_hash == zip_fingerprint(second).source_hash
    assert zip_fingerprint(first).source_hash != zip_fingerprint(changed).source_hash
    assert zip_fingerprint(first).file_size == 700


def test_zip_fingerprint_samples_head_and_tail_of_large_files(tmp_path):
    path = tmp_path / "big.zip"
    path.write_bytes(b"h" * 32 + b"m" * 64 + b"t" * 32)
    baseline = zip_fingerprint(path, sample_bytes=32).source_hash

    # Same size, different tail: a rebuilt archive changes its central directory.
    path.write_bytes(b"h" * 32 + b"m" * 64 + b"T" * 32)
    assert zip_fingerprint(path, sample_bytes=32).source_hash != baseline

    # Same size and same ends, middle differs: deliberately not detected.
    path.write_bytes(b"h" * 32 + b"M" * 64 + b"t" * 32)
    assert zip_fingerprint(path, sample_bytes=32).source_hash == baseline


# -- schema ----------------------------------------------------------------


def test_view_name_is_sanitized():
    assert view_name("ok-vqa/2") == "v_sample_source_ok_vqa_2"
    assert view_name("!!!") == "v_sample_source_dataset"


def test_create_view_escapes_quotes_and_backslashes():
    sql = create_view("o'k\\v")

    assert sql.endswith("WHERE dataset = 'o''k\\\\v'")
    assert sql.startswith("CREATE OR REPLACE VIEW v_sample_source_o_k_v AS")


# -- JsonlLedger keeps the pre-MySQL behaviour ------------------------------


def test_jsonl_ledger_tracks_ids_in_memory(tmp_path):
    ledger = JsonlLedger({"already"})
    version = ledger.open_dataset("okvqa", tmp_path, tmp_path)

    assert version.version_id is None
    assert ledger.scan_plan(version, "part.parquet") == ScanPlan()
    assert ledger.is_consumed(version, "already", 0, ScanPlan())
    assert not ledger.is_consumed(version, "fresh", 0, ScanPlan())

    ledger.mark_done(version, "fresh", "en")
    assert ledger.is_consumed(version, "fresh", 0, ScanPlan())


def test_open_ledger_without_mysql_config_returns_jsonl_ledger():
    assert isinstance(open_ledger(None), JsonlLedger)


def test_open_ledger_falls_back_when_connection_fails(monkeypatch, capsys):
    config = load_mysql_config({"host": "db", "user": "fv", "database": "d"})

    def boom(*args, **kwargs):
        raise MySQLUnavailable("connection refused")

    monkeypatch.setattr("finevision_to_sharegpt.db.mysql_ledger.MySQLLedger.__init__", boom)
    ledger = open_ledger(config, {"seen"})

    assert isinstance(ledger, JsonlLedger)
    assert ledger.completed_ids == {"seen"}
    assert "falling back to file mode" in capsys.readouterr().out


def test_open_ledger_fails_fast_when_configured(monkeypatch):
    config = load_mysql_config(
        {"host": "db", "user": "fv", "database": "d", "on_connect_error": "fail"}
    )

    def boom(*args, **kwargs):
        raise MySQLUnavailable("connection refused")

    monkeypatch.setattr("finevision_to_sharegpt.db.mysql_ledger.MySQLLedger.__init__", boom)
    with pytest.raises(MySQLUnavailable):
        open_ledger(config)


# -- gap logic --------------------------------------------------------------


def test_is_consumed_only_consults_the_retry_gap():
    ledger = MySQLLedger.__new__(MySQLLedger)
    version = DatasetVersion(dataset="okvqa", version_id=1)
    plan = ScanPlan(start_row=10, gap_end=50, consumed_ids={"okvqa:p:20"})

    # Inside the gap the id set decides.
    assert ledger.is_consumed(version, "okvqa:p:20", 20, plan)
    assert not ledger.is_consumed(version, "okvqa:p:21", 21, plan)
    # Above the watermark nothing has been seen, so no lookup is needed.
    assert not ledger.is_consumed(version, "okvqa:p:20", 60, plan)


# -- batch writer -----------------------------------------------------------


class _FakePool:
    def __init__(self):
        self.batches = []

    def run(self, action, retries=1):
        class _Cursor:
            def executemany(self_inner, statement, rows):
                self.batches.append((statement, list(rows)))

        return action(_Cursor())


def test_batch_writer_flushes_on_batch_size():
    pool = _FakePool()
    writer = BatchWriter(pool, "INSERT", batch_size=3, flush_interval_seconds=0)

    for index in range(5):
        writer.add((index,))

    assert [len(rows) for _statement, rows in pool.batches] == [3]
    writer.flush()
    assert [len(rows) for _statement, rows in pool.batches] == [3, 2]


def test_batch_writer_flushes_its_dependency_first():
    pool = _FakePool()
    order = []
    source = BatchWriter(pool, "SOURCE", batch_size=100, flush_interval_seconds=0)

    def before_flush():
        order.append("source")
        source.flush()

    status = BatchWriter(pool, "STATUS", batch_size=1, flush_interval_seconds=0, before_flush=before_flush)
    source.add((1,))
    status.add((2,))

    assert order == ["source"]
    assert [statement for statement, _rows in pool.batches] == ["SOURCE", "STATUS"]


def test_batch_writer_flush_is_a_noop_when_empty():
    pool = _FakePool()
    BatchWriter(pool, "INSERT", batch_size=10, flush_interval_seconds=0).flush()

    assert pool.batches == []


# -- connection failures explain themselves ----------------------------------


def test_too_many_connections_points_at_stale_connections():
    from finevision_to_sharegpt.db.pool import _explain_connect_failure

    config = load_mysql_config({"host": "db", "user": "fv", "database": "d"})
    message = _explain_connect_failure(Exception("(1040, 'Too many connections')"), config)

    assert "Threads_connected" in message
    assert "wait_timeout" in message
    # The per-thread pool is the thing to size against, so say so.
    assert "one connection per worker thread" in message


def test_connection_refused_names_the_host_and_the_fix():
    from finevision_to_sharegpt.db.pool import _explain_connect_failure

    config = load_mysql_config({"host": "10.0.0.5", "port": 3307, "user": "fv", "database": "d"})
    message = _explain_connect_failure(Exception("(2003, \"Can't connect to MySQL server\")"), config)

    assert "10.0.0.5:3307" in message
    assert "setup_local_mysql.sh" in message


def test_an_unrecognised_error_is_passed_through_unchanged():
    from finevision_to_sharegpt.db.pool import _explain_connect_failure

    config = load_mysql_config({"host": "db", "user": "fv", "database": "d"})

    assert _explain_connect_failure(Exception("something odd"), config) == "something odd"
