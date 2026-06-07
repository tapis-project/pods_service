import re
from datetime import datetime
from typing import List, Dict, Optional
from pydantic import Field, validator, create_model

from codes import PermissionLevel

from stores import pg_store
from tapisservice.tapisfastapi.utils import g
from tapisservice.config import conf
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, select, Column, String, JSON
from models_base import TapisModel, TapisApiModel
from models_misc import PermissionsModel


VALID_RESTART_POLICIES = ["ordered", "parallel"]


class StackBase(TapisApiModel):
    # Required
    stack_id: str = Field(..., description = "Name of this stack. Flat and unique per tenant, like pod_id/volume_id.", primary_key = True)

    # Optional
    description: str = Field("", description = "Description of this stack.")
    restart_policy: str = Field("ordered", description = "How stack actions order member pods. 'ordered' honors each pod's depends_on; 'parallel' acts on all members at once.")


class StackBaseRead(StackBase):
    # Provided
    status: str = Field("", description = "Cached aggregate status of member pods, e.g. '2/3 AVAILABLE'. Maintained by the health loop.")
    from_template: str = Field("", description = "Template tag this stack was instantiated from (e.g. 'n8nstack:queue-v1@...'), if any.")
    creation_ts: datetime | None = Field(None, description = "Time (UTC) that this stack was created.")
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this stack was updated.")


class StackBaseFull(StackBaseRead):
    # Provided
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this stack.")
    site_id: str = Field("", description = "Tapis site used during creation of this stack.")
    created_by: str = Field("", description = "Username that created this stack.")
    permissions: List[str] = Field([], description = "Stack permissions for each user. Member pods inherit max(pod, stack) permission.", sa_column=Column(ARRAY(String, dimensions=1)))
    action_logs: List[str] = Field([], description = "Timestamped log of stack actions and per-pod release/ready transitions.", sa_column=Column(ARRAY(String, dimensions=1)))
    secret_map: Dict[str, str] = Field({}, description = "Shared, stack-scoped secrets (the single source for ${stack:secrets:KEY} references in member pods). Resolved once; member pods hold only references. Redacted from API responses.", sa_column=Column(JSON))


TapisStackBaseFull = create_model("TapisStackBaseFull", __base__= type("_ComboModel", (StackBaseFull, TapisModel), {}))


class Stack(TapisStackBaseFull, table=True, validate=True):
    @validator('stack_id')
    def check_stack_id(cls, v):
        # In case we want to add reserved keywords.
        reserved_stack_ids = []
        if v in reserved_stack_ids:
            raise ValueError(f"stack_id overlaps with reserved stack ids: {reserved_stack_ids}")
        # Regex match full stack_id to ensure a-z0-9, first char alpha.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"stack_id must be lowercase alphanumeric. First character must be alpha.")
        # stack_id char limit = 64
        if len(v) > 64 or len(v) < 3:
            raise ValueError(f"stack_id length must be between 3-64 characters. Inputted length: {len(v)}")
        return v

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('created_by')
    def check_created_by(cls, v):
        # Set once at creation; preserved on subsequent reads.
        return v or g.username

    @validator('restart_policy')
    def check_restart_policy(cls, v):
        if v not in VALID_RESTART_POLICIES:
            raise ValueError(f"restart_policy must be one of {VALID_RESTART_POLICIES}. Got '{v}'.")
        return v

    @validator('creation_ts')
    def check_creation_ts(cls, v):
        return datetime.utcnow()

    @validator('update_ts')
    def check_update_ts(cls, v):
        return datetime.utcnow()

    @validator('permissions')
    def check_permissions(cls, v):
        # By default add author ADMIN permission to model.
        if not v:
            v = [f"{g.username}:ADMIN"]
        return v

    @validator('action_logs')
    def check_action_logs(cls, v):
        if not v:
            v = [f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: Stack created by '{g.username}'"]
        return v

    @validator('description')
    def check_description(cls, v):
        # ensure description is all ascii
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")
        # make sure description < 255 characters
        if len(v) > 255:
            raise ValueError(f"description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    def display(self):
        display = self.dict()
        display.pop('tenant_id')
        display.pop('site_id')
        display.pop('permissions')
        display.pop('secret_map', None)  # never expose shared secret values/sources
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
        stmt = select(Stack).where(Stack.permissions.overlap(permission_list))

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewStack(StackBase):
    """
    Object with fields that users are allowed to specify for the Stack class.
    """
    pass


class UpdateStack(TapisApiModel):
    """
    Object with fields that users are allowed to specify when updating the Stack class.
    """
    description: Optional[str] = Field(None, description = "Description of this stack.")
    restart_policy: Optional[str] = Field(None, description = "How stack actions order member pods: 'ordered' or 'parallel'.")


class StackActionRequest(TapisApiModel):
    """
    Body for POST /pods/stacks/{stack_id}/action.
    """
    action: str = Field(..., description = "Action to apply to all member pods: 'start', 'stop', or 'restart'.")
    force: Optional[bool] = Field(False, description = "For 'stop'/'restart': bypass reverse-order teardown and stop members immediately (ignore dependency order during shutdown). Startup ordering is still honored.")


class JoinStackRequest(TapisApiModel):
    """
    Body for POST /pods/{pod_id}/stack — add a pod to a stack.
    """
    stack_id: str = Field(..., description = "Stack to join. Must already exist and you must have USER+ on it.")


class StackResponseModel(StackBaseRead):
    """
    Response object for Stack class.
    """
    created_by: str = Field("", description = "Username that created this stack.")
    action_logs: List[str] = Field([], description = "Timestamped log of stack actions and per-pod transitions.")
    pods: Optional[List[Dict]] = Field(None, description = "Member pod displays. Present on GET /pods/stacks/{stack_id}; absent on list.")


class StackFromTemplateRequest(TapisApiModel):
    """
    Body for POST /pods/stacks/from-template — instantiate a kind='stack' template tag.
    """
    template: str = Field(..., description = "Stack template tag to instantiate, e.g. 'n8nstack:queue-v1'. Must be a kind='stack' tag.")
    stack_id: str = Field(..., description = "Flat, tenant-unique id for the new stack.")
    pod_ids: Optional[Dict[str, str]] = Field(None, description = "Per-member pod_id override {member_name: pod_id}. Members not listed derive '{stack_id}{name}'.")
    secrets: Optional[Dict[str, str]] = Field(None, description = "Values for the stack template's required secret placeholders, {KEY: value}. Random/optional secrets may be omitted.")
    overrides: Optional[Dict[str, Dict]] = Field(None, description = "Per-member field overrides {member_name: {field: value}} applied on top of the template.")
    description: Optional[str] = Field("", description = "Description for the new stack.")


class SaveStackAsTemplateRequest(TapisApiModel):
    """
    Body for POST /pods/stacks/{stack_id}/save_as_template — snapshot a live stack into a tag.
    """
    template_id: str = Field(..., description = "Template to add the snapshot tag to (must already exist).")
    tag: str = Field("latest", description = "Tag name for the snapshot.")
    commit_message: Optional[str] = Field("", description = "Commit message for the snapshot tag.")


class StackUpdateRequest(TapisApiModel):
    """
    Body for POST /pods/stacks/{stack_id}/update — re-derive a stack from a newer stack-template tag.

    Reviewed/explicit (never automatic): use ?dry_run=true to get the per-member plan
    (add/remove/recreate/patch) without side effects, then POST again to apply.
    """
    template: Optional[str] = Field(None, description = "Target stack-template ref to update to, e.g. 'immich:prod@2026-...'. Omit to use the newest tag matching the stack's pinned moving tag.")
    pod_ids: Optional[Dict[str, str]] = Field(None, description = "Per-member pod_id override for newly-added members, {member_name: pod_id}.")
    secrets: Optional[Dict[str, str]] = Field(None, description = "Values for any new required secret placeholders introduced by the target tag.")
    confirm: Optional[str] = Field(None, description = "Typed stack_id; required when the plan deletes or rebuilds members (destructive).")


class StackResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: StackResponseModel
    status: str
    version: str


class StacksResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[StackResponseModel]
    status: str
    version: str


class DeleteStackResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class StackPermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str
