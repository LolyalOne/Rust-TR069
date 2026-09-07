# Dispatch Record

## 2026-09-07T14:22:15Z

Você é o Project Orchestrator (Generation 4) encarregado de orquestrar e entregar a Refatoração Dual-Stack TR-069 Clássico e TR-369 para o projeto Rust-TR069.

### Informações de Ambiente e Identidade
- Seu diretório de trabalho: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/`
- Workspace do projeto: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069`
- Arquivo de requisições original: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (veja a seção mais recente: `## Follow-up — 2026-09-07T14:20:33Z`)

### Objetivo do Projeto (Dual-Stack TR-069 Clássico + TR-369 USP)
O sistema atual já opera com sucesso em USP/MQTT (TR-369). O objetivo agora é torná-lo Dual-Stack para suportar nativamente roteadores de nível de consumo (ex: ONTs Huawei EchoLife, TP-Link EX) via TR-069 Clássico (CWMP sobre HTTP/XML na porta 7547).

### Requisitos a Entregar:
1. **R1. Servidor HTTP (CWMP) no Rust Core**: Modificar `rust-core/src/main.rs` para rodar, junto com o cliente MQTT (Tokio), um servidor web embarcado (usando `axum` ou `actix-web`) escutando na porta `7547` (Padrão TR-069).
2. **R2. Parsing de XML/SOAP (TR-069 Inform)**: Receber requisições `POST` das ONTs legadas contendo XML/SOAP (`<SOAP-ENV:Envelope>`, `<cwmp:Inform>`), extraindo metadados principais (Número de Série, Fabricante, e parâmetros TR-181) com biblioteca rápida de XML (ex: `roxmltree` ou `quick-xml`).
3. **R3. Convergência MPSC**: Dados extraídos do XML (TR-069) devem convergir para a **mesma fila MPSC** que já processa as mensagens do MQTT (TR-369), mantendo o sink no PostgreSQL com dados homogeneizados.
4. **R4. Intercomunicação (FastAPI -> Postgres -> Rust)**: Como o TR-069 legado opera por polling das ONTs, permitir comandos pendentes (ex: `GetParameterValues`, `Reboot`) inseridos pela `python-api` via tabela do Postgres ou API HTTP direta para o Rust, entregando os comandos nas respostas HTTP/XML às ONTs.
5. **R5. Docker Compose**: Adicionar a exposição da porta `7547:7547` no serviço `rust-core` do `docker-compose.yml`.

### Critérios de Aceite:
- `rust-core` compila sem erros com as novas dependências.
- Contêiner Rust sobe com sucesso expondo a porta `7547` além da conexão MQTT.
- Teste com `curl` simulando payload XML de Inform da Huawei EchoLife na porta `7547` resulta no salvamento correto dos dados em `cpe_live_state` no PostgreSQL.
- Todos os testes unitários anteriores e o script `simulate_flow.sh` continuam funcionando para a parte MQTT (sem regressão).

### Protocolo de Coordenação:
- Crie e mantenha seu `BRIEFING.md` e `progress.md` em `.agents/orchestrator_4/`.
- Decomponha as tarefas, spawne subagentes especializados conforme as regras do framework Teamwork, supervisione os quality gates (reviewers, challengers, auditors por milestone/frente) e registre os relatórios.
- Ao concluir todos os critérios de aceite e testes ponta a ponta, reporte vitória detalhada com as evidências para o Sentinel para que a auditoria independente (Victory Auditor) seja acionada.
