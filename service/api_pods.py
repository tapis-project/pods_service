import re

from fastapi import APIRouter, Query
from channels import CommandChannel
from codes import REQUESTED, ON, USER
from pydantic import ValidationError

from models_pods import Pod, NewPod, Password, PodsResponse, PodResponse, PodBase, PodBaseRead
from models_stacks import Stack
from stack_utils import validate_stack_fields
from utils import check_permissions
from models_templates_utils import validate_pod_secret_map_against_template, get_template_merged_secret_map, combine_pod_and_template_recursively
from models_pods import PodBaseFull
from models_volume_mounts_utils import (
    validate_pod_volume_mounts_against_template, 
    get_template_merged_volume_mounts,
    validate_volume_mounts_permissions,
    resolve_volume_placeholders
)
from secret_utils import get_placeholder_warnings, resolve_secret_map, resolve_random_passwords, resolve_pod_networking, expand_short_secret_references, get_config_secret_map_warnings, check_pod_unresolved_patterns
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


#### /pods

@router.get(
    "/pods",
    tags=["Pods"],
    summary="list_pods",
    operation_id="list_pods",
    response_model=PodsResponse)
async def list_pods(
    derived: bool = Query(False, description="Return pod definitions merged/derived with templates (like GET /pods/{pod_id}/derived)."),
    derived_lite: bool = Query(False, description="Fast template-only derivation. Skips password lookup and legacy placeholder interpolation.")
):
    """
    Get all pods in your respective tenant and site that you have READ or higher access to.

    Returns a list of pods.
    """
    derive_mode = "none"
    if derived_lite:
        derive_mode = "lite"
    elif derived:
        derive_mode = "full"

    logger.info(f"GET /pods - Top of list_pods. derived={derived}, derived_lite={derived_lite}, derive_mode={derive_mode}")
    # TODO search
    # Admin mode: single DB call, figure out user's own pods in-memory
    if getattr(g, 'admin_active', False):
        pods = Pod.db_get_all(tenant=g.request_tenant_id, site=g.site_id, defer_columns=[Pod.logs, Pod.action_logs])
        read_levels = {'READ', 'USER', 'ADMIN', 'APPROVEDADMIN'}
        user_pod_ids = set()
        for pod in pods:
            for perm in pod.permissions:
                user, level = perm.split(':', 1)
                if user == g.username and level in read_levels:
                    user_pod_ids.add(pod.pod_id)
                    break
    else:
        pods = Pod.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)
    pods_to_show = []
    metadata = {}
    final_msg = "Pods retrieved successfully."
    for pod in pods:
        try:
            # Validate using your response model (e.g., PodBase or whatever Pod.display() returns)
            pod_data = pod.display()

            if derive_mode != "none":
                # Derive through template chain using the same merge utility as the pod-level derived endpoint.
                pod_for_derive = PodBaseFull(**pod.dict().copy())
                if pod_for_derive.template:
                    final_pod = combine_pod_and_template_recursively(
                        pod_for_derive,
                        pod_for_derive.template,
                        tenant=g.request_tenant_id,
                        site=g.site_id
                    )
                else:
                    final_pod = pod_for_derive

                if derive_mode == "full":
                    # Full derivation keeps parity with pod-level derived endpoint for legacy placeholder interpolation.
                    pods_env = Password.db_get_with_pk(pod_for_derive.pod_id, pod_for_derive.tenant_id, pod_for_derive.site_id).dict()
                    if final_pod.environment_variables:
                        for key, val in final_pod.environment_variables.items():
                            if not isinstance(val, str):
                                continue
                            new_val = val
                            tapis_matches = re.findall(r'<<TAPIS_(.*?)>>', val)
                            tapissecret_matches = re.findall(r'<<tapissecret_(.*?)>>', val)
                            for match in tapis_matches:
                                new_val = new_val.replace(f"<<TAPIS_{match}>>", pods_env.get(match, ""))
                            for match in tapissecret_matches:
                                new_val = new_val.replace(f"<<tapissecret_{match}>>", pods_env.get(match, ""))
                            final_pod.environment_variables[key] = new_val

                pod_data = final_pod.display()

            PodBaseRead(**pod_data)  # This will raise if invalid
            pods_to_show.append(pod_data)
        except ValidationError as e:
            # Remove 'url' from each error dict because it's unsightly and not useful to end user
            error_list = e.errors()
            for err in error_list:
                err.pop('url', None)
            logger.warning(f"Pod {getattr(pod, 'pod_id', 'COULD NOT FIND PODID')} failed validation: {error_list}")
            if "warnings" not in metadata:
                metadata["warnings"] = []
            metadata["warnings"].append(
                f"Pod {getattr(pod, 'pod_id', None)} failed validation; omitting; reach out to admin; this debug might help: {error_list}"
            )
            final_msg = "Some pods failed validation. Please check metadata.warnings for details."
        except Exception as e:
            logger.warning(f"Pod {getattr(pod, 'pod_id', 'COULD NOT FIND PODID')} failed derive/list processing: {e}")
            if "warnings" not in metadata:
                metadata["warnings"] = []
            metadata["warnings"].append(
                f"Pod {getattr(pod, 'pod_id', None)} failed derive/list processing; omitting; debug: {str(e)}"
            )
            final_msg = "Some pods failed processing. Please check metadata.warnings for details."
    if derive_mode != "none":
        metadata["derived"] = True
        metadata["derived_mode"] = derive_mode
    if getattr(g, 'admin_active', False):
        admin_only_count = sum(1 for p in pods_to_show if p.get('pod_id') not in user_pod_ids)
        metadata["admin_context"] = {
            "admin_mode": True,
            "user_accessible_ids": list(user_pod_ids),
            "msg": f"You can access {len(pods_to_show) - admin_only_count} pods, admin reveals {admin_only_count}"
        }
    logger.info("Pods retrieved.")
    return ok(result=pods_to_show, metadata=metadata, msg=final_msg)


def _metrics_visible_pods():
    """Permission-filtered pod set for the fleet metrics endpoints — same
    visibility rule as list_pods (admin_active sees the whole tenant)."""
    if getattr(g, 'admin_active', False):
        return Pod.db_get_all(tenant=g.request_tenant_id, site=g.site_id,
                              defer_columns=[Pod.logs, Pod.action_logs])
    return Pod.db_get_all_with_permission(user=g.username, level='READ',
                                          tenant=g.request_tenant_id, site=g.site_id)


@router.get(
    "/pods/metrics",
    tags=["Pods"],
    summary="list_pods_metrics",
    operation_id="list_pods_metrics")
async def list_pods_metrics():
    """
    Live cpu/mem usage for every pod you can read — in ONE metrics-backend
    query. The service answers from its own TTL cache (~45 s), so dashboards
    can poll this freely without load reaching Grafana: all users
    in a tenant share one upstream query per cache window.

    Returns {pods: {pod_id: {status, usage, resources}}, backend, [unavailable]}.
    `usage` is {"unavailable": reason} per-pod-absent or fleet-wide when the
    backend is down/unconfigured — always HTTP 200.
    """
    logger.info("GET /pods/metrics - Top of list_pods_metrics.")
    from metrics_backend import bulk_usage_dict, explore_hint, get_provider

    pods = _metrics_visible_pods()
    pattern = f"pods-{g.site_id}-{g.request_tenant_id}-.*"
    bulk = bulk_usage_dict(pattern)

    result = {
        "pods": {},
        "backend": {"name": get_provider().name, "explore": explore_hint()},
    }
    if "unavailable" in bulk:
        result["unavailable"] = bulk["unavailable"]
    usage_by_k8_name = bulk.get("pods", {})
    for pod in pods:
        res = pod.resources or {}
        result["pods"][pod.pod_id] = {
            "status":  pod.status,
            "k8_name": pod.k8_name,
            "usage":   usage_by_k8_name.get(pod.k8_name),  # None = no series (not running)
            "resources": {
                "cpu_request_m":  int(res.get("cpu_request",  0) or 0),
                "cpu_limit_m":    int(res.get("cpu_limit",    0) or 0),
                "mem_request_mb": int(res.get("mem_request",  0) or 0),
                "mem_limit_mb":   int(res.get("mem_limit",    0) or 0),
                "gpus":           int(res.get("gpus",         0) or 0),
            },
        }
    return ok(result=result, msg="Fleet metrics retrieved.")


@router.get(
    "/pods/metrics/history",
    tags=["Pods"],
    summary="list_pods_metrics_history",
    operation_id="list_pods_metrics_history")
async def list_pods_metrics_history(
    window_s: int = Query(3600, description="History window in seconds (300..86400)."),
    step_s: int = Query(120, description="Sample step in seconds (>=30; raised automatically to cap points/pod at 400)."),
):
    """
    Cpu/mem time series for every pod you can read — sparkline/chart food.
    ONE cached query_range per (window, step) serves the whole tenant for
    ~3 minutes, so every dashboard and per-pod page shares the same upstream
    query. Series: cpu = cores, mem = bytes, points = [unix_ts, value].
    """
    logger.info(f"GET /pods/metrics/history - window_s={window_s}, step_s={step_s}")
    from metrics_backend import bulk_range_dict, explore_hint, get_provider

    pods = _metrics_visible_pods()
    pattern = f"pods-{g.site_id}-{g.request_tenant_id}-.*"
    bulk = bulk_range_dict(pattern, window_s=window_s, step_s=step_s)

    result = {
        "pods": {},
        "backend": {"name": get_provider().name, "explore": explore_hint()},
    }
    if "unavailable" in bulk:
        result["unavailable"] = bulk["unavailable"]
    else:
        result["window_s"] = bulk.get("window_s")
        result["step_s"] = bulk.get("step_s")
    series_by_k8_name = bulk.get("pods", {})
    for pod in pods:
        series = series_by_k8_name.get(pod.k8_name)
        if series:
            result["pods"][pod.pod_id] = series
    return ok(result=result, msg="Fleet metrics history retrieved.")


@router.post(
    "/pods",
    tags=["Pods"],
    summary="create_pod",
    operation_id="create_pod",
    response_model=PodResponse)
async def create_pod(new_pod: NewPod):
    """
    Create a pod with inputted information.
    
    Notes:
    - Author will be given ADMIN level permissions to the pod.
    - status_requested defaults to "ON". So pod will immediately begin creation.

    Returns new pod object.
    """
    logger.info("POST /pods - Top of create_pod.")
    # Create full Pod object. Validates as well.
    pod = Pod(**new_pod.dict())
    
    # Create list of modified fields (which pertain to the user-changed PodBase fields).
    for arg in new_pod.dict(exclude_unset=True).keys():
        if arg not in PodBase.__fields__.keys():
            raise ValueError(f"modified_fields must match the fields of the Pod object. Got {arg}.")
        # resources is dict. Need to list if resources.cpu, etc are changed.
        # networking and volume_mounts are lists. Don't need to do anything extra.
        if arg == "resources":
            for sub_arg in new_pod.resources.dict(exclude_unset=True).keys():
                pod.modified_fields.append(f"resources.{sub_arg}")
        else:
            pod.modified_fields.append(arg)

    # Validate secret_map placeholders if pod uses a template
    placeholder_metadata = {}
    volume_mount_metadata = {}
    if pod.template:
        try:
            # Get merged secret_map from all chained templates
            template_secret_map = get_template_merged_secret_map(
                pod.template, 
                tenant=g.request_tenant_id, 
                site=g.site_id
            )
            
            # Validate that pod's secret_map overrides all required placeholders
            pod_secret_map = pod.secret_map or {}
            if hasattr(pod_secret_map, 'dict'):
                pod_secret_map = pod_secret_map.dict()
            
            validation_result = validate_pod_secret_map_against_template(
                pod_secret_map,
                template_secret_map,
                actor=getattr(g, 'username', None)
            )
            
            if not validation_result.is_valid:
                raise ValueError(validation_result.error_message)
            
            # Capture placeholder metadata from validation (already computed)
            placeholder_metadata = validation_result.metadata
            
            # Validate volume_mounts from template
            template_volume_mounts = get_template_merged_volume_mounts(
                pod.template,
                tenant=g.request_tenant_id,
                site=g.site_id
            )
            
            if template_volume_mounts:
                # Get pod's volume_mounts as dict
                pod_volume_mounts = pod.volume_mounts or {}
                # Convert to dict if it's a Pydantic model
                if hasattr(pod_volume_mounts, 'model_dump'):
                    pod_volume_mounts = pod_volume_mounts.model_dump()
                elif hasattr(pod_volume_mounts, 'dict'):
                    pod_volume_mounts = pod_volume_mounts.dict()
                
                # Get template_overrides.volume_mounts if present
                template_overrides_mounts = None
                if hasattr(pod, 'template_overrides') and pod.template_overrides:
                    if hasattr(pod.template_overrides, 'model_dump'):
                        overrides = pod.template_overrides.model_dump()
                    elif hasattr(pod.template_overrides, 'dict'):
                        overrides = pod.template_overrides.dict()
                    else:
                        overrides = dict(pod.template_overrides) if pod.template_overrides else {}
                    template_overrides_mounts = overrides.get('volume_mounts', {})
                
                # Resolve volume placeholders from template
                resolved_mounts, placeholder_errors, placeholder_meta = resolve_volume_placeholders(
                    pod_mounts=pod_volume_mounts,
                    template_mounts=template_volume_mounts,
                    template_overrides_mounts=template_overrides_mounts
                )
                
                # Check for unresolved placeholders
                if placeholder_errors:
                    raise ValueError(f"Volume mount placeholder errors: {'; '.join(placeholder_errors)}")
                
                # Store the resolved mounts on the pod.
                if conf.get("sparse_volume_mounts", False):
                    # SPARSE (experimental, see LAYERING_MODEL.md): store ONLY the user's own
                    # mounts (resolved) + explicit removals (None) — NOT the template's mounts.
                    # The template's mounts are merged in at derive time (per mount-path, pod
                    # wins), exactly like environment_variables/secret_map. This keeps
                    # volume_mounts off the materialize path so it stays a true sparse override.
                    sparse_mounts = {}
                    for p, v in (pod_volume_mounts or {}).items():
                        if v is None:
                            sparse_mounts[p] = None  # explicit removal of an inherited mount
                        elif p in resolved_mounts:
                            sparse_mounts[p] = resolved_mounts[p]
                    pod.volume_mounts = sparse_mounts
                else:
                    # LEGACY (default): materialize the full template-merged set into the row.
                    # - Template mounts with placeholders resolved
                    # - User's volume_mounts (overrides/additions)
                    # - Explicit removals (None values) are already removed from resolved_mounts
                    pod.volume_mounts = resolved_mounts
                
                # Include placeholder resolution info in metadata
                if placeholder_meta.get("volume_placeholders"):
                    volume_mount_metadata.update(placeholder_meta)
                
                # Validate resolved volume_mounts (permissions check)
                vm_validation = validate_pod_volume_mounts_against_template(
                    resolved_mounts,
                    template_volume_mounts,
                    user=g.username,
                    tenant=g.request_tenant_id,
                    site=g.site_id,
                    roles=getattr(g, 'roles', None)
                )
                
                # Capture volume mount warnings in metadata
                if vm_validation.metadata:
                    volume_mount_metadata.update(vm_validation.metadata)
                    # Set mounted_by on each volume mount entry for tracking who mounted each volume
                    if vm_validation.metadata.get("mounted_by") and pod.volume_mounts:
                        mounted_by = vm_validation.metadata["mounted_by"]
                        for mount_path, user in mounted_by.items():
                            if mount_path in pod.volume_mounts and pod.volume_mounts[mount_path]:
                                if isinstance(pod.volume_mounts[mount_path], dict):
                                    pod.volume_mounts[mount_path]["mounted_by"] = user
                                elif hasattr(pod.volume_mounts[mount_path], 'mounted_by'):
                                    pod.volume_mounts[mount_path].mounted_by = user
                
        except Exception as e:
            logger.error(f"Error validating template placeholders for pod {pod.pod_id}: {e}")
            raise ValueError(f"{str(e)}")
    elif pod.secret_map:
        # No template - check pod's own secret_map for any unresolved placeholders
        warnings, _ = get_placeholder_warnings(pod.secret_map, actor=g.username)
        if warnings:
            # Build simple string list format
            required = [f"REQUIRED: '{w['env_var']}' - {w.get('description', 'No description')}. Override in secret_map."
                       for w in warnings if not w.get("has_default")]
            optional = [f"OPTIONAL: '{w['env_var']}' - {w.get('description', 'No description')}. Default: '{w.get('default_value')}'."
                       for w in warnings if w.get("has_default")]
            if required or optional:
                placeholder_metadata["available_placeholders"] = {
                    "required": required,
                    "optional": optional
                }
    
    # Check environment_variables for secret_map-only patterns and warn
    if pod.environment_variables:
        config_warnings = get_config_secret_map_warnings(pod.environment_variables)
        if config_warnings:
            warning_messages = [
                f"WARNING: '{w['env_var']}' uses {w['pattern']} syntax. {w['message']}"
                for w in config_warnings
            ]
            placeholder_metadata["config_syntax_warnings"] = warning_messages
    
    # Validate pod's own volume_mounts permissions (not from template)
    if pod.volume_mounts and not pod.template:
        pod_volume_mounts = pod.volume_mounts or []
        if hasattr(pod_volume_mounts, 'dict'):
            pod_volume_mounts = [vm.dict() if hasattr(vm, 'dict') else vm for vm in pod_volume_mounts]
        
        vm_validation = validate_volume_mounts_permissions(
            pod_volume_mounts,
            user=g.username,
            tenant=g.request_tenant_id,
            site=g.site_id,
            roles=getattr(g, 'roles', None)
        )
        
        # For direct pod creation, permission errors should block
        if not vm_validation.is_valid:
            raise ValueError(vm_validation.error_message)
        
        # Set mounted_by on each volume mount entry for tracking who mounted each volume
        if vm_validation.metadata and vm_validation.metadata.get("mounted_by") and pod.volume_mounts:
            mounted_by = vm_validation.metadata["mounted_by"]
            for mount_path, user in mounted_by.items():
                if mount_path in pod.volume_mounts and pod.volume_mounts[mount_path]:
                    if isinstance(pod.volume_mounts[mount_path], dict):
                        pod.volume_mounts[mount_path]["mounted_by"] = user
                    elif hasattr(pod.volume_mounts[mount_path], 'mounted_by'):
                        pod.volume_mounts[mount_path].mounted_by = user

    # Merge metadata from both validations
    final_metadata = {}
    if placeholder_metadata:
        final_metadata.update(placeholder_metadata)
    if volume_mount_metadata:
        final_metadata.update(volume_mount_metadata)

    # Resolve random passwords and pod networking BEFORE db_create
    # These need to be persisted immediately, regardless of status_requested
    # Track generated random passwords for action_log after db_create
    generated_random_keys = []
    if pod.secret_map:
        working_map = dict(pod.secret_map)
        
        # Expand short secret references ${secret:name} -> ${secret:username:name}
        # This persists the owner so later resolution doesn't need to know who created the pod
        working_map = expand_short_secret_references(working_map, g.username)
        
        # Resolve random passwords (generates and stores in working_map)
        # Pass pod=None since pod isn't in DB yet; we'll log after db_create
        # Track which keys had random patterns for logging
        import re
        RANDOM_PASSWORD_PATTERN = re.compile(r'\$\{pods:random:(\d+)\}')
        for key, value in working_map.items():
            if isinstance(value, str):
                match = RANDOM_PASSWORD_PATTERN.fullmatch(value)
                if match:
                    generated_random_keys.append((key, int(match.group(1))))
        
        working_map, random_errors, _ = resolve_random_passwords(working_map, pod=None, actor=g.username)
        if random_errors:
            raise ValueError(f"Failed to generate random passwords: {'; '.join(random_errors)}")
        
        # Resolve pod networking references
        working_map, networking_errors = resolve_pod_networking(working_map, pod)
        if networking_errors:
            raise ValueError(f"Failed to resolve pod networking: {'; '.join(networking_errors)}")
        
        # Update pod's secret_map with resolved values
        pod.secret_map = working_map

    # Stack membership + dependency validation (before any db writes).
    if pod.stack_id:
        stack = Stack.db_get_with_pk(pod.stack_id, tenant=g.request_tenant_id, site=g.site_id)
        if not stack:
            raise ValueError(f"stack_id '{pod.stack_id}' does not exist. Create the stack first with POST /pods/stacks.")
        if not getattr(g, 'admin', False) and not check_permissions(g.username, USER, stack, "stack", roles=g.roles):
            raise ValueError(f"You need USER+ permission on stack '{pod.stack_id}' to create a pod in it.")
    validate_stack_fields(pod, tenant=g.request_tenant_id, site=g.site_id)

    # Create pod password db entry. If it's successful, we continue.
    password = Password(pod_id=pod.pod_id)
    password.db_create()
    logger.debug(f"Created password entry for {pod.pod_id}")
    # Create pod database entry
    pod.db_create()
    logger.debug(f"New pod saved in db. pod_id: {pod.pod_id}; image: {pod.image}; tenant: {g.request_tenant_id}.")
    
    # Log random password generation to action_logs now that pod is in DB
    if generated_random_keys:
        log_entries = [f"{key} (length: {length})" for key, length in generated_random_keys]
        pod.db_update(log=f"Generated random password(s): {', '.join(log_entries)}")
    # If status_requested = On, then we request pod and put a command. Else leave in default STOPPED state. 
    if pod.status_requested == ON:
        # Resolve secrets at API layer before sending to spawner
        # This allows edge spawners to work without direct SK access
        #
        # IMPORTANT: If pod uses a template, we must merge the template's secret_map
        # with the pod's secret_map BEFORE resolving, so template-defined secrets
        # get resolved and sent to the spawner.
        resolved_secrets = {}
        
        # Derive merged secret_map if pod uses a template
        if pod.template:
            # Use combine_pod_and_template_recursively to get the final merged secret_map
            pod_copy = PodBaseFull(**pod.dict().copy())
            derived_pod = combine_pod_and_template_recursively(
                pod_copy, pod.template, tenant=g.request_tenant_id, site=g.site_id
            )
            merged_secret_map = getattr(derived_pod, 'secret_map', {}) or {}
        else:
            merged_secret_map = pod.secret_map or {}
        
        if merged_secret_map:
            resolved_secrets, secret_errors = resolve_secret_map(
                merged_secret_map,
                site_id=g.site_id,
                tenant_id=g.request_tenant_id,
                actor=g.username,
                pod_id=pod.pod_id,
                pod=pod
            )
            if secret_errors:
                # Required secrets missing - fail the create
                # Clean up the created resources
                try:
                    pod.db_delete()
                    password.db_delete()
                except Exception as e:
                    logger.warning(f"Failed to cleanup after secret resolution error: {e}")
                raise ValueError(f"Failed to resolve secrets: {'; '.join(secret_errors)}")
        
        pod.status = REQUESTED
        pod.db_update()
        # Send command to start new pod
        ch = CommandChannel(name=pod.site_id)
        ch.put_cmd(object_id=pod.pod_id,
                   object_type="pod",
                   tenant_id=pod.tenant_id,
                   site_id=pod.site_id,
                   resolved_secrets=resolved_secrets)
        ch.close()
        logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")
    
    # Check for unresolved patterns in the created pod
    unresolved = check_pod_unresolved_patterns(
        secret_map=pod.secret_map,
        environment_variables=pod.environment_variables,
        volume_mounts=pod.volume_mounts
    )
    if unresolved:
        final_metadata["unresolved_patterns"] = unresolved

    return ok(result=pod.display(), metadata=final_metadata, msg="Pod created successfully.")
