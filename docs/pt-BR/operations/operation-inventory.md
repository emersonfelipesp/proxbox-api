# Inventario de operacoes registradas

O inventario mantido registra rotas, mas nao concede permissao para executa-las.
Ele constroi a aplicacao real e registra explicitamente as rotas geradas em um
processo filho isolado e offline. Nao inicia o lifespan, nao inicializa o banco,
nao invoca rotas ou dependencias e nao acessa sistemas gerenciados. O processo
nao herda credenciais, desabilita explicitamente o dotenv e rejeita sockets e
gravacoes fora de seu diretorio temporario. Essa protecao de auditoria Python
serve ao desenvolvimento; ela nao
representa um isolamento de chamadas de sistema em producao.

## Geracao e verificacao

Use o lock original do repositorio e o ambiente Python suportado:

```bash
uv sync --locked --extra test --extra docs --extra pbs --extra pdm
uv run python scripts/mounted_operation_inventory.py generate
uv run python scripts/mounted_operation_inventory.py verify
uv run python scripts/mounted_operation_inventory.py readiness
uv run mkdocs build --strict
```

A geracao publica JSON deterministico, esquemas e tabelas bilingues em
`contracts/`. Um documento de cobertura explicitamente pendente e criado apenas
quando ainda nao existe; disposicoes existentes nunca sao sobrescritas.
Apos regenerar, reconcilie a cobertura com o novo digest do inventario.
A verificacao coleta novamente os registros reais e falha diante de divergencias
ou entradas que nao pode avaliar. Pacotes opcionais ausentes e falhas nos
esquemas gerados sao erros, nunca inventarios menores considerados completos.

A prontidao e uma verificacao separada da completude dos registros. O codigo de
saida `2` indica colunas obrigatorias ainda pendentes. Um registro mecanicamente
completo nao concede autorizacao, revisao independente nem aprovacao para
ativacao em producao. A geracao e a documentacao podem passar enquanto a
prontidao continua corretamente bloqueada.

## Escopo e limitacoes

A matriz fixa contem vinte e duas entradas e quinze estados alcancaveis: sete
subconjuntos opcionais nao vazios sem o nucleo e oito subconjuntos com o nucleo.
O modo padrao e o modo explicito com todos os componentes sao equivalentes.
O decimo sexto estado booleano, sem nucleo e sem componentes opcionais, nao pode
ser selecionado: uma entrada vazia seleciona tudo; somente tokens desconhecidos
selecionam o nucleo. Espacos, maiusculas e repeticoes possuem testes explicitos.
As rotas geradas aparecem em todos os modos, pois o lifespan real as registra
independentemente da selecao do nucleo.

O adaptador exige o par FastAPI/Starlette revisado. Atualizacoes exigem revisao e
os testes independentes de HTTP, WebSocket, montagens e colisoes. Cada ocorrencia
e sua ordem permanecem no inventario, mesmo quando a definicao da operacao e
identica. Aliases gerados preservam operacao, caminho/metodo original, versao do
esquema e digest. Os caminhos sao relativos ao repositorio ou a distribuicao;
nao incluem horarios, caminhos absolutos locais ou hashes autorreferentes de
commits completos.

Colisoes exatas de caminho/metodo podem ser identificadas, mas isso nao comprova
toda a precedencia semantica de parametros, caminhos abrangentes e montagens
opacas. Metodos HTTP nunca determinam efeitos. Persistencia local, mutacao no
NetBox, revelacao de material, leituras e escritas gerenciadas e criacao/consumo
de capacidades interativas sao categorias distintas. Dependencias antecipadas
podem produzir efeitos antes da autenticacao do handler; tickets existentes
podem sobreviver a uma mudanca aplicada apenas ao criador. Ambos exigem rastreio.

A matriz completa de chamadores e efeitos permanece incompleta. Cada operacao
exige evidencias do chamador e handler final, alvo e procedimento/versao,
transporte, esquemas de entrada e resultado, finalidade/campos das credenciais,
controles de endpoint/transporte, permissoes e aprovacao, prazo,
idempotencia/reconciliacao, prova terminal da tarefa, auditoria e testes.
Colunas desconhecidas permanecem pendentes. Este recurso nao altera flags de
execucao, controles de escrita, autenticacao, politica SSH ou implantacao.

## Tabela de registros

O resumo cobre todas as entradas. A tabela detalhada preserva cada registro do
modo padrao; o artefato JSON preserva a sequencia completa de cada modo.
A compilacao verifica fontes e integridade antes de incorporar os trechos
locais gerados. Entradas ausentes, desatualizadas ou simbolicas causam falha.

--8<-- "mounted-operations.pt-BR.md"
