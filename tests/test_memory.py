from lao.memory import MemoryGovernor, kv_cache_gb


def gov(budget=96, free=100):
    return MemoryGovernor(budget, safety_factor=1.0, min_free_system_gb=4, probe=lambda: (128, free))


def test_rejects_request_larger_than_budget():
    d = gov().plan({"a": 100}, 0)
    assert not d.ok and "予算" in d.reason


def test_evicts_least_recently_used_idle_component():
    g = gov()
    g.mark_loaded("old", 40)
    g.mark_loaded("new", 40)
    d = g.plan({"x": 30}, 0)
    assert d.ok and d.evict == ["old"]


def test_does_not_evict_pinned_or_reserved_space():
    g = gov()
    g.mark_loaded("a", 60)
    g.reserve("job", 30)
    d = g.plan({"b": 20}, 0, pinned={"a"})
    assert not d.ok and "待機" in d.reason


def test_checks_os_free_memory():
    g = gov(free=10)
    d = g.plan({"a": 20}, 0)
    assert not d.ok and "他のアプリ" in d.reason


def test_safety_factor_applied():
    g = MemoryGovernor(96, safety_factor=1.2, min_free_system_gb=0, probe=lambda: (128, 128))
    assert not g.plan({"a": 85}, 0).ok  # 85 * 1.2 > 96
    assert g.plan({"a": 75}, 0).ok


def test_kv_cache():
    assert kv_cache_gb(2 * 36 * 8 * 64 * 2, 32768) == 2 * 36 * 8 * 64 * 2 * 32768 / 1024**3
    assert kv_cache_gb(None, 1000) == 0
