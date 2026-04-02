# proxbox_api/routes/virtualization Directory Guide

## Purpose

Virtualization route namespace and high-level endpoints.

## Current Files

- `__init__.py`: Virtualization route namespace. The `cluster-types/create` and `clusters/create` endpoints are stubs that return HTTP 501.

## Key Data Flow and Dependencies

- Acts as an entry namespace for cluster and virtual machine synchronization endpoints.

## Extension Guidance

- Promote TODO placeholders into service-backed handlers as functionality is implemented.
