from __future__ import annotations

import hashlib
from pathlib import Path


def detect_image_extension(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


class ImageStore:
    def __init__(self, output_root: Path | str, images_dir: str = "images") -> None:
        self.output_root = Path(output_root)
        self.images_dir = images_dir.strip("/")

    def relative_path(self, data: bytes, dataset_name: str | None = None) -> str:
        """Where this image belongs, without touching the filesystem.

        The name is the content hash, so the path is known from the bytes
        alone. That lets a metadata-only scan record correct paths and leave
        the pixels on disk for whoever actually consumes the sample.
        """

        digest = hashlib.sha256(data).hexdigest()
        ext = detect_image_extension(data)
        relative_dir = Path(self.images_dir)
        if dataset_name:
            relative_dir = relative_dir / _safe_path_part(dataset_name)
        return (relative_dir / f"{digest}.{ext}").as_posix()

    def save(self, data: bytes, dataset_name: str | None = None, write: bool = True) -> str:
        relative_path = self.relative_path(data, dataset_name)
        if not write:
            return relative_path
        absolute_path = self.output_root / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        if not absolute_path.exists():
            absolute_path.write_bytes(data)
        return relative_path


def _safe_path_part(value: str) -> str:
    return value.replace("\\", "_").replace("/", "_").strip() or "dataset"
