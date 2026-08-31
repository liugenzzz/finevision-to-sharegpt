from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PARQUET_GLOB = "*.parquet"


@dataclass(frozen=True)
class RegisteredDataset:
    """One dataset, held either as a zip archive or as a directory of parquet.

    FineVision-style trees ship bare parquet directories; older packs ship
    zips. Both are supported, and the directory form needs no extraction at
    all, so it costs no temp space.
    """

    name: str
    source_path: Path
    kind: str = "zip"

    @property
    def zip_path(self) -> Path:
        """Backwards-compatible alias for the archive form."""

        return self.source_path

    @property
    def is_directory(self) -> bool:
        return self.kind == "dir"

    @property
    def source_id(self) -> str:
        """Stable prefix for sample ids.

        Archives drop the ``.zip`` suffix, directories keep their full name,
        so ids stay identical to what earlier runs produced for zips.
        """

        return self.source_path.name if self.is_directory else self.source_path.stem


@dataclass(frozen=True)
class DatasetRegistry:
    data_root: Path
    datasets: dict[str, RegisteredDataset]


def load_dataset_registry(path: Path | str) -> DatasetRegistry:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data_root_value = data.get("data_root")
    if data_root_value is None:
        # No data_root given: fall back to the registry file's own directory.
        data_root = path.parent
    else:
        # A relative data_root resolves against the current working directory
        # (the scripts cd into the project root), matching how every other
        # path in the config files is interpreted.
        data_root = Path(str(data_root_value))

    datasets: dict[str, RegisteredDataset] = {}
    if data.get("auto_discover"):
        datasets.update(discover_datasets(data_root, data.get("auto_discover")))
    # Explicit entries win over discovered ones with the same name.
    for name, item in sorted((data.get("datasets") or {}).items()):
        datasets[str(name)] = _parse_entry(str(name), item, data_root)
    return DatasetRegistry(data_root=data_root, datasets=datasets)


def _parse_entry(name: str, item: Any, data_root: Path) -> RegisteredDataset:
    if not isinstance(item, dict) or not ("zip" in item or "dir" in item):
        raise ValueError(f"dataset {name!r} must include zip or dir")
    if "zip" in item and "dir" in item:
        raise ValueError(f"dataset {name!r} must include only one of zip or dir")
    kind = "zip" if "zip" in item else "dir"
    source_path = Path(str(item[kind]))
    if not source_path.is_absolute():
        source_path = data_root / source_path
    return RegisteredDataset(name=name, source_path=source_path, kind=kind)


def discover_datasets(data_root: Path, options: Any = True) -> dict[str, RegisteredDataset]:
    """Register every immediate subdirectory of ``data_root`` holding parquet.

    A FineVision checkout is one directory per dataset, hundreds of them, so
    listing each by hand is impractical. ``exclude`` drops names by exact
    match; ``include`` restricts to the named ones.
    """

    exclude: set[str] = set()
    include: set[str] | None = None
    if isinstance(options, dict):
        exclude = {str(item) for item in options.get("exclude") or ()}
        raw_include = options.get("include")
        include = {str(item) for item in raw_include} if raw_include else None

    discovered: dict[str, RegisteredDataset] = {}
    if not data_root.is_dir():
        raise ValueError(f"auto_discover needs data_root to be a directory: {data_root}")
    for child in sorted(data_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name in exclude or (include is not None and name not in include):
            continue
        if next(child.rglob(PARQUET_GLOB), None) is None:
            continue
        discovered[name] = RegisteredDataset(name=name, source_path=child, kind="dir")
    return discovered


def resolve_dataset_selection(registry: DatasetRegistry, selection: list[Any]) -> list[RegisteredDataset]:
    if not selection or selection == ["*"]:
        return [registry.datasets[name] for name in sorted(registry.datasets)]
    resolved = []
    for item in selection:
        name = item["name"] if isinstance(item, dict) else item
        if name == "*":
            resolved.extend(registry.datasets[key] for key in sorted(registry.datasets))
            continue
        try:
            resolved.append(registry.datasets[str(name)])
        except KeyError as exc:
            raise ValueError(f"unknown dataset: {name}") from exc
    return resolved
