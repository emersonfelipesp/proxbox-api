"""Top-level package exports for proxbox_api."""

import os

from fastapi.templating import Jinja2Templates

from proxbox_api.database import DatabaseSessionDep as SessionDep
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.session.netbox import get_netbox_session

# Initialize templates
base_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(base_dir, "templates")
templates = Jinja2Templates(directory=templates_dir)

__all__ = ["SessionDep", "NetBoxEndpoint", "get_netbox_session", "templates"]
