import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

from finevision_to_sharegpt.archive import extract_parquets_from_zip, find_zip_inputs
from finevision_to_sharegpt.parquet_reader import stream_parquet_rows


def test_find_zip_inputs_from_directory_and_glob(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"zip")
    (tmp_path / "b.txt").write_text("no", encoding="utf-8")

    assert find_zip_inputs(input_dir=tmp_path, glob_pattern="*.zip") == [tmp_path / "a.zip"]


def test_extract_parquets_from_zip_and_stream_rows(tmp_path):
    parquet_path = tmp_path / "part-000.parquet"
    table = pa.table(
        {
            "caption": ["A cat.", "A dog."],
            "image": [b"cat image", b"dog image"],
        }
    )
    pq.write_table(table, parquet_path)
    zip_path = tmp_path / "okvqa.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(parquet_path, arcname="nested/part-000.parquet")

    extracted = extract_parquets_from_zip(zip_path, tmp_path / "extract")
    rows = list(stream_parquet_rows(extracted[0].path))

    assert [item.name for item in extracted] == ["nested/part-000.parquet"]
    assert rows == [
        {"caption": "A cat.", "image": b"cat image"},
        {"caption": "A dog.", "image": b"dog image"},
    ]


def test_stream_parquet_rows_releases_file_when_closed_early(tmp_path):
    parquet_path = tmp_path / "part.parquet"
    table = pa.table({"caption": ["A cat.", "A dog."]})
    pq.write_table(table, parquet_path)

    rows = stream_parquet_rows(parquet_path)
    assert next(rows) == {"caption": "A cat."}
    rows.close()

    parquet_path.unlink()
    assert not parquet_path.exists()
