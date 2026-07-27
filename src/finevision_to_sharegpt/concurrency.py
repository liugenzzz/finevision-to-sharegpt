from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar


T = TypeVar("T")
R = TypeVar("R")


def resolve_concurrency(value: str | int, min_concurrency: int = 24, max_concurrency: int = 60) -> int:
    minimum = max(1, int(min_concurrency))
    maximum = max(1, int(max_concurrency))
    if str(value).lower() == "auto":
        return min(minimum, maximum)
    return max(1, int(value))


@dataclass
class DynamicLimiter:
    initial: int
    maximum: int
    success_threshold: int = 8

    def __post_init__(self) -> None:
        self.current = max(1, min(self.initial, self.maximum))
        self.maximum = max(1, self.maximum)
        self._successes = 0

    def record_success(self) -> None:
        self._successes += 1
        if self._successes >= self.success_threshold and self.current < self.maximum:
            self.current += 1
            self._successes = 0

    def record_failure(self) -> None:
        self.current = max(1, self.current // 2)
        self._successes = 0


def map_ordered(func: Callable[[T], R], values: Iterable[T], concurrency: int) -> Iterator[R]:
    if concurrency <= 1:
        for value in values:
            yield func(value)
        return

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        yield from executor.map(func, values)
