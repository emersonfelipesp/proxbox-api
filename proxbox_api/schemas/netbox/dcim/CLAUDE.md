# proxbox_api/schemas/netbox/dcim Directory Guide

## Purpose

Schemas for NetBox DCIM payloads used by synchronization endpoints.

## Modules and Responsibilities

- `__init__.py`: NetBox DCIM schema models used by API payloads.

## Key Data Flow and Dependencies

- Consumes enum and tag schemas to validate outgoing payload structures.

## Extension Guidance

- Update fields when NetBox DCIM models evolve and keep optionality accurate.
