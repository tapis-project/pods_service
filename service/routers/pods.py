from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import List, Dict
from req_utils import g

from datetime import datetime

router = APIRouter()

class ExportedData(BaseModel):
    name: str | None = Field(None, description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    description: str | None = Field(None, description = "Time (UTC) that this node was created.")
    export_path: str | None = Field(None, description = "Time (UTC) that this node was created.")
    author: str | None = Field(None, description = "Time (UTC) that this node was created.")
    format_description: str | None = Field(None, description = "Time (UTC) that this node was created.")


#schema https://pydantic-docs.helpmanual.io/usage/schema/
class Pod(BaseModel):
    name: str = Field(..., description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    requested_data: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    attached_data: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    exported_data: List[ExportedData] 

    # roles_required:
    # attempt_naive_import:
    # naive_import_command: str | None = None
    # custom_import_command:
    # custom_refresh_command:
    # auto_refresh: bool
    # status: str
    # update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was last updated.")


### /pods
@router.get("/pods", tags=["pods"])
async def get_pods():
    return [{"pod1": "stats"}]

@router.post("/pods", tags=["pods"])
async def create_pod():
    return [{"pod1": "stats"}]

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
    print(f"HGELLLO! {g.crazy}")
    g.crazy = 555

