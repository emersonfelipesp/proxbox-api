from typing import Annotated
from pathlib import Path

from fastapi import Depends
from sqlmodel import Field, Session, SQLModel, create_engine

# Get the path to the proxbox-api root directory (2 levels up from this file)
# Current file: proxbox-api/proxbox_api/database.py
# Root directory: proxbox-api/
# The database is being created in the root directory of the proxbox-api project.
root_dir = Path(__file__).parent.parent
sqlite_file_name = root_dir / 'database.db'
sqlite_url = f'sqlite:///{sqlite_file_name}'

connect_args = {'check_same_thread': False}
engine = create_engine(sqlite_url, connect_args=connect_args)

class NetBoxEndpoint(SQLModel, table=True):
    __table_args__ = {'extend_existing': True}  # Add this line to prevent redefinition errors
    
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ip_address: str = Field(index=True)
    domain: str = Field(index=True)
    port: int = Field(default=443)  # Default to HTTPS port
    token: str = Field()
    verify_ssl: bool = Field(default=True)
    
    @property
    def url(self) -> str:
        """Construct the full URL for the NetBox endpoint."""
        # Use HTTPS if port is 443 or verify_ssl is True
        protocol = 'https' if self.port == 443 or self.verify_ssl else 'http'
        host = self.domain if self.domain else self.ip_address.split('/')[0]
        return f"{protocol}://{host}:{self.port}"

def create_db_and_tables():
    # Drop existing tables and recreate them to ensure schema changes are applied
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

DatabaseSessionDep = Annotated[Session, Depends(get_session)]