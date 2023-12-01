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
     get_current_k8_pods, rm_service, KubernetesError, get_k8_logs, list_all_containers, run_k8_exec
from codes import AVAILABLE, DELETING, STOPPED, ERROR, REQUESTED, COMPLETE, RESTART, ON, OFF
from stores import pg_store, SITE_TENANT_DICT
from models_pods import Pod
from models_volumes import Volume
from models_snapshots import Snapshot
from psycopg2 import ProgrammingError
from sqlmodel import select
from tapisservice.config import conf
from tapisservice.logs import get_logger

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

def graceful_rm_pod(pod, log = None):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete pod, delete service, and change traefik to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    logger.info(f"Top of shutdown pod for pod: {pod.k8_name}")
    # Change pod status to SHUTTING DOWN
    pod.status = DELETING
    pod.db_update(log)
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
    """
    Check the health of Kubernetes pods.
    Only for the site specified in conf.site_id.
    Each site should get it's own health pod.
    Go through live containers first as base "truth". Set database from that info. (error if needed, update statuses)
    
    Args:
        k8_pods (list): A list of Kubernetes pods to check.

    Returns:
        None
    """

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

        # We don't run these checks on pods with status_requested = OFF as they're going through tear down stuff.
        if pod.status_requested in [OFF, RESTART]:
            continue

        # This is actually bad. Means the pod has stopped, which shouldn't be the case.
        # We'll put pod in error state with message.
        if k8_pod_phase == "Succeeded":
            logger.warning(f"Kube pod in succeeded phase.")
            status_container['message'] = "Pod phase in Succeeded, putting in COMPLETE status."
            pod.status_container = status_container
            pod.status = COMPLETE
            # We update if there's been a change.
            if pod != pre_health_pod:
                pod.db_update(f"health found pod in succeeded, set status to COMPLETE")
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
                        pod.db_update(f"health found pod in waiting state, set status to ERROR")
                    continue
                elif c_state.terminated:
                    logger.critical(f"Kube pod in terminated state. msg:{c_state.terminated.message}; reason: {c_state.terminated.reason}")
                    status_container['message'] = f"Pod in terminated state for reason: {c_state.terminated.message}."
                    pod.status_container = status_container
                    pod.status = ERROR
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        # Get logs for pod if it's being updated here as something must have changed.
                        logs = get_k8_logs(k8_pod['k8_name'])
                        pod.logs = logs
                        pod.db_update(f"health found pod in terminated state, set status to ERROR")
                    continue
                elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
                    logger.info(f"Kube pod in waiting state, still creating container.")
                    status_container['message'] = "Pod is still initializing."
                    pod.status_container = status_container
                    # We update if there's been a change.
                    if pod != pre_health_pod:
                        pod.db_update() # no logs needed, spawner already states it's being put in creating.
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
                        pod.db_update(f"health set status to AVAILABLE")
            else:
                # Not sure if this is possible/what happens here.
                # There is definitely an Error state. Can't replicate locally yet.
                logger.critical(f"NO c_state. {k8_pod['pod_info'].status}")

        # Getting here means pod is running. Store logs now.
        logs = get_k8_logs(k8_pod['k8_name'])
        if pod.logs != logs:
            pod.logs = logs
            pod.db_update() # just adding logs, no action_logs needed.

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
    """Go through database for all tenants in this site. Delete/Create whatever is needed.
    """
    all_pods = []
    stmt = select(Pod)
    failed_tenants = []
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        try:
            all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)
        except ProgrammingError as e:
            logger.warning(f"Tenant: {tenant} not found in database. Skipping.")
            failed_tenants.append(tenant)
            continue
    # If > 2/20 tenants fail we'll skip, expecting up to two new tenants.
    # Pods needs to restart after new tenants are added for their database to be created.
    # It should not break currently working health though. Thus skipping if only a small portion of tenants fail.
    if len(failed_tenants) >= 2:
        logger.critical(f"More than 2 tenants failed to connect to database. Possible error or waiting for startup. Shutting down.")
        return


    ### Go through all pod entries in the database
    for pod in all_pods:
        ### Delete pods with status_requested = OFF or RESTART
        if pod.status_requested in [OFF, RESTART] and pod.status != STOPPED:
            logger.info(f"pod_id: {pod.pod_id} found with status_requested: {pod.status_requested} and not STOPPED. Gracefully shutting pod down.")
            container_exists, service_exists = graceful_rm_pod(pod, f"health found running {pod.status_requested} pod, set status to DELETING") # SHOULD ONLY LOG ONCE!!!
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
                    pod.db_update(f"health set status to STOPPED, set to ON")
                else:
                    pod.db_update(f"health set status to STOPPED")

        ### DB entries without a running pod should be updated to STOPPED.
        if pod.status_requested in ['ON'] and pod.status in [AVAILABLE, DELETING, REQUESTED]:
            k8_pod_found = False
            for k8_pod in k8_pods:
                if pod.pod_id in k8_pod['pod_id']:
                    k8_pod_found = True

            if not k8_pod_found:
                # Check action_logs for proper course of action
                if not pod.action_logs:
                    # logs can be empty if an admin manually deleted them or if we ran a db migration. Accounting for that here.
                    log_str = "No action logs found. Expecting to go to 'else' to be shutdown"
                    time_difference = timedelta(minutes=5) # This line exists to stop linter complaints
                else:
                    # We let pods in CREATING or REQUESTED have timeout of 3 minutes before we stop the pod
                    # and let health try again. We check time based on pod action_logs.
                    # Get the most recent log and split on ': ' to get the time, log_str
                    log_time_str, log_str = pod.action_logs[-1].split(': ', maxsplit=1)
                    most_recent_log_time = datetime.strptime(log_time_str, '%y/%m/%d %H:%M')
                    time_difference = datetime.utcnow() - most_recent_log_time

                # We check pod logs to see if pod is in a state where it should have a 3 minute timeout
                if "set status to REQUESTED" in log_str or \
                    "set status to CREATING" in log_str or \
                    "Pod object created by" in log_str:
                    # If pod has been in state for 3 minutes we'll stop it (Note 3+1 allows a 1 minute buffer as a log can be written at :59 seconds)
                    if time_difference > timedelta(minutes=3+1):
                        initial_pod_status = pod.status
                        logger.info(f"pod_id: {pod.pod_id} found with no running pods and in {initial_pod_status} for 3 minutes. Setting status = STOPPED")
                        pod.status = STOPPED
                        pod.start_instance_ts = None
                        pod.time_to_stop_ts = None
                        pod.time_to_stop_instance = None
                        pod.status_container = {}
                        pod.db_update(f"health found no running pod and status = {initial_pod_status} for 3 minutes, stalled. Setting status = STOPPED")
                    else:
                        # Not stalled yet, we just continue
                        continue
                else:                 
                    logger.info(f"pod_id: {pod.pod_id} found with no running pods. Setting status = STOPPED.")
                    pod.status = STOPPED
                    pod.start_instance_ts = None
                    pod.time_to_stop_ts = None
                    pod.time_to_stop_instance = None
                    pod.status_container = {}
                    pod.db_update(f"health found no running pod, set status to STOPPED")

        ### Sets pods to status_requested = OFF when current time > time_to_stop_ts.
        if pod.status_requested in ['ON'] and pod.time_to_stop_ts and pod.time_to_stop_ts < datetime.utcnow():
            logger.info(f"pod_id: {pod.pod_id} time_to_stop trigger passed. Current time: {datetime.utcnow()} > time_to_stop_ts: {pod.time_to_stop_ts}")
            pod.status_requested = OFF
            pod.db_update(f"health set pod to OFF due to time_to_stop trigger")
        
        ### Start pods here by putting command setting status="REQUESTED", if status_requested = ON and status = STOPPED.
        if pod.status_requested in ['ON', RESTART] and pod.status == STOPPED:
            logger.info(f"pod_id: {pod.pod_id} found status_requested: {pod.status_requested} and STOPPED. Starting.")
            original_pod_status = pod.status_requested
            if pod.status_requested == RESTART:
                logger.info(f"pod_id: {pod.pod_id} in RESTART and STOPPED, so switching status_requested back to ON.")
                pod.status_requested = ON

            pod.status = REQUESTED
            pod.db_update(f"health found {original_pod_status} pod set to STOPPED, set status to REQUESTED")

            # Send command to start new pod
            ch = CommandChannel(name=pod.site_id)
            ch.put_cmd(object_id=pod.pod_id,
                       object_type="pod",
                       tenant_id=pod.tenant_id,
                       site_id=pod.site_id)
            ch.close()
            logger.debug(f"Command Channel - Added msg for pod_id: {pod.pod_id}.")


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
            logger.info(f"Can't connect to dbs yet idx: {idx}. e: {e.orig}") # args: {e.args} # add e.args for more detail
            # Health seems to take a few seconds to come up (due to database creation and api creation)
            # Increment and have a short wait
            idx += 1
            time.sleep(5)
    # Reached end of idx limit
    else:
        logger.critical("Health could not connect to databases. Shutting down!")
        return

    # Main health loop
    while True:
        logger.info(f"Running pods health checks. Now: {time.time()}")
        k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}

        check_k8_pods(k8_pods)
        check_k8_services()
        check_db_pods(k8_pods)

        ### Have a short wait
        time.sleep(3)


if __name__ == '__main__':
    main()
