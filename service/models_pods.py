from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from wsgiref import validate
from pydantic import BaseModel, Field, validator, root_validator, conint
from codes import PERMISSION_LEVELS, PermissionLevel

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from __init__ import t

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel, CredentialsModel, LogsModel


class Networking(TapisModel):
    protocol: str =  Field("http", description = "Whether to use http or tcp routing for requests to this pod.")
    port: int = Field(5000, description = "Pod port to expose via networking.url in this networking object.")
    url: str = Field("", description = "URL used to access the port of the pod defined in this networking object.")

    @validator('protocol')
    def check_protocol(cls, v):
        v = v.lower()
        valid_protocols = ['http', 'tcp', 'postgres']
        if v not in valid_protocols:
            raise ValueError(f"networking.protocol must be one of the following: {valid_protocols}.")
        return v

    @validator('port')
    def check_port(cls, v):
        if 10 > v  or v > 99999:
            raise ValueError(f"networking.port must be an int with 2 to 5 digits. Got port: {v}")
        return v

    @validator('url')
    def check_url(cls, v):
        if v:
            # Regex match to ensure url is safe with only [A-z0-9.-] chars.
            res = re.fullmatch(r'[a-z][a-z0-9.-]+', v)
            if not res:
                raise ValueError(f"networking.url can only contain lowercase alphanumeric characters, periods, and hyphens.")
            # pod_id char limit = 64
            if len(v) > 128:
                raise ValueError(f"networking.url length must be below 128 characters. Inputted length: {len(v)}")
        return v

class Resources(TapisModel):
    # CPU/Mem defaults are set in configschema.json
    cpu_request: int = Field(conf.default_pod_cpu_request, description = "CPU allocation pod requests at startup. In millicpus (m). 1000 = 1 cpu.")
    cpu_limit: int = Field(conf.default_pod_cpu_limit, description = "CPU allocation pod is allowed to use. In millicpus (m). 1000 = 1 cpu.")
    mem_request: int = Field(conf.default_pod_mem_request, description = "Memory allocation pod requests at startup. In megabytes (Mi)")
    mem_limit: int = Field(conf.default_pod_mem_limit, description = "Memory allocation pod is allowed to use. In megabytes (Mi)")

    @validator('cpu_request', 'cpu_limit')
    def check_cpu_resources(cls, v):
        if conf.minimum_pod_cpu_val > v  or v > conf.maximum_pod_cpu_val:
            raise ValueError(
                f"resources.cpu_x out of bounds. Received: {v}. Maximum: {conf.maximum_pod_cpu_val}. Minimum: {conf.minimum_pod_cpu_val}.",
                 " User requires extra role to break bounds. Contact admin."
                )
        return v

    @validator('mem_request', 'mem_limit')
    def check_mem_resources(cls, v):
        if conf.minimum_pod_mem_val > v  or v > conf.maximum_pod_mem_val:
            raise ValueError(
                f"resources.mem_x out of bounds. Received: {v}. Maximum: {conf.minimum_pod_mem_val}. Minimum: {conf.maximum_pod_mem_val}.",
                 " User requires extra role to break bounds. Contact admin."
                )
        return v

    @root_validator(pre=False)
    def ensure_request_lessthan_limit(cls, values):
        cpu_request = values.get("cpu_request")
        cpu_limit = values.get("cpu_limit")
        mem_request = values.get("mem_request")
        mem_limit = values.get("mem_limit")

        # Check cpu values
        if cpu_request and cpu_limit and cpu_request > cpu_limit:
            raise ValueError(f"resources.cpu_x found cpu_request({cpu_request}) > cpu_limit({cpu_limit}). Request must be less than limit.")

        # Check mem values
        if mem_request and mem_limit and mem_request > mem_limit:
            raise ValueError(f"resources.mem_x found mem_request({mem_request}) > mem_limit({mem_limit}). Request must be less than limit.")

        return values


class VolumeMount(TapisModel):
    type: str =  Field(..., description = "Type of volume to attach.")
    mount_path: str = Field(..., description = "Path to mount volume to.")
    sub_path: str = Field(..., description = "Path to mount volume to.")

    @validator('type')
    def check_type(cls, v):
        v = v.lower()
        valid_types = ['tapisvolume', 'pvc']
        if v not in valid_types:
            raise ValueError(f"volumemount.type must be one of the following: {valid_types}.")
        return v

    @validator('sub_path')
    def check_sub_path(cls, v):
        return v


class Pod(TapisModel, table=True, validate=True):
    # Required
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    command: List[str] | None = Field(None, description = "Command to run in pod.", sa_column=Column(ARRAY(String)))
    environment_variables: Dict[str, Any] = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.", sa_column=Column(ARRAY(String)))
    roles_required: List[str] = Field([], description = "Roles required to view this pod.", sa_column=Column(ARRAY(String)))
    status_requested: str = Field("ON", description = "Status requested by user, ON or OFF.")
    volume_mounts: Dict[str, VolumeMount] = Field({}, description = "Key: Volume name. Value: List of strs specifying volume folders/files to mount in pod", sa_column=Column(JSON))
    time_to_stop_default: int = Field(43200, description = "Default time (sec) for pod to run from instance start. -1 for unlimited. 12 hour default.")
    time_to_stop_instance: int | None = Field(None, description = "Time (sec) for pod to run from instance start. Reset each time instance is started. -1 for unlimited. None uses default.")
    networking: Dict[str, Networking] = Field({"default": {"protocol": "http", "port": 5000}}, description = "Networking information. {'url_suffix': {'protocol': 'http'  'tcp', 'port': int}/}", sa_column=Column(JSON))
    resources: Resources = Field({}, description = "Pod resource management", sa_column=Column(JSON))

    # Provided
    time_to_stop_ts: datetime | None = Field(None, description = "Time (UTC) that this pod is scheduled to be stopped. Change with time_to_stop_instance.")
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this pod.")
    site_id: str = Field("", description = "Tapis site used during creation of this pod.")
    k8_name: str = Field("", description = "Name to use for Kubernetes name.")
    status: str = Field("STOPPED", description = "Current status of pod.")
    status_container: Dict = Field({}, description = "Status of container if exists. Gives phase.", sa_column=Column(JSON))
    data_attached: List[str] = Field([], description = "Data attached.", sa_column=Column(ARRAY(String)))
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod", sa_column=Column(ARRAY(String)))
    creation_ts: datetime | None = Field(datetime.utcnow(), description = "Time (UTC) that this pod was created.")
    update_ts: datetime | None = Field(datetime.utcnow(), description = "Time (UTC) that this pod was updated.")
    start_instance_ts: datetime | None = Field(None, description = "Time (UTC) that this pod instance was started.")
    logs: str = Field("", description = "Logs from kubernetes pods, useful for debugging and reading results.")
    permissions: List[str] = Field([], description = "Pod permissions for each user.", sa_column=Column(ARRAY(String, dimensions=1)))

    # attempt_naive_import:
    # naive_import_command: str | None = None
    # custom_import_command:
    # custom_refresh_command:
    # auto_refresh: bool

    @validator('pod_id')
    def check_pod_id(cls, v):
        # In case we want to add reserved keywords.
        reserved_pod_ids = []
        if v in reserved_pod_ids:
            raise ValueError(f"pod_id overlaps with reserved pod ids: {reserved_pod_ids}")
        # Regex match full pod_id to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"pod_id must be lowercase alphanumeric. First character must be alpha.")
        # pod_id char limit = 64
        if len(v) > 64 or len(v) < 3:
            raise ValueError(f"pod_id length must be between 3-64 characters. Inputted length: {len(v)}")
        return v

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('permissions')
    def check_permissions(cls, v):
        #By default add author permissions to model.
        if not v:
            v = [f"{g.username}:ADMIN"]
        return v

    @validator('environment_variables')
    def check_environment_variables(cls, v):
        if v:
            if not isinstance(v, dict):
                raise TypeError(f"environment_variable must be dict. Got {type(v).__name__}.")
            for env_key, env_val in v.items():
                if not isinstance(env_key, str):
                    raise TypeError(f"environment_variable key must be str. Got {type(env_key).__name__}.")
                if not isinstance(env_val, str):
                    raise TypeError(f"environment_variable val must be str. Got {type(env_val).__name__}.")
        return v

    @validator('volume_mounts')
    def check_volume_mounts(cls, v):
        if v:
            if not isinstance(v, dict):
                raise TypeError(f"volume_mounts must be dict. Got {type(v).__name__}.")
            for vol_name, vol_mounts in v.items():
                if not isinstance(vol_name, str):
                    raise TypeError(f"volume_mounts key must be str. Got {type(vol_name).__name__}.")
                if not vol_mounts:
                    raise ValueError(f"volume_mounts val must exist")
                vol_name_regex = re.fullmatch(r'[a-z][a-z0-9]+', vol_name)
                if not vol_name_regex:
                    raise ValueError(f"volume_mounts key must be lowercase alphanumeric. First character must be alpha.")
        return v


    @validator('pod_template')
    def check_pod_template(cls, v):
        templates = ["neo4j", "postgres"]
        custom_allow_list = conf.image_allow_list or []

        #templates/neo4j
        #tuyamei/xx:ANY
        #postgres:ANY
        # config -> health -> update service db
        # api -> special role -> update "tenant" db

        if v.startswith("custom-"):
            if v.replace("custom-", "") not in custom_allow_list:
                raise ValueError(f"Custom pod_template images must be in allowlist.")
        elif v not in templates:
            raise ValueError(f"pod_template must be one of the following: {templates}.")
        return v

    @validator('time_to_stop_default')
    def check_time_to_stop_default(cls, v):
        if v != -1 and v < 300:
            raise ValueError(f"Pod time_to_stop_default must be -1 or be greater than 300 seconds.")
        return v

    @validator('time_to_stop_instance')
    def check_time_to_stop_instance(cls, v):
        if v and v != -1 and v < 300:
            raise ValueError(f"Pod time_to_stop_instance must be -1 or be greater than 300 seconds.")
        return v

    @validator('networking')
    def check_networking(cls, v):
        if v:
            # Only allow 3 url:port pairs per pod. Trying to keep services minimal.
            # I have uses for 2 ports, not 3, but might as well keep it available.
            if len(v) > 3:
                raise ValueError(f"networking dictionary may only contain up to 3 stanzas")

            # Check keys in networking dict
            # Check key is str, and properly formatted, this should be suffix to urls. "default" means no suffix.
            for env_key, env_val in v.items():
                if not isinstance(env_key, str):
                    raise TypeError(f"networking key must be str. Got type {type(env_key).__name__}.")
                res = re.fullmatch(r'[a-z0-9]+', env_key)
                if not res:
                    raise ValueError(f"networking key must be lowercase alphanumeric. Default if 'default'.")
                if len(env_key) > 64 or len(env_key) < 3:
                    raise ValueError(f"networking key length must be between 3-64 characters. Inputted length: {len(v)}")
        return v

    @root_validator(pre=False)
    def set_networking(cls, values):
        pod_template = values.get('pod_template')
        if pod_template == "neo4j":
            values['networking'] = {"default": Networking(protocol='tcp', port='7687')}
        if pod_template == "postgres":
            values['networking'] = {"default": Networking(protocol='postgres', port='5432')}
        return values

    @root_validator(pre=False)
    def set_k8_name_and_networking_urls(cls, values):
        # NOTE: Pydantic loops during validation, so for a few calls, tenant_id and site_id will be NONE.
        # Must account for this. By end of loop, everything will be set properly.
        # In this case "tacc" tenant is backup.
        site_id = values.get('site_id')
        tenant_id = values.get('tenant_id') or "tacc"
        pod_id = values.get('pod_id')
        ### k8_name: pods-<site>-<tenant>-<pod_id>
        values['k8_name'] = f"pods-{site_id}-{tenant_id}-{pod_id}"
        ### url: podname-networking_name.pods.tacc.develop.tapis.io
        # base_url in the form of https://tacc.develop.tapis.io.
        logger.debug("Fetching base_url for k8_name Pod root_validator from tenant_cache")
        base_url = t.tenant_cache.get_tenant_config(tenant_id=tenant_id).base_url

        # Ensure the object already exists, this function loops a lot before value is set.
        if values.get('networking'):
            for net_name, net_info in values['networking'].items():
                # The Networking model needs to be transformed to a dict if it's being used. When we get with alchemy
                # the entire object is already a dict though. So we always expect a dict.
                if not isinstance(net_info, dict):
                    net_info = net_info.dict()

                # if networking.name is specified we add it to the pod_name of our url after a hyphen. 'default' does not do this.
                # i.e. "podname-networkname" instead of just "podname" when networking.name == 'default'
                if net_name == 'default':
                    url = base_url.replace("https://", f"{pod_id}.pods.")
                else:
                    url = base_url.replace("https://", f"{pod_id}-{net_name}.pods.")

                # Set value to Networking object.
                values['networking'][net_name] = Networking(protocol=net_info['protocol'],
                                                            port=net_info['port'],
                                                            url=url)
        return values

    def display(self):
        display = self.dict()
        display.pop('logs')
        display.pop('k8_name')
        display.pop('tenant_id')
        display.pop('permissions')
        display.pop('site_id')
        display.pop('data_attached')
        display.pop('roles_inherited')
        return display

    @classmethod
    def db_get_all_with_permission(cls, user, level, tenant, site):
        """
        Get all and ensure permission exists.
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all_with_permissions() for tenant.site: {tenant}.{site}')

        # Get list of level specified + levels above.
        authorized_levels = PermissionLevel(level).authorized_levels()

        # Create list of permissions user needs to access this resource
        # In the case of level=USER, USER and ADMIN work, so: ["cgarcia:ADMIN", "cgarcia:USER"]
        permission_list = []
        for authed_level in authorized_levels:
            permission_list.append(f"{user}:{authed_level}")

        # Create statement
        stmt = select(Pod).where(Pod.permissions.overlap(permission_list))

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewPod(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_id: str = Field(..., description = "Name of this pod.")
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    command: List[str] | None = Field(None, description = "Command to run in pod.", sa_column=Column(ARRAY(String)))
    networking: Dict[str, Networking] = Field({"default": {"protocol": "http", "port": 5000}}, description = "Networking information. {'url_suffix': {'protocol': 'http'  'tcp', 'port': int}/}", sa_column=Column(JSON))
    environment_variables: Dict = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")
    volume_mounts: Dict[str, VolumeMount] = Field({}, description = "Key: Volume name. Value: List of strs specifying volume folders/files to mount in pod", sa_column=Column(JSON))
    time_to_stop_default: int = Field(43200, description = "Default time (sec) for pod to run from instance start. -1 for unlimited. 12 hour default.")
    time_to_stop_instance: int | None = Field(None, description = "Time (sec) for pod to run from instance start. Reset each time instance is started. -1 for unlimited. 12 hour default.")
    resources: Resources = Field({}, description = "Pod resource management", sa_column=Column(JSON))


class UpdatePod(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_id: str = Field(..., description = "Name of this pod.")
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    command: List[str] | None = Field(None, description = "Command to run in pod.", sa_column=Column(ARRAY(String)))
    networking: Dict[str, Networking] = Field({"default": {"protocol": "http", "port": 5000}}, description = "Networking information. {'url_suffix': {'protocol': 'http'  'tcp', 'port': int}/}", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")
    volume_mounts: Dict[str, VolumeMount] = Field({}, description = "Key: Volume name. Value: List of strs specifying volume folders/files to mount in pod", sa_column=Column(JSON))
    time_to_stop_instance: int | None = Field(None, description = "Time (sec) for pod to run from instance start. Reset each time instance is started. -1 for unlimited. 12 hour default.")
    resources: Resources = Field({}, description = "Pod resource management", sa_column=Column(JSON))


class PodResponseModel(TapisApiModel):
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")
    description: str = Field("", description = "Description of this pod.")
    command: List[str] | None = Field(None, description = "Command to run in pod.", sa_column=Column(ARRAY(String)))
    networking: Dict[str, Networking] = Field({"default": {"protocol": "http", "port": 5000}}, description = "Networking information. {'url_suffix': {'protocol': 'http'  'tcp', 'port': int}/}", sa_column=Column(JSON))
    environment_variables: Dict = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.", sa_column=Column(ARRAY(String)))
    roles_required: List[str] = Field([], description = "Roles required to view this pod.", sa_column=Column(ARRAY(String)))
    status_requested: str = Field("ON", description = "Status requested by user, ON or OFF.")
    status: str = Field("STOPPED", description = "Current status of pod.")
    status_container: Dict = Field({}, description = "Status of container if exists. Gives phase.", sa_column=Column(JSON))
    data_attached: List[str] = Field([], description = "Data attached.", sa_column=Column(ARRAY(String)))
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod", sa_column=Column(ARRAY(String)))
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was updated.")
    start_instance_ts: datetime | None = Field(None, description = "Time (UTC) that this pod instance was started.")
    volume_mounts: Dict[str, VolumeMount] = Field({}, description = "Key: Volume name. Value: List of strs specifying volume folders/files to mount in pod", sa_column=Column(JSON))
    time_to_stop_default: int = Field(43200, description = "Default time (sec) for pod to run from instance start. -1 for unlimited. 12 hour default.")
    time_to_stop_instance: int | None = Field(None, description = "Time (sec) for pod to run from instance start. Reset each time instance is started. -1 for unlimited. 12 hour default.")
    time_to_stop_ts: datetime | None = Field(None, description = "Time (UTC) that this pod is scheduled to be stopped. Change with time_to_stop_instance.")
    resources: Resources = Field({}, description = "Pod resource management", sa_column=Column(JSON))


class Password(TapisModel, table=True, validate=True):
    # Required
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    # Provided
    admin_username: str = Field("podsservice", description = "Admin username for pod.")
    admin_password: str = Field(None, description = "Admin password for pod.")
    user_username: str = Field(None, description = "User username for pod.")
    user_password: str = Field(None, description = "User password for pod.")
    # Provided
    tenant_id: str = Field(g.request_tenant_id, description = "Tapis tenant used during creation of this password's pod.")
    site_id: str = Field(g.site_id, description = "Tapis site used during creation of this password's pod.")

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('admin_password')
    def add_admin_password(cls, v):
        password = ''.join(choice(ascii_letters + digits) for i in range(30))
        return password

    @validator('user_password')
    def add_user_password(cls, v):
        password = ''.join(choice(ascii_letters + digits) for i in range(30))
        return password

    @root_validator(pre=False)
    def set_user_username(cls, values):
        values['user_username'] = values.get('pod_id')
        return values


class PodResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PodResponseModel
    status: str
    version: str


class PodsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[PodResponseModel]
    status: str
    version: str


class DeletePodResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class PodPermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class PodLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogsModel
    status: str
    version: str


class PodCredentialsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: CredentialsModel
    status: str
    version: str
