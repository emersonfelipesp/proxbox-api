"""Deterministic bilingual registration documentation from validated records."""

from .schema import Inventory, Operation, Registration


def cell(value: object) -> str:
    """Escape data as table text, never as executable Markdown or HTML."""
    text = str(value)
    for old, new in (
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("|", "&#124;"),
        ("`", "&#96;"),
        ("[", "&#91;"),
        ("]", "&#93;"),
        ("*", "&#42;"),
        ("_", "&#95;"),
        ("\\", "&#92;"),
        ("\n", " "),
        ("\r", " "),
    ):
        text = text.replace(old, new)
    return text


def _generated_identity(row: Operation) -> str:
    generated = row.generated
    if generated is None:
        return "-"
    return f"{generated.version}:{generated.operation_id}:alias={generated.alias}"


def _detail_lines(inventory: Inventory, registrations: list[Registration]) -> list[str]:
    lines: list[str] = []
    for registration in registrations:
        row = inventory.operations[registration.operation]
        handler = f"{row.handler.source.path}:{row.handler.line} {row.handler.qualname}"
        values = [
            registration.index,
            row.protocol,
            ",".join(row.methods),
            row.path,
            handler,
            _generated_identity(row),
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return lines


def _opt_in_only_registrations(inventory: Inventory) -> list[Registration]:
    default_operations = {row.operation for row in inventory.modes[0].registrations}
    return [
        row
        for row in inventory.runtime_codegen_opt_in.registrations
        if row.operation not in default_operations
    ]


def render(inventory: Inventory, language: str) -> bytes:
    """Render every default registration and all mode order/digest identities."""
    if language not in {"en", "pt-BR"}:
        raise ValueError("Unsupported documentation language")
    headers = (
        "Mode | Core | Sidecars | Registrations"
        if language == "en"
        else "Modo | Principal | Componentes opcionais | Registros"
    )
    lines = [
        "<!-- Generated from the validated registration inventory. Do not edit. -->",
        "",
        "| " + headers + " |",
        "|---|---|---|---|",
    ]
    for mode in inventory.modes:
        lines.append(
            f"| {cell(mode.name)} | {mode.core} | {cell(', '.join(mode.sidecars))} "
            f"| {len(mode.registrations)} |"
        )
    default_title = (
        "Default registration detail (`PROXBOX_RUNTIME_CODEGEN_ENABLED=false`)"
        if language == "en"
        else "Detalhes dos registros padrao (`PROXBOX_RUNTIME_CODEGEN_ENABLED=false`)"
    )
    title = (
        "Index | Protocol | Methods | Effective path | Handler | Generated identity"
        if language == "en"
        else "Indice | Protocolo | Metodos | Caminho efetivo | Funcao | Identidade gerada"
    )
    lines.extend(["", f"### {default_title}", "", "| " + title + " |", "|---|---|---|---|---|---|"])
    lines.extend(_detail_lines(inventory, inventory.modes[0].registrations))
    opt_in_only = _opt_in_only_registrations(inventory)
    opt_in_title = (
        "Runtime codegen opt-in additions (`PROXBOX_RUNTIME_CODEGEN_ENABLED=true`)"
        if language == "en"
        else "Adicoes do opt-in de codegen em runtime (`PROXBOX_RUNTIME_CODEGEN_ENABLED=true`)"
    )
    lines.extend(
        [
            "",
            f"### {opt_in_title}",
            "",
            (
                f"The complete opt-in application has {len(inventory.runtime_codegen_opt_in.registrations)} registrations; "
                f"the table below lists its {len(opt_in_only)} non-default operation identities."
                if language == "en"
                else f"A aplicacao completa com opt-in possui {len(inventory.runtime_codegen_opt_in.registrations)} registros; "
                f"a tabela abaixo lista suas {len(opt_in_only)} identidades de operacao que nao pertencem ao padrao."
            ),
            "",
            "| " + title + " |",
            "|---|---|---|---|---|---|",
        ]
    )
    lines.extend(_detail_lines(inventory, opt_in_only))
    return ("\n".join(lines) + "\n").encode("utf-8")
