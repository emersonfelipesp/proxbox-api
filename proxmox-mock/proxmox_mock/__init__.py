"""Schema-driven Proxmox mock API helpers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proxmox-mock-api")
except PackageNotFoundError:
    __version__ = "0.0.0"


def create_mock_app():
    from proxmox_mock.app import create_mock_app as _create_mock_app

    return _create_mock_app()


__all__ = ["__version__", "create_mock_app"]
