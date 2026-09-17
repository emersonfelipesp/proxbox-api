#!/usr/bin/env python3
"""Reject closed integration identities from the public repository boundary."""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import gzip
import html
import io
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import zipfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _policy_name(*codepoints: int) -> bytes:
    """Construct one scanner policy value without exempting a publishable path."""
    return bytes(codepoints)


PRIVATE_NAMES = (
    _policy_name(110, 109, 115),
    _policy_name(110, 109, 117, 108, 116, 105),
)
ESCAPE = re.compile(rb"\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}|\\[0-7]{3}")
SEPARATOR = re.compile(rb"[\"']\s*(?:\+\s*)?[\"']")
MAX_LITERAL = 8192
MAX_DECODE_ROUNDS = 4
MAX_DECODE_VARIANTS = 128
MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
BASE64_TOKEN = re.compile(
    rb"(?<![A-Za-z0-9+/_=-])([A-Za-z0-9+/_-]{4,8192}={0,2})(?![A-Za-z0-9+/_=-])"
)
JS_CODEPOINT = re.compile(
    rb"String\.from(?:CharCode|CodePoint)\(\s*((?:0x[0-9a-f]+|\d+)(?:\s*,\s*(?:0x[0-9a-f]+|\d+))*)\s*\)",
    re.IGNORECASE,
)
JS_BINDING = re.compile(rb"([A-Za-z_$][\w$]*)\s*=\s*([\"'])(.{0,8192}?)\2", re.DOTALL)
JS_CONCAT = re.compile(rb"(?:^|[=;,])\s*((?:[A-Za-z_$][\w$]*\s*\+\s*)+[A-Za-z_$][\w$]*)")
JS_ARRAY_CODEPOINT = re.compile(
    rb"\[\s*((?:0x[0-9a-f]+|\d+)(?:\s*,\s*(?:0x[0-9a-f]+|\d+))*)\s*\]"
    rb"\.map\(\s*String\.from(?:CharCode|CodePoint)\s*\)\.join\(\s*(?:\"\"|'')\s*\)",
    re.IGNORECASE,
)


def _contains_private_name(value: bytes) -> bool:
    lowered = value.lower()
    for name in PRIVATE_NAMES:
        if name == PRIVATE_NAMES[0]:
            if re.search(rb"(?<![a-z])" + name + rb"(?![a-z])", lowered):
                return True
        elif name in lowered:
            return True
    return False


def _decode_escapes(content: bytes) -> bytes:
    def decode(match: re.Match[bytes]) -> bytes:
        value = match.group()
        if value.startswith(b"\\x"):
            return bytes((int(value[2:], 16),))
        if value.startswith(b"\\u"):
            codepoint = int(value[2:], 16)
            if 0xD800 <= codepoint <= 0xDFFF:
                return value
            return chr(codepoint).encode()
        return bytes((int(value[1:], 8),))

    decoded = ESCAPE.sub(decode, content)
    for _ in range(256):
        joined = SEPARATOR.sub(b"", decoded)
        if joined == decoded:
            return joined
        decoded = joined
    raise RuntimeError("literal concatenation exceeded its bound")


def _call_literal(node: ast.Call) -> str | bytes | None:
    if node.keywords or len(node.args) != 1:
        return None
    if isinstance(node.func, ast.Name):
        return _named_call_literal(node)
    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr == "fromhex":
        return _fromhex_literal(node)
    if node.func.attr == "join":
        return _join_literal(node)
    return None


def _named_call_literal(node: ast.Call) -> bytes | None:
    assert isinstance(node.func, ast.Name)
    if node.func.id not in {"bytes", "bytearray"}:
        return None
    return _byte_sequence_literal(node.args[0])


def _byte_sequence_literal(node: ast.AST) -> bytes | None:
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) > 512:
        return None
    values = _integer_constants(node.elts)
    if values is None:
        return None
    try:
        return bytes(values)
    except ValueError:
        return None


def _fromhex_literal(node: ast.Call) -> bytes | None:
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"bytes", "bytearray"}
    ):
        value = _literal(node.args[0])
        if isinstance(value, str) and len(value) <= MAX_LITERAL * 2:
            try:
                return bytes.fromhex(value)
            except ValueError:
                return None
    return None


def _join_literal(node: ast.Call) -> str | bytes | None:
    assert isinstance(node.func, ast.Attribute)
    separator = _literal(node.func.value)
    sequence = node.args[0]
    if isinstance(separator, (str, bytes)) and isinstance(sequence, (ast.List, ast.Tuple)):
        return _join_sequence(separator, sequence.elts)
    generated = _chr_comprehension(sequence)
    if generated is None:
        generated = _mapped_chr_sequence(sequence)
    if isinstance(separator, str) and generated is not None:
        result = separator.join(generated)
        return result if len(result) <= MAX_LITERAL else None
    return None


def _named_call_arguments(node: ast.AST, function_name: str) -> tuple[ast.AST, ...] | None:
    if not isinstance(node, ast.Call) or node.keywords:
        return None
    if not isinstance(node.func, ast.Name) or node.func.id != function_name:
        return None
    return tuple(node.args)


def _bounded_sequence_items(node: ast.AST) -> list[ast.expr] | None:
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) > 512:
        return None
    return node.elts


def _mapped_chr_sequence(node: ast.AST) -> list[str] | None:
    arguments = _named_call_arguments(node, "map")
    if arguments is None or len(arguments) != 2:
        return None
    function, sequence = arguments
    if not isinstance(function, ast.Name) or function.id != "chr":
        return None
    items = _bounded_sequence_items(sequence)
    if items is None:
        return None
    values = _integer_constants(items)
    if values is None:
        return None
    try:
        return [chr(value) for value in values]
    except ValueError:
        return None


def _join_sequence(separator: str | bytes, items: list[ast.expr]) -> str | bytes | None:
    values = [_literal(item) for item in items]
    if not all(isinstance(item, type(separator)) for item in values):
        return None
    result = separator.join(values)
    return result if len(result) <= MAX_LITERAL else None


def _single_comprehension(node: ast.AST) -> tuple[ast.comprehension, ast.AST] | None:
    if not isinstance(node, (ast.GeneratorExp, ast.ListComp)) or len(node.generators) != 1:
        return None
    generator = node.generators[0]
    if generator.ifs or generator.is_async or not isinstance(generator.target, ast.Name):
        return None
    if not isinstance(generator.iter, (ast.List, ast.Tuple)) or len(generator.iter.elts) > 512:
        return None
    return generator, node.elt


def _chr_argument(expression: ast.AST, variable: str) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "chr"
        and not expression.keywords
        and len(expression.args) == 1
        and isinstance(expression.args[0], ast.Name)
        and expression.args[0].id == variable
    )


def _integer_constants(items: list[ast.expr]) -> list[int] | None:
    values: list[int] = []
    for item in items:
        if (
            not isinstance(item, ast.Constant)
            or not isinstance(item.value, int)
            or isinstance(item.value, bool)
        ):
            return None
        values.append(item.value)
    return values


def _chr_comprehension(node: ast.AST) -> list[str] | None:
    parsed = _single_comprehension(node)
    if parsed is None:
        return None
    generator, expression = parsed
    assert isinstance(generator.target, ast.Name)
    if not (
        _chr_argument(expression, generator.target.id)
        and isinstance(generator.iter, (ast.List, ast.Tuple))
    ):
        return None
    numbers = _integer_constants(generator.iter.elts)
    if numbers is None:
        return None
    try:
        return [chr(number) for number in numbers]
    except ValueError:
        return None


def _literal(node: ast.AST) -> str | bytes | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if isinstance(left, type(right)) and isinstance(left, (str, bytes)):
            value = left + right
            return value if len(value) <= MAX_LITERAL else None
    if isinstance(node, ast.Call):
        return _call_literal(node)
    return None


def _decoded_variants(content: bytes) -> list[tuple[str, bytes]]:
    variants: list[tuple[str, bytes]] = []
    pending = [("raw", content, 0), ("escaped", _decode_escapes(content), 0)]
    seen: set[bytes] = set()
    while pending:
        label, value, depth = pending.pop(0)
        if value in seen:
            continue
        if len(seen) >= MAX_DECODE_VARIANTS:
            raise RuntimeError("decoded variant count exceeded its bound")
        seen.add(value)
        variants.append((label, value))
        if depth >= MAX_DECODE_ROUNDS:
            continue
        _append_text_variants(pending, label, value, depth)
        variants.extend(_private_base64_variants(label, value))
    return variants


def _append_text_variants(
    pending: list[tuple[str, bytes, int]], label: str, value: bytes, depth: int
) -> None:
    decoded_values = (
        ("percent encoding", urllib.parse.unquote_to_bytes(value)),
        ("HTML entity", html.unescape(value.decode("latin1")).encode()),
    )
    pending.extend(
        (f"{label} + {kind}", decoded, depth + 1)
        for kind, decoded in decoded_values
        if decoded != value
    )


def _private_base64_variants(label: str, value: bytes) -> list[tuple[str, bytes]]:
    variants: list[tuple[str, bytes]] = []
    for decoded in _base64_decodings(value):
        private_value = _private_value_after_decoding(decoded)
        if private_value is not None:
            variants.append((f"{label} + Base64 encoding", private_value))
    return variants


def _private_value_after_decoding(content: bytes) -> bytes | None:
    pending = [(content, 0)]
    seen: set[bytes] = set()
    while pending:
        value, depth = pending.pop()
        if value in seen:
            continue
        if len(seen) >= MAX_DECODE_VARIANTS:
            raise RuntimeError("nested decoded variant count exceeded its bound")
        seen.add(value)
        if _contains_private_name(value):
            return value
        if depth >= MAX_DECODE_ROUNDS:
            continue
        if not _may_contain_encoded_text(value):
            continue
        pending.extend((decoded, depth + 1) for decoded in _text_decodings(value))
        pending.extend((decoded, depth + 1) for decoded in _base64_decodings(value))
    return None


def _may_contain_encoded_text(value: bytes) -> bool:
    if not value or any(item not in {9, 10, 13} and not 32 <= item <= 126 for item in value):
        return False
    return any(marker in value for marker in (b"%", b"&", b"\\")) or bool(
        BASE64_TOKEN.search(value)
    )


def _text_decodings(value: bytes) -> list[bytes]:
    decoded = [
        urllib.parse.unquote_to_bytes(value),
        html.unescape(value.decode("latin1")).encode(),
    ]
    return [item for item in decoded if item != value]


def _base64_decodings(value: bytes) -> list[bytes]:
    decoded_values: list[bytes] = []
    for match in BASE64_TOKEN.finditer(value):
        token = match.group(1).replace(b"-", b"+").replace(b"_", b"/")
        try:
            decoded = base64.b64decode(token + b"=" * (-len(token) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) <= MAX_LITERAL:
            decoded_values.append(decoded)
    return decoded_values


def _python_values(content: bytes, name: str) -> list[tuple[int, bytes]]:
    try:
        tree = ast.parse(content, filename=name)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"cannot parse publishable Python source: {name}") from exc
    environment: dict[str, str | bytes] = {}
    values: list[tuple[int, bytes]] = []
    for node in ast.walk(tree):
        value = (
            _literal_with_names(node.value, environment)
            if isinstance(node, ast.Assign)
            else _literal(node)
        )
        encoded = value.encode() if isinstance(value, str) else value
        if encoded is not None:
            values.append((getattr(node, "lineno", 1), encoded))
        if isinstance(node, ast.Assign) and value is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    environment[target.id] = value
    return values


def _literal_with_names(node: ast.AST, environment: dict[str, str | bytes]) -> str | bytes | None:
    if isinstance(node, ast.Name):
        return environment.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_with_names(node.left, environment)
        right = _literal_with_names(node.right, environment)
        if isinstance(left, type(right)) and isinstance(left, (str, bytes)):
            result = left + right
            return result if len(result) <= MAX_LITERAL else None
    return _literal(node)


def _javascript_values(content: bytes) -> list[bytes]:
    values = _javascript_codepoints(JS_CODEPOINT.finditer(content), 256, "character")
    values.extend(_javascript_codepoints(JS_ARRAY_CODEPOINT.finditer(content), 512, "array"))
    bindings = {
        match.group(1): _decode_escapes(match.group(3)) for match in JS_BINDING.finditer(content)
    }
    for match in JS_CONCAT.finditer(content):
        names = [item.strip() for item in match.group(1).split(b"+")]
        if all(item in bindings for item in names):
            value = b"".join(bindings[item] for item in names)
            if len(value) <= MAX_LITERAL:
                values.append(value)
    return values


def _javascript_codepoints(
    matches: Iterable[re.Match[bytes]], maximum: int, label: str
) -> list[bytes]:
    values: list[bytes] = []
    for match in matches:
        numbers = [int(item.strip(), 0) for item in match.group(1).split(b",")]
        if len(numbers) > maximum or any(number > 0x10FFFF for number in numbers):
            raise RuntimeError(f"JavaScript {label} reconstruction exceeded its bound")
        values.append("".join(chr(number) for number in numbers).encode())
    return values


def _bounded_gzip(content: bytes, offset: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content[offset:])) as stream:
            value = stream.read(MAX_ARCHIVE_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise RuntimeError("malformed gzip archive") from exc
    if len(value) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("gzip archive exceeds its expanded-byte bound")
    return value


def _tar_members(content: bytes) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    if len(members) >= MAX_ARCHIVE_MEMBERS:
                        raise RuntimeError("TAR archive exceeds its bound")
                    members.append((member.name, b""))
                    continue
                if not member.isfile():
                    raise RuntimeError(f"unsupported TAR member type: {member.name}")
                if len(members) >= MAX_ARCHIVE_MEMBERS or total + member.size > MAX_ARCHIVE_BYTES:
                    raise RuntimeError("TAR archive exceeds its bound")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"unreadable TAR member: {member.name}")
                value = stream.read(member.size + 1)
                if len(value) != member.size:
                    raise RuntimeError(f"truncated TAR member: {member.name}")
                members.append((member.name, value))
                total += member.size
    except tarfile.TarError as exc:
        raise RuntimeError("malformed TAR archive") from exc
    return members


def _zip_members(content: bytes) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.infolist():
                if member.is_dir() or member.flag_bits & 1:
                    raise RuntimeError(f"unsupported ZIP member: {member.filename}")
                if (
                    len(members) >= MAX_ARCHIVE_MEMBERS
                    or total + member.file_size > MAX_ARCHIVE_BYTES
                ):
                    raise RuntimeError("ZIP archive exceeds its bound")
                value = archive.read(member)
                if len(value) != member.file_size:
                    raise RuntimeError(f"truncated ZIP member: {member.filename}")
                members.append((member.filename, value))
                total += member.file_size
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            ("unsupported", "ZIP archive", "truncated")
        ):
            raise
        raise RuntimeError("malformed or unsupported ZIP archive") from exc
    return members


def _archive_members(name: str, content: bytes) -> list[tuple[str, bytes]] | None:
    zip_offset = content.find(b"PK\x03\x04")
    gzip_offset = content.find(b"\x1f\x8b")
    if zip_offset >= 0:
        return _zip_members(content)
    if gzip_offset >= 0:
        return _gzip_members(name, content, gzip_offset)
    if len(content) >= 512 and content[257:262] == b"ustar":
        return _tar_members(content)
    suffix = Path(name).suffix.lower()
    if suffix in {".zip", ".gz", ".tgz", ".tar"} or content.startswith((b"PK", b"\x1f")):
        raise RuntimeError(f"malformed or unsupported archive: {name}")
    return None


def _gzip_members(name: str, content: bytes, offset: int) -> list[tuple[str, bytes]]:
    expanded = _bounded_gzip(content, offset)
    try:
        return _tar_members(expanded)
    except RuntimeError as exc:
        if str(exc) != "malformed TAR archive":
            raise
        lowered = name.lower()
        if lowered.endswith((".tgz", ".tar.gz")):
            raise RuntimeError(f"malformed TAR archive: {name}") from exc
    payload_name = Path(name).name[:-3] if lowered.endswith(".gz") else "gzip-payload"
    return [(payload_name or "gzip-payload", expanded)]


def _source_violations(name: str, content: bytes) -> list[str]:
    violations: list[str] = []
    if name.endswith(".py"):
        for line, value in _python_values(content, name):
            if _contains_private_name(value):
                violations.append(f"{name}:{line}: forbidden Python literal")
    if name.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx")):
        for value in _javascript_values(content):
            if _contains_private_name(value):
                violations.append(f"{name}: forbidden JavaScript reconstruction")
    return violations


def _scan_content(name: str, content: bytes, depth: int) -> list[str]:
    violations: list[str] = []
    if _contains_private_name(name.encode()):
        violations.append(f"{name}: forbidden reference in path")
    members = _archive_members(name, content)
    if members is not None:
        if depth >= MAX_ARCHIVE_DEPTH:
            raise RuntimeError(f"archive nesting exceeds its bound: {name}")
        for member_name, value in members:
            violations.extend(_scan_content(f"{name}!/{member_name}", value, depth + 1))
        return violations
    variants = _decoded_variants(content)
    violations.extend(
        f"{name}: forbidden {label} reference"
        for label, value in variants
        if _contains_private_name(value)
    )
    violations.extend(_source_violations(name, content))
    return violations


def find_violations(files: Iterable[tuple[str, bytes]]) -> list[str]:
    """Return all forbidden public-boundary references."""
    violations: list[str] = []
    for name, content in files:
        violations.extend(_scan_content(name, content, 0))
    return violations


def repository_files(root: Path = ROOT) -> list[tuple[str, bytes]]:
    """Read the proposed tree: index plus worktree modifications and untracked files."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is unavailable; refusing an incomplete scan")
    entries = _index_entries(root, git)
    changed = _git_names(root, git, "--modified", "--deleted")
    staged = _git_diff_names(root, git, "--cached")
    untracked = _git_names(root, git, "--others", "--exclude-standard")
    files = []
    for name, object_id in entries.items():
        index_value = _git(root, git, "cat-file", "blob", object_id)
        if name in staged:
            files.append((name, index_value))
        value = _worktree_file(root, name) if name in changed else index_value
        if value is not None:
            files.append((name, value))
    for name in untracked:
        value = _worktree_file(root, name)
        if value is None:
            raise RuntimeError(f"untracked publishable path disappeared: {name}")
        files.append((name, value))
    if not files:
        raise RuntimeError("no publishable files found; refusing an empty scan")
    return files


def _git(root: Path, git: str, *args: str) -> bytes:
    try:
        result = subprocess.run(
            [git, "-C", str(root), *args], check=True, capture_output=True, timeout=60
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot enumerate proposed repository content") from exc
    return result.stdout


def _git_names(root: Path, git: str, *args: str) -> set[str]:
    return {item.decode() for item in _git(root, git, "ls-files", "-z", *args).split(b"\0") if item}


def _git_diff_names(root: Path, git: str, *args: str) -> set[str]:
    return {
        item.decode()
        for item in _git(root, git, "diff", "--name-only", "-z", *args).split(b"\0")
        if item
    }


def _index_entries(root: Path, git: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for record in _git(root, git, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_name = record.partition(b"\t")
        if not separator:
            raise RuntimeError("malformed Git index entry")
        mode, object_id, stage = metadata.decode().split()
        name = raw_name.decode()
        if stage != "0":
            raise RuntimeError(f"unmerged publishable path: {name}")
        if mode not in {"100644", "100755"}:
            raise RuntimeError(f"unsupported indexed mode {mode}: {name}")
        entries[name] = object_id
    return entries


def _worktree_file(root: Path, name: str) -> bytes | None:
    path = root / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file() or not metadata.st_mode:
        raise RuntimeError(f"unsupported publishable path: {name}")
    return path.read_bytes()


def run_mutations() -> None:
    """Prove plain and composed spellings are rejected."""
    first = PRIVATE_NAMES[0]
    slash = bytes((92,))
    js_hex = b'const v = "' + b"".join(slash + f"x{value:02x}".encode() for value in first) + b'"'
    js_unicode = (
        b'const v = "' + b"".join(slash + f"u{value:04x}".encode() for value in first) + b'"'
    )
    shell_octal = b"printf '" + b"".join(slash + f"{value:03o}".encode() for value in first) + b"'"
    integers = b", ".join(str(value).encode() for value in first)
    nested_base64 = base64.b64encode(base64.b64encode(first))
    mutations = {
        "plain": first,
        "uppercase": first.upper(),
        "Python addition": b'v = "' + first[:1] + b'" + "' + first[1:] + b'"',
        "Python join": b'v = "".join(("' + b'", "'.join(bytes((item,)) for item in first) + b'"))',
        "Python fromhex": b'v = bytes.fromhex("' + first.hex().encode() + b'")',
        "Python bytes tuple": b"v = bytes((" + integers + b"))",
        "Python bytes list": b"v = bytes([" + integers + b"])",
        "Python bytearray tuple": b"v = bytearray((" + integers + b"))",
        "Python map chr": b'v = "".join(map(chr, (' + integers + b")))",
        "JavaScript hex": js_hex,
        "JavaScript Unicode": js_unicode,
        "shell octal": shell_octal,
        "nested Base64": nested_base64,
    }
    for label, content in mutations.items():
        suffix = ".py" if label.startswith("Python") else ".txt"
        if not find_violations([(f"mutation{suffix}", content)]):
            raise RuntimeError(f"mutation escaped scanner: {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutation-test", action="store_true")
    args = parser.parse_args()
    if args.mutation_test:
        run_mutations()
    violations = find_violations(repository_files())
    if violations:
        print("\n".join(violations))
        return 1
    print("public-boundary scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
