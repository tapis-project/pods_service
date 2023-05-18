from fcntl import DN_DELETE
import json
import os
import time
import timeit
import datetime
import random
from typing import Literal, Dict, List, Tuple

from jinja2 import Environment, FileSystemLoader
from kubernetes import client, config
from requests.exceptions import ReadTimeout, ConnectionError

from tapisservice.logs import get_logger
logger = get_logger(__name__)
from tapisservice.tapisfastapi.utils import g, ok

from tapipy.errors import NotFoundError
from tapisservice.config import conf
from codes import AVAILABLE, CREATING
from stores import SITE_TENANT_DICT
from stores import pg_store
from sqlmodel import select
from kubernetes_utils import list_all_services

from __init__ import t, TapisResult


class VolumesError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message

def get_nfs_ips() -> Tuple[str, str]:
    nfs_ssh_ip = conf.get('nfs_develop_remote_url')
    if not nfs_ssh_ip:
        # We need to get the nfs ip from k8 services
        idx = 0
        while idx < 10:
            nfs_services = []
            for k8_service in list_all_services(filter_str="pods-nfs-ssh"):
                k8_name = k8_service.metadata.name
                nfs_services.append({'service_info': k8_service,
                                        'k8_name': k8_name})
            # Checking how many services met the filter (should hopefully be only one)
            match len(nfs_services):
                case 1:
                    try:
                        nfs_ssh_ip = nfs_services[0]['service_info'].spec.cluster_ip
                        break
                    except Exception as e:
                        logger.info(f"Exception while getting pods-nfs-ssh ip from K8 services. e: {e}")
                case 0:
                    logger.info(f"Couldn't find service matching pods-nfs-ssh. Trying again.")
                    pass
                case _:
                    logger.info(f"Got >1 services matching pods-nfs-ssh. Number of services: {len(nfs_services)}. Trying again.")                
                    pass
            # Increment and have a short wait
            idx += 1
            time.sleep(1)

        # Reached end of idx limit
        else:
            msg = f"Couldn't find service matching pods-nfs-ssh. Required, breaking."
            logger.info(msg)
            raise RuntimeError(msg)
        
    nfs_nfs_ip = ""
    # We need to get the nfs ip from k8 services
    idx = 0
    while idx < 10:
        nfs_services = []
        for k8_service in list_all_services(filter_str="pods-nfs"):
            k8_name = k8_service.metadata.name
            # pods-nfs also matches pods-nfs-ssh, so we manually pass that case
            if "pods-nfs-ssh" in k8_name:
                continue
            nfs_services.append({'service_info': k8_service,
                                    'k8_name': k8_name})
        # Checking how many services met the filter (should hopefully be only one)
        match len(nfs_services):
            case 1:
                try:
                    nfs_nfs_ip = nfs_services[0]['service_info'].spec.cluster_ip
                    break
                except Exception as e:
                    logger.info(f"Exception while getting pods-nfs ip from K8 services. e: {e}")
            case 0:
                logger.info(f"Couldn't find service matching pods-nfs. Trying again.")
                pass
            case _:
                logger.info(f"Got >1 services matching pods-nfs. Number of services: {len(nfs_services)}. Trying again.")                
                pass
        # Increment and have a short wait
        idx += 1
        time.sleep(1)

    # Reached end of idx limit
    else:
        msg = f"Couldn't find service matching pods-nfs. Required, breaking."
        logger.info(msg)
        raise RuntimeError(msg)

    return nfs_ssh_ip, nfs_nfs_ip


def files_mkdir(system_id: str, path: str = "", tenant_id: str = "", user: str = "pods") -> None:
    """ mkdir in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug("top of volume_utils.files_mkdir().")
    # Normalize path
    path = path.replace("///", "/").replace("//", "/")

    # Note: t.files.mkdir will error when a file already exists, will not error if a folder already exists.
    # mkdir in files to create /{path}
    try:
        t.files.mkdir(
            systemId = system_id,
            path = path,
            _x_tapis_tenant = tenant_id or g.tenant_id,
            _x_tapis_user = user)
    except Exception as e:
        errormsg = e.__dict__.get('message')
        if errormsg:
            if "Path exists as a file" in errormsg:
                msg = f"Got exception trying to run mkdir. Path already exists as a file. path: {path}"
                logger.info(msg)
                raise VolumesError(msg)
        else:
            msg = f"Got exception trying to run mkdir. path: {path}"
            logger.info(msg)
            raise VolumesError(msg)

    # Share path via Files API
    # t.files.sharePath(systemId=conf.nfs_tapis_system_id, path=f"/{path}", users=['cgarcia', 'testuser2']) 
    
    logger.info(f"Successfully ran files.mkdir. path: {path}.")

def files_listfiles(system_id: str, path: str, tenant_id: str = "", user: str = "pods") -> List[TapisResult]:
    """ list in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug(f"top of volume_utils.files_listfiles(), using tenant: {tenant_id}.")
    # Normalize path
    path = path.replace("///", "/").replace("//", "/")


    # Ensure /{path} does not already exist
    # We expect listFiles to give tapipy.errors.NotFoundError, if no pre-existing folder/file.
    try:
        ls_files = t.files.listFiles(
            systemId = system_id,
            path = path,
            _x_tapis_tenant = tenant_id or g.tenant_id,
            _x_tapis_user = user)
    except NotFoundError:
        msg = f"No folder/file found when running listFiles. path: {path}"
        logger.info(msg)
        raise NotFoundError(msg)
    except Exception as e:
        msg = f"Got exception trying to run listFiles. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully ran files.listFiles. path: {path}")
    return ls_files

def files_delete(system_id: str, path: str = "", tenant_id: str = "", user: str = "pods") -> None:
    """ delete folder in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug("top of volume_utils.files_delete().")
    # Normalize path
    path = path.replace("///", "/").replace("//", "/")

    # Note: t.files.delete will error when a file/folder doesn't exist.
    # delete /{path} folder
    try:
        t.files.delete(
            systemId = system_id,
            path = path,
            _x_tapis_tenant = tenant_id or g.tenant_id,
            _x_tapis_user = user)
    except NotFoundError:
        msg = f"No folder/file found when running delete. path: {path}"
        logger.info(msg)
        return
    except Exception as e:
        msg = f"Got exception trying to run delete. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)

    # Stop sharing path via Files API
    # t.files.sharePath(systemId=conf.nfs_tapis_system_id, path="/{path", users=['cgarcia', 'testuser2']) 

    logger.info(f"Successfully ran files.delete. path: {path}")

def files_insert(system_id: str, file: str, path: str, tenant_id: str = "", user: str = "pods") -> None:
    logger.debug("top of volume_utils.files_insert().")
    # Normalize path
    path = path.replace("///", "/").replace("//", "/")

    # insert to /{path}
    try:
        t.files.insert(
            systemId = system_id,
            path = path,
            file = file,
            _x_tapis_tenant = tenant_id or g.tenant_id,
            _x_tapis_user = user)
    except Exception as e:
        msg = f"Got exception trying to insert into volume folder. path: {path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully ran files.insert.")

def files_movecopy(system_id: str, operation: str, source_path:str, new_path: str, tenant_id: str = "", user: str = "pods") -> None:
    logger.debug("top of volume_utils.files_moveCopy().")
    # Normalize paths
    source_path = source_path.replace("///", "/").replace("//", "/")
    new_path = new_path.replace("///", "/").replace("//", "/")

    if operation not in ["MOVE", "COPY"]:
        msg = f"files_moveCopy expects operation argument with value 'MOVE' or 'COPY', got: {operation}"
        logger.info(msg)
        raise VolumesError(msg)

    # move or copy from source_path to new_path
    try:
        import requests as r
        import json
        res = r.put(f"{t.base_url}/v3/files/ops/{system_id}/{source_path}",
                    data = json.dumps({"operation": operation, "newPath": new_path}),
                    headers = {"X-Tapis-Token": t.service_tokens['admin']['access_token'].access_token,
                               "X-Tapis-User": "pods",
                               "X-Tapis-Tenant": tenant_id or g.tenant_id,
                               "Content-Type": "application/json"})
        # Operation argument is creating conflicts with tapipy.
        # t.files.moveCopy(
        #     systemId = system_id,
        #     operation = operation,
        #     path = source_path,
        #     newPath = new_path,
        #     _x_tapis_tenant = tenant_id or g.tenant_id,
        #     _x_tapis_user = user)
    except NotFoundError:
        msg = f"No folder/file found when running files.moveCopy. path: {source_path}"
        logger.info(msg)
        raise VolumesError(msg)
    except Exception as e:
        msg = f"Got exception trying to complete {operation} operation. source_path: {source_path}. new_path: {new_path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully ran files.moveCopy.")
