"""Registering a dataset means the parser can read it, not that a file exists."""

import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from finevision_to_sharegpt.dataset_probe import probe_dataset, verify_datasets
from finevision_to_sharegpt.dataset_registry import RegisteredDataset, discover_datasets


def usable_table():
    return pa.table(
        {
            "images": [[{"bytes": b"png bytes", "path": "a.png"}]],
            "texts": [[{"user": "What is this?", "assistant": "A cat."}]],
        }
    )


def make_dataset(root: Path, name: str, table: pa.Table) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    pq.write_table(table, directory / "part-000.parquet")
    return directory


# -- verify_datasets ---------------------------------------------------------


def test_only_datasets_the_parser_can_read_are_kept(tmp_path):
    good = make_dataset(tmp_path, "chartqa", usable_table())
    # Text with no image bytes: exactly what the text-only sets look like, and
    # what the parquet-file test happily waves through.
    text_only = make_dataset(
        tmp_path,
        "text_openorca",
        pa.table({"images": [[]], "texts": [[{"user": "hi", "assistant": "there"}]]}),
    )

    kept, dropped = verify_datasets(
        {
            "chartqa": RegisteredDataset("chartqa", good, "dir"),
            "text_openorca": RegisteredDataset("text_openorca", text_only, "dir"),
        }
    )

    assert list(kept) == ["chartqa"]
    assert dropped["text_openorca"].verdict == "missing_image"


def test_a_dataset_that_cannot_be_opened_is_dropped_not_raised(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "part-000.parquet").write_bytes(b"not parquet at all")

    kept, dropped = verify_datasets({"broken": RegisteredDataset("broken", broken, "dir")})

    assert kept == {}
    assert dropped["broken"].verdict == "unreadable"
    assert "打开失败" in dropped["broken"].detail


def test_every_dropped_dataset_carries_a_reason(tmp_path):
    no_text = make_dataset(tmp_path, "images_only", pa.table({"image": [b"\xff\xd8\xffbytes"]}))

    _kept, dropped = verify_datasets({"images_only": RegisteredDataset("images_only", no_text, "dir")})

    probe = dropped["images_only"]
    assert probe.verdict == "missing_text"
    assert probe.advice
    assert probe.columns == ["image"]


# -- discovery ---------------------------------------------------------------


def test_discovery_without_verify_still_registers_a_parquet_only_directory(tmp_path):
    make_dataset(tmp_path, "text_openorca", pa.table({"images": [[]], "texts": [[]]}))

    assert list(discover_datasets(tmp_path, True)) == ["text_openorca"]


def test_discovery_with_verify_drops_it(tmp_path):
    make_dataset(tmp_path, "chartqa", usable_table())
    make_dataset(tmp_path, "text_openorca", pa.table({"images": [[]], "texts": [[]]}))

    assert list(discover_datasets(tmp_path, {"verify": True})) == ["chartqa"]


def test_verify_composes_with_include_and_exclude(tmp_path):
    make_dataset(tmp_path, "chartqa", usable_table())
    make_dataset(tmp_path, "okvqa", usable_table())

    discovered = discover_datasets(tmp_path, {"verify": True, "exclude": ["okvqa"]})

    assert list(discovered) == ["chartqa"]


def test_a_zip_of_parquet_is_verified_through_the_archive(tmp_path):
    source = make_dataset(tmp_path, "src", usable_table())
    packed = tmp_path / "packed"
    packed.mkdir()
    with zipfile.ZipFile(packed / "okvqa.zip", "w") as archive:
        archive.write(source / "part-000.parquet", arcname="nested/part.parquet")

    assert probe_dataset(packed).verdict == "ok"
