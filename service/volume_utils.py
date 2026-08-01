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
    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_path = _contained_path(base_path, path)

    # Note: os.makedirs(path) will give 'FileExistsError' whether file or folder already exists
    # Note: os.makedirs(path, exist_ok) will give 'FileExistsError' only for files already existing
    try:
        os.makedirs(full_path, exist_ok=True)
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
    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_path = _contained_path(base_path, path)

    # We expect list_files to give FileNotFoundError, if no pre-existing folder/file
    try:
        ls_files = list_files(
            path = full_path,
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
    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_path = _contained_path(base_path, path)

    # Note: os.remove() will error when a file/folder doesn't exist.
    # delete /{path} folder
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
        else:
            shutil.rmtree(full_path)
    except Exception as e:
        msg = f"Got exception trying to delete file. path: {path}"
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully deleted file. path: {path}")


def _contained_path(base_path: str, path: str) -> str:
    """Join path under base_path and REFUSE to leave it. os.path.join drops
    base_path entirely if `path` is absolute, and normpath keeps leading '..',
    so join-then-check is the only safe form. Callers should already validate
    inputs (e.g. sub_path), but this is the last-line sink guard so no future
    caller can traverse out of the tenant base."""
    base_path = os.path.abspath(base_path)
    full_path = os.path.abspath(os.path.join(base_path, path.lstrip("/")))
    if full_path != base_path and not full_path.startswith(base_path + os.sep):
        raise VolumesError(f"Resolved path escapes the base directory (path traversal blocked): {path}")
    return full_path


def object_root(kind: str, object_id: str, tenant_id: str = "") -> str:
    """NFS root of ONE volume/snapshot — the containment boundary for endpoints
    that accept a user-supplied sub-path.

    Containing at the TENANT base is not enough. '/volumes/volA/../volB' never
    leaves the tenant, so a tenant-base guard resolves it happily to a sibling
    volume the caller has no permission on — and '/volumes/volA/../..' resolves
    to the tenant root, listing every volume and snapshot in the tenant. Passing
    this as base_path makes _contained_path refuse both.

    kind is 'volumes' or 'snapshots'.
    """
    if kind not in ("volumes", "snapshots"):
        raise VolumesError(f"object_root: unknown kind {kind!r}")
    return f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}/{kind}/{object_id}"


def file_exists(path: str, tenant_id: str = "", base_path: str = "") -> bool:
    """
    Check if a file exists in NFS.

    Args:
        path: Path to check (relative to base_path)
        tenant_id: Tenant ID for path resolution
        base_path: Optional explicit base path

    Returns:
        True if file exists, False otherwise
    """
    logger.debug(f"top of volume_utils.file_exists() - path: {path}")

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"

    full_path = _contained_path(base_path, path)
    return os.path.isfile(full_path)


def files_write_content(content: str, path: str, tenant_id: str = "", base_path: str = "", permissions: str = "0644") -> None:
    """
    Write string content directly to a file in NFS.
    
    Args:
        content: String content to write
        path: Path to write to (relative to base_path)
        tenant_id: Tenant ID for path resolution
        base_path: Optional explicit base path
        permissions: Unix file permissions as octal string (e.g., '0644')
    """
    logger.debug(f"top of volume_utils.files_write_content() - path: {path}")

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"

    # Containment sink guard: refuse any path that resolves outside base_path
    # (config_content sub_path traversal). Validated at the model too; this is
    # defense-in-depth at the write itself.
    full_path = _contained_path(base_path, path)

    # Ensure parent directory exists
    parent_dir = os.path.dirname(full_path)
    os.makedirs(parent_dir, exist_ok=True)
    
    try:
        # Write content to file
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        # Set file permissions
        try:
            mode = int(permissions, 8)
            os.chmod(full_path, mode)
        except (ValueError, OSError) as e:
            logger.warning(f"Failed to set permissions {permissions} on {path}: {e}")
            
        logger.info(f"Successfully wrote content to path: {path}")
    except Exception as e:
        msg = f"Got exception trying to write content to file. path: {path}. Error: {e}"
        logger.error(msg)
        raise VolumesError(msg)


def files_insert(file, path: str, tenant_id: str = "", base_path: str = "") -> None:
    logger.debug("top of volume_utils.files_insert().")
    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_path = _contained_path(base_path, path)

    try:
        # Save file to /{path}
        with open(full_path, "wb") as f:
            shutil.copyfileobj(file, f)
    except Exception as e:
        msg = f"Got exception trying to save file. path: {path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully uploaded file to path.")

def files_download(path: str, zip: bool = False, tenant_id: str = "", base_path: str = "") -> Tuple:
    """
    Stream a file or directory from a given path. If the path is a directory and zip is True,
    the directory is compressed into a ZIP archive and streamed.

    Args:
        path (str): The path to the file or directory to stream.
        zip (bool, optional): Whether to compress the directory into a ZIP archive if the path is a directory. Defaults to False.

    Returns:
        Tuple: A generator function for streaming the file or ZIP archive, and the filename for the Content-Disposition header.
    """
    def file_generator(file_path: str):
        with open(file_path, 'rb') as file:
            yield from file

    logger.debug("top of volume_utils.files_download().")

    # Establish base_path w/ tenant
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    nfs_file_path = _contained_path(base_path, path)
    logger.debug(f"Attempting to download file/dir from path: {nfs_file_path}")
    if os.path.isdir(nfs_file_path):
        if zip:
            # Create a ZIP archive from the directory and stream it
            from zipfile import ZipFile
            from io import BytesIO

            zip_filename = os.path.basename(nfs_file_path.rstrip("/")) + ".zip"
            zip_io = BytesIO()
            with ZipFile(zip_io, 'w') as zip_file:
                for root, dirs, files in os.walk(nfs_file_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, start=nfs_file_path)
                        zip_file.write(file_path, arcname=arcname)
            zip_io.seek(0)

            return (b for b in zip_io), zip_filename
        else:
            raise NotImplementedError("Streaming directories is only supported with query parameter zip=True.")
    elif os.path.isfile(nfs_file_path):
        # Stream the file directly
        filename = os.path.basename(nfs_file_path)
        return file_generator(nfs_file_path), filename
    else:
        raise FileNotFoundError("The specified path does not exist or is not accessible.")

def files_move(source_path:str, new_path: str, tenant_id: str = "", base_path: str = "") -> None:
    logger.debug("top of volume_utils.files_move().")
    # Establish base_path w/ tenant. BOTH ends are contained — a move is a read
    # AND a write, so an escape on either side is a traversal.
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_source = _contained_path(base_path, source_path)
    full_new = _contained_path(base_path, new_path)

    # move from source_path to new_path
    try:
        shutil.move(full_source, full_new)
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
    # Establish base_path w/ tenant. BOTH ends are contained — see files_move.
    base_path = base_path or f"{conf.nfs_base_path}/{tenant_id or g.tenant_id}"
    full_source = _contained_path(base_path, source_path)
    full_new = _contained_path(base_path, new_path)

    # copy from source_path to new_path
    try:
        if os.path.isfile(full_source):
            shutil.copy(full_source, full_new)
        else:
            shutil.copytree(full_source, full_new, dirs_exist_ok=True)
    except FileNotFoundError:
        msg = f"No folder/file found when copying path. path: {source_path}"
        logger.info(msg)
        raise VolumesError(msg)
    except Exception as e:
        msg = f"Got exception trying to complete copy operation. source_path: {source_path}. new_path: {new_path}."
        logger.info(msg)
        raise VolumesError(msg)

    logger.info(f"Successfully copied path.")
