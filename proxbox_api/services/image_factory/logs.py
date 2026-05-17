"""Packer log normalization and defensive secret scrubbing."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class PackerEvent:
    name: str
    data: dict[str, Any]


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|secret|authorization|cookie)\b(\s*[=:]\s*)([^\s,;]+)"
)
_PROXMOX_ENV_RE = re.compile(r"(?i)\b(PROXMOX_(?:URL|USERNAME|TOKEN|PASSWORD))=([^\s,;]+)")


def scrub_text(value: object, secrets: Iterable[str] = ()) -> str:
    text = "" if value is None else str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[redacted]")
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
    return _PROXMOX_ENV_RE.sub(r"\1=[redacted]", text)


def scrub_payload(payload: dict[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    scrubbed: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            scrubbed[key] = scrub_text(value, secrets)
        elif isinstance(value, dict):
            scrubbed[key] = scrub_payload(value, secrets)
        elif isinstance(value, list):
            scrubbed[key] = [
                scrub_text(item, secrets) if isinstance(item, str) else item for item in value
            ]
        else:
            scrubbed[key] = value
    return scrubbed


def normalize_machine_readable_line(
    line: str,
    *,
    phase: str,
    stream: str = "stdout",
    secrets: Iterable[str] = (),
) -> PackerEvent:
    message = line.strip()
    target = ""
    event_type = "raw"
    try:
        row = next(csv.reader([line]))
    except csv.Error:
        row = []
    if len(row) >= 4:
        target = row[1]
        event_type = row[2]
        message = ",".join(row[4:]) if len(row) > 4 else row[3]
    return PackerEvent(
        name="packer_log",
        data={
            "phase": phase,
            "stream": stream,
            "target": scrub_text(target, secrets),
            "type": scrub_text(event_type, secrets),
            "message": scrub_text(message, secrets),
        },
    )


def normalize_timestamp_ui_line(
    line: str,
    *,
    stream: str = "stdout",
    secrets: Iterable[str] = (),
) -> PackerEvent:
    message = scrub_text(line.strip(), secrets)
    lower = message.lower()
    if "artifact" in lower or "template" in lower and "created" in lower:
        return PackerEvent(
            name="packer_artifact",
            data={
                "stream": stream,
                "message": message,
            },
        )
    return PackerEvent(
        name="packer_log",
        data={
            "phase": "build",
            "stream": stream,
            "message": message,
        },
    )
