# proxbox_api/routes/virtualization Directory Guide

## Purpose

Virtualization route namespace and high-level endpoints.

## Current Files

- `__init__.py`: virtualization route namespace. The `cluster-types/create` and `clusters/create` endpoints are stubs that return HTTP 501.

## How These Routes Work

- This namespace acts as an entry point for cluster and virtual machine synchronization endpoints.
- The functional VM work lives under `virtual_machines/`; this package mainly owns the higher-level virtualization namespace and placeholders.

## Extension Guidance

- Promote TODO placeholders into service-backed handlers as functionality is implemented.
- Keep stubbed endpoints explicit so clients know which paths are not implemented yet.
