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
from sqlalchemy import JSON, CheckConstraint, Column, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.credentials import decrypt_value, encrypt_value

_DEFAULT_DB_PATH = "/data/database.db"
sqlite_file_name = Path(os.getenv("PROXBOX_DATABASE_PATH", _DEFAULT_DB_PATH)).expanduser()

# Attempt to create the parent directory. If the default `/data` path is not
# writable (e.g., in CI without Docker), fall back to a temporary location.
try:
    sqlite_file_name.parent.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    # Fall back to current working directory if /data is not writable.
    sqlite_file_name = Path.cwd() / "database.db"
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
    # WHY: deliberate safety invariant; agents must not flip this True autonomously. See AGENTS.md section "LLM Agent Safety Guardrails".
    allow_writes: bool = Field(default=False)
    # Transport access method (orthogonal to allow_writes). Values: "api"
    # (Read+Write over API only) or "api_ssh" (Read+Write over API + SSH). SSH is
    # an optional complement to API; "ssh only" is unrepresentable. NEW rows
    # default to "api" (this ORM default); PRE-EXISTING rows are backfilled to
    # "api_ssh" by _migrate_proxmox_endpoint_columns() so currently-ungated SSH
    # usage is not silently broken on upgrade. See proxbox_api/enum/proxmox.py
    # ProxmoxAccessMethod and the SSH-access gate in routes/ssh_terminal.py.
    access_methods: str = Field(default="api")
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
    def ssh_enabled(self) -> bool:
        """True when this endpoint permits the SSH transport (``api_ssh``)."""
        return self.access_methods == "api_ssh"

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


class CephOperationRunRecord(SQLModel, table=True):
    """Persisted Ceph v2 plan/apply/reconcile run state."""

    __tablename__: ClassVar[str] = "ceph_operation_run"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True)
    plan_id: str | None = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)
    actor: str | None = Field(default=None, index=True)
    source_branch_schema_id: str | None = Field(default=None, index=True)
    provider: str = Field(index=True)
    request_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    provider_task_refs: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time, index=True)
    updated_at: float = Field(default_factory=time.time, index=True)
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    errors: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    result_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )


class PrometheusSource(SQLModel, table=True):
    """Prometheus metric source for a Ceph cluster (Ceph v2 #94).

    Stores only connection metadata and an optional encrypted bearer token; no
    time-series data is persisted here. ``cluster_ref`` binds the source to a
    Ceph cluster / object reference so the metrics route can resolve the right
    source for a scope.
    """

    __tablename__: ClassVar[str] = "prometheus_source"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    url: str = Field()
    bearer_token: str | None = Field(default=None)
    credential_ref: str | None = Field(default=None)
    cluster_ref: str | None = Field(default=None, index=True)
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=15)
    scrape_interval_seconds: int = Field(default=60)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_bearer_token(self) -> str | None:
        return decrypt_value(self.bearer_token) if self.bearer_token else None

    def set_encrypted_bearer_token(self, value: str | None) -> None:
        self.bearer_token = encrypt_value(value) if value else None


class CephDashboardEndpoint(SQLModel, table=True):
    """Direct Ceph Dashboard API endpoint for Ceph v2 (#98).

    Stores connection metadata plus an encrypted password (or token) used to
    authenticate to the Ceph Manager Dashboard REST API. No Proxmox endpoint is
    required, so this works for external/standalone clusters. ``cluster_ref``
    binds the endpoint to a Ceph cluster / object reference.
    """

    __tablename__: ClassVar[str] = "ceph_dashboard_endpoint"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str = Field()
    username: str | None = Field(default=None)
    password: str | None = Field(default=None)
    token: str | None = Field(default=None)
    credential_ref: str | None = Field(default=None)
    cluster_ref: str | None = Field(default=None, index=True)
    api_version: str = Field(default="1.0")
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    timeout_seconds: int = Field(default=30)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_password(self) -> str | None:
        return decrypt_value(self.password) if self.password else None

    def set_encrypted_password(self, value: str | None) -> None:
        self.password = encrypt_value(value) if value else None

    def get_decrypted_token(self) -> str | None:
        return decrypt_value(self.token) if self.token else None

    def set_encrypted_token(self, value: str | None) -> None:
        self.token = encrypt_value(value) if value else None


class CephExternalCluster(SQLModel, table=True):
    """External (non-Proxmox) Ceph cluster binding for Ceph v2 (#97).

    Provider-neutral: references an optional Ceph Dashboard endpoint and
    Prometheus source (by id) and carries inline RGW Admin Ops credentials.
    No Proxmox endpoint / node / storage / task fields. ``ceph_version_hint``
    feeds capability detection.
    """

    __tablename__: ClassVar[str] = "ceph_external_cluster"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    cluster_ref: str | None = Field(default=None, index=True)
    ceph_version_hint: str | None = Field(default=None)
    dashboard_endpoint_id: int | None = Field(default=None)
    prometheus_source_id: int | None = Field(default=None)
    rgw_admin_url: str | None = Field(default=None)
    rgw_access_key: str | None = Field(default=None)
    rgw_secret_key: str | None = Field(default=None)
    ssh_host: str | None = Field(default=None)
    ssh_user: str | None = Field(default=None)
    ssh_credential_ref: str | None = Field(default=None)
    verify_ssl: bool = Field(default=True)
    enabled: bool = Field(default=True)
    last_seen_at: float | None = Field(default=None)

    def get_decrypted_rgw_access_key(self) -> str | None:
        return decrypt_value(self.rgw_access_key) if self.rgw_access_key else None

    def set_encrypted_rgw_access_key(self, value: str | None) -> None:
        self.rgw_access_key = encrypt_value(value) if value else None

    def get_decrypted_rgw_secret_key(self) -> str | None:
        return decrypt_value(self.rgw_secret_key) if self.rgw_secret_key else None

    def set_encrypted_rgw_secret_key(self, value: str | None) -> None:
        self.rgw_secret_key = encrypt_value(value) if value else None


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


class ApiKeyBootstrapConflict(RuntimeError):
    """A first-key bootstrap lost the durable database claim."""


class ApiKeyBootstrapClaim(SQLModel, table=True):
    """Permanent singleton proving that public key bootstrap was consumed."""

    __tablename__: ClassVar[str] = "api_key_bootstrap_claim"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_api_key_bootstrap_claim_singleton"),
        {"extend_existing": True},
    )

    id: int = Field(default=1, primary_key=True)
    initialized_at: float = Field(default_factory=time.time)


class ApiKey(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    label: str = Field(default="")
    key_hash: str = Field()
    is_active: bool = Field(default=True)
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    async def has_any_key_async(session: AsyncSession) -> bool:
        """Return whether at least one active key can authenticate."""

        result = await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
        return result.first() is not None

    @staticmethod
    def has_any_key(session: Session) -> bool:
        """Return whether at least one active key can authenticate."""

        return session.exec(select(ApiKey).where(ApiKey.is_active == True)).first() is not None  # noqa: E712

    @staticmethod
    async def has_any_record_async(session: AsyncSession) -> bool:
        """Return whether any key row exists, including inactive history."""

        result = await session.exec(select(ApiKey.id).limit(1))
        return result.first() is not None

    @staticmethod
    async def bootstrap_is_claimed_async(session: AsyncSession) -> bool:
        """Return whether this database was ever initialized with an API key."""

        if await session.get(ApiKeyBootstrapClaim, 1) is not None:
            return True
        return await ApiKey.has_any_record_async(session)

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
    async def bootstrap_first_key_async(
        session: AsyncSession,
        raw_key: str,
        label: str = "",
    ) -> "ApiKey":
        """Atomically consume the permanent claim and store the first key.

        The singleton primary key is the cross-process correctness boundary.
        The claim and bcrypt hash share one transaction, so a failed insert
        cannot permanently consume bootstrap without also storing the key.
        """

        key_hash = (
            await asyncio.to_thread(
                bcrypt.hashpw,
                raw_key.encode(),
                bcrypt.gensalt(rounds=12),
            )
        ).decode()
        claim = ApiKeyBootstrapClaim(id=1)
        obj = ApiKey(label=label, key_hash=key_hash)
        session.add(claim)
        session.add(obj)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ApiKeyBootstrapConflict from None
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


def _migrate_api_key_bootstrap_claim(target_engine: Engine = engine) -> None:
    """Permanently close bootstrap for every legacy database with key history."""

    claim_table = ApiKeyBootstrapClaim.__tablename__
    key_table = ApiKey.__tablename__
    try:
        inspector = inspect(target_engine)
        if not inspector.has_table(claim_table) or not inspector.has_table(key_table):
            return
    except Exception:
        return
    with target_engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT OR IGNORE INTO {claim_table} (id, initialized_at) "
                f"SELECT 1, :initialized_at WHERE EXISTS (SELECT 1 FROM {key_table} LIMIT 1)"
            ),
            {"initialized_at": time.time()},
        )


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
    if "access_methods" not in existing:
        # Backfill pre-existing rows to "api_ssh" (NON-BREAKING): SSH paths were
        # previously ungated, so defaulting legacy endpoints to "api" would 403
        # any in-use SSH terminal / cloud-image SSH on upgrade. New rows created
        # through the ORM use the model default "api" instead (see
        # ProxmoxEndpoint.access_methods).
        stmts.append(
            f"ALTER TABLE {table} ADD COLUMN access_methods VARCHAR NOT NULL DEFAULT 'api_ssh'"
        )
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


def _migrate_ceph_operation_run_columns() -> None:
    table = CephOperationRunRecord.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    column_specs: dict[str, str] = {
        "plan_id": "VARCHAR",
        "status": "VARCHAR NOT NULL DEFAULT 'pending'",
        "actor": "VARCHAR",
        "source_branch_schema_id": "VARCHAR",
        "provider": "VARCHAR NOT NULL DEFAULT 'proxmox'",
        "request_summary": "JSON NOT NULL DEFAULT '{}'",
        "provider_task_refs": "JSON NOT NULL DEFAULT '[]'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
        "warnings": "JSON NOT NULL DEFAULT '[]'",
        "errors": "JSON NOT NULL DEFAULT '[]'",
        "result_summary": "JSON NOT NULL DEFAULT '{}'",
    }
    stmts = [
        f"ALTER TABLE {table} ADD COLUMN {col} {spec}"
        for col, spec in column_specs.items()
        if col not in existing
    ]
    if not stmts:
        return
    now = time.time()
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
        conn.execute(
            text(f"UPDATE {table} SET created_at = :now WHERE created_at = 0"), {"now": now}
        )
        conn.execute(
            text(f"UPDATE {table} SET updated_at = :now WHERE updated_at = 0"), {"now": now}
        )


def _migrate_prometheus_source_columns() -> None:
    table = PrometheusSource.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    column_specs: dict[str, str] = {
        "bearer_token": "VARCHAR",
        "credential_ref": "VARCHAR",
        "cluster_ref": "VARCHAR",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 15",
        "scrape_interval_seconds": "INTEGER NOT NULL DEFAULT 60",
        "last_seen_at": "REAL",
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


def _migrate_ceph_dashboard_endpoint_columns() -> None:
    table = CephDashboardEndpoint.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    column_specs: dict[str, str] = {
        "username": "VARCHAR",
        "password": "VARCHAR",
        "token": "VARCHAR",
        "credential_ref": "VARCHAR",
        "cluster_ref": "VARCHAR",
        "api_version": "VARCHAR NOT NULL DEFAULT '1.0'",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "last_seen_at": "REAL",
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


def _migrate_ceph_external_cluster_columns() -> None:
    table = CephExternalCluster.__tablename__
    try:
        insp = inspect(engine)
        if not insp.has_table(table):
            return
        existing = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return
    column_specs: dict[str, str] = {
        "cluster_ref": "VARCHAR",
        "ceph_version_hint": "VARCHAR",
        "dashboard_endpoint_id": "INTEGER",
        "prometheus_source_id": "INTEGER",
        "rgw_admin_url": "VARCHAR",
        "rgw_access_key": "VARCHAR",
        "rgw_secret_key": "VARCHAR",
        "ssh_host": "VARCHAR",
        "ssh_user": "VARCHAR",
        "ssh_credential_ref": "VARCHAR",
        "verify_ssl": "BOOLEAN NOT NULL DEFAULT 1",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "last_seen_at": "REAL",
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
    _migrate_api_key_bootstrap_claim()
    _migrate_proxmox_endpoint_columns()
    _migrate_netbox_endpoint_columns()
    _migrate_deletion_request_columns()
    _migrate_pbs_endpoint_columns()
    _migrate_pdm_endpoint_columns()
    _migrate_ceph_operation_run_columns()
    _migrate_prometheus_source_columns()
    _migrate_ceph_dashboard_endpoint_columns()
    _migrate_ceph_external_cluster_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


DatabaseSessionDep = Annotated[Session, Depends(get_session)]
AsyncDatabaseSessionDep = Annotated[AsyncSession, Depends(get_async_session)]
