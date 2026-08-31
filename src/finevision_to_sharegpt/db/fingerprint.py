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
