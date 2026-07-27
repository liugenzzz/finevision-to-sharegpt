import json

from finevision_to_sharegpt.models import SourceSample, SourceTurn
from finevision_to_sharegpt.translation_job import (
    TranslationTask,
    record_to_source_sample,
    run_translation_job,
    run_translation_job_with_backend_pool,
)


def test_record_to_source_sample_reads_multiple_images_and_preserves_turns(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "a.jpg").write_bytes(b"\xff\xd8\xffone")
    (images_dir / "b.jpg").write_bytes(b"\x89PNG\r\n\x1a\ntwo")
    record = {
        "id": "multi",
        "images": ["images/a.jpg", "images/b.jpg"],
        "conversations": [
            {"from": "human", "value": "<image>\n<image>\nCompare."},
            {"from": "gpt", "value": "Two charts."},
        ],
    }

    sample = record_to_source_sample(record, tmp_path)

    assert sample.id == "multi"
    assert sample.image_bytes_list == [b"\xff\xd8\xffone", b"\x89PNG\r\n\x1a\ntwo"]
    assert [(turn.role, turn.text) for turn in sample.turns] == [
        ("human", "Compare."),
        ("gpt", "Two charts."),
    ]


def test_run_translation_job_writes_output_done_failed_and_final_json(tmp_path):
    output_jsonl = tmp_path / "out.jsonl"
    output_json = tmp_path / "out.json"
    done_path = tmp_path / "out.done.jsonl"
    failed_path = tmp_path / "out.failed.jsonl"
    tasks = [
        TranslationTask(
            id="one",
            sample=SourceSample("one", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["images/a.jpg"],
        ),
        TranslationTask(
            id="two",
            sample=SourceSample("two", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["images/b.jpg"],
        ),
    ]

    def translate(task):
        if task.id == "two":
            raise RuntimeError("model timeout")
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    stats = run_translation_job(tasks, output_jsonl, output_json, done_path, failed_path, translate, resume=False)

    assert stats == {"processed": 2, "written": 1, "failed": 1, "skipped": 0}
    assert output_jsonl.read_text(encoding="utf-8").splitlines() == [
        '{"id": "one", "images": ["images/a.jpg"], "conversations": []}'
    ]
    assert done_path.read_text(encoding="utf-8").splitlines() == [
        '{"id": "one", "images": ["images/a.jpg"], "conversations": []}'
    ]
    failed = json.loads(failed_path.read_text(encoding="utf-8").strip())
    assert failed["id"] == "two"
    assert failed["error"] == "model timeout"
    assert json.loads(output_json.read_text(encoding="utf-8")) == [
        {"id": "one", "images": ["images/a.jpg"], "conversations": []}
    ]


def test_run_translation_job_skips_completed_ids_from_done_file(tmp_path):
    output_jsonl = tmp_path / "out.jsonl"
    output_json = tmp_path / "out.json"
    done_path = tmp_path / "out.done.jsonl"
    failed_path = tmp_path / "out.failed.jsonl"
    done_path.write_text('{"id":"done","images":["images/a.jpg"],"conversations":[]}\n', encoding="utf-8")
    tasks = [
        TranslationTask(
            id="done",
            sample=SourceSample("done", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["images/a.jpg"],
        ),
        TranslationTask(
            id="new",
            sample=SourceSample("new", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["images/b.jpg"],
        ),
    ]
    calls = []

    def translate(task):
        calls.append(task.id)
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    stats = run_translation_job(tasks, output_jsonl, output_json, done_path, failed_path, translate, resume=True)

    assert calls == ["new"]
    assert stats["skipped"] == 1
    assert stats["written"] == 1


def test_run_translation_job_resumes_when_old_data_only_in_output_json(tmp_path):
    # Reproduces the bug where an earlier run produced only the final json
    # array (no jsonl). The old records must be preserved and skipped, not
    # dropped when the final json is regenerated from the jsonl.
    output_jsonl = tmp_path / "out.jsonl"
    output_json = tmp_path / "out.json"
    done_path = tmp_path / "out.done.jsonl"
    failed_path = tmp_path / "out.failed.jsonl"
    output_json.write_text(
        json.dumps([{"id": "old", "images": ["a.jpg"], "conversations": []}], ensure_ascii=False),
        encoding="utf-8",
    )
    tasks = [
        TranslationTask(
            id="old",
            sample=SourceSample("old", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["a.jpg"],
        ),
        TranslationTask(
            id="new",
            sample=SourceSample("new", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["b.jpg"],
        ),
    ]
    calls = []

    def translate(task):
        calls.append(task.id)
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    stats = run_translation_job(tasks, output_jsonl, output_json, done_path, failed_path, translate, resume=True)

    assert calls == ["new"]
    assert stats["skipped"] == 1
    assert stats["written"] == 1
    final = json.loads(output_json.read_text(encoding="utf-8"))
    assert {record["id"] for record in final} == {"old", "new"}


def test_run_translation_job_prunes_failed_log(tmp_path):
    # A previously-failed id that now succeeds must be removed from the failed
    # log, and re-failing the same id must not append a duplicate line.
    output_jsonl = tmp_path / "out.jsonl"
    output_json = tmp_path / "out.json"
    done_path = tmp_path / "out.done.jsonl"
    failed_path = tmp_path / "out.failed.jsonl"
    failed_path.write_text(
        '{"id": "recovers", "error": "old"}\n{"id": "still_bad", "error": "old"}\n',
        encoding="utf-8",
    )
    tasks = [
        TranslationTask(
            id="recovers",
            sample=SourceSample("recovers", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["a.jpg"],
        ),
        TranslationTask(
            id="still_bad",
            sample=SourceSample("still_bad", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]),
            image_paths=["b.jpg"],
        ),
    ]

    def translate(task):
        if task.id == "still_bad":
            raise RuntimeError("model timeout")
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    run_translation_job(tasks, output_jsonl, output_json, done_path, failed_path, translate, resume=True)

    failed_records = [json.loads(line) for line in failed_path.read_text(encoding="utf-8").splitlines() if line]
    assert [record["id"] for record in failed_records] == ["still_bad"]
    assert failed_records[0]["error"] == "model timeout"


def test_run_translation_job_with_backend_pool_writes_success_and_failure(tmp_path):
    class FakePool:
        def map_unordered(self, tasks, handler):
            for task in tasks:
                if task.id == "bad":
                    yield type(
                        "Result",
                        (),
                        {"ok": False, "item": task, "error": "backend failed", "backend_name": "gpu0"},
                    )()
                else:
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

    output_jsonl = tmp_path / "out.jsonl"
    output_json = tmp_path / "out.json"
    done_path = tmp_path / "out.done.jsonl"
    failed_path = tmp_path / "out.failed.jsonl"
    tasks = [
        TranslationTask("good", SourceSample("good", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["a.jpg"]),
        TranslationTask("bad", SourceSample("bad", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["b.jpg"]),
    ]

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    stats = run_translation_job_with_backend_pool(
        tasks,
        output_jsonl,
        output_json,
        done_path,
        failed_path,
        FakePool(),
        handler,
        resume=False,
    )

    assert stats == {"processed": 2, "written": 1, "failed": 1, "skipped": 0}
    assert json.loads(output_json.read_text(encoding="utf-8")) == [
        {"id": "good", "images": ["a.jpg"], "conversations": []}
    ]
    failed = json.loads(failed_path.read_text(encoding="utf-8").strip())
    assert failed["id"] == "bad"
    assert failed["backend"] == "gpu0"


def test_run_translation_job_with_backend_pool_passes_stream_to_pool(tmp_path):
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

    pool = FakePool()
    tasks = [
        TranslationTask(str(index), SourceSample(str(index), image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["a.jpg"])
        for index in range(3)
    ]

    def handler(task, client, timeout):
        return {"id": task.id, "images": task.image_paths, "conversations": []}

    run_translation_job_with_backend_pool(
        iter(tasks),
        tmp_path / "out.jsonl",
        tmp_path / "out.json",
        tmp_path / "out.done.jsonl",
        tmp_path / "out.failed.jsonl",
        pool,
        handler,
        resume=False,
    )

    assert pool.received_list is False


def test_run_translation_job_with_backend_pool_updates_progress_for_written_failed_and_skipped(tmp_path):
    class FakeProgress:
        def __init__(self):
            self.updates = 0
            self.postfixes = []

        def update(self, amount):
            self.updates += amount

        def set_postfix(self, **kwargs):
            self.postfixes.append(kwargs)

    class FakePool:
        def map_unordered(self, tasks, handler):
            for task in tasks:
                yield type(
                    "Result",
                    (),
                    {
                        "ok": task.id != "bad",
                        "item": task,
                        "value": {"id": task.id, "images": task.image_paths, "conversations": []},
                        "error": "failed",
                        "backend_name": "gpu0",
                    },
                )()

    progress = FakeProgress()
    done_path = tmp_path / "out.done.jsonl"
    done_path.write_text('{"id":"skip"}\n', encoding="utf-8")
    tasks = [
        TranslationTask("skip", SourceSample("skip", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["a.jpg"]),
        TranslationTask("ok", SourceSample("ok", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["b.jpg"]),
        TranslationTask("bad", SourceSample("bad", image_bytes=b"image", turns=[SourceTurn("human", "Q?")]), ["c.jpg"]),
    ]

    stats = run_translation_job_with_backend_pool(
        tasks,
        tmp_path / "out.jsonl",
        tmp_path / "out.json",
        done_path,
        tmp_path / "out.failed.jsonl",
        FakePool(),
        lambda task, client, timeout: {},
        resume=True,
        progress=progress,
    )

    assert stats == {"processed": 2, "written": 1, "failed": 1, "skipped": 1}
    assert progress.updates == 3
    assert progress.postfixes[-1] == {"processed": 2, "written": 1, "failed": 1, "skipped": 1}
