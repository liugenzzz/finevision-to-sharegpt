from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from .config_loader import BackendPoolConfig, BackendSpec


@dataclass(frozen=True)
class BackendResult:
    item: Any
    ok: bool
    value: Any = None
    error: str | None = None
    backend_name: str | None = None


class TranslationBackendPool:
    def __init__(
        self,
        config: BackendPoolConfig,
        client_factory: Callable[[BackendSpec], Any],
    ) -> None:
        self.config = config
        self.client_factory = client_factory

    def map_unordered(
        self,
        items: Iterable[Any],
        handler: Callable[[Any, Any, int], Any],
    ) -> Iterator[BackendResult]:
        worker_count = sum(max(1, backend.concurrency) for backend in self.config.backends)
        work_queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, worker_count * 4))
        result_queue: queue.Queue[BackendResult] = queue.Queue()
        disabled: dict[str, bool] = {backend.name: False for backend in self.config.backends}
        failures: dict[str, int] = {backend.name: 0 for backend in self.config.backends}
        state_lock = threading.Lock()
        sentinel = object()
        result_sentinel = object()

        def produce() -> None:
            for item in items:
                work_queue.put(item)
            for _ in range(worker_count):
                work_queue.put(sentinel)

        def worker(backend: BackendSpec) -> None:
            try:
                client = self.client_factory(backend)
                while True:
                    with state_lock:
                        if disabled.get(backend.name, False):
                            return
                    item = work_queue.get()
                    if item is sentinel:
                        return
                    result = self._run_with_retries(item, backend, client, handler)
                    if result.ok:
                        with state_lock:
                            failures[backend.name] = 0
                    else:
                        with state_lock:
                            failures[backend.name] += 1
                            if (
                                self.config.disable_backend_after_failures > 0
                                and failures[backend.name] >= self.config.disable_backend_after_failures
                            ):
                                disabled[backend.name] = True
                    result_queue.put(result)
            finally:
                result_queue.put(result_sentinel)

        threads: list[threading.Thread] = []
        producer_thread = threading.Thread(target=produce, name="translation-backend-producer", daemon=True)
        producer_thread.start()
        for backend in self.config.backends:
            for index in range(max(1, backend.concurrency)):
                thread = threading.Thread(target=worker, args=(backend,), name=f"{backend.name}-{index}", daemon=True)
                thread.start()
                threads.append(thread)

        finished_workers = 0
        while finished_workers < worker_count:
            result = result_queue.get()
            if result is result_sentinel:
                finished_workers += 1
                continue
            yield result

        producer_thread.join(timeout=1)
        for thread in threads:
            thread.join(timeout=1)

    def _run_with_retries(
        self,
        item: Any,
        backend: BackendSpec,
        client: Any,
        handler: Callable[[Any, Any, int], Any],
    ) -> BackendResult:
        last_error = ""
        attempts = max(0, self.config.max_retries) + 1
        for _attempt in range(attempts):
            try:
                value = handler(item, client, self.config.request_timeout)
                return BackendResult(item=item, ok=True, value=value, backend_name=backend.name)
            except Exception as exc:
                last_error = str(exc)
        return BackendResult(item=item, ok=False, error=last_error, backend_name=backend.name)
