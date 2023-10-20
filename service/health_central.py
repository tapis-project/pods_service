"""
health - central
Only ran alongside main api/db/traefik pods.
Takes care of NFS health


Does the following:
1. Does startup work for NFS mount
2. Goes through NFS mount and does cleaning
3. Deals with traefik proxy, logs, and metrics

"""

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
from models_volumes import Volume
from models_snapshots import Snapshot
from volume_utils import files_listfiles, files_delete, get_nfs_ips, files_mkdir
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import BaseTapyException

from __init__ import t
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
                files_delete(path=f"{conf.nfs_base_path}/{tenant}/volumes/{folder}", tenant_id=tenant)

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

        ### TODO: Check volume size
        # For existing volumes, check the size of the folder and ensure it's below volume size max
        ## Don't know what to do with those quite yet though


def check_nfs_tapis_system():
    """Ensures nfs is up and tapis is connected to it.
    This central health instance needs to be in the same K8 namespace as api and pods-nfs(?).
    We grab the nfs ssh ip.
    """
    logger.info("Top of check_nfs_tapis_system. Getting nfs_ssh_ip.")

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

    nfs_ssh_ip, nfs_nfs_ip = get_nfs_ips()
    logger.info(f"In check_nfs_tapis_system. Got nfs_ssh_ip: {nfs_ssh_ip}.")

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
    for pod in all_pods:
        # Each pod can have up to 3 networking objects with custom filled port/protocol/name
        for net_name, net_info in pod.networking.items():
            if not isinstance(net_info, dict):
                net_info = net_info.dict()

            template_info = {"routing_port": net_info['port'],
                             "url": net_info['url']}
            match net_info['protocol']:
                case "tcp":
                    tcp_proxy_info[pod.k8_name] = template_info
                case "http":
                    http_proxy_info[pod.k8_name] = template_info
                case "postgres":
                    postgres_proxy_info[pod.k8_name] = template_info

    # This functions only updates if config is out of date.
    update_traefik_configmap(tcp_proxy_info, http_proxy_info, postgres_proxy_info)


def main():
    # Try and run check_db_pods. Will try for 60 seconds until health is declared "broken".
    logger.info("Top of health. Checking if db's are initialized.")
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

    # Main health loop
    while True:
        logger.info(f"Running pods health checks. Now: {time.time()}")
        check_nfs_files()
        
        set_traefik_proxy()
        ### Have a short wait
        time.sleep(3)


if __name__ == '__main__':
    main()
