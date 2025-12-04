from asyncio import protocols
import http
import re
from sre_constants import ANY
from string import ascii_letters, digits
from secrets import choice
from datetime import datetime
from typing import List, Dict, Literal, Any, Set
from wsgiref import validate
from pydantic import BaseModel, Field, validator, conint, create_model
from codes import PermissionLevel

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
from models_base import TapisApiModel, TapisModel
from models_misc import PermissionsModel, CredentialsModel, LogsModel
from models_images import Image
from typing import Optional


class TemplateBase(TapisApiModel):
    # Required
    template_id: str = Field(..., description = "Name of template.", primary_key=True)
    # Optional
    description: str = Field("", description = "Description of template.")
    metatags: List[str] = Field([], description = "Metadata tags for additional search/listing functionality for the template.", sa_column=Column(ARRAY(String, dimensions=1)))
    archive_message: str = Field("", description = "If set, metadata message to give users of this template.")


class TemplateBaseRead(TemplateBase):
    # Provided
    creation_ts: Optional[datetime] | None = Field(None, description = "Time (UTC) that this template was created.")
    update_ts: Optional[datetime] | None = Field(None, description = "Time (UTC) that this template was updated.")


class TemplateBaseFull(TemplateBaseRead):
    # Provided
    tenant_id: str = Field("", description = "Tapis tenant used during creation of this template.")
    site_id: str = Field("", description = "Tapis site used during creation of this template.")
    # Permissions henceforth has a GLOBAL and TENANT key which determines sharing
    #template_ledger: List[TemplateTag] = Field([], description = "Key: Volume name. Value: List of strs specifying volume folders/files to mount in pod", sa_column=Column(JSON))
    permissions: List[str] = Field([], description = "Template permissions in user:level format.", sa_column=Column(ARRAY(String, dimensions=1)))

TapisTemplateBaseFull = create_model("TapisTemplateBaseFull", __base__= type("_ComboModel", (TemplateBaseFull, TapisModel), {}))


class Template(TapisTemplateBaseFull, table=True, validate=True):
    @validator('template_id')
    def check_template_id(cls, v):
        # In case we want to add reserved keywords.
        reserved_template_ids = ["tags"]
        if v in reserved_template_ids:
            raise ValueError(f"template_id overlaps with reserved template ids: {reserved_template_ids}")
        # Regex match full template_id to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"template_id must be lowercase alphanumeric. First character must be alpha.")
        # template_id char limit = 64
        if len(v) > 64 or len(v) < 3:
            raise ValueError(f"template_id length must be between 3-64 characters. Inputted length: {len(v)}")
        return v

    @validator('permissions')
    def check_permissions(cls, v):
        """Validate template permissions.
        
        Permission formats:
        - username:LEVEL - Standard user permission (e.g., 'jsmith:READ')
        - **:READ - Site-wide public access (all users across all tenants can READ)
        - tenant.<tenant_id>:READ - Tenant-wide public access (all users in specified tenant can READ)
        
        Notes:
        - '**' (site-wide) and 'tenant.*' permissions are admin-only (enforced at API layer)
        - Both '**' and 'tenant.*' only allow READ level for security
        - tenant.<tenant_id> allows admins to make templates public to specific tenants
        """
        #By default add author permissions to template.
        if not v:
            v = [f"{g.username}:ADMIN"]
        if v:
            if not isinstance(v, list):
                raise TypeError(f"permissions must be list. Got {type(v).__name__}.")
            for arg in v:
                if not isinstance(arg, str):
                    raise TypeError(f"permissions must be list of permissions (str). Got {type(arg).__name__}.")
                # Check that permission is in user:level format
                if ":" not in arg or len(arg.split(":")) != 2:
                    raise ValueError(f"permission '{arg}' is not in user:level format.")
                user, level = arg.split(":")
                
                # Handle site-wide wildcard '**'
                if user == "**":
                    # Site-wide permission level must be READ only
                    if level not in ["READ"]:
                        raise ValueError(f"Permission '{arg}' is not allowed. Site-wide wildcard '**' may only have READ level permissions.")
                # Handle tenant-wide 'tenant.<tenant_id>' format
                elif user.startswith("tenant."):
                    tenant_id = user[7:]  # Extract tenant_id after 'tenant.'
                    if not tenant_id:
                        raise ValueError(f"Permission '{arg}' is invalid. 'tenant.' must be followed by a tenant_id (e.g., 'tenant.dev:READ').")
                    # Validate tenant_id format (alphanumeric with hyphens/underscores)
                    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", tenant_id):
                        raise ValueError(f"Permission '{arg}' has invalid tenant_id. tenant_id must be alphanumeric (hyphens/underscores allowed).")
                    # Tenant-wide permission level must be READ only
                    if level not in ["READ"]:
                        raise ValueError(f"Permission '{arg}' is not allowed. Tenant-wide 'tenant.*' may only have READ level permissions.")
                # Handle standard username permissions
                elif not user.isascii() or not re.fullmatch(r"[a-zA-Z0-9_@\-]+", user):
                    raise ValueError(f"User part of permission '{arg}' must be alphanumeric or use hyphen.")
        return v
    
    @validator('metatags')
    def check_tags(cls, v):
        if v:
            if not isinstance(v, list):
                raise TypeError(f"metatags must be list. Got {type(v).__name__}.")
            for arg in v:
                if not isinstance(arg, str):
                    raise TypeError(f"metatags must be list of tags (str). Got {type(arg).__name__}.")
        return v

    @validator('description')
    def check_description(cls, v):
        # ensure description is all ascii
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")            
        # make sure description < 1600 characters
        if len(v) > 1600:
            raise ValueError(f"description field must be less than 1600 characters. Inputted length: {len(v)}")
        return v

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('creation_ts')
    def check_creation_ts(cls, v):
        return datetime.utcnow()
    
    @validator('update_ts')
    def check_update_ts(cls, v):
        return datetime.utcnow()

    def display(self):
        display = self.dict()
        # Remove full fields not in read
        #display.pop('template_ledger')
        display.pop('tenant_id')
        display.pop('permissions')
        display.pop('site_id')

        return display

    @classmethod
    def db_get_all_with_permission(cls, user, level, tenant, site):
        """
        Get all and ensure permission exists.
        """
        # we use incoming tenant to check if user in correct tenant to access * templates.
        incoming_tenant = tenant
        site, tenant, store = cls.get_site_tenant_session(tenant="siteadmintable", site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all_with_permissions() for tenant.site: {tenant}.{site}')

        # Get list of level specified + levels above.
        authorized_levels = PermissionLevel(level).authorized_levels()

        # Create list of permissions user needs to access this resource
        # In the case of level=USER, USER and ADMIN work, so: ["cgarcia:ADMIN", "cgarcia:USER"]
        permission_list = []
        for authed_level in authorized_levels:
            permission_list.append(f"{user}:{authed_level}")
            permission_list.append(f"tenant.{incoming_tenant}:{authed_level}")
            permission_list.append(f"**:{authed_level}")

        # Create statement
        stmt = select(Template).where(Template.permissions.overlap(permission_list))   

        # Run command
        results = store.run("execute", stmt, scalars=True, all=True)

        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results



class NewTemplate(TemplateBase):
    pass


class UpdateTemplate(TapisApiModel):
    """
    Object with fields that users are allowed to specify for the Pod class.
    """
    # Optional
    description: str = Field("", description = "Description of template.")
    metatags: List[str] = Field([], description = "Metadata tags for additional search/listing functionality for the template.", sa_column=Column(ARRAY(String, dimensions=1)))
    archive_message: str = Field("", description = "If set, metadata message to give users of this template.")


class TemplateResponseModel(TemplateBaseRead):
    pass


### Templates
class TemplateResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: TemplateResponseModel
    status: str
    version: str

class TemplatesResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[TemplateResponseModel]
    status: str
    version: str

class TemplatePermissionsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PermissionsModel
    status: str
    version: str


class TemplateDeleteResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str
