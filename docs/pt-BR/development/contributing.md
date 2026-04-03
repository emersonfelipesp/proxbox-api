# Contribuindo

## Fluxo recomendado

1. Crie branch a partir de `main`.
2. Implemente mudancas focadas.
3. Adicione/atualize testes.
4. Execute validacoes locais.
5. Abra PR com resumo claro.

## Checklist

- [ ] `pytest`
- [ ] `python -m compileall proxbox_api`
- [ ] `mkdocs build --strict` (quando docs mudarem)

## Expectativas de documentacao

- Atualize `docs/` quando o comportamento de endpoints mudar.
- Mantenha os docs em ingles como fonte principal.
- Atualize `docs/pt-BR/` para as paginas traduzidas relevantes.
- Documente mudancas de contrato gerado, integracoes de helpers/servicos e alteracoes de rotas que afetem os guias `CLAUDE.md`.
