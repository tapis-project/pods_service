"""
Does the following:
1. Go through running k8 pods
  a. Remove pods that are not in the database (dangling)
  b. Else update the database based on information from the pod
    - Update logs
    - Update status
      - If pod is in Completed, put in Completed
      - If pod is in Error, put in Error
      - If pod is in Running, put in Running
    - Update status_container
  c. Clean up dangling services.

2. Go through database. 
  a. Deletes pods with status_requested = OFF
     i. Delete pods with status_requested = OFF. Then sets status_requested = ON.
  b. Deletes pods in error status. (Maybe not?)
  c. Updates pod status.
  d. Ensures all database pods exist (For this site)
  e. Ensures all pods that exist are in the database.
    i. Also checks that pods are healthy and communicating

3. Enforce ttl for pods.

4. Look through S3? Prune old things?/Prune non-existing pods.

5. Check that spawner is alive? Create if needed?
  a. Maybe have a warning slack message if none available?

Running mode, either:
1. Periodically run script.
2. Always keep running in big loop.
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
from volume_utils import files_listfiles, files_delete, get_nfs_ips
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapipy.errors import BaseTapyException

from __init__ import t
logger = get_logger(__name__)


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def rm_pod(k8_name):
    container_exists = True
    service_exists = True
    try:
        rm_container(k8_name)
    except KubernetesError:
        # container not found
        container_exists = False
        pass
    try:
        rm_service(k8_name)
    except KubernetesError:
        # service not found
        service_exists = False
        pass

    return container_exists, service_exists

def rm_volume(k8_name):
    volume_exists = True
    try:
        rm_pvc(k8_name)
    except KubernetesError:
        # volume not found
        volume_exists = False
        pass

    return volume_exists

def graceful_rm_pod(pod):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete pod, delete service, and change traefik to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    logger.info(f"Top of shutdown pod for pod: {pod.k8_name}")
    # Change pod status to SHUTTING DOWN
    pod.status = DELETING
    pod.db_update()
    logger.debug(f"spawner has updated pod status to DELETING")

    return rm_pod(pod.k8_name)

def graceful_rm_volume(volume):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete volume, delete volume, and change traefik to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    logger.info(f"Top of shutdown volume for volume: {volume.k8_name}")
    # Change pod status to SHUTTING DOWN
    volume.status = DELETING
    volume.db_update()
    logger.debug(f"spawner has updated volume status to DELETING")

    return rm_volume(volume.k8_name)

def check_k8_pods(k8_pods):
    # This is all for only the site specified in conf.site_id.
    # Each site should get it's own health pod.
    # Go through live containers first as it's "truth". Set database from that info. (error if needed, update statuses)

    # Check each pod.
    for k8_pod in k8_pods:
        logger.info(f"Checking pod health for pod_id: {k8_pod['pod_id']}")

        # Check if pod is found in database.
        pod = Pod.db_get_with_pk(k8_pod['pod_id'], k8_pod['tenant_id'], k8_pod['site_id'])
        # We've found a pod without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 pod without any database entry. Deleting. Pod: {k8_pod['k8_name']}")
            rm_pod(k8_pod['k8_name'])
            continue
        
        pre_health_pod = pod.copy()

        # Found pod in db.
        # Add last_health_check attr.
        # TODO We could try and make a get to the pod to check if it's actually alive.

        k8_pod_phase = k8_pod['pod_info'].status.phase
        status_container = {"phase": k8_pod_phase,
                            "start_time": k8_pod['pod_info'].status.start_time.isoformat().replace('+00:00', '.000000'),
                            "message": ""}
        
        # Get pod container state
        # We try to get c_state. c_state when pending is None for a bit.
        try:
            c_state = k8_pod['pod_info'].status.container_statuses[0].state
        except:
            c_state = None
        logger.debug(f'state: {c_state}')

        # This is actually bad. Means the pod has stopped, which shouldn't be the case.
        # We'll put pod in error state with message.
        if k8_pod_phase == "Succeeded":
            logger.warning(f"Kube pod in succeeded phase.")
            status_container['message'] = "Pod phase in Succeeded, putting in COMPLETE status."
            pod.status_container = status_container
            pod.status = COMPLETE
            # We update if there's been a change.
            if pod != pre_health_pod:
                pod.db_update()
            continue
        elif k8_pod_phase in ["Running", "Pending", "Failed"]:
            # Check if container running or in error state
            # Container can be in waiting state due to ContainerCreating ofc
            if c_state:
                if c_state.waiting and c_state.waiting.reason != "ContainerCreating":
                    logger.critical(f"Kube pod in waiting state. msg:{c_state.waiting.message}; reason: {c_state.waiting.reason}")
                    status_container['message'] = f"Pod in waiting state for reason: {c_state.waiting.message}."
                    pod.status_container = status_container
                    pod.status = ERROR
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        pod.db_update()
                    continue
                elif c_state.terminated:
                    logger.critical(f"Kube pod in terminated state. msg:{c_state.terminated.message}; reason: {c_state.terminated.reason}")
                    status_container['message'] = f"Pod in terminated state for reason: {c_state.terminated.message}."
                    pod.status_container = status_container
                    pod.status = ERROR
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        pod.db_update()
                    continue
                elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
                    logger.info(f"Kube pod in waiting state, still creating container.")
                    status_container['message'] = "Pod is still initializing."
                    pod.status_container = status_container
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        pod.db_update()
                    continue
                elif c_state.running:
                    status_container['message'] = "Pod is running."
                    pod.status_container = status_container
                    # This is the first time pod is in AVAILABLE. Update start_instance_ts.
                    if pod.status != AVAILABLE:
                        pod.start_instance_ts = datetime.utcnow()
                        pod.status = AVAILABLE

                    if pod.start_instance_ts:
                        # This will set time_to_stop_ts the first time pod is available and if
                        # time_to_stop_instance or time_to_stop_default is updated. 
                        if isinstance(pod.time_to_stop_instance, int):
                            # If set to -1, we don't do ttl.
                            if not pod.time_to_stop_instance == -1:
                                pod.time_to_stop_ts = pod.start_instance_ts + timedelta(seconds=pod.time_to_stop_instance)
                        else:
                            # If set to -1, we don't do ttl.
                            if not pod.time_to_stop_default == -1:
                                pod.time_to_stop_ts = pod.start_instance_ts + timedelta(seconds=pod.time_to_stop_default)
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        pod.db_update()
            else:
                # Not sure if this is possible/what happens here.
                # There is definitely an Error state. Can't replicate locally yet.
                logger.critical(f"NO c_state. {k8_pod['pod_info'].status}")

        # Getting here means pod is running. Store logs now.
        logs = get_k8_logs(k8_pod['k8_name'])
        if pod.logs != logs:
            pod.logs = logs
            pod.db_update()

def check_k8_services():
    # This is all for only the site specified in conf.site_id.
    # Each site should get it's own health pod.
    # Go through live containers first as it's "truth". Set database from that info. (error if needed, update statuses)
    k8_services = get_current_k8_services() # Returns {service_info, site, tenant, pod_id}

    # Check each service.
    for k8_service in k8_services:
        logger.info(f"Checking service health for pod_id: {k8_service['pod_id']}")

        # Check for found service in database.
        pod = Pod.db_get_with_pk(k8_service['pod_id'], k8_service['tenant_id'], k8_service['site_id'])
        # We've found a service without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 service without any database entry. Deleting. Service: {k8_service['k8_name']}")
            rm_pod(k8_service['k8_name'])
            continue

def check_db_pods(k8_pods):
    """Go through database for all tenants in this site. Delete/Create whatever is needed. Do proxy config stuff.
    """
    all_pods = []
    stmt = select(Pod)
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)

    ### Go through all pod entries in the database
    for pod in all_pods:
        ### Delete pods with status_requested = OFF or RESTART
        if pod.status_requested in [OFF, RESTART] and pod.status != STOPPED:
            logger.info(f"pod_id: {pod.pod_id} found with status_requested: {pod.status_requested}. Gracefully shutting pod down.")
            container_exists, service_exists = graceful_rm_pod(pod)
            # if container and service not alive. Update status to STOPPED. UPDATE RESTART to ON.
            if not container_exists and not service_exists:
                logger.info(f"pod_id: {pod.pod_id} found with container and service stopped. Moving to status = STOPPED.")
                pod.status = STOPPED
                pod.start_instance_ts = None
                pod.time_to_stop_ts = None
                pod.time_to_stop_instance = None
                pod.status_container = {}
                if pod.status_requested == RESTART:
                    logger.info(f"pod_id: {pod.pod_id} in RESTART. Now in STOPPED, so switching status_requested back to ON.")
                    pod.status_requested = ON
                pod.db_update()
        
        ### DB entries without a running pod should be updated to STOPPED.
        if pod.status_requested in ['ON'] and pod.status in [AVAILABLE, DELETING]:
            k8_pod_found = False
            for k8_pod in k8_pods:
                if pod.pod_id in k8_pod['pod_id']:
                    k8_pod_found = True
            if not k8_pod_found:
                logger.info(f"pod_id: {pod.pod_id} found with no running pods. Setting status = STOPPED.")
                pod.status = STOPPED
                pod.start_instance_ts = None
                pod.time_to_stop_ts = None
                pod.time_to_stop_instance = None
                pod.status_container = {}
                pod.db_update()

        ### Sets pods to status_requested = OFF when current time > time_to_stop_ts.
        if pod.status_requested in ['ON'] and pod.time_to_stop_ts and pod.time_to_stop_ts < datetime.utcnow():
            logger.info(f"pod_id: {pod.pod_id} time_to_stop trigger passed. Current time: {datetime.utcnow()} > time_to_stop_ts: {pod.time_to_stop_ts}")
            pod.status_requested = OFF
            pod.db_update()
        
        ### Start pods here by putting command setting status="REQUESTED", if status_requested = ON and status = STOPPED.
        if pod.status_requested in ['ON'] and pod.status == STOPPED:
            logger.info(f"pod_id: {pod.pod_id} found status_requested = ON and status = STOPPED. Starting.")

            pod.status = REQUESTED
            pod.db_update()

            # Send command to start new pod
            ch = CommandChannel(name=pod.site_id)
            ch.put_cmd(object_id=pod.pod_id,
                       object_type="pod",
                       tenant_id=pod.tenant_id,
                       site_id=pod.site_id)
            ch.close()
            logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")


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

def check_nfs_files():
    """Go through database for all tenants in this site. Go through all nfs files, ensure there are no files corresponding with
    items that are not in the database.
    """

    logger.info("Top of check_nfs_files.")

    #all_site_files = files_listfiles(system_id=conf.nfs_tapis_system_id, path="/volumes", tenant_id="siteadmintable")

    for tenant in SITE_TENANT_DICT[conf.site_id]:
        logger.info(f"Top of check_nfs_files for tenant: {tenant}.\n")
        ### Volumes
        # Get all folders in the pods nfs volume folder
        try:
            tenant_volume_files = files_listfiles(system_id=conf.nfs_tapis_system_id, path="/volumes", tenant_id=tenant)
        except Exception as e:
            logger.error(f"Error getting /volumes from tenant: {tenant}. Error: {e}")
            tenant_volume_files = []
            continue

        # Go through database for tenant. Get all volumes
        tenant_volume_list = Volume.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_volume_dict = {}
        for volume in tenant_volume_list:
            # {volume_id: volume, ...}
            tenant_volume_dict[volume.volume_id] = volume

        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in volumes db.
        for file in tenant_volume_files:
            # Found match
            if tenant_volume_dict.get(file.name):
                logger.info(f"Found match for file: {file.name}")
                pass
            # File doesn't match any entries in volumes db. We will delete it
            else:
                logger.warning(f"Couldn't find volume with name: {file.name} in database: {tenant_volume_dict}. Deleting it now.\n")
                logger.debug(f"volume dict: {tenant_volume_dict}")
                logger.debug(f"volume files: {tenant_volume_files}")
                files_delete(system_id=conf.nfs_tapis_system_id, path=f"/volumes/{file.name}", tenant_id=tenant)

        ### Snapshots
        # Get all folders in the pods nfs snapshots folder
        try:
            tenant_snapshot_files = files_listfiles(system_id=conf.nfs_tapis_system_id, path="/snapshots", tenant_id=tenant)
        except Exception as e:
            logger.error(f"Error getting /snapshots from tenant: {tenant}. Error: {e}")
            tenant_snapshot_files = []
            continue

        # Go through database for tenant. Get all snapshots
        tenant_snapshot_list = Snapshot.db_get_all(tenant=tenant, site=conf.site_id)
        tenant_snapshot_dict = {}
        for snapshot in tenant_snapshot_list:
            # {snapshot_id: snapshot, ...}
            tenant_snapshot_dict[snapshot.snapshot_id] = snapshot
        
        # Go through all files entries in the tenant, looking for excess files. Ones who don't have entry in snapshots db.
        for file in tenant_snapshot_files:
            # Found match
            if tenant_snapshot_dict.get(file.name):
                logger.info(f"Found match for file: {file.name}")
                pass
            # File doesn't match any entries in snapshots db. We will delete it
            else:
                logger.warning(f"Couldn't find snapshot with name: {file.name} in database: {tenant_snapshot_dict}. Deleting it now.\n")
                logger.debug(f"snapshot dict: {tenant_snapshot_dict}")
                logger.debug(f"snapshot files: {tenant_snapshot_files}")
                files_delete(system_id=conf.nfs_tapis_system_id, path=f"/snapshots/{file.name}", tenant_id=tenant)

        ### TODO: Check volume size
        # For existing volumes, check the size of the folder and ensure it's below volume size max
        ## Don't know what to do with those quite yet though


def check_nfs_tapis_system():
    """Ensures nfs is up and tapis is connected to it.
    This health instance needs to be in the same K8 space as files.
    We grab the nfs ssh ip, provide it to systems, then we use files to mess with files, getting
    sharing, uploading, etc for "free".
    """
    logger.info("Top of check_nfs_tapis_system. Getting nfs_ssh_ip, creating system, and adding credential for all tenants.")

    # Check config to see if we should even run this.
    nfs_develop_mode = conf.nfs_develop_mode
    nfs_develop_remote_url = conf.get('nfs_develop_remote_url')
    nfs_develop_private_key = conf.get('nfs_develop_private_key')
    nfs_develop_public_key = conf.get('nfs_develop_public_key')

    remote_run = False
    if nfs_develop_mode:
        if nfs_develop_remote_url and nfs_develop_private_key and nfs_develop_public_key:
            remote_run = True
            logger.info("nfs_develop_mode is True and found remote_url, private_key, and public_key. Running check_nfs_tapis_system remotely.")
        else:
            logger.error("nfs_develop_mode is True but missing remote_url, private_key, or public_key. Leaving check_nfs_tapis_system.")
            return

    # If it's not a remote run then we need to either use config or derive keys from local Kubernetes environment
    if not remote_run:
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
                    logger.info(f"Found pod matching pods-nfs: {k8_name}")
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

        # We must either get PKI info from environment variables or derive info from pod.
        # Only grab keys from pod if remote_run = False
        # Attempt to derive PKI keys through k8 exec if not explicitly set in conf.
        if not nfs_develop_private_key and not nfs_develop_public_key:
            # Get private key from pod
            command = ["/bin/sh", "-c", "awk -v ORS='\\n' '1' /home/pods/.ssh/podskey"]
            derived_private_key, derived_err = run_k8_exec(k8_name, command)
            if derived_err:
                logger.error(f"Error deriving private key from nfs pod: {derived_err}")
                raise Exception(f"Error deriving private key from nfs pod during startup: {derived_err}")

            # Get public key from pod
            command = ["/bin/sh", "-c", "awk -v ORS='\\n' '1' /home/pods/.ssh/podskey.pub"]
            derived_public_key, derived_err = run_k8_exec(k8_name, command)
            if derived_err:
                logger.error(f"Error deriving public key from nfs pod: {derived_err}")
                raise Exception(f"Error deriving public key from nfs pod during startup: {derived_err}")

            # We successfully got both keys, we'll now use these derived ones.
            if derived_private_key and derived_public_key:
                logger.info(f"Successfully derived public and private keys from nfs pod. Using derived keys.")#\n\n{derived_public_key}\n\n{derived_private_key}\n")
                nfs_develop_private_key = derived_private_key
                nfs_develop_public_key = derived_public_key

    system_id = conf.nfs_tapis_system_id
    nfs_ssh_ip, nfs_nfs_ip = get_nfs_ips()
    logger.info(f"In check_nfs_tapis_system. Got nfs_ssh_ip: {nfs_ssh_ip}.")

    # Go through each tenant and create system
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        # Logging for tenant initialization
        logger.info(f"Initializing nfs for tenant: {tenant}.")
        root_dir = f"{conf.nfs_base_path}/{tenant}"
        create_tapis_system_and_creds_and_init(system_id, nfs_ssh_ip, root_dir, tenant, nfs_develop_private_key, nfs_develop_public_key, folder_init=True)

        # Our service tenant gets an extra system so health can inspect the entire nfs directory in one shot,
        # rather than getting files in each tenant's directory.
        if tenant == conf.service_tenant_id:
            system_id = f"{system_id}-admin"
            root_dir = f"{conf.nfs_base_path}"
            create_tapis_system_and_creds_and_init(system_id, nfs_ssh_ip, root_dir, tenant, nfs_develop_private_key, nfs_develop_public_key, folder_init=False)


def create_tapis_system_and_creds_and_init(system_id, nfs_ssh_ip, root_dir, tenant, private_key, public_key, folder_init=True):
    # System definition which we will "create" indiscriminately. If it already exists, we catch
    # the error and attempt a put to ensure definition is up-to-date for each tenant.
    system = {
        "id": system_id,
        "description": "Pods nfs system, located in the same k8 namespace as files. Pod named 'pods-nfs' set host to 'k get service pods-nfs-ssh' clusterIp fyi.",
        "systemType": "LINUX",
        "host": nfs_ssh_ip, # direct ip is absolutely required. Tried talking to Steve, it's needed.
        "defaultAuthnMethod": "PKI_KEYS",
        "effectiveUserId": "pods",
        "port": 22,
        "rootDir": root_dir,
        "canExec": False
    }

    # Create system, put if it already exists.
    try:
        res = t.systems.createSystem(
            systemId=system_id,
            **system,
            _x_tapis_tenant=tenant,
            _x_tapis_user='pods')
    except BaseTapyException as e:
        # System already exists, we'll just t.systems.putSystem (putSystem instead of patchSystem
        # as we'll use same input as createSystem).
        if "System already exists." in e.message:
            logger.info(f"Pods' nfs Tapis system already exists, running putSystem instead of createSystem")
            try:
                res = t.systems.putSystem(
                    **system,
                    systemId=system_id,
                    _x_tapis_tenant=tenant,
                    _x_tapis_user='pods')
            except BaseTapyException as e:
                msg = f"Error when running t.systems.putSystem. {e}"
                logger.info(msg)
                raise BaseTapyException(msg)
    try:
        # Log credential creation
        logger.info(f"Creating credential for {conf.site_id}.{tenant}.")
        # Create credential
        cred = t.systems.createUserCredential(
            systemId=system_id,
            userName='pods',
            privateKey=private_key, # If you want the `defaultAuthnMethod` of `PASSWORD` -> password=conf.nfs_pods_user_password
            publicKey=public_key,
            _x_tapis_tenant=tenant,
            _x_tapis_user='pods')
    except Exception as e:
        msg = f"Error creating credential for {conf.site_id}.{tenant} {system_id} system. e: {e}"
        logger.critical(msg)
        raise BaseTapyException(msg)

    if folder_init:
        try:
            logger.info(f"Creating tenant root folder for {conf.site_id}.{tenant}.")
            # Ensure tenant root folder exists, this will not cause issues even if volume is already in use.
            t.files.mkdir(
                systemId = system_id,
                path = "/",
                _x_tapis_tenant=tenant,
                _x_tapis_user='pods')
        except Exception as e:
            msg = f"Error creating tenant root folder for {conf.site_id}.{tenant} {system_id} system. e: {e}"
            logger.critical(msg)
            raise BaseTapyException(msg)

        try:
            logger.info(f"Creating tenant volumes folder for {conf.site_id}.{tenant}.")
            # Ensure tenant volumes folder exists, this will not cause issues even if volume is already in use.
            t.files.mkdir(
                systemId = system_id,
                path = "/volumes",
                _x_tapis_tenant=tenant,
                _x_tapis_user='pods')
        except Exception as e:
            msg = f"Error creating tenant volumes folder for {conf.site_id}.{tenant} {system_id} system. e: {e}"
            logger.critical(msg)
            raise BaseTapyException(msg)

        try:
            logger.info(f"Creating tenant snapshots folder for {conf.site_id}.{tenant}.")
            # Ensure tenant snapshots folder exists, this will not cause issues even if volume is already in use.
            t.files.mkdir(
                systemId = system_id,
                path = "/snapshots",
                _x_tapis_tenant=tenant,
                _x_tapis_user='pods')
        except Exception as e:
            msg = f"Error creating tenant snapshots folder for {conf.site_id}.{tenant} {system_id} system. e: {e}"
            logger.critical(msg)
            raise BaseTapyException(msg)


def main():
    # Try and run check_db_pods. Will try for 60 seconds until health is declared "broken".
    logger.info("Top of health. Checking if db's are initialized.")
    idx = 0
    while idx < 12:
        try:
            k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}
            check_db_pods(k8_pods)
            logger.info("Successfully connected to dbs.")
            break
        except Exception as e:
            logger.info(f"Can't connect to dbs yet idx: {idx}. e: {e}")
            # Health seems to take a few seconds to come up (due to database creation and api creation)
            # Increment and have a short wait
            idx += 1
            time.sleep(5)
    # Reached end of idx limit
    else:
        logger.critical("Health could not connect to databases. Shutting down!")
        return

    # We run nfs setup if development mode = true or if all develop configs are set.
    if conf.get('nfs_develop_remote_url') and conf.get('nfs_develop_private_key') and conf.get('nfs_develop_public_key'):
        check_nfs_tapis_system()
    elif not conf.nfs_develop_mode:
        check_nfs_tapis_system()

    # Main health loop
    while True:
        logger.info(f"Running pods health checks. Now: {time.time()}")
        k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}

        check_k8_pods(k8_pods)
        check_k8_services()
        check_db_pods(k8_pods)

        # We do not want health checks on remote environment when developing locally
        if not conf.nfs_develop_mode:
            check_nfs_files()

        ### Have a short wait
        time.sleep(1)


if __name__ == '__main__':
    main()
