# proxbox_api/services Directory Guide

## Purpose

Service layer package namespace for reusable business workflows.

## Modules and Responsibilities

- `__init__.py`: Service layer package namespace for proxbox_api.

## Key Data Flow and Dependencies

- routes import sync services from services/sync during orchestration.

## Extension Guidance

- Keep services side-effect aware and independent from HTTP request objects where possible.
