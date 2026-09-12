"""Deterministic bilingual registration documentation from validated records."""

from .schema import Inventory


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
    title = (
        "Index | Protocol | Methods | Effective path | Handler | Generated identity"
        if language == "en"
        else "Indice | Protocolo | Metodos | Caminho efetivo | Funcao | Identidade gerada"
    )
    lines.extend(["", "| " + title + " |", "|---|---|---|---|---|---|"])
    for registration in inventory.modes[0].registrations:
        row = inventory.operations[registration.operation]
        generated = row.generated
        identity = (
            f"{generated.version}:{generated.operation_id}:alias={generated.alias}"
            if generated
            else "-"
        )
        handler = f"{row.handler.source.path}:{row.handler.line} {row.handler.qualname}"
        values = [
            registration.index,
            row.protocol,
            ",".join(row.methods),
            row.path,
            handler,
            identity,
        ]
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return ("\n".join(lines) + "\n").encode("utf-8")
