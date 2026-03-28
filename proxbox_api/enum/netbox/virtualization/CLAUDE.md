# proxbox_api/enum/netbox/virtualization Directory Guide

## Purpose

Virtualization status enumerations for NetBox schema validation.

## Modules and Responsibilities

- `__init__.py`: Virtualization status options used by NetBox schema models.

## Key Data Flow and Dependencies

- schemas/netbox/virtualization imports ClusterStatusOptions for cluster payload correctness.

## Extension Guidance

- Update enum values with care; these values are sent to external NetBox APIs.
