import hashlib

from finevision_to_sharegpt.image_store import ImageStore, detect_image_extension


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"jpeg payload" + b"\xff\xd9"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"png payload"
WEBP_BYTES = b"RIFFxxxxWEBP" + b"webp payload"


def test_detect_image_extension_from_magic_bytes():
    assert detect_image_extension(JPEG_BYTES) == "jpg"
    assert detect_image_extension(PNG_BYTES) == "png"
    assert detect_image_extension(WEBP_BYTES) == "webp"
    assert detect_image_extension(b"unknown bytes") == "jpg"


def test_image_store_saves_content_hashed_relative_path(tmp_path):
    store = ImageStore(output_root=tmp_path)

    relative_path = store.save(JPEG_BYTES)

    digest = hashlib.sha256(JPEG_BYTES).hexdigest()
    assert relative_path == f"images/{digest}.jpg"
    assert (tmp_path / relative_path).read_bytes() == JPEG_BYTES


def test_image_store_reuses_existing_hash_path(tmp_path):
    store = ImageStore(output_root=tmp_path)

    first = store.save(PNG_BYTES)
    second = store.save(PNG_BYTES)

    assert first == second
    assert len(list((tmp_path / "images").iterdir())) == 1


def test_image_store_saves_under_dataset_subdirectory(tmp_path):
    store = ImageStore(output_root=tmp_path)

    relative_path = store.save(JPEG_BYTES, dataset_name="okvqa")

    digest = hashlib.sha256(JPEG_BYTES).hexdigest()
    assert relative_path == f"images/okvqa/{digest}.jpg"
    assert (tmp_path / relative_path).read_bytes() == JPEG_BYTES
