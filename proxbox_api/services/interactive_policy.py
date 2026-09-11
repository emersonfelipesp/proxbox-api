"""Process-pinned denial and owned lifetimes for unrestricted capabilities."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from starlette._utils import get_route_path
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DENIAL = "Interactive execution is unavailable."
CAPABILITY = "interactive-rpc-boundary-v1"
_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CURRENT: ContextVar[_Admission | None] = ContextVar("interactive_runtime", default=None)


class InteractiveStatus(BaseModel):
    """Closed, secret-free evidence for this component's scoped boundary only."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    component: Literal["proxbox-api"] = "proxbox-api"
    capability: Literal["interactive-rpc-boundary-v1"] = CAPABILITY
    mode: Literal["rpc_only", "legacy"]
    generation: str | None
    quiescing: bool
    active: int = Field(ge=0)
    cleanup_active: int = Field(ge=0)
    remote_outcome_unknown: bool
    local_ready: bool
    aggregate_ready: Literal[False] = False


class InteractiveDenied(Exception):
    """Static refusal without request, credential, or provider diagnostics."""

    def __init__(self) -> None:
        super().__init__(DENIAL)


@dataclass
class _Admission:
    runtime: InteractiveRuntime
    active: bool = True
    resources: AsyncExitStack = field(default_factory=AsyncExitStack)
    acquisitions: set[asyncio.Task] = field(default_factory=set)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Operator-only configuration captured once by the composition root."""

    mode: Literal["rpc_only", "legacy"] = "rpc_only"
    generation: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ExecutionPolicy:
        """Reject malformed settings without reflecting their values."""
        values = os.environ if environ is None else environ
        mode = values.get("PROXBOX_EXECUTION_MODE", "rpc_only")
        generation = values.get("PROXBOX_EXECUTION_GENERATION")
        if mode not in {"rpc_only", "legacy"}:
            raise ValueError("Invalid PROXBOX_EXECUTION_MODE configuration.")
        if generation is not None and not _GENERATION.fullmatch(generation):
            raise ValueError("Invalid PROXBOX_EXECUTION_GENERATION configuration.")
        return cls(mode=cast(Literal["rpc_only", "legacy"], mode), generation=generation)


class InteractiveRuntime:
    """One worker's admission, cleanup, and irreversible local quiesce state."""

    def __init__(self, policy: ExecutionPolicy, *, cleanup_timeout: float = 10.0) -> None:
        self.policy = policy
        self.cleanup_timeout = cleanup_timeout
        self.quiescing = False
        self.uncertain = False
        self._active: set[asyncio.Task] = set()
        self._cleanup: set[asyncio.Task] = set()
        self._pending: dict[str, Callable[[], Awaitable[None]]] = {}

    def retain_pending(self, identity: str, release: Callable[[], Awaitable[None]]) -> None:
        """Retain only this worker's pending capability cleanup."""
        self.require()
        self._pending[identity] = release

    def forget_pending(self, identity: str) -> None:
        """Drop the cleanup reference once the ticket has been removed."""
        self._pending.pop(identity, None)

    def require(self) -> None:
        """Recheck immediately before each material or transport boundary."""
        if self.policy.mode != "legacy" or self.quiescing:
            raise InteractiveDenied()

    @asynccontextmanager
    async def admission(self) -> AsyncIterator[None]:
        """Register before the first wait and revoke all inherited child work."""
        self.require()
        task = asyncio.current_task()
        if task is None or task in self._active:
            raise InteractiveDenied()
        self._active.add(task)
        admission = _Admission(self)
        token = _CURRENT.set(admission)
        try:
            yield
        finally:
            admission.active = False
            try:
                await self.finish_cleanup(_finish_admission(admission))
            finally:
                _CURRENT.reset(token)
                self._active.discard(task)

    def status(self) -> dict[str, object]:
        """Report local evidence, never assert aggregate fleet cutover."""
        return {
            "component": "proxbox-api",
            "capability": CAPABILITY,
            "mode": self.policy.mode,
            "generation": self.policy.generation,
            "quiescing": self.quiescing,
            "active": len(self._active),
            "cleanup_active": len(self._cleanup),
            "remote_outcome_unknown": self.uncertain,
            "local_ready": bool(self.policy.generation)
            and self.policy.mode == "rpc_only"
            and not self.quiescing
            and not self._active
            and not self._cleanup
            and not self.uncertain,
            "aggregate_ready": False,
        }

    async def quiesce(self) -> None:
        """Stop admissions and await bounded local teardown, not remote rollback."""
        self.quiescing = True
        tasks = self._active - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.cleanup_timeout)
            self.uncertain |= bool(pending)
        releases = tuple(self._pending.values())
        if releases:
            await self.finish_cleanup(self._release_pending(releases))
        if self._cleanup:
            _, pending = await asyncio.wait(self._cleanup, timeout=self.cleanup_timeout)
            self.uncertain |= bool(pending)

    async def _release_pending(self, releases: tuple[Callable[[], Awaitable[None]], ...]) -> None:
        for release in releases:
            await release()

    async def finish_cleanup(self, cleanup: Awaitable[object]) -> None:
        """Keep cleanup owned through repeated cancellation and bounded waits."""
        task = asyncio.create_task(_await_cleanup(cleanup))
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup_finished)
        deadline = asyncio.get_running_loop().time() + self.cleanup_timeout
        interrupted = False
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.uncertain = True
                break
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    def _cleanup_finished(self, task: asyncio.Task) -> None:
        self._cleanup.discard(task)
        if task.cancelled() or task.exception() is not None:
            self.uncertain = True


def current_interactive_runtime() -> InteractiveRuntime | None:
    """Recheck an inherited interactive owner without changing inventory callers."""
    admission = _CURRENT.get()
    if admission is None:
        return None
    if not admission.active:
        raise InteractiveDenied()
    runtime = admission.runtime
    runtime.require()
    return runtime


def require_interactive() -> InteractiveRuntime:
    """Require a live owned admission even when a service is called directly."""
    runtime = current_interactive_runtime()
    if runtime is None:
        raise InteractiveDenied()
    return runtime


async def acquire_interactive_resource[T](
    acquisition: Callable[[], Awaitable[T]], close: Callable[[T], Awaitable[object]]
) -> T:
    """Attach each acquired provider to its complete request, including siblings."""
    require_interactive()
    admission = _CURRENT.get()
    assert admission is not None
    task = asyncio.current_task()
    assert task is not None
    admission.acquisitions.add(task)
    try:
        context: AbstractAsyncContextManager[T] = owned_resource(acquisition, close)
        return await admission.resources.enter_async_context(context)
    finally:
        admission.acquisitions.discard(task)


async def _settle_acquisitions(acquisitions: set[asyncio.Task]) -> None:
    pending = tuple(acquisitions)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _finish_admission(admission: _Admission) -> None:
    try:
        await _settle_acquisitions(admission.acquisitions)
    finally:
        await admission.resources.aclose()


async def _await_cleanup(cleanup: Awaitable[object]) -> object:
    return await cleanup


async def _acquire_checked[T](acquisition: Callable[[], Awaitable[T]]) -> T:
    require_interactive()
    return await acquisition()


def owned_resource[T](
    acquisition: Callable[[], Awaitable[T]], close: Callable[[T], Awaitable[object]]
) -> AbstractAsyncContextManager[T]:
    """Close even a resource acquired after cancellation of its waiting caller."""
    return _OwnedResource(acquisition, close)


class _OwnedResource[T](AbstractAsyncContextManager[T]):
    def __init__(
        self, acquisition: Callable[[], Awaitable[T]], close: Callable[[T], Awaitable[object]]
    ) -> None:
        self.acquisition = acquisition
        self.close = close

    async def __aenter__(self) -> T:
        self.runtime = require_interactive()
        task = asyncio.create_task(_acquire_checked(self.acquisition))
        try:
            self.resource = await asyncio.shield(task)
        except BaseException:
            await self.runtime.finish_cleanup(_close_acquired(task, self.close))
            raise
        try:
            require_interactive()
        except BaseException:
            await self.runtime.finish_cleanup(self.close(self.resource))
            raise
        return self.resource

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.runtime.finish_cleanup(self.close(self.resource))


async def _close_acquired[T](
    task: asyncio.Task[T], close: Callable[[T], Awaitable[object]]
) -> None:
    resource = await task
    await close(resource)


def _guarded(scope: Scope) -> bool:
    if scope["type"] not in {"http", "websocket"}:
        return False
    path = get_route_path(scope).rstrip("/")
    if scope["type"] == "http":
        return scope.get("method") == "POST" and path in {
            "/ssh/sessions",
            "/proxmox/console/sessions",
        }
    return scope["type"] == "websocket" and (
        path in {"/ws", "/ws/virtual-machines"}
        or bool(re.fullmatch(r"/ssh/sessions/[^/]+/ws", path))
    )


class InteractiveBoundaryMiddleware:
    """Deny before FastAPI dependencies and own the complete ASGI lifetime."""

    def __init__(self, app: ASGIApp, runtime: InteractiveRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _guarded(scope):
            await self.app(scope, receive, send)
            return
        try:
            self.runtime.require()
        except InteractiveDenied:
            await self._deny(scope, receive, send)
            return
        async with self.runtime.admission():
            await self._run_owned(scope, receive, send)

    async def _run_owned(self, scope: Scope, receive: Receive, send: Send) -> None:
        closed = False
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal closed, started
            if message["type"] == "websocket.close":
                closed = True
            else:
                require_interactive()
            if message["type"] in {"http.response.start", "websocket.accept"}:
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except InteractiveDenied:
            if not started:
                await self._deny(scope, receive, send)
                closed = scope["type"] == "websocket"
        finally:
            if scope["type"] == "websocket" and not closed:
                await self.runtime.finish_cleanup(
                    send({"type": "websocket.close", "code": 1001, "reason": "Session ended"})
                )

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": DENIAL})
        else:
            await JSONResponse({"detail": DENIAL}, status_code=403)(scope, receive, send)
