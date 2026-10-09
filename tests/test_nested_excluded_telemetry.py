"""Nested excluded requests must restore the calling request's native context."""

from collections.abc import Callable
from contextlib import ExitStack

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.telemetry import get_telemetry_data
from fastapi.testclient import TestClient
from opentelemetry import context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from proxmox_mock.app import create_mock_app

from proxbox_api.app.factory import create_app
from proxbox_api.firecracker_agent.app import create_firecracker_agent_app


@pytest.mark.parametrize("factory", (create_app, create_firecracker_agent_app, create_mock_app))
@pytest.mark.parametrize("fail_inner", (False, True))
def test_nested_excluded_request_restores_parent_context(
    factory: Callable[..., FastAPI], fail_inner: bool, monkeypatch
) -> None:
    monkeypatch.setattr("proxbox_api.app.factory.AUTH_EXEMPT_PATHS", {"/_outer", "/_inner/leaf"})
    with ExitStack() as cleanup:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        cleanup.callback(provider.shutdown)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        options = {
            "auto_configure": False,
            "tracer_provider": provider,
            "metrics": False,
            "logs": False,
        }
        outer = factory(telemetry=options)
        excluded = factory(telemetry={**options, "exclude": lambda scope: True})
        leaf = FastAPI(telemetry=options)
        observations = []
        scopes = []

        @leaf.get("/leaf")
        async def nested(request: Request) -> dict[str, bool]:
            observations.append(get_telemetry_data())
            scopes.append(request.scope)
            assert get_telemetry_data() is None
            if fail_inner:
                raise RuntimeError("nested-excluded-canary")
            return {"excluded": True}

        excluded.mount("/_inner", leaf)

        @outer.get("/_outer")
        async def parent() -> dict[str, bool]:
            before = get_telemetry_data()
            before_context = context.get_current()
            assert before is not None
            transport = httpx.ASGITransport(app=excluded)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://in-process"
            ) as client:
                try:
                    response = await client.get("/_inner/leaf")
                except RuntimeError as error:
                    assert fail_inner and str(error) == "nested-excluded-canary"
                else:
                    assert not fail_inner
                    assert response.status_code == 200
                    assert response.json() == {"excluded": True}
            assert get_telemetry_data() is before
            assert context.get_current() == before_context
            return {"restored": True}

        client = TestClient(outer)
        cleanup.callback(client.close)
        response = client.get("/_outer")
        assert response.status_code == 200
        assert response.json() == {"restored": True}
        assert observations == [None]
        assert len(scopes) == 1 and "fastapi.telemetry" not in scopes[0]
        # This observation is in the caller thread; restoration is checked inside parent().
        assert get_telemetry_data() is None
        server_spans = [
            span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
        ]
        assert [span.name for span in server_spans] == ["GET /_outer"]
