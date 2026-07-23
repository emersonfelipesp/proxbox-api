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
from sqlalchemy import JSON, Column, UniqueConstraint, event, inspect, text
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
    endpoint_id: int | None = Field(default=None, index=True)
    endpoint_config_revision: str | None = Field(default=None, index=True)
    plan_digest: str | None = Field(default=None, index=True)
    requester: str | None = Field(default=None, index=True)
    approver: str | None = Field(default=None, index=True)
    approval_id: str | None = Field(default=None, index=True)
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
    lease_expires_at: float | None = Field(default=None, index=True)
    lease_owner: str | None = Field(default=None, index=True)
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    errors: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    result_summary: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )


class CephOperationEventRecord(SQLModel, table=True):
    """Append-only dispatch and provider-task evidence for one Ceph run."""

    __tablename__: ClassVar[str] = "ceph_operation_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_ceph_operation_event_run_sequence"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    sequence: int = Field(index=True)
    operation_index: int | None = Field(default=None, index=True)
    operation_id: str | None = Field(default=None, index=True)
    event: str = Field(index=True)
    status: str = Field(index=True)
    code: str
    message: str
    kind: str | None = Field(default=None, index=True)
    action: str | None = Field(default=None, index=True)
    target_ref: str | None = Field(default=None, index=True)
    provider_task_ref: str | None = Field(default=None, index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time, index=True)


class CephProviderTaskClaimRecord(SQLModel, table=True):
    """Globally unique ownership claim for one provider task reference.

    A provider task may produce several audit events, so uniqueness belongs in
    this dedicated table rather than on ``CephOperationEventRecord``.  Claims
    are never recycled: a reference observed in any earlier run can therefore
    never be polled as evidence for a later mutation.
    """

    __tablename__: ClassVar[str] = "ceph_provider_task_claim"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_task_ref",
            name="uq_ceph_provider_task_claim_provider_ref",
        ),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    endpoint_id: int = Field(index=True)
    endpoint_config_revision: str | None = Field(default=None, index=True)
    provider_task_ref: str = Field(index=True)
    run_id: str = Field(index=True)
    operation_index: int = Field(index=True)
    operation_id: str | None = Field(default=None, index=True)
    claimed_at: float = Field(default_factory=time.time, index=True)


class CephPlanRecord(SQLModel, table=True):
    """Canonical persisted Ceph v2 plan used as the only apply authority."""

    __tablename__: ClassVar[str] = "ceph_plan"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True)
    provider: str = Field(index=True)
    endpoint_id: int | None = Field(default=None, index=True)
    endpoint_config_revision: str | None = Field(default=None, index=True)
    requester: str = Field(index=True)
    source_branch_schema_id: str | None = Field(default=None, index=True)
    digest: str = Field(index=True)
    plan_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    created_at: float = Field(default_factory=time.time, index=True)
    expires_at: float = Field(index=True)


class CephApprovalRecord(SQLModel, table=True):
    """Hashed, expiring, single-use two-person approval for one Ceph plan."""

    __tablename__: ClassVar[str] = "ceph_approval"
    __table_args__ = {"extend_existing": True}

    id: str = Field(primary_key=True)
    plan_id: str = Field(index=True, unique=True)
    plan_digest: str = Field(index=True)
    endpoint_id: int | None = Field(default=None, index=True)
    endpoint_config_revision: str | None = Field(default=None, index=True)
    requester: str = Field(index=True)
    approver: str = Field(index=True)
    token_hash: str = Field(index=True, unique=True)
    created_at: float = Field(default_factory=time.time, index=True)
    expires_at: float = Field(index=True)
    consumed_at: float | None = Field(default=None, index=True)
    consumed_by: str | None = Field(default=None, index=True)
    operation_run_id: str | None = Field(default=None, index=True)


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
        "endpoint_id": "INTEGER",
        "endpoint_config_revision": "VARCHAR",
        "plan_digest": "VARCHAR",
        "requester": "VARCHAR",
        "approver": "VARCHAR",
        "approval_id": "VARCHAR",
        "status": "VARCHAR NOT NULL DEFAULT 'pending'",
        "actor": "VARCHAR",
        "source_branch_schema_id": "VARCHAR",
        "provider": "VARCHAR NOT NULL DEFAULT 'proxmox'",
        "request_summary": "JSON NOT NULL DEFAULT '{}'",
        "provider_task_refs": "JSON NOT NULL DEFAULT '[]'",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
        "lease_expires_at": "REAL",
        "lease_owner": "VARCHAR",
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
        conn.execute(
            text(
                f"UPDATE {table} SET lease_expires_at = updated_at "
                "WHERE lease_expires_at IS NULL AND status IN ('running', 'dispatching')"
            )
        )


def _migrate_ceph_authority_columns() -> None:
    """Add durable endpoint-revision bindings to existing plan/approval tables."""

    tables = {
        CephPlanRecord.__tablename__: {"endpoint_config_revision": "VARCHAR"},
        CephApprovalRecord.__tablename__: {"endpoint_config_revision": "VARCHAR"},
    }
    for table, column_specs in tables.items():
        try:
            insp = inspect(engine)
            if not insp.has_table(table):
                continue
            existing = {column["name"] for column in insp.get_columns(table)}
        except Exception:
            continue
        statements = [
            f"ALTER TABLE {table} ADD COLUMN {column} {spec}"
            for column, spec in column_specs.items()
            if column not in existing
        ]
        if not statements:
            continue
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))


class CephProviderTaskClaimMigrationError(RuntimeError):
    """Legacy task evidence cannot be represented by the global claim key."""


def _migrate_ceph_provider_task_claim_table() -> None:
    """Install global task claims and deterministically backfill legacy evidence.

    Provider task identifiers are provider-global, not endpoint-local.  Older
    databases used ``(provider, endpoint_id, provider_task_ref)`` uniqueness.
    A reference already observed on more than one endpoint is ambiguous and is
    therefore never auto-selected or discarded: startup fails with a stable
    migration reason while the original table remains intact for operator
    investigation.  Collision-free legacy tables are rebuilt transactionally
    with the global ``(provider, provider_task_ref)`` key.
    """

    run_table = CephOperationRunRecord.__tablename__
    event_table = CephOperationEventRecord.__tablename__
    claim_table = CephProviderTaskClaimRecord.__tablename__
    inspector = inspect(engine)
    claim_exists = inspector.has_table(claim_table)
    if not claim_exists:
        CephProviderTaskClaimRecord.__table__.create(engine, checkfirst=True)
        inspector = inspect(engine)

    has_legacy_events = inspector.has_table(run_table) and inspector.has_table(event_table)
    unique_column_sets = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(claim_table)
    }
    requires_global_rebuild = ("provider", "provider_task_ref") not in unique_column_sets

    candidate_queries = [
        f"""
        SELECT provider, endpoint_id, provider_task_ref
        FROM {claim_table}
        WHERE provider_task_ref IS NOT NULL
          AND TRIM(provider_task_ref) != ''
        """
    ]
    if has_legacy_events:
        candidate_queries.append(
            f"""
            SELECT
                run.provider AS provider,
                COALESCE(run.endpoint_id, 0) AS endpoint_id,
                event.provider_task_ref AS provider_task_ref
            FROM {event_table} AS event
            JOIN {run_table} AS run ON run.id = event.run_id
            WHERE event.provider_task_ref IS NOT NULL
              AND TRIM(event.provider_task_ref) != ''
            """
        )

    with engine.begin() as connection:
        collision = connection.execute(
            text(
                """
                WITH candidates AS (
                """
                + " UNION ALL ".join(candidate_queries)
                + """
                )
                SELECT provider, provider_task_ref
                FROM candidates
                GROUP BY provider, provider_task_ref
                HAVING COUNT(DISTINCT endpoint_id) > 1
                ORDER BY provider, provider_task_ref
                LIMIT 1
                """
            )
        ).first()
        if collision is not None:
            raise CephProviderTaskClaimMigrationError(
                "ceph_provider_task_claim_cross_endpoint_collision"
            )

        if requires_global_rebuild:
            replacement = f"{claim_table}_global_key"
            connection.execute(text(f"DROP TABLE IF EXISTS {replacement}"))
            connection.execute(
                text(
                    f"""
                    CREATE TABLE {replacement} (
                        id INTEGER NOT NULL PRIMARY KEY,
                        provider VARCHAR NOT NULL,
                        endpoint_id INTEGER NOT NULL,
                        endpoint_config_revision VARCHAR,
                        provider_task_ref VARCHAR NOT NULL,
                        run_id VARCHAR NOT NULL,
                        operation_index INTEGER NOT NULL,
                        operation_id VARCHAR,
                        claimed_at FLOAT NOT NULL,
                        CONSTRAINT uq_ceph_provider_task_claim_provider_ref
                            UNIQUE (provider, provider_task_ref)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    f"""
                    INSERT INTO {replacement} (
                        id,
                        provider,
                        endpoint_id,
                        endpoint_config_revision,
                        provider_task_ref,
                        run_id,
                        operation_index,
                        operation_id,
                        claimed_at
                    )
                    SELECT
                        id,
                        provider,
                        endpoint_id,
                        endpoint_config_revision,
                        provider_task_ref,
                        run_id,
                        operation_index,
                        operation_id,
                        claimed_at
                    FROM {claim_table}
                    ORDER BY claimed_at, id
                    """
                )
            )
            connection.execute(text(f"DROP TABLE {claim_table}"))
            connection.execute(text(f"ALTER TABLE {replacement} RENAME TO {claim_table}"))
            for column in (
                "provider",
                "endpoint_id",
                "endpoint_config_revision",
                "provider_task_ref",
                "run_id",
                "operation_index",
                "operation_id",
                "claimed_at",
            ):
                connection.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{claim_table}_{column} "
                        f"ON {claim_table} ({column})"
                    )
                )

        if not has_legacy_events:
            return
        connection.execute(
            text(
                f"""
                INSERT OR IGNORE INTO {claim_table} (
                    provider,
                    endpoint_id,
                    endpoint_config_revision,
                    provider_task_ref,
                    run_id,
                    operation_index,
                    operation_id,
                    claimed_at
                )
                SELECT
                    run.provider,
                    COALESCE(run.endpoint_id, 0),
                    run.endpoint_config_revision,
                    event.provider_task_ref,
                    event.run_id,
                    COALESCE(event.operation_index, -1),
                    event.operation_id,
                    event.created_at
                FROM {event_table} AS event
                JOIN {run_table} AS run ON run.id = event.run_id
                WHERE event.provider_task_ref IS NOT NULL
                  AND TRIM(event.provider_task_ref) != ''
                ORDER BY event.created_at, event.id
                """
            )
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
    _migrate_proxmox_endpoint_columns()
    _migrate_netbox_endpoint_columns()
    _migrate_deletion_request_columns()
    _migrate_pbs_endpoint_columns()
    _migrate_pdm_endpoint_columns()
    _migrate_ceph_operation_run_columns()
    _migrate_ceph_authority_columns()
    _migrate_ceph_provider_task_claim_table()
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
