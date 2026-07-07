"""
Unit tests for api_admin._check_volume_sizes — the admin-health subsystem that reports
freshness of the pods-health-central du sweep from volume_usage_logs evidence.

This is the guard added alongside the fix for the sweep that measured NOTHING (du paths
missing the tenant segment). It must catch: never-measured, stale-sweep, and one-object-
type-fully-unmeasured — without ever raising. DB access is mocked.
"""
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.append('/home/tapis/service')

import api_admin
import models_volumes
import models_snapshots
import models_volume_usage


class FakeConf:
    site_id = "tacc"


class FakeLog:
    def __init__(self, measured_at):
        self.measured_at = measured_at


def _run(volumes=0, snapshots=0, vol_logs=None, snap_logs=None):
    vol_logs = vol_logs or []
    snap_logs = snap_logs or []

    def _get_all_recent(obj_type, tenant, site, limit_per_object=1):
        return vol_logs if obj_type == "volume" else snap_logs

    with patch.object(api_admin, "SITE_TENANT_DICT", {"tacc": ["tacc"]}), \
         patch.object(api_admin, "conf", FakeConf()), \
         patch.object(models_volumes.Volume, "db_get_all",
                      staticmethod(lambda tenant, site: [None] * volumes)), \
         patch.object(models_snapshots.Snapshot, "db_get_all",
                      staticmethod(lambda tenant, site: [None] * snapshots)), \
         patch.object(models_volume_usage.VolumeUsageLog, "get_all_recent",
                      staticmethod(_get_all_recent)):
        return api_admin._check_volume_sizes()


def test_ok_when_nothing_to_measure():
    b = _run(volumes=0, snapshots=0)
    assert b["status"] == "ok"
    assert "No volumes or snapshots" in b["message"]


def test_warning_when_objects_exist_but_never_measured():
    # The exact silent-failure the guard exists for: du sweep found no paths.
    b = _run(volumes=2, snapshots=1, vol_logs=[], snap_logs=[])
    assert b["status"] == "warning"
    assert "NEVER been measured" in b["message"]
    assert b["volumes_total"] == 2 and b["volumes_with_measurements"] == 0


def test_ok_when_sweep_is_fresh():
    fresh = datetime.utcnow() - timedelta(minutes=5)
    b = _run(volumes=1, snapshots=1, vol_logs=[FakeLog(fresh)], snap_logs=[FakeLog(fresh)])
    assert b["status"] == "ok"
    assert "healthy" in b["message"]
    assert b["volumes_with_measurements"] == 1 and b["snapshots_with_measurements"] == 1


def test_warning_when_sweep_is_stale():
    stale = datetime.utcnow() - timedelta(minutes=60)   # > 30-min stale threshold
    b = _run(volumes=1, snapshots=1, vol_logs=[FakeLog(stale)], snap_logs=[FakeLog(stale)])
    assert b["status"] == "warning"
    assert "min old" in b["message"]


def test_warning_when_one_object_type_unmeasured():
    fresh = datetime.utcnow() - timedelta(minutes=2)
    # volumes measured, snapshots exist but never measured -> targeted warning
    b = _run(volumes=1, snapshots=1, vol_logs=[FakeLog(fresh)], snap_logs=[])
    assert b["status"] == "warning"
    assert "NO snapshots have measurements" in b["message"]
