import json
import os
import stat
import time
import timeit
import shutil
from datetime import datetime, timezone
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

from __init__ import TapisResult


class VolumesError(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message

def get_nfs_ip() -> str:
    # We need to get the nfs ip from k8 services
    nfs_nfs_ip = ""
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

    return nfs_nfs_ip


def files_mkdir(path: str = "", tenant_id: str = "", base_path: str = "") -> None:
    """ mkdir in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug("top of volume_utils.files_mkdir().")
    # Normalize path
    path = os.path.abspath(path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    # Note: os.makedirs(path) will give 'FileExistsError' whether file or folder already exists
    # Note: os.makedirs(path, exist_ok) will give 'FileExistsError' only for files already existing
    try:
        os.makedirs(f"{base_path}/{path}", exist_ok=True)
    except FileExistsError:
        msg = f"Got exception trying to run mkdir. File or folder already exists in path: {path}"
        logger.info(msg)
        raise VolumesError(msg)
    except Exception as e:
        msg = f"Got exception trying to run mkdir. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)
    
    logger.info(f"Successfully ran files.mkdir. path: {path}.")

def list_files(path, recursive=False, depth=0):
    """
    List all files in a directory, optionally recursively up to a specified depth.

    Args:
        path (str): The path to the directory to list files from.
        recursive (bool, optional): Whether to list files recursively. Defaults to False.
        depth (int, optional): The maximum depth to list files when `recursive` is True. Defaults to 0.

    Returns:
        list: A list of dictionaries, where each dictionary represents a file and has the following keys:
            - name (str): The name of the file.
            - type (str): The type of the file, which can be one of the following: 'file', 'dir', 'symbolic_link', 'other/unknown'.
            - owner_uid (int): The user ID of the file owner.
            - group_gid (int): The group ID of the file owner.
            - last_modified (str): A string representing the date and time when the file was last modified.
            - size (int): The size of the file in bytes.
            - nativePermissions (str): The native permissions of the file.

    """
    files = []
    for file in os.listdir(path):
        file_path = os.path.abspath(os.path.join(path, file))
        file_stat = os.stat(file_path)
        file_type = ''
        if os.path.isfile(file_path):
            file_type = 'file'
        elif os.path.isdir(file_path):
            file_type = 'dir'
        elif os.path.islink(file_path):
            file_type = 'symbolic_link'
        else:
            file_type = 'other/unknown'
        file_info = {
            'path': file_path,
            'name': file,
            'type': file_type,
            'size': file_stat.st_size,
            'lastModified': datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'nativePermissions': stat.filemode(file_stat.st_mode)[1:], # Remove leading file type character
            'owner': file_stat.st_uid,
            'group': file_stat.st_gid
            # Files also gives mimeType, nativePermissions, and url, we're going to ignore that.
        }
        files.append(file_info)
        if recursive and os.path.isdir(file_path) and depth > 0:
            files.extend(list_files(file_path, recursive=True, depth=depth-1))
    return files


def files_listfiles(path: str, limit: int = 1000, offset:int = 0, recurse: bool = False, tenant_id: str = "", base_path: str = "") -> List[TapisResult]:
    """ list in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug(f"top of volume_utils.files_listfiles(), using tenant: {tenant_id}.")
    # Normalize path
    path = os.path.abspath(path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    # We expect list_files to give FileNotFoundError, if no pre-existing folder/file
    try:
        ls_files = list_files(
            path = f"{base_path}/{path}",
            recursive = recurse,
            depth = 2)
    except FileNotFoundError:
        msg = f"No folder/file found when running list_files. path: {path}"
        logger.info(msg)
        raise FileNotFoundError(msg)
    except Exception as e:
        msg = f"Got exception trying to run list_files. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully ran list_files. path: {path}")
    return ls_files

def files_delete(path: str = "", tenant_id: str = "", base_path: str = "") -> None:
    """ delete folder in nfs vol

    Args:
        name (_type_): _description_
    """
    logger.debug("top of volume_utils.files_delete().")
    # Normalize path
    path = os.path.abspath(path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    # Note: os.remove() will error when a file/folder doesn't exist.
    # delete /{path} folder
    try:
        if os.path.isfile(f"{base_path}/{path}"):
            os.remove(f"{base_path}/{path}")
        else:
            shutil.rmtree(f"{base_path}/{path}")
    except Exception as e:
        msg = f"Got exception trying to delete file. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully deleted file. path: {path}")

def files_insert(file, path: str, tenant_id: str = "", base_path: str = "") -> None:
    logger.debug("top of volume_utils.files_insert().")
    # Normalize path
    path = os.path.abspath(path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    try:
        # Save file to /{path}
        with open(f"{base_path}/{path}", "wb") as f:
            shutil.copyfileobj(file, f)
    except Exception as e:
        msg = f"Got exception trying to save file. path: {path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully uploaded file to path.")

def files_move(source_path:str, new_path: str, tenant_id: str = "", base_path: str = "") -> None:
    logger.debug("top of volume_utils.files_move().")
    # Normalize paths
    source_path = os.path.abspath(source_path)
    new_path = os.path.abspath(new_path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    # move from source_path to new_path
    try:
        shutil.move(f"{base_path}/{source_path}", f"{base_path}/{new_path}")
    except FileNotFoundError:
        msg = f"No folder/file found when moving path. path: {source_path}"
        logger.info(msg)
        raise VolumesError(msg)
    except Exception as e:
        msg = f"Got exception trying to complete move operation. source_path: {source_path}. new_path: {new_path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully moved path.")

def files_copy(source_path:str, new_path: str, tenant_id: str = "", base_path: str = "") -> None:
    logger.debug("top of volume_utils.files_copy().")
    # Normalize paths
    source_path = os.path.abspath(source_path)
    new_path = os.path.abspath(new_path)

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    base_path = os.path.abspath(base_path)

    # copy from source_path to new_path
    try:
        if os.path.isfile(f"{base_path}/{source_path}"):
            shutil.copy(f"{base_path}/{source_path}", f"{base_path}/{new_path}")
        else:
            shutil.copytree(f"{base_path}/{source_path}", f"{base_path}/{new_path}", dirs_exist_ok=True)
    except FileNotFoundError:
        msg = f"No folder/file found when copying path. path: {source_path}"
        logger.info(msg)
        raise VolumesError(msg)
    except Exception as e:
        msg = f"Got exception trying to complete copy operation. source_path: {source_path}. new_path: {new_path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully copied path.")
