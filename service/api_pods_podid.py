import re
import json
from fastapi import APIRouter, Query
from models_pods import Pod, UpdatePod, PodResponse, Password, PodDeleteResponse, PodsFinalResponse, PodBaseFull
from channels import CommandChannel
from tapisservice.tapisfastapi.utils import g, ok, error
from models_templates_utils import combine_pod_and_template_recursively, get_template_merged_secret_map, validate_pod_secret_map_against_template
from kubernetes_utils import rm_pvc, KubernetesError, delete_configmap, NAMESPACE
from secret_utils import resolve_secret_map, inject_secrets_into_env_vars, check_pod_unresolved_patterns
from models_volume_mounts_utils import interpolate_config_content

from tapisservice.logs import get_logger
logger = get_logger(__name__)

router = APIRouter()


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
    for key, value in input_data.items():
        setattr(pod, key, value)

    post_update_pod = pod.dict().copy()

    # Only update if there's a change
    if post_update_pod != pre_update_pod:
        updated_fields = {key: post_update_pod[key] for key in post_update_pod if key in pre_update_pod and post_update_pod[key] != pre_update_pod[key]}
        # Add updated field names to pod's modified_fields list
        current_modified_fields = set(pod.modified_fields or [])
        new_modified_fields = set(updated_fields.keys())
        pod.modified_fields = list(current_modified_fields.union(new_modified_fields))
        pod.db_update(f"'{g.username}' updated pod, updated_fields: {json.dumps(updated_fields)}")
    else:
        return error(result=pod.display(), msg="Incoming data made no changes to pod. Is incoming data equal to current data?")
        
    return ok(
        result=pod.display(),
        msg="Pod updated successfully.",
        metadata={"note":("Pod will require restart when updating command, environment_variables,",
                          "status_requested, volume_mounts, networking, or resources.")})


@router.delete(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="delete_pod",
    operation_id="delete_pod",
    response_model=PodDeleteResponse)
async def delete_pod(pod_id):
    """
    Delete a pod.

    Returns "".
    """
    logger.info(f"DELETE /pods/{pod_id} - Top of delete_pod.")

    # Needs to delete pod, service, db_pod, db_password
    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    password = Password.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)

    # Clean up any PVCs associated with this pod (created for 'pvc' type volume mounts)
    # PVC name format: {k8_name}--pvc--{source_id[:20]}
    # One PVC per unique source_id, so track which we've already deleted
    if pod.volume_mounts:
        deleted_pvc_sources = set()
        deleted_configmaps = set()
        
        for mount_path, vol_mount in pod.volume_mounts.items():
            if vol_mount is None:
                continue
            # Handle both dict and VolumeMount objects
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
                # Clean up PVCs (one per unique source_id)
                source_id = vol_info.get("source_id", "")
                if source_id in deleted_pvc_sources:
                    continue
                deleted_pvc_sources.add(source_id)
                
                # Reconstruct the PVC name using the same logic as kubernetes_templates.py
                source_name_truncated = source_id[:20] if source_id else "pvc"
                pvc_name = f"{pod.k8_name}--pvc--{source_name_truncated}"
                if len(pvc_name) > 62:
                    pvc_name = pvc_name[:62]
                
                try:
                    rm_pvc(pvc_name)
                    logger.info(f"Deleted PVC {pvc_name} for pod {pod_id}")
                except KubernetesError as e:
                    logger.warning(f"Failed to delete PVC {pvc_name}: {e}")
                    
            elif vol_type == "ephemeral":
                # Clean up ConfigMaps for ephemeral mounts
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
                    logger.info(f"Deleted ConfigMap {configmap_name} for pod {pod_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete ConfigMap {configmap_name}: {e}")

    pod.db_delete()
    password.db_delete()

    return ok(result="", msg="Pod successfully deleted.")


@router.get(
    "/pods/{pod_id}",
    tags=["Pods"],
    summary="get_pod",
    operation_id="get_pod",
    response_model=PodResponse)
async def get_pod(
    pod_id: str,
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    check_unresolved: bool = Query(True, description="Check for unresolved ${...} patterns and include in metadata. Default: True")
    ):
    """
    Get a pod.

    Returns retrieved pod object.
    
    Use check_unresolved=true to detect any ${...} patterns that haven't been resolved.
    """
    logger.info(f"GET /pods/{pod_id} - Top of get_pod.")

    pod = Pod.db_get_with_pk(pod_id, tenant=g.request_tenant_id, site=g.site_id)
    
    metadata = {}
    if check_unresolved:
        unresolved = check_pod_unresolved_patterns(
            secret_map=pod.secret_map,
            environment_variables=pod.environment_variables,
            volume_mounts=pod.volume_mounts
        )
        if unresolved:
            metadata["unresolved_patterns"] = unresolved

    return ok(result=pod.display(include_configs=include_configs), metadata=metadata, msg="Pod retrieved successfully.")


@router.get(
    "/pods/{pod_id}/derived",
    tags=["Pods"],
    summary="get_derived_pod",
    operation_id="get_derived_pod",
    response_model=PodResponse)
async def get_derived_pod(
    pod_id: str,
    include_configs: bool = Query(False, description="Include full config_content for volume mounts using field. Default: false (shows placeholder with size)"),
    resolve_secrets: bool = Query(False, description="Resolve and show actual secret values (admin only). Default: false. Use to preview how secrets will be interpolated.")
    ):
    """
    Derive a pod's final definition if templates are used.

    Returns final pod definition to be used for pod creation.
    
    Use resolve_secrets=true (admin only) to preview how secrets will be interpolated
    into environment_variables and config_content.
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

    # If resolve_secrets=true, resolve the secret_map and inject into env vars and config_content
    resolved_secrets = {}
    resolve_errors = []
    if resolve_secrets:
        # Admin-only check - g.admin is set in auth.py based on PODS_ADMIN role
        if getattr(g, 'admin', False) and g.username not in ["cgarcia", "_pods_testuser_admin"]:
            raise Exception("resolve_secrets=true requires admin privileges (pods_admin role)")
        
        # Resolve secret_map values
        if final_pod.secret_map:
            resolved_secrets, resolve_errors = resolve_secret_map(
                secret_map=dict(final_pod.secret_map),
                site_id=input_pod.site_id,
                tenant_id=input_pod.tenant_id,
                actor=g.username,  # Short refs should be expanded at creation, explicit refs have owner embedded
                pod_id=input_pod.pod_id,
                pod=input_pod  # Pass pod for networking/random resolution
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