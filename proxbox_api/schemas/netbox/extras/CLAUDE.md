# proxbox_api/schemas/netbox/extras Directory Guide

## Purpose

Schemas for NetBox extras payloads such as tags.

## Modules and Responsibilities

- `__init__.py`: NetBox extras schema models such as tags.

## Key Data Flow and Dependencies

- Referenced by DCIM and virtualization schemas for typed nested objects.

## Extension Guidance

- Keep extras schemas generic and reusable across multiple domains.
