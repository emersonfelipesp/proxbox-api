"""Unified resolver for runtime configuration values.

Resolution order for every key:

    environment variable (override)  >  ProxboxPluginSettings (NetBox DB)  >  hardcoded default

This keeps env-vars usable as ad-hoc overrides while making
``ProxboxPluginSettings`` (managed via the netbox-proxbox UI / REST API) the
canonical source of truth so most ``.env`` entries can be removed in
production deployments.

Each helper accepts an optional ``minimum`` so callers can clamp pathological
values without repeating the same boilerplate at every read site.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from proxbox_api.logger import logger

if TYPE_CHECKING:
    from proxbox_api.types.structured_dicts import ProxboxSettingsDict


def _load_settings() -> "ProxboxSettingsDict | None":
    try:
        from proxbox_api.settings_client import get_settings

        return get_settings()
    except Exception as exc:  # noqa: BLE001
        logger.debug("runtime_settings: could not load plugin settings: %s", exc)
        return None


def get_int(
    *,
    settings_key: str,
    env: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(env, "").strip()
    if raw:
        try:
            return _clamp_int(int(raw), minimum, maximum)
        except ValueError:
            logger.warning("Invalid integer for %s=%r, falling back to settings/default", env, raw)

    settings = _load_settings()
    if settings is not None:
        value = settings.get(settings_key)
        if value is not None and value != "":
            try:
                return _clamp_int(int(value), minimum, maximum)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid integer in plugin settings %s=%r, using default", settings_key, value
                )

    return _clamp_int(default, minimum, maximum)


def get_float(
    *,
    settings_key: str,
    env: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.environ.get(env, "").strip()
    if raw:
        try:
            return _clamp_float(float(raw), minimum, maximum)
        except ValueError:
            logger.warning("Invalid float for %s=%r, falling back to settings/default", env, raw)

    settings = _load_settings()
    if settings is not None:
        value = settings.get(settings_key)
        if value is not None and value != "":
            try:
                return _clamp_float(float(value), minimum, maximum)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid float in plugin settings %s=%r, using default", settings_key, value
                )

    return _clamp_float(default, minimum, maximum)


def get_bool(*, settings_key: str, env: str, default: bool) -> bool:
    raw = os.environ.get(env, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False

    settings = _load_settings()
    if settings is not None:
        value = settings.get(settings_key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, (int, float)):
            return bool(value)

    return default


def _clamp_int(value: int, minimum: int | None, maximum: int | None) -> int:
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _clamp_float(value: float, minimum: float | None, maximum: float | None) -> float:
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


__all__ = ["get_int", "get_float", "get_bool"]
