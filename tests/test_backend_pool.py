import threading
import time

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
            raise RuntimeError("bad backend")
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
