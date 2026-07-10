"""Curation/scope tests — assert the reflected tool set against the real spec.

The surface is now the FULL create/read/update surface (operator request): exec,
file reads, secret creation + value use, and raw create/update for every
resource. Only DESTRUCTIVE resource deletes (and multipart uploads, oauth,
jupyter, gallery binaries, infra, the helper-owned from-template/update raw ops)
stay OUT. These tests encode that contract: what's IN, and what stays OUT.
"""


def test_still_out(tool_names):
    # Multipart uploads, admin observability (non-verbose), binary gallery bytes,
    # and jupyter special-casing stay excluded. exec / download / list_files /
    # secret VALUES are intentionally IN now, so they are NOT flagged here.
    bad = [n for n in tool_names if any(k in n for k in
           ("upload", "admin_", "gallery_photo", "jupyter"))]
    assert not bad, f"leaked: {bad}"


def test_resource_deletes_off_by_default(tool_names):
    # EXPOSE_DELETES=False: no resource deletes. Permission deletes (unshare) kept.
    leaked = [n for n in tool_names if n.startswith("delete_") and "permission" not in n]
    assert not leaked, f"resource deletes leaked: {leaked}"
    assert "delete_pod_permission" in tool_names  # unshare kept


def test_helper_owned_raw_ops_absent(tool_names):
    # The raw from-template / update ops are superseded by the deploy_from_template
    # / update_from_template helpers — must be absent to avoid duplicate tools.
    for n in ("create_stack_from_template", "update_stack_from_template"):
        assert n not in tool_names, f"{n} should be helper-owned, not reflected"


def test_full_crud_present(tool_names):
    # Raw create/update for every resource type.
    for n in ("create_pod", "update_pod",
              "create_volume", "update_volume",
              "create_snapshot", "update_snapshot",
              "create_secret", "update_secret", "get_secret_value",
              "add_image", "update_image",
              "create_stack", "update_stack",
              "add_template", "update_template", "add_template_tag",
              "save_pod_as_template_tag", "save_stack_as_template",
              "update_template_gallery_note"):
        assert n in tool_names, f"{n} should be present"


def test_exec_and_file_reads_present(tool_names):
    for n in ("exec_pod_commands",
              # pod file reads go through the ergonomic helpers (below); the raw
              # {url_path}-segment versions are retired.
              "list_pod_files", "read_pod_file",
              "list_volume_files", "get_volume_contents", "download_volume_file",
              "list_snapshot_files", "get_snapshot_contents", "download_snapshot_file"):
        assert n in tool_names, f"{n} should be present"


def test_raw_pod_readers_retired(tool_names):
    # Superseded by list_pod_files / read_pod_file (avoid the {url_path} footgun).
    for n in ("list_files_in_pod", "download_from_pod"):
        assert n not in tool_names, f"{n} should be retired in favor of the helper"


def test_expected_reads_and_lifecycle(tool_names):
    for n in ("list_pods", "get_pod", "get_derived_pod", "get_pod_logs",
              "start_pod", "stop_pod", "restart_pod", "stack_action",
              "list_stacks", "get_stack", "list_templates_and_tags",
              "get_template_tag", "list_volumes", "list_snapshots", "list_secrets"):
        assert n in tool_names, f"{n} should be present"


def test_permission_management_pods_and_stacks_by_default(tool_names):
    # Non-verbose keeps pod/stack permissions; volume/snapshot/template perms are
    # verbose-only (diagnostic surface).
    assert "set_pod_permission" in tool_names
    assert "set_stack_permission" in tool_names
    stray = [n for n in tool_names if n.endswith("_permission")
             and any(r in n for r in ("volume", "snapshot", "template"))]
    assert not stray, stray


def test_helpers_present(tool_names):
    for n in ("catalog", "list_deployable_templates", "deploy_from_template",
              "update_from_template"):
        assert n in tool_names


def test_catalog_size(tool_names):
    # Full CRUD surface (see route_maps.py). Guardrail against accidental scope
    # blowups (e.g. deletes/uploads/admin leaking in) as the spec grows.
    assert 55 <= len(tool_names) <= 85, f"unexpected tool count: {len(tool_names)}"
