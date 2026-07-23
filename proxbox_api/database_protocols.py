"""Typed database-session contracts shared by sync and async call paths.

The Ceph control plane is exercised with SQLModel's synchronous ``Session`` in
unit tests and uses SQLModel's ``AsyncSession`` in request handlers.  A concrete
union preserves each library's generic ``exec``/``get`` result types so static
analysis can verify statements and ORM results; the former ``Any``-returning
structural protocol erased precisely those safety-critical types.
"""

from __future__ import annotations

from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession

type DatabaseSession = Session | AsyncSession

# Compatibility name for the issue branch's existing annotations.  This is a
# concrete typed union, not a permissive ``Protocol`` and contains no ``Any``.
type DatabaseSessionProtocol = DatabaseSession

__all__ = ["DatabaseSession", "DatabaseSessionProtocol"]
