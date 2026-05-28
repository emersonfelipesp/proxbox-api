"""SQLModel database configuration and endpoint models."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Annotated, Any, ClassVar

import bcrypt
from fastapi import Depends
from sqlalchemy import JSON, Column, event, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.credentials import decrypt_value, encrypt_value

root_dir = Path(__file__).parent.parent
sqlite_file_name = Path(os.getenv("PROXBOX_DATABASE_PATH", root_dir / "database.db")).expanduser()
sqlite_file_name.parent.mkdir(parents=True, exist_ok=True)
sqlite_url = f"sqlite:///{sqlite_file_name}"
async_sqlite_url = f"sqlite+aiosqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args, poolclass=NullPool)

async_engine = create_async_engine(async_sqlite_url, connect_args=connect_args)
async_session_factory = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False
)


def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # noqa: ARG001
    """Enable WAL journal mode and a 5-second busy timeout on every new connection.

    WAL mode allows concurrent readers alongside a single writer, which prevents
    'database is locked' errors when multiple requests hit the auth-lockout check
    simultaneously.  The busy timeout makes writers wait up to 5 s before raising
    instead of failing immediately.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


event.listen(engine, "connect", _apply_sqlite_pragmas)
event.listen(async_engine.sync_engine, "connect", _apply_sqlite_pragmas)


class NetBoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    ip_address: str = Field(index=True)
    domain: str = Field(index=True)
    port: int = Field(default=443)
    token_version: str = Field(default="v1")
    token_key: str | None = Field(default=None)
    token: str = Field()
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)

    @property
    def url(self) -> str:
        protocol = "https" if self.port == 443 or self.verify_ssl else "http"
        host = self.domain if self.domain else self.ip_address.split("/")[0]
        return f"{protocol}://{host}:{self.port}"

    def get_decrypted_token(self) -> str:
        return decrypt_value(self.token) or ""

    def get_decrypted_token_key(self) -> str | None:
        return decrypt_value(self.token_key)

    def set_encrypted_token(self, value: str) -> None:
        self.token = encrypt_value(value) or value

    def set_encrypted_token_key(self, value: str | None) -> None:
        self.token_key = encrypt_value(value)


class ProxmoxEndpoint(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    ip_address: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    port: int = Field(default=8006)
    username: str = Field(index=True)
    password: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    allow_writes: bool = Field(default=False)
    enabled: bool = Field(default=True)
    token_name: str | None = Field(default=None)
    token_value: str | None = Field(default=None)
    timeout: int | None = Field(default=None)
    max_retries: int | None = Field(default=None)
    retry_backoff: float | None = Field(default=None)
    site_id: int | None = Field(default=None)
    site_slug: str | None = Field(default=None)
    site_name: str | None = Field(default=None)
    tenant_id: int | None = Field(default=None)
    tenant_slug: str | None = Field(default=None)
    tenant_name: str | None = Field(default=None)

    @property
    def has_token(self) -> bool:
        return bool(self.token_name and self.token_value)

    @property
    def host(self) -> str:
        return self.domain or self.ip_address

    def get_decrypted_password(self) -> str | None:
        return decrypt_value(self.password)

    def get_decrypted_token_value(self) -> str | None:
        return decrypt_value(self.token_value)

    def set_encrypted_password(self, value: str | None) -> None:
        self.password = encrypt_value(value)

    def set_encrypted_token_value(self, value: str | None) -> None:
        self.token_value = encrypt_value(value)


class DeletionRequestRecord(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    endpoint_id: int = Field(index=True)
    vmid: int = Field(index=True)
    node: str = Field(index=True)
    kind: str = Field(index=True)
    state: str = Field(default="pending", index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class FirewallIntentRequestRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "firewall_intent_request"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    endpoint_id: int = Field(index=True)
    actor: str | None = Field(default=None, index=True)
    action: str = Field(index=True)
    state: str = Field(default="planned", index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    plan_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class PBSEndpoint(SQLModel, table=True):
    """Proxmox Backup Server (PBS) endpoint record.

    Read-only integration in v1: credentials authorize PBS GET calls only.
    ``allow_writes`` is reserved for a future write surface and stays False.
    """

    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str = Field(index=True)
    port: int = Field(default=8007)
    token_id: str = Field()
    token_secret: str = Field()
    fingerprint: str | None = Field(default=None)
    verify_ssl: bool = Field(default=False)
    allow_writes: bool = Field(default=False)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def get_decrypted_token_secret(self) -> str | None:
        return decrypt_value(self.token_secret)

    def set_encrypted_token_secret(self, value: str) -> None:
        self.token_secret = encrypt_value(value) or value


class PDMEndpoint(SQLModel, table=True):
    """Proxmox Datacenter Manager (PDM) endpoint record.

    Read-only integration in v1: credentials authorize PDM GET calls only.
    ``allow_writes`` is reserved and stays False.
    """

    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str = Field(index=True)
    port: int = Field(default=8443)
    token_id: str = Field()
    token_secret: str = Field()
    fingerprint: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    allow_writes: bool = Field(default=False)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    @property
    def url(self) -> str:
        return f"https://{self.host}:{self.port}"

    def get_decrypted_token_secret(self) -> str | None:
        return decrypt_value(self.token_secret)

    def set_encrypted_token_secret(self, value: str) -> None:
        self.token_secret = encrypt_value(value) or value


class AuthLockout(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    ip_address: str = Field(primary_key=True)
    attempts: int = Field(default=0)
    first_attempt_time: float = Field(default=0)

    @staticmethod
    def is_locked_out(
        session: Session, ip: str, max_attempts: int = 5, lockout_duration: float = 300
    ) -> bool:
        lockout = session.get(AuthLockout, ip)
        if not lockout:
            return False
        if lockout.attempts >= max_attempts:
            if time.time() - lockout.first_attempt_time < lockout_duration:
                return True
            session.delete(lockout)
            session.commit()
        return False

    @staticmethod
    def record_failed_attempt(session: Session, ip: str) -> None:
        lockout = session.get(AuthLockout, ip)
        now = time.time()
        if not lockout:
            lockout = AuthLockout(ip_address=ip, attempts=1, first_attempt_time=now)
            session.add(lockout)
        else:
            if now - lockout.first_attempt_time > 300:
                lockout.attempts = 1
                lockout.first_attempt_time = now
            else:
                lockout.attempts += 1
        session.commit()

    @staticmethod
    def clear_failed_attempts(session: Session, ip: str) -> None:
        lockout = session.get(AuthLockout, ip)
        if lockout:
            session.delete(lockout)
            session.commit()

    @staticmethod
    async def is_locked_out_async(
        session: AsyncSession, ip: str, max_attempts: int = 5, lockout_duration: float = 300
    ) -> bool:
        lockout = await session.get(AuthLockout, ip)
        if not lockout:
            return False
        if lockout.attempts >= max_attempts:
            if time.time() - lockout.first_attempt_time < lockout_duration:
                return True
            await session.delete(lockout)
            await session.commit()
        return False

    @staticmethod
    async def record_failed_attempt_async(session: AsyncSession, ip: str) -> None:
        lockout = await session.get(AuthLockout, ip)
        now = time.time()
        if not lockout:
            lockout = AuthLockout(ip_address=ip, attempts=1, first_attempt_time=now)
            session.add(lockout)
        else:
            if now - lockout.first_attempt_time > 300:
                lockout.attempts = 1
                lockout.first_attempt_time = now
            else:
                lockout.attempts += 1
        await session.commit()

    @staticmethod
    async def clear_failed_attempts_async(session: AsyncSession, ip: str) -> None:
        lockout = await session.get(AuthLockout, ip)
        if lockout:
            await session.delete(lockout)
            await session.commit()


class ApiKey(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(default="")
    key_hash: str = Field()
    is_active: bool = Field(default=True)
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    async def has_any_key_async(session: AsyncSession) -> bool:
        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        return result.first() is not None

    @staticmethod
    def has_any_key(session: Session) -> bool:
        return session.exec(select(ApiKey).where(ApiKey.is_active == True)).first() is not None  # noqa: E712

    @staticmethod
    def store_key(session: Session, raw_key: str, label: str = "") -> "ApiKey":
        key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12)).decode()
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        session.commit()
        session.refresh(obj)
        return obj

    @staticmethod
    async def store_key_async(session: AsyncSession, raw_key: str, label: str = "") -> "ApiKey":
        key_hash = (
            await asyncio.to_thread(bcrypt.hashpw, raw_key.encode(), bcrypt.gensalt(rounds=12))
        ).decode()
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj

    @staticmethod
    def verify_any(session: Session, provided_key: str) -> bool:
        for row in session.exec(select(ApiKey).where(ApiKey.is_active == True)):  # noqa: E712
            try:
                if bcrypt.checkpw(provided_key.encode(), row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def verify_any_async(session: AsyncSession, provided_key: str) -> bool:
        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        provided = provided_key.encode()
        for row in result:
            try:
                if await asyncio.to_thread(bcrypt.checkpw, provided, row.key_hash.encode()):
                    return True
            except Exception:
                continue
        return False


def _migrate_proxmox_endpoint_columns() -> None:  # noqa: C901
    table = ProxmoxEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    stmts: list[str] = []
    if "timeout" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN timeout INTEGER")
    if "max_retries" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN max_retries INTEGER")
    if "retry_backoff" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN retry_backoff REAL")
    if "site_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_id INTEGER")
    if "site_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_slug VARCHAR")
    if "site_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN site_name VARCHAR")
    if "tenant_id" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER")
    if "tenant_slug" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_slug VARCHAR")
    if "tenant_name" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN tenant_name VARCHAR")
    if "allow_writes" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN allow_writes BOOLEAN NOT NULL DEFAULT 0")
    if "enabled" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
    if "verify_ssl" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN verify_ssl BOOLEAN NOT NULL DEFAULT 1")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_netbox_endpoint_columns() -> None:
    table = NetBoxEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    stmts: list[str] = []
    if "token_version" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_version VARCHAR DEFAULT 'v1'")
    if "token_key" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN token_key VARCHAR")
    if "enabled" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")
    if "verify_ssl" not in existing:
        stmts.append(f"ALTER TABLE {table} ADD COLUMN verify_ssl BOOLEAN NOT NULL DEFAULT 1")
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f"UPDATE {table} SET token_version = 'v1' "
                "WHERE token_version IS NULL OR TRIM(COALESCE(token_version, '')) = ''"
            )
        )
        # Ensure verify_ssl is never NULL (NULL → True means "verify by default").
        conn.execute(text(f"UPDATE {table} SET verify_ssl = 1 WHERE verify_ssl IS NULL"))


def _migrate_deletion_request_columns() -> None:
    table = DeletionRequestRecord.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    stmts: list[str] = []
    column_specs = {
        "endpoint_id": "INTEGER NOT NULL DEFAULT 0",
        "vmid": "INTEGER NOT NULL DEFAULT 0",
        "node": "VARCHAR NOT NULL DEFAULT ''",
        "kind": "VARCHAR NOT NULL DEFAULT 'qemu'",
        "state": "VARCHAR NOT NULL DEFAULT 'pending'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    }
    for column, spec in column_specs.items():
        if column not in existing:
            stmts.append(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
    if not stmts:
        return
    now = time.time()
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(
                f"UPDATE {table} SET created_at = :now WHERE created_at IS NULL OR created_at = 0"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                f"UPDATE {table} SET updated_at = :now WHERE updated_at IS NULL OR updated_at = 0"
            ),
            {"now": now},
        )


def _migrate_pbs_endpoint_columns() -> None:
    table = PBSEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    # PBS commonly uses self-signed certs, so verify_ssl defaults to 0 (False).
    column_specs: dict[str, str] = {
        "fingerprint": "VARCHAR",
        "allow_writes": "BOOLEAN NOT NULL DEFAULT 0",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 0",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _migrate_pdm_endpoint_columns() -> None:
    table = PDMEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    column_specs: dict[str, str] = {
        "fingerprint": "VARCHAR",
        "allow_writes": "BOOLEAN NOT NULL DEFAULT 0",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_proxmox_endpoint_columns()
    _migrate_netbox_endpoint_columns()
    _migrate_deletion_request_columns()
    _migrate_pbs_endpoint_columns()
    _migrate_pdm_endpoint_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


DatabaseSessionDep = Annotated[Session, Depends(get_session)]
AsyncDatabaseSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
