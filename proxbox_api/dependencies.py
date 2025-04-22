# Insert dependencies here

from pynetbox_api.extras.tag import Tags

import asyncio
from fastapi import Depends
from typing import Annotated

async def proxbox_tag():
    return await asyncio.to_thread(
        lambda: Tags(
            name='Proxbox',
            slug='proxbox',
            color='ff5722',
            description='Proxbox Identifier (used to identify the items the plugin created)'
        )
    )

# Proxbox Tag Dependency (used to identify the items the plugin created)
# It's used to tag the items created by the plugin
# NetBox Tag Object.
ProxboxTagDep = Annotated[Tags.Schema, Depends(proxbox_tag)]