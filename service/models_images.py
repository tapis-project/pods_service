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
from codes import PERMISSION_LEVELS

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


class ImageBase(TapisApiModel):
    # Required
    ##add uuid primary_Key=True
    image: str = Field(..., description = "Name of image to allow.", primary_key=True)
    # Optional
    tenants: List[str] = Field([], description = "Tenants that can use this image.", sa_column=Column(ARRAY(String)))
    description: str = Field("", description = "Description of image.")

class ImageBaseRead(ImageBase):
    # Provided
    creation_ts: datetime | None = Field(datetime.utcnow(), description = "Time (UTC) that this image was created.")
    added_by: str = Field("", description = "User who added image to allow list.")

class ImageBaseFull(ImageBaseRead):
    pass

TapisImageBaseFull = create_model("TapisImageBaseFull", __base__= type("_ComboModel", (ImageBaseFull, TapisModel), {}))

class Image(TapisImageBaseFull, table=True, validate=True):
    @validator('image')
    def check_image(cls, v):
        # Get rid of tag, we don't check that at all.
        if v.count(":") > 1:
            raise ValueError("image cannot have more than one ':' in the string. Should be used to separate the tag from the image name.")
        if ":" in v:
            v = v.split(":")[0]
        return v

    @validator('added_by')
    def check_added_by(cls, v):
        # Add author user.
        if v == "":
            v  = g.username
        if v == "":
            raise ValueError("added_by field must be set to a valid username.")
        if not v.isascii() or not v.isalnum():
            raise ValueError(f"added_by field must be alphanumeric. Inputted username: {v}")
        # no spaces
        if " " in v:
            raise ValueError(f"added_by field may not contain spaces. Invalid username: '{v}'")
        return v
    
    @validator('tenants')
    def check_tenants(cls, v):
        # ensure tenants is a list of strings
        if not isinstance(v, list):
            raise ValueError(f"tenants field must be a list. Inputted type: {type(v)}")
        for tenant in v:
            if not isinstance(tenant, str):
                raise ValueError(f"tenants field must be a list of strings. Inputted type: {type(tenant)}")
            # ensure tenant is all ascii
            if not tenant.isascii():
                raise ValueError(f"tenants field may only contain ASCII characters. Inputted tenant: {tenant}")
            # ensure no spaces
            if " " in tenant:
                raise ValueError(f"tenants field may not contain spaces. Invalid tenant: '{tenant}'")
            if tenant == "*":
                continue
            if tenant == "**":
                continue
            if tenant.startswith("-"):
                if not tenant[1:].isalnum():
                    raise ValueError(f"tenants field must be alphanumeric after '-'. Invalid tenant: '{tenant}'")
            elif not tenant.isalnum():
                raise ValueError(f"tenants field must be alphanumeric or '*'. Invalid tenant: '{tenant}'")
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
        return display

class NewImage(ImageBase):
    """
    Object with fields that users are allowed to specify for the Image class.
    """
    pass

class ImageResponseModel(ImageBaseRead):
    """
    Response object for Image class.
    """
    pass

class ImageResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: ImageResponseModel
    status: str
    version: str

class ImagesResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[ImageResponseModel]
    status: str
    version: str

class ImageDeleteResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str

class UpdateImage(TapisApiModel):
    tenants: List[str] = Field([], description = "Tenants that can use this image.", sa_column=Column(ARRAY(String)))
    description: str = Field("", description = "Description of image.")
