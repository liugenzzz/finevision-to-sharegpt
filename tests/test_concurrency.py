from finevision_to_sharegpt.concurrency import DynamicLimiter, map_ordered, resolve_concurrency


def test_resolve_concurrency_supports_fixed_and_auto():
    assert resolve_concurrency("1", max_concurrency=8) == 1
    assert resolve_concurrency("4", max_concurrency=8) == 4
    assert resolve_concurrency("auto", min_concurrency=24, max_concurrency=60) == 24
    assert resolve_concurrency("auto", min_concurrency=24, max_concurrency=8) == 8
    assert resolve_concurrency("auto", min_concurrency=0, max_concurrency=0) == 1


def test_map_ordered_preserves_input_order_with_threads():
    result = list(map_ordered(lambda value: value * 2, [3, 1, 2], concurrency=3))

    assert result == [6, 2, 4]


def test_map_ordered_runs_sequentially_when_concurrency_is_one():
    seen = []

    def record(value):
        seen.append(value)
        return value

    result = list(map_ordered(record, [1, 2, 3], concurrency=1))

    assert result == [1, 2, 3]
    assert seen == [1, 2, 3]


def test_dynamic_limiter_increases_after_successes_and_caps_at_max():
    limiter = DynamicLimiter(initial=2, maximum=5, success_threshold=2)

    limiter.record_success()
    assert limiter.current == 2
    limiter.record_success()
    assert limiter.current == 3
    for _ in range(10):
        limiter.record_success()

    assert limiter.current == 5


def test_dynamic_limiter_decreases_on_failure():
    limiter = DynamicLimiter(initial=4, maximum=8, success_threshold=2)

    limiter.record_failure()

    assert limiter.current == 2
