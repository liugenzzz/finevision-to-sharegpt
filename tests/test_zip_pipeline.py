import json
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

from finevision_to_sharegpt.config_loader import load_zip_task_config
from finevision_to_sharegpt.zip_pipeline import run_export_zips, run_translate_zips, should_translate_to_chinese


def make_zip_dataset(tmp_path, dataset_name="okvqa"):
    data_root = tmp_path / "zips"
    data_root.mkdir()
    parquet_path = tmp_path / "part.parquet"
    pq.write_table(
        pa.table(
            {
                "images": [
                    [b"\xff\xd8\xffone", b"\x89PNG\r\n\x1a\ntwo"],
                    [b"\xff\xd8\xffthree"],
                ],
                "texts": [
                    [{"user": "Compare.", "assistant": "Two charts."}],
                    [{"user": "Read.", "assistant": "ABC"}],
                ],
            }
        ),
        parquet_path,
    )
    zip_path = data_root / f"{dataset_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(parquet_path, arcname="nested/part.parquet")
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps({"data_root": str(data_root), "datasets": {dataset_name: {"zip": f"{dataset_name}.zip"}}}),
        encoding="utf-8",
    )
    return registry


def _write_translate_zip_config(tmp_path, registry, **overrides):
    data = {
        "dataset_registry": str(registry),
        "datasets": ["okvqa"],
        "output_jsonl": str(tmp_path / "output" / "train.jsonl"),
        "chinese_ratio": 1.0,
        "seed": 42,
        "resume": False,
    }
    data.update(overrides)
    path = tmp_path / "translate.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _ConsumeOnlyPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            raise AssertionError(f"unexpected translation task: {task.id}")
        if False:
            yield None


class _SuccessfulPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            yield type(
                "Result",
                (),
                {
                    "ok": True,
                    "item": task,
                    "value": handler(task, object(), 120),
                    "error": None,
                    "backend_name": "gpu0",
                },
            )()


class _FailingPool:
    def map_unordered(self, tasks, handler):
        for task in tasks:
            yield type(
                "Result",
                (),
                {
                    "ok": False,
                    "item": task,
                    "value": None,
                    "error": "temporary failure",
                    "backend_name": "gpu0",
                },
            )()


def test_should_translate_to_chinese_is_stable_for_same_seed():
    first = should_translate_to_chinese("okvqa:part:1", chinese_ratio=0.7, seed=42)
    second = should_translate_to_chinese("okvqa:part:1", chinese_ratio=0.7, seed=42)

    assert first is second
    assert should_translate_to_chinese("anything", chinese_ratio=1.0, seed=42)
    assert not should_translate_to_chinese("anything", chinese_ratio=0.0, seed=42)


def test_run_export_zips_writes_jsonl_json_images_and_report(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "export.json"
    output_jsonl = tmp_path / "output" / "train_en.jsonl"
    output_json = tmp_path / "output" / "train_en.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(output_jsonl),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    stats = run_export_zips(load_zip_task_config(config_path))

    records = [json.loads(line) for line in output_jsonl.read_text(encoding="utf-8").splitlines()]
    assert stats["written"] == 2
    assert len(records) == 2
    assert records[0]["id"] == "okvqa:nested/part.parquet:0"
    assert len(records[0]["images"]) == 2
    assert records[0]["images"][0].startswith("images/okvqa/")
    assert records[0]["conversations"][0]["value"].startswith("<image>\n<image>\n")
    assert (tmp_path / "output" / records[0]["images"][0]).exists()
    assert json.loads(output_json.read_text(encoding="utf-8")) == records
    report = json.loads((tmp_path / "output" / "report.json").read_text(encoding="utf-8"))
    assert report["datasets"]["okvqa"]["written"] == 2


def test_run_export_zips_updates_parquet_progress(tmp_path):
    class FakeProgressFactory:
        def __init__(self):
            self.instances = []

        def __call__(self, iterable, total, desc, unit, **kwargs):
            progress = FakeProgress(iterable, total, desc, unit, **kwargs)
            self.instances.append(progress)
            return progress

        def of_unit(self, unit):
            return [item for item in self.instances if item.unit == unit]

    class FakeProgress:
        def __init__(self, iterable, total, desc, unit, leave=None, position=None):
            self.iterable = iterable
            self.total = total
            self.desc = desc
            self.unit = unit
            self.leave = leave
            self.position = position
            self.postfixes = []
            self.descriptions = []
            self.n = 0
            self.closed = False

        def __iter__(self):
            yield from self.iterable

        def set_postfix(self, **kwargs):
            self.postfixes.append((self.n, kwargs))

        def set_description(self, text):
            self.descriptions.append(text)

        def update(self, step):
            self.n += step

        def close(self):
            self.closed = True

    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "export.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(tmp_path / "output" / "train_en.jsonl"),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )
    progress_factory = FakeProgressFactory()

    run_export_zips(load_zip_task_config(config_path), progress_factory=progress_factory)

    rows = progress_factory.of_unit("row")
    overall = progress_factory.of_unit("dataset")
    assert rows and rows[0].postfixes[-1][1]["written"] == 2
    # Shard bars collapse when done; only the dataset bar stays on screen.
    assert rows[0].leave is False
    assert len(overall) == 1
    assert overall[0].total == 1
    assert overall[0].leave is True
    assert overall[0].descriptions == ["[okvqa] of 1 datasets"]
    assert overall[0].postfixes[-1][1]["written"] == 2
    # The bar is driven by hand, so its count is correct at every redraw.
    assert overall[0].iterable is None
    assert overall[0].n == 1
    assert overall[0].closed


def test_run_translate_zips_translates_chinese_ratio_records_with_backend_pool(tmp_path):
    class FakePool:
        def map_unordered(self, tasks, handler):
            for task in tasks:
                yield type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "item": task,
                        "value": handler(task, object(), 120),
                        "error": None,
                        "backend_name": "gpu0",
                    },
                )()

    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "translate.json"
    output_jsonl = tmp_path / "output" / "train.jsonl"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(output_jsonl),
                "chinese_ratio": 1.0,
                "seed": 42,
                "resume": False,
            }
        ),
        encoding="utf-8",
    )
    translated = []

    def handler(task, client, timeout):
        translated.append(task.id)
        return {"id": task.id, "images": task.image_paths, "conversations": [{"from": "human", "value": "<image>\n中文"}]}

    stats = run_translate_zips(load_zip_task_config(config_path), FakePool(), handler)

    records = [json.loads(line) for line in output_jsonl.read_text(encoding="utf-8").splitlines()]
    raw_path = tmp_path / "output" / "okvqa" / "raw.jsonl"
    raw_records = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert stats["chinese"] == 2
    assert stats["english"] == 0
    assert translated == ["okvqa:nested/part.parquet:0", "okvqa:nested/part.parquet:1"]
    assert records[0]["conversations"][0]["value"].endswith("中文")
    assert [record["id"] for record in raw_records] == [
        "okvqa:nested/part.parquet:0",
        "okvqa:nested/part.parquet:1",
    ]
    assert raw_records[0]["conversations"][0]["value"].endswith("Compare.")
    assert raw_records[0]["images"] == records[0]["images"]
    assert not raw_path.with_suffix(".json").exists()


def test_run_translate_zips_writes_per_dataset_and_combined(tmp_path):
    class FakePool:
        def map_unordered(self, tasks, handler):
            for task in tasks:
                yield type(
                    "Result",
                    (),
                    {"ok": True, "item": task, "value": handler(task, object(), 120), "error": None, "backend_name": "gpu0"},
                )()

    data_root = tmp_path / "zips"
    data_root.mkdir()
    for name in ("okvqa", "captcha"):
        parquet_path = tmp_path / f"{name}.parquet"
        pq.write_table(
            pa.table({"images": [[b"\xff\xd8\xff" + name.encode()]], "texts": [[{"user": "Q", "assistant": "A"}]]}),
            parquet_path,
        )
        with zipfile.ZipFile(data_root / f"{name}.zip", "w") as archive:
            archive.write(parquet_path, arcname="part.parquet")
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps(
            {"data_root": str(data_root), "datasets": {"okvqa": {"zip": "okvqa.zip"}, "captcha": {"zip": "captcha.zip"}}}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "output"
    config_path = tmp_path / "translate.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa", "captcha"],
                "output_jsonl": str(out / "train.jsonl"),
                "chinese_ratio": 1.0,
                "seed": 42,
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": [{"from": "human", "value": "<image>\n中文"}]}

    run_translate_zips(load_zip_task_config(config_path), FakePool(), handler)

    combined = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(combined) == 2
    assert json.loads((out / "train.json").read_text(encoding="utf-8")) == combined

    okvqa = [json.loads(line) for line in (out / "okvqa" / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    captcha = [json.loads(line) for line in (out / "captcha" / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["id"] for record in okvqa] == ["okvqa:part.parquet:0"]
    assert [record["id"] for record in captcha] == ["captcha:part.parquet:0"]
    assert okvqa[0]["images"][0].startswith("images/okvqa/")
    assert json.loads((out / "okvqa" / "train.json").read_text(encoding="utf-8")) == okvqa
    assert json.loads((out / "captcha" / "train.json").read_text(encoding="utf-8")) == captcha


def test_run_translate_zips_streams_chinese_tasks_to_backend_pool(tmp_path):
    class FakePool:
        def __init__(self):
            self.received_list = None

        def map_unordered(self, tasks, handler):
            self.received_list = isinstance(tasks, list)
            for task in tasks:
                yield type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "item": task,
                        "value": handler(task, object(), 120),
                        "error": None,
                        "backend_name": "gpu0",
                    },
                )()

    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "translate.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(tmp_path / "output" / "train.jsonl"),
                "chinese_ratio": 1.0,
                "seed": 42,
                "resume": False,
            }
        ),
        encoding="utf-8",
    )
    pool = FakePool()

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    run_translate_zips(load_zip_task_config(config_path), pool, handler)

    assert pool.received_list is False


def test_run_translate_zips_updates_parquet_progress(tmp_path):
    class FakePool:
        def map_unordered(self, tasks, handler):
            for task in tasks:
                yield type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "item": task,
                        "value": handler(task, object(), 120),
                        "error": None,
                        "backend_name": "gpu0",
                    },
                )()

    class FakeProgressFactory:
        def __init__(self):
            self.instances = []

        def __call__(self, iterable, total, desc, unit, **kwargs):
            progress = FakeProgress(iterable, total, desc, unit, **kwargs)
            self.instances.append(progress)
            return progress

        def of_unit(self, unit):
            return [item for item in self.instances if item.unit == unit]

    class FakeProgress:
        def __init__(self, iterable, total, desc, unit, leave=None, position=None):
            self.iterable = iterable
            self.total = total
            self.desc = desc
            self.unit = unit
            self.leave = leave
            self.position = position
            self.postfixes = []
            self.descriptions = []
            self.n = 0
            self.closed = False

        def __iter__(self):
            yield from self.iterable

        def set_postfix(self, **kwargs):
            self.postfixes.append((self.n, kwargs))

        def set_description(self, text):
            self.descriptions.append(text)

        def update(self, step):
            self.n += step

        def close(self):
            self.closed = True

    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "translate.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(tmp_path / "output" / "train.jsonl"),
                "chinese_ratio": 1.0,
                "seed": 42,
                "resume": False,
            }
        ),
        encoding="utf-8",
    )
    progress_factory = FakeProgressFactory()

    run_translate_zips(
        load_zip_task_config(config_path),
        FakePool(),
        lambda task, client, timeout: {"id": task.id, "images": task.image_paths, "conversations": []},
        progress_factory=progress_factory,
    )

    rows = progress_factory.of_unit("row")
    overall = progress_factory.of_unit("dataset")
    assert rows and rows[0].postfixes[-1][1]["chinese"] == 2
    assert len(overall) == 1
    # Translation lands asynchronously, so the overall bar has to be refreshed
    # from the result loop, not only while scanning.
    assert overall[0].postfixes[-1][1]["written"] == 2


def test_run_translate_zips_emit_raw_false_does_not_create_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = _write_translate_zip_config(
        tmp_path,
        registry,
        chinese_ratio=0.0,
        emit_raw=False,
    )

    stats = run_translate_zips(
        load_zip_task_config(config_path),
        _ConsumeOnlyPool(),
        lambda *_args: {},
    )

    assert stats["written"] == 2
    assert (tmp_path / "output" / "train.jsonl").exists()
    assert not (tmp_path / "output" / "okvqa" / "raw.jsonl").exists()


def test_run_translate_zips_resume_false_truncates_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    raw_path = tmp_path / "output" / "okvqa" / "raw.jsonl"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"id":"stale"}\n', encoding="utf-8")
    config_path = _write_translate_zip_config(tmp_path, registry, chinese_ratio=0.0)

    run_translate_zips(
        load_zip_task_config(config_path),
        _ConsumeOnlyPool(),
        lambda *_args: {},
    )

    raw_ids = [json.loads(line)["id"] for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert raw_ids == [
        "okvqa:nested/part.parquet:0",
        "okvqa:nested/part.parquet:1",
    ]


def test_run_translate_zips_resume_does_not_duplicate_raw_after_translation_failure(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = _write_translate_zip_config(tmp_path, registry)

    first_stats = run_translate_zips(
        load_zip_task_config(config_path),
        _FailingPool(),
        lambda *_args: {},
    )

    raw_path = tmp_path / "output" / "okvqa" / "raw.jsonl"
    assert first_stats["failed"] == 2
    assert len(raw_path.read_text(encoding="utf-8").splitlines()) == 2

    _write_translate_zip_config(tmp_path, registry, resume=True)

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    second_stats = run_translate_zips(
        load_zip_task_config(config_path),
        _SuccessfulPool(),
        handler,
    )

    raw_ids = [json.loads(line)["id"] for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert second_stats["written"] == 2
    assert raw_ids == [
        "okvqa:nested/part.parquet:0",
        "okvqa:nested/part.parquet:1",
    ]


def test_run_export_zips_does_not_create_raw_jsonl(tmp_path):
    registry = make_zip_dataset(tmp_path)
    config_path = tmp_path / "export.json"
    config_path.write_text(
        json.dumps(
            {
                "dataset_registry": str(registry),
                "datasets": ["okvqa"],
                "output_jsonl": str(tmp_path / "output" / "train_en.jsonl"),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    run_export_zips(load_zip_task_config(config_path))

    assert not (tmp_path / "output" / "okvqa" / "raw.jsonl").exists()


# -- per-dataset limits across a resume ---------------------------------------


def make_zip_dataset_with_rows(tmp_path, rows, dataset_name="okvqa"):
    data_root = tmp_path / "zips"
    data_root.mkdir(exist_ok=True)
    parquet_path = tmp_path / f"{dataset_name}.parquet"
    pq.write_table(
        pa.table(
            {
                "images": [[b"\xff\xd8\xff" + str(i).encode()] for i in range(rows)],
                "texts": [[{"user": f"Q{i}", "assistant": f"A{i}"}] for i in range(rows)],
            }
        ),
        parquet_path,
    )
    zip_path = data_root / f"{dataset_name}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(parquet_path, arcname="nested/part.parquet")
    registry = tmp_path / "datasets.json"
    registry.write_text(
        json.dumps({"data_root": str(data_root), "datasets": {dataset_name: {"zip": f"{dataset_name}.zip"}}}),
        encoding="utf-8",
    )
    return registry


def _echo_handler(task, client, timeout):
    return {"id": task.id, "images": task.image_paths, "conversations": []}


def test_limit_counts_what_an_earlier_run_already_finished(tmp_path):
    """A restart must not translate a fresh quota on top of the existing output.

    Rows an earlier run finished come back as ``skipped``. If those do not count
    toward the limit, every interruption adds another full quota, and a run that
    gets restarted a few times ends up with several times the planned mix.
    """

    registry = make_zip_dataset_with_rows(tmp_path, rows=6)
    config_path = _write_translate_zip_config(tmp_path, registry, limit_per_dataset=2)
    output = tmp_path / "output" / "train.jsonl"

    first = run_translate_zips(load_zip_task_config(config_path), _SuccessfulPool(), _echo_handler)
    assert first["written"] == 2

    _write_translate_zip_config(tmp_path, registry, limit_per_dataset=2, resume=True)
    second = run_translate_zips(load_zip_task_config(config_path), _SuccessfulPool(), _echo_handler)

    assert second["written"] == 0
    assert second["skipped"] == 2
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_a_resume_still_fills_a_limit_that_was_not_reached(tmp_path):
    """Counting skipped rows must not stop a run that is genuinely unfinished."""

    registry = make_zip_dataset_with_rows(tmp_path, rows=6)
    config_path = _write_translate_zip_config(tmp_path, registry, limit_per_dataset=2)
    output = tmp_path / "output" / "train.jsonl"

    run_translate_zips(load_zip_task_config(config_path), _SuccessfulPool(), _echo_handler)

    # The plan grew: this dataset should now contribute 5 rows in total.
    _write_translate_zip_config(tmp_path, registry, limit_per_dataset=5, resume=True)
    second = run_translate_zips(load_zip_task_config(config_path), _SuccessfulPool(), _echo_handler)

    assert second["written"] == 3
    assert second["skipped"] == 2
    ids = [json.loads(line)["id"] for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(ids) == 5
    assert len(set(ids)) == 5


def test_export_limit_also_counts_earlier_rows(tmp_path):
    registry = make_zip_dataset_with_rows(tmp_path, rows=6)
    output = tmp_path / "export" / "train.jsonl"

    def write_config(resume):
        path = tmp_path / "export.json"
        path.write_text(
            json.dumps(
                {
                    "dataset_registry": str(registry),
                    "datasets": ["okvqa"],
                    "output_jsonl": str(output),
                    "limit_per_dataset": 2,
                    "resume": resume,
                }
            ),
            encoding="utf-8",
        )
        return path

    run_export_zips(load_zip_task_config(write_config(False)))
    second = run_export_zips(load_zip_task_config(write_config(True)))

    assert second["written"] == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
