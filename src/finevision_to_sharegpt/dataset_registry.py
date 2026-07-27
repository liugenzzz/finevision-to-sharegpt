from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RegisteredDataset:
    name: str
    zip_path: Path


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
    for name, item in sorted((data.get("datasets") or {}).items()):
        if not isinstance(item, dict) or "zip" not in item:
            raise ValueError(f"dataset {name!r} must include zip")
        zip_path = Path(str(item["zip"]))
        if not zip_path.is_absolute():
            zip_path = data_root / zip_path
        datasets[str(name)] = RegisteredDataset(name=str(name), zip_path=zip_path)
    return DatasetRegistry(data_root=data_root, datasets=datasets)


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
