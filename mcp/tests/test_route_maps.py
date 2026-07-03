"""Curation/scope tests — assert the reflected tool set against the real spec."""


def test_no_dangerous_tools(tool_names):
    bad = [n for n in tool_names if any(k in n for k in
           ("exec", "upload", "download", "list_files", "_value", "admin_", "gallery_photo"))]
    assert not bad, f"dangerous tools leaked: {bad}"


def test_no_resource_deletes(tool_names):
    # permission deletes are intentionally kept; resource deletes must not be.
    leaked = [n for n in tool_names if n.startswith("delete_") and "permission" not in n]
    assert not leaked, leaked


def test_no_raw_creates(tool_names):
    # creation is funneled through deploy_from_template; raw complex-body creates
    # (and the raw from-template ops superseded by helpers) must be absent.
    for n in ("create_pod", "update_pod", "create_stack", "add_template_tag",
              "create_volume", "create_snapshot", "add_image", "create_secret",
              "create_stack_from_template", "update_stack_from_template"):
        assert n not in tool_names, f"{n} should be excluded"


def test_expected_reads_and_lifecycle(tool_names):
    for n in ("list_pods", "get_pod", "get_derived_pod", "get_pod_logs",
              "start_pod", "stop_pod", "restart_pod", "stack_action",
              "list_stacks", "get_stack", "list_templates_and_tags",
              "get_template_tag", "list_volumes", "list_snapshots", "list_secrets"):
        assert n in tool_names, f"{n} should be present"


def test_permission_management_pods_and_stacks_only(tool_names):
    assert "set_pod_permission" in tool_names
    assert "set_stack_permission" in tool_names
    stray = [n for n in tool_names if n.endswith("_permission")
             and any(r in n for r in ("volume", "snapshot", "template"))]
    assert not stray, stray


def test_helpers_present(tool_names):
    for n in ("catalog", "list_deployable_templates", "deploy_from_template",
              "update_from_template"):
        assert n in tool_names


def test_catalog_size_is_curated(tool_names):
    # Trimmed to a curated core (see route_maps.py). Guardrail against accidental
    # scope blowups when the spec grows.
    assert 25 <= len(tool_names) <= 45, f"unexpected tool count: {len(tool_names)}"
