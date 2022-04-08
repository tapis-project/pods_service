from asyncio import create_task
from unittest import result
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime

from tomlkit import date

app = FastAPI()




class ExportedData(BaseModel):
    name: str | None = Field(None, description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    description: str | None = Field(None, description = "Time (UTC) that this node was created.")
    export_path: str | None = Field(None, description = "Time (UTC) that this node was created.")
    author: str | None = Field(None, description = "Time (UTC) that this node was created.")
    format_description: str | None = Field(None, description = "Time (UTC) that this node was created.")


#schema https://pydantic-docs.helpmanual.io/usage/schema/
class FillerNode(BaseModel):
    name: str = Field(..., description = "Time (UTC) that this node was created.")
    create_ts: datetime | None = Field(None, description = "Time (UTC) that this node was created.")
    requested_data: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    attached_data: List[str] = Field(None, description = "Time (UTC) that this node was created.")
    exported_data: List[ExportedData] 
        
        
    roles_required:
    attempt_naive_import:
    naive_import_command: str | None = None
    custom_import_command:
    custom_refresh_command:
    auto_refresh: bool
    status: str
    update_ts: datetime | None = Field(None, description = "Time (UTC) that this node was last updated.")
    



@app.get("/fillerapi/healthcheck")
async def api_healthcheck():
    """
    Health check for service. Returns healthy when api is running.
    TODO: Should add database health check, should add kubernetes health check
    """
    return {"message": "I promise I'm healthy."}

@app.get("/fillerapi/")
async def api_list_fillernode() -> result:
    fillernodes = []
    return fillernodes

async def api_create_fillernode():
    

