"""Guard: granian must be installed from one exact, deliberately bumped pin.

Invariant: every place that installs granian in a container or CI file uses
the same single ``==X.Y.Z`` version (or a full ``${GRANIAN_VERSION}``
reference whose ``ARG`` default is that same version).

Scope: this covers container and CI files only (every ``Dockerfile*``,
``docker/``, GitHub and Gitea workflows, and ``requirements*.txt`` /
``constraints*.txt``). The ``pyproject.toml`` dependency range and
``uv.lock`` are intentionally out of scope.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox"}
VAR_RE = re.compile(r"^\$\{GRANIAN_VERSION\}$|^\$GRANIAN_VERSION$")
INSTALL_CMD_RE = re.compile(
    r"(?:\bpip3?|\bpython3?\s+-m\s+pip|\buv\s+pip|\bpipx)\s+install\b"
    r"|\buv\s+add\b|\buv\s+tool\s+install\b"
)
# Any token whose project name is granian (extras allowed), however it continues:
# ``granian``, ``granian==1``, ``granian[tls]``, ``granian@https://...``.
GRANIAN_TOKEN_RE = re.compile(r"^granian(?:\[[^\]]*\])?(?![A-Za-z0-9._-])", re.IGNORECASE)
# A direct artifact path or URL for granian, e.g. ``./granian-2.8.4.whl``.
GRANIAN_PATH_RE = re.compile(r"(?:^|/)granian[^/]*\.(?:whl|tar\.gz|zip)$", re.IGNORECASE)
SPEC_CHARS = "=<>~!,@"
REQUIRED_STAGES = ("granian", "granian-pyo3-rust")
CI_WORKFLOW = Path(".github/workflows/ci.yml")
ARG_RE = re.compile(r"^\s*ARG\s+GRANIAN_VERSION=(\S+)\s*$", re.MULTILINE)
EXACT_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _build_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            parts = rel.parts
            is_workflow = parts in {(".github", "workflows"), (".gitea", "workflows")}
            if (
                name.startswith("Dockerfile")
                or (parts[:1] == ("docker",))
                or (is_workflow and name.endswith((".yml", ".yaml")))
                or (parts[:2] == (".gitea", "workflows"))
                or re.fullmatch(r"(requirements|constraints)[^/]*\.txt", name)
            ):
                files.append(Path(dirpath) / name)
    return files


def _tokens(text: str) -> list[str]:
    return [t.rstrip(";&|\\") for t in re.split(r"""[\s'"]+""", text) if t.rstrip(";&|\\")]


def _specs_in_tokens(tokens: list[str]) -> list[str]:
    found: list[str] = []
    for i, tok in enumerate(tokens):
        if GRANIAN_PATH_RE.search(tok):
            found.append(tok)
            continue
        if not GRANIAN_TOKEN_RE.match(tok):
            continue
        spec, j = tok, i
        while j + 1 < len(tokens) and (tokens[j + 1][0] in SPEC_CHARS or spec[-1] in SPEC_CHARS):
            j += 1
            spec += tokens[j]
        found.append(spec)
    return found


def _line_specs(line: str) -> list[str]:
    m = INSTALL_CMD_RE.search(line)
    return _specs_in_tokens(_tokens(line[m.start() :])) if m else []


def _run_scalars(node: object) -> list[str]:
    """Collect every ``run`` string scalar (already YAML-folded) in a workflow."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_run_scalars(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_run_scalars(item))
    return out


def _workflow_specs(text: str, rel: str) -> list[str]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # an unparsable workflow must not be skipped
        raise AssertionError(f"{rel}: workflow YAML cannot be parsed: {exc}") from exc
    specs: list[str] = []
    for script in _run_scalars(doc):
        for line in script.replace("\\\n", " ").splitlines():
            specs.extend(_line_specs(line))
    return specs


def _dockerfile_stage_specs(text: str) -> dict[str, list[str]]:
    """Resolve ``$GRANIAN_VERSION`` installs using Docker's real ARG scoping.

    A global ARG (before the first FROM) is only visible inside a stage after the
    stage redeclares it with a bare ``ARG GRANIAN_VERSION``. A stage-local
    ``ARG GRANIAN_VERSION=X`` applies from that line onward in that stage only.
    Anything that stays unresolved is left as a literal and fails requirement
    parsing in ``check``.
    """
    global_default: str | None = None
    stage_value: str | None = None
    in_stage = False
    stage = ""
    stages: dict[str, list[str]] = {"": []}
    for line in text.replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if re.match(r"FROM\b", stripped, re.IGNORECASE):
            in_stage, stage_value = True, None
            alias = re.search(r"\sAS\s+(\S+)\s*$", stripped, re.IGNORECASE)
            stage = alias.group(1).lower() if alias else f"#{len(stages)}"
            stages.setdefault(stage, [])
            continue
        arg = re.match(r"ARG\s+GRANIAN_VERSION(?:=(\S+))?\s*$", stripped, re.IGNORECASE)
        if arg:
            if not in_stage:
                global_default = arg.group(1)
            elif arg.group(1) is not None:
                stage_value = arg.group(1)
            else:
                stage_value = global_default
            continue
        for spec in _line_specs(line):
            name, sep, rest = spec.partition("==")
            if sep and VAR_RE.match(rest.strip()) and stage_value is not None:
                spec = f"{name}=={stage_value}"
            stages[stage].append(spec)
    return stages


def _dockerfile_specs(text: str) -> list[str]:
    return [s for specs in _dockerfile_stage_specs(text).values() for s in specs]


def install_specs(root: Path) -> list[tuple[str, str]]:
    """Return (relative path, requirement string) for every granian install."""
    found: list[tuple[str, str]] = []
    for path in _build_files(root):
        rel = str(path.relative_to(root))
        raw = path.read_text(encoding="utf-8", errors="ignore")
        is_req = bool(re.fullmatch(r"(requirements|constraints)[^/]*\.txt", path.name))
        is_workflow = path.suffix in {".yml", ".yaml"} and path.parent.relative_to(root).parts in {
            (".github", "workflows"),
            (".gitea", "workflows"),
        }
        if is_workflow:
            specs = _workflow_specs(raw, rel)
        elif path.name.startswith("Dockerfile"):
            specs = _dockerfile_specs(raw)
        elif is_req:
            specs = []
            for line in raw.replace("\\\n", " ").splitlines():
                body = line.split("#", 1)[0].strip()
                specs.extend(_specs_in_tokens(body.split()) if body else [])
        else:
            specs = [s for line in raw.replace("\\\n", " ").splitlines() for s in _line_specs(line)]
        found.extend((rel, s) for s in specs)
    return found


def arg_versions(root: Path) -> list[tuple[str, str]]:
    return [
        (str(p.relative_to(root)), v)
        for p in _build_files(root)
        for v in ARG_RE.findall(p.read_text(encoding="utf-8", errors="ignore"))
    ]


def check(root: Path) -> list[str]:
    """Return a list of violations; empty means the invariant holds."""
    problems: list[str] = []
    versions: set[str] = set()
    for rel, spec in install_specs(root):
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            problems.append(f"{rel}: unparseable requirement {spec!r}")
            continue
        if req.url is not None or "@" in spec or "://" in spec:
            problems.append(f"{rel}: direct reference {spec!r} bypasses the pin")
            continue
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or not EXACT_RE.match(specs[0].version):
            problems.append(f"{rel}: {spec!r} must have exactly one ==X.Y.Z specifier")
            continue
        versions.add(specs[0].version)
    for rel, value in arg_versions(root):
        if EXACT_RE.match(value):
            versions.add(value)
        else:
            problems.append(f"{rel}: ARG GRANIAN_VERSION={value!r} is not X.Y.Z")
    if len(versions) > 1:
        problems.append(f"granian pin differs across files: {sorted(versions)}")
    return problems


def check_required(root: Path) -> list[str]:
    """Each Granian image stage and the CI e2e install must install granian once."""
    problems: list[str] = []
    dockerfile = root / "Dockerfile"
    stages = _dockerfile_stage_specs(dockerfile.read_text(encoding="utf-8"))
    for stage in REQUIRED_STAGES:
        specs = stages.get(stage, [])
        if len(specs) != 1 or not re.fullmatch(r"granian(?:\[[^\]]*\])?==\d+\.\d+\.\d+", specs[0]):
            problems.append(
                f"Dockerfile stage {stage!r} needs exactly one pinned granian install: {specs}"
            )
    ci = root / CI_WORKFLOW
    if not _workflow_specs(ci.read_text(encoding="utf-8"), str(CI_WORKFLOW)):
        problems.append(f"{CI_WORKFLOW}: no granian install site")
    return problems


def test_granian_install_sites_exist() -> None:
    assert install_specs(ROOT), "expected granian install sites in image/CI files"
    assert check_required(ROOT) == []


def test_granian_is_pinned_identically_everywhere() -> None:
    assert not check(ROOT)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    base = {"Dockerfile": "ARG GRANIAN_VERSION=2.8.4\nRUN uv pip install granian==2.8.4\n"}
    for rel, body in {**base, **files}.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tmp_path


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"Dockerfile.x": 'RUN uv pip install "granian[tls]==2.8.4"\n'})
    assert check(root) == []


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        ("Dockerfile.foo", "RUN pip3 install granian>=2.0\n"),
        ("Dockerfile.foo", "RUN pip install granian\n"),
        ("Dockerfile.foo", "RUN python3 -m pip install granian\n"),
        ("Dockerfile.foo", "RUN pip install granian[tls]~=2.8\n"),
        ("Dockerfile.foo", "RUN pip install granian == 2.7.0\n"),
        ("Dockerfile.foo", "RUN uv pip install granian==2.8.4 granian==2.7.0\n"),
        ("Dockerfile.foo", 'RUN uv pip install "granian==${GRANIAN_VERSION}.post1"\n'),
        ("Dockerfile.foo", "RUN uv tool install granian\n"),
        ("Dockerfile.foo", "RUN pipx install granian>=2\n"),
        ("Dockerfile.foo", "RUN uv add granian\n"),
        (".github/workflows/x.yaml", "run: pip3 install granian>=2.8\n"),
        (".gitea/workflows/x.yml", "run: pip install granian\n"),
        ("docker/entry.sh", "pip install granian\n"),
        ("requirements-dev.txt", "granian>=2.0\n"),
        ("sub/constraints.txt", "granian[tls]\n"),
        ("Dockerfile.foo", "RUN uv pip install granian==2.7.0\n"),
        ("Dockerfile.foo", "ARG GRANIAN_VERSION=2.7.0\n"),
        (".github/workflows/x.yml", "run: >\n  uv pip install\n  granian\n"),
        (".github/workflows/x.yml", "run: >-\n  uv pip install\n  granian>=2\n"),
        (".gitea/workflows/x.yml", "run: |\n  uv pip install \\\n    granian\n"),
        (
            "Dockerfile.foo",
            "FROM a AS s\nRUN uv pip install granian==${GRANIAN_VERSION}\n",
        ),
        (
            "Dockerfile.foo",
            "ARG GRANIAN_VERSION=2.8.4\nFROM a AS s\nRUN uv pip install granian==${GRANIAN_VERSION}\n",
        ),
    ],
)
def test_bypass_forms_are_rejected(tmp_path: Path, rel: str, body: str) -> None:
    assert check(_tree(tmp_path, {rel: body}))


def test_pin_with_extras_and_marker_is_accepted(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"Dockerfile.foo": "RUN pip3 install granian[tls]==2.8.4\n"})
    assert check(root) == []


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("Dockerfile", "granian==${GRANIAN_VERSION}", "granian"),
        ("Dockerfile", "granian==${GRANIAN_VERSION}", "granian>=2"),
        ("Dockerfile", "ARG GRANIAN_VERSION=2.8.4", "ARG GRANIAN_VERSION=2.7.0"),
        (".github/workflows/ci.yml", "granian==2.8.4", "granian"),
        (".github/workflows/ci.yml", "granian==2.8.4", "granian==2.7.0"),
    ],
)
def test_mutating_real_files_is_detected(tmp_path: Path, rel: str, old: str, new: str) -> None:
    for path in _build_files(ROOT):
        target = tmp_path / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    victim = tmp_path / rel
    text = victim.read_text(encoding="utf-8")
    assert old in text
    victim.write_text(text.replace(old, new), encoding="utf-8")
    assert check(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "RUN pip install granian@https://example.invalid/granian.whl\n",
        "RUN pip install granian @ file:///x.whl\n",
        "RUN pip install granian@git+https://example.invalid/g.git\n",
        "RUN pip install granian[tls]@https://example.invalid/granian.whl\n",
        "RUN pip install GRANIAN @ https://example.invalid/g.whl\n",
        "RUN pip install ./granian-2.8.4-cp312-linux_x86_64.whl\n",
        "RUN pip install granian-2.8.4.tar.gz\n",
    ],
)
def test_direct_references_are_rejected(tmp_path: Path, body: str) -> None:
    assert check(_tree(tmp_path, {"Dockerfile.foo": body}))


def _copy_real(tmp_path: Path) -> None:
    for path in _build_files(ROOT):
        target = tmp_path / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_removing_a_stage_install_is_detected(tmp_path: Path, stage: str) -> None:
    _copy_real(tmp_path)
    victim = tmp_path / "Dockerfile"
    text = victim.read_text(encoding="utf-8")
    pattern = re.compile(r'  && uv pip install [^\n]*"granian==\$\{GRANIAN_VERSION\}" \\\n')
    start = text.index(f"AS {stage}\n")
    head, tail = text[:start], text[start:]
    tail, count = pattern.subn("", tail, count=1)
    assert count == 1
    victim.write_text(head + tail, encoding="utf-8")
    assert check_required(tmp_path)


def test_removing_the_ci_install_is_detected(tmp_path: Path) -> None:
    _copy_real(tmp_path)
    victim = tmp_path / CI_WORKFLOW
    text = victim.read_text(encoding="utf-8")
    assert " granian==2.8.4 " in text
    victim.write_text(text.replace(" granian==2.8.4 ", " "), encoding="utf-8")
    assert check_required(tmp_path)


def test_stage_with_arg_removed_is_detected(tmp_path: Path) -> None:
    for path in _build_files(ROOT):
        target = tmp_path / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    victim = tmp_path / "Dockerfile"
    text = victim.read_text(encoding="utf-8")
    head, sep, tail = text.rpartition("FROM runtime-base-pyo3-rust AS granian-pyo3-rust")
    assert sep
    victim.write_text(head + sep + tail.replace("ARG GRANIAN_VERSION=2.8.4\n", "", 1))
    assert check(tmp_path)


def test_global_arg_redeclared_in_stage_is_accepted(tmp_path: Path) -> None:
    body = "ARG GRANIAN_VERSION=2.8.4\nFROM a AS s\nARG GRANIAN_VERSION\nRUN pip install granian==$GRANIAN_VERSION\n"
    assert check(_tree(tmp_path, {"Dockerfile.foo": body})) == []
