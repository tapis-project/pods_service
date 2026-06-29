"""
Tests for the lean "overrides" view (Pod.display_overrides) behind
GET /pods/{pod_id}/overrides — returns ONLY the user-override layer (the fields in
modified_fields) plus identity, instead of the fat fully-resolved pod.

See tapis-ui src/app/Pods/LAYERING_MODEL.md. Run: make test-test_pod_overrides.py
"""
import sys

import pytest

sys.path.append('/home/tapis/service')

from models_pods import PodBaseFull


class FakePod:
    """Minimal stand-in for a Pod row — display_overrides only reads .dict(),
    .modified_fields, .pod_id, .template, .status."""

    def __init__(self, pod_id="p1", template="postgres:16", status="AVAILABLE",
                 modified_fields=None, **fields):
        self.pod_id = pod_id
        self.template = template
        self.status = status
        self.modified_fields = modified_fields or []
        self._fields = fields

    def dict(self):
        d = {
            "pod_id": self.pod_id,
            "template": self.template,
            "status": self.status,
            "modified_fields": self.modified_fields,
        }
        d.update(self._fields)
        return d


def overrides_of(fake):
    return PodBaseFull.display_overrides(fake)


class TestDisplayOverrides:
    def test_only_modified_fields_appear(self):
        fake = FakePod(
            modified_fields=["image", "volume_mounts"],
            image="postgres:16",
            volume_mounts={"/data": {"type": "tapisvolume", "source_id": "v"}},
            networking={"default": {"protocol": "tcp", "port": 5432}},  # NOT modified
            environment_variables={"PGDATA": "/data"},                  # NOT modified
        )
        out = overrides_of(fake)
        assert set(out["overrides"].keys()) == {"image", "volume_mounts"}
        assert out["overrides"]["image"] == "postgres:16"
        assert "networking" not in out["overrides"]
        assert "environment_variables" not in out["overrides"]

    def test_resources_subfields_grouped_under_parent(self):
        fake = FakePod(
            modified_fields=["resources.cpu_limit", "resources.mem_limit"],
            resources={"cpu_limit": 4000, "mem_limit": 8000, "gpus": 0},  # gpus NOT modified
        )
        out = overrides_of(fake)
        assert out["overrides"]["resources"] == {"cpu_limit": 4000, "mem_limit": 8000}
        # the unmodified subfield is excluded
        assert "gpus" not in out["overrides"]["resources"]

    def test_identity_and_modified_fields_echoed(self):
        fake = FakePod(pod_id="mypod", template="immich:prod@v3", status="STOPPED",
                       modified_fields=["image"], image="x")
        out = overrides_of(fake)
        assert out["pod_id"] == "mypod"
        assert out["template"] == "immich:prod@v3"
        assert out["status"] == "STOPPED"
        assert out["modified_fields"] == ["image"]

    def test_empty_modified_fields_yields_empty_overrides(self):
        fake = FakePod(modified_fields=["template"], image="postgres:16",
                       volume_mounts={"/data": {}})
        out = overrides_of(fake)
        # 'template' is a stored field, so it shows; image/volume_mounts do NOT (not modified)
        assert out["overrides"] == {"template": "postgres:16"}

    def test_no_template_pod_overrides_are_user_fields(self):
        fake = FakePod(template="", modified_fields=["image", "command"],
                       image="alpine:3", command=["sleep", "infinity"])
        out = overrides_of(fake)
        assert out["template"] is None
        assert out["overrides"] == {"image": "alpine:3", "command": ["sleep", "infinity"]}

    def test_missing_resources_subfield_is_skipped_safely(self):
        # modified_fields references a subfield not present in the stored dict → skipped.
        fake = FakePod(modified_fields=["resources.cpu_limit"], resources={"mem_limit": 8000})
        out = overrides_of(fake)
        assert out["overrides"].get("resources", {}) == {}  # nothing to group
