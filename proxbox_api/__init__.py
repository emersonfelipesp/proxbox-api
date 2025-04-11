from pynetbox_api.database import SessionDep, NetBoxEndpoint
from pynetbox_api.session import RawNetBoxSession
 
import asyncio
from fastapi import Depends
from typing import Annotated

from pynetbox_api.extras.tag import Tags

async def proxbox_tag():
    return await asyncio.to_thread(
        lambda: Tags(
            name='Proxbox',
            slug='proxbox',
            color='ff5722',
            description='Proxbox Identifier (used to identify the items the plugin created)'
        )
    )

ProxboxTagDep = Annotated[Tags.Schema, Depends(proxbox_tag)]

__all__ = ['SessionDep', 'NetBoxEndpoint', 'RawNetBoxSession']