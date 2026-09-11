# Limite Interativo Exclusivo de RPC

O padrão `PROXBOX_EXECUTION_MODE=rpc_only` recusa capacidades irrestritas de SSH
e console do Proxmox e os dois WebSockets legados de sincronização sem ator.
Esse limite tem escopo definido: não certifica que todas as operações do
backend já possuem uma implementação de RPC auditada.

## Configuração do processo

A composição da aplicação lê uma única vez estas opções exclusivas do operador,
sem consultar o NetBox, configurações em cache ou flags dos endpoints:

```dotenv
PROXBOX_EXECUTION_MODE=rpc_only
PROXBOX_EXECUTION_GENERATION=operator-selected-cutover-generation
```

Somente os valores exatos `rpc_only` e `legacy` são aceitos. Modo ausente
seleciona `rpc_only`; valor vazio, grafia incorreta, caixa diferente ou espaços
provocam erro de configuração. Uma geração informada deve conter de 1 a 128
letras ASCII, dígitos, pontos, sublinhados, dois-pontos ou hífens, começando
com letra ou dígito. Geração ausente permite iniciar o inventário, mas nunca
comprova prontidão para a transição. Não existe geração automática por worker,
fallback para configurações do plugin, endpoint de ativação dinâmica ou retorno
automático a `legacy`. `PROXBOX_FEATURES` é independente e não libera execução.

O operador pode selecionar `legacy` explicitamente para compatibilidade. Esse
modo preserva autenticação real, autorização de usuário e objeto nos serviços
companheiros, espaços de IDs, restrições de transporte SSH, chave do servidor
fixada e políticas persistidas de TLS e autenticação. Não é uma exceção de
aprovação RPC e nunca informa prontidão local exclusiva de RPC.

## Superfícies protegidas

| Superfície | Comportamento em RPC-only | Compatibilidade explícita |
|---|---|---|
| `POST /ssh/sessions` | HTTP 403 antes de criar ticket ou reter credenciais inline | Autenticação de serviço e ticket de uso único |
| `WS /ssh/sessions/{session_id}/ws` | Código 1008 antes de consumir tickets, inclusive antigos válidos, ou obter credenciais | Autenticação limitada por tempo antes do NetBox; credenciais de uso único não abrem esse provedor |
| `POST /proxmox/console/sessions` | HTTP 403 antes de resolver endpoint, descriptografar, conectar ou criar proxy | QEMU noVNC, QEMU terminal e LXC terminal com cliente Proxmox sob responsabilidade explícita |
| `WS /ws` e `WS /ws/virtual-machines` | Código 1008 antes de autenticação e provedores com efeitos | Autenticação limitada por tempo antes de tokens, sessões, coletores e reconciliação de tag |

A resposta HTTP é `{"detail":"Interactive execution is unavailable."}`.
A recusa pode anteceder autenticação e validação do corpo porque não concede
acesso; não comprova a validade da chave apresentada. O WebSocket contador,
inventário independente e consulta exclusiva da chave pública mantêm contratos
separados. Métodos diferentes de GET nas rotas geradas `/proxmox/api2/*`
continuam proibidos nos dois modos, sem exceção genérica por lease ou flag.

O limite ASGI por caminho exato antecede a resolução de dependências. Os
serviços também exigem admissão ativa sob responsabilidade do runtime. Nos
WebSockets de sincronização, o resolvedor FastAPI fixado executa a dependência
de autenticação da rota antes do grafo existente de provedores. Testes ASGI
reais devem continuar passando ao atualizar dependências; um wrapper com
subdependências antecipadas de clientes não é equivalente.

## Responsabilidade e encerramento

Cada worker acompanha a requisição ou WebSocket completo: autenticação,
aquisição de credenciais, conexão SSH, criação do PTY, tarefas de relay e
limpeza. A quiescência local é irreversível durante o processo: recusa novas
admissões, cancela operações ativas, impede novos envios de entrada, resize e
saída e remove somente seus próprios tickets SSH pendentes e referências inline.
Excluir um ticket não revoga uma sessão ativa. Liberar referências Python não
significa apagar a memória de forma segura.

Uma aquisição continua sob responsabilidade explícita mesmo quando o chamador
é cancelado. O recurso recebido posteriormente é fechado, nunca entregue.
A limpeza suporta cancelamentos repetidos e possui prazo limitado; trabalho
residual continua contabilizado e marca `remote_outcome_unknown` de forma
persistente no processo. O encerramento SSH aguarda após terminate e, quando
necessário, após kill. Fechar o transporte local não comprova rollback remoto.
Criar um proxy de console concede capacidade futura de escrita interativa,
mesmo sem alterar a configuração do guest. Quiescência durante uma criação
ambígua registra incerteza e não entrega a capacidade privada.

O lifespan realiza a quiescência local antes de descartar o banco. Isso não
é um protocolo de ativação de toda a frota nem autorização para desconectar
sessões reais. Reiniciar um worker não revoga processos SSH ou tickets que
outro worker legado do NMS já possui.

A sincronização legada também mantém cada aquisição Proxmox individualmente,
antes que a dependência compartilhada registre seu encerramento normal. Uma
falha paralela não pode abandonar um cliente já adquirido; um conector tardio
permanece sob responsabilidade até encerrar ou tornar-se limpeza explicitamente
pendente. O encerramento resolve as tarefas de aquisição antes dos clientes
registrados; uma pilha de contextos isolada não é suficiente. Chamadas de
inventário independentes preservam seu encerramento e não exigem admissão
interativa.

## Status local e transição agregada

`GET /execution-policy` usa a autenticação local existente, sem provedores
gerenciados e sem contabilizar a consulta como sessão interativa. O schema
fechado e sem segredos contém:

| Campo | Significado |
|---|---|
| `component` | Valor fixo `proxbox-api` |
| `capability` | Contrato fixo `interactive-rpc-boundary-v1` |
| `mode`, `generation` | Política fixada no processo e geração, ou geração nula |
| `quiescing` | Estado local irreversível de encerramento |
| `active`, `cleanup_active` | Operações e limpezas residuais locais |
| `remote_outcome_unknown` | Incerteza local mantida mesmo após os recursos deixarem o registro |
| `local_ready` | Somente este limite interativo: RPC-only, geração presente, sem quiescência, operações, limpeza ou incerteza |
| `aggregate_ready` | Sempre falso; não certifica outros componentes ou a frota |

O NMS usa `NMS_PROXBOX_EXECUTION_MODE` e a mesma
`PROXBOX_EXECUTION_GENERATION`, mas responde por seu consumidor Redis e relay.
O coordenador ainda precisa comprovar capacidades publicadas compatíveis,
gerações iguais, todos os caminhos de entrada e chamadores e a situação de
cada worker legado e sessão ativa. Componentes ausentes, workers desconhecidos,
efeitos remotos não resolvidos e rollout parcial mantêm o estado indisponível.
Reiniciar um processo não resolve incerteza anterior da frota. Não restaure
automaticamente o modo permissivo.

Este componente não implementa revogação Redis do NMS, admissão do plugin antes
da persistência, quarentena de filas de sincronização, o catálogo completo de
operações RPC ou o coordenador agregado. São pré-requisitos explícitos da
integração principal. Nenhuma versão mínima já publicada é apresentada como
suficiente para essas capacidades novas.

## Verificação

Os testes nativos e ASGI estão em `tests/test_interactive_policy.py`,
`tests/test_interactive_boundary.py`, `tests/test_interactive_ssh_lifetime.py`,
`tests/test_interactive_console_lifetime.py`, `tests/test_interactive_provider_lifetime.py`,
`tests/test_ssh_terminal.py` e `tests/proxmox/test_console_route.py`. Usam estado
local descartável, incluindo transporte AsyncSSH real em loopback. A cobertura
completa mantém o limite de 65.40% incluindo branches e o escopo separado das
rotas geradas. Dispensa de CI hospedado não dispensa testes locais, mutações de
segurança, complexidade ou revisão independente.
