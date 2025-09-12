from proxbox_api.database import DatabaseSessionDep as SessionDep, NetBoxEndpoint
from proxbox_api.session.netbox import get_netbox_session
from fastapi.templating import Jinja2Templates
import os

# Initialize templates
base_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(base_dir, 'templates')
templates = Jinja2Templates(directory=templates_dir)

__all__ = ['SessionDep', 'NetBoxEndpoint', 'get_netbox_session', 'templates']