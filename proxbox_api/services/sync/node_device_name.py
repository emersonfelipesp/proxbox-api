"""Validation and rendering for Proxmox node Device names."""

from __future__ import annotations

import re
from string import Formatter

from proxbox_api.logger import logger
from proxbox_api.runtime_settings import get_str

DEFAULT_NODE_DEVICE_NAME_TEMPLATE = "{node}"
NODE_DEVICE_NAME_TEMPLATE_ENV = "PROXBOX_NODE_DEVICE_NAME_TEMPLATE"
NODE_DEVICE_NAME_TEMPLATE_SETTING = "node_device_name_template"
NODE_DEVICE_NAME_PLACEHOLDERS = frozenset({"node", "cluster", "cluster_slug", "endpoint"})
_DNS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_FORMATTER = Formatter()


class NodeDeviceNameError(ValueError):
    """A valid template rendered an unusable name for one Proxmox node."""

    def __init__(
        self,
        *,
        node: str,
        cluster: str,
        endpoint: str,
        template: str,
        rendered_name: str,
        reason: str,
    ) -> None:
        self.node = node
        self.cluster = cluster
        self.endpoint = endpoint
        self.template = template
        self.rendered_name = rendered_name
        self.rendered_length = len(rendered_name)
        super().__init__(
            f"Node {node!r} in cluster {cluster!r} rendered an invalid NetBox Device name "
            f"from template {template!r} (length {self.rendered_length}): {reason}"
        )


def slugify_cluster_name(value: str) -> str:
    """Return a DNS-label-safe cluster slug."""

    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "cluster"


def validate_rendered_device_name(name: str) -> str:
    """Validate the rendered value against NetBox and DNS name limits."""

    if not name or len(name) > 64 or not _DNS_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Rendered node device name must be 1-64 DNS-safe letters, digits, hyphens, or dots."
        )
    labels = name.split(".")
    if any(
        not label or len(label) > 63 or not _DNS_LABEL_PATTERN.fullmatch(label) for label in labels
    ):
        raise ValueError("Rendered node device name contains an invalid DNS label.")
    return name


def validate_node_device_name_template(template: str) -> str:
    """Reject unsafe format features and validate a representative render."""

    value = str(template or "").strip()
    if not value:
        raise ValueError("Node device name template must not be blank.")
    fields: set[str] = set()
    try:
        parsed = list(_FORMATTER.parse(value))
    except ValueError as error:
        raise ValueError(f"Invalid node device name template: {error}") from error
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in NODE_DEVICE_NAME_PLACEHOLDERS:
            raise ValueError(f"Unknown node device name placeholder: {field_name!r}.")
        if format_spec:
            raise ValueError("Node device name placeholders do not support format specs.")
        if conversion:
            raise ValueError("Node device name placeholders do not support conversions.")
        fields.add(field_name)
    if "node" not in fields:
        raise ValueError("Node device name template must contain {node}.")
    sample = value.format(
        node="node",
        cluster="cluster",
        cluster_slug="cluster",
        endpoint="endpoint",
    )
    validate_rendered_device_name(sample)
    return value


def resolve_node_device_name_template(
    endpoint_template: object = None,
    *,
    global_template: object = None,
    endpoint_name: str = "<unknown>",
) -> str:
    """Resolve endpoint override, then global/env setting, then safe default."""

    endpoint_value = str(endpoint_template or "").strip()
    candidate = endpoint_value
    if not candidate:
        fallback = str(global_template or DEFAULT_NODE_DEVICE_NAME_TEMPLATE).strip()
        candidate = get_str(
            settings_key=NODE_DEVICE_NAME_TEMPLATE_SETTING,
            env=NODE_DEVICE_NAME_TEMPLATE_ENV,
            default=fallback or DEFAULT_NODE_DEVICE_NAME_TEMPLATE,
        )
    try:
        return validate_node_device_name_template(candidate)
    except ValueError as error:
        logger.error(
            "Invalid node device name template %r for endpoint %r; falling back to %r: %s",
            candidate,
            endpoint_name,
            DEFAULT_NODE_DEVICE_NAME_TEMPLATE,
            error,
        )
        return DEFAULT_NODE_DEVICE_NAME_TEMPLATE


def render_node_device_name(
    node: str,
    cluster: str,
    endpoint: str,
    template: str | None = None,
) -> str:
    """Render a NetBox Device name or reject unusable node-specific output."""

    resolved = resolve_node_device_name_template(template, endpoint_name=endpoint)
    rendered = resolved.format(
        node=node,
        cluster=cluster,
        cluster_slug=slugify_cluster_name(cluster),
        endpoint=endpoint,
    )
    try:
        return validate_rendered_device_name(rendered)
    except ValueError as error:
        raise NodeDeviceNameError(
            node=node,
            cluster=cluster,
            endpoint=endpoint,
            template=resolved,
            rendered_name=rendered,
            reason=str(error),
        ) from error
