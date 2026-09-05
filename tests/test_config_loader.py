import json

from finevision_to_sharegpt.config_loader import (
    load_backend_config,
    load_translate_json_config,
    load_zip_task_config,
)


def test_load_backend_config_reads_fixed_concurrency_backends(tmp_path):
    path = tmp_path / "backend.json"
    path.write_text(
        json.dumps(
            {
                "request_timeout": 90,
                "max_retries": 2,
                "disable_backend_after_failures": 20,
                "backends": [
                    {
                        "name": "gpu0",
                        "api_base": "http://model-a",
                        "model": "Qwen",
                        "api_key": "sk-a",
                        "concurrency": 16,
                        "weight": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_backend_config(path)

    assert config.request_timeout == 90
    assert config.max_retries == 2
    assert config.disable_backend_after_failures == 20
    assert config.backends[0].name == "gpu0"
    assert config.backends[0].concurrency == 16


def test_load_translate_json_config_uses_default_sidecar_paths(tmp_path):
    path = tmp_path / "translate_json.json"
    path.write_text(
        json.dumps(
            {
                "input": "/data/english.jsonl",
                "output_jsonl": "/data/chinese.jsonl",
                "output_json": "/data/chinese.json",
                "images_root": "/data",
                "backend_config": "/config/backend.json",
                "resume": True,
            }
        ),
        encoding="utf-8",
    )

    config = load_translate_json_config(path)

    assert config.done_path.as_posix() == "/data/chinese.done.jsonl"
    assert config.failed_path.as_posix() == "/data/chinese.failed.jsonl"
    assert config.resume is True


def test_load_zip_task_config_derives_sidecar_paths_from_output_jsonl(tmp_path):
    path = tmp_path / "translate_zips.json"
    path.write_text(
        json.dumps(
            {
                "dataset_registry": "/config/datasets.json",
                "output_jsonl": "/output/train.jsonl",
            }
        ),
        encoding="utf-8",
    )

    config = load_zip_task_config(path)

    assert config.output_json.as_posix() == "/output/train.json"
    assert config.images_root.as_posix() == "/output/images"
    assert config.failed_path.as_posix() == "/output/failed.jsonl"
    assert config.rejected_path.as_posix() == "/output/rejected.jsonl"
    assert config.report_path.as_posix() == "/output/report.json"
    assert config.chinese_ratio == 1.0
    assert config.seed == 42
    assert config.limit_per_dataset is None
    assert config.datasets[0].name == "*"


def test_load_zip_task_config_defaults_emit_raw_to_true(tmp_path):
    path = tmp_path / "translate_zips.json"
    path.write_text(
        json.dumps(
            {
                "dataset_registry": "/config/datasets.json",
                "output_jsonl": "/output/train.jsonl",
            }
        ),
        encoding="utf-8",
    )

    config = load_zip_task_config(path)

    assert config.emit_raw is True


def test_load_zip_task_config_parses_emit_raw_false(tmp_path):
    path = tmp_path / "translate_zips.json"
    path.write_text(
        json.dumps(
            {
                "dataset_registry": "/config/datasets.json",
                "output_jsonl": "/output/train.jsonl",
                "emit_raw": False,
            }
        ),
        encoding="utf-8",
    )

    config = load_zip_task_config(path)

    assert config.emit_raw is False


def test_load_zip_task_config_parses_flat_fields_and_dataset_overrides(tmp_path):
    path = tmp_path / "translate_zips.json"
    path.write_text(
        json.dumps(
            {
                "dataset_registry": "/config/datasets.json",
                "datasets": ["okvqa", {"name": "captcha", "chinese_ratio": 0.25, "limit": 5000}],
                "output_jsonl": "/output/train.jsonl",
                "output_json": "/custom/train-array.json",
                "images_root": "/custom/images",
                "failed_path": "/custom/failed.jsonl",
                "rejected_path": "/custom/rejected.jsonl",
                "report_path": "/custom/report.json",
                "chinese_ratio": 0.7,
                "seed": 99,
                "limit_per_dataset": 100,
                "backend_config": "/config/backend.json",
                "resume": True,
            }
        ),
        encoding="utf-8",
    )

    config = load_zip_task_config(path)

    assert config.datasets[0].name == "okvqa"
    assert config.datasets[0].chinese_ratio is None
    assert config.datasets[1].name == "captcha"
    assert config.datasets[1].chinese_ratio == 0.25
    assert config.datasets[1].limit == 5000
    assert config.output_json.as_posix() == "/custom/train-array.json"
    assert config.images_root.as_posix() == "/custom/images"
    assert config.failed_path.as_posix() == "/custom/failed.jsonl"
    assert config.rejected_path.as_posix() == "/custom/rejected.jsonl"
    assert config.report_path.as_posix() == "/custom/report.json"
    assert config.chinese_ratio == 0.7
    assert config.seed == 99
    assert config.limit_per_dataset == 100


def test_backend_api_keys_expand_environment_variables(tmp_path, monkeypatch):
    """真 key 不该进版本库，和 MySQL 密码走同一套 ${VAR} 展开。"""

    import json

    from finevision_to_sharegpt.config_loader import load_backend_config

    monkeypatch.setenv("FV_TEST_BACKEND_KEY", "sk-secret")
    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps(
            {
                "backends": [
                    {
                        "name": "remote",
                        "api_base": "http://x/v1/chat/completions",
                        "model": "m",
                        "api_key": "${FV_TEST_BACKEND_KEY}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_backend_config(path).backends[0].api_key == "sk-secret"


def test_a_missing_backend_key_says_which_config_to_look_in(tmp_path, monkeypatch):
    import json

    import pytest

    from finevision_to_sharegpt.config_loader import load_backend_config

    monkeypatch.delenv("FV_TEST_ABSENT_KEY", raising=False)
    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps(
            {
                "backends": [
                    {
                        "name": "remote",
                        "api_base": "http://x/v1/chat/completions",
                        "model": "m",
                        "api_key": "${FV_TEST_ABSENT_KEY}",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="referenced by backend config"):
        load_backend_config(path)
