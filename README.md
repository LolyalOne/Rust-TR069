# ⚡ Rust-TR069 / TR-369 USP Next-Gen ACS
### *Carrier-Grade, Event-Driven Auto Configuration Server operando sobre MQTT & PostgreSQL Híbrido*

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/Rust-2021_Edition-orange.svg?logo=rust)](https://www.rust-lang.org/)
[![Tokio](https://img.shields.io/badge/Runtime-Tokio_Async-blueviolet.svg)](https://tokio.rs/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI_Async-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL_Hybrid-336791.svg?logo=postgresql)](https://www.postgresql.org/)
[![MQTT](https://img.shields.io/badge/Broker-Eclipse_Mosquitto-red.svg?logo=eclipse-mosquitto)](https://mosquitto.org/)
[![Docker](https://img.shields.io/badge/Orchestration-Docker_Compose-2496ED.svg?logo=docker)](https://www.docker.com/)
[![Protobuf](https://img.shields.io/badge/BBF_TR--369-1.3_Compliant-blue.svg)](https://www.broadband-forum.org/)
[![Tests](https://img.shields.io/badge/Tests-100%25_Passed-brightgreen.svg)](#-baterias-de-testes-unitários-e-de-integridade)

---

## 🌟 Visão do Projeto: A Nova Fronteira do Gerenciamento de Redes e ISPs

O gerenciamento tradicional de dispositivos de telecomunicações (CPEs, ONUs, OLTs, Roteadores Wi-Fi 6) esteve preso por mais de uma década às limitações do protocolo legado **TR-069 (CWMP)**: sessões SOAP/XML pesadas sobre HTTP síncrono, alto consumo de banda, polling ineficiente e latência inaceitável para tomada de decisão em tempo real.

O **Rust-TR069 (USP ACS)** nasce para quebrar esse paradigma. Projetado do zero sob a especificação **TR-369 (User Services Platform - USP)** da *Broadband Forum (BBF)*, este sistema introduz uma **Arquitetura Orientada a Eventos (Event-Driven)** ultrarrápida, resiliente e escalável sobre o broker **MQTT**:

* ⚡ **De Segundos para Milissegundos:** Telemetria em tempo real compactada em **Protocol Buffers (Protobuf)** trafegando via tópicos MQTT.
* 🛡️ **Segurança de Memória e Concorrência Extrema:** O núcleo de processamento MTP é forjado em **Rust puro** com runtime assíncrono Tokio, canal MPSC de desacoplamento e isolamento com backpressure.
* 💾 **Engenharia de Dados Híbrida (RAM + Disco):** Ingestão de milhões de métricas voláteis em tabelas de memória ultrarrápidas (`UNLOGGED` em `tmpfs`), reconciliando com persistência em disco apenas sob variações ópticas significativas ($> 1.0	ext{ dBm}$), com zero amplificação de escrita WAL na tabela de inventário.
* 🌐 **SDN / Controller-Agent Desacoplado:** Controle imperativo de CPEs via API RESTful assíncrona (FastAPI) com latência sub-milissegundo para automações com n8n, CRMs e painéis de operação.
* 🎛️ **Eficiência de Recursos Sem Precedentes:** Projetado para rodar com contenção física rigorosa de memória (**WSL 2 com 6 GB de RAM** ou servidores dedicados), pronto para escalar horizontalmente para infraestruturas de telecomunicações de grande porte.

---

## 🏗️ Arquitetura de Alto Nível

```mermaid
graph TD
    subgraph "Borda / Dispositivos (CPEs)"
        CPE1["ONU / Roteador Wi-Fi (CPE 1)"]
        CPE2["ONU / Roteador Wi-Fi (CPE 2)"]
        CPEn["Dispositivo TR-369 (CPE n)"]
    end

    subgraph "Broker MTP (Eclipse Mosquitto 1883)"
        TOPIC_IN["usp/endpoint/{cpe_id}/notify<br/>usp/endpoint/{cpe_id}/telemetry"]
        TOPIC_OUT["usp/endpoint/{cpe_id}/request"]
    end

    subgraph "MTP Core Worker (Rust Tokio)"
        MQTT_CLI["Receptor Assíncrono rumqttc"]
        MPSC["Canal Tokio MPSC<br/>(Buffer Bounded: 1024)"]
        PROST["Decodificador Dual (Protobuf BBF + JSON)"]
        DB_SINK["Sink SQLx Atômico (JSONB Concatenation ||)"]
        HEALTH_RUST["Healthcheck Monitor (/tmp/healthy)"]
    end

    subgraph "PostgreSQL 15 Híbrido"
        RAM_TBL["Tier 1 (RAM tmpfs): cpe_live_state<br/>(UNLOGGED na ram_tablespace)"]
        TRIGGER["Trigger: reconcile_live_to_history<br/>(Delta Óptico > 1.0 dBm)"]
        DISK_TBL["Tier 2 (Disco): cpe_historical_metrics<br/>cpe_inventory (Persistente)"]
    end

    subgraph "Manager & SDN Controller (FastAPI)"
        API["FastAPI Async Engine (Python 3.11)"]
        GUNICORN["Gunicorn (2 Workers UvicornWorker)"]
        PUB["Publicador MQTT Assíncrono (QoS 1)"]
    end

    subgraph "Integrações Externas"
        N8N["Automação ISP (n8n / CRM / ERP)"]
        DASH["Frontend / Painel NOC / CLI"]
    end

    %% Fluxos Upstream (Telemetria)
    CPE1 -->|Protobuf TR-369 / JSON| TOPIC_IN
    CPE2 -->|Protobuf TR-369 / JSON| TOPIC_IN
    CPEn -->|Protobuf TR-369 / JSON| TOPIC_IN
    TOPIC_IN --> MQTT_CLI
    MQTT_CLI -->|Filtra /request| MPSC
    MPSC --> PROST
    PROST --> DB_SINK
    DB_SINK -->|Zero-WAL Fast Write| RAM_TBL
    RAM_TBL -->|Disparo sob Delta > 1.0 dBm| TRIGGER
    TRIGGER -->|Gravação Histórica (Sem Update em Inventory)| DISK_TBL

    %% Fluxos Downstream (Comandos)
    DASH -->|POST /api/v1/cpes/{id}/reboot| API
    N8N -->|POST /api/v1/cpes| API
    API -->|Consulta Microsegundos em RAM| RAM_TBL
    API --> GUNICORN
    GUNICORN --> PUB
    PUB -->|Comando TR-369 Reboot| TOPIC_OUT
    TOPIC_OUT -->|Execução Remota| CPE1
```

---

## 🧩 Os 4 Pilares da Engenharia do Sistema

### 1. Banco de Dados Híbrido Dual-Tier (PostgreSQL 15+)
* **Tablespace Top-Level em RAM (`ram_tablespace`):** Montagem dedicada em volume `tmpfs` montado em `/var/lib/postgresql/ram_data` (com `uid=70, gid=70` para PostgreSQL Alpine). O comando `CREATE TABLESPACE ram_tablespace` é executado no nível superior, fora de blocos transacionais.
* **Tabela Volátil In-RAM (`cpe_live_state`):** Criada como `UNLOGGED` alocada diretamente na `ram_tablespace`. Bypassa completamente o *Write-Ahead Logging* (WAL), permitindo taxas de ingestão de dezenas de milhares de mensagens por segundo com latência de escrita em microssegundos.
* **Tabela de Inventário Confiável (`cpe_inventory`):** Armazenamento relacional durável em disco com chaves únicas (`serial_number`), metadados de fabricante, modelo, OUI (até 6 caracteres) e versões de hardware/software.
* **Trigger Inteligente de Reconciliação (`reconcile_live_to_history`):** Função PL/pgSQL disparada em operações de `INSERT` ou `UPDATE` na tabela `cpe_live_state`. Analisa variações de atenuação de sinal óptico ($|	ext{NEW} - 	ext{OLD}| > 1.0	ext{ dBm}$) e grava snapshots auditáveis na tabela persistente `cpe_historical_metrics`.
* **Zero WAL Write Amplification:** A trigger nunca executa `UPDATE` na tabela persistente `cpe_inventory` durante o fluxo de telemetria volátil, eliminando por completo a amplificação de I/O em disco. Uma view de compatibilidade retroativa (`cpe_state_history`) garante interoperabilidade com queries legadas.

### 2. MTP Ingest Engine em Rust (`rust-core`)
* **Decodificação Dual Resiliente (`PayloadDecoder`):** Compatibilidade total com os schemas oficiais **TR-369 1.3 BBF Protocol Buffers** (`Record`, `NoSessionContextRecord`, `Msg`, `Notify`, `Operate` via `prost`) com chaveamento dinâmico e fallback transparente para payloads **JSON** (utilizados por simuladores de teste como o `simulate_flow.sh`).
* **Canal Tokio MPSC com Bounded Backpressure (1024 mensagens):** Desacopla estritamente a thread de rede MQTT do loop de persistência no PostgreSQL. As mensagens são enfileiradas com timeout de guarda (500 ms) para impedir que lentidões transientes no banco bloqueiem o *keep-alive* do MQTT (`PINGREQ`).
* **Assinatura Rumqttc e Prevenção de Loops de Feedback:** Assina `usp/endpoint/#` com QoS 1 e descarta ativamente tópicos terminados em `/request` (comandos do Controller), evitando auto-ingestão e loops infinitos de mensagens.
* **Upsert Atômico com Concatenação JSONB (`||`):** Atualiza `current_parameters` e `telemetry_metrics` incrementalmente utilizando o operador nativo `||` do PostgreSQL, preservando parâmetros existentes reportados em mensagens anteriores. Auto-provisiona dispositivos não registrados previamente (`ON CONFLICT (cpe_id) DO NOTHING`).
* **Footprint Mínimo e Multi-Stage Build:** Compilado em container Alpine multi-stage (`rust:alpine` $
ightarrow$ `alpine:latest`), gerando um executável enxuto de ~3.9 MB com consumo em execução **menor que 30 MB de RAM**.
* **Monitoramento de Saúde (`/tmp/healthy`):** Tarefa assíncrona dedicada que sonda periodicamente o broker Mosquitto e executa `SELECT 1` no banco, mantendo o arquivo de healthcheck atualizado para o Docker.

### 3. Manager API & SDN Controller (`python-api`)
* **Stack Assíncrona de Alta Performance:** Construída sobre Python 3.11, **FastAPI** e **SQLAlchemy 2.0 Assíncrono** com driver nativo **`asyncpg`**.
* **Isolamento de Processos via Gunicorn:** Executado com 2 workers assíncronos baseados em `uvicorn.workers.UvicornWorker` e reciclagem de requisições (`max_requests = 1000`) para garantia estrita de contenção física de memória abaixo de 1.0 GB.
* **Endpoints RESTful Padronizados:**
  * `POST /api/v1/cpes`: Cadastro e re-registro idempotente de dispositivos no inventário (tratamento de conflito de serial com HTTP 409).
  * `GET /api/v1/cpes`: Listagem paginada de dispositivos com filtros opcionais de status.
  * `GET /api/v1/cpes/{cpe_id}`: Recuperação detalhada dos metadados autoritativos do CPE.
  * `PUT / PATCH /api/v1/cpes/{cpe_id}`: Atualização segura de campos de inventário (com validação estrita de tamanho de OUI $\le 6$).
  * `DELETE /api/v1/cpes/{cpe_id}`: Remoção com deleção em cascata (`cpe_live_state` e `cpe_historical_metrics`).
  * `GET /api/v1/cpes/{cpe_id}/live-state` (e alias `/live`): Leitura em microssegundos direto da memória RAM.
  * `GET /api/v1/cpes/{cpe_id}/history`: Consulta paginada do histórico auditável de métricas geradas pela trigger.
  * `POST /api/v1/cpes/{cpe_id}/reboot`: Injeção instantânea de comando TR-369 via MQTT no tópico `usp/endpoint/{cpe_id}/request` com QoS 1.
  * `GET /health` e `GET /api/v1/health`: Verificação de saúde da API e conectividade com banco de dados.

### 4. Infraestrutura como Código, Limites Rígidos & DevContainer
* **Perfil de Recursos Rígido (Ambientes Restritos ou Servidores):**
  * PostgreSQL: **1.5 GB** (com `tmpfs` de 1 GB para RAM tablespace)
  * Eclipse Mosquitto: **500 MB**
  * Rust USP Core: **500 MB** (consumo real < 30 MB)
  * Python FastAPI: **1.0 GB**
  * Host / Margem Livre: **~2.5 GB** em estações com 6 GB totais.
* **DevContainer Homologado (`.devcontainer/devcontainer.json`):** Configuração pronta para VS Code com extensões completas para Rust (`rust-analyzer`), Python (`Pylance`), Docker e TOML.
* **Configurador Dinâmico de Limites (`configure_limits.py`):** Aplicativo CLI interativo e com suporte a argumentos de linha de comando para ajustar com segurança e persistir os limites de RAM no `docker-compose.yml` ao migrar entre estações de desenvolvimento e servidores dedicados.

---

## 📂 Estrutura do Repositório

```text
.
├── .agents/                               # Documentação de governança, relatórios de handoff e auditorias
├── .devcontainer/
│   └── devcontainer.json                  # Ambiente padronizado VS Code (Rust, Python, Docker)
├── mosquitto/
│   └── mosquitto.conf                     # Configuração do broker MQTT (listener 1883, persistência)
├── postgres/
│   ├── init.sql                           # DDL com ram_tablespace em tmpfs, unlogged live_state e trigger
│   ├── test_schema.py                     # Suíte de testes DDL, integridade e simulação de triggers (20 testes)
│   └── test_reconciliation_empirical.py  # Testes empíricos de reconciliação de sinal óptico (23 testes)
├── rust-core/                             # Núcleo de ingestão assíncrono em Rust
│   ├── Cargo.toml                         # Dependências (tokio, rumqttc, sqlx, prost, chrono)
│   ├── build.rs                           # Compilação de schemas Protobuf via prost-build
│   ├── Dockerfile                         # Build multi-stage Alpine (< 30 MB RAM)
│   ├── proto/
│   │   └── usp.proto                      # Schemas BBF TR-369 1.3 (Record, Msg, Notify, Operate)
│   └── src/
│       └── main.rs                        # Engine assíncrono, decodificador dual, MPSC e upsert SQLx
├── python-api/                            # Manager REST e Controller MQTT em FastAPI
│   ├── Dockerfile                         # Imagem container enxuta Python 3.11-slim
│   ├── requirements.txt                   # Dependências (FastAPI, SQLAlchemy v2, asyncpg, aiomqtt)
│   ├── gunicorn_conf.py                   # Configuração de isolamento com 2 workers Uvicorn
│   ├── app/
│   │   ├── main.py                        # Ponto de entrada FastAPI e ciclo de vida (lifespan)
│   │   ├── config.py                      # Configurações centralizadas via pydantic-settings
│   │   ├── database.py                    # Engine e sessões assíncronas asyncpg
│   │   ├── models.py                      # Modelos declarativos SQLAlchemy 2.0
│   │   ├── schemas.py                     # Schemas Pydantic v2 para validação e serialização
│   │   ├── mqtt.py                        # Cliente MQTT assíncrono para injeção de comandos
│   │   └── routers/
│   │       ├── cpes.py                    # Endpoints de inventário, live-state, history e reboot
│   │       └── health.py                  # Endpoint de monitoramento de integridade
│   └── tests/
│       ├── test_api.py                    # Bateria de testes de endpoints e ciclo de vida
│       └── test_adversarial.py            # Testes de resiliência a falhas, concorrência e bordas (20 testes)
├── configure_limits.py                    # Utilitário CLI para configuração de cotas de memória
├── simulate_flow.sh                       # Script de simulação e verificação automatizada E2E (5 etapas)
├── docker-compose.yml                     # Orquestração com limites de memória, tmpfs e healthchecks
└── README.md                              # Documentação principal do projeto
```

---


## ⚙️ Configuração do Ambiente (Variáveis e Credenciais)

O sistema foi desenhado para rodar *"out-of-the-box"* com configurações padrão para facilitar o desenvolvimento. No entanto, para ambientes de produção ou customizados, você deve configurar as credenciais do Banco de Dados e do broker MQTT.

A API em Python utiliza o `pydantic-settings` e suporta carregamento automático de variáveis via arquivo `.env`. O `docker-compose.yml` também herda essas variáveis se declaradas localmente.

### Principais Variáveis Disponíveis

| Variável | Valor Padrão (Desenvolvimento) | Descrição |
|----------|--------------------------------|-----------|
| `POSTGRES_DB` | `acs_db` | Nome do banco de dados |
| `POSTGRES_USER` | `acs_user` | Usuário do banco PostgreSQL |
| `POSTGRES_PASSWORD` | `acs_password` | Senha do banco PostgreSQL |
| `DATABASE_URL` | `postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db` | URL de conexão completa usada pelas aplicações (Rust e Python) |
| `MQTT_HOST` | `mosquitto` | Hostname ou IP do broker MQTT |
| `MQTT_PORT` | `1883` | Porta de comunicação do MQTT |
| `DEBUG` | `False` | Habilita logs mais detalhados na API FastAPI |

### Como Alterar as Configurações

1. Crie um arquivo `.env` na raiz do projeto (mesmo diretório do `docker-compose.yml`):
   ```env
   POSTGRES_USER=admin_isp
   POSTGRES_PASSWORD=senha_super_segura
   DATABASE_URL=postgresql+asyncpg://admin_isp:senha_super_segura@postgres:5432/acs_db
   ```
2. Caso altere as credenciais, lembre-se de atualizar os valores correspondentes dentro do `docker-compose.yml` nas seções `environment` de cada serviço, ou utilize a interpolação do próprio Docker Compose (ex: `POSTGRES_USER=${POSTGRES_USER}`).

## 🚀 Guia de Execução e Testes

### Pré-requisitos
* [Docker](https://docs.docker.com/get-docker/) e Docker Compose v2 instalados.
* [Python 3.10+](https://www.python.org/) e [Rust](https://www.rust-lang.org/) (para desenvolvimento local fora de containers).
* [Git](https://git-scm.com/) instalado.

---

### 1. Inicializar a Stack Completa em Containers

Clone o repositório e inicie todos os serviços com build determinístico:

```bash
git clone https://github.com/LolyalOne/Rust-TR069.git
cd Rust-TR069

# Inicializa Postgres, Mosquitto, Rust Core e FastAPI com healthchecks em cascata
docker compose up -d --build
```

Aguarde alguns instantes até que todos os 4 containers alcancem o status `healthy`:
```bash
docker compose ps
```

---

### 2. Executar a Simulação Ponta a Ponta Automatizada (E2E)

O script `simulate_flow.sh` valida de forma 100% autônoma o ciclo de vida completo do ACS TR-369:

```bash
chmod +x simulate_flow.sh
./simulate_flow.sh
```

#### O script executa 5 passos de validação estrita:
1. **Registro de CPE via API:** Executa `POST /api/v1/cpes` e confirma a gravação na `cpe_inventory`.
2. **Publicação de Telemetria TR-369 via MQTT:** Injeta payload de telemetria no broker Mosquitto.
3. **Consumo pelo Rust Core & Gravação em RAM:** Valida a decodificação da mensagem pelo worker Rust e o upsert na tabela volátil `cpe_live_state` na `ram_tablespace`.
4. **Variação Métrica & Trigger de Reconciliação:** Publica alteração métrica com delta óptico $> 1.0	ext{ dBm}$ e valida a persistência do evento na tabela histórica `cpe_historical_metrics`.
5. **Injeção Reversa de Comando:** Dispara `POST /api/v1/cpes/{id}/reboot` via FastAPI e confirma com um subscriber que o comando TR-369 foi publicado com sucesso no tópico MQTT `usp/endpoint/{cpe_id}/request`.

---

### 3. Baterias de Testes Unitários e de Integridade

Todas as camadas do projeto possuem suítes de testes automatizados dedicadas:

#### A. Testes do Banco de Dados PostgreSQL
Validação de integridade DDL, constraints, isolamento da tablespace em RAM, ausência de blocos transacionais incorretos e simulação empírica da trigger de reconciliação óptica:

```bash
python3 postgres/test_schema.py && python3 postgres/test_reconciliation_empirical.py
```
*(Resultado: 43 testes executados, 43 aprovados).*

#### B. Testes do Core Worker em Rust
Verificação de decodificação wire-compatible Protobuf TR-369 1.3, decodificação JSON, tolerância a falhas, payloads malformados, filtragem de tópicos e isolamento de loops:

```bash
cd rust-core
cargo test
cd ..
```
*(Resultado: 19 testes unitários e adversariais executados, 19 aprovados).*

#### C. Testes da Manager API em Python FastAPI
Testes funcionais de rotas CRUD, concorrência, paginação, tolerância a indisponibilidade do broker MQTT (HTTP 503) e tratamento de conflitos de integridade (HTTP 409):

```bash
PYTHONPATH=python-api python3 -m pytest -v python-api/tests/
```
*(Resultado: 20 testes executados, 20 aprovados).*

---

### 4. Gerenciamento Dinâmico de Limites de RAM

Para visualizar ou reconfigurar as cotas de memória física do `docker-compose.yml` de forma segura e validada:

```bash
# Modo interativo com menu
python3 configure_limits.py

# Ou visualizar as cotas atuais diretamente via CLI
python3 configure_limits.py --show

# Ou definir limites específicos por parâmetro
python3 configure_limits.py --set-limit postgres 2G --set-limit rust-core 1G
```

---

## 🗺️ Roadmap de Desenvolvimento

- [x] **Milestone 1 — Infraestrutura, Setup & Portabilidade**
  - [x] Definição de limites físicos de memória no `docker-compose.yml` (Postgres 1.5G, Mosquitto 500M, Rust 500M, FastAPI 1G).
  - [x] Configuração de volume `tmpfs` dedicado para a montagem de dados em memória do PostgreSQL (`uid=70, gid=70`).
  - [x] Healthchecks integrados e robustos para todos os 4 serviços garantindo inicialização determinística.
  - [x] Configuração completa do **DevContainer** (`.devcontainer/devcontainer.json`) com suporte a Rust, Python e Docker.
  - [x] Aplicativo CLI interativo e programático `configure_limits.py` para escalabilidade de hardware.
  - [x] Script de simulação e integração contínua `simulate_flow.sh` cobrindo os 5 passos fundamentais.

- [x] **Milestone 2 — Banco de Dados Híbrido (Dual-Tier PostgreSQL)**
  - [x] Criação de tablespace de memória (`ram_tablespace`) em comando top-level fora de blocos de transação.
  - [x] Tabela persistente autoritativa `cpe_inventory` com índices otimizados para busca e metadados.
  - [x] Tabela volátil ultrarrápida `cpe_live_state` (`UNLOGGED`) em RAM para ingestão de telemetria sem WAL.
  - [x] Trigger de reconciliação `reconcile_live_to_history` com disparo estrito sob variação óptica $> 1.0	ext{ dBm}$.
  - [x] Persistência direta em `cpe_historical_metrics` com **zero amplificação de escrita WAL** na tabela de inventário.
  - [x] View de compatibilidade retroativa `cpe_state_history`.
  - [x] Validação automatizada completa com 43 testes em `test_schema.py` e `test_reconciliation_empirical.py`.

- [x] **Milestone 3 — Rust USP Core Worker**
  - [x] Mapeamento oficial dos schemas Protocol Buffers do TR-369 1.3 BBF (`Record`, `Msg`, `Notify`, `Operate`).
  - [x] Decodificador dual (`PayloadDecoder`) com chaveamento dinâmico entre binário Protobuf BBF e payloads JSON.
  - [x] Pipeline assíncrono com canal Tokio MPSC (bounded buffer: 1024) desacoplando ingestão MQTT de gravações no banco.
  - [x] Filtragem de tópicos MQTT (`is_command_topic`) prevenindo loops de feedback em tópicos `/request`.
  - [x] Upsert atômico dinâmico via SQLx com operador de concatenação JSONB (`||`) para preservação de parâmetros.
  - [x] Auto-provisionamento de inventário sob telemetria de novos dispositivos (`ON CONFLICT DO NOTHING`).
  - [x] Tarefa de monitoramento de saúde gerando `/tmp/healthy` para o healthcheck do container.
  - [x] Dockerfile multi-stage Alpine com footprint em execução inferior a 30 MB de RAM.
  - [x] Suíte com 19 testes unitários e testes adversariais aprovados com 100% de sucesso.

- [x] **Milestone 4 — Python FastAPI Manager & SDN Controller**
  - [x] API RESTful assíncrona em Python 3.11 com FastAPI e SQLAlchemy 2.0 (`asyncpg`).
  - [x] Isolamento de memória e concorrência via Gunicorn com 2 workers `uvicorn.workers.UvicornWorker`.
  - [x] Endpoints CRUD completos para `cpe_inventory` com validação Pydantic v2 e controle de conflito de serial (HTTP 409).
  - [x] Endpoint de consulta de telemetria em tempo real direto da RAM (`GET /api/v1/cpes/{cpe_id}/live-state`).
  - [x] Endpoint de histórico e auditoria de métricas ópticas (`GET /api/v1/cpes/{cpe_id}/history`).
  - [x] Disparador de comandos remotos TR-369 via MQTT (`POST /api/v1/cpes/{cpe_id}/reboot`) com QoS 1.
  - [x] Tratamento gracioso de indisponibilidade de broker MQTT com retorno HTTP 503.
  - [x] Suíte com 20 testes automatizados cobrindo rotas, paginação, deleção em cascata e resiliência adversarial.

- [x] **Milestone 5 — Orquestração de Containers & Simulação E2E Concluída**
  - [x] Orquestração homogênea dos 4 containers com inicialização determinística e limites físicos ativos.
  - [x] Certificação completa do script de simulação `simulate_flow.sh` com código de saída 0.
  - [x] Homologação das interfaces entre Mosquitto, Rust Core, PostgreSQL e FastAPI Manager.
  - [x] Documentação técnica exaustiva e padronização para distribuição open-source.
  - [ ] *(Extensão Futura)*: Interface gráfica NOC em tempo real (React / Vite / Tailwind) com visualização topológica de rede.
  - [ ] *(Extensão Futura)*: Webhooks para integrações de faturamento e automações avançadas no **n8n**.

---

## 📚 Referências & Padrões
* [Broadband Forum TR-369 (USP)](https://www.broadband-forum.org/technical/download/TR-369.pdf) — *User Services Platform Architecture & Protocol*.
* [Broadband Forum TR-181](https://usp-data-models.broadband-forum.org/) — *Device Data Model for TR-069/TR-369*.
* [GenieACS](https://github.com/genieacs/genieacs) — *Referência open-source para tratamento de particularidades de CPEs legados*.
* [obuspa (Open Broadband USP Agent)](https://github.com/BroadbandForum/obuspa) — *Agente de referência oficial da Broadband Forum*.

---

<p align="center">
  <b>Construído para transformar a operação de ISPs com velocidade, confiabilidade e engenharia de software de ponta.</b><br>
  Desenvolvido com o poder da concorrência segura em <b>Rust</b> e a agilidade assíncrona de <b>Python</b>.
</p>
