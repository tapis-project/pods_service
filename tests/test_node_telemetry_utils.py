"""
Pure unit tests for node_telemetry_utils (Phase 3 log ingest + metrics history).

No service imports, no DB, no cluster — runnable with pytest OR the bare
mini-runner (python3 -c "importlib exec + call test_*") used when the local
pytest shim is broken. Keep every test dependency-free and fixture-free.
"""
import gzip
import json
import sys
import zlib
from datetime import datetime, timedelta

sys.path.append('/home/tapis/service')
sys.path.append('service')

from node_telemetry_utils import (
    decode_payload,
    parse_json_payload,
    parse_ts,
    normalize_log_entries,
    normalize_metric_samples,
    clamp_window_step,
    downsample_samples,
    sanitize_bench_settings,
    supported_encodings,
    METRIC_FIELDS,
    BENCH_DEFAULT_ENCODINGS,
    BENCH_DEFAULT_CORPORA,
)

NOW = datetime(2026, 7, 30, 12, 0, 0)


# ── decode_payload ───────────────────────────────────────────────────────────

def test_decode_identity_passthrough():
    assert decode_payload(b'{"a":1}', None, 100) == b'{"a":1}'
    assert decode_payload(b'{"a":1}', "identity", 100) == b'{"a":1}'
    assert decode_payload(b'{"a":1}', "", 100) == b'{"a":1}'


def test_decode_identity_over_limit_rejected():
    try:
        decode_payload(b"x" * 11, "identity", 10)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "limit" in str(e)


def test_decode_gzip_roundtrip():
    raw = json.dumps({"entries": [{"line": "hello"}]}).encode()
    assert decode_payload(gzip.compress(raw), "gzip", 4096) == raw
    # zlib-wrapped also accepted (wbits=47 auto-detects)
    assert decode_payload(zlib.compress(raw), "GZIP", 4096) == raw


def test_decode_gzip_bomb_rejected():
    bomb = gzip.compress(b"A" * 100_000)   # tiny compressed, big decompressed
    try:
        decode_payload(bomb, "gzip", 1024)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "limit" in str(e)


def test_decode_gzip_garbage_rejected():
    try:
        decode_payload(b"not gzip at all", "gzip", 1024)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "gzip" in str(e)


def test_decode_unknown_encoding_rejected():
    try:
        decode_payload(b"x", "br", 1024)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unsupported" in str(e)


def test_supported_encodings_baseline():
    encs = supported_encodings()
    assert "identity" in encs and "gzip" in encs   # zstd optional by install


# ── parse_json_payload / parse_ts ────────────────────────────────────────────

def test_parse_json_payload_rejects_non_object():
    try:
        parse_json_payload(b'[1,2]')
        assert False, "expected ValueError"
    except ValueError as e:
        assert "object" in str(e)
    try:
        parse_json_payload(b'\xff\xfe')
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_parse_ts_forms():
    epoch = datetime(2026, 7, 30, 12, 0, 0)
    unix = 1785412800.0  # 2026-07-30T12:00:00Z
    assert parse_ts(unix) == epoch
    assert parse_ts(int(unix)) == epoch
    assert parse_ts(str(unix)) == epoch                       # numeric string
    assert parse_ts("2026-07-30T12:00:00Z") == epoch          # ISO + Z
    assert parse_ts("2026-07-30T14:00:00+02:00") == epoch     # tz-aware → UTC
    assert parse_ts("2026-07-30T12:00:00") == epoch           # naive
    assert parse_ts(True) is None
    assert parse_ts("garbage") is None
    assert parse_ts(None) is None
    assert parse_ts(1e30) is None                             # overflow


# ── normalize_log_entries ────────────────────────────────────────────────────

def test_normalize_log_entries_happy_path():
    rows, dropped, truncated = normalize_log_entries(
        [{"source": "web", "ts": 1785412800, "line": "GET / 200"},
         {"line": "agent self line"}],
        NOW, max_batch=10, max_line_chars=100)
    assert dropped == 0 and truncated == 0
    assert rows[0]["source"] == "web"
    assert rows[0]["ts"] == datetime(2026, 7, 30, 12, 0, 0)
    assert rows[1]["source"] == "agent"       # default source
    assert rows[1]["ts"] == NOW               # missing ts → receipt time


def test_normalize_log_entries_drops_and_truncates():
    rows, dropped, truncated = normalize_log_entries(
        ["not a dict", {"nope": 1}, {"line": ""}, {"line": "x" * 50},
         {"line": "ok"}, {"line": "over batch cap"}],
        NOW, max_batch=2, max_line_chars=10)
    assert len(rows) == 2
    assert rows[0]["line"] == "x" * 10        # truncated
    assert truncated == 1
    assert dropped == 4                       # 3 malformed + 1 over cap


def test_normalize_log_entries_bad_ts_falls_back():
    rows, _, _ = normalize_log_entries(
        [{"line": "x", "ts": "not a time"}], NOW, 10, 100)
    assert rows[0]["ts"] == NOW


def test_normalize_log_entries_rejects_non_list():
    try:
        normalize_log_entries({"line": "x"}, NOW, 10, 100)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_normalize_log_entries_source_clamped():
    rows, _, _ = normalize_log_entries(
        [{"source": "s" * 300, "line": "x"}], NOW, 10, 100)
    assert len(rows[0]["source"]) == 128


# ── normalize_metric_samples ─────────────────────────────────────────────────

def test_normalize_metric_samples_happy_path():
    rows, dropped = normalize_metric_samples(
        [{"ts": 1785412800, "load1": 0.5, "cpu_count": 8,
          "mem_used_bytes": 1024, "mem_total_bytes": 2048,
          "root_disk_pct": 41.2, "docker_running": 3, "docker_total": 5,
          "extra_ignored": "yes"}],
        NOW, max_batch=10)
    assert dropped == 0
    row = rows[0]
    assert row["ts"] == datetime(2026, 7, 30, 12, 0, 0)
    assert row["load1"] == 0.5 and row["cpu_count"] == 8
    assert row["k8s_running"] is None         # absent field → None column
    assert "extra_ignored" not in row


def test_normalize_metric_samples_requires_ts_and_a_value():
    rows, dropped = normalize_metric_samples(
        [{"load1": 1.0},                      # no ts
         {"ts": "garbage", "load1": 1.0},     # bad ts
         {"ts": 1785412800},                  # ts but zero values
         {"ts": 1785412800, "load1": True},   # bool is not a number
         "not a dict"],
        NOW, max_batch=10)
    assert rows == [] and dropped == 5


def test_normalize_metric_samples_far_future_dropped():
    rows, dropped = normalize_metric_samples(
        [{"ts": 1785412800 + 40 * 86400, "load1": 1.0}], NOW, 10)
    assert rows == [] and dropped == 1


def test_normalize_metric_samples_batch_cap():
    samples = [{"ts": 1785412800 + i, "load1": 1.0} for i in range(5)]
    rows, dropped = normalize_metric_samples(samples, NOW, max_batch=3)
    assert len(rows) == 3 and dropped == 2


# ── clamp_window_step / downsample_samples ───────────────────────────────────

def test_clamp_window_step():
    assert clamp_window_step(3600, 60) == (3600, 60)
    assert clamp_window_step(10, 60) == (600, 60)                 # window floor
    assert clamp_window_step(100 * 86400, 3600) == (30 * 86400, 5184)  # cap + step raise...
    w, s = clamp_window_step(30 * 86400, 30)
    assert w // s <= 500                                          # point cap holds
    assert clamp_window_step(3600, 1)[1] == 30                    # step floor


def test_downsample_means_and_buckets():
    start = 1785412800.0
    rows = [
        {"ts": datetime(2026, 7, 30, 12, 0, 10), "load1": 1.0, "mem_used_bytes": 100},
        {"ts": datetime(2026, 7, 30, 12, 0, 50), "load1": 3.0, "mem_used_bytes": 300},
        {"ts": datetime(2026, 7, 30, 12, 5, 0), "load1": 5.0},   # later bucket
    ]
    series = downsample_samples(rows, start, window_s=600, step_s=60)
    assert series["load1"][0] == [start, 2.0]                     # mean of bucket 0
    assert series["load1"][1] == [start + 300, 5.0]
    assert series["mem_used_bytes"] == [[start, 200.0]]
    # empty buckets are OMITTED — gap between the two points is preserved
    assert len(series["load1"]) == 2


def test_downsample_out_of_window_ignored():
    start = 1785412800.0
    rows = [
        {"ts": datetime(2026, 7, 30, 11, 0, 0), "load1": 9.0},   # before window
        {"ts": datetime(2026, 7, 30, 13, 0, 0), "load1": 9.0},   # after window
    ]
    series = downsample_samples(rows, start, window_s=600, step_s=60)
    assert series["load1"] == []


def test_downsample_all_fields_present():
    series = downsample_samples([], 0.0, 600, 60)
    for f in METRIC_FIELDS:
        assert series[f] == []


# ── sanitize_bench_settings ──────────────────────────────────────────────────

def test_bench_settings_defaults():
    s = sanitize_bench_settings({})
    assert s["encodings"] == BENCH_DEFAULT_ENCODINGS
    assert s["corpora"] == BENCH_DEFAULT_CORPORA
    assert s["probe_count"] == 10
    assert s["dry_run"] is True                    # storing is the deliberate choice
    s2 = sanitize_bench_settings("not a dict")
    assert s2["dry_run"] is True


def test_bench_settings_clamps_and_filters():
    s = sanitize_bench_settings({
        "encodings": ["gzip:9", "brotli", "identity"],   # brotli dropped
        "corpora": ["entropy", "malware"],               # malware dropped
        "line_bytes": [1, 512, 999999, 80, 80],          # out-of-range dropped, deduped
        "line_counts": [5, 100, 50000],
        "probe_count": 500,
        "dry_run": False,
    })
    assert s["encodings"] == ["gzip:9", "identity"]
    assert s["corpora"] == ["entropy"]
    assert s["line_bytes"] == [80, 512]
    assert s["line_counts"] == [100]
    assert s["probe_count"] == 50
    assert s["dry_run"] is False


def test_bench_settings_empty_selections_fall_back():
    s = sanitize_bench_settings({"encodings": ["nope"], "line_bytes": [1]})
    assert s["encodings"] == BENCH_DEFAULT_ENCODINGS
    assert len(s["line_bytes"]) == 3
