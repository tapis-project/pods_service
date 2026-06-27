"""
Tests for derive_pod_for_display (api_pods_podid) — the helper behind
GET /pods/{pod_id}?derived_lite=true (and ?derived=true). It lets the single
get_pod endpoint return template-MERGED values together with modified_fields, so
the Overview UI can make ONE request instead of GET /pods/{id} + GET /pods/{id}/derived.

What's proven (see tapis-ui src/app/Pods/LAYERING_MODEL.md):
  - mode "none"  → the raw stored pod, untouched (no merge, no copy).
  - "lite"/"full" on a TEMPLATED pod → combine_pod_and_template_recursively runs and
    modified_fields survives onto the merged object (so provenance + values in one call).
  - any mode on a TEMPLATE-LESS pod → combine is a no-op (never called); pod as-is.
  - "lite" skips the Password lookup; "full" interpolates legacy <<TAPIS_*>> from it.

Run in-container:  make test-test_get_pod_derived.py
"""
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.append('/home/tapis/service')

import api_pods_podid as mod
from api_pods_podid import derive_pod_for_display


def stored_pod(template="", modified_fields=None, env=None, **extra):
    """Stand-in for a stored Pod row. .dict() feeds PodBaseFull(**...), which we patch
    to a passthrough so the test controls the resulting object."""
    mf = list(modified_fields if modified_fields is not None else [])
    d = dict(
        pod_id="p1",
        tenant_id="dev",
        site_id="tacc",
        template=template,
        modified_fields=mf,
        environment_variables=dict(env or {}),
        **extra,
    )
    ns = SimpleNamespace(**d)
    ns.dict = lambda: dict(d)
    return ns


def _passthrough_podbasefull(**kwargs):
    # PodBaseFull(**pod.dict()) → an inspectable namespace.
    ns = SimpleNamespace(**kwargs)
    if getattr(ns, "environment_variables", None) is None:
        ns.environment_variables = {}
    return ns


class TestNoneMode:
    def test_returns_raw_pod_untouched(self):
        pod = stored_pod(template="postgres:16", modified_fields=["image"])
        with patch.object(mod, "combine_pod_and_template_recursively") as combine:
            out = derive_pod_for_display(pod, "none", tenant="dev", site="tacc")
        assert out is pod  # same object, no copy
        combine.assert_not_called()  # no merge at all


class TestTemplatedLite:
    def test_merges_and_preserves_modified_fields(self):
        pod = stored_pod(template="postgres:16", modified_fields=["image"], env={})
        merged = SimpleNamespace(
            template="postgres:16",
            modified_fields=["image"],  # combine preserves the override ledger
            image="postgres:16",  # template-supplied value now materialized
            environment_variables={"PGDATA": "/data"},
        )
        with patch.object(mod, "PodBaseFull", side_effect=_passthrough_podbasefull), \
             patch.object(mod, "combine_pod_and_template_recursively", return_value=merged) as combine, \
             patch.object(mod, "Password") as Password:
            out = derive_pod_for_display(pod, "lite", tenant="dev", site="tacc")
        combine.assert_called_once()
        assert combine.call_args.args[1] == "postgres:16"  # template ref passed through
        assert out is merged
        assert out.modified_fields == ["image"]  # provenance survives the merge
        assert out.image == "postgres:16"  # merged value present
        # The whole point of "lite": no password/legacy-placeholder work.
        Password.db_get_with_pk.assert_not_called()


class TestTemplatelessAnyMode:
    @pytest.mark.parametrize("mode", ["lite", "full"])
    def test_no_template_is_a_noop_merge(self, mode):
        pod = stored_pod(
            template="", modified_fields=["image", "command"], env={"A": "b"}
        )
        with patch.object(mod, "PodBaseFull", side_effect=_passthrough_podbasefull), \
             patch.object(mod, "combine_pod_and_template_recursively") as combine, \
             patch.object(mod, "Password") as Password:
            Password.db_get_with_pk.return_value = MagicMock(dict=lambda: {})
            out = derive_pod_for_display(pod, mode, tenant="dev", site="tacc")
        combine.assert_not_called()  # nothing to merge against
        assert out.template == ""
        assert out.modified_fields == ["image", "command"]  # ledger intact
        assert out.environment_variables == {"A": "b"}  # values unchanged


class TestTemplatedFull:
    def test_interpolates_legacy_placeholders_from_password_table(self):
        pod = stored_pod(template="t:1", env={})
        merged = SimpleNamespace(
            template="t:1",
            modified_fields=["environment_variables"],
            environment_variables={
                "PW": "<<TAPIS_password>>",
                "SECRET": "<<tapissecret_api_key>>",
                "PLAIN": "x",
            },
        )
        pw_obj = MagicMock()
        pw_obj.dict.return_value = {"password": "s3cr3t", "api_key": "ak-123"}
        with patch.object(mod, "PodBaseFull", side_effect=_passthrough_podbasefull), \
             patch.object(mod, "combine_pod_and_template_recursively", return_value=merged), \
             patch.object(mod, "Password") as Password:
            Password.db_get_with_pk.return_value = pw_obj
            out = derive_pod_for_display(pod, "full", tenant="dev", site="tacc")
        assert out.environment_variables["PW"] == "s3cr3t"  # <<TAPIS_password>> resolved
        assert out.environment_variables["SECRET"] == "ak-123"  # <<tapissecret_*>> resolved
        assert out.environment_variables["PLAIN"] == "x"  # untouched
        Password.db_get_with_pk.assert_called_once()

    def test_lite_does_not_interpolate_placeholders(self):
        # Same merged env, but "lite" leaves the legacy placeholders as-is.
        pod = stored_pod(template="t:1", env={})
        merged = SimpleNamespace(
            template="t:1",
            modified_fields=[],
            environment_variables={"PW": "<<TAPIS_password>>"},
        )
        with patch.object(mod, "PodBaseFull", side_effect=_passthrough_podbasefull), \
             patch.object(mod, "combine_pod_and_template_recursively", return_value=merged), \
             patch.object(mod, "Password") as Password:
            out = derive_pod_for_display(pod, "lite", tenant="dev", site="tacc")
        assert out.environment_variables["PW"] == "<<TAPIS_password>>"  # left untouched
        Password.db_get_with_pk.assert_not_called()
