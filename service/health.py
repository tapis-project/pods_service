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
from kubernetes import client, config
from kubernetes_utils import get_current_k8_services, get_current_k8_pods, rm_container, \
    get_current_k8_pods, rm_service, KubernetesError, update_nginx_configmap, get_k8_logs
from codes import RUNNING, SHUTTING_DOWN, STOPPED, ERROR, COMPLETE, RESTART, ON, OFF
from stores import pg_store, SITE_TENANT_DICT
from models import Pod, ExportedData
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

def graceful_rm_pod(pod):
    """
    This is async. Commands run, but deletion takes some time.
    Needs to delete pod, delete service, and change caddy to "offline" response.
    TODO Set status to shutting down. Something else will put into "STOPPED".
    """
    logger.info(f"Top of shutdown pod for pod: {pod.k8_name}")
    # Change pod status to SHUTTING DOWN
    pod.status = SHUTTING_DOWN
    pod.db_update()
    logger.debug(f"spawner has updated pod status to SHUTTING_DOWN")

    return rm_pod(pod.k8_name)

def check_k8_pods():
    # This is all for only the site specified in conf.site_id.
    # Each site should get it's own health pod.
    # Go through live containers first as it's "truth". Set database from that info. (error if needed, update statuses)
    k8_pods = get_current_k8_pods() # Returns {pod_info, site, tenant, pod_id}

    # Check each pod.
    for k8_pod in k8_pods:
        logger.info(f"Checking pod health for pod_id: {k8_pod['pod_id']}")

        # Check for found pod in database.
        pod = Pod.db_get_with_pk(k8_pod['pod_id'], k8_pod['tenant_id'], k8_pod['site_id'])
        # We've found a pod without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 pod without any database entry. Deleting. Pod: {k8_pod['k8_name']}")
            rm_pod(k8_pod['k8_name'])
            continue
        
        # Found pod in db.
        # TODO Update status in db.
        # Add last_health_check attr.
        # Delete pods when their status_requested = OFF or RESTART.
        # TODO We could try and make a get to the pod to check if it's actually alive.

        k8_pod_phase = k8_pod['pod_info'].status.phase
        status_container = {"phase": k8_pod_phase,
                            "start_time": str(k8_pod['pod_info'].status.start_time),
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
                    pod.db_update()
                    continue
                elif c_state.terminated:
                    logger.critical(f"Kube pod in terminated state. msg:{c_state.terminated.message}; reason: {c_state.terminated.reason}")
                    status_container['message'] = f"Pod in terminated state for reason: {c_state.terminated.message}."
                    pod.status_container = status_container
                    pod.status = ERROR
                    pod.db_update()
                    continue
                elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
                    logger.info(f"Kube pod in waiting state, still creating container.")
                    status_container['message'] = "Pod is still initializing."
                    pod.status_container = status_container
                    pod.db_update()
                    continue
                elif c_state.running:
                    status_container['message'] = "Pod is running."
                    pod.status_container = status_container
                    pod.status = RUNNING
                    pod.db_update()
            else:
                # Not sure if this is possible/what happens here.
                # There is definitely an Error state. Can't replicate locally yet.
                logger.critical(f"NO c_state. {k8_pod['pod_info'].status}")
        
        # Getting here means pod is running. Store logs now.
        logs = get_k8_logs(k8_pod['k8_name'])
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

def check_db_pods():
    """Go through database for all tenants in this site. Delete/Create whatever is needed. Do nginx stuff.
    """
    all_pods = []
    stmt = select(Pod)
    for tenant in SITE_TENANT_DICT[conf.site_id]:
        all_pods += pg_store[conf.site_id][tenant].run("execute", stmt, scalars=True, all=True)

    ### Delete pods with status_requested = OFF or RESTART
    for pod in all_pods:
        if pod.status_requested in [OFF, RESTART] and pod.status != STOPPED:
            logger.info(f"pod_id: {pod.pod_id} found with status_requested: {pod.status_requested}. Gracefully shutting pod down.")
            container_exists, service_exists = graceful_rm_pod(pod)
            # if container and service not alive. Update status to STOPPED. UPDATE RESTART to ON.
            if not container_exists and not service_exists:
                logger.info(f"pod_id: {pod.pod_id} found with container and service stopped. Moving to status = STOPPED.")
                pod.status = STOPPED
                pod.status_container = {}
                if pod.status_requested == RESTART:
                    logger.info(f"pod_id: {pod.pod_id} in RESTART. Now in STOPPED, so switching status_requested back to ON.")
                    pod.status_requested = ON
                pod.db_update()
                
    ### Nginx ports and config changes
    # Get unused_instance_ports for nginx later.
    unused_instance_ports = list(range(52001, 52999))
    for pod in all_pods:
        try:
            unused_instance_ports.remove(pod.instance_port)
        except ValueError:
            pass
    tcp_pod_nginx_info = {} # for nginx config later
    http_pod_nginx_info = {} # for nginx config later
    for pod in all_pods:
        # Check for shutting down status
        # check for 1 or -1 in instance_ports to set/delete port.
        # 1 means we need to assign an instance_port.
        if pod.instance_port == 1:
            new_port = random.choice(unused_instance_ports)
            unused_instance_ports.remove(new_port)
            pod.instance_port = new_port
            pod.db_update()
        
        if pod.instance_port > 52000:
            template_info = {"instance_port": pod.instance_port,
                             "routing_port": pod.routing_port,
                             "url": pod.url}
            match pod.server_protocol:
                case "tcp":
                    tcp_pod_nginx_info[pod.k8_name] = template_info
                case "http":
                    http_pod_nginx_info[pod.k8_name] = template_info

    # This functions only updates if config is out of date.
    update_nginx_configmap(tcp_pod_nginx_info, http_pod_nginx_info)


def main():
    # Try and run check_db_pods. Will try for 30 seconds until health is declared "broken".
    logger.info("Top of health. Checking if db's are initialized.")
    idx = 0
    while idx < 12:
        try:
            check_db_pods()
            logger.info("Successfully connected to dbs.")
            break
        except Exception as e:
            logger.info(f"Can't connect to dbs yet idx: {idx}. e: {e}")
            # Health seems to take a few seconds to come up (due to database creation and api creation)
            time.sleep(5)
            idx += 1
    if idx == 12:
        logger.critical("Health could not connect to databases. Shutting down!")
        return

    while True:
        logger.info(f"Running pods health checks. Now: {time.time()}")
        check_k8_pods()
        check_k8_services()
        check_db_pods()
        ########
        #######
        # Go through database for each tenant in site now.
        logger.info(f"Health going through all tenants in site: {conf.site_id}")
        for tenant, store in pg_store[conf.site_id].items():
            pass
        ### Go through all of caddy.

        ### Go through and check services.
        
        ### Have a short wait
        time.sleep(1)

if __name__ == '__main__':
    main()


# shutdown_all_k8_pods()?
# # we need to delete any worker that is in SHUTDOWN REQUESTED or SHUTTING down for too long
# if worker_status == codes.SHUTDOWN_REQUESTED or worker_status == codes.SHUTTING_DOWN:
#     worker_last_health_check_time = worker.get('last_health_check_time')
#     if not worker_last_health_check_time:
#         worker_last_health_check_time = worker.get('create_time')
#     if not worker_last_health_check_time:
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker in SHUTDOWN and no health checks.')
#     elif worker_last_health_check_time < get_current_utc_time() - datetime.timedelta(minutes=5):
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker in SHUTDOWN for too long.')
