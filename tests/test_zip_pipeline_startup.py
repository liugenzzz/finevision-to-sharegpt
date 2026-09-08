"""重启时不该重读整份产出。

500 万条的 train.jsonl 有 7 GB，而启动路径以前要把它读三遍——两遍给一个
稳态下追加 0 条的 backfill，一遍给一个 MySQL 模式下没人查的 set。加上 166 个
分数据集文件，一次重启读 21 GB，全在网络盘上。
"""

import json
import pathlib

from finevision_to_sharegpt import zip_pipeline
from finevision_to_sharegpt.config_loader import DatasetRequest, ZipTaskConfig
from finevision_to_sharegpt.dataset_registry import RegisteredDataset
from finevision_to_sharegpt.db.config import MysqlConfig
from finevision_to_sharegpt.db.ledger import JsonlLedger


def make_config(tmp_path, **overrides):
    fields = dict(
        dataset_registry=tmp_path / "registry.json",
        datasets=[],
        output_jsonl=tmp_path / "out" / "train.jsonl",
        output_json=tmp_path / "out" / "train.json",
        images_root=tmp_path / "out" / "images",
        resume=True,
    )
    fields.update(overrides)
    return ZipTaskConfig(**fields)


def write_records(path, ids, dataset="chartqa"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for sample_id in ids:
            handle.write(json.dumps({
                "id": sample_id,
                "images": [f"images/{dataset}/{sample_id}.jpg"],
                "conversations": [{"from": "human", "value": "hi"}],
            }, ensure_ascii=False) + "\n")


def datasets_for(tmp_path, names=("chartqa",)):
    return [
        (RegisteredDataset(name=name, source_path=tmp_path / name), DatasetRequest(name=name))
        for name in names
    ]


# -- backfill 只在真落后时才跑 -----------------------------------------------


def test_a_backfill_that_would_append_nothing_is_not_run(tmp_path):
    """稳态：两边一直是同步写的，backfill 读几百 MB 只为追加 0 条。"""

    config = make_config(tmp_path)
    datasets = datasets_for(tmp_path)
    write_records(config.output_jsonl, ["a", "b", "c"])
    ds_jsonl, _ = zip_pipeline._dataset_output_paths(config, "chartqa")
    write_records(ds_jsonl, ["a", "b", "c"])

    assert zip_pipeline._backfill_is_needed(config, datasets) is False


def test_a_backfill_still_runs_when_the_per_dataset_file_is_behind(tmp_path):
    """老产出确实缺分数据集文件的时候，一次都不能少跑。"""

    config = make_config(tmp_path)
    datasets = datasets_for(tmp_path)
    write_records(config.output_jsonl, ["a", "b", "c"])

    assert zip_pipeline._backfill_is_needed(config, datasets) is True

    zip_pipeline._backfill_dataset_jsonls(config, datasets)

    ds_jsonl, _ = zip_pipeline._dataset_output_paths(config, "chartqa")
    assert [json.loads(line)["id"] for line in ds_jsonl.read_text(encoding="utf-8").splitlines()] \
        == ["a", "b", "c"]
    # 补完之后就不该再跑了。
    assert zip_pipeline._backfill_is_needed(config, datasets) is False


def test_the_check_never_opens_the_files(tmp_path, monkeypatch):
    """判据只能是 stat：一份 7 GB 的产出不该为了这个判断被读一遍，更别说解析。"""

    config = make_config(tmp_path)
    datasets = datasets_for(tmp_path)
    write_records(config.output_jsonl, ["a", "b", "c"])
    ds_jsonl, _ = zip_pipeline._dataset_output_paths(config, "chartqa")
    write_records(ds_jsonl, ["a", "b", "c"])

    real_open = pathlib.Path.open

    def forbidden(self, *args, **kwargs):
        raise AssertionError(f"判断要不要 backfill 时打开了 {self}")

    monkeypatch.setattr(pathlib.Path, "open", forbidden)
    try:
        assert zip_pipeline._backfill_is_needed(config, datasets) is False
    finally:
        monkeypatch.setattr(pathlib.Path, "open", real_open)


# -- 那一大坨 id 只在文件账本模式下加载 ---------------------------------------


def test_mysql_mode_does_not_load_the_completed_ids(tmp_path, monkeypatch):
    """MySQL 账本的 is_consumed 查的是 scan_plan 现查的集合，收下这个从不读。"""

    config = make_config(tmp_path, mysql=MysqlConfig(
        host="h", port=3306, user="u", password="p", database="d"))
    write_records(config.output_jsonl, ["a", "b", "c"])

    loaded = []
    monkeypatch.setattr(zip_pipeline, "load_completed_ids",
                        lambda paths: loaded.append(list(paths)) or {"a", "b", "c"})

    # 站在 MySQLLedger 的位置上——不是 JsonlLedger，所以那个集合不该被加载。
    stub = type("MySQLLedgerStub", (), {"completed_ids": set()})()
    monkeypatch.setattr(zip_pipeline, "open_ledger", lambda *a, **k: stub)
    monkeypatch.setattr(zip_pipeline, "load_dataset_registry", lambda path: [])
    monkeypatch.setattr(zip_pipeline, "_selected_datasets", lambda registry, names: [])

    zip_pipeline._prepare_zip_run(config, truncate_failed=False)

    assert loaded == []


def test_a_mysql_fallback_to_file_mode_still_loads_them(tmp_path, monkeypatch):
    """陷阱：MySQL 连不上时 open_ledger 会退回文件账本。

    那时候这个集合是续跑唯一的依据——按「配了 MySQL」来跳过加载，会让整轮
    已翻完的样本重翻一遍。所以判据必须是拿到手的账本类型。
    """

    config = make_config(tmp_path, mysql=MysqlConfig(
        host="h", port=3306, user="u", password="p", database="d"))
    write_records(config.output_jsonl, ["a", "b", "c"])

    monkeypatch.setattr(zip_pipeline, "load_dataset_registry", lambda path: [])
    monkeypatch.setattr(zip_pipeline, "_selected_datasets", lambda registry, names: [])
    # 连不上、fail_fast 关着 → 退回文件账本
    monkeypatch.setattr(zip_pipeline, "open_ledger", lambda *a, **k: JsonlLedger(set()))

    _store, _datasets, ledger = zip_pipeline._prepare_zip_run(config, truncate_failed=False)

    assert ledger.completed_ids == {"a", "b", "c"}
    assert ledger.is_consumed(None, "a", 0, None) is True


def test_startup_still_wires_the_backfill_when_it_is_needed(tmp_path, monkeypatch):
    """接线本身也要有测试：跳过判断写错成「一律跳过」时得当场红。

    只测 _backfill_is_needed 是不够的——它返回 True 而调用点没接上，
    老产出的分数据集文件就会永远空着，而合并那份看起来一切正常。
    """

    config = make_config(tmp_path)
    datasets = datasets_for(tmp_path)
    write_records(config.output_jsonl, ["a", "b", "c"])

    monkeypatch.setattr(zip_pipeline, "load_dataset_registry", lambda path: [])
    monkeypatch.setattr(zip_pipeline, "_selected_datasets", lambda registry, names: datasets)

    zip_pipeline._prepare_zip_run(config, truncate_failed=False)

    ds_jsonl, _ = zip_pipeline._dataset_output_paths(config, "chartqa")
    assert ds_jsonl.exists(), "分数据集文件没被补上——backfill 的调用点断了"
    assert len(ds_jsonl.read_text(encoding='utf-8').splitlines()) == 3
