# proxbox_api/diode Directory Guide

## Purpose

Experimental integration area for NetBox Labs Diode ingestion workflows.

## Current Files

- `diode.py`: Experimental Diode client integration example script.
- `docker-compose.yaml`: Local compose file for running the Diode integration sandbox.

## Key Data Flow and Dependencies

- `diode.py` creates a Diode client and ingests sample entities to a gRPC target.
- This directory is not currently wired into the FastAPI runtime path.

## Extension Guidance

- Treat this area as sandbox code until promoted into routes or services.
- Remove credentials and move runtime values to environment variables before production use.
