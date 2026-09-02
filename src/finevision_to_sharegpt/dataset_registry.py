from __future__ import annotations

import json
import os
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
    # 源文本的语言。中文原生的集合标上 "zh" 就不必翻译，也不会在导出时
    # 被当成英文样本。注册表里不写就是 "en"，因为 FineVision 全是英文。
    source_lang: str = "en"

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
        roots = [path.parent]
    elif isinstance(data_root_value, list):
        # Several roots: collections rarely live under one parent, and a
        # sibling directory should not need its own registry file.
        roots = [Path(str(item)) for item in data_root_value]
    else:
        # A relative data_root resolves against the current working directory
        # (the scripts cd into the project root), matching how every other
        # path in the config files is interpreted.
        roots = [Path(str(data_root_value))]
    data_root = roots[0]

    datasets: dict[str, RegisteredDataset] = {}
    auto_discover = data.get("auto_discover")
    if auto_discover:
        for root in roots:
            for name, dataset in discover_datasets(root, auto_discover).items():
                if name in datasets:
                    raise ValueError(
                        f"dataset name {name!r} was discovered under more than one data_root "
                        f"({datasets[name].source_path} and {dataset.source_path}); "
                        "rename one directory or list them explicitly under datasets"
                    )
                datasets[name] = dataset
    # Explicit entries win over discovered ones with the same name.
    for name, item in sorted((data.get("datasets") or {}).items()):
        datasets[str(name)] = _parse_entry(str(name), item, data_root)
    if auto_discover and not datasets:
        # Returning an empty registry would let every command "succeed" while
        # writing nothing, which reads as a silent data loss.
        raise ValueError(
            "\n".join(describe_empty_discovery(root, auto_discover) for root in roots)
        )
    return DatasetRegistry(data_root=data_root, datasets=datasets)


def describe_empty_discovery(data_root: Path, options: Any) -> str:
    """Explain why a scan of an existing directory registered nothing."""

    children = [item for item in sorted(data_root.iterdir()) if item.is_dir()]
    message = [f"auto_discover found no datasets under {data_root}"]
    if not children:
        message.append("  it has no subdirectories at all; is this the right level of the tree?")
        return "\n".join(message)
    if isinstance(options, dict) and (options.get("include") or options.get("exclude")):
        names = ", ".join(item.name for item in children[:20])
        message.append(f"  include/exclude filtered everything out; subdirectories are: {names}")
        return "\n".join(message)
    names = ", ".join(item.name for item in children[:20])
    if isinstance(options, dict) and options.get("verify"):
        message.append("  verify is on, so a subdirectory only counts if the parser can read a row")
        message.append("  run scripts/probe_dataset.py <data_root> --all to see why each one failed")
        message.append(f"  subdirectories: {names}")
        return "\n".join(message)
    message.append("  its subdirectories hold no .parquet files")
    message.append(f"  subdirectories: {names}")
    message.append("  if the parquet sits one level deeper, point data_root at that level")
    return "\n".join(message)


def _parse_entry(name: str, item: Any, data_root: Path) -> RegisteredDataset:
    if not isinstance(item, dict) or not ("zip" in item or "dir" in item):
        raise ValueError(f"dataset {name!r} must include zip or dir")
    if "zip" in item and "dir" in item:
        raise ValueError(f"dataset {name!r} must include only one of zip or dir")
    kind = "zip" if "zip" in item else "dir"
    source_path = Path(str(item[kind]))
    if not source_path.is_absolute():
        source_path = data_root / source_path
    return RegisteredDataset(
        name=name,
        source_path=source_path,
        kind=kind,
        source_lang=str(item.get("source_lang") or "en"),
    )


def discover_datasets(data_root: Path, options: Any = True) -> dict[str, RegisteredDataset]:
    """Register every immediate subdirectory of ``data_root`` holding parquet.

    A FineVision checkout is one directory per dataset, hundreds of them, so
    listing each by hand is impractical. ``exclude`` drops names by exact
    match; ``include`` restricts to the named ones.

    ``verify`` raises the bar from "holds a parquet file" to "the parser can
    actually read a row". Holding parquet is a weak test: a text-only set passes
    it and then contributes nothing but rejected rows, which cost a full read on
    every scan and can never be retried because ``rejected`` is terminal. It
    opens one shard per dataset, so it is opt-in rather than the default — the
    place that wants it is ``register_datasets.py``, which pays the cost once and
    pins the result.
    """

    exclude: set[str] = set()
    include: set[str] | None = None
    verify = False
    if isinstance(options, dict):
        exclude = {str(item) for item in options.get("exclude") or ()}
        raw_include = options.get("include")
        include = {str(item) for item in raw_include} if raw_include else None
        verify = bool(options.get("verify"))

    discovered: dict[str, RegisteredDataset] = {}
    if not data_root.is_dir():
        raise ValueError(describe_bad_root(data_root))
    for child in sorted(data_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name in exclude or (include is not None and name not in include):
            continue
        if next(child.rglob(PARQUET_GLOB), None) is None:
            continue
        discovered[name] = RegisteredDataset(name=name, source_path=child, kind="dir")
    if verify:
        # Imported here so the common path does not pay for pyarrow and the
        # parser just to resolve a registry.
        from .dataset_probe import verify_datasets

        discovered, _dropped = verify_datasets(discovered)
    return discovered


def describe_bad_root(data_root: Path) -> str:
    """Explain precisely why ``data_root`` cannot be scanned.

    A bare "not a directory" is useless for diagnosis: a typo, a file, a
    broken symlink and an unreadable mount all look identical. Naming the
    first component that goes missing, and listing what does exist beside
    it, turns a guessing game into a one-look fix.
    """

    if data_root.is_symlink() and not data_root.exists():
        target = os.readlink(data_root)
        return f"auto_discover: data_root {data_root} is a broken symlink pointing at {target}"
    if data_root.is_file():
        return f"auto_discover: data_root {data_root} is a file, not a directory"
    if data_root.exists():
        return (
            f"auto_discover: data_root {data_root} exists but cannot be listed as a directory "
            "(check permissions, or whether the mount is present in this shell)"
        )

    ancestor = data_root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    missing = data_root
    while missing.parent != ancestor and missing.parent != missing:
        missing = missing.parent

    message = [
        f"auto_discover: data_root {data_root} does not exist",
        f"  the path is fine up to {ancestor}",
        f"  but {missing.name!r} is not there",
    ]
    try:
        siblings = sorted(item.name for item in ancestor.iterdir())
    except OSError as exc:
        message.append(f"  and {ancestor} could not be listed ({exc})")
        return "\n".join(message)

    close = [name for name in siblings if name.lower() == missing.name.lower()]
    if close:
        message.append(f"  did you mean {close[0]!r}? (case differs)")
    shown = siblings[:20]
    listing = ", ".join(shown) + (f", ... (+{len(siblings) - len(shown)} more)" if len(siblings) > len(shown) else "")
    message.append(f"  {ancestor} contains: {listing or '(empty)'}")
    return "\n".join(message)


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
