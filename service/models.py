import re
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from wsgiref import validate
from pydantic import BaseModel, Field, validator, root_validator
from codes import PERMISSION_LEVELS, PermissionLevel

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from __init__ import t

from sqlalchemy import UniqueConstraint
from sqlalchemy.inspection import inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, Session, SQLModel, select, JSON, Column, String
from models_base import TapisModel, TapisApiModel


class Pod(TapisModel, table=True, validate=True):
    # Required
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    environment_variables: Dict = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.", sa_column=Column(ARRAY(String)))
    roles_required: List[str] = Field([], description = "Roles required to view this pod.", sa_column=Column(ARRAY(String)))
    status_requested: str = Field("ON", description = "Status requested by user, ON or OFF.")

    # Provided
    tenant_id: str = Field(g.request_tenant_id or 'tacc', description = "Tapis tenant used during creation of this pod.")
    site_id: str = Field(g.site_id or 'tacc', description = "Tapis site used during creation of this pod.")
    k8_name: str = Field(None, description = "Name to use for Kubernetes name.")
    url: str = Field(None, description = "Url used to access this database if it is running.")
    status: str = Field("STOPPED", description = "Current status of pod.")
    status_container: Dict = Field({}, description = "Status of container if exists. Gives phase.", sa_column=Column(JSON))
    data_attached: List[str] = Field([], description = "Data attached.", sa_column=Column(ARRAY(String)))
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod", sa_column=Column(ARRAY(String)))
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was updated.")
    server_protocol: str = Field("http", description = "Protocol to route server with. tcp or http.")
    routing_port: int = Field(5000, description = "Port Nginx points to in Pod.")
    instance_port: int = Field(1, description = "Port that Nginx uses internally to route calls. 52001-52999, 1, or -1.")
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
        if len(v) > 64:
            raise ValueError(f"pod_id must be less than 64 characters. Inputted length: {len(v)}")
        return v

    @validator('instance_port')
    def check_instance_port(cls, v):
        # Ports 49152-65535 are for private/dynamic use.
        # We don't need that many, going to use 52001-52999.
        # port = 1: Indicates to health.py that port should be set there (so it's atomic)
        # port = -1: Indicates that port is not set in the case that pod is STOPPED
        if v not in range(52001, 52999) and v not in [1, -1]:
            raise ValueError(f"instance_port must be in range 52001-52999, 1, or -1.")
        return v

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
            for key, val in v.items():
                if not isinstance(key, str):
                    raise TypeError(f"environment_variable key must be str. Got {type(key).__name__}.")
                if not isinstance(val, str):
                    raise TypeError(f"environment_variable val must be str. Got {type(val).__name__}.")
        return v

    @validator('pod_template')
    def check_pod_template(cls, v):
        templates = ["neo4j"]
        custom_allow_list = ["jstubbs/abaco_test", "tuyamei/smartfoodshed-server"]

        if v.startswith("custom-"):
            if v.replace("custom-", "") not in custom_allow_list:
                raise ValueError(f"Custom pod_template images must be in allowlist.")
        elif v not in templates:
            raise ValueError(f"pod_template must be one of the following: {templates}.")
        return v

    @root_validator(pre=False)
    def set_url_and_k8_name(cls, values):
        # NOTE: Pydantic loops during validation, so for a few calls, tenant_id and site_id will be NONE.
        # Must account for this. By end of loop, everything will be set properly.
        # In this case "tacc" tenant is backup.
        site_id = values.get('site_id')
        tenant_id = values.get('tenant_id') or "tacc"
        pod_id = values.get('pod_id')
        ### k8_name: pods-<site>-<tenant>-<pod_id>
        values['k8_name'] = f"pods-{site_id}-{tenant_id}-{pod_id}"
        ### url: podname.pods.tacc.develop.tapis.io
        # base_url in the form of https://tacc.develop.tapis.io.
        base_url = t.tenant_cache.get_tenant_config(tenant_id=tenant_id).base_url
        # new url in the form of pod_id.tacc.develop.tapis.io
        values['url'] = base_url.replace("https://", f"{pod_id}.pods.")
        return values

    @root_validator(pre=False)
    def set_routing_port_and_protocol_for_templates(cls, values):
        pod_template = values.get('pod_template')
        if pod_template == "neo4j":
            values['routing_port'] = 7687
            values['server_protocol'] = "tcp"
        return values

    def display(self):
        display = self.dict()
        display.pop('logs')
        display.pop('instance_port')
        display.pop('routing_port')
        display.pop('k8_name')
        display.pop('tenant_id')
        display.pop('server_protocol')
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


#schema https://pydantic-docs.helpmanual.io/usage/schema/
class ExportedData(TapisModel, table=True, validate=True):
    # Required
    source_pod: str | None = Field(None, description = "Time (UTC) that this node was created.", primary_key = True)

    # Optional
    tag: List[str] = Field([], description = "Roles required to view this pod")
    description: str | None = Field(None, description = "Time (UTC) that this node was created.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")

    # Provided
    tenant_id: str = Field(None, description = "Tapis tenant used during creation of this pod.")
    site_id: str = Field(None, description = "Tapis site used during creation of this pod.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod")
    export_path: str | None = Field(None, description = "Time (UTC) that this node was created.")
    source_owner: str | None = Field(None, description = "Time (UTC) that this node was created.")


class Password(TapisModel, table=True, validate=True):
    # Required
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    # Provided
    admin_username: str = Field("podsservice", description = "Admin username for pod.")
    admin_password: str = Field(None, description = "Admin password for pod.")
    user_username: str = Field(None, description = "User username for pod.")
    user_password: str = Field(None, description = "User password for pod.")
    
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


class NewPod(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_id: str = Field(..., description = "Name of this pod.")
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    environment_variables: Dict = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")


class UpdatePod(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    pod_id: str = Field(..., description = "Name of this pod.")
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")

    # Optional
    description: str = Field("", description = "Description of this pod.")
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")


class SetPermission(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    user: str = Field(..., description = "User to modify permissions for.")
    level: str = Field(..., description = "Permission level to give the user.")

    @validator('level')
    def check_level(cls, v):
        if v not in PERMISSION_LEVELS:
            raise ValueError(f"level must be in {PERMISSION_LEVELS}")
        return v

class DeletePermission(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Required
    user: str = Field(..., description = "User to delete permissions from.")


class PodResponseModel(TapisApiModel):
    pod_id: str = Field(..., description = "Name of this pod.", primary_key = True)
    pod_template: str = Field(..., description = "Which pod template to use, or which custom image to run, must be on allowlist.")
    description: str = Field("", description = "Description of this pod.")
    environment_variables: Dict = Field({}, description = "Environment variables to inject into k8 pod; Only for custom pods.", sa_column=Column(JSON))
    data_requests: List[str] = Field([], description = "Requested pod names.", sa_column=Column(ARRAY(String)))
    roles_required: List[str] = Field([], description = "Roles required to view this pod.", sa_column=Column(ARRAY(String)))
    status_requested: str = Field("ON", description = "Status requested by user, ON or OFF.")
    url: str = Field(None, description = "Url used to access this database if it is running.")
    status: str = Field("STOPPED", description = "Current status of pod.")
    status_container: Dict = Field({}, description = "Status of container if exists. Gives phase.", sa_column=Column(JSON))
    data_attached: List[str] = Field([], description = "Data attached.", sa_column=Column(ARRAY(String)))
    roles_inherited: List[str] = Field([], description = "Inherited roles required to view this pod", sa_column=Column(ARRAY(String)))
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was updated.")


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


class PermissionsModel(TapisApiModel):
    permissions: List[str] = Field([], description = "Pod permissions for each user.")


class PodPermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class LogsModel(TapisApiModel):
    logs: str = Field("", description = "Logs from kubernetes pods, useful for debugging and reading results.")


class PodLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: LogsModel
    status: str
    version: str


class CredentialsModel(TapisApiModel):
    user_username: str
    user_password: str


class PodCredentialsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: CredentialsModel
    status: str
    version: str
