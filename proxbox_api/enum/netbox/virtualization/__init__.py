"""Virtualization status options used by NetBox schema models."""

from enum import Enum


class ClusterStatusOptions(str, Enum):
    planned = "planned"
    staging = "staging"
    active = "active"
    decommissioning = "decommissioning"
    offline = "offline"
