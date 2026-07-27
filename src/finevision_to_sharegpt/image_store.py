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

    def save(self, data: bytes, dataset_name: str | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        ext = detect_image_extension(data)
        relative_dir = Path(self.images_dir)
        if dataset_name:
            relative_dir = relative_dir / _safe_path_part(dataset_name)
        relative_path = relative_dir / f"{digest}.{ext}"
        absolute_path = self.output_root / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        if not absolute_path.exists():
            absolute_path.write_bytes(data)
        return relative_path.as_posix()


def _safe_path_part(value: str) -> str:
    return value.replace("\\", "_").replace("/", "_").strip() or "dataset"
