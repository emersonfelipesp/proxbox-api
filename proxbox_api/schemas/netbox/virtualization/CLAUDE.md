# proxbox_api/schemas/netbox/virtualization Directory Guide

## Purpose

Schemas for NetBox virtualization objects such as clusters and cluster types.

## Current Files

- `__init__.py`: NetBox virtualization schema models for clusters and types.

## How These Schemas Flow

- Virtualization route and sync modules use these models to shape valid NetBox payloads.
- Status and choice enums are wired through this package to keep cluster payloads valid.

## Extension Guidance

- Mirror NetBox model constraints and status choices carefully.
- Keep schema names stable so route and service imports remain simple.
