from fastapi import APIRouter
from pydantic import BaseModel, Field, validator
from typing import List, Dict, Literal
#from stores import neo_store
from req_utils import g, ok
from tapisservice.logs import get_logger
from channels import CommandChannel
logger = get_logger(__name__)

import re

from datetime import datetime

router = APIRouter()


class TapisModel(BaseModel):
    class Config:
        validate_assignment = True


#schema https://pydantic-docs.helpmanual.io/usage/schema/
class ExportedData(TapisModel):
    name: str | None = Field(None, description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    description: str | None = Field(None, description = "Time (UTC) that this node was created.")
    export_path: str | None = Field(None, description = "Time (UTC) that this node was created.")
    author: str | None = Field(None, description = "Time (UTC) that this node was created.")
    format_description: str | None = Field(None, description = "Time (UTC) that this node was created.")


class NewPod(TapisModel):
    name: str = Field(..., description = "Name of this pod.")
    database_type: Literal["neo4j", "temp"] = Field("neo4j", description = "Which database this pod should attempt to create.")
    data_requests: List[str] = Field([], description = "Requested pod names.")
    roles_required: List[str] = Field([], description = "Roles required to view this pod")
    status: str = Field("SUBMITTED", description = "Status of pod.")
    # attempt_naive_import:
    # naive_import_command: str | None = None
    # custom_import_command:
    # custom_refresh_command:
    # auto_refresh: bool

    @validator('name')
    def check_name(cls, v):
        # In case we want to add reserved keywords.
        reserved_names = []
        if v in reserved_names:
            raise ValueError(f"name overlaps with reserved names: {reserved_names}")
        # Regex match full name to ensure a-z0-9.
        res = re.fullmatch(r'[a-z][a-z0-9]+', v)
        if not res:
            raise ValueError(f"name must be lowercase alphanumeric. First character must be alpha.")
        return v

    
    ## DB Stuff
    def db_create(cls):
        print('Top of pod.db_create()')
        # res = neo_store['tacc'].run(
        #     "CREATE (p:Pod {name:$name, database_type:$database_type, roles_required:$roles_required, data_requests:$data_requests}) RETURN p",
        #     parameters=cls.dict())
        return res
    
    @classmethod
    def from_db(cls, dict_get: Dict[str, str]):
        print('Top of pod.from_db()')
        query = "MATCH (p:Pod) WHERE"
        for item_name, item_val in dict_get.items():
            #item name should be in object params (search later on)
            if " " in item_name:
                raise ValueError("Dict keys must be single words. Found space.")
            query = f"{query} p.{item_name} = ${item_name}" 
        query = f"{query} RETURN p"
        
        print(query)
        # res = neo_store['tacc'].run(query,
        #                             parameters=dict_get)
        
        db_dict = data_parse(res)[0]
        return cls(**db_dict)
    
    @classmethod
    def db_get(cls, dict_get: Dict[str, str]):
        print('Top of pod.db_get()')
        query = "MATCH (p:Pod) WHERE"
        for item_name, item_val in dict_get.items():
            #item name should be in object params (search later on)
            if " " in item_name:
                raise ValueError("Dict keys must be single words. Found space.")
            query = f"{query} p.{item_name} = ${item_name}" 
        query = f"{query} RETURN p"
        
        print(query)
        # res = neo_store['tacc'].run(query,
        #                             parameters=dict_get)
        return res
    
    @classmethod
    def db_get_all(cls):
        print('Top of pod.db_get_all()')
        # res = neo_store['tacc'].run(
        #     "MATCH (p:Pod) RETURN p",
        #     parameters=None)
        return res


class Pod(TapisModel):
    name: str = Field(..., description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    requested_data: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    data_attached: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    data_exported: List[ExportedData]

    # roles_required:
    # attempt_naive_import:
    # naive_import_command: str | None = None
    # custom_import_command:
    # custom_refresh_command:
    # auto_refresh: bool
    # status: str
    # update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was last updated.")

class TapisReturn(TapisModel):
    message: str = "The request was successful."
    metadata: Dict = {}
    result: str | List | int | Dict | None = None
    status: str = "success"
    version: str = "dev"



### /pods
@router.get("/pods", tags=["pods"])
async def get_pods():
    return [{"pod1": "stats"}]

@router.post("/pods", tags=["pods"])
async def create_pod(pod: NewPod):
    logger.info("POST /pods - Top of create_pod.")


    ch = CommandChannel(name='tacc')

    ch.put_cmd(pod_name=pod.name,
               tenant_id=g.request_tenant_id,
               site_id='tacc')
    ch.close()
    logger.debug("Command Channel - Added msg for new pod.")

    return ok(pod)


@router.put("/pods/", tags=["pods"])
async def update_pod():
    return [{"pod1": "stats"}]

@router.delete("/pods/", tags=["pods"])
async def del_pod():
    return [{"pod1": "stats"}]


### /pods/{pod_name}
@router.get("/pods/{pod_name}", tags=["pods"])
async def get_pod(pod_name):
    return [{pod_name: "specific_stats"}]

def test2():
    print(f"hello! {g.test}")
    g.test = 555

