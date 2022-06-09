from fcntl import DN_DELETE
import json
import os
import time
import timeit
import datetime
import random
from typing import Literal, Dict, List

from jinja2 import Environment, FileSystemLoader
from kubernetes import client, config
from requests.exceptions import ReadTimeout, ConnectionError

from tapisservice.logs import get_logger
logger = get_logger(__name__)

from tapisservice.config import conf
from codes import BUSY, READY, RUNNING, CREATING_CONTAINER
from stores import SITE_TENANT_DICT
from stores import pg_store
from sqlmodel import select
from models import Pod

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

def get_current_k8_pods(service_name: str = "pods", site_id: str = conf.site_id):
    """
    The get_current_k8_pods function returns a list of dictionaries containing the following keys:
        - pod_info: The Kubernetes API object for the container.
        - site_id: The site ID (e.g., 'east', 'west') where this container is located.
        - tenant_id: The tenant ID (e.g., 'acme-prod') where this container is located.
        - pod_id: A string representing the name of the pod, e.g., &quot;pods-east-acme-prod&quot;.  This value can be used to filter out pods in other sites or tenants when needed.
    
    :param filter_str:str=&quot;pods&quot;: Filter the list of containers
    :return: A list of dictionaries
    :doc-author: Trelent
    """
    """Get all containers, filter for just db, and display."""
    filter_str = f"{service_name}-{site_id}"
    db_containers = []
    for k8_pod in list_all_containers():
        k8_name = k8_pod.metadata.name
        if filter_str in k8_name:
            # db name format = "pods-<site>-<tenant>-<pod_id>
            # so split on - to get parts (containers use _, pods use -)
            try:
                parts = k8_name.split('-')
                site_id = parts[1]
                tenant_id = parts[2]
                pod_id = parts[3]
                db_containers.append({'pod_info': k8_pod,
                                      'site_id': site_id,
                                      'tenant_id': tenant_id,
                                      'pod_id': pod_id,
                                      'k8_name': k8_name})
            except Exception as e:
                msg = f"Exception parsing k8 pods. e: {e}"
                print(msg)
                pass
    return db_containers

def get_k8_logs(name: str):
    try:
        logs = k8.read_namespaced_pod_log(namespace=NAMESPACE, name=name)
        return logs
    except Exception as e:
        return ""

def container_running(name: str):
    """
    Check if k8 pod is currently running.

    Args:
        name (str): Name of k8 pod to look for, pods-<site>-<tenant>-<pod_id> format.
    
    Raises:
        KeyError: _description_
        KubernetesError: K8 got an error running read_namespaced_pod().

    Returns:
        bool: True if running, False otherwise.
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
    
def stop_container(name: str):
    """
    Attempt to stop running pod, with retry logic. Should only be called with a running pod.

    Args:
        name (str): Name of k8 pod to stop, pods-<site>-<tenant>-<pod_id> format.

    Raises:
        KeyError: _description_
        KubernetesStopContainerError: _description_

    Returns:
        bool: True if pod deleted successfully, False otherwise.
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

def create_pod(name: str,
               image: str,
               revision: int,
               command: List | None = None,
               init_command: List | None = None,
               ports_dict: Dict = {},
               environment: Dict = {},
               mounts: List = [],
               mem_request: str | None = None,
               cpu_request: str | None = None,
               mem_limit: str | None = None,
               cpu_limit: str | None = None,
               user: str | None = None,
               image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = "Always"):
    """
    Creates and runs a k8 pod.

    Notes:
    Not like Abaco. This is purely container creation using inputs. Nothing specific to the pod to be created.
    Meaning, no permissions, no adding conf files.

    Args:
        name (str): _description_
        image (str): _description_
        revision (int): _description_
        command (List): _description_
        ports_dict (Dict, optional): _description_. Defaults to {}.
        environment (Dict, optional): _description_. Defaults to {}.
        mounts (List, optional): _description_. Defaults to [].
        mem_limit (str | None, optional): _description_. Defaults to None.
        max_cpus (str | None, optional): _description_. Defaults to None.
        user (str | None, optional): _description_. Defaults to None.
        image_pull_policy ("Always" | "IfNotPresent" | "Never"): _description_. Defaults to "Always".

    Raises:
        KubernetesStartContainerError: _description_
        KubernetesError: _description_

    Returns:
        k8pod: Pod info resulting from create_namespaced_pod.
    """    
    logger.debug("top of kubernetes_utils.create_pod().")

    ### Ports
    ports = []
    for port_name, port_val in ports_dict.items():
        ports.append(client.V1ContainerPort(name=port_name, container_port=port_val))
    logger.debug(f"Pod declared ports: {ports}")

    ### Environment
    environment.update({
        'image': image,
        'revision': revision,
        # Kubernetes sets some default envs. We write over these here + use enable_service_links=False in PodSpec
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
    logger.debug(f"Volumes: {volumes}; pod_id: {name}")
    logger.debug(f"Volume_mounts: {volume_mounts}; pod_id: {name}")

    ### Resource Limits + Requests - memory and cpu
    # Memory - k8 uses no suffix (for bytes), Ki, Mi, Gi, Ti, Pi, or Ei (Does not accept kb, mb, or gb at all)
    # CPUs - In millicpus (m)
    # Limits
    resource_limits = {}
    if mem_limit:
        resource_limits["memory"] = mem_limit
    if cpu_limit:
        resource_limits["cpu"] = f"{cpu_limit}m"
    # Requests
    resource_requests = {}
    if mem_request:
        resource_requests["memory"] = mem_request
    if cpu_request:
        resource_requests["cpu"] = f"{cpu_request}m"
    # Define resource requirements if resource limits specified
    resources = client.V1ResourceRequirements(limits = resource_limits, requests = resource_requests)

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
            msg = f"Got exception getting user uid/gid: {e}; pod_id: {name}"
            logger.info(msg)
            raise KubernetesStartContainerError(msg)
    # Define security context if uid and gid are found
    if uid and gid:
        security = client.V1SecurityContext(run_as_user=uid, run_as_group=gid)

    ### Init container creation
    if init_command:
        init_container = client.V1Container(
            name=f"{name}-init",
            command=init_command,
            image=image,
            volume_mounts=volume_mounts,
            env=env,
            resources=resources,
            image_pull_policy=image_pull_policy
        )
        init_containers = [init_container]
    else:
        init_containers = []

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
            init_containers=init_containers,
            containers=[container],
            volumes=volumes,
            restart_policy="Never",
            security_context=security_context,
            enable_service_links=False
        )
        pod_metadata = client.V1ObjectMeta(
            name=name,
            labels={"app": name}
        )
        pod_body = client.V1Pod(
            metadata=pod_metadata,
            spec=pod_spec,
            kind="Pod",
            api_version="v1"
        )
        k8pod = k8.create_namespaced_pod(
            namespace=NAMESPACE,
            body=pod_body
        )
    except Exception as e:
        msg = f"Got exception trying to create pod with image: {image}. {repr(e)}. e: {e}"
        logger.info(msg)
        raise KubernetesError(msg)
    logger.info(f"Pod created successfully.")
    return k8pod


def create_service(name, ports_dict={}):
    """
    Takes a given dict of ports and creates a service for a specific k8 pod.

    Args:
        name (_type_): _description_
        ports_dict (dict, optional): _description_. Defaults to {}.

    Raises:
        KubernetesError: _description_

    Returns:
        _type_: _description_
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
            type="NodePort",
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
        msg = f"Got exception trying to start service with name: {name}. {e}"
        logger.info(msg)
        raise KubernetesError(msg)
    logger.info(f"Pod started successfully.")
    return k8service


def get_current_instance_ports():
    """
    Takes pod name. Calls get_current_k8_pods(). Cross references with database. There's a nginx_ports store.
    Stores pod name, instance port, routing port. Concat data from all tenants. Create nginx_info from that.

    Store gets updated by pod creation or pod deletion.

    Result
    """
    all_instance_ports = []

    stmt = select(Pod.instance_port)
    result_list = pg_store['tacc']['tacc'].run("execute", stmt, all=True)
    for result in result_list:
        if result[0]:
            all_instance_ports.append(result[0])
    
    return all_instance_ports


def update_nginx_configmap(tcp_pod_nginx_info: Dict[str, Dict[str, str]], http_pod_nginx_info: Dict[str, Dict[str, str]]):
    """
    Update fn for nginx configmap. Will read kubernetes/db data and create nginx server stanza bits where neccessary.
    Should be site specific.

    Args:
        pod_nginx_info ({"pod_id1": {"routing_port": int, "instance_port": int}, ..., ...}): Dict of dict that 
            specifies ports needed to create pod service.
    """
    template_env = Environment(loader=FileSystemLoader("service/templates"))
    template = template_env.get_template('nginx-template.j2')
    rendered_template = template.render(tcp_pod_nginx_info = tcp_pod_nginx_info, http_pod_nginx_info = http_pod_nginx_info, namespace = NAMESPACE)

    # Only update the configmap if the current configmap is out of date.
    current_template = k8.read_namespaced_config_map(name='pods-nginx', namespace=NAMESPACE)
    
    if not current_template.data['nginx.conf'] == rendered_template:
        # Update the configmap with the new template immediately.
        config_map = client.V1ConfigMap(data = {"nginx.conf": rendered_template})
        k8.patch_namespaced_config_map(name='pods-nginx', namespace=NAMESPACE, body=config_map)
        # Auto updates nginx pod. Changes take place according to kubelet sync frequency duration (60s default).
