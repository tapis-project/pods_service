"""
Does the following:
1. Go through database. 
  a. Deletes pods in shutting down status.
  b. Deletes pods in error status.
  c. Updates pod status.
  d. Ensures all database pods exist (For this site)
  e. Ensures all pods that exist are in the database.
    i. Also checks that pods are healthy and communicating

2. Enforce ttl for pods.

3. Look through S3? Prune old things?/Prune non-existing pods.

4. Check that spawner is alive? Create if needed?
  a. Maybe have a warning slack message if none available?

Running mode, either:
1. Periodically run script.
2. Always keep running in big loop.
"""

import time
from kubernetes import client, config
from kubernetes_utils import rm_container, get_current_k8_pods, rm_service, KubernetesError

from codes import READY, SHUTTING_DOWN, SHUTDOWN_REQUESTED, STOPPED, ERROR
from stores import pg_store
from models import Pod, ExportedData
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()


def rm_pod(k8_name):
    # TODO Change Caddy address!

    try:
        rm_container(k8_name)
    except KubernetesError:
        # pod not found
        pass
    try:
        rm_service(k8_name)
    except KubernetesError:
        # service not found
        pass

def graceful_rm_pod(pod):
    """
    Needs to delete pod, delete service, and change caddy to "offline" response.
    TODO Set status to shutting down. Something else will put into "shutdown".
    """
    logger.info(f"Top of shutdown pod for pod: {pod.k8_name}")
    # Change pod status to SHUTTING DOWN
    pod.status = SHUTTING_DOWN
    pod.db_update()
    logger.debug(f"spawner has updated pod status to SHUTTING_DOWN")
    rm_pod(pod.k8_name)

def check_k8_pods():
    # This is all for only the site specified in conf.site_id.
    # Each site should get it's own health pod.
    # Go through live containers first as it's "truth". Set database from that info. (error if needed, update statuses)
    k8_pods = get_current_k8_pods() # Returns {k8_pod_info, site, tenant, pod_name}

    # Check each pod.
    for k8_pod in k8_pods:
        logger.info(f"Checking health for pod_name: {k8_pod['pod_name']}")

        # Check for found pod in database.
        pod = Pod.db_get_with_pk(k8_pod['pod_name'], k8_pod['tenant_id'], k8_pod['site_id'])
        # We've found a pod without a database entry. Shut it and potential service down.
        if not pod:
            logger.warning(f"Found k8 pod without any database entry. Deleting. Pod: {k8_pod['k8_name']}")
            rm_pod(k8_pod['k8_name'])
            continue
        
        # Found pod in db.
        # TODO Update status in db.
        # Add last_health_check attr.
        # Delete things in Shutdown requested.
        # We could try and make a get to the pod to check if still alive.

        k8_pod_phase = k8_pod['pod_info'].status.phase
        container_status = {"phase": k8_pod_phase,
                            "start_time": str(k8_pod['pod_info'].status.start_time),
                            "message": ""}
        
        # This is actually bad. Means the pod has stopped, which shouldn't be the case.
        # We'll put pod in error state with message.
        if k8_pod_phase == "Succeeded":
            logger.critical(f"Kube pod in succeeded phase.")
            container_status['message'] = "Pod phase in Succeeded, stopped processing. Erroring out."
            pod.container_status = container_status
            pod.status = ERROR
            pod.db_update()
            continue
        elif k8_pod_phase in ["Running", "Pending"]:
            # Check if container running or in error state (pods are always only one container (so far))
            # Container can be in waiting state due to ContainerCreating ofc
            # We try to get c_state. container_status when pending is None for a bit.
            try:
                c_state = k8_pod['pod_info'].status.container_statuses[0].state
            except:
                c_state = None
            logger.debug(f'state: {c_state}')
            if c_state:
                if c_state.waiting and c_state.waiting.reason != "ContainerCreating":
                    logger.critical(f"Kube pod in waiting state. msg:{c_state.waiting.message}; reason: {c_state.waiting.reason}")
                    container_status['message'] = f"Pod in waiting state for reason: {c_state.waiting.message}."
                    pod.container_status = container_status
                    pod.status = ERROR
                    pod.db_update()
                    continue
                elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
                    logger.info(f"Kube pod in waiting state, still creating container.")
                    container_status['message'] = "Pod is still initializing."
                    pod.container_status = container_status
                    pod.db_update()
                    continue
                elif c_state.running:
                    container_status['message'] = "Pod is running."
                    pod.container_status = container_status
                    pod.status = READY
                    pod.db_update()
                    continue
            else:
                # Not sure if this is possible/what happens here.
                # There is definitely an Error state. Can't replicate locally yet.
                logger.critical(f"NO c_state. {k8_pod['pod_info'].status}")

def main():
    while True:
        logger.info(f"Running kgservice health checks. Now: {time.time()}")
        check_k8_pods()
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
# def check_containers():
#     # Delete hanging containers without db record.
#     logger.info(f"Top of check_containers in health. Looking to delete hanging containers.")
#     worker_records = workers_store['tacc'].items()
#     worker_ids_in_db = []
#     for worker in worker_records:
#         worker_ids_in_db.append(worker['id'].lower())
#     logger.info(f"check_containers(). List of all worker_ids found in db: {worker_ids_in_db}")

#     worker_containers = get_current_k8_pods()
#     # We check only by worker_id (not actors-worker-tenant-actor_id-worker_id) because it's easier. + better to leave container, then delete
#     for container in worker_containers:
#         container_worker_id = container['worker_id']
#         if not container_worker_id in worker_ids_in_db:
#             # Couldn't find worker doc matching worker container
#             # reconstitute container name actors-worker-<tenant>-<actor-id>-<worker-id>
#             container_id = f"actors-worker-{container['tenant_id']}-{container['actor_id']}-{container_worker_id}"
#             logger.debug(f"Container {container_id} found, but no worker_id == {container_worker_id} found in db. Hanging container, deleting pod.")
#             rm_container(container_id)

# worker_id = worker['id']
# worker_status = worker.get('status')
# # if the worker has only been requested, it will not have a host_id. it is possible
# # the worker will ultimately get scheduled on a different host; however, if there is
# # some issue and the worker is "stuck" in the early phases, we should remove it..
# if 'host_id' not in worker:
#     # check for an old create time
#     worker_create_t = worker.get('create_time')
#     # in versions prior to 1.9, worker create_time was not set until after it was READY
#     if not worker_create_t:
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker did not have a host_id or create_time field.')
#     # if still no host after 5 minutes, delete it
#     if worker_create_t <  get_current_utc_time() - datetime.timedelta(minutes=5):
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker did not have a host_id and had '
#                                                             'old create_time field.')
#     continue


# # we need to delete any worker that is in SHUTDOWN REQUESTED or SHUTTING down for too long
# if worker_status == codes.SHUTDOWN_REQUESTED or worker_status == codes.SHUTTING_DOWN:
#     worker_last_health_check_time = worker.get('last_health_check_time')
#     if not worker_last_health_check_time:
#         worker_last_health_check_time = worker.get('create_time')
#     if not worker_last_health_check_time:
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker in SHUTDOWN and no health checks.')
#     elif worker_last_health_check_time < get_current_utc_time() - datetime.timedelta(minutes=5):
#         hard_delete_worker(actor_id, worker_id, reason_str='Worker in SHUTDOWN for too long.')
