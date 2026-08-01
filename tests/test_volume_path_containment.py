"""
Security regression: config_content sub_path path-traversal.

sub_path is joined into the on-disk NFS write path at pod spawn (a background
path, not a request), so an unvalidated '..' let config_content escape the
volume/tenant base — authenticated arbitrary file write as the service user.
Two layers now guard it: a model validator on sub_path, and a containment sink
guard in volume_utils. Pure unit test (no cluster/DB).
"""
import sys
import pytest

sys.path.append('/home/tapis/service')

import volume_utils
from volume_utils import _contained_path, VolumesError
from models_volume_mounts_utils import VolumeMount


# ── sink guard ──────────────────────────────────────────────────────────────

def test_contained_path_allows_normal():
    base = "/nfs/tacc"
    assert _contained_path(base, "volumes/v1/file.txt") == "/nfs/tacc/volumes/v1/file.txt"
    # leading slash is treated as relative to base, not absolute-escape
    assert _contained_path(base, "/volumes/v1/x") == "/nfs/tacc/volumes/v1/x"


@pytest.mark.parametrize("evil", [
    "../../../../etc/passwd",
    "volumes/v1/../../../../etc/passwd",
    "..",
    "../othertenant/secret",
    "volumes/v1/../../../root/.ssh/authorized_keys",
])
def test_contained_path_blocks_traversal(evil):
    with pytest.raises(VolumesError):
        _contained_path("/nfs/tacc", evil)


# ── model validator ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["../escape", "a/../../b", "/abs/path", "x\\y"])
def test_sub_path_validator_rejects(bad):
    with pytest.raises(Exception):
        VolumeMount(type="tapisvolume", source_id="v1", sub_path=bad,
                    config_content="data", config_filename="f.txt")


def test_sub_path_validator_allows_normal():
    m = VolumeMount(type="tapisvolume", source_id="v1", sub_path="conf/nested",
                    config_content="data", config_filename="f.txt")
    assert m.sub_path == "conf/nested"


# ── every path helper is contained, not just the config-write pair ──────────
#
# The original fix only guarded files_write_content/file_exists. The other seven
# helpers kept `path = os.path.abspath(path)` then f"{base_path}/{path}", which
# collapses '..' BEFORE the join — so "/volumes/volA/../volB" resolved to
# "/volumes/volB" and was then happily joined under the tenant base. Tenant
# containment held; per-OBJECT scoping did not. Reachable in the wild via
# GET /pods/volumes/{id}/list?path=../othervolume (and ?path=../.. for the whole
# tenant root). These assert the escape is refused at the sink, so no future
# caller can reintroduce it.

_TRAVERSALS = [
    "/volumes/volA/../volB",          # sibling volume — the reported ?path= case
    "/volumes/volA/../..",            # tenant root: every volume and snapshot
    "/volumes/volA/../../../etc",     # out of the tenant entirely
    "volumes/volA/../../snapshots",   # no leading slash, same escape
]


# ── the containment BOUNDARY matters, not just the guard ────────────────────
#
# Guarding at the tenant base is NOT sufficient and this is the subtle part:
# "/volumes/volA/../volB" never leaves the tenant, so a tenant-base guard
# resolves it happily to a sibling volume the caller has no permission on, and
# "/volumes/volA/../.." resolves to the tenant root itself. Endpoints taking a
# user-supplied sub-path must pass object_root() as base_path so the boundary is
# the single volume/snapshot. These lock that in.

_CROSS_OBJECT = ["../volB", "../../", "../../../etc", "../../snapshots/snapX"]


@pytest.mark.parametrize("evil", _CROSS_OBJECT)
def test_tenant_base_is_NOT_sufficient(evil):
    """Documents why object_root exists: these all pass a tenant-base check."""
    tenant_base = "/podsnfs/tacc"
    caller_built = f"/volumes/volA/{evil.strip('/')}"
    try:
        resolved = _contained_path(tenant_base, caller_built)
    except VolumesError:
        return  # fine — some escape the tenant too
    # It resolved. Prove it landed somewhere the caller has no permission on.
    assert not resolved.startswith("/podsnfs/tacc/volumes/volA"), (
        "expected this to escape volA under a tenant-base guard")


@pytest.mark.parametrize("evil", _CROSS_OBJECT)
def test_object_root_blocks_cross_object_access(evil):
    """The real guard: base_path is the volume root, so a sibling is unreachable."""
    volume_root = "/podsnfs/tacc/volumes/volA"
    with pytest.raises(VolumesError):
        _contained_path(volume_root, evil.strip("/"))


def test_object_root_allows_subdirectories():
    volume_root = "/podsnfs/tacc/volumes/volA"
    assert _contained_path(volume_root, "") == volume_root
    assert _contained_path(volume_root, "sub/dir/f.txt") == f"{volume_root}/sub/dir/f.txt"


@pytest.mark.parametrize("evil", _CROSS_OBJECT)
def test_all_path_helpers_refuse_traversal(evil, monkeypatch):
    """Each helper must raise before touching the filesystem.

    base_path is the VOLUME root — the boundary the API actually passes now (see
    volume_utils.object_root). Against a tenant root these same payloads resolve
    cleanly to a sibling volume, which is precisely the bug object_root fixes and
    is asserted separately in test_tenant_base_is_NOT_sufficient.
    """
    base = "/podsnfs/tacc/volumes/volA"

    def _boom(*a, **kw):
        raise AssertionError("filesystem was touched before containment check")

    for mod, name in [(volume_utils.os, "makedirs"), (volume_utils.os, "remove"),
                      (volume_utils.shutil, "rmtree"), (volume_utils.shutil, "move"),
                      (volume_utils.shutil, "copy"), (volume_utils.shutil, "copytree")]:
        monkeypatch.setattr(mod, name, _boom)

    sub = evil.strip("/")
    with pytest.raises(VolumesError):
        volume_utils.files_mkdir(path=sub, base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_listfiles(path=sub, base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_delete(path=sub, base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_download(path=sub, base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_insert(file=None, path=sub, base_path=base)
    # move/copy take two paths — an escape on EITHER end is a traversal
    with pytest.raises(VolumesError):
        volume_utils.files_move(source_path=sub, new_path="ok", base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_move(source_path="ok", new_path=sub, base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_copy(source_path=sub, new_path="ok", base_path=base)
    with pytest.raises(VolumesError):
        volume_utils.files_copy(source_path="ok", new_path=sub, base_path=base)


def test_normal_subdirectory_listing_still_resolves():
    """The ?path= feature itself must keep working — contained, not disabled."""
    assert _contained_path("/nfs/tacc", "/volumes/volA/sub/dir") == "/nfs/tacc/volumes/volA/sub/dir"
    assert _contained_path("/nfs/tacc", "/volumes/volA") == "/nfs/tacc/volumes/volA"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
