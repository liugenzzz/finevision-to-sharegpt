import json

from finevision_to_sharegpt.dataset_registry import load_dataset_registry, resolve_dataset_selection


def test_dataset_registry_resolves_named_datasets_relative_to_data_root(tmp_path):
    data_root = tmp_path / "zips"
    data_root.mkdir()
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps(
            {
                "data_root": str(data_root),
                "datasets": {
                    "okvqa": {"zip": "okvqa.zip"},
                    "chartqa": {"zip": "nested/chartqa.zip"},
                },
            }
        ),
        encoding="utf-8",
    )

    registry = load_dataset_registry(registry_path)
    selected = resolve_dataset_selection(registry, ["chartqa", "okvqa"])

    assert [item.name for item in selected] == ["chartqa", "okvqa"]
    assert selected[0].zip_path == data_root / "nested" / "chartqa.zip"
    assert selected[1].zip_path == data_root / "okvqa.zip"


def test_dataset_registry_supports_wildcard_selection(tmp_path):
    registry_path = tmp_path / "datasets.json"
    registry_path.write_text(
        json.dumps(
            {
                "data_root": "/data/zips",
                "datasets": {
                    "b": {"zip": "b.zip"},
                    "a": {"zip": "a.zip"},
                },
            }
        ),
        encoding="utf-8",
    )

    registry = load_dataset_registry(registry_path)
    selected = resolve_dataset_selection(registry, ["*"])

    assert [item.name for item in selected] == ["a", "b"]
