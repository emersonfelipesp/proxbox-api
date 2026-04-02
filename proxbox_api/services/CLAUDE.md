# proxbox_api/services Directory Guide

## Purpose

Service layer package namespace for reusable business workflows.

## Current Modules

- `__init__.py`: Service package namespace.
- `proxmox_helpers.py`: Shared Proxmox helper functions used by route orchestration.
- `sync/`: Sync workflows for clusters, devices, virtual machines, storage, backups, and task history.

## Key Data Flow and Dependencies

- Routes import sync services from `services/sync` during orchestration.

## Extension Guidance

- Keep services side-effect aware and independent from HTTP request objects where possible.
