"""
Unit tests for the fleet-metrics history path (metrics_backend.bulk_range_dict), which
backs GET /pods/metrics/history.

Verifies the range-query clamping (window floor/ceiling, step floor, and step-raised-to-
cap-points so a huge window can't ask the backend for unbounded series) and the
never-raises degrade contract.
"""
import sys

sys.path.append('/home/tapis/service')

import metrics_backend as mb


class RecordingProvider(mb.NoneProvider):
    """Captures the (window_s, step_s) it was asked for; returns an empty series."""
    def __init__(self):
        super().__init__()
        self.calls = []

    def get_bulk_range(self, pattern, window_s, step_s):
        self.calls.append((window_s, step_s))
        return {}


def test_window_clamped_up_to_floor():
    res = mb.bulk_range_dict("pods-.*", window_s=100, step_s=60, provider=RecordingProvider())
    assert res["window_s"] == 300           # 5-min floor

def test_window_clamped_down_to_max():
    res = mb.bulk_range_dict("pods-.*", window_s=10**9, step_s=300, provider=RecordingProvider())
    assert res["window_s"] == mb.RANGE_WINDOW_S_MAX   # never > 24h

def test_step_clamped_up_to_floor():
    res = mb.bulk_range_dict("pods-.*", window_s=3600, step_s=5, provider=RecordingProvider())
    assert res["step_s"] == 30

def test_step_raised_so_points_stay_bounded():
    # max window at the 30s floor would be 2880 points; step must rise to cap at 400.
    res = mb.bulk_range_dict("pods-.*", window_s=mb.RANGE_WINDOW_S_MAX, step_s=30,
                             provider=RecordingProvider())
    assert res["step_s"] == mb.RANGE_WINDOW_S_MAX // mb.RANGE_MAX_POINTS
    assert res["window_s"] // res["step_s"] <= mb.RANGE_MAX_POINTS

def test_reported_window_step_match_what_backend_was_queried_with():
    p = RecordingProvider()
    res = mb.bulk_range_dict("pods-.*", window_s=100, step_s=5, provider=p)
    assert p.calls == [(res["window_s"], res["step_s"])]

def test_never_raises_on_backend_error():
    class BoomProvider(mb.NoneProvider):
        def get_bulk_range(self, *a):
            raise mb.MetricsUnavailable("thanos down")
    res = mb.bulk_range_dict("pods-.*", provider=BoomProvider())
    assert "unavailable" in res and "thanos down" in res["unavailable"]

def test_none_provider_is_unavailable():
    res = mb.bulk_range_dict("pods-.*", provider=mb.NoneProvider())
    assert "unavailable" in res
