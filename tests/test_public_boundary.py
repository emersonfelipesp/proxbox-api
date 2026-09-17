from __future__ import annotations

import base64
import gzip
import io
import subprocess
import tarfile
import zipfile
from importlib import util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_public_boundary.py"
SPEC = util.spec_from_file_location("check_public_boundary", SCRIPT)
assert SPEC and SPEC.loader
boundary = util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


def test_repository_public_boundary() -> None:
    assert boundary.find_violations(boundary.repository_files()) == []


def test_mutation_corpus() -> None:
    boundary.run_mutations()


def test_mixed_and_multiline_literal_streams_are_rejected() -> None:
    slash = bytes((92,))
    mutations = (
        b'const value = "' + slash + b'x6e" + "' + slash + b'u006d" + "s"',
        b"printf '" + slash + b"156" + slash + b"155s'",
        b'const value = "' + slash + b'x6e" +\n  "m" +\n  "' + slash + b'u0073"',
    )
    for content in mutations:
        assert boundary.find_violations([("mutation.js", content)])


def _token() -> bytes:
    return boundary.PRIVATE_NAMES[0]


def _percent(*, layered: bool = False) -> bytes:
    value = b"".join(f"%{item:02x}".encode() for item in _token())
    return value.replace(b"%", b"%25") if layered else value


def _html_entities() -> bytes:
    return b"".join(f"&#{item};".encode() for item in _token())


def _tar(payload: bytes, *, prefix: bytes = b"") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo("package")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("package/payload.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return prefix + output.getvalue()


def _tar_symlink() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        archive.addfile(member)
    return output.getvalue()


def _zip(payload: bytes, *, prefix: bytes = b"", member: str = "payload.txt") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
    return prefix + output.getvalue()


def test_archive_encoding_and_constant_binding_bypasses_are_rejected() -> None:
    token = _token()
    integers = b", ".join(str(item).encode() for item in token)
    mutations = (
        ("percent.txt", _percent()),
        ("layered.txt", _percent(layered=True)),
        ("html.txt", _html_entities()),
        ("base64.txt", base64.b64encode(token)),
        ("percent-base64.txt", _percent_encoded(base64.b64encode(token))),
        ("html-base64.txt", _html_encoded(base64.b64encode(token))),
        ("charcode.js", b"const x=String.fromCharCode(110,109,115)"),
        ("array-charcode.js", b'const x=[110,109,115].map(String.fromCharCode).join("")'),
        ("bindings.js", b'const a="n",b="m",c="s";const x=a+b+c'),
        ("bindings.py", b'a="n"\nb="m"\nc="s"\nx=a+b+c\n'),
        ("chr-generator.py", b'v="".join(chr(x) for x in (110,109,115))'),
        ("bytes-tuple.py", b"v=bytes((" + integers + b"))"),
        ("bytes-list.py", b"v=bytes([" + integers + b"])"),
        ("bytearray-tuple.py", b"v=bytearray((" + integers + b"))"),
        ("map-chr.py", b'v="".join(map(chr,(' + integers + b")))"),
        ("standalone.txt.gz", gzip.compress(token)),
        ("archive.tgz", _tar(token)),
        ("prefixed.bin", _tar(token, prefix=b"SFX-PREFIX")),
        ("archive.zip", _zip(token)),
        ("prefixed.exe", _zip(token, prefix=b"SFX-PREFIX")),
        ("member.zip", _zip(b"clean", member=_token().decode() + ".txt")),
    )
    for name, content in mutations:
        assert boundary.find_violations([(name, content)]), name


def test_two_through_four_base64_layers_are_rejected() -> None:
    encoded = _token()
    for layers in range(1, 5):
        encoded = base64.b64encode(encoded)
        if layers >= 2:
            assert boundary.find_violations([(f"base64-{layers}.txt", encoded)])


def _percent_encoded(value: bytes) -> bytes:
    return b"".join(f"%{item:02x}".encode() for item in value)


def _html_encoded(value: bytes) -> bytes:
    return b"".join(f"&#{item};".encode() for item in value)


@pytest.mark.parametrize(
    ("name", "content"),
    (("broken.zip", b"PK\x03\x04broken"), ("broken.tgz", b"\x1f\x8bbroken")),
)
def test_malformed_archives_fail_closed(name: str, content: bytes) -> None:
    with pytest.raises(RuntimeError, match="malformed"):
        boundary.find_violations([(name, content)])


def test_tar_links_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="unsupported TAR member type"):
        boundary.find_violations([("linked.tgz", _tar_symlink())])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_repository_files_uses_index_then_worktree_overlay(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    target = tmp_path / "candidate.txt"
    target.write_bytes(_token())
    _git(tmp_path, "add", target.name)
    target.write_text("clean working tree")
    files = boundary.repository_files(tmp_path)
    assert boundary.find_violations(files)

    target.write_text("modified candidate")
    assert dict(boundary.repository_files(tmp_path))[target.name] == b"modified candidate"
    target.unlink()
    assert dict(boundary.repository_files(tmp_path))[target.name] == _token()


def test_repository_files_rejects_broken_symlink_and_index_symlink(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "README").write_text("clean")
    _git(tmp_path, "add", "README")
    broken = tmp_path / "broken"
    broken.symlink_to("missing")
    with pytest.raises(RuntimeError, match="unsupported publishable path"):
        boundary.repository_files(tmp_path)
    _git(tmp_path, "add", "broken")
    with pytest.raises(RuntimeError, match="unsupported indexed mode 120000"):
        boundary.repository_files(tmp_path)
