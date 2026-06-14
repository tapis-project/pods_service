"""
health - central
Only ran alongside main api/db/traefik pods.
Takes care of NFS health


Does the following:
1. Does startup work for NFS mount
2. Goes through NFS mount and does cleaning
3. Deals with traefik proxy, logs, and metrics

"""

import os
import socket
import subprocess
import time
import random
from datetime import datetime, timedelta
from channels import CommandChannel
from kubernetes import client, config
from kubernetes_utils import get_current_k8_services, get_current_k8_pods, rm_container, rm_pvc, \
     get_current_k8_pods, rm_service, KubernetesError, update_traefik_configmap, get_k8_logs, list_all_containers, run_k8_exec
from codes import AVAILABLE, DELETING, STOPPED, ERROR, REQUESTED, COMPLETE, RESTART, ON, OFF
from stores import pg_store, SITE_TENANT_DICT
from models_pods import Pod
from models_templates_utils import combine_pod_and_template_recursively
from models_volumes import Volume
from models_snapshots import Snapshot
from volume_utils import files_listfiles, files_delete, files_mkdir
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import BaseTapyException

logger = get_logger(__name__)


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def add_path(tree, path, file):
    # Function to create a tree of dictionaries that represent the file structure of the NFS system.
    path = path.replace(f"{conf.nfs_base_path}/", "", 1) # must delete {nfs_base_path}/ for parsing
    nodes = path.split('/')
    current = tree
    for node in nodes:
        if file['type'] == "dir":
            current = current.setdefault(node, {})
        elif file['type'] == "file":
            current = current.setdefault(node, file)
    return tree


def check_nfs_files():
    """Go through database for all tenants in this site. Go through all nfs files, ensure there are no files corresponding with
    items that are not in the database.
    """

    logger.info("Top of check_nfs_files.")

    # Get all files recursively in the nfs volume
    all_site_files = files_listfiles(path="/", recurse=True, base_path=conf.nfs_base_path)
    # Take all files and create a dictionary tree that's easier to parse.
    file_tree = {}
    for file in all_site_files:
        add_path(file_tree, file['path'], file)

    for tenant in SITE_TENANT_DICT[conf.site_id]:
        logger.info(f"Top of check_nfs_files for tenant: {tenant}.\n")
        ### Volumes
        # Go through database for tenant. Get all volumes
        tenant_volume_list = Volume.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_volume_dict = {}
        for volume in tenant_volume_list:
            # {volume_id: volume, ...}
            tenant_volume_dict[volume.volume_id] = volume

        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in volumes db.
        for folder in file_tree[tenant]['volumes'].keys():
            # Found match
            if tenant_volume_dict.get(folder):
                logger.info(f"Found match for folder: {folder}")
                pass
            # File doesn't match any entries in volumes db. We will delete it
            else:
                logger.warning(f"Couldn't find volume with name: {folder} in database: {tenant_volume_dict}. Deleting it now.\n")
                logger.debug(f"volume dict: {tenant_volume_dict}")
                logger.debug(f"volume files: {file_tree[tenant]['volumes']}")
                files_delete(path=f"/volumes/{folder}", tenant_id=tenant)

        ### Snapshots
        # Go through database for tenant. Get all snapshots
        tenant_snapshot_list = Snapshot.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_snapshot_dict = {}
        for snapshot in tenant_snapshot_list:
            # {snapshot_id: snapshot, ...}
            tenant_snapshot_dict[snapshot.snapshot_id] = snapshot
        
        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in snapshots db.
        for folder in file_tree[tenant]['snapshots'].keys():
            # Found match
            if tenant_snapshot_dict.get(folder):
                logger.info(f"Found match for folder: {folder}")
                pass
            # File doesn't match any entries in snapshots db. We will delete it
            else:
                logger.warning(f"Couldn't find snapshot with name: {folder} in database: {tenant_snapshot_dict}. Deleting it now.\n")
                logger.debug(f"snapshot dict: {tenant_snapshot_dict}")
                logger.debug(f"snapshot files: {file_tree[tenant]['snapshots']}")
                files_delete(path=f"/snapshots/{folder}", tenant_id=tenant)



def check_volume_sizes():
    """Measure NFS disk usage for all volumes and snapshots.

    Runs `du -sm` on each object's directory, updates the `size` field in the DB,
    writes a VolumeUsageLog entry, and emits a warning when over size_limit.
    No enforcement action is taken yet — alerts only.
    """
    from models_volume_usage import VolumeUsageLog

    def _measure_mb(path: str) -> float | None:
        if not os.path.exists(path):
            return None
        try:
            result = subprocess.run(
                ["du", "-sm", path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return float(result.stdout.split("\t")[0])
        except Exception as e:
            logger.warning(f"du failed for {path}: {e}")
        return None

    for tenant in SITE_TENANT_DICT[conf.site_id]:
        # ── volumes ───────────────────────────────────────────────────────────
        try:
            volumes = Volume.db_get_all(tenant=tenant, site=conf.site_id)
            for vol in volumes:
                path = os.path.join(conf.nfs_base_path, "volumes", vol.volume_id)
                size_mb = _measure_mb(path)
                if size_mb is None:
                    continue
                vol.size = int(size_mb)
                vol.db_update()
                VolumeUsageLog.log_measurement(
                    object_id=vol.volume_id,
                    object_type="volume",
                    tenant_id=tenant,
                    site_id=conf.site_id,
                    size_mb=size_mb,
                    size_limit_mb=float(vol.size_limit) if vol.size_limit else None,
                )
                VolumeUsageLog.purge_old(vol.volume_id, "volume", tenant, conf.site_id)
                if vol.size_limit and size_mb > float(vol.size_limit):
                    logger.warning(
                        f"Volume {vol.volume_id} ({tenant}) is OVER size limit: "
                        f"{size_mb:.1f} MB > {vol.size_limit} MB (no enforcement yet)"
                    )
                else:
                    logger.debug(f"Volume {vol.volume_id}: {size_mb:.1f} MB")
        except Exception as e:
            logger.warning(f"check_volume_sizes volumes error for tenant={tenant}: {e}")

        # ── snapshots ─────────────────────────────────────────────────────────
        try:
            snapshots = Snapshot.db_get_all(tenant=tenant, site=conf.site_id)
            for snap in snapshots:
                path = os.path.join(conf.nfs_base_path, "snapshots", snap.snapshot_id)
                size_mb = _measure_mb(path)
                if size_mb is None:
                    continue
                snap.size = int(size_mb)
                snap.db_update()
                VolumeUsageLog.log_measurement(
                    object_id=snap.snapshot_id,
                    object_type="snapshot",
                    tenant_id=tenant,
                    site_id=conf.site_id,
                    size_mb=size_mb,
                    size_limit_mb=float(snap.size_limit) if snap.size_limit else None,
                )
                VolumeUsageLog.purge_old(snap.snapshot_id, "snapshot", tenant, conf.site_id)
                if snap.size_limit and size_mb > float(snap.size_limit):
                    logger.warning(
                        f"Snapshot {snap.snapshot_id} ({tenant}) is OVER size limit: "
                        f"{size_mb:.1f} MB > {snap.size_limit} MB (no enforcement yet)"
                    )
        except Exception as e:
            logger.warning(f"check_volume_sizes snapshots error for tenant={tenant}: {e}")


def check_nfs_tapis_system():
    """Ensures nfs is up and tapis is connected to it.
    This central health instance needs to be in the same K8 namespace as api and pods-nfs(?).
    We grab the nfs ssh ip.
    """
    logger.info("Top of check_nfs_tapis_system.")

    # Get K8 pod name named pods-nfs
    k8_name = ""
    idx = 0
    while idx < 20:
        nfs_pods = []
        for k8_pod in list_all_containers(filter_str="pods-nfs"):
            k8_name = k8_pod.metadata.name
            # pods-nfs also matches pods-nfs-mkdir, so we manually pass that case
            if "pods-nfs-mkdirs" in k8_name:
                continue
            nfs_pods.append({'pod_info': k8_pod,
                                'k8_name': k8_name})
        # Checking how many services met the filter (should hopefully be only one)
        match len(nfs_pods):
            case 1:
                logger.info(f"Found one pod matching pods-nfs. Name: {nfs_pods[0]['k8_name']}")
                break
            case 0:
                logger.info(f"Couldn't find pod matching pods-nfs. Trying again.")
                pass
            case _:
                logger.info(f"Got >1 pods matching pods-nfs. Matching pods: {[pod['k8_name'] for pod in nfs_pods]}. Trying again.")
                pass
        # Increment and have a short wait
        idx += 1
        time.sleep(3)
    else:
        logger.error(f"Couldn't find pod matching pods-nfs after 20 tries. Exiting check_nfs_tapis_system.")
        return

    # k8_name could have been changed by now, so we need to set from nfs_pods.
    k8_name = nfs_pods[0]['k8_name']

    # Go through each tenant and initialize folders
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        # Logging for tenant initialization
        logger.info(f"Initializing nfs folders for tenant: {tenant}.")
        nfs_folder_init(tenant)


def nfs_folder_init(tenant):
    try:
        logger.info(f"Creating tenant root folder for {conf.site_id}.{tenant}.")
        # Ensure tenant root folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant root folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)

    try:
        logger.info(f"Creating tenant volumes folder for {conf.site_id}.{tenant}.")
        # Ensure tenant volumes folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/volumes", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant volumes folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)

    try:
        logger.info(f"Creating tenant snapshots folder for {conf.site_id}.{tenant}.")
        # Ensure tenant snapshots folder exists, this will not cause issues, even if storage is already in use.
        res = files_mkdir(path = "/snapshots", tenant_id=tenant)
    except Exception as e:
        msg = f"Error creating tenant snapshots folder for {conf.site_id}.{tenant}. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)


def _check_custom_domain_dns(custom_domain: str, pods_ingress: str) -> bool:
    """Return True if custom_domain resolves to the same IP as pods_ingress."""
    try:
        return socket.gethostbyname(custom_domain) == socket.gethostbyname(pods_ingress)
    except socket.gaierror:
        return False


def set_traefik_proxy():
    all_pods = []
    stmt = select(Pod)
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)

    ### Proxy ports and config changes
    # For proxy config later. proxy_info_x = {pod.k8_name: {routing_port, url}, ...} 
    tcp_proxy_info = {}
    http_proxy_info = {}
    postgres_proxy_info = {}
    for input_pod in all_pods:
        #logger.critical(f"TESTINGERROR-input_pod.tenant_id: {input_pod.tenant_id}, input_pod.site_id: {input_pod.site_id}")
        try: 
            pod = combine_pod_and_template_recursively(input_pod, input_pod.template, tenant=input_pod.tenant_id, site=input_pod.site_id)
        except Exception as e:
            logger.critical(f"Error combining pod and template. Skipping pod {input_pod.pod_id}. e: {e}")
            continue
        # Each pod can have up to 3 networking objects with custom filled port/protocol/name
        for net_name, net_info in pod.networking.items():
            if not isinstance(net_info, dict):
                net_info = net_info.dict()

            # name should be pods-tacc-tacc-mypod unless specified, if so
            # then something like pods-tacc-tacc-mypod-mynet
            if net_name != "default":
                traefik_service_name = f"{pod.k8_name}-{net_name}"
            else:
                traefik_service_name = pod.k8_name

            template_info = {"routing_port": net_info['port'],
                             "url": net_info['url'],
                             "k8_service": pod.k8_name}
            ## cors headers
            cors_info = {
                "cors_allow_origins": net_info.get('cors_allow_origins', []),
                "cors_allow_methods": net_info.get('cors_allow_methods', []),
                "cors_allow_headers": net_info.get('cors_allow_headers', []),
                "cors_allow_credentials": net_info.get('cors_allow_credentials', False),
                "cors_max_age": net_info.get('cors_max_age', 100),
            }
            ## tapis auth
            # The goal is: https://tacc.develop.tapis.io/v3/pods/{{pod_id}}/auth
            pod_id_section, tapis_domain = net_info['url'].split('.pods.') ## Should return `mypod` & `tacc.tapis.io` with proper tenant and schmu
            if '-' in pod_id_section:
                pod_id, network_section = pod_id_section.split('-') # e.g. `mypod-networking2` if there's several networking objects
            else:
                pod_id = pod_id_section
            forward_auth_info = {
                "tapis_auth": net_info.get('tapis_auth', False),
                "auth_url": f"https://{tapis_domain}/v3/pods/{pod_id}/auth",
                "tapis_auth_response_headers": net_info.get('tapis_auth_response_headers', {}),
                "tapis_auth_excluded_paths": net_info.get('tapis_auth_excluded_paths', []),
                "tapis_auth_excluded_path_regex": net_info.get('tapis_auth_excluded_path_regex', []),
            }
            ## ip allow list
            ip_allow_list_info = {
                "ip_allow_list": net_info.get('ip_allow_list', [])
            }
            ## proxy compression (defaults ensure backwards compat with old pods missing these fields)
            compression_info = {
                "proxy_compression": net_info.get('proxy_compression', True),
                "proxy_compression_encodings": net_info.get('proxy_compression_encodings', ['zstd', 'br', 'gzip']),
                "proxy_compression_excluded_content_types": list(set(
                    # Smart defaults: already-compressed formats where re-compression wastes CPU
                    ['application/gzip', 'application/zip', 'application/zstd', 'application/x-tar',
                     'application/x-bzip2', 'application/x-xz', 'application/x-7z-compressed',
                     'image/png', 'image/jpeg', 'image/webp', 'image/gif',
                     'video/mp4', 'video/webm', 'audio/mpeg', 'audio/ogg']
                    + net_info.get('proxy_compression_excluded_content_types', [])
                )),
                "proxy_compression_min_response_body_bytes": net_info.get('proxy_compression_min_response_body_bytes', 1024),
            }
            ## custom domain (BYOD) — only relevant for http protocol
            custom_domain = net_info.get('custom_domain', '')
            custom_domain_verified = net_info.get('custom_domain_verified', False)
            if custom_domain and net_info.get('protocol') == 'http':
                pods_ingress = 'pods.' + net_info['url'].split('.pods.', 1)[1]
                currently_verified = _check_custom_domain_dns(custom_domain, pods_ingress)
                if currently_verified != custom_domain_verified:
                    try:
                        raw_net = {k: (v.dict() if hasattr(v, 'dict') else dict(v)) for k, v in input_pod.networking.items()}
                        raw_net.setdefault(net_name, {})['custom_domain_verified'] = currently_verified
                        input_pod.networking = raw_net
                        input_pod.db_update(tenant=input_pod.tenant_id, site=input_pod.site_id, user_update=False)
                        logger.info(f"custom_domain_verified={currently_verified} for pod {pod.pod_id} net '{net_name}'")
                    except Exception as e:
                        logger.error(f"Failed to update custom_domain_verified for pod {pod.pod_id}: {e}")
                    custom_domain_verified = currently_verified
            custom_domain_info = {
                "custom_domain": custom_domain,
                "custom_domain_verified": custom_domain_verified,
            }
            logger.debug(f"pod_id: {pod_id}, tapis_domain: {tapis_domain}, net_info: {net_info}, traefik_forward_auth_info: {forward_auth_info}, cors_info: {cors_info}, ip_allow_list: {ip_allow_list_info}, compression_info: {compression_info}")
            match net_info['protocol']:
                case "tcp":
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    tcp_proxy_info[traefik_service_name] = template_info
                case "http":
                    # tapis auth
                    if forward_auth_info['tapis_auth']:
                        template_info.update(forward_auth_info)
                    # cors settings
                    if cors_info['cors_allow_origins']:
                        template_info.update(cors_info)
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    # proxy compression
                    template_info.update(compression_info)
                    # custom domain (BYOD)
                    template_info.update(custom_domain_info)
                    http_proxy_info[traefik_service_name] = template_info
                case "postgres":
                    # ip_allow_list
                    template_info.update(ip_allow_list_info)
                    postgres_proxy_info[traefik_service_name] = template_info
                case "local_only":
                    # when users only need networking to connect to other pods in the same namespace
                    pass

    # This functions only updates if config is out of date.
    update_traefik_configmap(tcp_proxy_info, http_proxy_info, postgres_proxy_info)


def main():
    """
    Main function for health checks.
    """
    # Try and run check_db_pods. Will try for 60 seconds until health is declared "broken".
    logger.info("Top of health central. Checking if db's are initialized.")
    idx = 0
    while idx < 12:
        try:
            #Volume.db_get_all(tenant="admin", site="tacc")
            check_nfs_tapis_system()

            check_nfs_files()
            logger.info("Successfully ran through check_nfs_files().")
            break
        except Exception as e:
            logger.info(f"Can't run check_nfs_files() yet idx: {idx}. e: {e.args}")
            # Increment and have a short wait
            idx += 1
            time.sleep(5)
    # Reached end of idx limit
    else:
        logger.critical("Health could not run check_nfs_files(). Shutting down!")
        return

    # Main health loop — tick counter drives low-frequency tasks
    _tick = 0
    _SIZE_CHECK_INTERVAL = 200  # every ~10 min (200 ticks × 3 s)
    while True:
        logger.info(f"\n\n\nRunning pods health checks. Now: {time.time()}")
        try:
            set_traefik_proxy()
        except Exception as e:
            logger.error(f"Error setting traefik proxy. e: {e}", exc_info=True)

        try:
            check_nfs_files()
        except Exception as e:
            logger.error(f"Error running check_nfs_files. e: {e}", exc_info=True)

        if _tick % _SIZE_CHECK_INTERVAL == 0:
            try:
                check_volume_sizes()
            except Exception as e:
                logger.error(f"Error running check_volume_sizes. e: {e}", exc_info=True)

        _tick += 1
        time.sleep(3)


if __name__ == '__main__':
    main()
