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


class Template(TapisModel, table=True, validate=True):
    # Required
    object_name: str = Field(..., description = "Name of image to allow.", primary_key=True)

    # Optional
    tenants: List[str] = Field([], description = "Tenants that can use this image.")

    # Provided
    creation_time: datetime = Field(..., description = "Time image was added to allow list.")
    added_by: str = Field(..., description = "User who added image to allow list.")
    #__table_args__ = ({"schema": "siteadmintables"},)