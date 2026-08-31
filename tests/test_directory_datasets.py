"""FineVision-style datasets: a directory of parquet rather than a zip."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finevision_to_sharegpt.archive import iter_dataset_parquets, list_parquets_in_dir
from finevision_to_sharegpt.config_loader import load_zip_task_config
from finevision_to_sharegpt.dataset_registry import (
    RegisteredDataset,
    discover_datasets,
    load_dataset_registry,
)
from finevision_to_sharegpt.db.fingerprint import directory_fingerprint, source_fingerprint
from finevision_to_sharegpt.zip_pipeline import run_export_zips


def make_dataset_dir(root, name, shards=2, rows=3):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for shard in range(shards):
        pq.write_table(
            pa.table(
                {
                    "images": [[b"\xff\xd8\xff%s%d%d" % (name.encode(), shard, i)] for i in range(rows)],
                    "texts": [[{"user": f"Q{shard}-{i}", "assistant": f"A{shard}-{i}"}] for i in range(rows)],
                }
            ),
            directory / f"train-{shard:05d}-of-{shards:05d}.parquet",
            row_group_size=2,
        )
    return directory


# -- registry ---------------------------------------------------------------


def test_registry_accepts_a_directory_entry(tmp_path):
    make_dataset_dir(tmp_path / "data", "CoSyn_400k_chart")
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps(
            {
                "data_root": str(tmp_path / "data"),
                "datasets": {"CoSyn_400k_chart": {"dir": "CoSyn_400k_chart"}},
            }
        ),
        encoding="utf-8",
    )

    dataset = load_dataset_registry(registry_path).datasets["CoSyn_400k_chart"]

    assert dataset.kind == "dir"
    assert dataset.is_directory
    assert dataset.source_path == tmp_path / "data" / "CoSyn_400k_chart"
    assert dataset.source_id == "CoSyn_400k_chart"


def test_zip_entries_keep_their_identity(tmp_path):
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": "/data", "datasets": {"okvqa": {"zip": "okvqa.zip"}}}),
        encoding="utf-8",
    )

    dataset = load_dataset_registry(registry_path).datasets["okvqa"]

    assert dataset.kind == "zip"
    assert not dataset.is_directory
    # The .zip suffix is dropped, so sample ids match pre-existing runs.
    assert dataset.source_id == "okvqa"
    assert dataset.zip_path == dataset.source_path


def test_registry_rejects_an_entry_with_neither_zip_nor_dir(tmp_path):
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(json.dumps({"datasets": {"x": {"path": "y"}}}), encoding="utf-8")

    with pytest.raises(ValueError, match="must include zip or dir"):
        load_dataset_registry(registry_path)


def test_registry_rejects_an_entry_with_both_zip_and_dir(tmp_path):
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"datasets": {"x": {"zip": "a.zip", "dir": "a"}}}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="only one of zip or dir"):
        load_dataset_registry(registry_path)


# -- auto discovery ---------------------------------------------------------


def test_discovery_finds_dataset_directories_and_ignores_loose_files(tmp_path):
    root = tmp_path / "FineVision"
    make_dataset_dir(root, "arxivqa")
    make_dataset_dir(root, "CoSyn_400k_chart")
    (root / "README.md").write_text("card", encoding="utf-8")
    (root / "empty_dir").mkdir()

    discovered = discover_datasets(root)

    assert sorted(discovered) == ["CoSyn_400k_chart", "arxivqa"]
    assert all(item.is_directory for item in discovered.values())


def test_discovery_handles_names_with_parentheses(tmp_path):
    root = tmp_path / "FineVision"
    make_dataset_dir(root, "lrv_normal(filtered)")
    make_dataset_dir(root, "mapqa(mathv360k)")

    discovered = discover_datasets(root)

    assert sorted(discovered) == ["lrv_normal(filtered)", "mapqa(mathv360k)"]


def test_discovery_honours_include_and_exclude(tmp_path):
    root = tmp_path / "FineVision"
    for name in ("a", "b", "c"):
        make_dataset_dir(root, name)

    assert sorted(discover_datasets(root, {"exclude": ["b"]})) == ["a", "c"]
    assert sorted(discover_datasets(root, {"include": ["b", "c"]})) == ["b", "c"]


def test_auto_discover_via_registry_file_and_explicit_override(tmp_path):
    root = tmp_path / "FineVision"
    make_dataset_dir(root, "arxivqa")
    make_dataset_dir(root, "other")
    elsewhere = make_dataset_dir(tmp_path / "custom", "arxivqa")
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps(
            {
                "data_root": str(root),
                "auto_discover": True,
                "datasets": {"arxivqa": {"dir": str(elsewhere)}},
            }
        ),
        encoding="utf-8",
    )

    registry = load_dataset_registry(registry_path)

    assert sorted(registry.datasets) == ["arxivqa", "other"]
    # An explicit entry wins over the discovered one of the same name.
    assert registry.datasets["arxivqa"].source_path == elsewhere


def test_missing_data_root_names_the_component_that_is_absent(tmp_path):
    (tmp_path / "FineVision").mkdir()
    (tmp_path / "other").mkdir()
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(tmp_path / "FineVisoin" / "sub"), "auto_discover": True}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as caught:
        load_dataset_registry(registry_path)

    message = str(caught.value)
    assert "does not exist" in message
    assert f"the path is fine up to {tmp_path}" in message
    assert "'FineVisoin' is not there" in message
    # The listing of what is actually there is what makes the typo obvious.
    assert "FineVision" in message and "other" in message


def test_missing_data_root_points_out_a_case_mismatch(tmp_path):
    (tmp_path / "FineVision").mkdir()
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(tmp_path / "finevision"), "auto_discover": True}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="case differs"):
        load_dataset_registry(registry_path)


def test_data_root_that_is_a_file_says_so(tmp_path):
    target = tmp_path / "FineVision"
    target.write_text("not a directory", encoding="utf-8")
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(target), "auto_discover": True}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="is a file, not a directory"):
        load_dataset_registry(registry_path)


def test_broken_symlink_data_root_says_so(tmp_path):
    link = tmp_path / "FineVision"
    link.symlink_to(tmp_path / "nowhere")
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(link), "auto_discover": True}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="broken symlink"):
        load_dataset_registry(registry_path)


# -- listing ----------------------------------------------------------------


def test_list_parquets_in_dir_uses_relative_posix_names(tmp_path):
    directory = make_dataset_dir(tmp_path, "ds", shards=3)
    (directory / "nested").mkdir()
    pq.write_table(pa.table({"n": [1]}), directory / "nested" / "extra.parquet")

    found = list_parquets_in_dir(directory)

    assert [item.name for item in found] == [
        "nested/extra.parquet",
        "train-00000-of-00003.parquet",
        "train-00001-of-00003.parquet",
        "train-00002-of-00003.parquet",
    ]
    assert all(item.path.is_file() for item in found)


def test_iter_dataset_parquets_reads_a_directory_without_extracting(tmp_path):
    directory = make_dataset_dir(tmp_path, "ds")
    dataset = RegisteredDataset(name="ds", source_path=directory, kind="dir")
    extract_root = tmp_path / "tmp"
    extract_root.mkdir()

    found = iter_dataset_parquets(dataset, extract_root)

    assert len(found) == 2
    # Nothing is copied, so the temp dir stays untouched.
    assert list(extract_root.iterdir()) == []
    assert found[0].path.parent == directory


# -- fingerprint ------------------------------------------------------------


def test_directory_fingerprint_is_stable_and_ignores_mtime(tmp_path):
    directory = make_dataset_dir(tmp_path, "ds")
    baseline = directory_fingerprint(directory)

    assert directory_fingerprint(directory).source_hash == baseline.source_hash

    # Touching every file must not orphan the consumption history.
    for path in directory.rglob("*.parquet"):
        path.touch()
    assert directory_fingerprint(directory).source_hash == baseline.source_hash


def test_directory_fingerprint_changes_when_the_inventory_changes(tmp_path):
    directory = make_dataset_dir(tmp_path, "ds")
    baseline = directory_fingerprint(directory)

    pq.write_table(pa.table({"n": [1, 2, 3]}), directory / "train-00002-of-00002.parquet")
    assert directory_fingerprint(directory).source_hash != baseline.source_hash


def test_source_fingerprint_dispatches_on_directory_or_file(tmp_path):
    directory = make_dataset_dir(tmp_path, "ds")
    archive = tmp_path / "ds.zip"
    archive.write_bytes(b"not really a zip, only bytes")

    assert source_fingerprint(directory).source_hash == directory_fingerprint(directory).source_hash
    assert source_fingerprint(archive).file_size == archive.stat().st_size


# -- end to end -------------------------------------------------------------


def test_export_runs_against_directory_datasets(tmp_path):
    root = tmp_path / "FineVision"
    make_dataset_dir(root, "CoSyn_400k_chart", shards=2, rows=3)
    make_dataset_dir(root, "lrv_normal(filtered)", shards=1, rows=2)
    (root / "README.md").write_text("card", encoding="utf-8")
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(root), "auto_discover": True}), encoding="utf-8"
    )
    config_path = tmp_path / "task.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry_path),
                "datasets": ["*"],
                "output_jsonl": str(tmp_path / "out" / "train.jsonl"),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    stats = run_export_zips(load_zip_task_config(config_path))
    records = [
        json.loads(line)
        for line in (tmp_path / "out" / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert stats["written"] == 8
    assert stats["datasets"]["CoSyn_400k_chart"]["written"] == 6
    assert stats["datasets"]["lrv_normal(filtered)"]["written"] == 2
    # Sample ids carry the directory name and the shard file name.
    assert records[0]["id"] == "CoSyn_400k_chart:train-00000-of-00002.parquet:0"
    assert records[0]["images"][0].startswith("images/CoSyn_400k_chart/")
    assert any(r["images"][0].startswith("images/lrv_normal(filtered)/") for r in records)


def test_directory_dataset_resumes_without_repeating(tmp_path):
    root = tmp_path / "FineVision"
    make_dataset_dir(root, "arxivqa", shards=2, rows=4)
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps({"data_root": str(root), "auto_discover": True}), encoding="utf-8"
    )

    def config(limit):
        path = tmp_path / f"task_{limit}.json"
        path.write_text(
            json.dumps(
                {
                    "dataset_registry": str(registry_path),
                    "datasets": ["*"],
                    "output_jsonl": str(tmp_path / "out" / "train.jsonl"),
                    "limit_per_dataset": limit,
                    "resume": True,
                }
            ),
            encoding="utf-8",
        )
        return load_zip_task_config(path)

    first = run_export_zips(config(3))
    second = run_export_zips(config(3))
    ids = [
        json.loads(line)["id"]
        for line in (tmp_path / "out" / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert first["written"] == 3
    assert second["written"] == 3
    assert len(ids) == len(set(ids)) == 6


# -- metadata-only scan ------------------------------------------------------


def test_image_store_can_plan_a_path_without_writing(tmp_path):
    from finevision_to_sharegpt.image_store import ImageStore

    store = ImageStore(output_root=tmp_path, images_dir="images")
    data = b"\xff\xd8\xffpayload"

    planned = store.relative_path(data, dataset_name="okvqa")
    deferred = store.save(data, dataset_name="okvqa", write=False)

    assert planned == deferred
    assert not (tmp_path / planned).exists()

    written = store.save(data, dataset_name="okvqa")
    # The path a metadata-only scan recorded is the one the real write uses.
    assert written == planned
    assert (tmp_path / written).read_bytes() == data
