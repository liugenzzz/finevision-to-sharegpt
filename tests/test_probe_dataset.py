"""Probing a collection: can the pipeline actually read what is in there?"""

import sys
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_dataset  # noqa: E402


def write_parquet(path: Path, table: pa.Table) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return path


def readable_table() -> pa.Table:
    return pa.table(
        {
            "images": [[{"bytes": b"png bytes", "path": "a.png"}]],
            "texts": [[{"user": "What is this?", "assistant": "A cat."}]],
        }
    )


# -- parquet -----------------------------------------------------------------


def test_a_parquet_with_image_bytes_and_turns_is_registrable(tmp_path):
    write_parquet(tmp_path / "data" / "part-000.parquet", readable_table())

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "ok"
    assert probe.rows_ok == 1
    assert probe.images == 1
    assert probe.columns == ["images", "texts"]
    assert "What is this?" in probe.sample_text


def test_image_paths_without_bytes_read_as_missing_image(tmp_path):
    # The failure survey_roots.py cannot see: the extension census says
    # parquet, but the images live outside the file as bare paths.
    write_parquet(
        tmp_path / "part-000.parquet",
        pa.table({"image": ["/data/a.png"], "texts": [[{"user": "hi", "assistant": "there"}]]}),
    )

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "missing_image"
    assert "图片路径" in probe.advice


def test_images_without_any_text_read_as_missing_text(tmp_path):
    write_parquet(tmp_path / "part-000.parquet", pa.table({"image": [b"png bytes"]}))

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "missing_text"


def test_an_empty_parquet_is_reported_as_empty(tmp_path):
    write_parquet(tmp_path / "part-000.parquet", readable_table().slice(0, 0))

    assert probe_dataset.probe_dataset(tmp_path).verdict == "empty"


def test_only_the_requested_number_of_rows_is_parsed(tmp_path):
    table = pa.concat_tables([readable_table()] * 20)
    write_parquet(tmp_path / "part-000.parquet", table)

    assert probe_dataset.probe_dataset(tmp_path, rows=3).rows_seen == 3


# -- zip ---------------------------------------------------------------------


def test_a_zip_of_parquet_is_probed_through_the_archive(tmp_path):
    parquet = write_parquet(tmp_path / "src" / "part-000.parquet", readable_table())
    (tmp_path / "pack").mkdir()
    with zipfile.ZipFile(tmp_path / "pack" / "okvqa.zip", "w") as archive:
        archive.write(parquet, arcname="nested/part-000.parquet")

    probe = probe_dataset.probe_dataset(tmp_path / "pack")

    assert probe.verdict == "ok"
    assert probe.source == "okvqa.zip!nested/part-000.parquet"


def test_a_zip_of_loose_images_is_unreadable_and_says_what_is_inside(tmp_path):
    with zipfile.ZipFile(tmp_path / "images.zip", "w") as archive:
        archive.writestr("a.png", b"png")
        archive.writestr("b.png", b"png")

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "unreadable"
    assert ".png×2" in probe.detail


def test_a_directory_of_videos_is_unreadable(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"video")

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "unreadable"
    assert ".mp4×1" in probe.detail


# -- cli ---------------------------------------------------------------------


def test_all_probes_every_subdirectory_and_survives_a_broken_one(tmp_path, capsys):
    write_parquet(tmp_path / "good" / "part-000.parquet", readable_table())
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "part-000.parquet").write_bytes(b"not parquet at all")

    assert probe_dataset.main([str(tmp_path), "--all"]) == 0

    output = capsys.readouterr().out
    assert "共 2 个，可注册 1 个" in output
    assert "打开失败" in output


def test_only_bad_hides_the_usable_ones_but_still_counts_them(tmp_path, capsys):
    """一百多个集合里要处理的只有不 ok 的，但汇总得照样算上全部。"""

    write_parquet(tmp_path / "good" / "part-000.parquet", readable_table())
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "part-000.parquet").write_bytes(b"not parquet at all")

    assert probe_dataset.main([str(tmp_path), "--all", "--only-bad"]) == 0

    output = capsys.readouterr().out
    assert "打开失败" in output
    assert "共 2 个，可注册 1 个" in output
    # 可注册的那个不该出现在逐条列表里，只出现在按结论分组的汇总里。
    assert output.count("good") == 1


def test_json_output_carries_the_reason_for_each_dataset(tmp_path, capsys):
    """结论要能被别的脚本读，不能只有给人看的那份。"""

    import json

    write_parquet(tmp_path / "good" / "part-000.parquet", readable_table())
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "part-000.parquet").write_bytes(b"not parquet at all")
    target = tmp_path / "verdicts.json"

    assert probe_dataset.main([str(tmp_path), "--all", "--json", str(target)]) == 0

    payload = {item["dataset"]: item for item in json.loads(target.read_text(encoding="utf-8"))}
    assert payload["good"]["verdict"] == "ok"
    assert payload["broken"]["verdict"] == "unreadable"
    assert "打开失败" in payload["broken"]["detail"]
    assert payload["good"]["advice"]


def test_a_missing_path_is_an_error(tmp_path, capsys):
    assert probe_dataset.main([str(tmp_path / "nope")]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_a_failed_probe_does_not_claim_the_rows_had_no_images(tmp_path, capsys):
    # missing_text means the images WERE found; printing "0 张图" contradicts it.
    write_parquet(tmp_path / "part-000.parquet", pa.table({"image": [b"png bytes"]}))

    probe_dataset.report("flickr30k", probe_dataset.probe_dataset(tmp_path))

    output = capsys.readouterr().out
    assert "一条都没解析出来" in output
    assert "0 张图" not in output


def test_dot_directories_are_not_treated_as_collections(tmp_path, capsys):
    # .cache is what HuggingFace leaves behind. Reporting it as a broken dataset
    # is noise, and walking it to find that out is wasted on a large tree.
    write_parquet(tmp_path / "chartqa" / "part-000.parquet", readable_table())
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "x.lock").write_text("", encoding="utf-8")

    assert probe_dataset.main([str(tmp_path), "--all"]) == 0

    captured = capsys.readouterr()
    assert ".cache" not in captured.out
    assert "跳过 1 个点开头的目录" in captured.err


def test_progress_goes_to_stderr_so_a_redirected_report_stays_clean(tmp_path, capsys):
    write_parquet(tmp_path / "chartqa" / "part-000.parquet", readable_table())

    probe_dataset.main([str(tmp_path), "--all"])

    captured = capsys.readouterr()
    assert "[1/1] chartqa" in captured.err
    assert "[1/1]" not in captured.out


def test_a_collection_with_no_parquet_names_what_it_holds_without_listing_everything(tmp_path):
    for index in range(700):
        (tmp_path / f"img{index}.png").write_bytes(b"png")

    probe = probe_dataset.probe_dataset(tmp_path)

    assert probe.verdict == "unreadable"
    assert ".png×" in probe.detail
