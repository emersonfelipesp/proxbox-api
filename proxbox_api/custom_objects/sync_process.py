"""
Custom object to manage sync processes.
This is not implemented yet!
"""
from fastapi import APIRouter
from datetime import datetime

from pydantic import BaseModel, RootModel
from typing import List

from pynetbox_api.utils import GenericSchema
from pynetbox_api.session import NetBoxBase
from pynetbox_api.extras.tag import Tags

__all__ = [
    "SyncProcessSchema",
    "SyncProcessSchemaList",
    "SyncProcessSchemaIn",
    "SyncProcess"
]

class SyncProcess(NetBoxBase):
    class BasicSchema(BaseModel):
        id: int | None = None
        url: str | None = None
        display: str | None = None
        name: str | None = None
        description: str | None = None
    
    class Schema(GenericSchema, BasicSchema):
        sync_type: str | None = None
        status: str | None = None
        runtime: float | None = None
        started_at: datetime | None = None
        completed_at: datetime | None = None
        

    class SchemaIn(BaseModel):
        name: str = 'SyncProcess Placeholder'
        start_time = datetime.now()
        tags: List[int] | None = None

    SyncProcessSchemaList = RootModel[List[Schema]]
    
    app = 'plugins.proxbox'
    name = 'sync_processes'
    schema = Schema
    schema_in = SchemaIn
    schema_list = SyncProcessSchemaList
    unique_together = ['name', 'slug']
    
    # API
    prefix = '/SyncProcess'
    api_router = APIRouter(tags=['DCIM / SyncProcess'])