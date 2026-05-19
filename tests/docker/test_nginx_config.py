"""Tests for the nginx config template shipped with the proxbox-api Docker image."""
import re
from pathlib import Path

TEMPLATE = Path(__file__).parents[2] / "docker" / "nginx" / "proxbox-https.conf.template"


def test_template_exists():
    assert TEMPLATE.exists(), f"nginx config template not found at {TEMPLATE}"


def test_no_deprecated_listen_http2():
    """listen ... http2 inline flag was deprecated in nginx 1.25.0 (June 2023).
    The replacement is a standalone 'http2 on;' directive at the server block level.
    See https://nginx.org/en/docs/http/ngx_http_v2_module.html
    """
    content = TEMPLATE.read_text()
    deprecated = re.search(r"listen\s+.*\bhttp2\b", content)
    assert deprecated is None, (
        f"Deprecated 'listen ... http2' syntax found at: {deprecated.group()!r}. "
        "Use standalone 'http2 on;' directive instead (nginx >= 1.25.0)."
    )


def test_standalone_http2_directive_present():
    """The modern http2 directive must be present so HTTP/2 is still enabled."""
    content = TEMPLATE.read_text()
    assert re.search(r"^\s*http2\s+on\s*;", content, re.MULTILINE), (
        "Missing standalone 'http2 on;' directive in nginx config template."
    )
