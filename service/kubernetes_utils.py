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
