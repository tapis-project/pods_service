"""
Unit tests for traffic_utils — parsing Traefik JSON access logs into pod traffic records.

Covers the router-name parsing (site/tenant/pod_id, with the @entrypoint/@file suffix
stripping), timestamp parsing, and the full entry->record conversion including duration
unit conversion, source-IP port stripping, and the tapis-user / gate-code identity logic.
"""
import sys
import json
from datetime import datetime

sys.path.append('/home/tapis/service')

import traffic_utils as tu


# ---- router name -> pod_id -----------------------------------------------

def test_extract_pod_id_plain():
    assert tu.extract_pod_id_from_router("pods-tacc-tacc-mypod@file") == "mypod"

def test_extract_pod_id_strips_entrypoint_suffix():
    # Traefik appends @{entrypoint} before @file — both must be stripped.
    assert tu.extract_pod_id_from_router("pods-tacc-tacc-mypod@http@file") == "mypod"
    assert tu.extract_pod_id_from_router("pods-tacc-tacc-mypod@websecure@file") == "mypod"

def test_extract_pod_id_allows_dashes_in_pod_id():
    assert tu.extract_pod_id_from_router("pods-tacc-dev-my-pod-with-dashes@web@file") == "my-pod-with-dashes"

def test_extract_pod_id_no_suffix():
    assert tu.extract_pod_id_from_router("pods-tacc-tacc-barepod") == "barepod"

def test_extract_pod_id_non_router_returns_none():
    assert tu.extract_pod_id_from_router("not-a-pods-router") is None
    assert tu.extract_pod_id_from_router("") is None
    assert tu.extract_pod_id_from_router(None) is None


# ---- router name -> (tenant, site) ---------------------------------------

def test_extract_tenant_site():
    # convention is pods-{site}-{tenant}-{pod_id}
    tenant, site = tu.extract_tenant_site_from_router("pods-tacc-dev-mypod@file")
    assert (tenant, site) == ("dev", "tacc")

def test_extract_tenant_site_non_match():
    assert tu.extract_tenant_site_from_router("garbage") == (None, None)


# ---- log line parsing ----------------------------------------------------

def test_parse_access_logs_skips_blank_and_unparseable_and_incomplete():
    good = json.dumps({"time": "2024-01-01T00:00:00Z", "RouterName": "pods-tacc-tacc-p@file"})
    missing_router = json.dumps({"time": "2024-01-01T00:00:00Z"})
    missing_time = json.dumps({"RouterName": "pods-tacc-tacc-p@file"})
    raw = "\n".join(["", good, "{not json", missing_router, missing_time, "  "])
    entries = tu.parse_traefik_access_logs(raw)
    assert len(entries) == 1
    assert entries[0]["RouterName"] == "pods-tacc-tacc-p@file"


# ---- timestamp -----------------------------------------------------------

def test_parse_ts_valid_z_suffix_is_naive_utc():
    dt = tu.parse_ts("2024-06-01T12:30:00Z")
    assert isinstance(dt, datetime)
    assert dt.tzinfo is None            # normalized to naive UTC
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2024, 6, 1, 12, 30)

def test_parse_ts_invalid_returns_none():
    assert tu.parse_ts("not-a-timestamp") is None
    assert tu.parse_ts("") is None


# ---- full entry -> record ------------------------------------------------

def _entry(**over):
    base = {
        "time": "2024-06-01T12:00:00Z",
        "RouterName": "pods-tacc-dev-mypod@http@file",
        "RequestMethod": "GET",
        "RequestPath": "/api/thing?x=1",
        "OriginStatus": 200,
        "Duration": 5_000_000,          # 5 ms in nanoseconds
        "ClientHost": "10.0.0.5:54321",
        "entryPointName": "websecure",
    }
    base.update(over)
    return base

def test_entry_to_record_full():
    rec = tu.entry_to_traffic_record(_entry(**{"request_X-Tapis-User": "alice"}))
    assert rec["pod_id"] == "mypod"
    assert rec["tenant_id"] == "dev" and rec["site_id"] == "tacc"
    assert rec["method"] == "GET" and rec["status_code"] == 200
    assert rec["duration_ms"] == 5.0          # ns -> ms
    assert rec["source_ip"] == "10.0.0.5"     # port stripped
    assert rec["username"] == "alice"
    assert rec["raw_headers"] is None         # user present -> no raw headers

def test_entry_to_record_gate_code_identity():
    # No tapis user, but access-gate stamps a gate code -> "gate:<label>" username.
    rec = tu.entry_to_traffic_record(_entry(**{"request_X-Tapis-Gate-Code": "betalist"}))
    assert rec["username"] == "gate:betalist"

def test_entry_to_record_anon_collects_raw_headers_but_not_token():
    rec = tu.entry_to_traffic_record(_entry(**{
        "request_X-Custom": "v",
        "request_X-Tapis-Token": "secretshouldnotappear",
    }))
    assert rec["username"] is None
    assert rec["raw_headers"] == {"X-Custom": "v"}   # token excluded


def test_entry_to_record_redacts_sensitive_query_param_in_path():
    # Traefik's RequestPath includes the query; the access gate shares link
    # secrets as ?access=<secret>. Non-sensitive params stay, the secret is masked.
    rec = tu.entry_to_traffic_record(_entry(RequestPath="/gate/redeem?access=SUPERSECRET&page=2"))
    assert rec["path"] == "/gate/redeem?access=REDACTED&page=2"


def test_entry_to_record_anon_omits_credential_headers():
    rec = tu.entry_to_traffic_record(_entry(**{
        "request_User-Agent": "curl/8",
        "request_Cookie": "pods_gate_mypod=LIVESESSIONSECRET",
        "request_Authorization": "Bearer shouldnotappear",
    }))
    assert rec["username"] is None
    # Session cookie + bearer excluded from persisted headers; benign header kept.
    assert rec["raw_headers"] == {"User-Agent": "curl/8"}


def test_entry_to_record_none_when_router_not_a_pod():
    assert tu.entry_to_traffic_record(_entry(RouterName="dashboard@internal")) is None

def test_entry_to_record_none_when_timestamp_bad():
    assert tu.entry_to_traffic_record(_entry(time="garbage")) is None
