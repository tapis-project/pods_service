import json
import os
import time
import timeit
import datetime
import random
from typing import Literal, Dict, List

from kubernetes import client, config
from requests.exceptions import ReadTimeout, ConnectionError

from tapisservice.logs import get_logger
logger = get_logger(__name__)

from tapisservice.config import conf
from codes import BUSY, READY, RUNNING, CREATING_CONTAINER


# k8 client creation
config.load_incluster_config()
k8 = client.CoreV1Api()

host_id = os.environ.get('SPAWNER_HOST_ID', conf.spawner_host_id)
host_ip = conf.spawner_host_ip
logger.debug(f"host_id: {host_id}; host_ip: {host_ip}")

class KubernetesError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message

class KubernetesStartContainerError(KubernetesError):
    pass

class KubernetesStopContainerError(KubernetesError):
    pass


def get_kubernetes_namespace():
    """
    Attempt to get namespace from filesystem
    Should be in file /var/run/secrets/kubernetes.io/serviceaccount/namespace
    
    We first take config, if not available, we grab from filesystem. Meaning
    config should usually be empty.
    """
    namespace = conf.get("kubernetes_namespace", None)
    if not namespace:
        try:
            logger.debug("Attempting to get kubernetes_namespace from file.")
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
                content = f.readlines()
                namespace = content[0].strip()
        except Exception as e:
            logger.debug(f"Couldn't grab kubernetes namespace from filesystem. e: {e}")
        
    if not namespace:
        msg = "In get_kubernetes_namespace(). Failed to get namespace."
        logger.debug(msg)
        raise KubernetesError(msg)
    logger.debug(f"In get_kubernetes_namespace(). Got namespace: {namespace}.")
    return namespace

# Get k8 namespace for future use.
NAMESPACE = get_kubernetes_namespace()

def rm_container(k8_name):
    """
    Remove a container.
    :param cid:
    :return:
    """    
    try:
        k8.delete_namespaced_pod(name=k8_name, namespace=NAMESPACE)
    except Exception as e:
        logger.info(f"Got exception trying to remove pod: {k8_name}. Exception: {e}")
        raise KubernetesError(f"Error removing pod {k8_name}, exception: {str(e)}")
    logger.info(f"pod {k8_name} removed.")

def rm_service(service_id):
    """
    Remove a container.
    :param service_id:
    :return:
    """    
    try:
        k8.delete_namespaced_service(name=service_id, namespace=NAMESPACE)
    except Exception as e:
        logger.info(f"Got exception trying to remove pod: {service_id}. Exception: {e}")
        raise KubernetesError(f"Error removing pod {service_id}, exception: {str(e)}")
    logger.info(f"pod {service_id} removed.")

def list_all_containers():
    """Returns a list of all containers in a particular namespace """
    pods = k8.list_namespaced_pod(NAMESPACE).items
    return pods

def get_current_k8_pods(filter_str: str = "kgservice"):
    """Get all containers, filter for just db, and display."""
    db_containers = []
    for k8_pod in list_all_containers():
        k8_name = k8_pod.metadata.name
        if filter_str in k8_name:
            # db name format = "kgservice-<site>-<tenant>-<pod_name>
            # so split on - to get parts (containers use _, pods use -)
            try:
                parts = k8_name.split('-')
                site_id = parts[1]
                tenant_id = parts[2]
                pod_name = parts[3]
                db_containers.append({'pod_info': k8_pod,
                                      'site_id': site_id,
                                      'tenant_id': tenant_id,
                                      'pod_name': pod_name,
                                      'k8_name': k8_name})
            except Exception as e:
                msg = f"Exception parsing k8 pods. e: {e}"
                print(msg)
                pass
    return db_containers

def container_running(name=None):
    """
    Check if there is a running pods whose name contains the string, `name`. Note that this function will
    return True if any running container has a name which contains the input `name`.
    """
    logger.debug("top of kubernetes_utils.container_running().")
    if not name:
        raise KeyError(f"kubernetes_utils.container_running received name: {name}")
    try:
        if k8.read_namespaced_pod(namespace=NAMESPACE, name=name).status.phase == 'Running':
            return True
    except client.ApiException:
        # pod not found
        return False
    except Exception as e:
        msg = f"There was an error checking kubernetes_utils.container_running for name: {name}. Exception: {e}"
        logger.error(msg)
        raise KubernetesError(msg)
    
def stop_container(name):
    """
    Attempt to stop a running pod, with retry logic. Should only be called with a running pod.
    :param name: the pod name of the pod to be stopped.
    :return:
    """
    if not name:
        raise KeyError(f"kubernetes_utils.container_running received name: {name}")

    i = 0
    while i < 10:        
        try:
            k8.delete_namespaced_pod(namespace=NAMESPACE, name=name)
            return True
        except client.ApiException:
            # pod not found
            return False
        except Exception as e:
            logger.error(f"Got another exception trying to stop the actor container. Exception: {e}")
            i += 1
            continue
    raise KubernetesStopContainerError("Error. Pod not deleted after 10 attempts.")

def create_container(name: str,
                     image: str,
                     revision: int,
                     command: List,
                     ports_dict: Dict = {},
                     environment: Dict = {},
                     mounts: List = [],
                     mem_limit: str | None = None,
                     max_cpus: str | None = None,
                     user: str | None = None,
                     image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "Always"):
    """
    No permissions. No conf files. Not like Abaco. Just runs a Kube container with specified parameters.
    ###### WIP
    Creates and runs an actor pod and supervises the execution, collecting statistics about resource consumption
    with kubernetes utils.

    :param actor_id: the dbid of the actor; for updating worker status
    :param worker_id: the worker id; also for updating worker status
    :param execution_id: the id of the execution.
    :param image: the actor's image;
    :param msg: the message being passed to the actor.
    :param user: string in the form {uid}:{gid} representing the uid and gid to run the command as.
    :param environment: dictionary representing the environment to instantiate within the actor container.
    :param privileged: whether this actor is "privileged"; i.e., its container should run in privileged mode with the
    docker daemon mounted.
    :param mounts: list of dictionaries representing the mounts to add; each dictionary mount should have 3 keys:
    host_path, container_path and format (which should have value 'ro' or 'rw').
    :param fifo_host_path: If not None, a string representing a path on the host to a FIFO used for passing binary data to the actor.
    :param socket_host_path: If not None, a string representing a path on the host to a socket used for collecting results from the actor.
    :param mem_limit: The maximum amount of memory the Actor container can use; should be the same format as the --memory Docker flag.
    :param max_cpus: The maximum number of CPUs each actor will have available to them. Does not guarantee these CPU resources; serves as upper bound.
    :return: result (dict), logs (str) - `result`: statistics about resource consumption; `logs`: output from docker logs.apiVersion: apps/v1
    """
    logger.debug("top of kubernetes_utils.create_container().")

    ### Ports
    ports = []
    for port_name, port_val in ports_dict.items():
        ports.append(client.V1ContainerPort(name=port_name, container_port=port_val))
    logger.debug(f"Pod declared ports: {ports}")

    ### Environment
    # Kubernetes sets some default envs. We write over these here + use enable_service_links=False in PodSpec
    environment.update({
        'image': image,
        'revision': revision,
        'KUBERNETES_PORT': "",
        'KUBERNETES_SERVICE_HOST': "",
        'KUBERNETES_SERVICE_PORT': "",
        'KUBERNETES_SERVICE_PORT_HTTPS': "",
        'KUBERNETES_PORT_443_TCP': "",
        'KUBERNETES_PORT_443_TCP_ADDR': "",
        'KUBERNETES_PORT_443_TCP_PORT': "",
        'KUBERNETES_PORT_443_TCP_PROTO': ""        
    })
    env = []
    for env_name, env_val in environment.items():
        env.append(client.V1EnvVar(name=env_name, value=str(env_val)))
    logger.debug(f"Pod declared environment variables: {env}")

    ### Volumes/Volume Mounts
    # Get mounts ready for k8 spec
    if mounts:
        volumes, volume_mounts = mounts
    else:
        volumes = []
        volume_mounts = []
    logger.debug(f"Volumes: {volumes}; pod_name: {name}")
    logger.debug(f"Volume_mounts: {volume_mounts}; pod_name: {name}")

    ### Resource Limits - memory and cpu
    resources = None
    resource_limits = {}
    # Memory - k8 uses no suffix (for bytes), Ki, Mi, Gi, Ti, Pi, or Ei (Does not accept kb, mb, or gb at all)
    if not mem_limit or mem_limit == -1:
        mem_limit = None # Unlimited memory
    else:
        resource_limits["memory"] = mem_limit
    # CPUs - In nanocpus (n)
    if not max_cpus or max_cpus == -1:
        max_cpus = None # Unlimited CPI
    else:
        max_cpus = f"{max_cpus}m"
        resource_limits["cpu"] = max_cpus
    # Define resource requirements if resource limits specified
    if resource_limits:
        resources = client.V1ResourceRequirements(limits = resource_limits)

    ### Security Context
    security_context = None
    uid = None
    gid = None
    if user:
        try:
            # user should be None or "223232:323232" ("uid:gid")
            uid, gid = user.split(":")
        except Exception as e:
            # error starting the pod, user will need to debug
            msg = f"Got exception getting user uid/gid: {e}; pod_name: {name}"
            logger.info(msg)
            raise KubernetesStartContainerError(msg)
    # Define security context if uid and gid are found
    if uid and gid:
        security = client.V1SecurityContext(run_as_user=uid, run_as_group=gid)

    ### Define and start the pod
    try:
        container = client.V1Container(
            name=name,
            command=command,
            image=image,
            volume_mounts=volume_mounts,
            env=env,
            resources=resources,
            ports=ports,
            image_pull_policy=image_pull_policy
        )
        pod_spec = client.V1PodSpec(
            containers=[container],
            volumes=volumes,
            restart_policy="Never",
            security_context=security_context,
            enable_service_links=False
        )
        pod_body = client.V1Pod(
            metadata=client.V1ObjectMeta(name=name),
            spec=pod_spec,
            kind="Pod",
            api_version="v1"
        )
        k8pod = k8.create_namespaced_pod(
            namespace=NAMESPACE,
            body=pod_body
        )
    except Exception as e:
        msg = f"Got exception trying to create pod with image: {image}. {repr(e)}"
        logger.info(msg)
        raise KubernetesError(msg)
    logger.info(f"Pod created successfully.")
    return k8pod


def create_service(name, ports_dict={}):
    """
    """
    logger.debug("top of kubernetes_utils.create_service().")

    ### Ports
    ports = []
    for port_name, port_val in ports_dict.items():
        ports.append(client.V1ServicePort(name=port_name, port=port_val, target_port=port_val))
    logger.debug(f"Pod declared ports: {ports}")

    ### Define and start the service
    try:
        service_spec = client.V1ServiceSpec(
            selector={"app": name},
            ports=ports
        )
        service_body = client.V1Service(
            metadata=client.V1ObjectMeta(name=name),
            spec=service_spec,
            kind="Service",
            api_version="v1"
        )
        k8service = k8.create_namespaced_service(
            namespace=NAMESPACE,
            body=service_body
        )
    except Exception as e:
        msg = f"Got exception trying to start service with name: {name}. {repr(e)}"
        logger.info(msg)
        raise KubernetesError(msg)
    logger.info(f"Pod started successfully.")
    return k8service






        
    #start_time = get_current_utc_time()



    # Wait for pod to start running. Unique to kube as docker container start command is blocking. Not here.
    # To check for availablity we first check if pod is in Succeeded phase (possible with fast execs). If not we can't trust
    # pod.phase of "Running" as that means nothing. Need to look at container status. Container status is outputted as
    # state: {"running": None, "terminated": None, "waiting": None}
    # None is an object containing container_id, exit_code, start/finish time, message (error if applicable), reason, and signal.
    # Reason can be "CrashLoopBackOff" in waiting, "Completed" in terminated (should mean Succeeded pod phase),
    # "ContainerCreating" in waiting, etc. Only one state object is ever specified, we need to error in the case of errors,
    # and mark running if running.
    
    # # local bool tracking whether the actor pod is still running
    # container_creating = False
    # running = False
    # loop_idx = 0
    # while True:
    #     loop_idx += 1
    #     pod = k8.read_namespaced_pod(namespace=NAMESPACE, name=name)
    #     pod_phase = pod.status.phase
    #     if pod_phase == "Succeeded":
    #         logger.debug(f"Kube exec in succeeded phase. (worker {worker_id}; {execution_id})")
    #         running = True
    #         break
    #     elif pod_phase in ["Running", "Pending"]:
    #         # Check if container running or in error state (exec pods are always only one container (so far))
    #         # Container can be in waiting state due to ContainerCreating ofc
    #         # We try to get c_state. container_status when pending is None for a bit.
    #         try:
    #             c_state = pod.status.container_statuses[0].state
    #         except:
    #             c_state = None
    #         logger.debug(f'state: {c_state}')
    #         if c_state:
    #             if c_state.waiting and c_state.waiting.reason != "ContainerCreating":
    #                 msg = f"Found kube container waiting with reason: {c_state.waiting.reason} (worker {worker_id}; {execution_id})"
    #                 logger.error(msg)
    #                 raise KubernetesStartContainerError(msg)
    #             elif c_state.waiting and c_state.waiting.reason == "ContainerCreating":
    #                 if not container_creating:
    #                     container_creating = True
    #                     Execution.update_status(actor_id, execution_id, CREATING_CONTAINER)
    #             elif c_state.running:
    #                 running = True
    #                 break
        
    #     # TODO: Add some more checks here, check for kube container error statuses.
    #     if loop_idx % 60:
    #         logger.debug(f"Waiting for kube exec to get to running. {loop_idx} sec. (worker {worker_id}; {execution_id})")
    #     if loop_idx == 300:
    #         msg = f"Kube exec not ready after 5 minutes. shutting it down. (worker {worker_id}; {execution_id})"
    #         logger.warning(msg)
    #         raise KubernetesStartContainerError(msg)
    #     time.sleep(1)
            
    # if running:
    #     Execution.update_status(actor_id, execution_id, RUNNING)

    # # Stats loop waiting for execution to end
    # # a counter of the number of iterations through the main "running" loop;
    # # this counter is used to determine when less frequent actions, such as log aggregation, need to run.
    # loop_idx = 0
    # log_ex = Actor.get_actor_log_ttl(actor_id)
    # logs = None
    # while running and not globals.force_quit:
    #     loop_idx += 1
    #     logger.debug(f"top of kubernetes_utils while running loop; loop_idx: {loop_idx}")

    #     # grab the logs every 3rd iteration --
    #     if loop_idx % 3 == 0:
    #         logs = None
    #         logs = k8.read_namespaced_pod_log(namespace=NAMESPACE, name=name)
    #         Execution.set_logs(execution_id, logs, actor_id, tenant, worker_id, log_ex)

    #     ## Check pod to see if we're still running
    #     logger.debug(f"about to check pod status: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    #     # Waiting for pod availability
    #     i = 0
    #     pod = None
    #     while i < 10:
    #         try:
    #             pod = k8.read_namespaced_pod(namespace=NAMESPACE, name=name)                    
    #             break # pod was found
    #         except client.rest.ApiException: # pod not found                    
    #             logger.error(f"Got an IndexError trying to get the pod object. (worker {worker_id}; {execution_id})")
    #             time.sleep(0.1)
    #             i += 1
    #     logger.debug(f"done checking status: {timeit.default_timer()}; i: {i}; (worker {worker_id}; {execution_id})")
    #     if not pod: # Couldn't find pod
    #         logger.error(f"Couldn't retrieve pod! Stopping pod; name: {name}; (worker {worker_id}; {execution_id})")
    #         stop_container(name)
    #         logger.info(f"pod {name} stopped. (worker {worker_id}; {execution_id})")
    #         running = False
    #         continue

    #     # Get pod state
    #     try:
    #         state = pod.status.phase
    #     except:
    #         state = "broken"
    #         logger.error(f"KUBE BUG:couldn't get status.phase. pod: {pod}")
    #     if state != 'Running': # If we're already in Running, Success is the only option.
    #         logger.debug(f"pod finished, final state: {state}; (worker {worker_id}; {execution_id})")
    #         running = False
    #         continue
    #     else:
    #         # pod still running; check for force_quit OR max_run_time
    #         runtime = timeit.default_timer() - start
    #         if globals.force_quit:
    #             logger.warning(f"issuing force quit: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    #             stop_container
    #             running = False
    #         if max_run_time > 0 and max_run_time < runtime:
    #             logger.warning(f"hit runtime limit: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    #             stop_container
    #             running = False
    #     logger.debug(f"right after checking pod state: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    # logger.info(f"pod stopped:{timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    # stop = timeit.default_timer()
    # globals.force_quit = False

    # # get info from pod execution, including exit code; Exceptions from any of these commands
    # # should not cause the worker to shutdown or prevent starting subsequent actor pods.
    # logger.debug("Pod finished")
    # exit_code = 'undetermined'
    # try:
    #     pod = k8.read_namespaced_pod(namespace=NAMESPACE, name=name)
    #     try:
    #         c_state = pod.status.container_statuses[0].state # to be used to set final_state
    #         # Sets final state equal to whichever c_state object exists (only 1 exists at a time ever)
    #         pod_state = c_state.running or c_state.terminated or c_state.waiting
    #         pod_state = pod_state.to_dict()
    #         try:
    #             exit_code = pod_state.get('exit_code', 'No Exit Code')
    #             startedat_ISO = pod_state.get('started_at', 'No "started at" time (k8 - request feature on github)')
    #             finishedat_ISO = pod_state.get('finished_at' 'No "finished at" time (k8 - request feature on github)')
    #             # if times exist, converting ISO8601 times to unix timestamps
    #             if not 'github' in startedat_ISO:
    #                 # Slicing to 23 to account for accuracy up to milliseconds and replace to get rid of ISO 8601 'Z'
    #                 startedat_ISO = startedat_ISO.replace('Z', '')[:23]
    #                 pod_state['StartedAt'] = datetime.datetime.strptime(startedat_ISO, "%Y-%m-%dT%H:%M:%S.%f")

    #             if not 'github' in finishedat_ISO:
    #                 finishedat_ISO = pod.finishedat_ISO.replace('Z', '')[:23]
    #                 pod_state['FinishedAt'] = datetime.datetime.strptime(finishedat_ISO, "%Y-%m-%dT%H:%M:%S.%f")
    #         except Exception as e:
    #             logger.error(f"Datetime conversion failed for pod {name}. "
    #                          f"Exception: {e}; (worker {worker_id}; {execution_id})")
    #     except Exception as e:
    #         logger.error(f"Could not determine final state for pod {name}. "
    #                      f"Exception: {e}; (worker {worker_id}; {execution_id})")
    #         pod_state = {'unavailable': True}
    # except client.rest.ApiException:
    #     logger.error(f"Could not get pod info for name: {name}. "
    #                  f"Exception: {e}; (worker {worker_id}; {execution_id})")

    # logger.debug(f"right after getting pod object: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    # # get logs from pod
    # try:
    #     if not logs:
    #         logs = k8.read_namespaced_pod_log(namespace=NAMESPACE, name=name)
    #     if not logs:
    #         # there are issues where container do not have logs associated with them when they should.
    #         logger.info(f"Pod: {name} had NO logs associated with it. (worker {worker_id}; {execution_id})")
    # except Exception as e:
    #     logger.error(f"Unable to get logs for exec. error: {e}. (worker {worker_id}; {execution_id})")
    # logger.debug(f"right after getting pod logs: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")

    # # remove actor container with retrying logic -- check for specific filesystem errors from kube
    # if not leave_container:
    #     logger.debug("deleting container")
    #     keep_trying = True
    #     count = 0
    #     while keep_trying and count < 10:
    #         keep_trying = False
    #         count = count + 1
    #         try:
    #             stop_container(name)
    #             logger.info(f"Actor pod removed. (worker {worker_id}; {execution_id})")
    #         except Exception as e:
    #             # if the pod is already gone we definitely want to quit:
    #             if "Reason: Not Found" in str(e):
    #                 logger.info("Got 'Not Found' exception - quiting. "
    #                             f"Exception: {e}; (worker {worker_id}; {execution_id})")
    #                 break
    #             else:
    #                 logger.error("Unexpected exception trying to remove actor pod. Giving up."
    #                              f"Exception: {e}; type: {type(e)}; (worker {worker_id}; {execution_id})")
    # else:
    #     logger.debug(f"leaving actor pod since leave_container was True. (worker {worker_id}; {execution_id})")
    # logger.debug(f"right after removing actor container: {timeit.default_timer()}; (worker {worker_id}; {execution_id})")
    # result['runtime'] = int(stop - start)
    # return result, logs, pod_state, exit_code, start_time
