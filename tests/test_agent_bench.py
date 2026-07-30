"""
Unit tests for the agent's bench suite + startup milestones — no network, no
docker; patched by hand so this runs under pytest OR the bare mini-runner.
Skips in-container like the other agent test files.
"""

import io
import json
import os
import sys
import urllib.request

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "agent")
if not os.path.exists(os.path.join(AGENT_DIR, "pods_agent.py")):
    try:
        import pytest
        pytest.skip("agent/ not present (in-container run)", allow_module_level=True)
    except ImportError:
        raise SystemExit("agent/ not present")
sys.path.insert(0, AGENT_DIR)

import pods_agent as agent


def with_patched(obj, name, value):
    class _P:
        def __enter__(self):
            self.old = getattr(obj, name)
            setattr(obj, name, value)
        def __exit__(self, *a):
            setattr(obj, name, self.old)
    return _P()


PARAMS = {
    "encodings": ["identity", "gzip:6"],
    "corpora": ["json", "text"],
    "line_bytes": [80, 512],
    "line_counts": [50],
    "probe_count": 3,
    "dry_run": True,
}
STATE = {"api_base": "https://x/v3/pods", "node_id": "n1"}


# ── milestones ───────────────────────────────────────────────────────────────

def test_milestone_first_occurrence_wins():
    agent.MILESTONES.pop("bench_test_ms", None)
    agent.milestone("bench_test_ms")
    first = agent.MILESTONES["bench_test_ms"]
    agent.milestone("bench_test_ms")
    assert agent.MILESTONES["bench_test_ms"] == first
    agent.MILESTONES.pop("bench_test_ms", None)


def test_status_carries_milestones():
    status = agent.sample_status([], {})
    assert "proc_start" in status["startup_milestones"]


# ── corpus + encoders ────────────────────────────────────────────────────────

def test_synth_corpus_exact_sizes():
    for kind in ("json", "text", "entropy"):
        lines = agent.synth_corpus(kind, 128, 10)
        assert len(lines) == 10
        assert all(len(l) == 128 for l in lines)
    # entropy must not compress like text does
    body_e = agent._entries_body(agent.synth_corpus("entropy", 512, 50))
    body_t = agent._entries_body(agent.synth_corpus("text", 512, 50))
    import gzip as _g
    assert len(_g.compress(body_e)) > len(_g.compress(body_t))


def test_bench_encoders_skips_unsupported_zstd():
    encs = agent.bench_encoders(["identity", "gzip:1", "zstd:3"])
    names = [n for n, _, _ in encs]
    assert "identity" in names and "gzip:1" in names
    try:
        from compression import zstd  # noqa: F401
        assert "zstd:3" in names
    except ImportError:
        assert "zstd:3" not in names
    # roundtrip every encoder
    for _, comp, decomp in encs:
        assert decomp(comp(b"hello world" * 20)) == b"hello world" * 20


# ── sections ─────────────────────────────────────────────────────────────────

def test_bench_compression_rows_have_both_dimensions():
    rows = agent.bench_compression(PARAMS, {})
    assert rows, "expected rows"
    for r in rows:
        # every row: time AND bytes
        assert {"corpus", "encoding", "raw_bytes", "wire_bytes", "ratio",
                "compress_ms", "decompress_ms"} <= set(r)
    gz = [r for r in rows if r["encoding"] == "gzip:6" and r["corpus"].startswith("json")]
    assert gz and all(r["ratio"] > 2 for r in gz)


def test_bench_payload_shapes():
    p = agent.bench_payload([], {})
    for key in ("heartbeat", "heartbeat_plus_inventory", "one_metrics_sample"):
        assert p[key]["json_bytes"] > 0 and p[key]["gzip_bytes"] > 0


def test_bench_ingest_dry_run_url_and_rows():
    seen = {}
    class FakeResp(io.BytesIO):
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen.setdefault("encodings", []).append(req.headers.get("Content-encoding"))
        return FakeResp(json.dumps({"result": {"timings": {"decode_ms": 1.5, "insert_ms": None}}}).encode())
    with with_patched(urllib.request, "urlopen", fake_urlopen):
        rows = agent.bench_ingest(PARAMS, STATE, {})
    assert "dry_run=true" in seen["url"]
    assert any(e == "gzip" for e in seen["encodings"])
    assert all(r.get("server_decode_ms") == 1.5 for r in rows if "error" not in r)
    assert all(r.get("dry_run") is True for r in rows if "error" not in r)


def test_run_bench_suite_fault_isolated():
    def boom(*a, **k):
        raise RuntimeError("section exploded")
    fake_ok = lambda *a, **k: {"ok": True}
    with with_patched(agent, "bench_latency", boom), \
         with_patched(agent, "bench_ingest", fake_ok), \
         with_patched(agent, "bench_clock", fake_ok):
        report = agent.run_bench_suite(PARAMS, STATE, {}, [], {})
    assert "section exploded" in report["latency"]["error"]
    assert report["ingest"] == {"ok": True}          # neighbors survive
    assert report["compression"], "local sections still ran"
    assert report["meta"]["duration_ms"] >= 0
    assert report["meta"]["settings"] == PARAMS
