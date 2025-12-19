from codes import ERROR, SPAWNER_SETUP, CREATING, \
    REQUESTED, DELETING
from models_pods import Pod, Password, PodBaseFull
from models_templates_tags import TemplateTag, TemplateTagPodDefinition
from models_templates_utils import combine_pod_and_template_recursively
from models_volume_mounts_utils import interpolate_legacy_secrets
from kubernetes_utils import create_pod, create_service, create_pvc, create_configmap, KubernetesError
from kubernetes import client, config

from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
from volume_utils import get_nfs_ip
import os
import hashlib

logger = get_logger(__name__)

# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


### This is quite an important function
def start_generic_pod(input_pod, revision: int):
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
    # Need to replace all "<<TAPIS_vars>>"(legacy) or "<<tapissecret_vars>>" with vals from secrets
    # currently just the passwords db table. Eventually that'll become pods_env which itself could reference sk if that's needed.
    pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
    pods_env = pods_env.dict()
    
    # Interpolate legacy secrets in environment_variables
    if pod.environment_variables:
        for key, val in pod.environment_variables.items():
            if isinstance(val, str):
                pod.environment_variables[key] = interpolate_legacy_secrets(val, pods_env)

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
            # Kubernetes volume names must be <= 63 characters
            mount_hash = hashlib.md5(mount_path.encode()).hexdigest()[:8]
            source_name = source_id if source_id else "ephemeral"
            source_name = source_name[:9]
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
                    if config_content:
                        from volume_utils import file_exists, files_write_content
                        
                        # Interpolate secrets in config_content
                        interpolated_content = interpolate_legacy_secrets(config_content, pods_env)
                        
                        # Determine config filename
                        cfg_filename = config_filename or os.path.basename(mount_path)
                        config_file_path = f"volumes/{source_id}/{cfg_filename}"
                        
                        # Check config_update_mode
                        should_write = True
                        if config_update_mode == "once":
                            if file_exists(config_file_path, tenant_id=pod.tenant_id):
                                logger.info(f"Config file '{config_file_path}' already exists, skipping write (config_update_mode=once)")
                                should_write = False
                        
                        if should_write:
                            try:
                                files_write_content(
                                    content=interpolated_content,
                                    path=config_file_path,
                                    tenant_id=pod.tenant_id,
                                    permissions=config_permissions
                                )
                                logger.info(f"Wrote config content to NFS: {config_file_path}")
                            except Exception as e:
                                logger.error(f"Failed to write config content to NFS {config_file_path}: {e}")
                    
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
                    if not config_content:
                        logger.warning(f"Ephemeral mount at '{mount_path}' has no config_content, skipping")
                        continue
                    
                    # Extract filename from mount_path for ConfigMap key
                    cfg_filename = config_filename or os.path.basename(mount_path)
                    config_dir_path = os.path.dirname(mount_path)
                    
                    # Create ConfigMap-based volume
                    configmap_name = full_k8_name.lower()[:63]  # K8s name length limit
                    
                    # Check config_update_mode
                    from kubernetes_utils import create_configmap, configmap_exists
                    
                    should_create = True
                    if config_update_mode == "once":
                        if configmap_exists(configmap_name, namespace=conf.spawner_host_id):
                            logger.info(f"ConfigMap '{configmap_name}' already exists, skipping creation (config_update_mode=once)")
                            should_create = False
                    
                    if should_create:
                        # Interpolate secrets in config_content
                        interpolated_content = interpolate_legacy_secrets(config_content, pods_env)
                        
                        # Create a ConfigMap for this ephemeral config
                        try:
                            create_configmap(
                                name=configmap_name,
                                data={cfg_filename: interpolated_content},
                                namespace=conf.spawner_host_id
                            )
                        except Exception as e:
                            logger.error(f"Failed to create ConfigMap {configmap_name}: {e}")
                            continue
                    
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
                    create_pvc(name=full_k8_name)
                    persistent_volume = client.V1PersistentVolumeClaimVolumeSource(claim_name=full_k8_name)
                    volumes.append(client.V1Volume(name=full_k8_name, persistent_volume_claim=persistent_volume))
                    
                    # Build sub_path for PVC mount
                    if sub_path:
                        pvc_sub_path = f"{pod.tenant_id}/volumes/{source_id}/{sub_path}"
                    else:
                        pvc_sub_path = f"{pod.tenant_id}/volumes/{source_id}"
                    
                    volume_mounts.append(client.V1VolumeMount(
                        name=full_k8_name,
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
        "user": None
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = ports_dict)
