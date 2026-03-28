# proxbox_api/schemas/netbox Directory Guide

## Purpose

Schemas representing NetBox connection and configuration data.

## Modules and Responsibilities

- `__init__.py`: Schemas for NetBox session settings and connection details.

## Key Data Flow and Dependencies

- Used by plugin configuration routes and dependency setup routines.

## Extension Guidance

- Avoid embedding runtime logic in schemas; keep them declarative.
