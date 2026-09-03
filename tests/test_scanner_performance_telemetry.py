from aitos.forensics.scanner_performance_telemetry import _cgroup_stats


def test_cgroup_stats_is_non_fatal():
    stats = _cgroup_stats()
    assert stats is None or isinstance(stats, dict)


def test_cgroup_stats_contains_integer_counters_when_available():
    stats = _cgroup_stats()
    if stats is not None:
        assert all(isinstance(key, str) for key in stats)
        assert all(isinstance(value, int) for value in stats.values())
