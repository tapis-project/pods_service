"""
Tests for the `sparse_volume_mounts` conf flag — de-materializing volume_mounts so it
behaves like environment_variables/secret_map (sparse store + per-mount-path merge at
derive) instead of the legacy materialize-at-create + skip-merge-if-modified.

Focus: the DERIVE behavior change in combine_pod_and_template_recursively. Flag OFF
(default) must be byte-for-byte the legacy behavior; flag ON must merge template ∪ pod
per mount-path even when volume_mounts is in modified_fields.

See tapis-ui src/app/Pods/LAYERING_MODEL.md. Run: make test-test_sparse_volume_mounts.py
"""
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.append('/home/tapis/service')

# Reuse the established mocks from the combine test suite.
from test_combine_pod_template import (
    MockPod,
    make_template,
    MockTenantCache,
    MockTemplate,
)

import models_templates_utils as mtu
from models_templates_utils import combine_pod_and_template_recursively


class _ConfProxy:
    """Stand-in for models_templates_utils.conf that overrides only the
    sparse_volume_mounts key and delegates everything else to the real conf.

    We patch the module-level ``conf`` name (a clean setattr/restore) rather than
    ``patch.object(conf, "get")`` — conf.get is a class method, so patch.object
    creates an instance shadow that can't be delattr'd on teardown (AttributeError: get)."""

    def __init__(self, real, enabled):
        self._real = real
        self._enabled = enabled

    def get(self, key, default=None):
        if key == "sparse_volume_mounts":
            return self._enabled
        return self._real.get(key, default)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _derive(pod, *, sparse):
    mock_t_obj = MagicMock()
    mock_t_obj.tenant_cache = MockTenantCache()
    template = make_template(
        volume_mounts={
            "/data": {"type": "tapisvolume", "source_id": "datavolume", "read_only": False},
            "/config": {"type": "pvc", "source_id": "configvolume", "read_only": False},
        }
    )
    with patch("models_templates_utils.derive_template_info") as mock_derive, \
         patch("models_templates_utils.t", mock_t_obj), \
         patch("models_templates_utils.conf", _ConfProxy(mtu.conf, sparse)):
        mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
        return combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")


# A sparse pod: stores ONLY its own mount, but volume_mounts IS in modified_fields
# (the user touched it). This is the shape a flag-ON create produces.
def _sparse_pod(mounts):
    return MockPod(
        environment_variables={"_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES": "True"},
        volume_mounts=mounts,
        modified_fields=["volume_mounts"],
    )


class TestSparseFlagOn:
    def test_template_mounts_merge_in_despite_modified(self):
        # Flag ON: even with volume_mounts in modified_fields, the template's mounts merge in
        # (pod's own mount kept). This is the whole point — no skip-if-modified.
        result = _sparse_pod({"/mine": {"type": "tapisvolume", "source_id": "myvol"}})
        result = _derive(result, sparse=True)
        assert "/data" in result.volume_mounts      # from template
        assert "/config" in result.volume_mounts     # from template
        assert "/mine" in result.volume_mounts       # the user's sparse override

    def test_pod_wins_per_path(self):
        # User overrides a template path: pod's value wins for that path, others still inherited.
        pod = _sparse_pod({"/data": {"type": "tapisvolume", "source_id": "customdata", "read_only": True}})
        result = _derive(pod, sparse=True)
        assert result.volume_mounts["/data"]["source_id"] == "customdata"
        assert "/config" in result.volume_mounts  # untouched template mount still present

    def test_none_removes_inherited_mount(self):
        # Explicit removal of a template mount via None.
        pod = _sparse_pod({"/config": None})
        result = _derive(pod, sparse=True)
        assert "/config" not in result.volume_mounts  # removed
        assert "/data" in result.volume_mounts         # other template mount kept


class TestLegacyFlagOff:
    def test_modified_skips_template_merge(self):
        # Flag OFF (default): volume_mounts in modified_fields → skip merge, use stored value only.
        # Pins the unchanged legacy behavior (the materialized row is authoritative).
        pod = _sparse_pod({"/mine": {"type": "tapisvolume", "source_id": "myvol"}})
        result = _derive(pod, sparse=False)
        assert "/mine" in result.volume_mounts
        assert "/data" not in result.volume_mounts    # template NOT merged (skipped)
        assert "/config" not in result.volume_mounts

    def test_unmodified_still_merges_under_legacy(self):
        # Flag OFF, NOT modified → template merge runs as before (sanity: we didn't break the
        # non-modified path).
        pod = MockPod(
            environment_variables={"_TAPIS_INTERNAL_USE_TEMPLATE_VOLUMES": "True"},
            volume_mounts={"/mine": {"type": "tapisvolume", "source_id": "myvol"}},
            modified_fields=[],
        )
        result = _derive(pod, sparse=False)
        assert "/data" in result.volume_mounts
        assert "/mine" in result.volume_mounts


# ── Nested / complex volume_mounts under the sparse flag ───────────────────────
# Templates with config_content + multiple mount paths; verify per-path merge keeps
# template config intact, lets the pod override one path's nested config, adds new
# paths, and removes inherited ones — all while flag is ON.

def _derive_complex(pod, template_mounts, *, sparse):
    mock_t_obj = MagicMock()
    mock_t_obj.tenant_cache = MockTenantCache()
    template = make_template(volume_mounts=template_mounts)
    with patch("models_templates_utils.derive_template_info") as mock_derive, \
         patch("models_templates_utils.t", mock_t_obj), \
         patch("models_templates_utils.conf", _ConfProxy(mtu.conf, sparse)):
        mock_derive.return_value = ("template1:latest@2024-01-01", MockTemplate(), template)
        return combine_pod_and_template_recursively(pod, "template1", tenant="dev", site="tacc")


TEMPLATE_MOUNTS = {
    "/data": {"type": "tapisvolume", "source_id": "datavol", "read_only": False},
    "/etc/app.conf": {"type": "ephemeral", "config_content": "verbose=false\nport=8080"},
    "/cache": {"type": "pvc", "source_id": "cachevol"},
}


class TestSparseNestedVolumeMounts:
    def test_template_config_content_preserved_when_pod_adds_a_mount(self):
        # Pod adds an unrelated mount; the template's config_content mount survives untouched.
        pod = _sparse_pod({"/extra": {"type": "tapisvolume", "source_id": "extravol"}})
        result = _derive_complex(pod, TEMPLATE_MOUNTS, sparse=True)
        assert set(result.volume_mounts) == {"/data", "/etc/app.conf", "/cache", "/extra"}
        assert result.volume_mounts["/etc/app.conf"]["config_content"] == "verbose=false\nport=8080"

    def test_pod_overrides_one_paths_nested_config(self):
        # Pod overrides the config mount's content at the SAME path → pod wins for that path,
        # other template mounts untouched.
        pod = _sparse_pod({"/etc/app.conf": {"type": "ephemeral", "config_content": "verbose=true\nport=9090"}})
        result = _derive_complex(pod, TEMPLATE_MOUNTS, sparse=True)
        assert result.volume_mounts["/etc/app.conf"]["config_content"] == "verbose=true\nport=9090"
        assert "/data" in result.volume_mounts and "/cache" in result.volume_mounts

    def test_remove_one_keep_others_add_new(self):
        # Mixed op: remove /cache (None), override /data, add /logs — all in one sparse pod.
        pod = _sparse_pod({
            "/cache": None,
            "/data": {"type": "tapisvolume", "source_id": "customdata", "read_only": True},
            "/logs": {"type": "ephemeral", "config_content": "level=debug"},
        })
        result = _derive_complex(pod, TEMPLATE_MOUNTS, sparse=True)
        assert "/cache" not in result.volume_mounts                       # removed
        assert result.volume_mounts["/data"]["source_id"] == "customdata"  # overridden
        assert result.volume_mounts["/data"]["read_only"] is True
        assert result.volume_mounts["/logs"]["config_content"] == "level=debug"  # added
        assert "/etc/app.conf" in result.volume_mounts                    # inherited untouched

    def test_legacy_off_skips_merge_for_complex_template(self):
        # Flag OFF + modified → stored sparse value only (template's complex mounts NOT merged).
        pod = _sparse_pod({"/logs": {"type": "ephemeral", "config_content": "x=1"}})
        result = _derive_complex(pod, TEMPLATE_MOUNTS, sparse=False)
        assert set(result.volume_mounts) == {"/logs"}
