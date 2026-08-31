from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

SAMPLE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ZipFingerprint:
    """Identity of one concrete revision of a dataset archive."""

    source_hash: str
    file_size: int
    file_mtime: int


def zip_fingerprint(path: Path | str, sample_bytes: int = SAMPLE_BYTES) -> ZipFingerprint:
    """Fingerprint an archive from its size plus its head and tail.

    Hashing tens of gigabytes on every run costs a full read, and ``mtime``
    alone changes on a plain copy. Mixing the size into the digest with the
    two ends keeps the check cheap while still catching a rebuilt archive:
    a different revision changes the central directory in the tail even when
    the head is untouched.
    """

    path = Path(path)
    stat = path.stat()
    size = stat.st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    digest.update(b":")
    with path.open("rb") as handle:
        if size <= sample_bytes * 2:
            digest.update(handle.read())
        else:
            digest.update(handle.read(sample_bytes))
            handle.seek(-sample_bytes, 2)
            digest.update(handle.read(sample_bytes))
    return ZipFingerprint(
        source_hash=digest.hexdigest(),
        file_size=size,
        file_mtime=int(stat.st_mtime),
    )


def directory_fingerprint(path: Path | str, pattern: str = "*.parquet") -> ZipFingerprint:
    """Fingerprint a dataset directory from its parquet inventory.

    Hashes the sorted ``(relative path, size)`` pairs rather than file
    contents: a FineVision directory is tens of gigabytes, and re-reading it
    on every run to detect a change nobody made would dominate startup.
    Mtime is deliberately excluded because rsync and cp rewrite it, which
    would orphan the consumption history on every copy.

    The blind spot is a regenerated file with an identical name and byte
    count; for parquet that is not a realistic collision.
    """

    path = Path(path)
    digest = hashlib.sha256()
    total = 0
    newest = 0
    for item in sorted(path.rglob(pattern)):
        if not item.is_file():
            continue
        stat = item.stat()
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b":")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\n")
        total += stat.st_size
        newest = max(newest, int(stat.st_mtime))
    return ZipFingerprint(source_hash=digest.hexdigest(), file_size=total, file_mtime=newest)


def source_fingerprint(path: Path | str) -> ZipFingerprint:
    """Fingerprint a dataset source, archive or directory."""

    path = Path(path)
    if path.is_dir():
        return directory_fingerprint(path)
    return zip_fingerprint(path)
