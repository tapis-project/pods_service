from codes import ERROR, SPAWNER_SETUP, CREATING, \
    REQUESTED, DELETING
from models_pods import Pod, Password, PodBaseFull
from models_templates_tags import TemplateTag, TemplateTagPodDefinition
from models_templates_utils import combine_pod_and_template_recursively
from models_volume_mounts_utils import interpolate_legacy_secrets, interpolate_config_content
from secret_utils import inject_secrets_into_env_vars
from kubernetes_utils import create_pod, create_service, create_pvc, create_configmap, configmap_exists, KubernetesError, NAMESPACE
from kubernetes import client, config

from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
from volume_utils import get_nfs_ip
import os
import hashlib
import re

logger = get_logger(__name__)

# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


### This is quite an important function
def start_generic_pod(input_pod, revision: int, resolved_secrets: dict = None):
    resolved_secrets = resolved_secrets or {}
    
    ###
    ### Templates
    ###
    # This all is needed as I need an object that can validate (PodBaseFull)
    # And I need template or non-template pods to have the same fields. get_with_pk returns complete dict
    # PodBaseFull returns dict with Pydantic models as vals. This forces both cases to work the same.
    pod_init = PodBaseFull(**input_pod.dict().copy()) # Create a copy of pod data we'll merge template data into
    logger.debug(f"Attempting to start generic pod; name: {pod_init.k8_name}; revision: {revision}")

    # Derive the final pod object by combining the pod and templates
    if pod_init.template:
        pod = combine_pod_and_template_recursively(pod_init, pod_init.template, tenant=pod_init.tenant_id, site=pod_init.site_id)
    else:
        pod = pod_init
    logger.debug(f"derived_pod - pod_init.template: {pod_init.template} - template exists?: {bool(pod_init.template)}-------------\n{pod.display()}")

    ###
    ### SECRETS
    ###
    # Get legacy pods_env from Password table for legacy <<TAPIS_vars>> and <<tapissecret_vars>> placeholders
    pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
    pods_env = pods_env.dict()
    
    # Process environment_variables:
    # 1. Inject resolved secrets and process ${pods:secrets:KEY} references
    # 2. Then interpolate legacy <<TAPIS_*>> and <<tapissecret_*>> placeholders
    if pod.environment_variables:
        # First inject resolved secrets into env vars (handles ${pods:secrets:KEY})
        processed_env, env_errors = inject_secrets_into_env_vars(
            pod.environment_variables,
            resolved_secrets,
            fail_on_missing=False  # Don't fail on spawner - validation happened at API layer
        )
        if env_errors:
            logger.warning(f"Secret injection warnings for pod {pod.pod_id}: {env_errors}")
        
        # Then interpolate legacy secrets for backward compatibility
        for key, val in processed_env.items():
            if isinstance(val, str):
                processed_env[key] = interpolate_legacy_secrets(val, pods_env)
        
        pod.environment_variables = processed_env

    # Interpolate legacy secrets in command
    if pod.command and isinstance(pod.command, list):
        pod.command = [
            interpolate_legacy_secrets(item, pods_env) if isinstance(item, str) else item
            for item in pod.command
        ]

    # Interpolate legacy secrets in arguments  
    if pod.arguments and isinstance(pod.arguments, list):
        pod.arguments = [
            interpolate_legacy_secrets(item, pods_env) if isinstance(item, str) else item
            for item in pod.arguments
        ]

    volumes = []
    volume_mounts = []

    # Track PVCs that have been created for pod_id + source_id to avoid duplicates
    created_pvc_volumes = {}

    # Process volume mounts (dict-based structure keyed by mount_path)
    if pod.volume_mounts:
        nfs_nfs_ip = get_nfs_ip()
        
        for mount_path, vol_mount in pod.volume_mounts.items():
            # Skip None entries (removed mounts)
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
            source_id = vol_info.get("source_id", "")
            sub_path = vol_info.get("sub_path", "")
            read_only = vol_info.get("read_only")
            config_content = vol_info.get("config_content", "")
            config_permissions = vol_info.get("config_permissions", "0644")
            config_filename = vol_info.get("config_filename", "")
            config_update_mode = vol_info.get("config_update_mode", "always").lower()
            
            # Set read_only defaults based on type if not specified
            if read_only is None:
                read_only = vol_type in ['tapissnapshot', 'ephemeral']
            
            # Generate unique k8 name using hash of mount_path
            # Kubernetes volume names must be <= 63 characters and RFC 1123 compliant
            # (lowercase alphanumeric or '-', must start/end with alphanumeric)
            mount_hash = hashlib.md5(mount_path.encode()).hexdigest()[:8]
            source_name = source_id if source_id else "ephemeral"
            source_name = source_name[:9]
            # Sanitize source_name: remove invalid characters for K8s names
            # Only allow lowercase alphanumeric and hyphens, must start/end with alphanumeric
            source_name = re.sub(r'[^a-z0-9-]', '', source_name.lower())
            if not source_name or not source_name[0].isalnum():
                source_name = "vol" + source_name
            if source_name and not source_name[-1].isalnum():
                source_name = source_name.rstrip('-')
            if not source_name:
                source_name = "ephemeral"
            full_k8_name = f"{pod.k8_name}--{source_name}--{mount_hash}"
            # Truncate to 63 chars if needed (hash at end ensures uniqueness)
            if len(full_k8_name) > 62:
                full_k8_name = full_k8_name[:62]
            
            match vol_type:
                case "tapisvolume":
                    nfs_volume = client.V1NFSVolumeSource(path="/", server=nfs_nfs_ip)
                    volumes.append(client.V1Volume(name=full_k8_name, nfs=nfs_volume))
                    
                    # Build sub_path for NFS mount
                    if sub_path:
                        nfs_sub_path = f"{pod.tenant_id}/volumes/{source_id}/{sub_path}"
                    else:
                        nfs_sub_path = f"{pod.tenant_id}/volumes/{source_id}"
                    
                    # Handle config_content for tapisvolume - write to NFS
                    logger.debug(f"tapisvolume at '{mount_path}': config_content={bool(config_content)}, config_filename='{config_filename}', source_id='{source_id}'")
                    if config_content:
                        from volume_utils import file_exists, files_write_content
                        
                        logger.info(f"tapisvolume has config_content ({len(config_content)} bytes), will write to NFS")
                        
                        # Interpolate secrets in config_content:
                        # 1. First interpolate ${pods:secrets:KEY} with resolved_secrets
                        # 2. Then interpolate legacy <<TAPIS_*>> placeholders for backward compatibility
                        interpolated_content = interpolate_config_content(config_content, resolved_secrets, fail_on_missing=False)
                        interpolated_content = interpolate_legacy_secrets(interpolated_content, pods_env)
                        
                        # Determine config filename. The write path must mirror the k8s mount's
                        # nfs_sub_path — a sub_path mount exposes only that subdirectory, so a
                        # root-level write would never appear at mount_path (and once-mode would
                        # probe the wrong file).
                        cfg_filename = config_filename or os.path.basename(mount_path)
                        if sub_path:
                            config_file_path = f"volumes/{source_id}/{sub_path}/{cfg_filename}"
                        else:
                            config_file_path = f"volumes/{source_id}/{cfg_filename}"
                        
                        logger.info(f"Config file path: '{config_file_path}', tenant_id: '{pod.tenant_id}'")
                        
                        # Check config_update_mode
                        should_write = True
                        if config_update_mode == "once":
                            exists = file_exists(config_file_path, tenant_id=pod.tenant_id)
                            logger.info(f"config_update_mode=once, file_exists={exists}")
                            if exists:
                                logger.info(f"Config file '{config_file_path}' already exists, skipping write (config_update_mode=once)")
                                should_write = False
                        
                        if should_write:
                            try:
                                logger.info(f"Writing config content to NFS: {config_file_path}")
                                files_write_content(
                                    content=interpolated_content,
                                    path=config_file_path,
                                    tenant_id=pod.tenant_id,
                                    permissions=config_permissions
                                )
                                logger.info(f"Successfully wrote config content to NFS: {config_file_path}")
                            except Exception as e:
                                logger.error(f"Failed to write config content to NFS {config_file_path}: {e}", exc_info=True)
                    
                    volume_mounts.append(client.V1VolumeMount(
                        name=full_k8_name,
                        mount_path=mount_path,
                        sub_path=nfs_sub_path,
                        read_only=read_only
                    ))
                    
                case "tapissnapshot":
                    nfs_volume = client.V1NFSVolumeSource(path="/", server=nfs_nfs_ip)
                    volumes.append(client.V1Volume(name=full_k8_name, nfs=nfs_volume))
                    
                    # Build sub_path for NFS mount
                    if sub_path:
                        nfs_sub_path = f"{pod.tenant_id}/snapshots/{source_id}/{sub_path}"
                    else:
                        nfs_sub_path = f"{pod.tenant_id}/snapshots/{source_id}"
                    
                    volume_mounts.append(client.V1VolumeMount(
                        name=full_k8_name,
                        mount_path=mount_path,
                        sub_path=nfs_sub_path,
                        read_only=read_only  # Defaults to True for snapshots
                    ))
                    
                case "ephemeral":
                    # Ephemeral config volumes - inline config_content mounted as ConfigMap
                    # No external Volume resource needed
                    logger.info(
                        f"ephemeral mount at '{mount_path}': config_content present={bool(config_content)}, "
                        f"length={len(config_content) if config_content else 0}, "
                        f"resolved_secrets_keys={sorted(resolved_secrets.keys()) if resolved_secrets else []}"
                    )
                    if not config_content:
                        logger.warning(f"Ephemeral mount at '{mount_path}' has no config_content, skipping")
                        continue
                    
                    # Extract filename from mount_path for ConfigMap key
                    cfg_filename = config_filename or os.path.basename(mount_path)
                    config_dir_path = os.path.dirname(mount_path)
                    
                    # Create ConfigMap-based volume — name via the shared helper so the
                    # health-loop reconciler computes the identical name.
                    configmap_name = ephemeral_configmap_name(pod.k8_name, mount_path)
                    
                    # Check config_update_mode
                    should_create = True
                    if config_update_mode == "once":
                        if configmap_exists(configmap_name, namespace=NAMESPACE):
                            logger.info(f"ConfigMap '{configmap_name}' already exists, skipping creation (config_update_mode=once)")
                            should_create = False
                    
                    if should_create:
                        # Interpolate secrets in config_content:
                        # 1. First interpolate ${pods:secrets:KEY} with resolved_secrets
                        # 2. Then interpolate legacy <<TAPIS_*>> placeholders for backward compatibility
                        interpolated_content = interpolate_config_content(config_content, resolved_secrets, fail_on_missing=False)
                        interpolated_content = interpolate_legacy_secrets(interpolated_content, pods_env)
                        # Count unresolved placeholders to surface secret resolution issues
                        import re as _re
                        unresolved = _re.findall(r'\$\{pods:secrets:[^}]+\}', interpolated_content)
                        if unresolved:
                            logger.warning(
                                f"ephemeral mount '{mount_path}': {len(unresolved)} unresolved secret placeholder(s) "
                                f"in config_content after interpolation: {unresolved[:5]}. "
                                f"Check that secret_map keys match the placeholder names."
                            )
                        else:
                            logger.info(f"ephemeral mount '{mount_path}': all placeholders resolved, interpolated={len(interpolated_content)} bytes")

                        # Create a ConfigMap for this ephemeral config
                        try:
                            create_configmap(
                                name=configmap_name,
                                data={cfg_filename: interpolated_content},
                                namespace=NAMESPACE
                            )
                            logger.info(f"Created/updated ConfigMap '{configmap_name}' in namespace '{NAMESPACE}'")
                        except Exception as e:
                            logger.error(f"Failed to create/update ConfigMap {configmap_name}: {e}")
                            # If the CM doesn't exist at all we can't mount it — skip.
                            # If it exists (e.g. created by a prior run but update failed due to
                            # missing RBAC update permission), mount the existing version rather
                            # than leaving the pod without the volume entirely.
                            if not configmap_exists(configmap_name, namespace=NAMESPACE):
                                logger.error(f"ConfigMap {configmap_name} does not exist; skipping mount for {mount_path}")
                                continue
                            logger.warning(f"Mounting existing ConfigMap {configmap_name} at {mount_path} (content may be stale).")
                    
                    # Create volume referencing the ConfigMap
                    # Set file mode from config_permissions (convert octal string to int)
                    try:
                        mode = int(config_permissions, 8) if config_permissions else 0o644
                    except ValueError:
                        mode = 0o644
                    
                    configmap_volume = client.V1ConfigMapVolumeSource(
                        name=configmap_name,
                        default_mode=mode,
                        items=[client.V1KeyToPath(key=cfg_filename, path=cfg_filename)]
                    )
                    volumes.append(client.V1Volume(name=full_k8_name, config_map=configmap_volume))
                    
                    # Mount the configmap - mount the directory containing the file
                    volume_mounts.append(client.V1VolumeMount(
                        name=full_k8_name,
                        mount_path=mount_path,
                        sub_path=cfg_filename,  # Mount just the file
                        read_only=True  # ConfigMaps are always read-only
                    ))
                    
                case "pvc":
                    # For PVC mounts, create one PVC per source_id and reuse it for multiple mounts
                    # This allows mounting the same PVC at different paths with different sub_paths
                    if source_id in created_pvc_volumes:
                        # Reuse existing PVC volume
                        pvc_volume_name = created_pvc_volumes[source_id]
                    else:
                        # Create new PVC for this source_id
                        # PVC name format: {k8_name}--pvc--{source_id[:20]}
                        source_name_truncated = source_id[:20] if source_id else "pvc"
                        pvc_volume_name = f"{pod.k8_name}--pvc--{source_name_truncated}"
                        if len(pvc_volume_name) > 62:
                            pvc_volume_name = pvc_volume_name[:62]
                        
                        create_pvc(name=pvc_volume_name)
                        persistent_volume = client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_volume_name)
                        volumes.append(client.V1Volume(name=pvc_volume_name, persistent_volume_claim=persistent_volume))
                        created_pvc_volumes[source_id] = pvc_volume_name
                    
                    # Build sub_path for PVC mount
                    if sub_path:
                        pvc_sub_path = f"{pod.tenant_id}/volumes/{source_id}/{sub_path}"
                    else:
                        pvc_sub_path = f"{pod.tenant_id}/volumes/{source_id}"
                    
                    volume_mounts.append(client.V1VolumeMount(
                        name=pvc_volume_name,
                        mount_path=mount_path,
                        sub_path=pvc_sub_path,
                        read_only=read_only
                    ))
                    
                case _:
                    logger.warning(f"Unknown volume mount type: {vol_type}")

    # Each pod can have up to 3 networking objects with custom filled port/protocol/name
    # net_dict takes net_name:port.
    ports_dict = {}
    for net_name, net_info in pod.networking.items():
        if not isinstance(net_info, dict):
            net_info = net_info.dict()

        ports_dict.update({net_name: net_info['port']})

    container = {
        "name": pod.k8_name,
        "command": pod.command,
        "args": pod.arguments,
        "revision": revision,
        "image": pod.image,
        "ports_dict": ports_dict,
        "environment": pod.environment_variables.copy(),
        "tapis_permissions": pod.permissions,
        "mounts": [volumes, volume_mounts],
        "queue": pod.compute_queue,
        "mem_request": pod.resources.mem_request,
        "cpu_request": pod.resources.cpu_request,
        "mem_limit": pod.resources.mem_limit,
        "cpu_limit": pod.resources.cpu_limit,
        "ephemeral_storage_request": pod.resources.ephemeral_storage_request,
        "ephemeral_storage_limit": pod.resources.ephemeral_storage_limit,
        "gpus": pod.resources.gpus,
        "user": None,
        "healthchecks": pod.healthchecks,
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = ports_dict)


def ephemeral_configmap_name(k8_name, mount_path):
    """Compute the expected ConfigMap name for an ephemeral volume mount.
    Must stay in sync with the naming convention in start_generic_pod."""
    mount_hash = hashlib.md5(mount_path.encode()).hexdigest()[:8]
    full_name = f"{k8_name}--ephemeral--{mount_hash}"
    if len(full_name) > 62:
        full_name = full_name[:62]
    return full_name.lower()[:63]


def ensure_pod_configmaps(input_pod, resolved_secrets=None, existing_configmaps=None):
    """
    Verify that all ConfigMaps for ephemeral volume mounts exist.
    Regenerate any that are missing (e.g., after cluster migration, node
    rescheduling, or accidental deletion).

    Args:
        input_pod: Pod object from database.
        resolved_secrets: Dict of resolved secret values for config_content interpolation.
        existing_configmaps: Optional set of ConfigMap names already known to exist
                             (avoids per-mount K8s API calls when checking many pods).

    Returns:
        list: Names of ConfigMaps that were regenerated (empty if all existed).
    """
    resolved_secrets = resolved_secrets or {}

    # Derive final pod (with template if applicable) — same as start_generic_pod
    pod_init = PodBaseFull(**input_pod.dict().copy())
    if pod_init.template:
        pod = combine_pod_and_template_recursively(
            pod_init, pod_init.template, tenant=pod_init.tenant_id, site=pod_init.site_id
        )
    else:
        pod = pod_init

    if not pod.volume_mounts:
        return []

    # Quick scan: bail early if no ephemeral mounts
    has_ephemeral = False
    for vol_mount in pod.volume_mounts.values():
        if vol_mount is None:
            continue
        vtype = vol_mount.get("type", "") if isinstance(vol_mount, dict) else getattr(vol_mount, "type", "")
        if vtype.lower() == "ephemeral":
            has_ephemeral = True
            break
    if not has_ephemeral:
        return []

    # Get legacy pods_env for backward-compatible interpolation
    pods_env_obj = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
    pods_env = pods_env_obj.dict() if pods_env_obj else {}

    regenerated = []

    for mount_path, vol_mount in pod.volume_mounts.items():
        if vol_mount is None:
            continue

        # Normalise to dict
        if hasattr(vol_mount, 'model_dump'):
            vol_info = vol_mount.model_dump()
        elif hasattr(vol_mount, 'dict'):
            vol_info = vol_mount.dict()
        elif isinstance(vol_mount, dict):
            vol_info = vol_mount
        else:
            vol_info = dict(vol_mount)

        if vol_info.get("type", "").lower() != "ephemeral":
            continue

        config_content = vol_info.get("config_content", "")
        if not config_content:
            continue

        config_filename = vol_info.get("config_filename", "")
        config_permissions = vol_info.get("config_permissions", "0644")

        configmap_name = ephemeral_configmap_name(pod.k8_name, mount_path)

        # Check existence — prefer the pre-fetched set when available
        if existing_configmaps is not None:
            exists = configmap_name in existing_configmaps
        else:
            exists = configmap_exists(configmap_name, namespace=NAMESPACE)

        if exists:
            continue

        # ConfigMap is missing — regenerate it
        logger.warning(f"ConfigMap '{configmap_name}' missing for pod {pod.pod_id} "
                       f"at mount '{mount_path}'. Regenerating.")

        cfg_filename = config_filename or os.path.basename(mount_path)

        # Interpolate secrets in config_content (same two-pass approach as start_generic_pod)
        interpolated_content = interpolate_config_content(
            config_content, resolved_secrets, fail_on_missing=False
        )
        interpolated_content = interpolate_legacy_secrets(interpolated_content, pods_env)

        try:
            create_configmap(
                name=configmap_name,
                data={cfg_filename: interpolated_content},
                namespace=NAMESPACE,
            )
            regenerated.append(configmap_name)
            logger.info(f"Regenerated ConfigMap '{configmap_name}' for pod {pod.pod_id}")
        except Exception as e:
            logger.error(f"Failed to regenerate ConfigMap '{configmap_name}' "
                         f"for pod {pod.pod_id}: {e}")

    return regenerated
