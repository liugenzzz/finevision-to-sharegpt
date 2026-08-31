import argparse
import json
from pathlib import Path

import finevision_to_sharegpt.cli as cli
from finevision_to_sharegpt.cli import build_parser, main


def _command_names(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("parser has no subcommands")


def test_cli_validate_writes_clean_and_rejects(tmp_path):
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "clean.jsonl"
    rejects_path = tmp_path / "rejects.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "images": ["images/a.jpg"],
                        "conversations": [{"from": "human", "value": "<image>\n问题"}],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "images": ["images/a.jpg", "images/b.jpg"],
                        "conversations": [{"from": "human", "value": "<image>\n问题"}],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "validate",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--rejects",
            str(rejects_path),
        ]
    )

    assert code == 0
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(rejects_path.read_text(encoding="utf-8").splitlines()) == 1


def test_cli_only_exposes_documented_commands():
    assert _command_names(build_parser()) == {
        "translate-json",
        "translate-zips",
        "export-zips",
        "validate",
        "merge",
        "list-datasets",
        "db-init",
        "db-scan",
        "db-export",
        "db-status",
        "db-storage",
    }


def test_cli_help_returns_success(capsys):
    code = main(["--help"])

    captured = capsys.readouterr()
    assert code == 0
    assert "translate-json" in captured.out
    assert "translate-zips" in captured.out
    assert "export-zips" in captured.out
    assert "validate" in captured.out
    assert "merge" in captured.out
    assert "sample" not in captured.out
    assert "mix" not in captured.out


def test_cli_merge_writes_jsonl_json_and_prints_stats(tmp_path, capsys):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text('{"id":"one"}\n', encoding="utf-8")
    second.write_text('{"id":"one"}\n{"id":"two"}\n', encoding="utf-8")

    code = main(
        [
            "merge",
            "--inputs",
            str(first),
            str(second),
            "--output",
            str(output),
        ]
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert code == 0
    assert capsys.readouterr().out.strip() == f"read=3 written=2 duplicates=1 output={output}"
    assert [record["id"] for record in records] == ["one", "two"]
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == records


def test_translate_json_progress_is_enabled_by_default_and_can_be_disabled():
    parser = build_parser()

    default_args = parser.parse_args(["translate-json"])
    disabled_args = parser.parse_args(["translate-json", "--no-progress"])

    assert default_args.progress
    assert not disabled_args.progress


def test_translate_json_arguments_parse():
    parser = build_parser()

    translate_json_args = parser.parse_args(
        [
            "translate-json",
            "--input",
            "/data/en.jsonl",
            "--output",
            "/data/zh.jsonl",
            "--images-root",
            "/data",
            "--concurrency",
            "auto",
            "--min-concurrency",
            "12",
            "--max-concurrency",
            "16",
        ]
    )

    assert translate_json_args.command == "translate-json"
    assert translate_json_args.images_root == "/data"
    assert translate_json_args.concurrency == "auto"
    assert translate_json_args.min_concurrency == 12
    assert translate_json_args.max_concurrency == 16


def test_config_commands_arguments_parse():
    parser = build_parser()

    translate_json_args = parser.parse_args(["translate-json", "--config", "/config/translate_json.json"])
    translate_zips_args = parser.parse_args(["translate-zips", "--config", "/config/translate_zips.json"])
    export_zips_args = parser.parse_args(["export-zips", "--config", "/config/export_zips.json"])

    assert translate_json_args.command == "translate-json"
    assert translate_json_args.config == "/config/translate_json.json"
    assert translate_zips_args.command == "translate-zips"
    assert export_zips_args.command == "export-zips"


def test_cli_translate_json_config_writes_jsonl_and_json(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            return '{"conversations":[{"from":"human","value":"问题？"},{"from":"gpt","value":"答案。"}]}'

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"\xff\xd8\xffimage")
    input_path = tmp_path / "english.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "id": "one",
                "images": ["images/a.jpg"],
                "conversations": [
                    {"from": "human", "value": "<image>\nQuestion?"},
                    {"from": "gpt", "value": "Answer."},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    backend_config = tmp_path / "backend.json"
    backend_config.write_text(
        json.dumps(
            {
                "backends": [
                    {
                        "name": "gpu0",
                        "api_base": "http://model",
                        "model": "qwen",
                        "api_key": "sk",
                        "concurrency": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "translate_json.json"
    output_jsonl = tmp_path / "chinese.jsonl"
    output_json = tmp_path / "chinese.json"
    config.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "output_jsonl": str(output_jsonl),
                "output_json": str(output_json),
                "images_root": str(tmp_path),
                "backend_config": str(backend_config),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    code = main(["translate-json", "--config", str(config)])

    assert code == 0
    records = [json.loads(line) for line in output_jsonl.read_text(encoding="utf-8").splitlines()]
    assert records[0]["id"] == "one"
    assert records[0]["images"] == ["images/a.jpg"]
    assert json.loads(output_json.read_text(encoding="utf-8")) == records


def test_cli_translate_json_config_uses_progress_bar(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            return '{"conversations":[{"from":"human","value":"问题？"}]}'

    class FakeTqdm:
        instances = []

        def __init__(self, iterable=None, total=None, desc=None, unit=None):
            self.iterable = iterable
            self.total = total
            self.desc = desc
            self.unit = unit
            self.updates = 0
            FakeTqdm.instances.append(self)

        def update(self, amount):
            self.updates += amount

        def set_postfix(self, **kwargs):
            self.postfix = kwargs

        def close(self):
            self.closed = True

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    monkeypatch.setattr(cli, "tqdm", FakeTqdm)
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"\xff\xd8\xffimage")
    input_path = tmp_path / "english.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "id": "one",
                "images": ["images/a.jpg"],
                "conversations": [{"from": "human", "value": "<image>\nQuestion?"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    backend_config = tmp_path / "backend.json"
    backend_config.write_text(
        json.dumps(
            {
                "backends": [
                    {"name": "gpu0", "api_base": "http://model", "model": "qwen", "api_key": "sk", "concurrency": 1}
                ]
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "translate_json.json"
    config.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "output_jsonl": str(tmp_path / "out.jsonl"),
                "output_json": str(tmp_path / "out.json"),
                "images_root": str(tmp_path),
                "backend_config": str(backend_config),
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    main(["translate-json", "--config", str(config)])

    assert FakeTqdm.instances[0].total == 1
    assert FakeTqdm.instances[0].unit == "record"
    assert FakeTqdm.instances[0].updates == 1


def test_cli_zip_config_commands_pass_tqdm_progress_factory(monkeypatch, tmp_path):
    received = []

    def fake_export_runner(config, progress_factory=None):
        received.append(("export", progress_factory))
        return {"written": 0}

    def fake_translate_runner(config, pool, handler, progress_factory=None, translation_meta=None):
        received.append(("translate", progress_factory))
        return {"written": 0}

    monkeypatch.setattr(cli, "run_export_zips_config", fake_export_runner)
    monkeypatch.setattr(cli, "run_translate_zips_config", fake_translate_runner)
    monkeypatch.setattr(cli, "_make_backend_pool", lambda backend_config: object())

    backend_config = tmp_path / "backend.json"
    backend_config.write_text(
        json.dumps(
            {
                "backends": [
                    {"name": "gpu0", "api_base": "http://model", "model": "qwen", "api_key": "sk", "concurrency": 1}
                ]
            }
        ),
        encoding="utf-8",
    )
    translate_config = tmp_path / "translate_zips.json"
    export_config = tmp_path / "export_zips.json"
    base = {
        "dataset_registry": str(tmp_path / "datasets.json"),
        "datasets": ["*"],
        "output_jsonl": str(tmp_path / "out.jsonl"),
    }
    translate_data = dict(base, backend_config=str(backend_config))
    export_data = dict(base)
    translate_config.write_text(json.dumps(translate_data), encoding="utf-8")
    export_config.write_text(json.dumps(export_data), encoding="utf-8")

    main(["translate-zips", "--config", str(translate_config)])
    main(["export-zips", "--config", str(export_config)])

    assert received == [("translate", cli.tqdm), ("export", cli.tqdm)]


def test_container_internal_scripts_call_config_commands():
    scripts = {
        "scripts/in_translate_json.sh": "translate-json",
        "scripts/in_translate_zips.sh": "translate-zips",
        "scripts/in_export_zips.sh": "export-zips",
        "scripts/in_db_init.sh": "db-init",
        "scripts/in_db_scan.sh": "db-scan",
        "scripts/in_db_export.sh": "db-export",
        "scripts/in_db_status.sh": "db-status",
    }

    for script_path, command in scripts.items():
        text = Path(script_path).read_text(encoding="utf-8")
        assert command in text
        assert "--config" in text


def test_legacy_external_docker_wrapper_scripts_are_removed():
    legacy_scripts = [
        "scripts/run_translate_json.sh",
        "scripts/run_translate_zips.sh",
        "scripts/run_translate.sh",
        "scripts/run_sample.sh",
    ]

    for script_path in legacy_scripts:
        assert not Path(script_path).exists()


def test_cli_translate_json_translates_existing_sharegpt(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            return '{"conversations":[{"from":"human","value":"显示了什么？"},{"from":"gpt","value":"一张图表。"}]}'

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "a.jpg").write_bytes(b"\xff\xd8\xffimage")
    input_path = tmp_path / "english.jsonl"
    output_path = tmp_path / "chinese.json"
    input_path.write_text(
        json.dumps(
            {
                "id": "one",
                "images": ["images/a.jpg"],
                "conversations": [
                    {"from": "human", "value": "<image>\nWhat is shown?"},
                    {"from": "gpt", "value": "A chart."},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "translate-json",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--images-root",
            str(tmp_path),
            "--no-progress",
        ]
    )

    records = json.loads(output_path.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    record = records[0]
    assert code == 0
    assert record["id"] == "one"
    assert record["conversations"] == [
        {"from": "human", "value": "<image>\n显示了什么？"},
        {"from": "gpt", "value": "一张图表。"},
    ]


def test_cli_translate_json_writes_json_array_from_jsonl_input(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            return '{"conversations":[{"from":"human","value":"问题？"},{"from":"gpt","value":"答案。"}]}'

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "a.jpg").write_bytes(b"\xff\xd8\xffimage")
    input_path = tmp_path / "english.jsonl"
    output_path = tmp_path / "chinese.json"
    input_path.write_text(
        json.dumps(
            {
                "id": "one",
                "images": ["images/a.jpg"],
                "conversations": [
                    {"from": "human", "value": "<image>\nQuestion?"},
                    {"from": "gpt", "value": "Answer."},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "translate-json",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--images-root",
            str(tmp_path),
            "--concurrency",
            "2",
            "--no-progress",
        ]
    )

    records = json.loads(output_path.read_text(encoding="utf-8"))
    assert code == 0
    assert records == [
        {
            "id": "one",
            "images": ["images/a.jpg"],
            "conversations": [
                {"from": "human", "value": "<image>\n问题？"},
                {"from": "gpt", "value": "答案。"},
            ],
        }
    ]


def test_cli_translate_json_legacy_args_support_multi_image_records(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            assert isinstance(image_bytes, list)
            assert len(image_bytes) == 2
            return '{"conversations":[{"from":"human","value":"比较。"},{"from":"gpt","value":"两个图表。"}]}'

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "a.jpg").write_bytes(b"\xff\xd8\xffimage-a")
    (images_dir / "b.jpg").write_bytes(b"\xff\xd8\xffimage-b")
    input_path = tmp_path / "english.jsonl"
    output_path = tmp_path / "chinese.json"
    input_path.write_text(
        json.dumps(
            {
                "id": "multi",
                "images": ["images/a.jpg", "images/b.jpg"],
                "conversations": [
                    {"from": "human", "value": "<image>\n<image>\nCompare."},
                    {"from": "gpt", "value": "Two charts."},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "translate-json",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--images-root",
            str(tmp_path),
            "--no-progress",
        ]
    )

    records = json.loads(output_path.read_text(encoding="utf-8"))
    assert code == 0
    assert records[0]["images"] == ["images/a.jpg", "images/b.jpg"]
    assert records[0]["conversations"][0]["value"] == "<image>\n<image>\n比较。"


def test_cli_translate_json_resume_skips_completed_records(monkeypatch, tmp_path):
    class FakeClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def chat(self, prompt, image_bytes, timeout=120):
            FakeClient.calls += 1
            return '{"conversations":[{"from":"human","value":"第二个问题？"},{"from":"gpt","value":"第二个答案。"}]}'

    monkeypatch.setattr(cli, "QwenClient", FakeClient)
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "a.jpg").write_bytes(b"\xff\xd8\xffimage-a")
    (images_dir / "b.jpg").write_bytes(b"\xff\xd8\xffimage-b")
    input_path = tmp_path / "english.jsonl"
    output_path = tmp_path / "chinese.json"
    done_path = output_path.with_suffix(output_path.suffix + ".done.jsonl")
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "one",
                        "images": ["images/a.jpg"],
                        "conversations": [
                            {"from": "human", "value": "<image>\nFirst question?"},
                            {"from": "gpt", "value": "First answer."},
                        ],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": "two",
                        "images": ["images/b.jpg"],
                        "conversations": [
                            {"from": "human", "value": "<image>\nSecond question?"},
                            {"from": "gpt", "value": "Second answer."},
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    done_path.write_text(
        json.dumps(
            {
                "id": "one",
                "images": ["images/a.jpg"],
                "conversations": [
                    {"from": "human", "value": "<image>\n第一个问题？"},
                    {"from": "gpt", "value": "第一个答案。"},
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "translate-json",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--images-root",
            str(tmp_path),
            "--concurrency",
            "2",
            "--resume",
            "--no-progress",
        ]
    )

    records = json.loads(output_path.read_text(encoding="utf-8"))
    assert code == 0
    assert FakeClient.calls == 1
    assert [record["id"] for record in records] == ["one", "two"]
    assert len(done_path.read_text(encoding="utf-8").splitlines()) == 2
