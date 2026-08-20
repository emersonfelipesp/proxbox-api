"""Derive a NetBox ``Platform`` name from Proxmox guest-OS information.

NetBox virtual machines carry a ``platform`` field, which is the natural home for the
guest operating system, and Proxbox never populated it. Two sources are reachable during
a sync and neither was used:

* **``ostype`` from the VM configuration** — coarse but always present, and free: it is
  already in the config payload the sync fetches.
* **The QEMU guest agent's ``get-osinfo``** — the exact product, e.g.
  ``{"name": "Ubuntu", "version-id": "22.04", "pretty-name": "Ubuntu 22.04.5 LTS"}``.
  Costs one extra Proxmox request per eligible VM, so it is opt-in.

``pretty-name`` is deliberately **not** used. It embeds the patch level, so every minor
update would mint a new NetBox platform and the list would grow without bound. The refined
name is ``name`` plus ``version-id``.
"""

from __future__ import annotations

import re
import unicodedata

# ``ostype`` values Proxmox reports, mapped to the platform Proxbox creates.
#
# Transcribed from the Proxmox VE API documentation for the QEMU ``ostype`` and LXC
# ``ostype`` parameters. Deliberately a data table: adding a guest type is a data change,
# not a code change. An unknown value maps to nothing rather than being guessed at.
_OSTYPE_PLATFORMS: dict[str, str] = {
    # QEMU — Linux
    "l24": "Linux (kernel 2.4)",
    "l26": "Linux (kernel 2.6 or newer)",
    # QEMU — other UNIX
    "solaris": "Solaris",
    "other": "Other",
    # QEMU — Windows
    "wxp": "Windows XP",
    "w2k": "Windows 2000",
    "w2k3": "Windows Server 2003",
    "w2k8": "Windows Server 2008",
    "wvista": "Windows Vista",
    "win7": "Windows 7",
    "win8": "Windows 8",
    "win10": "Windows 10",
    "win11": "Windows 11",
    # LXC distributions
    "alpine": "Alpine Linux",
    "archlinux": "Arch Linux",
    "centos": "CentOS",
    "debian": "Debian",
    "devuan": "Devuan",
    "fedora": "Fedora",
    "gentoo": "Gentoo",
    "nixos": "NixOS",
    "opensuse": "openSUSE",
    "ubuntu": "Ubuntu",
    "unmanaged": "Unmanaged",
}

#: NetBox ``Platform.name`` is a 100-character CharField and ``slug`` is 100 characters.
_NETBOX_PLATFORM_NAME_MAX = 100
_NETBOX_PLATFORM_SLUG_MAX = 100

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def platform_slug(name: str) -> str:
    """Return a stable NetBox slug for a platform name.

    Matching on slug is what keeps repeated syncs converging on one record instead of
    accumulating near-duplicates, so this must be deterministic for a given name.
    """
    # Fold accents rather than dropping the characters silently, so "Fedora Coré" and
    # "Fedora Core" do not collapse to different slugs by losing different characters.
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_only.lower()).strip("-")
    return slug[:_NETBOX_PLATFORM_SLUG_MAX].strip("-")


def platform_from_ostype(ostype: object) -> str | None:
    """Map a Proxmox ``ostype`` to a platform name, or ``None`` when unknown.

    ``None`` means *leave the platform unset*. Guessing would put a wrong operating
    system on an inventory page, which is worse than an empty field.
    """
    if not isinstance(ostype, str):
        return None
    return _OSTYPE_PLATFORMS.get(ostype.strip().lower())


def platform_from_guest_agent(osinfo: object) -> str | None:
    """Map a QEMU guest-agent ``get-osinfo`` result to a platform name.

    This reads data produced outside the repository by a guest the operator does not
    necessarily control, so every shape must degrade rather than raise: a non-mapping
    payload, missing keys, wrong-typed values, and oversized strings all return ``None``
    so the caller falls back to the ``ostype``-derived value.
    """
    if not isinstance(osinfo, dict):
        return None

    # Proxmox returns the agent payload under "result"; accept both the wrapped and
    # unwrapped forms so a caller does not have to know which it holds.
    inner = osinfo.get("result")
    if isinstance(inner, dict):
        osinfo = inner

    name = osinfo.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None

    version = osinfo.get("version-id")
    parts = [name]
    if isinstance(version, str) and version.strip():
        parts.append(version.strip())

    # Never `pretty-name`: it embeds the patch level, so every minor update would create
    # a new NetBox platform.
    candidate = " ".join(parts)
    # Collapse whitespace a guest may have embedded, then bound the length.
    candidate = " ".join(candidate.split())
    if not candidate:
        return None
    return candidate[:_NETBOX_PLATFORM_NAME_MAX].strip()


def resolve_platform_name(
    *,
    ostype: object,
    guest_agent_osinfo: object = None,
) -> str | None:
    """Best available platform name: agent-refined when usable, else ``ostype``-derived.

    The agent value is preferred because it names the actual product; the ``ostype``
    value is the floor. Both absent means the platform stays unset.
    """
    return platform_from_guest_agent(guest_agent_osinfo) or platform_from_ostype(ostype)
