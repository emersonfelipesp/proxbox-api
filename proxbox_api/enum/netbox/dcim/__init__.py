"""DCIM status options used by NetBox schema models."""

from enum import Enum

class StatusOptions(str, Enum):
    planned = "planned"
    staging = "staging"
    active = "active"
    decommissioning = "decommissioning"
    retired = "retired"