"""
Pure helpers for node telemetry (Phase 3: log ingest + metrics history).

ZERO service imports (no g/stores/models) so these are unit-testable outside the
cluster — same pattern as stack_template_utils. Everything stateful (DB writes,
quota pruning, auth) lives in api_nodes.py.

Transport notes:
  * Content-Encoding: identity | gzip | zstd. gzip is the universal baseline —
    the agent is stdlib-only and Python has no stdlib zstd until 3.14
    (compression.zstd), so agents send gzip today and upgrade themselves when
    their interpreter has zstd. The server decodes zstd only when the optional
    `zstandard` package is installed; otherwise the 415-style ValueError tells
    the agent to fall back (which it does automatically).
  * Every decode path enforces max_bytes on the DECOMPRESSED size, so a
    compressed bomb cannot balloon in memory.
"""
import json
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


# ── Decode / parse ───────────────────────────────────────────────────────────

def supported_encodings() -> List[str]:
    """Content-Encodings this server can decode — advertised to agents via the
    checkin endpoints dict so they pick the best one they can produce."""
    encs = ["identity", "gzip"]
    try:
        import zstandard  # noqa: F401
        encs.append("zstd")
    except ImportError:
        pass
    return encs


def decode_payload(body: bytes, content_encoding: Optional[str], max_bytes: int) -> bytes:
    """Decompress a request body per Content-Encoding with a hard cap on the
    decompressed size. Raises ValueError with an agent-actionable message."""
    enc = (content_encoding or "identity").strip().lower()
    if enc in ("", "identity"):
        if len(body) > max_bytes:
            raise ValueError(f"payload exceeds {max_bytes} byte limit")
        return body
    if enc == "gzip":
        d = zlib.decompressobj(wbits=47)  # 47 = auto-accept gzip or zlib headers
        try:
            raw = d.decompress(body, max_bytes + 1)
        except zlib.error as e:
            raise ValueError(f"gzip decode failed: {e}")
        if len(raw) > max_bytes or d.unconsumed_tail:
            raise ValueError(f"decompressed payload exceeds {max_bytes} byte limit")
        return raw
    if enc == "zstd":
        try:
            import zstandard
        except ImportError:
            raise ValueError("zstd not supported by this server; send gzip instead")
        try:
            return zstandard.ZstdDecompressor().decompress(body, max_output_size=max_bytes)
        except zstandard.ZstdError as e:
            raise ValueError(f"zstd decode failed (or exceeds {max_bytes} byte limit): {e}")
    raise ValueError(f"unsupported Content-Encoding '{enc}' (use identity, gzip, or zstd)")


def parse_json_payload(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"body is not valid UTF-8 JSON: {e}")
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    return payload


def parse_ts(value: Any) -> Optional[datetime]:
    """Epoch seconds (int/float) or ISO-8601 string -> naive UTC datetime
    (matching the codebase's utcnow() convention). None when unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        v = value.strip()
        try:
            return parse_ts(float(v))   # "1712345678.5" — epoch as a string
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


# ── Log entry normalization ──────────────────────────────────────────────────

def normalize_log_entries(
    entries: Any,
    now: datetime,
    max_batch: int,
    max_line_chars: int,
    max_source_chars: int = 128,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Validate/clamp a raw `entries` list into insertable rows.

    Returns (rows, dropped, truncated):
      rows      — [{source, ts (datetime), line}], oldest kept first, capped at max_batch
      dropped   — entries discarded (not a dict, no line, or over the batch cap)
      truncated — lines cut to max_line_chars
    Missing/unparseable ts falls back to `now` (server receipt time) so a log
    line is never lost to a bad clock.
    """
    if not isinstance(entries, list):
        raise ValueError("'entries' must be a list")
    rows: List[Dict[str, Any]] = []
    dropped = 0
    truncated = 0
    for entry in entries:
        if len(rows) >= max_batch:
            dropped += 1
            continue
        if not isinstance(entry, dict):
            dropped += 1
            continue
        line = entry.get("line")
        if not isinstance(line, str) or line == "":
            dropped += 1
            continue
        if len(line) > max_line_chars:
            line = line[:max_line_chars]
            truncated += 1
        source = entry.get("source")
        if not isinstance(source, str) or not source:
            source = "agent"
        rows.append({
            "source": source[:max_source_chars],
            "ts": parse_ts(entry.get("ts")) or now,
            "line": line,
        })
    return rows, dropped, truncated


# ── Metrics sample normalization ─────────────────────────────────────────────

# Sample fields the server stores; anything else in a sample is ignored.
# Gauges are floats/ints straight off the agent's metrics-lite pass.
METRIC_FIELDS = (
    "load1", "cpu_count",
    "mem_used_bytes", "mem_total_bytes",
    "root_disk_pct",
    "docker_running", "docker_total",
    "k8s_running", "k8s_total",
)


def normalize_metric_samples(
    samples: Any,
    now: datetime,
    max_batch: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """Validate/clamp a raw metrics_samples list into insertable rows.
    Returns (rows, dropped). A sample must carry a parseable ts (metrics without
    a time axis are meaningless — unlike logs there is no safe fallback, since
    checkin batches can span many minutes offline)."""
    if not isinstance(samples, list):
        raise ValueError("'metrics_samples' must be a list")
    rows: List[Dict[str, Any]] = []
    dropped = 0
    for sample in samples:
        if len(rows) >= max_batch:
            dropped += 1
            continue
        if not isinstance(sample, dict):
            dropped += 1
            continue
        ts = parse_ts(sample.get("ts"))
        if ts is None or ts > now + timedelta(days=1):
            dropped += 1
            continue
        row: Dict[str, Any] = {"ts": ts}
        has_value = False
        for field in METRIC_FIELDS:
            v = sample.get(field)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                row[field] = None
                continue
            row[field] = v
            has_value = True
        if not has_value:
            dropped += 1
            continue
        rows.append(row)
    return rows, dropped


# ── Series downsampling (metrics read path) ──────────────────────────────────

def clamp_window_step(
    window_s: int,
    step_s: int,
    min_window_s: int = 600,
    max_window_s: int = 30 * 86400,
    min_step_s: int = 30,
    max_points: int = 500,
) -> Tuple[int, int]:
    """Same discipline as the pods metrics history endpoint: window clamped,
    step raised until points <= max_points, so a caller can never make the DB
    (or the UI) chew an unbounded series."""
    window_s = max(min_window_s, min(int(window_s), max_window_s))
    step_s = max(min_step_s, int(step_s))
    if window_s // step_s > max_points:
        step_s = window_s // max_points
    return window_s, step_s


def downsample_samples(
    rows: List[Dict[str, Any]],
    start_epoch: float,
    window_s: int,
    step_s: int,
    fields: Tuple[str, ...] = METRIC_FIELDS,
) -> Dict[str, List[List[float]]]:
    """Bucket raw sample rows (each {ts: datetime, <field>: number|None}) into
    per-field [[epoch_ts, mean], ...] series.

    Buckets with NO samples are OMITTED — never interpolated — so consumers can
    render off periods honestly (the UI draws a gap/band instead of a line
    sailing across downtime). Bucket timestamp = bucket start.
    """
    n_buckets = max(1, window_s // step_s)
    sums: Dict[str, Dict[int, List[float]]] = {f: {} for f in fields}
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, datetime):
            continue
        epoch = ts.replace(tzinfo=timezone.utc).timestamp()
        bucket = int((epoch - start_epoch) // step_s)
        if bucket < 0 or bucket >= n_buckets:
            continue
        for f in fields:
            v = row.get(f)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            acc = sums[f].setdefault(bucket, [0.0, 0.0])
            acc[0] += float(v)
            acc[1] += 1.0
    series: Dict[str, List[List[float]]] = {}
    for f in fields:
        pts = []
        for bucket in sorted(sums[f]):
            total, count = sums[f][bucket]
            pts.append([start_epoch + bucket * step_s, round(total / count, 4)])
        series[f] = pts
    return series
