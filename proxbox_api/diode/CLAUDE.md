# proxbox_api/diode Directory Guide

## Purpose

Experimental integration area for NetBox Labs Diode ingestion workflows.

## Current Files

- `diode.py`: example client script for Diode ingestion.
- `docker-compose.yaml`: local compose file for the Diode sandbox.

## Current Role in the App

- This directory is not wired into the FastAPI runtime path.
- It exists for experimentation and isolated validation of Diode-related workflows before they are promoted into the main app.

## Extension Guidance

- Treat this directory as sandbox code until functionality is formally moved into routes or services.
- Keep secrets and runtime-specific values out of the tracked files.
- If code here becomes part of the application, move the behavior into the proper package and add a scoped guide there.
