"""Private, generation-bound authority for uncached plugin-root acquisition."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, cast
from uuid import uuid4

from netbox_sdk.config import authorization_header_value

from proxbox_api.exception import ProxboxException

_RUNTIME_PATH = "/api/plugins/proxbox/settings/runtime/"
_AUTHORITY_TIMEOUT_SECONDS = 2.0
_MAX_RESPONSE_BYTES = 65_536
_MAX_KEY_BYTES = 4096
_LOCK = threading.Lock()


class PluginKeyAuthorityError(ProxboxException):
    """A failed authority check is not an ordinary missing-key condition."""

    def __init__(self) -> None:
        super().__init__(
            message="Plugin encryption-key authorization is unavailable.",
            http_status_code=503,
            redact_log_details=True,
        )


@dataclass(frozen=True, slots=True)
class PluginKeyBinding:
    """Opaque client-generation identity; contains no authentication material."""

    endpoint_id: int
    generation: str


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedPluginKey:
    """One fresh runtime result, never a reusable permission decision."""

    raw_key: str = field(repr=False)
    binding: PluginKeyBinding


@dataclass(frozen=True, slots=True, repr=False)
class _Candidate:
    binding: PluginKeyBinding
    base_url: str = field(repr=False)
    authorization: str = field(repr=False)
    ssl_verify: bool


_CLIENTS: dict[int, tuple[object, _Candidate]] = {}
_CANDIDATES: dict[PluginKeyBinding, _Candidate] = {}
_DEFAULT_BINDING: PluginKeyBinding | None = None


def _valid_authority_url_parts(parsed: urllib.parse.SplitResult) -> bool:
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
    )


def _valid_authority_base_url(base_url: str) -> bool:
    if len(base_url) > 4096 or any(ord(character) < 32 for character in base_url):
        return False
    try:
        parsed = urllib.parse.urlsplit(base_url)
        _ = parsed.port
        return _valid_authority_url_parts(parsed)
    except ValueError:
        return False


def _configuration_inputs(config: object) -> tuple[str, str, bool] | None:
    base_url = getattr(config, "base_url", None)
    if not isinstance(base_url, str) or not _valid_authority_base_url(base_url):
        return None
    try:
        authorization = authorization_header_value(config)
    except Exception:
        return None
    if not isinstance(authorization, str) or not authorization:
        return None
    if "\r" in authorization or "\n" in authorization:
        return None
    return base_url.rstrip("/"), authorization, getattr(config, "ssl_verify", True) is not False


def _candidate_inputs(candidate: _Candidate) -> tuple[str, str, bool]:
    return candidate.base_url, candidate.authorization, candidate.ssl_verify


def _retire_conflicting_candidates_locked(
    facade: object, inputs: tuple[str, str, bool], *, endpoint_id: int
) -> bool:
    """Retire changed identities, including replacement facades for the same row."""
    retired_default = False
    for identity, (_client, candidate) in list(_CLIENTS.items()):
        same_row = candidate.binding.endpoint_id == endpoint_id
        if identity != id(facade) and not same_row:
            continue
        if same_row and _candidate_inputs(candidate) == inputs:
            continue
        _CLIENTS.pop(identity)
        _CANDIDATES.pop(candidate.binding, None)
        retired_default |= candidate.binding == _DEFAULT_BINDING
    return retired_default


def register_plugin_key_candidate(facade: object, config: object, *, endpoint_id: int) -> None:
    """Capture immutable inputs without authenticating or selecting a root source."""
    # An invalid default configuration is a blocked candidate, not standalone mode.
    inputs = _configuration_inputs(config) or ("", "", True)
    with _LOCK:
        retired_default = _retire_conflicting_candidates_locked(
            facade, inputs, endpoint_id=endpoint_id
        )
        if id(facade) in _CLIENTS:
            return
        base_url, authorization, ssl_verify = inputs
        binding = PluginKeyBinding(endpoint_id, str(uuid4()))
        candidate = _Candidate(binding, base_url, authorization, ssl_verify)
        _CLIENTS[id(facade)] = facade, candidate
        _CANDIDATES[binding] = candidate
    if retired_default:
        from proxbox_api.credentials import retire_plugin_key_material

        retire_plugin_key_material()


def designate_default_plugin_authority(facade: object) -> None:
    """Only default-service selection can designate a candidate; metadata cannot."""
    global _DEFAULT_BINDING
    with _LOCK:
        record = _CLIENTS.get(id(facade))
        if record is not None and record[0] is facade and _DEFAULT_BINDING is None:
            _DEFAULT_BINDING = record[1].binding


def reset_plugin_key_selection() -> None:
    """Explicit source reset permits a later default-service reselection."""
    global _DEFAULT_BINDING
    with _LOCK:
        _DEFAULT_BINDING = None


def invalidate_plugin_key_authority(endpoint_id: int | None = None) -> None:
    """Retire snapshots while retaining an opaque blocked default identity."""
    with _LOCK:
        identities = [
            identity
            for identity, (_facade, candidate) in _CLIENTS.items()
            if endpoint_id is None or candidate.binding.endpoint_id == endpoint_id
        ]
        for identity in identities:
            _facade, candidate = _CLIENTS.pop(identity)
            _CANDIDATES.pop(candidate.binding, None)
    # The key lock must never be held during registry or transport operations.
    from proxbox_api.credentials import retire_plugin_key_material

    retire_plugin_key_material(endpoint_id)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class _RuntimeResponse(Protocol):
    status: int
    headers: object

    def geturl(self) -> str: ...
    def read1(self, size: int) -> bytes: ...


def _read_bounded_response(response: _RuntimeResponse, *, deadline: float) -> bytes:
    data = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise PluginKeyAuthorityError()
        chunk = response.read1(min(4096, _MAX_RESPONSE_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > _MAX_RESPONSE_BYTES:
            raise PluginKeyAuthorityError()


def _decode_runtime_key(body: bytes) -> str:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise PluginKeyAuthorityError()
    key = payload.get("encryption_key")
    if not isinstance(key, str) or not key.strip():
        raise PluginKeyAuthorityError()
    if len(key.encode("utf-8")) > _MAX_KEY_BYTES:
        raise PluginKeyAuthorityError()
    return key.strip()


def _authority_opener(candidate: _Candidate) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    if not candidate.ssl_verify:
        context = ssl._create_unverified_context()
    return urllib.request.build_opener(
        _RefuseRedirects(), urllib.request.HTTPSHandler(context=context)
    )


def _request_runtime_key(candidate: _Candidate) -> str:
    """Use only the captured configuration, never a decrypting session factory."""
    deadline = time.monotonic() + _AUTHORITY_TIMEOUT_SECONDS
    url = f"{candidate.base_url}{_RUNTIME_PATH}"
    try:
        if not candidate.base_url or not candidate.authorization:
            raise PluginKeyAuthorityError()
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": candidate.authorization,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        opener = _authority_opener(candidate)
        with opener.open(request, timeout=_AUTHORITY_TIMEOUT_SECONDS) as raw_response:
            response = cast(_RuntimeResponse, raw_response)
            if response.status != 200 or response.geturl() != url:
                raise PluginKeyAuthorityError()
            headers = cast("dict[str, str]", response.headers)
            if headers.get("Content-Encoding", "identity").lower() != "identity":
                raise PluginKeyAuthorityError()
            body = _read_bounded_response(response, deadline=deadline)
            key = _decode_runtime_key(body)
            if time.monotonic() >= deadline:
                raise PluginKeyAuthorityError()
            return key
    except PluginKeyAuthorityError:
        raise
    except Exception:
        raise PluginKeyAuthorityError() from None


def get_fresh_plugin_key(
    expected_binding: PluginKeyBinding | None = None,
) -> AuthorizedPluginKey | None:
    """A new sensitive acquisition cannot use metadata caches or old grants."""
    with _LOCK:
        binding = _DEFAULT_BINDING
        candidate = _CANDIDATES.get(binding) if binding is not None else None
    if expected_binding is not None and binding != expected_binding:
        raise PluginKeyAuthorityError()
    if binding is None:
        return None
    if candidate is None:
        raise PluginKeyAuthorityError()
    key = _request_runtime_key(candidate)
    with _LOCK:
        current = _DEFAULT_BINDING == binding and _CANDIDATES.get(binding) is candidate
    if not current:
        raise PluginKeyAuthorityError()
    return AuthorizedPluginKey(key, binding)
