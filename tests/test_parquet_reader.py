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

    # Row groups 0-4 hold rows 0-499 and are never decoded. What matters is
    # which groups get read, not how many calls it takes to read them.
    read = {group for call in read_groups for group in (call or [])}
    assert read == set(range(5, 10))


def test_stream_parquet_rows_still_yields_bare_rows(tmp_path):
    path = _write(tmp_path, rows=10, row_group_size=5)

    assert [row["n"] for row in stream_parquet_rows(path)] == list(range(10))
    assert parquet_num_rows(path) == 10


# -- 续跑时不该为了丢掉一行而先解码它 ------------------------------------------


def test_a_skipped_row_group_is_never_read(tmp_path, monkeypatch):
    """续跑的主要开销：把已完成的行连图片一起解出来，再丢掉。

    sample_id 是 <数据集>:<分片>:<行号>，跟行内容无关——所以该不该跳，
    解码之前就知道。现场是 7000 行/秒，一次重启要 35 分钟才走回断点。
    """

    path = _write(tmp_path, rows=1000, row_group_size=100)
    read_groups = []
    original = pq.ParquetFile.iter_batches

    def spy(self, *args, **kwargs):
        read_groups.append(kwargs.get("row_groups"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
    # 前 500 行已完成
    rows = list(iter_parquet_rows_from(path, skip=lambda index: index < 500))

    read = {group for call in read_groups for group in (call or [])}
    assert read == set(range(5, 10)), "已完成的那几组不该被读"
    assert len(rows) == 1000, "跳过的行也要照数，否则配额会算少"
    assert all(row is None for index, row in rows[:500])
    assert all(row is not None for index, row in rows[500:])


def test_the_row_indexes_stay_absolute_when_rows_are_skipped(tmp_path):
    """行号是 sample_id 的一部分，跳过再多也不能重新编号。"""

    path = _write(tmp_path, rows=100, row_group_size=10)

    rows = list(iter_parquet_rows_from(path, skip=lambda index: index % 3 == 0))

    assert [index for index, _row in rows] == list(range(100))
    assert [index for index, row in rows if row is None] == list(range(0, 100, 3))


def test_a_partly_finished_row_group_still_yields_its_unfinished_rows(tmp_path):
    """整组跳不掉的时候必须退回逐行判断，一行都不能漏。"""

    path = _write(tmp_path, rows=50, row_group_size=50)   # 只有一个 row group

    rows = list(iter_parquet_rows_from(path, skip=lambda index: index != 37))

    assert len(rows) == 50
    kept = [(index, row) for index, row in rows if row is not None]
    assert [index for index, _row in kept] == [37]


def test_skipping_composes_with_start_row(tmp_path):
    """水位线之下的行不产出，水位线之上已完成的产出 None，其余产出真行。"""

    path = _write(tmp_path, rows=100, row_group_size=10)

    rows = list(iter_parquet_rows_from(path, start_row=40, skip=lambda index: index < 60))

    assert [index for index, _row in rows] == list(range(40, 100))
    assert [index for index, row in rows if row is None] == list(range(40, 60))
