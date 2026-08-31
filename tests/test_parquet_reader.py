import pyarrow as pa
import pyarrow.parquet as pq

from finevision_to_sharegpt.parquet_reader import (
    iter_parquet_rows_from,
    parquet_num_rows,
    stream_parquet_rows,
)


def _write(tmp_path, rows=1000, row_group_size=100):
    path = tmp_path / "part.parquet"
    pq.write_table(pa.table({"n": list(range(rows))}), path, row_group_size=row_group_size)
    return path


def test_iter_parquet_rows_from_yields_absolute_indexes(tmp_path):
    path = _write(tmp_path)

    rows = list(iter_parquet_rows_from(path))

    assert [index for index, _row in rows] == list(range(1000))
    assert [row["n"] for _index, row in rows] == list(range(1000))


def test_iter_parquet_rows_from_resumes_mid_file(tmp_path):
    path = _write(tmp_path)

    rows = list(iter_parquet_rows_from(path, start_row=250))

    assert rows[0][0] == 250
    assert rows[0][1]["n"] == 250
    assert len(rows) == 750


def test_iter_parquet_rows_from_resumes_inside_a_row_group(tmp_path):
    path = _write(tmp_path)

    rows = list(iter_parquet_rows_from(path, start_row=255))

    assert [index for index, _row in rows][:3] == [255, 256, 257]
    assert len(rows) == 745


def test_iter_parquet_rows_from_past_the_end_is_empty(tmp_path):
    path = _write(tmp_path)

    assert list(iter_parquet_rows_from(path, start_row=1000)) == []
    assert list(iter_parquet_rows_from(path, start_row=5000)) == []


def test_iter_parquet_rows_from_skips_whole_row_groups(tmp_path, monkeypatch):
    path = _write(tmp_path)
    read_groups = []
    original = pq.ParquetFile.iter_batches

    def spy(self, *args, **kwargs):
        read_groups.append(kwargs.get("row_groups"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
    list(iter_parquet_rows_from(path, start_row=500))

    # Row groups 0-4 hold rows 0-499 and are never decoded.
    assert read_groups == [list(range(5, 10))]


def test_stream_parquet_rows_still_yields_bare_rows(tmp_path):
    path = _write(tmp_path, rows=10, row_group_size=5)

    assert [row["n"] for row in stream_parquet_rows(path)] == list(range(10))
    assert parquet_num_rows(path) == 10
