from codes import ERROR, SPAWNER_SETUP, CREATING, \
    REQUESTED, DELETING
from models_pods import Pod, Password, PodBaseFull
from models_templates_tags import TemplateTag, TemplateTagPodDefinition
from models_templates_utils import combine_pod_and_template_recursively
from kubernetes_utils import create_pod, create_service, create_pvc, KubernetesError
from kubernetes import client, config

from tapisservice.config import conf
from tapisservice.logs import get_logger
from tapisservice.errors import BaseTapisError
from volume_utils import get_nfs_ip
import re

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
    pod = PodBaseFull(**input_pod.dict().copy()) # Create a copy of pod data we'll merge template data into
    logger.debug(f"Attempting to start generic pod; name: {pod.k8_name}; revision: {revision}")

    if pod.template:
        # Derive the final pod object by combining the pod and templates
        final_pod = combine_pod_and_template_recursively(pod, pod.template, tenant=pod.tenant_id, site=pod.site_id)
        logger.debug(f"final_pod -----------------------\n{final_pod.display()}")

        ###
        ### SECRETS
        ###
        # Need to replace all "<<TAPIS_vars>>" with vals from secrets for example needs to work for "dsadsadsa <<TAPIS_mysecret>> dsadsadsa".
        # currently just the passwords db table. Eventually that'll become pods_env which itself could reference sk if that's needed.
        pods_env = Password.db_get_with_pk(pod.pod_id, pod.tenant_id, pod.site_id)
        pods_env = pods_env.dict()
        if final_pod.environment_variables:
            for key, val in final_pod.environment_variables.items():
                new_val = val
                if isinstance(val, str):
                    # regex to create list of [<<TAPIS_*>> strings, str of inner variable without >><<]
                    matches = re.findall(r'<<TAPIS_(.*?)>>', val)
                    for match in matches:
                        new_val = new_val.replace(f"<<TAPIS_{match}>>", pods_env.get(match))
                    final_pod.environment_variables[key] = new_val

        # #command
        # if final_pod.command:
        #     for key in final_pod.command:
        #         if isinstance(key, str):
        #             matches = re.findall(r'<<TAPIS_(.*?)>>', key)
        #             for match in matches:
        #                 final_pod.command[key] = key.replace(f"<<TAPIS_{match}>>", pods_env.get(match))
        # #arguments
        # if final_pod.arguments:
        #     for key in final_pod.arguments:
        #         if isinstance(key, str):
        #             matches = re.findall(r'<<TAPIS_(.*?)>>', key)
        #             for match in matches:
        #                 final_pod.arguments[key] = key.replace(f"<<TAPIS_{match}>>", pods_env.get(match))

    volumes = []
    volume_mounts = []


    # Create PVC if requested.
    if pod.volume_mounts:
        nfs_nfs_ip = get_nfs_ip()
        for vol_name, vol_info in pod.volume_mounts.items():
            vol_info = vol_info.dict() # turn Resource back into dict.
            full_k8_name = f"{pod.k8_name}--{vol_name}"
            match vol_info.get("type"):
                case "tapisvolume":
                    nfs_volume = client.V1NFSVolumeSource(path = f"/", server = nfs_nfs_ip) # f"/podsnfs/{pod.tenant_id}/volumes/{vol_name}"
                    volumes.append(client.V1Volume(name = full_k8_name, nfs = nfs_volume))
                    volume_mounts.append(client.V1VolumeMount(name = full_k8_name, mount_path = vol_info.get("mount_path"), sub_path = f"{pod.tenant_id}/volumes/{vol_name}")) # vol_info.get("sub_path")))
                case "tapissnapshot":
                    nfs_volume = client.V1NFSVolumeSource(path = f"/", server = nfs_nfs_ip) # f"/podsnfs/{pod.tenant_id}/snapshots/{vol_name}"
                    volumes.append(client.V1Volume(name = full_k8_name, nfs = nfs_volume))
                    volume_mounts.append(client.V1VolumeMount(name = full_k8_name, mount_path = vol_info.get("mount_path"), sub_path = f"{pod.tenant_id}/snapshots/{vol_name}")) # vol_info.get("sub_path")))
                case "pvc":
                    create_pvc(name = full_k8_name)
                    persistent_volume = client.V1PersistentVolumeClaimVolumeSource(claim_name = full_k8_name)
                    volumes.append(client.V1Volume(name = full_k8_name, persistent_volume_claim = persistent_volume))
                    volume_mounts.append(client.V1VolumeMount(name = full_k8_name, mount_path = vol_info.get("mount_path"), sub_path = f"{pod.tenant_id}/volumes/{vol_name}"))
                case _:
                    pass
                    #error!

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
        "mounts": [volumes, volume_mounts],
        "queue": pod.compute_queue,
        "mem_request": pod.resources.mem_request,
        "cpu_request": pod.resources.cpu_request,
        "mem_limit": pod.resources.mem_limit,
        "cpu_limit": pod.resources.cpu_limit,
        "gpus": pod.resources.gpus,
        "user": None
    }

    # Create init_container, container, and service.
    create_pod(**container)
    create_service(name = pod.k8_name, ports_dict = ports_dict)
