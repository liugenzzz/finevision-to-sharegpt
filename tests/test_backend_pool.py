import threading
import time

import pytest

from finevision_to_sharegpt.backend_pool import TranslationBackendPool
from finevision_to_sharegpt.config_loader import BackendPoolConfig, BackendSpec


def backend_config(**overrides):
    data = {
        "backends": [
            BackendSpec("gpu0", "http://gpu0", "model", "sk", concurrency=2),
        ],
        "request_timeout": 5,
        "max_retries": 2,
        "disable_backend_after_failures": 20,
    }
    data.update(overrides)
    return BackendPoolConfig(**data)


def test_backend_pool_retries_failed_items_before_success():
    attempts = {"count": 0}
    pool = TranslationBackendPool(backend_config(), client_factory=lambda backend: backend.name)

    def handler(item, client, timeout):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary")
        return {"id": item, "client": client}

    results = list(pool.map_unordered(["one"], handler))

    assert attempts["count"] == 3
    assert results[0].ok
    assert results[0].value == {"id": "one", "client": "gpu0"}


def test_backend_pool_returns_failed_result_after_retries_exhausted():
    pool = TranslationBackendPool(backend_config(max_retries=1), client_factory=lambda backend: backend.name)

    def handler(item, client, timeout):
        raise RuntimeError(f"failed {item}")

    results = list(pool.map_unordered(["one"], handler))

    assert len(results) == 1
    assert not results[0].ok
    assert results[0].item == "one"
    assert results[0].error == "failed one"
    assert results[0].backend_name == "gpu0"


def test_backend_pool_disables_backend_after_consecutive_failures():
    config = backend_config(
        backends=[
            BackendSpec("bad", "http://bad", "model", "sk", concurrency=1),
            BackendSpec("good", "http://good", "model", "sk", concurrency=1),
        ],
        max_retries=0,
        disable_backend_after_failures=1,
    )
    pool = TranslationBackendPool(config, client_factory=lambda backend: backend.name)
    calls = []

    def handler(item, client, timeout):
        calls.append((item, client))
        if client == "bad":
            raise RuntimeError("[Errno 111] Connection refused")
        return {"id": item, "client": client}

    results = list(pool.map_unordered(["one", "two", "three"], handler))

    assert any(not result.ok and result.backend_name == "bad" for result in results)
    assert sum(1 for _item, client in calls if client == "bad") == 1
    assert any(result.ok and result.backend_name == "good" for result in results)


def test_backend_pool_respects_per_backend_concurrency():
    config = backend_config(
        backends=[
            BackendSpec("gpu0", "http://gpu0", "model", "sk", concurrency=2),
            BackendSpec("gpu1", "http://gpu1", "model", "sk", concurrency=1),
        ]
    )
    active = {"gpu0": 0, "gpu1": 0}
    max_seen = {"gpu0": 0, "gpu1": 0}
    lock = threading.Lock()
    pool = TranslationBackendPool(config, client_factory=lambda backend: backend.name)

    def handler(item, client, timeout):
        with lock:
            active[client] += 1
            max_seen[client] = max(max_seen[client], active[client])
        time.sleep(0.02)
        with lock:
            active[client] -= 1
        return item

    list(pool.map_unordered(range(8), handler))

    assert max_seen["gpu0"] <= 2
    assert max_seen["gpu1"] <= 1


def test_backend_pool_streams_items_without_consuming_entire_iterable_before_yielding():
    pool = TranslationBackendPool(backend_config(backends=[BackendSpec("gpu0", "http://gpu0", "model", "sk", 1)]), client_factory=lambda backend: backend.name)
    consumed = {"count": 0}

    def values():
        for item in range(50):
            consumed["count"] += 1
            yield item

    def handler(item, client, timeout):
        return item

    iterator = pool.map_unordered(values(), handler)
    first = next(iterator)

    assert first.value == 0
    assert consumed["count"] < 50


# -- 全部后端挂掉 ------------------------------------------------------------


def test_the_last_backend_is_never_disabled():
    """全部不可达时也要把任务跑完（并全部记为失败），而不是停摆。

    摘掉最后一个后端等于让整轮任务原地死亡，而失败的样本本来就会进
    failed.jsonl、之后可以单独重试。停摆比失败贵得多。
    """

    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=1),
                  BackendSpec("b", "http://b", "m", "sk", concurrency=1)],
        request_timeout=1,
        max_retries=0,
        disable_backend_after_failures=2,
    )
    pool = TranslationBackendPool(config, client_factory=lambda backend: object())

    def always_fails(item, client, timeout):
        raise RuntimeError("[Errno 111] Connection refused")

    results = list(pool.map_unordered(range(50), always_fails))

    assert len(results) == 50           # 每一条都被尝试过
    assert not any(result.ok for result in results)


def test_one_dead_backend_does_not_stop_a_run(capsys):
    config = BackendPoolConfig(
        backends=[BackendSpec("dead", "http://d", "m", "sk", concurrency=1),
                  BackendSpec("alive", "http://a", "m", "sk", concurrency=1)],
        request_timeout=1,
        max_retries=0,
        disable_backend_after_failures=2,
    )
    pool = TranslationBackendPool(
        config, client_factory=lambda backend: backend.name)

    def handler(item, client, timeout):
        if client == "dead":
            raise RuntimeError("[Errno 111] Connection refused")
        return item * 2

    results = list(pool.map_unordered(range(20), handler))

    assert any(result.ok for result in results)
    # 摘掉一个后端要说出来，否则少掉一半算力只表现为"变慢了"。
    assert "backend dead disabled" in capsys.readouterr().err


def test_a_worker_that_dies_building_its_client_is_not_silent():
    """构造客户端就抛的情况：worker 立刻死光，一条都没产出。

    这时候没有任何后端被"摘掉"——它们连一次调用都没发出去过——所以只看
    disabled 是发现不了的。判据得是「进了队列的都被尝试过」。
    """

    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=2)],
        request_timeout=1,
        max_retries=0,
    )

    def explode(backend):
        raise ValueError("api_base is nonsense")

    pool = TranslationBackendPool(config, client_factory=explode)

    with pytest.raises(RuntimeError, match="worker\\(s\\) died"):
        list(pool.map_unordered(range(10), lambda *a: None))


def test_a_complete_run_does_not_raise():
    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=4)],
        request_timeout=1,
        max_retries=0,
    )
    pool = TranslationBackendPool(config, client_factory=lambda backend: object())

    results = list(pool.map_unordered(range(200), lambda item, client, timeout: item * 2))

    assert len(results) == 200
    assert all(result.ok for result in results)


def test_the_error_says_how_many_items_were_never_attempted():
    """真正停摆时（worker 构造就崩）要说清楚有多少条从未被尝试过。"""

    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=2)],
        request_timeout=1,
        max_retries=0,
    )

    def explode(backend):
        raise ValueError("api_base is nonsense")

    pool = TranslationBackendPool(config, client_factory=explode)

    with pytest.raises(RuntimeError, match="item\\(s\\) were never attempted"):
        list(pool.map_unordered(range(100), lambda *a: None))


# -- 什么才算"后端坏了" -------------------------------------------------------


def test_timeouts_never_disable_a_backend():
    """高并发下响应慢、或者撞上一串长对话，健康的后端也会连续超时几十次。

    按次数摘后端，摘掉的往往是好的——现场六个后端就是这么全被摘光、
    整轮任务停摆的。
    """

    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=1),
                  BackendSpec("b", "http://b", "m", "sk", concurrency=1)],
        request_timeout=1,
        max_retries=0,
        disable_backend_after_failures=2,
    )
    pool = TranslationBackendPool(config, client_factory=lambda backend: object())

    def always_times_out(item, client, timeout):
        raise RuntimeError("ReadTimeout: timed out waiting for response")

    results = list(pool.map_unordered(range(60), always_times_out))

    assert len(results) == 60
    assert not any(result.backend_fault for result in results)


def test_a_sample_the_model_could_not_translate_is_not_a_backend_fault():
    config = BackendPoolConfig(
        backends=[BackendSpec("a", "http://a", "m", "sk", concurrency=1)],
        request_timeout=1,
        max_retries=0,
        disable_backend_after_failures=2,
    )
    pool = TranslationBackendPool(config, client_factory=lambda backend: object())

    def hard_sample(item, client, timeout):
        raise RuntimeError("not_json -> fallback: 39 turns exceeds the fallback cap of 12")

    results = list(pool.map_unordered(range(30), hard_sample))

    assert len(results) == 30
    assert not any(result.backend_fault for result in results)


def test_unreachable_and_unauthorized_do_count_as_backend_faults():
    from finevision_to_sharegpt.backend_pool import is_backend_fault

    assert is_backend_fault("[Errno 111] Connection refused")
    assert is_backend_fault("HTTP 401 Unauthorized")
    assert is_backend_fault("The model `Qwen3.6-27B` does not exist.")
    assert not is_backend_fault("ReadTimeout")
    assert not is_backend_fault("HTTP 503 Service Unavailable")
    assert not is_backend_fault(None)


def test_duplicate_backend_names_are_rejected_at_load(tmp_path):
    """重名的两个条目会共用失败计数：一个坏了另一个跟着被摘，日志里只有一个名字。"""

    import json

    from finevision_to_sharegpt.config_loader import load_backend_config

    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps(
            {
                "backends": [
                    {"name": "vllm", "api_base": "http://a", "model": "m"},
                    {"name": "vllm", "api_base": "http://b", "model": "m"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="backend names must be unique; vllm"):
        load_backend_config(path)
