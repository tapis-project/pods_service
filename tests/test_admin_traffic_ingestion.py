"""
Unit tests for api_admin._check_traffic_ingestion — the admin-health subsystem that
reports whether Traefik traffic is being ingested into traffic_logs.

Validates the status rollup (ok / warning-stale / warning-empty / error) and that the
per-tenant meta tables (siteadmintable/defaulttables) are skipped. pg_store is mocked
so no real DB is touched.
"""
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.append('/home/tapis/service')

import api_admin


class FakeRow:
    def __init__(self, pod_id, latest_ts, cnt):
        self.pod_id = pod_id
        self.latest_ts = latest_ts
        self.cnt = cnt


class FakeStore:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise = raise_exc

    def run(self, *a, **kw):
        if self._raise:
            raise self._raise
        return self._rows


def _pg_store(real_store):
    # A real tenant store plus the two meta tables that must be skipped.
    return {"tacc": {"tacc": real_store,
                     "siteadmintable": FakeStore(),
                     "defaulttables": FakeStore()}}


def test_ok_when_traffic_is_fresh():
    fresh = datetime.utcnow() - timedelta(seconds=10)
    with patch.object(api_admin, "pg_store", _pg_store(FakeStore(rows=[FakeRow("p1", fresh, 5)]))):
        res = api_admin._check_traffic_ingestion()
    assert res["status"] == "ok"
    assert res["total_rows"] == 5
    assert len(res["per_pod"]) == 1
    assert res["per_pod"][0]["pod_id"] == "p1"


def test_warning_when_newest_row_is_stale():
    stale = datetime.utcnow() - timedelta(seconds=300)  # > 120s threshold
    with patch.object(api_admin, "pg_store", _pg_store(FakeStore(rows=[FakeRow("p1", stale, 3)]))):
        res = api_admin._check_traffic_ingestion()
    assert res["status"] == "warning"
    assert "stall" in res["message"].lower()


def test_warning_when_no_rows():
    with patch.object(api_admin, "pg_store", _pg_store(FakeStore(rows=[]))):
        res = api_admin._check_traffic_ingestion()
    assert res["status"] == "warning"
    assert res["total_rows"] == 0
    assert res["per_pod"] == []


def test_error_on_db_exception():
    with patch.object(api_admin, "pg_store", _pg_store(FakeStore(raise_exc=RuntimeError("db down")))):
        res = api_admin._check_traffic_ingestion()
    assert res["status"] == "error"
    assert "db down" in res["message"]


def test_meta_tables_are_skipped():
    fresh = datetime.utcnow() - timedelta(seconds=5)
    real = FakeStore(rows=[FakeRow("p1", fresh, 1)])
    meta = MagicMock()
    pg = {"tacc": {"tacc": real, "siteadmintable": meta, "defaulttables": meta}}
    with patch.object(api_admin, "pg_store", pg):
        res = api_admin._check_traffic_ingestion()
    assert res["status"] == "ok"
    meta.run.assert_not_called()   # meta tables never queried
