from __future__ import annotations

import queue
import sys
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

        # 进了队列的每一条都必须被某个 worker 尝试过。这两个计数是本方法唯一
        # 可靠的完成判据——后端被摘光、client_factory 构造就抛、worker 被别的
        # 异常打死，三种情况下线程都会照常放下哨兵、循环照常凑满、生成器照常
        # 返回，区别只在于产出是局部的还是空的。数一数就能把它们全认出来。
        produced = 0
        consumed = 0
        worker_errors: list[BaseException] = []

        def produce() -> None:
            nonlocal produced
            for item in items:
                work_queue.put(item)
                with state_lock:
                    produced += 1
            for _ in range(worker_count):
                work_queue.put(sentinel)

        def worker(backend: BackendSpec) -> None:
            nonlocal consumed
            try:
                client = self.client_factory(backend)
                while True:
                    with state_lock:
                        if disabled.get(backend.name, False):
                            return
                    item = work_queue.get()
                    if item is sentinel:
                        return
                    with state_lock:
                        consumed += 1
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
                                and not disabled[backend.name]
                            ):
                                disabled[backend.name] = True
                                # 摘掉一个后端是运维事件，不是细节：一声不吭地少掉
                                # 四分之一算力，进度条上只表现为"变慢了"。
                                print(
                                    f"[warn] backend {backend.name} disabled after "
                                    f"{failures[backend.name]} consecutive failures; "
                                    f"last error: {result.error}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                    result_queue.put(result)
            except BaseException as exc:  # noqa: BLE001 - 记下来，最后一起报
                with state_lock:
                    worker_errors.append(exc)
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

        with state_lock:
            dead = sorted(name for name, off in disabled.items() if off)
            missed = produced - consumed
            first_error = worker_errors[0] if worker_errors else None
        # 生产者还活着说明它卡在 put 上：worker 全没了，队列再也不会被取空。
        if producer_thread.is_alive() or missed > 0:
            reason = (
                f"every backend was disabled after "
                f"{self.config.disable_backend_after_failures} consecutive failures "
                f"({', '.join(dead)})"
                if dead and len(dead) == len(disabled)
                else f"{len(worker_errors)} worker(s) died: {first_error!r}"
                if first_error is not None
                else "workers stopped before the queue was drained"
            )
            raise RuntimeError(
                f"translation stopped early — {reason}. "
                f"at least {max(missed, 0)} item(s) were never attempted; "
                "the results produced so far are partial"
            )

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
