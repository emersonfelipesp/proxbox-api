"""Contract tests: image factory operates without any DB persistence."""

from pathlib import Path
from unittest.mock import MagicMock

from proxbox_api.schemas.image_factory import PackerImageBuildRequest, PackerImageBuildResponse


def test_image_build_run_table_removed():
    """ImageBuildRun SQLModel class no longer exists in database module."""
    from proxbox_api import database

    assert not hasattr(database, "ImageBuildRun")


def test_models_no_db_crud():
    """DB CRUD helpers are removed from image_factory models."""
    from proxbox_api.services.image_factory import models

    assert not hasattr(models, "create_image_build_run")
    assert not hasattr(models, "get_image_build_run")
    assert not hasattr(models, "update_image_build_run")
    assert not hasattr(models, "response_from_run")


def test_live_run_helpers_present():
    """In-memory live-run helpers remain available."""
    from proxbox_api.services.image_factory import models

    assert callable(models.register_live_run)
    assert callable(models.get_live_run)
    assert callable(models.drop_live_run)
    assert callable(models.response_from_live)


def test_response_from_live_shape():
    """response_from_live produces a valid PackerImageBuildResponse."""
    from proxbox_api.services.image_factory.models import LiveImageBuildRun, response_from_live
    from proxbox_api.services.image_factory.renderer import RenderedPackerWorkdir
    from proxbox_api.services.image_factory.runner import PackerRunner

    request = PackerImageBuildRequest(
        endpoint_id=1,
        target_node="pve1",
        output_vmid=9000,
        output_name="test-template",
        os_family="debian",
        os_release="bookworm",
        image_version="12",
        vm_storage="local-lvm",
        provisioner_recipe="debian-base",
    )
    rendered = MagicMock(spec=RenderedPackerWorkdir)
    rendered.template_path = Path("/tmp/test.pkr.hcl")
    runner = MagicMock(spec=PackerRunner)

    live = LiveImageBuildRun(
        build_id="test-123",
        request=request,
        rendered=rendered,
        runner=runner,
    )
    resp = response_from_live(live, status="running")
    assert isinstance(resp, PackerImageBuildResponse)
    assert resp.build_id == "test-123"
    assert resp.status == "running"
    assert resp.endpoint_id == 1
    assert resp.target_node == "pve1"
    assert resp.output_vmid == 9000
    assert resp.output_name == "test-template"
    assert resp.log_url == "/cloud/image-factory/builds/test-123/stream"


def test_response_from_live_timestamps():
    """response_from_live passes through timestamps."""
    from datetime import datetime, timezone

    from proxbox_api.services.image_factory.models import LiveImageBuildRun, response_from_live
    from proxbox_api.services.image_factory.renderer import RenderedPackerWorkdir
    from proxbox_api.services.image_factory.runner import PackerRunner

    request = PackerImageBuildRequest(
        endpoint_id=1,
        target_node="pve1",
        output_vmid=9000,
        output_name="test",
        os_family="debian",
        os_release="bookworm",
        image_version="12",
        vm_storage="local-lvm",
        provisioner_recipe="test",
    )
    rendered = MagicMock(spec=RenderedPackerWorkdir)
    rendered.template_path = Path("/tmp/test.pkr.hcl")
    runner = MagicMock(spec=PackerRunner)
    live = LiveImageBuildRun(
        build_id="ts-test",
        request=request,
        rendered=rendered,
        runner=runner,
    )
    now = datetime.now(timezone.utc)
    resp = response_from_live(live, status="completed", started_at=now, completed_at=now)
    assert resp.started_at == now
    assert resp.completed_at == now
    assert resp.status == "completed"


def test_endpoint_by_url_extract_host():
    """_extract_host parses hostnames from various URL formats."""
    from proxbox_api.routes.cloud.image_factory import _extract_host

    assert _extract_host("https://pve1.example.com:8006") == "pve1.example.com"
    assert _extract_host("https://10.0.30.100:8006/api2/json") == "10.0.30.100"
    assert _extract_host("http://pve2:8006") == "pve2"
