from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .dataset_registry import RegisteredDataset


@dataclass(frozen=True)
class ExtractedParquet:
    name: str
    path: Path


def find_zip_inputs(
    inputs: list[Path | str] | None = None,
    input_dir: Path | str | None = None,
    glob_pattern: str = "*.zip",
) -> list[Path]:
    found = [Path(item) for item in (inputs or [])]
    if input_dir is not None:
        found.extend(sorted(Path(input_dir).glob(glob_pattern)))
    return sorted(found)


def extract_parquets_from_zip(zip_path: Path | str, extract_root: Path | str) -> list[ExtractedParquet]:
    zip_path = Path(zip_path)
    extract_root = Path(extract_root)
    target_root = extract_root / zip_path.stem
    target_root.mkdir(parents=True, exist_ok=True)
    extracted: list[ExtractedParquet] = []

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.endswith(".parquet"):
                continue
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe zip member path: {member.filename}")
            output_path = target_root / member_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output_path.open("wb") as target:
                target.write(source.read())
            extracted.append(ExtractedParquet(name=member.filename, path=output_path))

    return extracted


def list_parquets_in_dir(dir_path: Path | str) -> list[ExtractedParquet]:
    """List parquet files under a dataset directory without copying anything.

    Names are directory-relative posix paths, matching the member names a zip
    would yield, so sample ids look the same either way.
    """

    dir_path = Path(dir_path)
    found = [
        ExtractedParquet(name=path.relative_to(dir_path).as_posix(), path=path)
        for path in sorted(dir_path.rglob("*.parquet"))
        if path.is_file()
    ]
    return found


def iter_dataset_parquets(
    dataset: "RegisteredDataset",
    extract_root: Path | str,
) -> list[ExtractedParquet]:
    """Resolve one dataset to its parquet files.

    A directory is read in place, so it needs no temp space and no upfront
    decompression; an archive is expanded under ``extract_root`` as before.
    """

    if dataset.is_directory:
        return list_parquets_in_dir(dataset.source_path)
    return extract_parquets_from_zip(dataset.source_path, extract_root)
