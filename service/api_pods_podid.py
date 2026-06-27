import re
import json
from fastapi import APIRouter, Query
from models_pods import Pod, UpdatePod, PodResponse, Password, PodDeleteResponse, PodsFinalResponse, PodBaseFull, ResetPodFields
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from models_templates_utils import combine_pod_and_template_recursively, get_template_merged_secret_map, validate_pod_secret_map_against_template
from kubernetes_utils import rm_pvc, KubernetesError, delete_configmap, NAMESPACE
from secret_utils import resolve_secret_map, inject_secrets_into_env_vars, check_pod_unresolved_patterns, expand_short_secret_references
from models_volume_mounts_utils import interpolate_config_content, validate_volume_mounts_permissions
from utils import check_permissions
from stack_utils import find_dependents, validate_stack_fields
from errors import PermissionsException
import codes

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


# ── audit log helpers ─────────────────────────────────────────────────────────

def _leaf_diffs(old, new, path=""):
    """Recursively yield (path, old_val, new_val) for every changed leaf."""
    diffs = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            child = f"{path}.{k}" if path else k
            if k not in old:
                diffs.append((child, None, new[k]))
            elif k not in new:
                diffs.append((child, old[k], None))
            else:
                diffs.extend(_leaf_diffs(old[k], new[k], child))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            diffs.append((path, old, new))
    elif old != new:
        diffs.append((path, old, new))
    return diffs


def _fmt_val(v):
    """Human-readable representation of a value for action log."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    return json.dumps(v, separators=(",", ":"))


#### /pods/{pod_id}

@router.put(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="update_pod",
    operation_id="update_pod",
    response_model=PodResponse)
async def update_pod(pod_id, update_pod: UpdatePod):
    """
    Update a pod.

    Note:
    - Pod will not be restarted, you must restart the pod for any pod-related changes to proliferate.

    Returns updated pod object.
    """
    logger.info(f"UPDATE /pods/{pod_id} - Top of update_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    
    pre_update_pod = pod.dict().copy()

    # Pod existence is already checked above. Now we validate update and update with values that are set.
    input_data = update_pod.dict(exclude_unset=True)
    
    # Check if volume_mounts is being updated
    volume_mounts_changed = 'volume_mounts' in input_data
    
    for key, value in input_data.items():
        setattr(pod, key, value)

    # Expand short secret references ${secret:name} → ${secret:username:name} so the
    # health loop can resolve them without actor context (same as create_pod does).
    if 'secret_map' in input_data and pod.secret_map:
        pod.secret_map = expand_short_secret_references(pod.secret_map, g.username)

    # If volume_mounts changed, validate permissions and update mounted_by on each entry
    if volume_mounts_changed:
        # Get pre-update volume_mounts to check for modifications to other users' mounts
        pre_volume_mounts = pre_update_pod.get('volume_mounts', {}) or {}
        
        # Check if user is ADMIN on this pod
        user_is_admin = check_permissions(
            user=g.username,
            level=codes.ADMIN,
            object=pod,
            object_type="pod",
            roles=getattr(g, 'roles', None),
            tenant=g.request_tenant_id
        )
        
        pod_volume_mounts = pod.volume_mounts or {}
        if hasattr(pod_volume_mounts, 'dict'):
            pod_volume_mounts = pod_volume_mounts.dict()
        elif hasattr(pod_volume_mounts, 'model_dump'):
            pod_volume_mounts = pod_volume_mounts.model_dump()
        
        # For non-ADMIN users, check if they're trying to modify/remove another user's mount
        if not user_is_admin:
            for mount_path, old_mount in pre_volume_mounts.items():
                if old_mount is None:
                    continue
                old_mounted_by = old_mount.get('mounted_by') if isinstance(old_mount, dict) else getattr(old_mount, 'mounted_by', None)
                
                # Skip mounts without mounted_by (legacy data) - anyone can modify
                if not old_mounted_by:
                    continue
                
                # Check if this mount was modified or removed by someone other than the mounter
                if old_mounted_by != g.username:
                    new_mount = pod_volume_mounts.get(mount_path)
                    
                    # Check if mount was removed
                    if new_mount is None or mount_path not in pod_volume_mounts:
                        raise PermissionsException(f"Cannot remove mount at '{mount_path}' - mounted by '{old_mounted_by}', not '{g.username}'. Only ADMIN or the original mounter can remove.")
                    
                    # Check if mount was modified (compare source_id)
                    old_source = old_mount.get('source_id') if isinstance(old_mount, dict) else getattr(old_mount, 'source_id', None)
                    new_source = new_mount.get('source_id') if isinstance(new_mount, dict) else getattr(new_mount, 'source_id', None)
                    if old_source != new_source:
                        raise PermissionsException(f"Cannot modify mount at '{mount_path}' - mounted by '{old_mounted_by}', not '{g.username}'. Only ADMIN or the original mounter can modify.")
        
        # Only validate volume mount permissions for NEW or MODIFIED mounts
        # Unchanged mounts were already validated when they were first added
        if pod_volume_mounts:
            # Build a dict of only new/modified mounts that need validation
            mounts_to_validate = {}
            for mount_path, mount_config in pod_volume_mounts.items():
                if mount_config is None:
                    continue
                old_mount = pre_volume_mounts.get(mount_path)
                if old_mount is None:
                    # New mount - needs validation
                    mounts_to_validate[mount_path] = mount_config
                else:
                    # Check if source_id changed
                    old_source_id = old_mount.get('source_id') if isinstance(old_mount, dict) else getattr(old_mount, 'source_id', None)
                    new_source_id = mount_config.get('source_id') if isinstance(mount_config, dict) else getattr(mount_config, 'source_id', None)
                    if old_source_id != new_source_id:
                        # Modified mount - needs validation
                        mounts_to_validate[mount_path] = mount_config
            
            # Only call validation if there are new/modified mounts
            if mounts_to_validate:
                vm_validation = validate_volume_mounts_permissions(
                    mounts_to_validate,
                    user=g.username,
                    tenant=g.request_tenant_id,
                    site=g.site_id,
                    roles=getattr(g, 'roles', None)
                )
                
                # Permission errors should block the update
                if not vm_validation.is_valid:
                    raise PermissionsException(vm_validation.error_message)
                
                # Set mounted_by on new/modified mounts
                if vm_validation.metadata and vm_validation.metadata.get("mounted_by"):
                    new_mounted_by = vm_validation.metadata["mounted_by"]
                    for mount_path in mounts_to_validate:
                        mount_config = pod.volume_mounts.get(mount_path)
                        if mount_config and mount_path in new_mounted_by:
                            if isinstance(mount_config, dict):
                                mount_config["mounted_by"] = new_mounted_by[mount_path]
                            elif hasattr(mount_config, 'mounted_by'):
                                mount_config.mounted_by = new_mounted_by[mount_path]
            
            # Preserve mounted_by on unchanged mounts
            for mount_path, mount_config in pod.volume_mounts.items():
                if mount_config is None:
                    continue
                if mount_path not in mounts_to_validate:
                    # Unchanged mount - preserve original mounted_by
                    old_mount = pre_volume_mounts.get(mount_path)
                    if old_mount:
                        old_mounted_by_user = old_mount.get('mounted_by') if isinstance(old_mount, dict) else getattr(old_mount, 'mounted_by', None)
                        if old_mounted_by_user:
                            if isinstance(mount_config, dict):
                                mount_config["mounted_by"] = old_mounted_by_user
                            elif hasattr(mount_config, 'mounted_by'):
                                mount_config.mounted_by = old_mounted_by_user

    # Validate stack dependency fields when topology changed (setattr above bypasses model validators).
    if 'depends_on' in input_data or 'ready_condition' in input_data:
        validate_stack_fields(pod, tenant=g.request_tenant_id, site=g.site_id)

    post_update_pod = pod.dict().copy()

    # Only update if there's a change
    if post_update_pod != pre_update_pod:
        updated_fields = {key: post_update_pod[key] for key in post_update_pod if key in pre_update_pod and post_update_pod[key] != pre_update_pod[key]}
        # Add updated field names to pod's modified_fields list
        current_modified_fields = set(pod.modified_fields or [])
        new_modified_fields = set(updated_fields.keys())
        pod.modified_fields = list(current_modified_fields.union(new_modified_fields))

        # Build a leaf-level diff so the action log shows exactly what changed
        # and what it changed FROM — no need to hunt through prior log entries.
        all_diffs = []
        for key in updated_fields:
            all_diffs.extend(_leaf_diffs(pre_update_pod.get(key), post_update_pod.get(key), key))
        MAX_SHOWN = 8
        parts = []
        for p, o, n in all_diffs[:MAX_SHOWN]:
            # environment_variables/secret_map values can hold pasted tokens/secrets, and
            # action_logs are READ-visible — record that the key changed, never the value.
            if p.split(".", 1)[0] in ("environment_variables", "secret_map"):
                if o is None:
                    parts.append(f"{p}: added (hidden)")
                elif n is None:
                    parts.append(f"{p}: removed")
                else:
                    parts.append(f"{p}: changed (hidden)")
            else:
                parts.append(f"{p}: {_fmt_val(o)}→{_fmt_val(n)}")
        if len(all_diffs) > MAX_SHOWN:
            parts.append(f"+{len(all_diffs) - MAX_SHOWN} more")
        change_str = ", ".join(parts) if parts else "no leaf changes detected"
        pod.db_update(f"'{g.username}' updated pod: {change_str}")
    else:
        return error(result=pod.display(), msg="Incoming data made no changes to pod. Is incoming data equal to current data?")
        
    return ok(
        result=pod.display(),
        msg="Pod updated successfully.",
        metadata={"note":("Pod will require restart when updating command, environment_variables,",
                          "status_requested, volume_mounts, networking, or resources.")})


# Fields that can be reset to their template default. The value is what the field
# is cleared to BEFORE re-deriving from the template (so combine_pod_and_template
# re-supplies it). See CONFIG_CONTENT_MODEL.md (tapis-ui). Identity/lifecycle
# fields (pod_id, template, status_requested, …) are intentionally excluded.
RESETTABLE_FIELD_DEFAULTS = {
    "volume_mounts": {},
    "environment_variables": {},
    "secret_map": {},
    "networking": {},
    "resources": {},
    "template_overrides": None,
    "healthchecks": None,
    "command": None,
    "arguments": None,
    "image": "",
    "description": "",
    "compute_queue": "default",
    "depends_on": None,
    "ready_condition": None,
}


@router.post(
    "/pods/{pod_id}/reset_field",
    tags=["Pods"],
    summary="reset_pod_field",
    operation_id="reset_pod_field",
    response_model=PodResponse)
async def reset_pod_field(pod_id, reset_fields: ResetPodFields):
    """
    Reset one or more pod fields to their template default.

    Removes each field from `modified_fields` and re-materializes it from the
    pod's template, re-linking the field to the template (undoing a pod-level
    override). Only valid for template-backed pods.

    NOTE: whole-field — resetting `volume_mounts` reverts ALL of the pod's mounts
    to the template, including any the user added. Restart required to apply.
    """
    logger.info(f"RESET /pods/{pod_id}/reset_field - fields: {reset_fields.fields}")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    if not pod.template:
        raise ValueError("Pod has no template; there is no template default to reset to.")

    fields = list(reset_fields.fields or [])
    if not fields:
        raise ValueError("No fields provided to reset.")

    invalid = [f for f in fields if f not in RESETTABLE_FIELD_DEFAULTS]
    if invalid:
        raise ValueError(
            f"Cannot reset field(s) {invalid}. Resettable fields: {sorted(RESETTABLE_FIELD_DEFAULTS.keys())}."
        )

    pre_reset_pod = pod.dict().copy()

    # Provenance ledger without the reset fields (and their sub-fields, e.g.
    # resources.cpu_limit) so combine_pod_and_template stops skipping them.
    new_modified = [
        m for m in (pod.modified_fields or [])
        if m not in fields and not any(m.startswith(f"{f}.") for f in fields)
    ]

    # Re-derive: combine a copy whose provenance no longer claims these fields, so
    # the template supplies them, then re-materialize the derived values into the
    # pod. Re-materializing (vs. clearing to empty) keeps the configs visible in
    # the raw pod view — matching how a freshly-created template pod looks.
    derive_copy = PodBaseFull(**pod.dict().copy())
    for field in fields:
        setattr(derive_copy, field, RESETTABLE_FIELD_DEFAULTS[field])
    derive_copy.modified_fields = list(new_modified)
    derived = combine_pod_and_template_recursively(
        derive_copy, derive_copy.template, tenant=g.request_tenant_id, site=g.site_id
    )

    for field in fields:
        setattr(pod, field, getattr(derived, field, RESETTABLE_FIELD_DEFAULTS[field]))
    pod.modified_fields = new_modified

    post_reset_pod = pod.dict().copy()
    if post_reset_pod == pre_reset_pod:
        return error(
            result=pod.display(),
            msg="Reset made no changes — those fields already match the template default.")

    pod.db_update(f"'{g.username}' reset to template default: {', '.join(fields)}")

    return ok(
        result=pod.display(),
        msg=f"Reset {len(fields)} field(s) to template default. Restart the pod to apply.",
        metadata={"note": "Field(s) re-linked to the template; restart the pod to apply changes."})


def delete_pod_resources(pod, password):
    """Tear down a pod's Kubernetes-side resources (PVCs and ConfigMaps created for pvc/ephemeral
    volume mounts) and delete its DB rows. Shared by delete_pod and the stack cascade-delete so the
    cleanup logic lives in exactly one place."""
    if pod.volume_mounts:
        deleted_pvc_sources = set()
        deleted_configmaps = set()

        for mount_path, vol_mount in pod.volume_mounts.items():
            if vol_mount is None:
                continue
            if hasattr(vol_mount, 'dict'):
                vol_info = vol_mount.dict()
            elif hasattr(vol_mount, 'model_dump'):
                vol_info = vol_mount.model_dump()
            elif isinstance(vol_mount, dict):
                vol_info = vol_mount
            else:
                vol_info = dict(vol_mount)

            vol_type = vol_info.get("type", "").lower()

            if vol_type == "pvc":
                source_id = vol_info.get("source_id", "")
                if source_id in deleted_pvc_sources:
                    continue
                deleted_pvc_sources.add(source_id)
                source_name_truncated = source_id[:20] if source_id else "pvc"
                pvc_name = f"{pod.k8_name}--pvc--{source_name_truncated}"
                if len(pvc_name) > 62:
                    pvc_name = pvc_name[:62]
                try:
                    rm_pvc(pvc_name)
                    logger.info(f"Deleted PVC {pvc_name} for pod {pod.pod_id}")
                except KubernetesError as e:
                    logger.warning(f"Failed to delete PVC {pvc_name}: {e}")

            elif vol_type == "ephemeral":
                import hashlib
                mount_hash = hashlib.md5(mount_path.encode()).hexdigest()[:8]
                source_name = "ephemeral"[:9]
                configmap_name = f"{pod.k8_name}--{source_name}--{mount_hash}".lower()
                if len(configmap_name) > 62:
                    configmap_name = configmap_name[:62]
                if configmap_name in deleted_configmaps:
                    continue
                deleted_configmaps.add(configmap_name)
                try:
                    delete_configmap(configmap_name, namespace=NAMESPACE)
                    logger.info(f"Deleted ConfigMap {configmap_name} for pod {pod.pod_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete ConfigMap {configmap_name}: {e}")

    pod.db_delete()
    if password:
        password.db_delete()


@router.delete(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="delete_pod",
    operation_id="delete_pod",
    response_model=PodDeleteResponse)
async def delete_pod(pod_id, force: bool = False):
    """
    Delete a pod.

    Notes:
    - If other pods depend_on this pod, the delete is blocked (409-style) unless `?force=true`,
      which first removes this pod from those pods' depends_on lists.

    Returns "".
    """
    logger.info(f"DELETE /pods/{pod_id} - Top of delete_pod.")

    # Needs to delete pod, service, db_pod, db_password
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    password = Password.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Dependency protection: refuse to delete a pod others depend_on unless forced.
    dependents = find_dependents(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    if dependents and not force:
        return error(
            result=[d.pod_id for d in dependents],
            msg=f"Pod '{pod_id}' is a dependency of {len(dependents)} other pod(s): "
                f"{[d.pod_id for d in dependents]}. Use ?force=true to remove the dependency and delete.",
        )
    if dependents and force:
        for d in dependents:
            d.depends_on = [x for x in (d.depends_on or []) if x != pod_id]
            d.db_update(f"'{g.username}' removed depends_on '{pod_id}' (dependency pod deleted)")

    delete_pod_resources(pod, password)

    return ok(result="", msg="Pod successfully deleted.")


# ── Provenance: per-field "what is pod vs what is template" ────────────────────
# Fields attributed by the provenance view, with their service defaults (the value
# a non-template pod has when the user sets nothing). Used to tell a template-
# provided value apart from a plain service default.
PROVENANCE_FIELD_DEFAULTS = {
    "image": "",
    "command": None,
    "arguments": None,
    "environment_variables": {},
    "secret_map": {},
    "volume_mounts": {},
    "networking": {"default": {"protocol": "http", "port": 5000}},
    "resources": {},
    "healthchecks": None,
    "compute_queue": "default",
    "time_to_stop_default": 43200,
    "time_to_stop_instance": None,
    "description": "",
    "status_requested": "ON",
    "depends_on": None,
    "ready_condition": "available",
    "template_overrides": None,
}


def _prov_norm(v):
    """Normalize a value to a plain JSON-comparable form (pydantic objects → dict)."""
    if hasattr(v, "model_dump"):
        try:
            return v.model_dump()
        except Exception:
            pass
    if hasattr(v, "dict"):
        try:
            return v.dict()
        except Exception:
            pass
    return v


def compute_pod_provenance(pod, tenant, site):
    """Per-field attribution of a pod's effective values: which came from the USER
    (a pod override, tracked in modified_fields), from the TEMPLATE, or are plain
    service DEFAULTS. This is the canonical "what is pod vs what is template" format.

    Provenance is decided by `modified_fields` (the override ledger) — NOT by whether
    a value happens to be non-empty. A materialized-but-unmodified field (e.g. a
    template pod's volume_mounts, copied into the row at creation) correctly reads as
    'template' here even though its stored value is non-empty.

    For each field returns: source (pod|template|default), in_modified_fields,
    template_provides, and the pod_value / template_value / derived_value so you can
    see exactly where the effective value came from.
    """
    mf = set(pod.modified_fields or [])
    has_template = bool(pod.template)

    if has_template:
        # Derived = the real merged view (pod overrides layered on top of template).
        derived = combine_pod_and_template_recursively(
            PodBaseFull(**pod.dict().copy()), pod.template, tenant=tenant, site=site)
        # Template-only = a clean pod (overridable fields reset to default, nothing
        # marked modified) carrying just the template — isolates the template's own
        # contribution from the pod's materialized/overridden values.
        template_only = PodBaseFull(**pod.dict().copy())
        for f, dflt in PROVENANCE_FIELD_DEFAULTS.items():
            setattr(template_only, f, dflt)
        template_only.modified_fields = []
        template_only = combine_pod_and_template_recursively(
            template_only, template_only.template, tenant=tenant, site=site)
    else:
        derived = pod
        template_only = None

    fields = attribute_pod_fields(
        modified_fields=pod.modified_fields,
        pod_view=pod,
        template_only_view=template_only,
        derived_view=derived,
        has_template=has_template,
    )

    counts = {"pod": 0, "template": 0, "default": 0}
    for info in fields.values():
        counts[info["source"]] += 1

    return {
        "pod_id": pod.pod_id,
        "template": pod.template or None,
        "has_template": has_template,
        "modified_fields": list(pod.modified_fields or []),
        "summary": counts,
        "fields": fields,
    }


def attribute_pod_fields(modified_fields, pod_view, template_only_view, derived_view, has_template):
    """Pure attribution: given the three views of a pod (raw stored, template-only,
    and merged/derived) plus the modified_fields ledger, decide each field's source.

    source = 'pod'      → field (or a field.* subfield) is in modified_fields (user override)
             'template' → not modified, and the template provides a non-default value
             'default'  → not modified and template doesn't set it (plain service default)

    Kept free of model/DB/g so it can be tested exhaustively. `*_view` are anything
    with the pod field names as attributes (real models, SimpleNamespace, mocks).
    """
    mf = set(modified_fields or [])
    fields = {}
    for f, dflt in PROVENANCE_FIELD_DEFAULTS.items():
        in_mod = (f in mf) or any(m.startswith(f + ".") for m in mf)
        pod_val = _prov_norm(getattr(pod_view, f, None))
        der_val = _prov_norm(getattr(derived_view, f, None))
        tmpl_val = (
            _prov_norm(getattr(template_only_view, f, None)) if has_template else None
        )
        template_provides = has_template and tmpl_val != _prov_norm(dflt)

        if in_mod:
            source = "pod"
        elif template_provides:
            source = "template"
        else:
            source = "default"

        fields[f] = {
            "source": source,
            "in_modified_fields": in_mod,
            "template_provides": template_provides,
            "pod_value": pod_val,
            "template_value": tmpl_val,
            "derived_value": der_val,
        }
    return fields


@router.get(
    "/pods/{pod_id}/overrides",
    tags=["Pods"],
    summary="get_pod_overrides",
    operation_id="get_pod_overrides",
    response_model=PodsFinalResponse)
async def get_pod_overrides(pod_id):
    """
    Lean "what the user actually set" view: only the pod's override layer (the fields in
    modified_fields) plus identity (pod_id, template, status). The normal GET resolves every
    default/template value, hiding what's a real user override and bloating the payload; this
    returns just the sparse override layer — legible and safe to round-trip on edit. Pairs with
    /provenance (per-field source). See tapis-ui src/app/Pods/LAYERING_MODEL.md.
    """
    logger.info(f"GET /pods/{pod_id}/overrides - Top of get_pod_overrides.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    return ok(result=pod.display_overrides(), msg="Pod overrides (user-set layer) retrieved successfully.")


@router.get(
    "/pods/{pod_id}/provenance",
    tags=["Pods"],
    summary="get_pod_provenance",
    operation_id="get_pod_provenance",
    response_model=PodsFinalResponse)
async def get_pod_provenance(pod_id):
    """
    Per-field provenance for a pod: which fields are the user's overrides (pod),
    which come from the template, and which are plain service defaults.

    Source-of-truth is `modified_fields`, NOT value-emptiness — so a template pod's
    materialized-but-unmodified fields read as 'template'. For each field you get
    source (pod|template|default), in_modified_fields, and the pod/template/derived
    values, so the determination is fully inspectable.
    """
    logger.info(f"GET /pods/{pod_id}/provenance - Top of get_pod_provenance.")
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    result = compute_pod_provenance(pod, tenant=g.request_tenant_id, site=g.site_id)
    return ok(result=result, msg="Pod provenance computed successfully.")


def derive_pod_for_display(pod, mode, *, tenant, site):
    """
    Produce the pod object that GET /pods/{id} should display, in one of three modes.

    Why this exists: a pod is stored SPARSE — fields the user didn't override are
    empty on the row, and the template supplies them only when merged. So the UI
    needs two things to render the Overview: the RESOLVED values (merge the template)
    AND `modified_fields` (the override ledger, to colour each field pod-vs-template).
    Historically that meant two calls: GET /pods/{id} (sparse + modified_fields) plus
    GET /pods/{id}/derived (merged values). This helper lets the single GET return both.

    mode:
      "none" → the raw stored pod (sparse; template fields empty unless overridden).
      "lite" → template-merged values via combine_pod_and_template_recursively, with
               NO password lookup / legacy <<TAPIS_*>> interpolation. The cheap
               "resolved definition" the Overview UI wants — merge is in-memory.
      "full" → "lite" + legacy <<TAPIS_*>>/<<tapissecret_*>> interpolation from the
               Password table (parity with GET /pods/{id}/derived).

    In every mode the returned object keeps `modified_fields` (combine starts from the
    pod's own dict and never clears it), so .display() carries both resolved values and
    provenance. Template-less pods are returned unchanged (combine is a no-op), so
    ?derived_lite is free for them — same single cheap response either way.
    """
    if mode == "none":
        return pod
    pod_for_derive = PodBaseFull(**pod.dict().copy())
    if pod_for_derive.template:
        final_pod = combine_pod_and_template_recursively(
            pod_for_derive, pod_for_derive.template, tenant=tenant, site=site)
    else:
        final_pod = pod_for_derive
    if mode == "full":
        # Legacy placeholder interpolation — parity with the /derived endpoint.
        pods_env = Password.db_get_with_pk(
            pod_for_derive.pod_id, pod_for_derive.tenant_id, pod_for_derive.site_id).dict()
        if final_pod.environment_variables:
            for key, val in final_pod.environment_variables.items():
                if not isinstance(val, str):
                    continue
                new_val = val
                for match in re.findall(r'<<TAPIS_(.*?)>>', val):
                    new_val = new_val.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
                for match in re.findall(r'<<tapissecret_(.*?)>>', val):
                    new_val = new_val.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
                final_pod.environment_variables[key] = new_val
    return final_pod


@router.get(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="get_pod",
    operation_id="get_pod",
    response_model=PodResponse)
async def get_pod(
    pod_id: str,
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    check_unresolved: bool = Query(True, description="Check for unresolved ${...} patterns and include in metadata. Default: True"),
    derived: bool = Query(False, description="Return the template-merged definition (also interpolates legacy <<TAPIS_*>> placeholders). Like GET /pods/{pod_id}/derived but on this endpoint, so the merged values come back alongside modified_fields in one call."),
    derived_lite: bool = Query(False, description="Fast template-only merge for display: combines templates but skips password lookup + legacy placeholder interpolation. Returns resolved values AND modified_fields in a single call. Template-less pods are unchanged.")
    ):
    """
    Get a pod.

    Returns retrieved pod object.

    Use check_unresolved=true to detect any ${...} patterns that haven't been resolved.
    Use derived_lite=true (or derived=true) to get the template-merged values together
    with modified_fields in a single request, instead of also calling /derived.
    """
    logger.info(f"GET /pods/{pod_id} - Top of get_pod. derived={derived}, derived_lite={derived_lite}")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    derive_mode = "lite" if derived_lite else ("full" if derived else "none")

    metadata = {}
    if check_unresolved:
        # Report unresolved ${...} on the STORED values (pre-merge) — what's actually persisted.
        unresolved = check_pod_unresolved_patterns(
            secret_map=pod.secret_map,
            environment_variables=pod.environment_variables,
            volume_mounts=pod.volume_mounts
        )
        if unresolved:
            metadata["unresolved_patterns"] = unresolved

    final = derive_pod_for_display(pod, derive_mode, tenant=g.request_tenant_id, site=g.site_id)
    if derive_mode != "none":
        metadata["derived"] = True
        metadata["derived_mode"] = derive_mode

    return ok(result=final.display(include_configs=include_configs), metadata=metadata, msg="Pod retrieved successfully.")


@router.get(
    "/pods/{pod_id}/derived",
    tags=["Pods"],
    summary="get_derived_pod",
    operation_id="get_derived_pod",
    response_model=PodResponse)
async def get_derived_pod(
    pod_id: str,
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    resolve_secrets: bool = Query(False, description="Resolve and show secret values (admin only). Default: false. Honours each secret's readable flag: a write-only (readable=False) secret is NOT read — its value comes back as the sentinel '<<write-only>>'. Use to preview how secrets interpolate."),
    reveal_write_only: bool = Query(False, description="Admin escalation for resolve_secrets: also read+reveal write-only (readable=False) secret values, bypassing the readable flag (the same privileged read pods use at start). Admin only. Implies resolve_secrets.")
    ):
    """
    Derive a pod's final definition if templates are used.

    Returns final pod definition to be used for pod creation.

    Use resolve_secrets=true (admin only) to preview how secrets will be interpolated
    into environment_variables and config_content. By default this honours each
    secret's write-only (readable=False) flag — those come back as '<<write-only>>'.
    Pass reveal_write_only=true (admin only) to bypass that and see write-only values.
    """
    logger.info(f"GET /pods/{pod_id}/derived - Top of get_derived_pod.")

    input_pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    pod = PodBaseFull(**input_pod.dict().copy()) # Create a copy of pod data we'll merge template data into
    if pod.template:
        # Derive the final pod object by combining the pod and templates
        final_pod = combine_pod_and_template_recursively(pod, pod.template, tenant=g.request_tenant_id, site=g.site_id)
    else:
        final_pod = pod

    ###
    ### SECRETS
    ###
    # Need to replace all "<<TAPIS_vars>>" or "<<tapissecret_vars>>" with vals from secrets
    # currently just the passwords db table. Eventually that'll become pods_env which itself could reference sk if that's needed.
    pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
    pods_env = pods_env.dict()
    
    # Handle legacy <<TAPIS_*>> and <<tapissecret_*>> placeholders
    for key, val in final_pod.environment_variables.items():
        new_val = val
        if isinstance(val, str):
            # Find both TAPIS_ and tapissecret_ patterns
            tapis_matches = re.findall(r'<<TAPIS_(.*?)>>', val)
            tapissecret_matches = re.findall(r'<<tapissecret_(.*?)>>', val)
            
            # Handle TAPIS_ replacements
            for match in tapis_matches:
                new_val = new_val.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
            
            # Handle tapissecret_ replacements
            for match in tapissecret_matches:
                new_val = new_val.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
                
            final_pod.environment_variables[key] = new_val

    # If resolve_secrets=true, resolve the secret_map and inject into env vars and config_content.
    # reveal_write_only implies resolve_secrets and additionally bypasses the write-only gate.
    resolved_secrets = {}
    resolve_errors = []
    if resolve_secrets or reveal_write_only:
        # Both are privileged operations — require admin role (g.admin)
        if not getattr(g, 'admin', False):
            raise PermissionsException("resolve_secrets/reveal_write_only require admin privileges (pods_admin role)")

        # for_display=True honours each secret's readable flag (write-only → sentinel).
        # reveal_write_only flips it off, reading write-only values via the service
        # account (same mechanism pods use at start) — the explicit admin escape hatch.
        for_display = not reveal_write_only

        # Resolve secret_map values
        if final_pod.secret_map:
            resolved_secrets, resolve_errors = resolve_secret_map(
                secret_map=dict(final_pod.secret_map),
                site_id=input_pod.site_id,
                tenant_id=input_pod.tenant_id,
                actor=g.username,  # Short refs should be expanded at creation, explicit refs have owner embedded
                pod_id=input_pod.pod_id,
                pod=input_pod,  # Pass pod for networking/random resolution
                for_display=for_display
            )
            if resolve_errors:
                logger.warning(f"Secret resolution errors for derived pod {pod_id}: {resolve_errors}")
        
        # Update secret_map with resolved values so users can see what gets injected
        if resolved_secrets:
            final_pod.secret_map = resolved_secrets
        
        # Inject resolved secrets into environment_variables
        if resolved_secrets and final_pod.environment_variables:
            processed_env, env_errors = inject_secrets_into_env_vars(
                final_pod.environment_variables,
                resolved_secrets,
                fail_on_missing=False
            )
            final_pod.environment_variables = processed_env
        
        # Interpolate secrets into config_content in volume_mounts
        if resolved_secrets and final_pod.volume_mounts:
            for mount_path, vol_mount in final_pod.volume_mounts.items():
                if vol_mount is None:
                    continue
                # Get config_content from the mount
                if hasattr(vol_mount, 'config_content') and vol_mount.config_content:
                    interpolated = interpolate_config_content(
                        vol_mount.config_content,
                        resolved_secrets,
                        fail_on_missing=False
                    )
                    vol_mount.config_content = interpolated
                elif isinstance(vol_mount, dict) and vol_mount.get('config_content'):
                    interpolated = interpolate_config_content(
                        vol_mount['config_content'],
                        resolved_secrets,
                        fail_on_missing=False
                    )
                    vol_mount['config_content'] = interpolated

    # Build metadata with template placeholder info if pod uses a template
    metadata = {}
    if pod.template:
        try:
            template_secret_map = get_template_merged_secret_map(
                pod.template,
                tenant=g.request_tenant_id,
                site=g.site_id
            )
            pod_secret_map = dict(final_pod.secret_map) if final_pod.secret_map else {}
            validation_result = validate_pod_secret_map_against_template(
                pod_secret_map,
                template_secret_map,
                actor=g.username
            )
            metadata = validation_result.metadata
        except Exception as e:
            logger.warning(f"Could not compute placeholder metadata for derived pod: {e}")
    
    # Add resolve_secrets info to metadata
    if resolve_secrets:
        metadata['secrets_resolved'] = True
        if resolve_errors:
            metadata['secret_resolution_errors'] = resolve_errors

    # Check for unresolved patterns in the final derived pod
    unresolved = check_pod_unresolved_patterns(
        secret_map=final_pod.secret_map,
        environment_variables=final_pod.environment_variables,
        volume_mounts=final_pod.volume_mounts
    )
    if unresolved:
        metadata["unresolved_patterns"] = unresolved

    return ok(result=final_pod.display(include_configs=include_configs), metadata=metadata, msg="Final derived pod retrieved successfully.")