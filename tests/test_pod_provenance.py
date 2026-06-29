"""
Intensive tests for per-field pod provenance — the "what is pod vs what is template"
determination behind GET /pods/{pod_id}/provenance (api_pods_podid.attribute_pod_fields).

The point being proven: provenance is decided by `modified_fields` (the override
ledger), NOT by whether a stored value is non-empty. A pod created from a tag stores
NON-empty defaults (networking, resources) and a MATERIALIZED volume_mounts at
creation — the old raw-vs-derived emptiness heuristic mislabels all of those as
'pod'. These tests pin the correct behavior: untouched template fields read
'template', user overrides read 'pod', everything else 'default'.

Run in-container:  make test-test_pod_provenance.py
"""
import sys
from types import SimpleNamespace

import pytest

sys.path.append('/home/tapis/service')

from api_pods_podid import (
    attribute_pod_fields,
    PROVENANCE_FIELD_DEFAULTS,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def view(**overrides):
    """A pod 'view' (raw / template-only / derived) with every provenance field set
    to its service default, overridable per-test. Mirrors any object combine returns."""
    vals = dict(PROVENANCE_FIELD_DEFAULTS)
    vals.update(overrides)
    return SimpleNamespace(**vals)


# A representative template tag's pod_definition: a postgres-ish template that sets
# image, command, env, a config mount, and non-default networking.
TEMPLATE_VALUES = dict(
    image="postgres:16",
    command=["postgres"],
    environment_variables={"PGDATA": "/var/lib/postgresql/data"},
    volume_mounts={"/etc/postgresql/postgresql.conf": {"type": "ephemeral", "config_content": "max_connections=200"}},
    networking={"default": {"protocol": "tcp", "port": 5432}},
)


def template_only_view():
    """What the template alone yields (combine of a clean pod + template)."""
    return view(**TEMPLATE_VALUES)


def materialized_pod_view(**overrides):
    """A pod's RAW stored row right after create-from-tag: unset fields hold service
    DEFAULTS (image="", networking=default port 5000, env={}), EXCEPT volume_mounts,
    which is materialized from the template at creation. This is the shape that fools
    the emptiness heuristic."""
    vals = dict(
        # image/command/env left at default — NOT materialized into the row
        volume_mounts=TEMPLATE_VALUES["volume_mounts"],  # materialized at creation
    )
    vals.update(overrides)
    return view(**vals)


# ── the core diagnosis: untouched template pod ─────────────────────────────────

class TestUntouchedTemplatePod:
    """A pod created from a tag with NO user edits: modified_fields == ['template']."""

    def setup_method(self):
        self.fields = attribute_pod_fields(
            modified_fields=["template"],
            pod_view=materialized_pod_view(),
            template_only_view=template_only_view(),
            derived_view=view(**TEMPLATE_VALUES),  # derived == template for unmodified
            has_template=True,
        )

    @pytest.mark.parametrize("field", ["image", "command", "environment_variables", "networking", "volume_mounts"])
    def test_template_provided_fields_read_template(self, field):
        # Every field the template sets reads 'template' — even networking (a non-empty
        # DEFAULT on the row) and volume_mounts (MATERIALIZED, non-empty on the row).
        assert self.fields[field]["source"] == "template", self.fields[field]

    def test_materialized_volume_mounts_is_not_mislabeled_pod(self):
        # The exact regression: non-empty stored volume_mounts must NOT read 'pod'.
        vm = self.fields["volume_mounts"]
        assert vm["pod_value"]  # non-empty on the raw row
        assert vm["in_modified_fields"] is False
        assert vm["source"] == "template"

    def test_default_networking_is_not_mislabeled_pod(self):
        # Stored networking is the non-empty service default; template overrides it →
        # 'template', not 'pod'. (Old heuristic: non-empty raw ⇒ 'pod'.)
        assert self.fields["networking"]["source"] == "template"

    @pytest.mark.parametrize("field", ["resources", "compute_queue", "healthchecks", "time_to_stop_default"])
    def test_unset_untemplated_fields_read_default(self, field):
        assert self.fields[field]["source"] == "default", self.fields[field]

    def test_no_field_reads_pod(self):
        povr = [f for f, i in self.fields.items() if i["source"] == "pod"]
        assert povr == [], f"untouched template pod should have zero pod overrides, got {povr}"


# ── user overrides flip to 'pod' ───────────────────────────────────────────────

class TestUserOverrides:
    def test_overridden_scalar_reads_pod(self):
        fields = attribute_pod_fields(
            modified_fields=["template", "image"],
            pod_view=materialized_pod_view(image="myorg/custom:1"),
            template_only_view=template_only_view(),
            derived_view=view(**{**TEMPLATE_VALUES, "image": "myorg/custom:1"}),
            has_template=True,
        )
        assert fields["image"]["source"] == "pod"
        assert fields["image"]["in_modified_fields"] is True
        # other template fields untouched
        assert fields["command"]["source"] == "template"

    def test_edited_config_marks_volume_mounts_pod(self):
        # The exact config-edit-freezes-template case: editing config adds volume_mounts
        # to modified_fields → 'pod'.
        fields = attribute_pod_fields(
            modified_fields=["template", "volume_mounts"],
            pod_view=materialized_pod_view(),
            template_only_view=template_only_view(),
            derived_view=view(**TEMPLATE_VALUES),
            has_template=True,
        )
        assert fields["volume_mounts"]["source"] == "pod"

    def test_resources_subfield_modified_marks_resources_pod(self):
        # modified_fields tracks resources at sub-field granularity (resources.cpu_limit).
        fields = attribute_pod_fields(
            modified_fields=["template", "resources.cpu_limit"],
            pod_view=materialized_pod_view(resources={"cpu_limit": 4000}),
            template_only_view=template_only_view(),
            derived_view=view(**TEMPLATE_VALUES, resources={"cpu_limit": 4000}),
            has_template=True,
        )
        assert fields["resources"]["source"] == "pod"
        assert fields["resources"]["in_modified_fields"] is True

    def test_override_equal_to_template_still_reads_pod(self):
        # If the user explicitly set a value (it's in modified_fields), it's 'pod' even
        # when the value happens to equal the template's — the ledger is the truth.
        fields = attribute_pod_fields(
            modified_fields=["template", "image"],
            pod_view=materialized_pod_view(image="postgres:16"),
            template_only_view=template_only_view(),
            derived_view=view(**TEMPLATE_VALUES),
            has_template=True,
        )
        assert fields["image"]["source"] == "pod"


# ── no-template pods ───────────────────────────────────────────────────────────

class TestNoTemplate:
    def test_everything_is_default_or_pod_never_template(self):
        fields = attribute_pod_fields(
            modified_fields=["image"],
            pod_view=view(image="alpine:3"),
            template_only_view=None,
            derived_view=view(image="alpine:3"),
            has_template=False,
        )
        assert fields["image"]["source"] == "pod"  # user-set
        assert fields["image"]["template_value"] is None
        assert fields["networking"]["source"] == "default"
        assert all(i["source"] != "template" for i in fields.values())


# ── summary shape ──────────────────────────────────────────────────────────────

class TestSummaryShape:
    def test_every_field_has_full_breakdown(self):
        fields = attribute_pod_fields(
            modified_fields=["template"],
            pod_view=materialized_pod_view(),
            template_only_view=template_only_view(),
            derived_view=view(**TEMPLATE_VALUES),
            has_template=True,
        )
        assert set(fields) == set(PROVENANCE_FIELD_DEFAULTS)
        for info in fields.values():
            assert set(info) == {
                "source",
                "in_modified_fields",
                "template_provides",
                "pod_value",
                "template_value",
                "derived_value",
            }
            assert info["source"] in ("pod", "template", "default")


# ── Detailed: full create-from-template → edit lifecycle ───────────────────────
# Walks a pod through create → edit image → edit config (volume_mounts) → edit a
# resources subfield, asserting each field's source transitions correctly and that
# the summary counts move with it. The crux: editing one field never disturbs the
# attribution of the others.

# A complex, nested template volume_mounts set (multiple paths + config_content).
COMPLEX_MOUNTS = {
    "/data": {"type": "tapisvolume", "source_id": "datavol", "read_only": False},
    "/etc/app.conf": {"type": "ephemeral", "config_content": "a=1\nb=2", "config_permissions": "0644"},
    "/cache": {"type": "pvc", "source_id": "cachevol"},
}


def _tmpl_view():
    return view(**{**TEMPLATE_VALUES, "volume_mounts": COMPLEX_MOUNTS})


class TestLifecycle:
    def _attr(self, modified, pod_overrides=None, derived_extra=None):
        pod = materialized_pod_view(volume_mounts=COMPLEX_MOUNTS, **(pod_overrides or {}))
        derived = view(**{**TEMPLATE_VALUES, "volume_mounts": COMPLEX_MOUNTS, **(derived_extra or {})})
        return attribute_pod_fields(
            modified_fields=modified,
            pod_view=pod,
            template_only_view=_tmpl_view(),
            derived_view=derived,
            has_template=True,
        )

    def test_stage1_fresh_all_template(self):
        f = self._attr(["template"])
        for key in ("image", "command", "environment_variables", "networking", "volume_mounts"):
            assert f[key]["source"] == "template", (key, f[key])
        assert not any(i["source"] == "pod" for i in f.values())

    def test_stage2_edit_image_only_image_pod(self):
        f = self._attr(["template", "image"],
                       pod_overrides={"image": "myorg/db:1"},
                       derived_extra={"image": "myorg/db:1"})
        assert f["image"]["source"] == "pod"
        assert f["image"]["pod_value"] == "myorg/db:1"
        assert f["volume_mounts"]["source"] == "template"
        assert f["networking"]["source"] == "template"

    def test_stage3_edit_config_marks_volume_mounts_pod(self):
        edited = dict(COMPLEX_MOUNTS)
        edited["/etc/app.conf"] = {"type": "ephemeral", "config_content": "a=99\nb=2"}
        pod = materialized_pod_view(volume_mounts=edited, image="myorg/db:1")
        derived = view(**{**TEMPLATE_VALUES, "volume_mounts": edited, "image": "myorg/db:1"})
        f = attribute_pod_fields(["template", "image", "volume_mounts"], pod, _tmpl_view(), derived, True)
        assert f["volume_mounts"]["source"] == "pod"
        # the edited nested value is captured for inspection
        assert f["volume_mounts"]["pod_value"]["/etc/app.conf"]["config_content"] == "a=99\nb=2"
        assert f["image"]["source"] == "pod"

    def test_stage4_edit_resources_subfield(self):
        f = self._attr(["template", "resources.cpu_limit"],
                       pod_overrides={"resources": {"cpu_limit": 4000}},
                       derived_extra={"resources": {"cpu_limit": 4000}})
        # whole 'resources' reads 'pod' via the subfield match
        assert f["resources"]["source"] == "pod"
        assert f["resources"]["in_modified_fields"] is True
        # untouched fields stay template
        assert f["image"]["source"] == "template"
        assert f["volume_mounts"]["source"] == "template"

    def test_summary_counts_shift_with_edits(self):
        fresh = self._attr(["template"])
        edited = self._attr(["template", "image", "volume_mounts"],
                            pod_overrides={"image": "x"},
                            derived_extra={"image": "x"})

        def counts(f):
            c = {"pod": 0, "template": 0, "default": 0}
            for i in f.values():
                c[i["source"]] += 1
            return c

        cf, ce = counts(fresh), counts(edited)
        assert cf["pod"] == 0
        assert ce["pod"] == 2          # image + volume_mounts
        assert ce["template"] == cf["template"] - 2  # those two moved out of template


class TestNestedAndDefaultsEdges:
    def test_complex_volume_mounts_values_captured(self):
        # A fresh template pod: volume_mounts is 'template', and all three nested
        # mounts (incl config_content) round-trip through derived_value.
        f = attribute_pod_fields(["template"], materialized_pod_view(volume_mounts=COMPLEX_MOUNTS),
                                 _tmpl_view(), view(**{**TEMPLATE_VALUES, "volume_mounts": COMPLEX_MOUNTS}), True)
        vm = f["volume_mounts"]
        assert vm["source"] == "template"
        assert set(vm["derived_value"].keys()) == {"/data", "/etc/app.conf", "/cache"}
        assert vm["derived_value"]["/etc/app.conf"]["config_content"] == "a=1\nb=2"

    def test_template_value_equal_to_default_reads_default(self):
        # If the template "sets" a field to the very same value as the service default,
        # template_provides is False → 'default' (it adds nothing over the floor).
        # resources default is {}; a template that leaves resources at {} → 'default'.
        f = attribute_pod_fields(["template"], materialized_pod_view(),
                                 template_only_view(), view(**TEMPLATE_VALUES), True)
        assert f["resources"]["source"] == "default"
        assert f["compute_queue"]["source"] == "default"

    def test_no_template_everything_default_no_template_values(self):
        f = attribute_pod_fields(["image"], view(image="alpine"), None, view(image="alpine"), False)
        assert f["image"]["source"] == "pod"          # user-set
        assert f["image"]["template_value"] is None
        assert all(i["source"] != "template" for i in f.values())
        assert f["networking"]["source"] == "default"
